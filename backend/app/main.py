"""Local application API (spec 13). Run: uvicorn app.main:app --no-access-log"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import artifacts, auth, datasets, db, jobs, netguard, pipeline, storage
from .agent import llm
from .audit import audit
from .auth import Principal, owned, require_case
from .config import settings
from .evidence import policy
from .ingest import extraction, ocr, validate
from .ingest.images import strip_metadata
from .util import dumps, loads, new_id, now, sha256_bytes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("medassist.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    jobs.recover_after_restart()
    if settings.llm_provider == "openrouter" and not settings.allow_cloud_model:
        raise RuntimeError("MEDASSIST_LLM_PROVIDER=openrouter sends case content to a cloud model; "
                           "set MEDASSIST_ALLOW_CLOUD_MODEL=1 to confirm (synthetic data only).")
    if settings.ocr_engine == "openrouter" and not settings.allow_cloud_model:
        raise RuntimeError("MEDASSIST_OCR_ENGINE=openrouter sends page images to a cloud model; "
                           "set MEDASSIST_ALLOW_CLOUD_MODEL=1 to confirm (synthetic data only).")
    if settings.network_guard:
        uses_cloud = "openrouter" in (settings.llm_provider, settings.ocr_engine)
        cloud = ["https://openrouter.ai"] if uses_cloud else []
        netguard.install(settings.ollama_url, settings.gateway_url, *cloud)
    log.info("started provider=%s model=%s", settings.llm_provider, llm.model_label())
    yield


app = FastAPI(title="Local Medical Assistant", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

CSP = ("default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; "
       "style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


@app.middleware("http")
async def security_and_logging(request: Request, call_next):
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("unhandled error")
        response = JSONResponse({"detail": "internal error"}, status_code=500)
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = response.headers.get("Cache-Control", "no-store")
    route = request.scope.get("route")
    # Route templates only: never raw paths/queries (tokens, IDs combined with names).
    log.info("%s %s %s %.0fms", request.method, getattr(route, "path", "-"), response.status_code, (time.perf_counter() - t0) * 1000)
    return response


Doctor = Depends(auth.doctor)


# ---- auth -------------------------------------------------------------------------

class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


@app.get("/api/auth/state")
def auth_state(request: Request):
    p = auth._load(request)
    return {"setup_needed": not auth.has_user(), "authenticated": p is not None, "kind": p.kind if p else None,
            "csrf": p.csrf if p else None}


@app.post("/api/auth/setup")
def auth_setup(body: Credentials, request: Request, response: Response):
    auth.check_origin(request)
    uid = auth.create_user(body.username, body.password)
    return {"csrf": auth.issue_session(response, "doctor", user_id=uid)}


@app.post("/api/auth/login")
def auth_login(body: Credentials, request: Request, response: Response):
    auth.check_origin(request)
    uid = auth.login(body.username, body.password)
    audit(f"user:{uid}", "login")
    return {"csrf": auth.issue_session(response, "doctor", user_id=uid)}


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response, p: Principal = Doctor):
    auth.end_session(request, response)
    audit(p.actor, "logout")
    return {"ok": True}


# ---- system -------------------------------------------------------------------------

@app.get("/api/system/status")
def system_status(p: Principal = Doctor):
    ok, eng = ocr.available()
    gw: dict = {"ok": False}
    try:
        with httpx.Client(base_url=settings.gateway_url, timeout=3, trust_env=False) as c:
            gw = c.get("/v1/health", headers={"X-Gateway-Key": settings.gateway_secret()}).json()
    except Exception as e:  # noqa: BLE001
        gw = {"ok": False, "error": type(e).__name__}
    return {"model": llm.status(), "ocr": {"ok": ok, "engine": eng, "cloud": settings.ocr_engine == "openrouter"}, "gateway": gw, "agent": settings.agent_backend,
            "limits": {"upload_mb": settings.max_upload_bytes // 2**20, "pdf_pages": settings.max_pdf_pages,
                       "sheet_rows": settings.max_sheet_rows}, "retention_days": settings.retention_days or None}


@app.post("/api/system/gateway-probe")
def gateway_probe(p: Principal = Doctor):
    try:
        with httpx.Client(base_url=settings.gateway_url, timeout=30, trust_env=False) as c:
            return c.post("/v1/probe", headers={"X-Gateway-Key": settings.gateway_secret()}).json()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": type(e).__name__}


# ---- cases & conversations --------------------------------------------------------------

class CaseIn(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    retention_policy: str | None = Field(default=None, max_length=200)


@app.get("/api/cases")
def list_cases(p: Principal = Doctor):
    rows = db.q("SELECT id, label, created_at, updated_at FROM cases WHERE owner=? ORDER BY updated_at DESC", (p.user_id,))
    return [dict(r) for r in rows]


@app.post("/api/cases")
def create_case(body: CaseIn, p: Principal = Doctor):
    cid = new_id("case")
    db.ex("INSERT INTO cases(id, owner, label, retention_policy, created_at, updated_at) VALUES (?,?,?,?,?,?)",
          (cid, p.user_id, body.label.strip(), body.retention_policy, now(), now()))
    conv = new_id("conv")
    db.ex("INSERT INTO conversations(id, case_id, title, model, runtime_version, created_at) VALUES (?,?,?,?,?,?)",
          (conv, cid, "Conversation 1", llm.model_label(), f"{settings.agent_backend}/{settings.llm_provider}", now()))
    audit(p.actor, "case.create", cid, [cid])
    return {"id": cid, "conversation_id": conv}


@app.get("/api/cases/{case_id}")
def get_case(case_id: str, p: Principal = Doctor):
    c = require_case(p, case_id)
    convs = db.q("SELECT id, title, model, created_at FROM conversations WHERE case_id=? ORDER BY created_at", (case_id,))
    return {**{k: c[k] for k in ("id", "label", "retention_policy", "created_at")}, "conversations": [dict(r) for r in convs]}


@app.delete("/api/cases/{case_id}")
def delete_case(case_id: str, p: Principal = Doctor):
    """Delete originals, working images, extracted text, conversations, artifacts, caches and indexes."""
    require_case(p, case_id)
    with db.tx() as c:
        for t in ("messages", "documents", "extractions", "datasets", "artifacts", "evidence_queries", "evidence_records", "conversations"):
            c.execute(f"DELETE FROM {t} WHERE case_id=?", (case_id,))
        job_ids = [r["id"] for r in c.execute("SELECT id FROM jobs WHERE case_id=?", (case_id,)).fetchall()]
        c.execute("DELETE FROM job_events WHERE job_id IN (SELECT id FROM jobs WHERE case_id=?)", (case_id,))
        c.execute("DELETE FROM jobs WHERE case_id=?", (case_id,))
        c.execute("DELETE FROM search_fts WHERE case_id=?", (case_id,))
        c.execute("UPDATE sessions SET revoked=1 WHERE case_id=?", (case_id,))
        c.execute("UPDATE pairings SET state='revoked' WHERE case_id=?", (case_id,))
        c.execute("DELETE FROM cases WHERE id=?", (case_id,))
    for j in job_ids:
        jobs.cancel(j)
    storage.delete_case(case_id)
    db.connect().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    audit(p.actor, "case.delete", case_id, [case_id])
    return {"deleted": True, "note": "Live data removed. Backups expire according to the clinic's backup retention policy; "
                                     "deletion does not guarantee forensic erasure."}


@app.post("/api/cases/{case_id}/conversations")
def create_conversation(case_id: str, p: Principal = Doctor):
    require_case(p, case_id)
    n = db.q1("SELECT COUNT(*) n FROM conversations WHERE case_id=?", (case_id,))["n"]
    cid = new_id("conv")
    db.ex("INSERT INTO conversations(id, case_id, title, model, runtime_version, created_at) VALUES (?,?,?,?,?,?)",
          (cid, case_id, f"Conversation {n + 1}", llm.model_label(), f"{settings.agent_backend}/{settings.llm_provider}", now()))
    return {"id": cid}


@app.get("/api/cases/{case_id}/items")
def case_items(case_id: str, p: Principal = Doctor):
    require_case(p, case_id)
    docs = []
    for r in db.q("SELECT d.*, e.review_state ex_state, e.rows ex_rows FROM documents d LEFT JOIN extractions e ON e.document_id=d.id "
                  "WHERE d.case_id=? ORDER BY d.created_at DESC", (case_id,)):
        meta = loads(r["meta"], {})
        unrev = extraction.unreviewed_critical(loads(r["ex_rows"])) if r["ex_rows"] else 0
        docs.append({"id": r["id"], "filename": r["filename"], "kind": r["kind"], "state": r["state"], "source": r["source"],
                     "page_count": r["page_count"], "created_at": r["created_at"], "extraction_state": r["ex_state"],
                     "unreviewed_critical": unrev, "datasets": meta.get("datasets", []), "notes": meta.get("notes", []),
                     "quality": meta.get("quality", []), "embedded_images": len(meta.get("embedded_images", [])),
                     "job_id": meta.get("job_id")})
    dss = [datasets.summary(datasets.load(r["id"])) for r in db.q("SELECT id FROM datasets WHERE case_id=? ORDER BY created_at DESC", (case_id,))]
    arts = [artifacts.summary(dict(r)) for r in db.q("SELECT * FROM artifacts WHERE case_id=? ORDER BY created_at DESC", (case_id,))]
    return {"documents": docs, "datasets": dss, "artifacts": arts}


@app.get("/api/cases/{case_id}/search")
def case_search(case_id: str, q: str = Query(min_length=1, max_length=200), p: Principal = Doctor):
    require_case(p, case_id)
    safe = " ".join('"' + t.replace('"', "") + '"' for t in q.split() if t.strip())
    rows = db.q("SELECT ref_type, ref_id, snippet(search_fts, 0, '[', ']', '...', 12) snip FROM search_fts "
                "WHERE search_fts MATCH ? AND case_id=? LIMIT 50", (safe, case_id))
    return [dict(r) for r in rows]


# ---- uploads ---------------------------------------------------------------------------

async def _read_capped(f: UploadFile) -> bytes:
    data = await f.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(413, f"File exceeds {settings.max_upload_bytes // 2**20} MB. Split it into smaller files.")
    return data


def _store_document(case_id: str, data: bytes, filename: str, source: str, actor: str, extra_meta: dict | None = None,
                    parent: str | None = None) -> dict:
    try:
        info = validate.sniff(data, filename)
    except validate.Rejected as e:
        audit(actor, "upload", case_id, [], "rejected")
        raise HTTPException(422, str(e)) from None
    digest = sha256_bytes(data)
    dup = db.q1("SELECT id FROM documents WHERE case_id=? AND sha256=?", (case_id, digest))
    if dup:
        return {"document_id": dup["id"], "duplicate": True}
    doc_id = new_id("doc")
    original_ref = None
    working_ref = None
    if info.kind == "image" and source == "phone":
        working = strip_metadata(data)  # metadata never enters the working copy
        working_ref = storage.put(case_id, working, ".png", kind="work")
        if settings.retain_capture_originals:
            original_ref = storage.put(case_id, data, ".bin", kind="orig")
    else:
        original_ref = storage.put(case_id, data, ".bin", kind="orig")
    meta = {"notes": info.notes or [], **(extra_meta or {})}
    db.ex("INSERT INTO documents(id, case_id, filename, mime, kind, sha256, size, original_ref, working_ref, state, page_count, source, meta,"
          " parent_document_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          (doc_id, case_id, filename[:255], info.mime, info.kind, digest, len(data), original_ref, working_ref, "uploaded",
           info.page_count, source, dumps(meta), parent, now()))
    job_id = jobs.submit(case_id, "ingest", {"document_id": doc_id})
    pipeline.set_doc_state(doc_id, "uploaded", {"job_id": job_id})
    db.ex("UPDATE cases SET updated_at=? WHERE id=?", (now(), case_id))
    audit(actor, "upload", case_id, [doc_id, job_id])
    return {"document_id": doc_id, "job_id": job_id, "kind": info.kind}


@app.post("/api/cases/{case_id}/uploads")
async def upload(case_id: str, files: list[UploadFile] = File(...), idempotency_key: str | None = Header(None),
                 p: Principal = Doctor):
    require_case(p, case_id)
    if len(files) > 20:
        raise HTTPException(400, "Upload at most 20 files at once.")
    blobs = [(await _read_capped(f), f.filename or "upload") for f in files]

    def do() -> dict:
        return {"documents": [_store_document(case_id, b, n, "desktop", p.actor) for b, n in blobs]}
    return jobs.idempotent(f"upload:{case_id}", idempotency_key, do)


# ---- pairing / phone ------------------------------------------------------------------

class PairIn(BaseModel):
    case_id: str


class ExchangeIn(BaseModel):
    token: str = Field(min_length=10, max_length=100)
    device_label: str = Field(default="phone", max_length=60)


@app.post("/api/pairings")
def pair_create(body: PairIn, p: Principal = Doctor):
    pid, token, exp = auth.create_pairing(p, body.case_id)
    return {"pairing_id": pid, "token": token, "expires_at": exp, "path": "/capture#t=" + token}


@app.get("/api/pairings/{pairing_id}")
def pair_status(pairing_id: str, p: Principal = Doctor):
    r = db.q1("SELECT id, case_id, state, device_label, expires_at FROM pairings WHERE id=?", (pairing_id,))
    if r is None:
        raise HTTPException(404, "not found")
    require_case(p, r["case_id"])
    d = dict(r)
    if d["state"] == "issued" and d["expires_at"] < now():
        d["state"] = "expired"
    return d


@app.post("/api/pairings/{pairing_id}/confirm")
def pair_confirm(pairing_id: str, p: Principal = Doctor):
    auth.set_pairing_state(p, pairing_id, "confirmed")
    return {"ok": True}


@app.post("/api/pairings/{pairing_id}/revoke")
def pair_revoke(pairing_id: str, p: Principal = Doctor):
    auth.set_pairing_state(p, pairing_id, "revoked")
    return {"ok": True}


@app.post("/api/pairings/exchange")
def pair_exchange(body: ExchangeIn, request: Request, response: Response):
    auth.check_origin(request)
    pid = auth.exchange_pairing(body.token, body.device_label, response)
    return {"pairing_id": pid, "state": "exchanged", "message": "Confirm the pairing on the workstation."}


@app.get("/api/phone/session")
def phone_session(p: Principal = Depends(auth.phone_pending)):
    r = db.q1("SELECT state FROM pairings WHERE id=?", (p.pairing_id,))
    c = db.q1("SELECT label FROM cases WHERE id=?", (p.case_id,))
    confirmed = r is not None and r["state"] == "confirmed"
    uploads = db.q("SELECT id, state, created_at FROM documents WHERE case_id=? AND source='phone' AND meta LIKE ? ORDER BY created_at",
                   (p.case_id, f'%"pairing_id": "{p.pairing_id}"%')) if confirmed else []
    return {"state": r["state"] if r else "revoked", "csrf": p.csrf,
            "case_label": c["label"] if (c and confirmed) else None,
            "uploads": [dict(u) for u in uploads]}


@app.post("/api/phone/uploads")
async def phone_upload(files: list[UploadFile] = File(...), idempotency_key: str | None = Header(None),
                       p: Principal = Depends(auth.phone)):
    if len(files) > 30:
        raise HTTPException(400, "Upload at most 30 pages at once.")
    blobs = [(await _read_capped(f), f.filename or "capture.jpg") for f in files]

    def do() -> dict:
        out = []
        for i, (b, _) in enumerate(blobs):
            out.append(_store_document(p.case_id, b, f"phone-capture-{int(now())}-{i + 1}.jpg", "phone", p.actor,
                                       {"pairing_id": p.pairing_id, "page_order": i + 1}))
        return {"documents": [{"document_id": d["document_id"], "duplicate": d.get("duplicate", False)} for d in out]}
    return jobs.idempotent(f"phone:{p.pairing_id}", idempotency_key, do)


# ---- conversations / jobs ---------------------------------------------------------------

class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


@app.get("/api/conversations/{conv_id}/messages")
def list_messages(conv_id: str, p: Principal = Doctor):
    owned(p, "conversations", conv_id)
    rows = db.q("SELECT m.id, m.role, m.content, m.meta, m.job_id, m.created_at FROM messages m WHERE conversation_id=? ORDER BY created_at",
                (conv_id,))
    out = []
    for r in rows:
        meta = loads(r["meta"], {})
        if meta.get("type") == "evidence_approval":
            meta["query"] = pipeline.evidence_query_view(meta["query_id"])
        out.append({**dict(r), "meta": meta})
    return out


@app.post("/api/conversations/{conv_id}/messages")
def post_message(conv_id: str, body: MessageIn, p: Principal = Doctor):
    conv = owned(p, "conversations", conv_id)
    mid = new_id("msg")
    db.ex("INSERT INTO messages(id, conversation_id, case_id, role, content, meta, created_at) VALUES (?,?,?,?,?,?,?)",
          (mid, conv_id, conv["case_id"], "user", body.text, "{}", now()))
    db.index_text(conv["case_id"], "message", mid, body.text)
    jid = jobs.submit(conv["case_id"], "chat", {"conversation_id": conv_id, "message_id": mid, "text": body.text})
    db.ex("UPDATE messages SET job_id=? WHERE id=?", (jid, mid))
    db.ex("UPDATE cases SET updated_at=? WHERE id=?", (now(), conv["case_id"]))
    return {"message_id": mid, "job_id": jid}


def _job_view(j: dict) -> dict:
    return {k: j[k] for k in ("id", "case_id", "kind", "state", "stage", "error", "attempt", "created_at", "updated_at")} | {
        "result": loads(j["result"]) if j["result"] else None}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, p: Principal = Doctor):
    return _job_view(owned(p, "jobs", job_id))


@app.get("/api/cases/{case_id}/jobs")
def case_jobs(case_id: str, p: Principal = Doctor):
    require_case(p, case_id)
    return [_job_view(dict(r)) for r in db.q("SELECT * FROM jobs WHERE case_id=? ORDER BY created_at DESC LIMIT 50", (case_id,))]


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request, after: int = 0, p: Principal = Doctor):
    owned(p, "jobs", job_id)

    async def gen():
        seq = after
        last_beat = time.monotonic()
        while True:
            if await request.is_disconnected():
                return
            evs = await asyncio.to_thread(jobs.events_since, job_id, seq)
            for e in evs:
                seq = e["seq"]
                yield f"id: {seq}\nevent: {e['type']}\ndata: {dumps(e)}\n\n"
                if e["type"] == "done":
                    return
            if time.monotonic() - last_beat > 15:
                last_beat = time.monotonic()
                yield ": keepalive\n\n"
            await asyncio.sleep(0.4)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, p: Principal = Doctor):
    owned(p, "jobs", job_id)
    jobs.cancel(job_id)
    audit(p.actor, "job.cancel", None, [job_id])
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str, p: Principal = Doctor):
    owned(p, "jobs", job_id)
    try:
        jobs.retry(job_id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    return {"ok": True}


# ---- documents & extraction -----------------------------------------------------------

@app.get("/api/documents/{doc_id}")
def get_document(doc_id: str, p: Principal = Doctor):
    d = owned(p, "documents", doc_id)
    meta = loads(d["meta"], {})
    return {k: d[k] for k in ("id", "case_id", "filename", "mime", "kind", "size", "state", "page_count", "source", "created_at", "sha256")} | {
        "pages": len(meta.get("page_refs", [])) or (1 if d["working_ref"] else 0), "quality": meta.get("quality", []),
        "notes": meta.get("notes", []), "datasets": meta.get("datasets", []), "embedded_images": len(meta.get("embedded_images", [])),
        "has_original": bool(d["original_ref"]), "job_id": meta.get("job_id")}


@app.get("/api/documents/{doc_id}/pages/{n}.png")
def document_page(doc_id: str, n: int, p: Principal = Doctor):
    d = owned(p, "documents", doc_id)
    refs = loads(d["meta"], {}).get("page_refs") or ([d["working_ref"]] if d["working_ref"] else [])
    if not 1 <= n <= len(refs):
        raise HTTPException(404, "page not available")
    return FileResponse(storage.path_of(refs[n - 1]), media_type="image/png", headers={"Cache-Control": "private, no-store"})


@app.get("/api/documents/{doc_id}/original")
def document_original(doc_id: str, p: Principal = Doctor):
    d = owned(p, "documents", doc_id)
    if not d["original_ref"]:
        raise HTTPException(404, "original not retained")
    audit(p.actor, "document.download_original", d["case_id"], [doc_id])
    return FileResponse(storage.path_of(d["original_ref"]), media_type=d["mime"], filename=d["filename"])


@app.get("/api/documents/{doc_id}/extraction")
def get_extraction(doc_id: str, p: Principal = Doctor):
    owned(p, "documents", doc_id)
    r = db.q1("SELECT * FROM extractions WHERE document_id=?", (doc_id,))
    if r is None:
        raise HTTPException(404, "no extraction for this document")
    canon = loads(r["canonical"])
    ex = loads(r["rows"])
    return {"document_id": doc_id, "version": r["version"], "schema": r["schema"], "review_state": r["review_state"],
            "parser_version": r["parser_version"], "engine": canon.get("engine"),
            "pages": [{"page": pg["page"], "width": pg["width"], "height": pg["height"],
                       "spans": [{"id": s["id"], "bbox": s["bbox"], "text": s["text"], "score": s.get("score")} for s in pg["spans"]]}
                      for pg in canon["pages"]],
            "header": ex["header"], "rows": ex["rows"], "fields": extraction.SCHEMAS[ex["schema"]]["row_fields"],
            "unreviewed_critical": extraction.unreviewed_critical(ex)}


class FieldUpdate(BaseModel):
    row_id: str  # "header" for document-level fields
    field: str
    action: str = Field(pattern="^(confirm|correct|unknown)$")
    raw_text: str | None = Field(default=None, max_length=500)


class ExtractionPatch(BaseModel):
    version: int
    updates: list[FieldUpdate] = Field(default_factory=list, max_length=2000)
    add_row: bool = False
    delete_rows: list[str] = Field(default_factory=list)


@app.patch("/api/documents/{doc_id}/extraction")
def patch_extraction(doc_id: str, body: ExtractionPatch, p: Principal = Doctor):
    doc = owned(p, "documents", doc_id)
    with db.tx() as c:
        r = c.execute("SELECT * FROM extractions WHERE document_id=?", (doc_id,)).fetchone()
        if r is None:
            raise HTTPException(404, "no extraction")
        if r["version"] != body.version:
            raise HTTPException(409, "The extraction changed since you loaded it. Reload and re-apply your corrections.")
        ex = loads(r["rows"])
        spec = extraction.SCHEMAS[ex["schema"]]
        if body.add_row:
            ex["rows"].append({"id": f"m{len(ex['rows'])}_{int(now())}", "line_id": None, "page": 1, "proposed_by": "manual",
                               "fields": {f: extraction.make_field(None, f, None, None, f in spec["critical"]) for f in spec["row_fields"]}})
        if body.delete_rows:
            ex["rows"] = [x for x in ex["rows"] if x["id"] not in set(body.delete_rows)]
        for u in body.updates:
            if u.row_id == "header":
                target = ex["header"]
                if u.field not in spec["header_fields"]:
                    raise HTTPException(422, f"unknown header field {u.field}")
                if u.field not in target:
                    target[u.field] = extraction.make_field(None, u.field, None, None, True)
            else:
                row = next((x for x in ex["rows"] if x["id"] == u.row_id), None)
                if row is None or u.field not in spec["row_fields"]:
                    raise HTTPException(422, f"unknown field {u.row_id}.{u.field}")
                target = row["fields"]
            old = target[u.field]
            if u.action == "confirm":
                if old.get("raw_text") is None:
                    raise HTTPException(422, "Cannot confirm an empty field; enter the value or mark it unknown.")
                old["review_state"] = "confirmed"
            elif u.action == "unknown":
                new = extraction.make_field(None, u.field, old.get("source"), None, old["critical"])
                new["review_state"] = "confirmed_unknown"
                new["issues"] = ["Confirmed as unknown/unreadable by the doctor."]
                new["previous_raw_text"] = old.get("raw_text")
                target[u.field] = new
            else:
                new = extraction.make_field(u.raw_text, u.field, old.get("source"), None, old["critical"])
                new["review_state"] = "corrected"
                new["previous_raw_text"] = old.get("raw_text")
                new["ocr_score"] = None
                target[u.field] = new
        c.execute("UPDATE extractions SET rows=?, version=version+1, review_state='needs_review', updated_at=? WHERE document_id=?",
                  (dumps(ex), now(), doc_id))
    audit(p.actor, "extraction.review", doc["case_id"], [doc_id], f"{len(body.updates)} updates")
    return get_extraction(doc_id, p)


class FreezeIn(BaseModel):
    allow_draft: bool = False


@app.post("/api/documents/{doc_id}/extraction/freeze")
def freeze_extraction(doc_id: str, body: FreezeIn, p: Principal = Doctor):
    doc = owned(p, "documents", doc_id)
    r = db.row_dict(db.q1("SELECT * FROM extractions WHERE document_id=?", (doc_id,)))
    if r is None:
        raise HTTPException(404, "no extraction")
    try:
        did = pipeline.dataset_from_extraction(doc, r, body.allow_draft)
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    d = datasets.load(did)
    audit(p.actor, "extraction.freeze", doc["case_id"], [doc_id, did], d["review_state"])
    return datasets.summary(d)


class ReprocessIn(BaseModel):
    rotate: int = Field(default=0)
    schema_name: str = Field(default="lab_results", pattern="^(lab_results|medications)$")


@app.post("/api/documents/{doc_id}/reprocess")
def reprocess(doc_id: str, body: ReprocessIn, p: Principal = Doctor):
    doc = owned(p, "documents", doc_id)
    if doc["kind"] not in {"image", "pdf"}:
        raise HTTPException(422, "only images and PDFs are processed with OCR")
    if body.rotate % 90:
        raise HTTPException(422, "rotation must be a multiple of 90")
    jid = jobs.submit(doc["case_id"], "ingest", {"document_id": doc_id})
    pipeline.set_doc_state(doc_id, "uploaded", {"job_id": jid, "rotate": body.rotate % 360, "schema": body.schema_name})
    return {"job_id": jid}


@app.post("/api/documents/{doc_id}/embedded-images/{n}/ocr")
def ocr_embedded_image(doc_id: str, n: int, p: Principal = Doctor):
    import io
    import zipfile

    doc = owned(p, "documents", doc_id)
    media = loads(doc["meta"], {}).get("embedded_images", [])
    if not 0 <= n < len(media):
        raise HTTPException(404, "no such embedded image")
    with zipfile.ZipFile(io.BytesIO(storage.read(doc["original_ref"]))) as zf:
        info = zf.getinfo(media[n])
        if info.file_size > settings.max_upload_bytes:
            raise HTTPException(413, "embedded image too large")
        data = zf.read(media[n])
    return _store_document(doc["case_id"], data, f"{doc['filename']}#image{n + 1}", "embedded", p.actor, parent=doc_id)


# ---- datasets ------------------------------------------------------------------------

@app.get("/api/datasets/{ds_id}")
def get_dataset(ds_id: str, p: Principal = Doctor):
    owned(p, "datasets", ds_id)
    return datasets.summary(datasets.load(ds_id))


@app.get("/api/datasets/{ds_id}/rows")
def dataset_rows(ds_id: str, offset: int = 0, limit: int = Query(100, le=500), sort: str | None = None, desc: bool = False,
                 q: str | None = None, p: Principal = Doctor):
    owned(p, "datasets", ds_id)
    return datasets.page(datasets.load(ds_id), max(offset, 0), limit, sort, desc, q)


@app.get("/api/datasets/{ds_id}/versions")
def dataset_versions(ds_id: str, p: Principal = Doctor):
    d = owned(p, "datasets", ds_id)
    rows = db.q("SELECT id, version, label, review_state, created_at, transformations FROM datasets WHERE lineage_id=? AND case_id=? ORDER BY version",
                (d["lineage_id"], d["case_id"]))
    return [{**dict(r), "transformations": loads(r["transformations"], [])} for r in rows]


class TransformIn(BaseModel):
    ops: list[dict] = Field(min_length=1, max_length=20)
    label: str | None = Field(default=None, max_length=120)


@app.post("/api/datasets/{ds_id}/transform")
def transform_dataset(ds_id: str, body: TransformIn, p: Principal = Doctor):
    d = owned(p, "datasets", ds_id)
    try:
        nid = datasets.transform(ds_id, body.ops, body.label)
    except datasets.TransformError as e:
        raise HTTPException(422, str(e)) from None
    audit(p.actor, "dataset.transform", d["case_id"], [ds_id, nid])
    return datasets.summary(datasets.load(nid))


class ArtifactIn(BaseModel):
    format: str = Field(pattern="^(xlsx|csv|json|docx|pdf|md)$")
    title: str | None = Field(default=None, max_length=120)


@app.post("/api/datasets/{ds_id}/artifacts")
def dataset_artifact(ds_id: str, body: ArtifactIn, idempotency_key: str | None = Header(None), p: Principal = Doctor):
    d = owned(p, "datasets", ds_id)

    def do() -> dict:
        prior = db.q1("SELECT a.lineage_id FROM artifacts a JOIN datasets s ON s.id=a.dataset_id WHERE s.lineage_id=? AND a.format=? "
                      "ORDER BY a.created_at DESC LIMIT 1", (d["lineage_id"], body.format))
        aid = artifacts.create(d["case_id"], body.format, ds_id, body.title, lineage_id=prior["lineage_id"] if prior else None)
        audit(p.actor, "artifact.create", d["case_id"], [ds_id, aid])
        return {"artifact_id": aid}
    try:
        return jobs.idempotent(f"artifact:{ds_id}", idempotency_key, do)
    except artifacts.ArtifactError as e:
        raise HTTPException(422, str(e)) from None


@app.post("/api/messages/{msg_id}/artifacts")
def message_artifact(msg_id: str, body: ArtifactIn, idempotency_key: str | None = Header(None), p: Principal = Doctor):
    m = db.q1("SELECT * FROM messages WHERE id=?", (msg_id,))
    if m is None:
        raise HTTPException(404, "not found")
    require_case(p, m["case_id"])
    meta = loads(m["meta"], {})
    if meta.get("type") != "evidence_answer":
        raise HTTPException(422, "only evidence answers can be exported as reports")
    if body.format not in {"docx", "pdf", "md"}:
        raise HTTPException(422, "evidence summaries export as docx, pdf or md")

    def do() -> dict:
        aid = artifacts.create(m["case_id"], body.format, None, body.title or "Evidence summary", answer=meta["answer"])
        audit(p.actor, "artifact.create", m["case_id"], [msg_id, aid])
        return {"artifact_id": aid}
    return jobs.idempotent(f"msgart:{msg_id}", idempotency_key, do)


# ---- artifacts ------------------------------------------------------------------------

@app.get("/api/artifacts/{art_id}")
def get_artifact(art_id: str, p: Principal = Doctor):
    return artifacts.summary(owned(p, "artifacts", art_id))


@app.get("/api/artifacts/{art_id}/preview")
def artifact_preview(art_id: str, p: Principal = Doctor):
    a = owned(p, "artifacts", art_id)
    prev = loads(a["preview"], {})
    out = {**artifacts.summary(a), "preview": prev}
    if a["format"] == "json":
        import json as _json
        raw = storage.read(a["storage_ref"])
        out["json"] = _json.loads(raw) if len(raw) < 5_000_000 else None
    return out


@app.get("/api/artifacts/{art_id}/pages/{n}.png")
def artifact_page(art_id: str, n: int, p: Principal = Doctor):
    a = owned(p, "artifacts", art_id)
    if a["format"] != "pdf":
        raise HTTPException(404, "not a PDF")
    from .ingest.pdf import page_count, render_page_png
    cache = storage.cache_dir(a["case_id"]) / f"{art_id}-{n}.png"
    if not cache.exists():
        data = storage.read(a["storage_ref"])
        if not 1 <= n <= page_count(data):
            raise HTTPException(404, "no such page")
        png, _, _ = render_page_png(data, n - 1, scale=1.6)
        cache.write_bytes(png)
    return FileResponse(cache, media_type="image/png", headers={"Cache-Control": "private, no-store"})


@app.get("/api/artifacts/{art_id}/versions")
def artifact_versions(art_id: str, p: Principal = Doctor):
    a = owned(p, "artifacts", art_id)
    rows = db.q("SELECT * FROM artifacts WHERE lineage_id=? AND case_id=? ORDER BY version", (a["lineage_id"], a["case_id"]))
    return [artifacts.summary(dict(r)) for r in rows]


@app.get("/api/artifacts/{art_id}/download")
def artifact_download(art_id: str, p: Principal = Doctor):
    a = owned(p, "artifacts", art_id)
    audit(p.actor, "artifact.download", a["case_id"], [art_id])
    safe_title = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in a["title"])[:60].strip() or "artifact"
    draft = "-DRAFT" if a["review_state"] == "draft" else ""
    return FileResponse(storage.path_of(a["storage_ref"]), media_type=artifacts.MIME[a["format"]],
                        filename=f"{safe_title}-v{a['version']}{draft}.{a['format']}")


# ---- evidence queries ------------------------------------------------------------------

class ApproveIn(BaseModel):
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviseIn(BaseModel):
    concepts: list[str] = Field(min_length=1, max_length=8)
    population: str | None = None
    study_types: list[str] = Field(default_factory=list)
    year_from: int | None = None
    year_to: int | None = None


@app.get("/api/evidence-queries/{qid}")
def get_query(qid: str, p: Principal = Doctor):
    owned(p, "evidence_queries", qid)
    v = pipeline.evidence_query_view(qid)
    v["records"] = [{"pmid": r["pmid"], "metadata": loads(r["metadata"]), "access_level": r["access_level"], "retrieval_time": r["retrieval_time"]}
                    for r in db.q("SELECT * FROM evidence_records WHERE query_id=?", (qid,))]
    return v


@app.post("/api/evidence-queries/{qid}/approve")
def approve_query(qid: str, body: ApproveIn, p: Principal = Doctor):
    q = owned(p, "evidence_queries", qid)
    with db.tx() as c:
        r = c.execute("SELECT * FROM evidence_queries WHERE id=?", (qid,)).fetchone()
        if r["status"] != "pending_approval":
            raise HTTPException(409, f"query is {r['status']}, not awaiting approval")
        if r["request_hash"] != body.request_hash:
            raise HTTPException(409, "the query changed; review the current topic and approve again")
        norm = loads(r["request"])
        res = policy.evaluate(policy.EvidenceRequest(**{k: v for k, v in norm.items() if v not in (None, [])}),
                              pipeline.case_identifiers(q["case_id"]))
        if not res.ok or policy.request_hash(res.request) != r["request_hash"]:
            c.execute("UPDATE evidence_queries SET status='blocked' WHERE id=?", (qid,))
            raise HTTPException(422, "the query no longer passes the outbound policy")
        c.execute("UPDATE evidence_queries SET status='approved', approved_at=? WHERE id=?", (now(), qid))
    audit(p.actor, "evidence.approve", q["case_id"], [qid])
    jid = jobs.submit(q["case_id"], "evidence_answer", {"query_id": qid})
    return {"job_id": jid}


@app.post("/api/evidence-queries/{qid}/revise")
def revise_query(qid: str, body: ReviseIn, p: Principal = Doctor):
    q = owned(p, "evidence_queries", qid)
    if q["status"] not in {"pending_approval", "needs_revision", "blocked"}:
        raise HTTPException(409, "only unsent queries can be revised")
    v = pipeline.create_evidence_query(q["case_id"], q["conversation_id"], q["question_message_id"], body.model_dump(), supersedes=qid)
    db.ex("UPDATE messages SET meta=json_set(meta, '$.query_id', ?) WHERE conversation_id=? AND json_extract(meta, '$.query_id')=?",
          (v["id"], q["conversation_id"], qid))
    return v


@app.post("/api/evidence-queries/{qid}/decline")
def decline_query(qid: str, p: Principal = Doctor):
    q = owned(p, "evidence_queries", qid)
    db.ex("UPDATE evidence_queries SET status='declined' WHERE id=? AND status IN ('pending_approval','needs_revision','blocked')", (qid,))
    audit(p.actor, "evidence.decline", q["case_id"], [qid])
    return pipeline.evidence_query_view(qid)


# ---- vocabulary & audit --------------------------------------------------------------

@app.get("/api/vocabulary")
def vocab(q: str = "", p: Principal = Doctor):
    index, cats, version = policy.vocabulary()
    terms = sorted({t for t in index.values() if q.lower() in t})[:200]
    return {"version": version, "terms": [{"term": t, "category": cats.get(t)} for t in terms]}


class VocabIn(BaseModel):
    terms: list[dict] = Field(min_length=1, max_length=100)


@app.post("/api/vocabulary")
def vocab_add(body: VocabIn, p: Principal = Doctor):
    try:
        n = policy.add_terms(body.terms, p.actor)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    audit(p.actor, "vocabulary.add", None, [], f"{n} terms")
    return {"added": n}


@app.get("/api/audit")
def audit_log(case_id: str | None = None, p: Principal = Doctor):
    if case_id:
        require_case(p, case_id)
        rows = db.q("SELECT * FROM audit WHERE case_id=? ORDER BY seq DESC LIMIT 200", (case_id,))
    else:
        rows = db.q("SELECT * FROM audit ORDER BY seq DESC LIMIT 200")
    return [dict(r) for r in rows]


# ---- frontend (served locally; no CDN assets) ------------------------------------------

def _spa(path: str) -> Response:
    dist = settings.frontend_dist
    target = (dist / path).resolve()
    if path and dist.resolve() in target.parents and target.is_file():
        headers = {"Cache-Control": "public, max-age=31536000, immutable"} if "/assets/" in str(target) else {}
        return FileResponse(target, headers=headers)
    index = dist / "index.html"
    if index.exists():
        return FileResponse(index, headers={"Cache-Control": "no-store"})
    return JSONResponse({"detail": "frontend not built: run `npm run build` in frontend/"}, status_code=404)


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    if path.startswith("api/"):
        raise HTTPException(404, "not found")
    return _spa(path)


