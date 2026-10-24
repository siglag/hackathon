"""Versioned datasets and validated transformations (spec 5, 10, 12).

Transforms never mutate a dataset: each produces a new version with its
parent and the operation list recorded. Only the operations below exist; the
model cannot submit code, SQL or expressions.
"""
from __future__ import annotations

import copy
import statistics
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from . import db
from .util import dumps, loads, new_id, now

FILTER_OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "contains", "is_null", "not_null"}
AGG_FNS = {"count", "sum", "mean", "min", "max"}


class Op(BaseModel):
    op: str
    column: str | None = None
    columns: list[str] | None = None
    operator: str | None = None
    value: Any = None
    direction: str | None = "asc"
    to: str | None = None
    name: str | None = None
    group_by: list[str] | None = None
    metrics: list[dict] | None = None
    row_id: str | None = None

    @field_validator("op")
    @classmethod
    def _op(cls, v: str) -> str:
        if v not in {"filter", "sort", "select", "rename", "aggregate", "add_column", "edit_cell", "drop_rows"}:
            raise ValueError(f"unsupported operation '{v}'")
        return v


class OpList(BaseModel):
    ops: list[Op] = Field(min_length=1, max_length=20)


class TransformError(ValueError):
    pass


def create(case_id: str, label: str, columns: list[dict], rows: list[dict], source_documents: list[str],
           review_state: str, parent: dict | None = None, transformations: list[dict] | None = None,
           meta: dict | None = None) -> str:
    did = new_id("ds")
    lineage = parent["lineage_id"] if parent else did
    version = (parent["version"] + 1) if parent else 1
    db.ex(
        "INSERT INTO datasets(id, case_id, label, parent_id, lineage_id, version, columns, rows, source_documents, transformations, review_state, meta, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (did, case_id, label, parent["id"] if parent else None, lineage, version, dumps(columns), dumps(rows),
         dumps(source_documents), dumps(transformations or []), review_state, dumps(meta or {}), now()),
    )
    db.index_text(case_id, "dataset", did, label + " " + " ".join(c["name"] for c in columns))
    return did


def load(dataset_id: str) -> dict:
    r = db.q1("SELECT * FROM datasets WHERE id=?", (dataset_id,))
    if r is None:
        raise KeyError(dataset_id)
    d = dict(r)
    for k in ("columns", "rows", "source_documents", "transformations", "meta"):
        d[k] = loads(d[k], [] if k != "meta" else {})
    return d


def summary(d: dict) -> dict:
    return {
        "id": d["id"], "label": d["label"], "version": d["version"], "lineage_id": d["lineage_id"], "parent_id": d["parent_id"],
        "review_state": d["review_state"], "columns": d["columns"], "row_count": len(d["rows"]),
        "transformations": d["transformations"], "source_documents": d["source_documents"], "created_at": d["created_at"],
        "meta": d.get("meta", {}),
    }


def _val(row: dict, col: str) -> Any:
    c = row["cells"].get(col)
    return None if c is None else c.get("v")


def _cmp(a: Any, op: str, b: Any) -> bool:
    if op == "is_null":
        return a is None
    if op == "not_null":
        return a is not None
    if op == "contains":
        return a is not None and str(b).lower() in str(a).lower()
    if a is None:
        return False
    if isinstance(a, (int, float)) and not isinstance(b, (int, float)):
        try:
            b = float(b)
        except (TypeError, ValueError):
            return False if op not in {"ne"} else True
    if isinstance(a, str) and not isinstance(b, str):
        b = str(b)
    try:
        return {"eq": a == b, "ne": a != b, "gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
    except TypeError:
        return False


def _sort_key(v: Any):
    # None sorts last; numbers before strings, never coerced.
    if v is None:
        return (2, 0, "")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return (0, v, "")
    return (1, 0, str(v).lower())


def apply_ops(d: dict, ops: list[Op]) -> tuple[list[dict], list[dict], list[dict]]:
    cols = copy.deepcopy(d["columns"])
    rows = copy.deepcopy(d["rows"])
    names = lambda: [c["name"] for c in cols]  # noqa: E731
    applied = []
    for o in ops:
        def need(col: str | None) -> str:
            if not col or col not in names():
                raise TransformError(f"Unknown column '{col}'. Available: {names()}")
            return col

        if o.op == "filter":
            col = need(o.column)
            if o.operator not in FILTER_OPS:
                raise TransformError(f"Unsupported filter operator '{o.operator}'.")
            rows = [r for r in rows if _cmp(_val(r, col), o.operator, o.value)]
        elif o.op == "sort":
            col = need(o.column)
            rows.sort(key=lambda r: _sort_key(_val(r, col)), reverse=(o.direction == "desc"))
            if o.direction == "desc":  # keep nulls last on descending sorts too
                rows = [r for r in rows if _val(r, col) is not None] + [r for r in rows if _val(r, col) is None]
        elif o.op == "select":
            keep = [need(c) for c in (o.columns or [])]
            if not keep:
                raise TransformError("select needs at least one column")
            cols = [c for c in cols if c["name"] in keep]
            for r in rows:
                r["cells"] = {k: v for k, v in r["cells"].items() if k in keep}
        elif o.op == "rename":
            col = need(o.column)
            if not o.to or o.to in names():
                raise TransformError("rename target missing or already exists")
            for c in cols:
                if c["name"] == col:
                    c["name"] = o.to
            for r in rows:
                if col in r["cells"]:
                    r["cells"][o.to] = r["cells"].pop(col)
        elif o.op == "add_column":
            if not o.name or o.name in names():
                raise TransformError("add_column needs a new, unique name")
            src_col = o.column
            if src_col:
                need(src_col)
            cols.append({"name": o.name, "type": "string", "derived": True})
            for r in rows:
                if src_col:  # copy unit/metadata of another column's cells
                    c = r["cells"].get(src_col, {})
                    v = c.get("unit") if o.value == "unit_of" else c.get("v")
                    r["cells"][o.name] = {"v": v, "derived_from": src_col}
                else:
                    r["cells"][o.name] = {"v": o.value, "derived": "constant"}
        elif o.op == "aggregate":
            gb = [need(c) for c in (o.group_by or [])]
            metrics = o.metrics or [{"fn": "count"}]
            for m in metrics:
                if m.get("fn") not in AGG_FNS:
                    raise TransformError(f"Unsupported aggregate '{m.get('fn')}'")
                if m.get("fn") != "count":
                    need(m.get("column"))
            groups: dict[tuple, list[dict]] = {}
            for r in rows:
                groups.setdefault(tuple(_val(r, c) for c in gb), []).append(r)
            new_cols = [dict(next(c for c in cols if c["name"] == g)) for g in gb]
            new_rows = []
            for gi, (key, members) in enumerate(groups.items()):
                cells = {g: {"v": k} for g, k in zip(gb, key)}
                for m in metrics:
                    fn, col = m["fn"], m.get("column")
                    label = f"{fn}_{col}" if col else "count"
                    if fn == "count":
                        v = len(members)
                    else:
                        nums = [x for x in (_val(r, col) for r in members) if isinstance(x, (int, float)) and not isinstance(x, bool)]
                        v = None if not nums else {"sum": sum, "mean": statistics.fmean, "min": min, "max": max}[fn](nums)
                    cells[label] = {"v": v, "derived": {"fn": fn, "column": col, "n_rows": len(members),
                                                        "input_rows": [r["id"] for r in members][:1000]}}
                new_rows.append({"id": f"g{gi}", "cells": cells})
            for m in metrics:
                label = f"{m['fn']}_{m.get('column')}" if m.get("column") else "count"
                new_cols.append({"name": label, "type": "number", "derived": True})
            cols, rows = new_cols, new_rows
        elif o.op == "edit_cell":
            col = need(o.column)
            target = next((r for r in rows if r["id"] == o.row_id), None)
            if target is None:
                raise TransformError(f"Unknown row '{o.row_id}'")
            old = target["cells"].get(col, {})
            target["cells"][col] = {**old, "v": o.value, "r": str(o.value) if o.value is not None else None,
                                    "edited": {"previous": old.get("v")}, "rs": "corrected"}
        elif o.op == "drop_rows":
            ids = set(o.columns or [])
            rows = [r for r in rows if r["id"] not in ids]
        applied.append(o.model_dump(exclude_none=True))
    from .ingest.tabular import infer_type
    for c in cols:
        if not c.get("derived"):
            c["type"] = infer_type([_val(r, c["name"]) for r in rows[:5000]])
    return cols, rows, applied


def transform(dataset_id: str, raw_ops: list[dict], label: str | None = None) -> str:
    try:
        ops = OpList(ops=raw_ops).ops
    except ValidationError as e:
        raise TransformError(f"Invalid operations: {e.errors()[0].get('msg')}") from None
    d = load(dataset_id)
    cols, rows, applied = apply_ops(d, ops)
    # Edits by the doctor do not make a draft dataset reviewed; reviewed stays reviewed.
    return create(d["case_id"], label or d["label"], cols, rows, d["source_documents"], d["review_state"], parent=d,
                  transformations=d["transformations"] + [{"ops": applied, "at": now(), "parent": d["id"]}], meta=d.get("meta"))


def page(d: dict, offset: int, limit: int, sort: str | None, desc: bool, search: str | None) -> dict:
    rows = d["rows"]
    if search:
        s = search.lower()
        rows = [r for r in rows if any(s in str(c.get("v", "")).lower() for c in r["cells"].values())]
    if sort and sort in {c["name"] for c in d["columns"]}:
        rows = sorted(rows, key=lambda r: _sort_key(_val(r, sort)), reverse=desc)
    return {"total": len(rows), "offset": offset, "rows": rows[offset: offset + limit]}
