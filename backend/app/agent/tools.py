"""Restricted application tools (spec 10).

Tools take opaque IDs and resolve them server-side within the active case.
The model cannot submit paths, SQL, shell commands, code or network
addresses. Authorization is enforced here, independent of model output.
Results are bounded in size.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import artifacts, datasets, db
from ..ingest import extraction
from ..util import loads, now

MAX_RESULT_CHARS = 6000


class ToolError(Exception):
    """Error returned to the model as a tool result (not a crash)."""


@dataclass
class ToolContext:
    case_id: str
    conversation_id: str
    actor: str
    emit: Callable[[str, dict], None]
    user_text: str = ""  # the doctor's message this turn; record values must appear in it literally
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


MAX_RECORD_ROWS = 50


def _in_message(raw: str, text: str) -> bool:
    """True when ``raw`` occurs in the message as a whole token (so '1' does not match inside '12.1')."""
    pat = r"(?<![0-9A-Za-z.])" + re.escape(raw) + r"(?![0-9A-Za-z])"
    return re.search(pat, text, re.I) is not None


def record_from_message(ctx: ToolContext, schema: str, rows: list[dict], append_to: str | None = None,
                        label: str | None = None) -> dict:
    if schema not in extraction.SCHEMAS:
        raise ToolError(f"unknown schema '{schema}'")
    if not isinstance(rows, list) or not rows:
        raise ToolError("rows must be a non-empty list")
    if len(rows) > MAX_RECORD_ROWS:
        raise ToolError(f"at most {MAX_RECORD_ROWS} rows per call")
    spec = extraction.SCHEMAS[schema]
    fields = spec["row_fields"]
    text = " ".join(ctx.user_text.split())
    src = {"entered_in_chat": True, "conversation_id": ctx.conversation_id}
    parent = None
    if append_to is not None:
        _case_row(ctx, "datasets", append_to)
        parent = datasets.load(append_to)
        missing_cols = [f for f in fields if f not in {c["name"] for c in parent["columns"]}]
        if missing_cols:
            raise ToolError(f"dataset {append_to} has no columns {missing_cols}; omit append_to to create a new dataset")
    # Same rule as document extraction: the model may only map text the doctor
    # wrote; a value not literally present in the message is rejected.
    new_rows, issues = [], []
    used_ids = {r["id"] for r in parent["rows"]} if parent else set()
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise ToolError("each row must be an object")
        unknown = set(r) - set(fields)
        if unknown:
            raise ToolError(f"row {i}: unknown fields {sorted(unknown)}; {schema} fields are {fields}")
        cells: dict[str, dict] = {}
        for f in fields:
            raw = r.get(f)
            raw = None if raw is None else " ".join(str(raw).split())
            if raw and not _in_message(raw, text):
                raise ToolError(f"row {i}: '{raw}' for {f} is not written in the doctor's message; "
                                "copy values exactly as typed and leave missing ones null")
            fd = extraction.make_field(raw, f, None, None, f in spec["critical"])
            cell = {"v": fd["value"], "r": fd["raw_text"], "src": src, "rs": "unreviewed"}
            if fd.get("comparator"):
                cell["comparator"] = fd["comparator"]
            if fd["issues"]:
                cell["issues"] = fd["issues"]
                issues += [f"row {i} {f}: {x}" for x in fd["issues"]]
            cells[f] = cell
        if all(c["v"] is None and c["r"] is None for c in cells.values()):
            raise ToolError(f"row {i} has no values")
        amount = cells["value" if schema == "lab_results" else "dose"]
        if amount["v"] is not None and cells["unit"]["v"]:
            amount["unit"] = cells["unit"]["v"]
        n = len(used_ids)
        while f"m{n}" in used_ids:
            n += 1
        used_ids.add(f"m{n}")
        new_rows.append({"id": f"m{n}", "cells": cells})
    from ..ingest.tabular import infer_type
    cols = [dict(c) for c in parent["columns"]] if parent else [{"name": f, "type": "string"} for f in fields]
    all_rows = (parent["rows"] if parent else []) + new_rows
    for c in cols:
        if not c.get("derived"):
            c["type"] = infer_type([(r["cells"].get(c["name"]) or {}).get("v") for r in all_rows])
    # Appending unreviewed rows makes the whole version a draft.
    if parent:
        ops = parent["transformations"] + [{"ops": [{"op": "append_rows", "source": "chat", "rows": [r["id"] for r in new_rows]}],
                                            "at": now(), "parent": parent["id"]}]
        did = datasets.create(ctx.case_id, label or parent["label"], cols, all_rows, parent["source_documents"], "draft",
                              parent=parent, transformations=ops, meta=parent.get("meta"))
    else:
        did = datasets.create(ctx.case_id, label or f"{schema.replace('_', ' ').title()} entered in chat", cols, all_rows, [],
                              "draft", meta={"entered_in_chat": True})
    ctx.created.append({"type": "dataset", "id": did})
    ctx.emit("dataset", {"dataset_id": did})
    return {"dataset_id": did, "rows_added": len(new_rows), "row_count": len(all_rows), "review_state": "draft",
            "issues": issues[:20],
            "note": "Values entered in chat are UNREVIEWED until the doctor checks them in the table."}


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
    "record_from_message": (record_from_message, {
        "type": "object", "properties": {
            "schema": {"type": "string", "enum": ["lab_results", "medications"]},
            "rows": {"type": "array", "items": {"type": "object", "properties": {
                f: {"type": "string"} for f in dict.fromkeys(
                    extraction.SCHEMAS["lab_results"]["row_fields"] + extraction.SCHEMAS["medications"]["row_fields"])}},
                "description": "One object per result/medication. Copy each value EXACTLY as the doctor typed it; null if not given."},
            "append_to": {"type": "string", "description": "Optional dataset ID to add the rows to (new version)"},
            "label": {"type": "string"}},
         "required": ["schema", "rows"]},
        "Create a record from values the doctor typed in THIS message (e.g. 'Hb 12.1 g/dL on 2026-03-02'). "
        "lab_results fields: test, value, unit, reference_range, flag, collection_date. "
        "medications fields: medication, dose, unit, route, frequency, start_date. "
        "Creates a new draft dataset, or a new version of append_to with the rows added."),
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
