"""Restricted application tools (spec 10).

Tools take opaque IDs and resolve them server-side within the active case.
The model cannot submit paths, SQL, shell commands, code or network
addresses. Authorization is enforced here, independent of model output.
Results are bounded in size.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import artifacts, datasets, db
from ..util import loads

MAX_RESULT_CHARS = 6000


class ToolError(Exception):
    """Error returned to the model as a tool result (not a crash)."""


@dataclass
class ToolContext:
    case_id: str
    conversation_id: str
    actor: str
    emit: Callable[[str, dict], None]
    created: list[dict] = field(default_factory=list)  # artifacts/datasets created this turn
    evidence_queries: list[str] = field(default_factory=list)


def _case_row(ctx: ToolContext, table: str, obj_id: Any) -> dict:
    if not isinstance(obj_id, str) or len(obj_id) > 40:
        raise ToolError("invalid id")
    r = db.q1(f"SELECT * FROM {table} WHERE id=? AND case_id=?", (obj_id, ctx.case_id))
    if r is None:
        raise ToolError(f"{obj_id} not found in the active case")
    return dict(r)


def read_document(ctx: ToolContext, document_id: str, page: int | None = None, max_lines: int = 60) -> dict:
    doc = _case_row(ctx, "documents", document_id)
    ex = db.q1("SELECT canonical, review_state FROM extractions WHERE document_id=?", (doc["id"],))
    if ex is None:
        meta = loads(doc["meta"], {})
        return {"document_id": doc["id"], "kind": doc["kind"], "state": doc["state"], "tables": meta.get("datasets", []),
                "note": "No text layer; for spreadsheets use read_table with the dataset IDs."}
    canon = loads(ex["canonical"])
    lines = []
    for p in canon["pages"]:
        if page and p["page"] != page:
            continue
        for ln in p["lines"]:
            lines.append({"ref": f"{doc['id']}:{ln['id']}", "page": p["page"], "text": ln["text"]})
    max_lines = max(1, min(int(max_lines), 120))
    return {"document_id": doc["id"], "pages": len(canon["pages"]), "review_state": ex["review_state"],
            "lines": lines[:max_lines], "truncated": len(lines) > max_lines,
            "note": "Transcribed record text (OCR); values are unverified until reviewed."}


def read_table(ctx: ToolContext, dataset_id: str, columns: list[str] | None = None, offset: int = 0, limit: int = 30) -> dict:
    _case_row(ctx, "datasets", dataset_id)
    d = datasets.load(dataset_id)
    limit = max(1, min(int(limit), 50))
    cols = [c["name"] for c in d["columns"] if not columns or c["name"] in columns]
    rows = []
    for r in d["rows"][int(offset): int(offset) + limit]:
        rows.append({"row": r["id"], **{c: (r["cells"].get(c) or {}).get("v") for c in cols}})
    return {"dataset_id": d["id"], "label": d["label"], "review_state": d["review_state"],
            "columns": [c for c in d["columns"] if c["name"] in cols], "total_rows": len(d["rows"]), "offset": offset, "rows": rows}


def propose_extraction(ctx: ToolContext, document_id: str, schema: str = "lab_results") -> dict:
    from ..pipeline import run_extraction

    doc = _case_row(ctx, "documents", document_id)
    if doc["kind"] not in {"image", "pdf"}:
        raise ToolError("extraction applies to scanned images or PDFs; spreadsheets are read directly")
    res = run_extraction(doc, schema)
    ctx.emit("extraction", {"document_id": doc["id"]})
    return {**res, "note": "Draft fields require the doctor's review in the extraction panel before interpretation."}


def transform_table(ctx: ToolContext, dataset_id: str, operations: list[dict], label: str | None = None) -> dict:
    _case_row(ctx, "datasets", dataset_id)
    if not isinstance(operations, list):
        raise ToolError("operations must be a list")
    try:
        new_id = datasets.transform(dataset_id, operations, label)
    except datasets.TransformError as e:
        raise ToolError(str(e)) from None
    d = datasets.load(new_id)
    ctx.created.append({"type": "dataset", "id": new_id})
    ctx.emit("dataset", {"dataset_id": new_id})
    return {"new_dataset_id": new_id, "version": d["version"], "row_count": len(d["rows"]),
            "columns": [c["name"] for c in d["columns"]], "review_state": d["review_state"]}


def create_artifact(ctx: ToolContext, dataset_id: str, format: str, title: str | None = None) -> dict:
    _case_row(ctx, "datasets", dataset_id)
    prior = db.q1("SELECT lineage_id FROM artifacts WHERE case_id=? AND dataset_id IN "
                  "(SELECT id FROM datasets WHERE lineage_id=(SELECT lineage_id FROM datasets WHERE id=?)) AND format=? "
                  "ORDER BY created_at DESC LIMIT 1", (ctx.case_id, dataset_id, format))
    try:
        aid = artifacts.create(ctx.case_id, format, dataset_id, title, lineage_id=prior["lineage_id"] if prior else None)
    except artifacts.ArtifactError as e:
        raise ToolError(str(e)) from None
    a = db.q1("SELECT * FROM artifacts WHERE id=?", (aid,))
    ctx.created.append({"type": "artifact", "id": aid})
    ctx.emit("artifact", {"artifact_id": aid})
    return {"artifact_id": aid, "format": a["format"], "version": a["version"], "review_state": a["review_state"],
            "note": "Shown in the artifact panel. Downloads are explicit." +
                    (" Contains UNREVIEWED values and is marked DRAFT." if a["review_state"] == "draft" else "")}


def propose_evidence_query(ctx: ToolContext, concepts: list[str], population: str | None = None,
                           study_types: list[str] | None = None, year_from: int | None = None, year_to: int | None = None) -> dict:
    from ..pipeline import create_evidence_query

    q = create_evidence_query(ctx.case_id, ctx.conversation_id, None,
                              {"concepts": concepts, "population": population, "study_types": study_types or [],
                               "year_from": year_from, "year_to": year_to})
    ctx.evidence_queries.append(q["id"])
    return {"query_id": q["id"], "status": q["status"], "outgoing_topic": q.get("description"),
            "note": "Nothing has been sent. The doctor must approve this exact topic first."}


CATALOG: dict[str, tuple[Callable, dict, str]] = {
    "read_document": (read_document, {
        "type": "object", "properties": {"document_id": {"type": "string"}, "page": {"type": "integer"},
                                         "max_lines": {"type": "integer"}}, "required": ["document_id"]},
        "Read transcribed text lines (with source refs) of a document in the active case."),
    "read_table": (read_table, {
        "type": "object", "properties": {"dataset_id": {"type": "string"}, "columns": {"type": "array", "items": {"type": "string"}},
                                         "offset": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["dataset_id"]},
        "Read rows of a dataset (typed values; max 50 rows per call)."),
    "propose_extraction": (propose_extraction, {
        "type": "object", "properties": {"document_id": {"type": "string"},
                                         "schema": {"type": "string", "enum": ["lab_results", "medications"]}}, "required": ["document_id"]},
        "Propose draft structured fields from a scanned document. The doctor must review them."),
    "transform_table": (transform_table, {
        "type": "object", "properties": {
            "dataset_id": {"type": "string"},
            "label": {"type": "string"},
            "operations": {"type": "array", "items": {"type": "object", "properties": {
                "op": {"type": "string", "enum": ["filter", "sort", "select", "rename", "aggregate", "add_column"]},
                "column": {"type": "string"}, "columns": {"type": "array", "items": {"type": "string"}},
                "operator": {"type": "string", "enum": ["eq", "ne", "gt", "gte", "lt", "lte", "contains", "is_null", "not_null"]},
                "value": {}, "direction": {"type": "string", "enum": ["asc", "desc"]}, "to": {"type": "string"},
                "name": {"type": "string"}, "group_by": {"type": "array", "items": {"type": "string"}},
                "metrics": {"type": "array", "items": {"type": "object", "properties": {
                    "fn": {"type": "string", "enum": ["count", "sum", "mean", "min", "max"]}, "column": {"type": "string"}}}}},
                "required": ["op"]}}},
        "required": ["dataset_id", "operations"]},
        "Create a NEW dataset version by applying operations (filter, sort, select, rename, aggregate, add_column). "
        "Example: [{\"op\":\"sort\",\"column\":\"collection_date\",\"direction\":\"asc\"}]. Never overwrites the input."),
    "create_artifact": (create_artifact, {
        "type": "object", "properties": {"dataset_id": {"type": "string"},
                                         "format": {"type": "string", "enum": ["xlsx", "csv", "json", "docx", "pdf", "md"]},
                                         "title": {"type": "string"}}, "required": ["dataset_id", "format"]},
        "Create a downloadable file (Excel=xlsx, CSV, JSON, Word=docx, PDF, Markdown) from a dataset version."),
    "propose_evidence_query": (propose_evidence_query, {
        "type": "object", "properties": {
            "concepts": {"type": "array", "items": {"type": "string"}, "description": "1-4 GENERAL medical concepts; no names, ages, dates or IDs"},
            "population": {"type": "string", "enum": ["adults", "older adults", "children", "adolescents", "infants", "pregnant women", "women", "men"]},
            "study_types": {"type": "array", "items": {"type": "string", "enum": ["systematic review", "meta-analysis", "randomized controlled trial",
                                                                                     "guideline", "review", "clinical trial", "observational study"]}},
            "year_from": {"type": "integer"}, "year_to": {"type": "integer"}}, "required": ["concepts"]},
        "Prepare a GENERAL literature search topic for the doctor to approve. Nothing is sent until approved."),
}


def schemas(names: list[str]) -> list[dict]:
    return [{"type": "function", "function": {"name": n, "description": CATALOG[n][2], "parameters": CATALOG[n][1]}} for n in names]


def execute(ctx: ToolContext, name: str, args: Any, allowed: list[str]) -> str:
    if name not in allowed or name not in CATALOG:
        raise ToolError(f"tool '{name}' is not available")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            raise ToolError("arguments must be a JSON object") from None
    if not isinstance(args, dict):
        raise ToolError("arguments must be a JSON object")
    fn, schema, _ = CATALOG[name]
    props = schema["properties"]
    unknown = set(args) - set(props)
    if unknown:
        raise ToolError(f"unknown arguments: {sorted(unknown)}")
    missing = [k for k in schema.get("required", []) if k not in args]
    if missing:
        raise ToolError(f"missing arguments: {missing}")
    try:
        result = fn(ctx, **args)
    except TypeError:
        raise ToolError("invalid argument types") from None
    out = json.dumps(result, ensure_ascii=False, default=str)
    if len(out) > MAX_RESULT_CHARS:
        out = out[:MAX_RESULT_CHARS] + '..."(truncated)"'
    return out
