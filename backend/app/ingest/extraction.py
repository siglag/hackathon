"""Field extraction proposals and deterministic checks (spec 8.6-8.9, 12).

Rules enforced here:
* Every field keeps ``raw_text`` exactly as recognized, plus a parsed value.
* Qwen may only propose *mappings* of existing line cells to schema fields.
  A proposed value that is not literally present in the referenced line is
  discarded: the model can never introduce a medical value.
* Units and decimal punctuation are never silently changed; ambiguous values
  stay ``None`` with an issue for the doctor.
* Clinically material fields always require review regardless of OCR score.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..util import strip_think

log = logging.getLogger("medassist.extraction")

EXTRACTOR_VERSION = "lab-extract-1"

SCHEMAS = {
    "lab_results": {
        "row_fields": ["test", "value", "unit", "reference_range", "flag", "collection_date"],
        "critical": {"test", "value", "unit", "collection_date"},
        "header_fields": ["patient_name", "patient_id", "date_of_birth", "collection_date", "facility"],
    },
    "medications": {
        "row_fields": ["medication", "dose", "unit", "route", "frequency", "start_date"],
        "critical": {"medication", "dose", "unit", "route", "frequency", "start_date"},
        "header_fields": ["patient_name", "patient_id", "date_of_birth", "allergies", "facility"],
    },
}

# Header fields that identify a person: never leave the workstation and feed
# the outbound identifier detector.
IDENTIFYING_FIELDS = {"patient_name", "patient_id", "date_of_birth"}

KNOWN_UNITS = {
    "g/dl", "g/l", "mg/dl", "mg/l", "mmol/l", "umol/l", "µmol/l", "μmol/l", "nmol/l", "pmol/l", "%", "u/l", "iu/l",
    "miu/l", "uiu/ml", "µiu/ml", "ng/ml", "pg/ml", "ng/dl", "meq/l", "x10^9/l", "x10^12/l", "10^9/l", "10^12/l",
    "10*9/l", "10*3/ul", "10^3/ul", "k/ul", "m/ul", "fl", "pg", "mm/h", "mm/hr", "sec", "s", "ratio", "mg", "g", "mcg",
    "µg", "ml", "l", "units", "iu", "ml/min/1.73m2", "ml/min", "mosm/kg", "cells/ul", "/ul", "/hpf", "mmhg", "bpm",
}

_NUM = re.compile(r"^(?P<cmp>[<>≤≥]=?)?\s*(?P<num>[+-]?\d+(?:\.\d+)?)$")
_COMMA_NUM = re.compile(r"^[<>≤≥]?\s*\d+,\d+$")
_RANGE = re.compile(r"^[<>≤≥]?\s*\d+(?:[.,]\d+)?\s*(?:[-–—]|to)\s*\d+(?:[.,]\d+)?$|^[<>≤≥]\s*\d+(?:[.,]\d+)?$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_AMBIG_DATE = re.compile(r"^\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}$")
_ARABIC_INDIC = re.compile(r"[٠-٩۰-۹]")
_FLAG = re.compile(r"^(H|L|HH|LL|High|Low|Abnormal|\*)$", re.I)
_HEADER_PATTERNS = {
    "patient_name": re.compile(r"\b(?:patient(?:\s+name)?|name)\s*[:\-]\s*(.+?)(?=\s{2,}|\s+(?:MRN|ID|DOB|Age|Sex)\b|$)", re.I),
    "patient_id": re.compile(r"\b(?:MRN|patient\s*id|hospital\s*(?:no|number)|ID)\s*[:#\-]\s*([A-Za-z0-9\-/]+)", re.I),
    "date_of_birth": re.compile(r"\b(?:DOB|date\s+of\s+birth|birth\s*date)\s*[:\-]\s*([0-9./\-]+|\d{1,2}\s+\w+\s+\d{4})", re.I),
    "collection_date": re.compile(r"\b(?:collected|collection\s+date|sample\s+date|date\s+collected|specimen\s+date)\s*[:\-]\s*([0-9./\-]+)", re.I),
    "allergies": re.compile(r"\ballerg(?:y|ies)\s*[:\-]\s*(.+)$", re.I),
}


def parse_number(raw: str) -> tuple[float | None, dict, list[str]]:
    s = raw.strip()
    extra: dict[str, Any] = {}
    if _ARABIC_INDIC.search(s):
        return None, extra, ["Arabic-Indic digits are not validated in this release; enter the value manually."]
    if _COMMA_NUM.match(s):
        return None, extra, ["Comma decimal separator: confirm the intended value (not converted automatically)."]
    m = _NUM.match(s)
    if not m:
        return None, extra, ["Not a plain number; kept as text."]
    if m.group("cmp"):
        extra["comparator"] = m.group("cmp")
    return float(m.group("num")), extra, []


def parse_date(raw: str) -> tuple[str | None, list[str]]:
    s = raw.strip()
    if _ISO_DATE.match(s):
        return s, []
    if _AMBIG_DATE.match(s):
        return None, ["Date order (day/month) is ambiguous; confirm."]
    return None, ["Unrecognized date format; kept as text."]


def make_field(raw: str | None, name: str, src: dict | None, score: float | None, critical: bool) -> dict:
    f: dict[str, Any] = {
        "raw_text": raw,
        "value": None,
        "source": src,
        "ocr_score": score,
        "parser_version": EXTRACTOR_VERSION,
        "review_state": "unreviewed",
        "critical": critical,
        "issues": [],
    }
    if raw is None or raw.strip() == "":
        f["raw_text"] = None
        if critical:
            f["issues"].append("Missing; stays unknown unless entered manually.")
        return f
    if name in {"value", "dose"}:
        v, extra, issues = parse_number(raw)
        f["value"] = v
        f.update(extra)
        f["issues"] += issues
    elif name.endswith("date") or name == "date_of_birth":
        f["value"], issues = parse_date(raw)
        f["issues"] += issues
    elif name == "unit":
        f["value"] = raw.strip()
        if raw.strip().lower().replace(" ", "") not in KNOWN_UNITS:
            f["issues"].append("Unit not in the recognized list; verify.")
    else:
        f["value"] = raw.strip()
    if score is not None and score < 0.9:
        f["issues"].append(f"Low recognition score ({score:.2f}).")
    return f


def _span_map(canonical: dict) -> dict[str, dict]:
    return {s["id"]: {**s, "page": p["page"]} for p in canonical["pages"] for s in p["spans"]}


def _src_for(line: dict, cell_index: int | None, spans: dict, doc_id: str) -> tuple[dict, float | None]:
    if cell_index is None:
        sid_list = line["span_ids"]
    else:
        sid_list = [line["span_ids"][cell_index]]
    ss = [spans[s] for s in sid_list]
    bbox = [min(s["bbox"][0] for s in ss), min(s["bbox"][1] for s in ss), max(s["bbox"][2] for s in ss), max(s["bbox"][3] for s in ss)]
    scores = [s["score"] for s in ss if s.get("score") is not None]
    return {"document_id": doc_id, "page": ss[0]["page"], "bbox": bbox, "span_ids": sid_list, "line_id": line["id"]}, (min(scores) if scores else None)


def _split_cells(line: dict) -> list[tuple[str, int]]:
    """Cells with their span index; splits a span holding 'value unit' pairs."""
    out = []
    for i, c in enumerate(line["cells"]):
        parts = re.split(r"\s{2,}", c.strip())
        for p in parts:
            out.append((p.strip(), i))
    return out


def deterministic_rows(canonical: dict, schema: str) -> tuple[list[dict], dict]:
    """Heuristic lab-row parser over grouped lines."""
    doc_id = canonical["document_id"]
    spans = _span_map(canonical)
    spec = SCHEMAS[schema]
    header: dict[str, dict] = {}
    rows: list[dict] = []
    for page in canonical["pages"]:
        for line in page["lines"]:
            text = line["text"]
            matched_header = False
            for key, rx in _HEADER_PATTERNS.items():
                if key in spec["header_fields"] and key not in header:
                    m = rx.search(text)
                    if m:
                        src, score = _src_for(line, None, spans, doc_id)
                        header[key] = make_field(m.group(1).strip(), key, src, score, True)
                        matched_header = True
            if matched_header or schema != "lab_results":
                continue
            cells = _split_cells(line)
            if len(cells) < 2:
                continue
            # A lab row: name cell (alphabetic) followed by a numeric-looking value.
            name, name_i = cells[0]
            if not re.search(r"[A-Za-z]", name) or _NUM.match(name):
                continue
            vi = next((k for k in range(1, len(cells)) if _NUM.match(cells[k][0]) or _COMMA_NUM.match(cells[k][0])), None)
            if vi is None:
                continue
            rest = cells[vi + 1:]
            unit = next(((c, i) for c, i in rest if not _RANGE.match(c) and not _FLAG.match(c) and not _NUM.match(c)), None)
            rng = next(((c, i) for c, i in rest if _RANGE.match(c)), None)
            flag = next(((c, i) for c, i in rest if _FLAG.match(c)), None)
            fields = {}
            for fname, cell in (("test", (name, name_i)), ("value", cells[vi]), ("unit", unit), ("reference_range", rng), ("flag", flag)):
                if cell is None:
                    fields[fname] = make_field(None, fname, None, None, fname in spec["critical"])
                else:
                    src, score = _src_for(line, cell[1], spans, doc_id)
                    fields[fname] = make_field(cell[0], fname, src, score, fname in spec["critical"])
            rows.append({"id": f"r{len(rows)}", "line_id": line["id"], "page": page["page"], "proposed_by": "rules", "fields": fields})
    # Collection date from the header applies to rows; recorded as a derived copy with its source.
    cd = header.get("collection_date")
    for r in rows:
        if cd is not None:
            f = dict(cd)
            f["issues"] = list(cd["issues"]) + ["Taken from the report header."]
            f["review_state"] = "unreviewed"
            r["fields"]["collection_date"] = f
        else:
            r["fields"]["collection_date"] = make_field(None, "collection_date", None, None, True)
    return rows, header


def model_rows(canonical: dict, schema: str, chat_json) -> list[dict] | None:
    """Ask Qwen to map line cells to schema fields; validate strictly.

    ``chat_json(system, user) -> dict`` is injected so this module stays
    testable without a model.
    """
    spec = SCHEMAS[schema]
    lines = [ln for p in canonical["pages"] for ln in p["lines"]]
    if not lines or len(lines) > 200:
        return None
    by_id = {ln["id"]: ln for ln in lines}
    listing = "\n".join(f'{ln["id"]}: ' + " | ".join(ln["cells"]) for ln in lines)
    fields = spec["row_fields"]
    system = (
        "You map lines of a recognized medical record into a table schema. "
        "Copy cell text EXACTLY as written. Never invent, correct, convert or infer values. "
        "If a field is not present on the line, use null. Return JSON only."
    )
    user = (
        f"Schema fields per row: {fields}. Only include lines that are {schema.replace('_', ' ')} rows.\n"
        'Return {"rows":[{"line_id":"...", ' + ", ".join(f'"{f}": "exact text or null"' for f in fields) + "}]}\n\n" + listing
    )
    try:
        data = chat_json(system, user)
    except Exception as e:  # noqa: BLE001
        log.warning("model extraction unavailable: %s", type(e).__name__)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        return None
    spans = _span_map(canonical)
    out = []
    for r in data["rows"][:500]:
        if not isinstance(r, dict):
            continue
        line = by_id.get(str(r.get("line_id")))
        if line is None:
            continue
        cells = _split_cells(line)
        row_fields = {}
        for fname in fields:
            raw = r.get(fname)
            if raw is None or fname == "collection_date":
                continue
            raw = str(raw).strip()
            idx = next((ci for c, ci in cells if c == raw), None)
            if idx is None:
                idx = next((ci for c, ci in cells if raw and raw in c), None)
            if idx is None:
                continue  # not literally present: discard (model may not introduce values)
            src, score = _src_for(line, idx, spans, canonical["document_id"])
            row_fields[fname] = make_field(raw, fname, src, score, fname in spec["critical"])
        if not row_fields:
            continue
        for fname in fields:
            row_fields.setdefault(fname, make_field(None, fname, None, None, fname in spec["critical"]))
        out.append({"id": f"r{len(out)}", "line_id": line["id"], "page": spans[line["span_ids"][0]]["page"],
                    "proposed_by": "model", "fields": row_fields})
    return out


def _unexplained_numeric_lines(canonical: dict, rows: list[dict], header: dict) -> int:
    used = {r["line_id"] for r in rows} | {f["source"]["line_id"] for f in header.values() if f.get("source")}
    return sum(1 for p in canonical["pages"] for ln in p["lines"] if ln["id"] not in used and re.search(r"\d", ln["text"]))


def propose(canonical: dict, schema: str, chat_json=None) -> dict:
    rows, header = deterministic_rows(canonical, schema)
    # The model only helps when the rules leave numeric lines unexplained
    # (unusual layouts); it costs minutes per page on a small local GPU.
    if chat_json is not None and (not rows or _unexplained_numeric_lines(canonical, rows, header) >= 2):
        mrows = model_rows(canonical, schema, chat_json)
        if mrows and len(mrows) > len(rows):
            cd = header.get("collection_date")
            for r in mrows:
                f = dict(cd) if cd else make_field(None, "collection_date", None, None, True)
                if cd:
                    f["issues"] = list(cd["issues"]) + ["Taken from the report header."]
                r["fields"]["collection_date"] = f
            rows = mrows
    return {"schema": schema, "header": header, "rows": rows, "extractor_version": EXTRACTOR_VERSION}


def parse_json_reply(text: str) -> Any:
    text = strip_think(text)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in reply")
    return json.loads(m.group(0))


def all_fields(extraction: dict):
    for k, f in extraction.get("header", {}).items():
        yield ("header", k), f
    for r in extraction.get("rows", []):
        for k, f in r["fields"].items():
            yield (r["id"], k), f


def unreviewed_critical(extraction: dict) -> int:
    return sum(1 for _, f in all_fields(extraction) if f["critical"] and f["review_state"] == "unreviewed")
