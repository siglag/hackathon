import time

from fastapi.testclient import TestClient

from app import db
from app.evidence import policy
from app.main import app


def _client():
    c = TestClient(app)
    c.__enter__()
    return c


def _login(c, username="doc", setup=False):
    if setup:
        r = c.post("/api/auth/setup", json={"username": username, "password": "correct horse battery"})
    else:
        r = c.post("/api/auth/login", json={"username": username, "password": "correct horse battery"})
    assert r.status_code == 200, r.text
    c.headers["x-csrf-token"] = r.json()["csrf"]
    return c


def _wait(c, job_id, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/api/jobs/{job_id}").json()
        if j["state"] in {"done", "failed", "cancelled", "interrupted"}:
            return j
        time.sleep(0.2)
    raise AssertionError("job timeout")


c = _client()
_login(c, setup=True)


def test_auth_and_csrf_required():
    anon = TestClient(app)
    assert anon.get("/api/cases").status_code == 401
    r = c.post("/api/cases", json={"label": "x"}, headers={"x-csrf-token": "wrong"})
    assert r.status_code == 403
    r = c.post("/api/cases", json={"label": "x"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_upload_csv_transform_export_and_isolation():
    case = c.post("/api/cases", json={"label": "Case A"}).json()
    r = c.post(f"/api/cases/{case['id']}/uploads", files={"files": ("labs.csv", b"test,value,date\nHb,13.2,2026-01-02\nGlu,5.8,2026-01-01\n", "text/csv")},
               headers={"idempotency-key": "k1"})
    assert r.status_code == 200, r.text
    doc = r.json()["documents"][0]
    again = c.post(f"/api/cases/{case['id']}/uploads", files={"files": ("labs.csv", b"x", "text/csv")}, headers={"idempotency-key": "k1"})
    assert again.json() == r.json()  # idempotent
    assert _wait(c, doc["job_id"])["state"] == "done"
    items = c.get(f"/api/cases/{case['id']}/items").json()
    ds = items["datasets"][0]
    t = c.post(f"/api/datasets/{ds['id']}/transform", json={"ops": [{"op": "sort", "column": "date"}]}).json()
    rows = c.get(f"/api/datasets/{t['id']}/rows").json()["rows"]
    assert [x["cells"]["test"]["v"] for x in rows] == ["Glu", "Hb"]
    a = c.post(f"/api/datasets/{t['id']}/artifacts", json={"format": "xlsx"}).json()
    dl = c.get(f"/api/artifacts/{a['artifact_id']}/download")
    assert dl.status_code == 200 and dl.content[:2] == b"PK"

    # A second doctor account cannot exist, but a phone paired to another case must not see this one.
    other = c.post("/api/cases", json={"label": "Case B"}).json()
    pr = c.post("/api/pairings", json={"case_id": other["id"]}).json()
    phone = TestClient(app)
    ex = phone.post("/api/pairings/exchange", json={"token": pr["token"]})
    assert ex.status_code == 200
    assert phone.post("/api/pairings/exchange", json={"token": pr["token"]}).status_code == 410  # single use
    phone.headers["x-csrf-token"] = phone.cookies.get("ma_csrf")
    up = phone.post("/api/phone/uploads", files={"files": ("p.jpg", b"\xff\xd8\xff", "image/jpeg")})
    assert up.status_code == 403  # not yet confirmed on the workstation
    assert c.post(f"/api/pairings/{pr['pairing_id']}/confirm").status_code == 200
    for path in (f"/api/cases/{case['id']}/items", f"/api/datasets/{ds['id']}", f"/api/artifacts/{a['artifact_id']}/preview",
                 f"/api/jobs/{doc['job_id']}/events", f"/api/conversations/{case['conversation_id']}/messages"):
        assert phone.get(path).status_code == 401, path
    c.post(f"/api/pairings/{pr['pairing_id']}/revoke")
    assert phone.get("/api/phone/session").status_code == 401

    d = c.delete(f"/api/cases/{case['id']}")
    assert d.status_code == 200
    assert db.q1("SELECT COUNT(*) n FROM search_fts WHERE case_id=?", (case["id"],))["n"] == 0
    assert c.get(f"/api/artifacts/{a['artifact_id']}/download").status_code == 404


def test_evidence_approval_binds_to_exact_request():
    from app import pipeline
    case = c.post("/api/cases", json={"label": "Case C"}).json()
    q = pipeline.create_evidence_query(case["id"], case["conversation_id"], None,
                                       {"concepts": ["type 2 diabetes", "metformin"], "population": "adults"})
    assert q["status"] == "pending_approval"
    other_hash = policy.request_hash({**q["request"], "population": None})
    assert c.post(f"/api/evidence-queries/{q['id']}/approve", json={"request_hash": other_hash}).status_code == 409
    rev = c.post(f"/api/evidence-queries/{q['id']}/revise", json={"concepts": ["type 2 diabetes"]}).json()
    assert rev["id"] != q["id"]
    assert c.post(f"/api/evidence-queries/{q['id']}/approve", json={"request_hash": q["request_hash"]}).status_code == 409  # superseded

    blocked = pipeline.create_evidence_query(case["id"], case["conversation_id"], None, {"concepts": ["Case C"]})
    assert blocked["status"] in {"needs_revision", "blocked"}
    assert c.post(f"/api/evidence-queries/{blocked['id']}/approve", json={"request_hash": blocked["request_hash"]}).status_code == 409


def test_gateway_rejects_unapproved_or_changed_requests():
    from app.gateway_service import app as gw
    g = TestClient(gw)
    norm = policy.evaluate(policy.EvidenceRequest(concepts=["type 2 diabetes"])).request
    h = policy.request_hash(norm)
    assert g.post("/v1/search", json={"request": norm, "request_hash": h}).status_code == 401
    k = {"X-Gateway-Key": "test-secret"}
    changed = {**norm, "concepts": ["metformin", "type 2 diabetes"]}
    assert g.post("/v1/search", json={"request": changed, "request_hash": h}, headers=k).status_code == 409
    raw = {**norm, "concepts": ["John Smith"]}
    assert g.post("/v1/search", json={"request": raw, "request_hash": policy.request_hash(raw)}, headers=k).status_code == 422
    assert g.post("/v1/fetch", json={"request_hash": h, "pmids": ["123"]}, headers=k).status_code == 409


def test_mcp_client_refuses_non_allowlisted_hosts_and_tools():
    import pytest

    from app.evidence.mcp_client import MCPClient, MCPError
    with pytest.raises(MCPError):
        MCPClient("https://evil.example/mcp", ("pubmed.caseyjhand.com",), 5, 1000)
    with pytest.raises(MCPError):
        MCPClient("http://pubmed.caseyjhand.com/mcp", ("pubmed.caseyjhand.com",), 5, 1000)
    m = MCPClient("https://pubmed.caseyjhand.com/mcp", ("pubmed.caseyjhand.com",), 5, 1000)
    with pytest.raises(MCPError):
        m.call("pubmed_format_citations", {})


def test_status_identifies_cloud_ocr_independently_of_chat(monkeypatch):
    from dataclasses import replace
    from app import main

    monkeypatch.setattr(main, "settings", replace(main.settings, ocr_engine="openrouter"))
    monkeypatch.setattr(main.ocr, "available", lambda: (True, "openrouter:synthetic-vision"))
    monkeypatch.setattr(main.llm, "status", lambda: {"ok": True, "cloud": False, "model": "local"})
    response = c.get("/api/system/status")
    assert response.status_code == 200
    assert response.json()["ocr"] == {"ok": True, "engine": "openrouter:synthetic-vision", "cloud": True}
    assert response.json()["model"]["cloud"] is False
