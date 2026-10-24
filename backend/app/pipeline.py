"""Job handlers wiring ingestion, extraction, chat and evidence together."""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

import httpx

from . import artifacts, datasets, db, netguard, storage
from .agent import llm
from .agent.adapter import get_adapter
from .audit import audit
from .config import settings
from .evidence import answer as ans
from .evidence import phi, policy
from .ingest import canonical, extraction, images, ocr, pdf, tabular
from .jobs import Cancelled, JobContext, JobError, handler
from .util import dumps, loads, new_id, now

log = logging.getLogger("medassist.pipeline")


# ---- documents --------------------------------------------------------------------

def set_doc_state(doc_id: str, state: str, meta_update: dict | None = None) -> None:
    if meta_update:
        r = db.q1("SELECT meta FROM documents WHERE id=?", (doc_id,))
        meta = loads(r["meta"], {}) if r else {}
        meta.update(meta_update)
        db.ex("UPDATE documents SET state=?, meta=? WHERE id=?", (state, dumps(meta), doc_id))
    else:
        db.ex("UPDATE documents SET state=? WHERE id=?", (state, doc_id))


def _model_chat_json():
    return llm.chat_json if llm.status().get("ok") else None


def save_extraction(doc: dict, canon: dict, schema: str = "lab_results") -> dict:
    proposal = extraction.propose(canon, schema, _model_chat_json())
    prev = db.q1("SELECT version FROM extractions WHERE document_id=?", (doc["id"],))
    version = (prev["version"] + 1) if prev else 1
    db.ex(
        "INSERT INTO extractions(document_id, case_id, version, schema, canonical, rows, review_state, parser_version, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(document_id) DO UPDATE SET version=excluded.version, schema=excluded.schema,"
        " canonical=excluded.canonical, rows=excluded.rows, review_state=excluded.review_state,"
        " parser_version=excluded.parser_version, updated_at=excluded.updated_at",
        (doc["id"], doc["case_id"], version, schema, dumps(canon), dumps(proposal), "needs_review",
         f"{canonical.PARSER_VERSION}/{extraction.EXTRACTOR_VERSION}", now()),
    )
    db.index_text(doc["case_id"], "document", doc["id"], canonical.full_text(canon))
    return {"document_id": doc["id"], "schema": schema, "rows": len(proposal["rows"]),
            "header_fields": sorted(proposal["header"]), "unreviewed_critical": extraction.unreviewed_critical(proposal),
            "proposed_by": sorted({r["proposed_by"] for r in proposal["rows"]}) or ["rules"]}


def run_extraction(doc: dict, schema: str) -> dict:
    ex = db.q1("SELECT canonical FROM extractions WHERE document_id=?", (doc["id"],))
    if ex is None:
        raise JobError("The document has not been processed yet.")
    return save_extraction(doc, loads(ex["canonical"]), schema)


def _ocr_page(ctx: JobContext, png: bytes, page_no: int, total: int) -> list[dict]:
    ctx.stage("ocr", f"Recognizing text, page {page_no} of {total}", progress=(page_no - 1) / max(total, 1))
    try:
        return ocr.recognize_png(png)
    except ocr.OCRUnavailable as e:
        raise JobError(f"{e} The original is stored; you can transcribe values manually.") from None
    except Cancelled:
        raise
    except MemoryError:
        log.error("ocr out of memory page=%s", page_no)
        raise JobError("OCR ran out of memory on this page. Lower MEDASSIST_OCR_MAX_SIDE or split the file. "
                       "The original is stored; retry afterwards.") from None
    except Exception as e:  # noqa: BLE001 - engine/model loading failures
        log.error("ocr failed job=%s page=%s err=%s", ctx.id, page_no, type(e).__name__)
        if settings.ocr_engine == "openrouter":
            raise JobError(f"Cloud OCR failed ({type(e).__name__}). Check the OpenRouter connection and OCR model configuration. "
                           "The original is stored; retry or transcribe manually.") from None
        if isinstance(e, netguard.EgressBlocked):
            raise JobError("OCR models are not in the local cache, and the API may not download them. "
                           "Run scripts/setup_models.py as the same user that runs the API, then retry.") from None
        raise JobError(f"The local OCR engine failed to run ({type(e).__name__}). If models are missing, run scripts/setup_models.py "
                       "(setup step, no patient data loaded). The original is stored; retry afterwards.") from None


@handler("ingest", heavy=True)
def ingest(ctx: JobContext) -> dict:
    doc = db.row_dict(db.q1("SELECT * FROM documents WHERE id=? AND case_id=?", (ctx.payload["document_id"], ctx.case_id)))
    if doc is None:
        raise JobError("Document no longer exists.")
    data = storage.read(doc["original_ref"] or doc["working_ref"])
    meta = loads(doc["meta"], {})
    ctx.stage("parsing", "Validating and reading the file")
    set_doc_state(doc["id"], "processing")

    if doc["kind"] in {"csv", "xlsx", "json"}:
        try:
            if doc["kind"] == "csv":
                tables, extra = tabular.parse_csv(data, doc["id"]), {}
            elif doc["kind"] == "xlsx":
                tables, extra = tabular.parse_xlsx(data, doc["id"])
            else:
                tables, _orig = tabular.parse_json(data, doc["id"])
                extra = {"notes": [] if tables else ["No array of records found; the JSON is kept as a tree only."]}
        except tabular.TabularError as e:
            raise JobError(str(e)) from None
        ds_ids = []
        for t in tables:
            if not t["columns"]:
                continue
            ds_ids.append(datasets.create(ctx.case_id, f"{t['name']}", t["columns"], t["rows"], [doc["id"]], "imported",
                                          meta={"notes": t["notes"], "origin": doc["kind"]}))
            ctx.check()
        set_doc_state(doc["id"], "ready", {"datasets": ds_ids, "notes": (meta.get("notes") or []) + extra.get("notes", []),
                                           "embedded_images": extra.get("embedded_images", [])})
        ctx.emit("dataset", {"dataset_ids": ds_ids})
        return {"document_id": doc["id"], "datasets": ds_ids}

    pages: list[dict] = []
    page_refs: list[str] = []
    quality: list[dict] = []
    if doc["kind"] == "image":
        norm = images.normalize(data, rotate_deg=int(meta.get("rotate", 0)))
        ref = storage.put(ctx.case_id, norm.png, ".png", kind="work")
        ctx.on_cleanup(lambda r=ref: storage.path_of(r).unlink(missing_ok=True))
        page_refs.append(ref)
        quality.append({**norm.quality, "operations": norm.operations})
        spans = _ocr_page(ctx, norm.png, 1, 1)
        pages.append({"page": 1, "width": norm.width, "height": norm.height, "spans": spans})
        engine = ocr.ENGINE_VERSION
    elif doc["kind"] == "pdf":
        text_pages = pdf.extract_text_spans(data)
        engine = "pdf-text"
        for tp in text_pages:
            ctx.check()
            png, w, h = pdf.render_page_png(data, tp["page"] - 1)
            ref = storage.put(ctx.case_id, png, ".png", kind="work")
            ctx.on_cleanup(lambda r=ref: storage.path_of(r).unlink(missing_ok=True))
            page_refs.append(ref)
            if len(tp["spans"]) >= 3:
                pages.append(tp)
                quality.append({"source": "embedded text"})
            else:
                spans = _ocr_page(ctx, png, tp["page"], len(text_pages))
                pages.append({"page": tp["page"], "width": w, "height": h, "spans": spans})
                quality.append(images.quality_checks(images.cv2.cvtColor(images.load_rgb(png), images.cv2.COLOR_BGR2GRAY)))
                engine = f"pdf-text+{ocr.ENGINE_VERSION}"
    else:
        raise JobError("Unsupported document kind.")

    ctx.stage("parsing", "Reconstructing lines and proposing fields")
    canon = canonical.build(doc["id"], pages, engine)
    db.ex("UPDATE documents SET working_ref=? WHERE id=?", (page_refs[0], doc["id"]))
    summary = save_extraction(doc, canon, meta.get("schema", "lab_results"))
    set_doc_state(doc["id"], "needs_review", {"page_refs": page_refs, "quality": quality})
    ctx.emit("extraction", {"document_id": doc["id"]})
    return {**summary, "_final_stage": "needs_review"}


def dataset_from_extraction(doc: dict, ex_row: dict, allow_draft: bool) -> str:
    ex = loads(ex_row["rows"])
    pending = extraction.unreviewed_critical(ex)
    if pending and not allow_draft:
        raise ValueError(f"{pending} clinically material field(s) still need review")
    fields = extraction.SCHEMAS[ex["schema"]]["row_fields"]
    cols = [{"name": f, "type": "string"} for f in fields] + [{"name": "source_page", "type": "number"}]
    rows = []
    for r in ex["rows"]:
        cells = {}
        for f in fields:
            fd = r["fields"].get(f) or {}
            v = fd.get("value")
            cell = {"v": v, "r": fd.get("raw_text"), "src": fd.get("source"), "rs": fd.get("review_state"),
                    "score": fd.get("ocr_score")}
            if fd.get("comparator"):
                cell["comparator"] = fd["comparator"]
            if f == "value" and r["fields"].get("unit", {}).get("value"):
                cell["unit"] = r["fields"]["unit"]["value"]
            cells[f] = cell
        cells["source_page"] = {"v": r["page"], "src": {"document_id": doc["id"], "page": r["page"]}}
        rows.append({"id": r["id"], "cells": cells})
    from .ingest.tabular import infer_type
    for c in cols:
        c["type"] = infer_type([row["cells"][c["name"]]["v"] for row in rows])
    state = "reviewed" if not pending else "draft"
    label = f"{ex['schema'].replace('_', ' ').title()} from {doc['id']}"
    did = datasets.create(doc["case_id"], label, cols, rows, [doc["id"]], state,
                          meta={"extraction_version": ex_row["version"], "unreviewed_critical": pending})
    if state == "reviewed":
        db.ex("UPDATE extractions SET review_state='reviewed' WHERE document_id=?", (doc["id"],))
        set_doc_state(doc["id"], "ready")
    return did


# ---- local identifiers (never leave the workstation) ---------------------------------

def case_identifiers(case_id: str) -> set[str]:
    vals: list[str] = []
    c = db.q1("SELECT label FROM cases WHERE id=?", (case_id,))
    if c:
        vals.append(c["label"])
    for d in db.q("SELECT filename FROM documents WHERE case_id=?", (case_id,)):
        vals.append(d["filename"])
        vals.append(d["filename"].rsplit(".", 1)[0])
    for e in db.q("SELECT rows FROM extractions WHERE case_id=?", (case_id,)):
        hdr = loads(e["rows"], {}).get("header", {})
        for k in extraction.IDENTIFYING_FIELDS:
            if hdr.get(k, {}).get("raw_text"):
                vals.append(hdr[k]["raw_text"])
    for ds in db.q("SELECT columns, rows FROM datasets WHERE case_id=?", (case_id,)):
        cols = [col["name"] for col in loads(ds["columns"], [])]
        idcols = [n for n in cols if any(k in n.lower() for k in ("name", "patient", "mrn", "dob", "birth", "phone", "address", "email"))]
        if idcols:
            for r in loads(ds["rows"], [])[:5000]:
                for n in idcols:
                    v = (r["cells"].get(n) or {}).get("v")
                    if v is not None:
                        vals.append(str(v))
    return phi.identifier_tokens(vals)


# ---- evidence queries ------------------------------------------------------------------

def create_evidence_query(case_id: str, conversation_id: str, question_message_id: str | None, raw: dict,
                          supersedes: str | None = None) -> dict:
    qid = new_id("evq")
    raw = {k: v for k, v in raw.items() if v not in (None, [], "")}
    try:
        req = policy.EvidenceRequest(**raw)
        res = policy.evaluate(req, case_identifiers(case_id))
    except Exception as e:  # noqa: BLE001 - pydantic validation
        res = policy.PolicyResult(ok=False, problems=[f"Invalid research topic: {str(e).splitlines()[0][:200]}"])
    if res.blocked:
        status = "blocked"
    elif res.ok:
        status = "pending_approval"
    else:
        status = "needs_revision"
    norm = res.request or {"concepts": [], "population": None, "study_types": [], "year_from": None, "year_to": None}
    h = policy.request_hash(norm)
    search = policy.build_search(norm)["query"] if res.request else ""
    db.ex(
        "INSERT INTO evidence_queries(id, case_id, conversation_id, question_message_id, request, request_hash, search_string,"
        " held_terms, status, policy_version, provider, outcome, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (qid, case_id, conversation_id, question_message_id, dumps(norm), h, search,
         dumps({"held": res.held_terms, "problems": res.problems, "blocked": [b["type"] for b in res.blocked],
                "supersedes": supersedes}),
         status, policy.POLICY_VERSION, "pubmed-mcp", None, now()),
    )
    if supersedes:
        db.ex("UPDATE evidence_queries SET status='superseded' WHERE id=? AND status IN ('pending_approval','needs_revision','blocked')",
              (supersedes,))
    audit("agent", "evidence.propose", case_id, [qid], status)
    return evidence_query_view(qid)


def evidence_query_view(qid: str) -> dict:
    r = db.row_dict(db.q1("SELECT * FROM evidence_queries WHERE id=?", (qid,)))
    assert r
    norm = loads(r["request"])
    held = loads(r["held_terms"], {})
    return {"id": r["id"], "status": r["status"], "request": norm, "request_hash": r["request_hash"],
            "description": policy.describe(norm) if norm.get("concepts") else "",
            "search_string": r["search_string"], "held_terms": held.get("held", []), "problems": held.get("problems", []),
            "blocked_types": held.get("blocked", []), "provider": r["provider"], "outcome": loads(r["outcome"]) if r["outcome"] else None,
            "approved_at": r["approved_at"], "policy_version": r["policy_version"], "conversation_id": r["conversation_id"],
            "case_id": r["case_id"]}


def _gateway(path: str, body: dict) -> dict:
    with httpx.Client(base_url=settings.gateway_url, timeout=settings.gateway_timeout_s * 3, trust_env=False) as c:
        r = c.post(path, json=body, headers={"X-Gateway-Key": settings.gateway_secret()})
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except Exception:  # noqa: BLE001
            detail = f"HTTP {r.status_code}"
        raise JobError(f"Evidence gateway: {detail}")
    return r.json()


def _add_message(case_id: str, conversation_id: str, role: str, content: str, meta: dict, job_id: str | None) -> str:
    mid = new_id("msg")
    db.ex("INSERT INTO messages(id, conversation_id, case_id, role, content, meta, job_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
          (mid, conversation_id, case_id, role, content, dumps(meta), job_id, now()))
    db.index_text(case_id, "message", mid, content)
    return mid


def _reviewed_records(case_id: str, limit: int = 30) -> list[dict]:
    out = []
    for ds in db.q("SELECT id FROM datasets WHERE case_id=? AND review_state IN ('reviewed','imported') ORDER BY created_at DESC LIMIT 3",
                   (case_id,)):
        d = datasets.load(ds["id"])
        for r in d["rows"]:
            if len(out) >= limit:
                return out
            parts = []
            for c in d["columns"]:
                cell = r["cells"].get(c["name"]) or {}
                if c["name"].lower() in {"patient_name", "name", "patient", "mrn", "patient_id", "dob", "date_of_birth"}:
                    continue
                if cell.get("v") is not None:
                    parts.append(f"{c['name']}={cell['v']}")
            out.append({"ref": f"R{len(out) + 1}", "dataset_id": d["id"], "row_id": r["id"], "text": "; ".join(parts)[:300]})
    return out


@handler("evidence_answer", heavy=True)
def evidence_answer(ctx: JobContext) -> dict:
    q = evidence_query_view(ctx.payload["query_id"])
    conv = q["conversation_id"]
    db.ex("DELETE FROM messages WHERE job_id=?", (ctx.id,))  # retry must not duplicate the answer
    if q["status"] not in {"approved", "completed", "failed"}:
        raise JobError("This research topic has not been approved.")
    qm = db.q1("SELECT content FROM evidence_queries e JOIN messages m ON m.id=e.question_message_id WHERE e.id=?", (q["id"],))
    question = qm["content"] if qm else q["description"]
    ctx.stage("retrieving_evidence", f"Searching PubMed for: {q['description']}")
    id_hashes = [hashlib.sha256(t.encode()).hexdigest() for t in case_identifiers(ctx.case_id)]
    try:
        sr = _gateway("/v1/search", {"request": q["request"], "request_hash": q["request_hash"], "identifier_hashes": id_hashes})
    except (JobError, httpx.HTTPError) as e:
        msg = str(e) if isinstance(e, JobError) else "Evidence gateway is not reachable."
        db.ex("UPDATE evidence_queries SET status='failed', outcome=? WHERE id=?", (dumps({"error": msg}), q["id"]))
        _add_message(ctx.case_id, conv, "assistant",
                     f"Online evidence search is unavailable ({msg}). No answer was generated from model memory. "
                     "Local record review, transformation and export still work.",
                     {"type": "evidence_unavailable", "query_id": q["id"]}, ctx.id)
        return {"query_id": q["id"], "outcome": "unavailable"}
    pmids = sr.get("pmids", [])[:6]
    ctx.check()
    articles: list[dict] = []
    if pmids:
        ctx.stage("retrieving_evidence", f"Fetching {len(pmids)} article records")
        fr = _gateway("/v1/fetch", {"request_hash": q["request_hash"], "pmids": pmids, "fulltext": True})
        articles = fr.get("articles", [])
    by_pmid = {a["pmid"]: a for a in articles}
    passages = ans.select_passages(articles, question, q["request"]["concepts"])
    db.ex("DELETE FROM evidence_records WHERE query_id=?", (q["id"],))
    for a in articles:
        rec_passages = [p for p in passages if p["pmid"] == a["pmid"]]
        content = dumps({k: a.get(k) for k in ("title", "abstract", "fulltext")})
        db.ex("INSERT INTO evidence_records(id, case_id, query_id, pmid, doi, pmcid, metadata, passages, retrieval_time, content_hash, access_level)"
              " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              (new_id("evr"), ctx.case_id, q["id"], a["pmid"], a.get("doi"), a.get("pmcid"),
               dumps({k: v for k, v in a.items() if k not in {"abstract", "fulltext"}}), dumps(rec_passages), sr["retrieved_at"],
               hashlib.sha256(content.encode()).hexdigest(), "full text" if a.get("fulltext") else ("abstract" if a.get("abstract") else "citation")))
    outcome = {"total": sr.get("total"), "pmids": pmids, "effective_query": sr.get("effective_query"), "retrieved_at": sr["retrieved_at"]}
    if not passages:
        db.ex("UPDATE evidence_queries SET status='completed', outcome=? WHERE id=?", (dumps({**outcome, "result": "insufficient"}), q["id"]))
        _add_message(ctx.case_id, conv, "assistant",
                     "The approved search returned no usable evidence passages, so I cannot answer this from the literature. "
                     "I will not answer from model memory. Consider broadening the topic or consulting a current guideline.",
                     {"type": "evidence_answer", "query_id": q["id"], "answer": {"findings": [], "citations": [], "unanswered": [question[:300]],
                                                                                "limitations": [], "case_interpretation": [], "conflicts": []}}, ctx.id)
        return {"query_id": q["id"], "outcome": "insufficient"}
    ctx.stage("generating", "Drafting an answer from the retrieved passages")
    records = _reviewed_records(ctx.case_id)
    system, user = ans.build_prompt(question, passages, by_pmid, records)
    try:
        draft = llm.chat_json(system, user, max_tokens=1800)
    except llm.ModelUnavailable as e:
        raise JobError(str(e)) from None
    except ValueError:
        draft = {}
    result = ans.validate(draft, passages, records, by_pmid)
    if not draft:
        result["limitations"].insert(0, "The local model returned no usable draft (unparseable or truncated output), "
                                        "so no answer is given. Retry, or review the retrieved sources directly.")
    result["records"] = [r for r in records if any(r["ref"] in f.get("records", []) for f in result["case_interpretation"])]
    result["retrieved_at"] = sr["retrieved_at"]
    result["query"] = {"id": q["id"], "description": q["description"], "effective_query": sr.get("effective_query")}
    retracted = [a["pmid"] for a in articles if "retracted" in a.get("flags", [])]
    if retracted:
        result["limitations"].insert(0, f"Excluded retracted article(s): PMID {', '.join(retracted)}.")
    text = _answer_text(result)
    db.ex("UPDATE evidence_queries SET status='completed', outcome=? WHERE id=?", (dumps({**outcome, "result": "answered" if result["findings"] else "abstained"}), q["id"]))
    _add_message(ctx.case_id, conv, "assistant", text, {"type": "evidence_answer", "query_id": q["id"], "answer": result}, ctx.id)
    audit("agent", "evidence.answer", ctx.case_id, [q["id"]], "answered" if result["findings"] else "abstained")
    return {"query_id": q["id"], "outcome": "answered" if result["findings"] else "abstained"}


def _answer_text(r: dict) -> str:
    if not r["findings"]:
        return ("The retrieved evidence does not support a confident answer, so I am not providing one. "
                + (" Open questions: " + "; ".join(r["unanswered"][:3]) if r["unanswered"] else ""))
    lines = ["Literature findings:"]
    lines += [f"- {f['text']} [{', '.join(f['refs'])}]" for f in r["findings"]]
    if r["case_interpretation"]:
        lines.append("Case-specific interpretation (draft):")
        lines += [f"- {f['text']} [{', '.join(f['refs'])}]" for f in r["case_interpretation"]]
    return "\n".join(lines)


# ---- chat turns -----------------------------------------------------------------------

CLASSIFY_SYSTEM = (
    "Classify the doctor's message. Examples: 'Is metformin safe in CKD?' -> evidence_question; "
    "'Make an Excel sheet of the labs' -> data_task; 'Sort by date' -> data_task; 'hello' -> other. intent is one of: "
    "evidence_question (asks for medical knowledge: treatment, dosing, risks, guidelines, evidence, interpretation needing literature), "
    "data_task (read records, extract, tables, sort/filter, create Excel/CSV/JSON/Word/PDF files), "
    "other (greetings, app help). Return JSON {\"intent\": \"...\"}."
)

CONCEPT_SYSTEM = (
    "Turn a doctor's question into a GENERAL literature search topic. Remove everything about the specific patient: "
    "names, ages, dates, IDs, places, rare details. Use 1-4 broad medical concepts, preferably from CANDIDATES. "
    "Return JSON {\"concepts\":[...], \"population\": one of [adults, older adults, children, adolescents, infants, pregnant women, women, men] or null, "
    "\"study_types\": subset of [systematic review, meta-analysis, randomized controlled trial, guideline, review, clinical trial, observational study]}."
)


_DATA_WORDS = re.compile(r"\b(excel|xlsx|sheet|spreadsheet|table|csv|json|pdf|docx|word|file|export|sort|filter|column|row|"
                         r"rename|extract|transcribe|scan|upload|dataset|report|summary of (?:the )?(?:labs|results|records))\b", re.I)
_KNOWLEDGE_WORDS = re.compile(r"\b(should|recommend\w*|benefit\w*|effective\w*|efficacy|safe(?:ty)?|risk\w*|dos(?:e|ing|age)|"
                              r"first[- ]line|treat\w*|therap\w*|manage\w*|guideline\w*|evidence|contraindicat\w*|interact\w*|"
                              r"prognos\w*|diagnos\w*|indicat\w*|outcomes?|side effects?|adverse|prevent\w*|why|what causes)\b", re.I)


def _classify(text: str) -> str:
    """Model classification with a deterministic backstop.

    A medical-knowledge question must never be answered by the data agent from
    model memory, so a clear knowledge question with known medical concepts and
    no data-task wording is routed to evidence retrieval regardless of the model.
    """
    heuristic_evidence = bool(policy.match_in_text(text)) and bool(_KNOWLEDGE_WORDS.search(text)) and not _DATA_WORDS.search(text)
    try:
        r = llm.chat_json(CLASSIFY_SYSTEM, text[:2000], max_tokens=50)
        intent = str(r.get("intent", "")).strip()
    except ValueError:
        intent = ""
    if intent not in {"evidence_question", "data_task", "other"}:
        intent = "data_task"
    if heuristic_evidence:
        return "evidence_question"
    return intent


def _propose_topic(text: str) -> dict:
    candidates = policy.match_in_text(text)
    try:
        r = llm.chat_json(CONCEPT_SYSTEM, f"CANDIDATES: {candidates}\n\nQUESTION (local only): {text[:2000]}", max_tokens=200)
    except ValueError:
        r = {}
    concepts = [str(c) for c in (r.get("concepts") or []) if isinstance(c, (str, int))][:6]
    # Keep model concepts that resolve; fall back to deterministic candidates.
    resolved = [c for c in concepts if policy.resolve(c)]
    if not resolved:
        concepts = candidates[:3] or concepts
    pop = r.get("population") if r.get("population") in policy.POPULATION_FILTER else None
    st = [s for s in (r.get("study_types") or []) if s in policy.STUDY_TYPE_PT][:3] or ["systematic review", "meta-analysis", "guideline"]
    return {"concepts": concepts or ["treatment"], "population": pop, "study_types": st}


@handler("chat", heavy=True)
def chat_turn(ctx: JobContext) -> dict:
    conv = ctx.payload["conversation_id"]
    text = ctx.payload["text"]
    db.ex("DELETE FROM messages WHERE job_id=? AND role='assistant'", (ctx.id,))
    created_this_turn: list[dict] = []

    def _rollback():
        for c in created_this_turn:
            if c["type"] == "artifact":
                r = db.q1("SELECT storage_ref FROM artifacts WHERE id=?", (c["id"],))
                if r:
                    storage.path_of(r["storage_ref"]).unlink(missing_ok=True)
                db.ex("DELETE FROM artifacts WHERE id=?", (c["id"],))
            elif c["type"] == "dataset":
                db.ex("DELETE FROM datasets WHERE id=?", (c["id"],))
    ctx.on_cleanup(_rollback)

    st = llm.status()
    if not st.get("ok"):
        raise JobError(f"The model ({llm.model_label()}) is not available: {st.get('error', 'not reachable')}. "
                       "No other model is used as a fallback.")
    ctx.stage("generating", "Understanding the request")
    intent = _classify(text)
    ctx.emit("intent", {"intent": intent})
    if intent == "evidence_question":
        ctx.stage("generating", "Separating case details from a general research topic")
        raw = _propose_topic(text)
        q = create_evidence_query(ctx.case_id, conv, ctx.payload["message_id"], raw)
        if q["status"] == "pending_approval":
            content = ("This needs a literature search. Please review the general research topic below. "
                       "Nothing has been sent; only the topic shown will leave this workstation after you approve it.")
        elif q["status"] == "blocked":
            content = ("I could not form a safe general research topic: a possible identifier was detected. "
                       "Edit the topic in general terms, or research this manually.")
        else:
            content = "I could not form a valid general research topic. Edit the concepts below, or research this manually."
        _add_message(ctx.case_id, conv, "assistant", content, {"type": "evidence_approval", "query_id": q["id"]}, ctx.id)
        ctx.emit("evidence_query", {"query_id": q["id"], "status": q["status"]})
        return {"intent": intent, "query_id": q["id"]}

    agent = get_adapter()
    ctx.stage("generating", "Working on the request")
    emit = ctx.emit

    def tracking_emit(t: str, d: dict) -> None:
        if t == "artifact":
            created_this_turn.append({"type": "artifact", "id": d["artifact_id"]})
        if t == "dataset" and d.get("dataset_id"):
            created_this_turn.append({"type": "dataset", "id": d["dataset_id"]})
        emit(t, d)

    res = agent.run_turn(ctx.case_id, conv, text, tracking_emit, ctx.check)
    ctx.check()
    # The model's prose can misstate what it made (e.g. call a DOCX a PDF);
    # append the facts from the database.
    facts = []
    for c in res.created:
        if c["type"] == "artifact":
            a = db.q1("SELECT format, title, version, review_state FROM artifacts WHERE id=?", (c["id"],))
            if a:
                facts.append(f"{a['format'].upper()} file \"{a['title']}\" v{a['version']}" + (" (DRAFT)" if a["review_state"] == "draft" else ""))
        elif c["type"] == "dataset":
            d = db.q1("SELECT label, version, json_array_length(rows) n FROM datasets WHERE id=?", (c["id"],))
            if d:
                facts.append(f"dataset \"{d['label']}\" v{d['version']} ({d['n']} rows)")
    if facts:
        res.text = (res.text.strip() + "\n\nCreated: " + "; ".join(facts) + ".").strip()
    _add_message(ctx.case_id, conv, "assistant", res.text or "(no reply)",
                 {"type": "agent", "tools": res.tool_events, "created": res.created, "steps": res.steps,
                  "agent": f"{agent.name}/{agent.version}", "model": llm.model_label(),
                  "provider": llm.provider_name()}, ctx.id)
    created_this_turn.clear()  # success: keep outputs
    return {"intent": intent, "created": res.created, "steps": res.steps}


@handler("artifact")
def artifact_job(ctx: JobContext) -> dict:
    p = ctx.payload
    ctx.stage("generating", f"Creating {p['format']}")
    answer = None
    if p.get("message_id"):
        m = db.q1("SELECT meta FROM messages WHERE id=? AND case_id=?", (p["message_id"], ctx.case_id))
        answer = loads(m["meta"], {}).get("answer") if m else None
    try:
        aid = artifacts.create(ctx.case_id, p["format"], p.get("dataset_id"), p.get("title"), answer=answer, lineage_id=p.get("lineage_id"))
    except (artifacts.ArtifactError, KeyError) as e:
        raise JobError(str(e)) from None
    ctx.emit("artifact", {"artifact_id": aid})
    return {"artifact_id": aid}
