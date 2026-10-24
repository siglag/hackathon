"""Artifact generation and previews (spec 6, 10, 12).

* Generated from a dataset version (and optionally an evidence answer) using
  controlled templates and deterministic library calls.
* Formula-like strings are escaped on export; the raw value stays in the
  dataset provenance.
* Artifacts built from unreviewed values carry a conspicuous DRAFT banner.
* DOCX/PDF/HTML previews are rendered from the same report model, so the
  preview and the downloaded file show the same content.
"""
from __future__ import annotations

import csv
import html
import io
import json
from pathlib import Path
from typing import Any

from . import datasets, db, storage
from .config import settings
from .util import dumps, loads, new_id, now

FORMATS = {"xlsx", "csv", "json", "docx", "pdf", "md"}
MIME = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
    "json": "application/json",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
    "md": "text/markdown",
}
DRAFT_BANNER = "DRAFT - contains values NOT reviewed by a clinician. Verify against the source record before clinical use."
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class ArtifactError(ValueError):
    pass


def escape_formula(v: Any) -> Any:
    if isinstance(v, str) and v.startswith(FORMULA_PREFIXES):
        # Numbers like "-5" are not formula-like in practice but still escaped when text.
        return "'" + v
    return v


def _cell_text(c: dict | None) -> Any:
    if c is None:
        return None
    if c.get("unavailable"):
        return "[unavailable]"
    return c.get("v")


# ---- report model --------------------------------------------------------------

def build_report(title: str, dataset: dict | None, answer: dict | None, draft: bool) -> dict:
    sections: list[dict] = []
    if answer:
        sections += answer_sections(answer)
    if dataset:
        sections.append({
            "kind": "table", "heading": f"Data: {dataset['label']} (version {dataset['version']})",
            "columns": [c["name"] for c in dataset["columns"]],
            "rows": [[_cell_text(r["cells"].get(c["name"])) for c in dataset["columns"]] for r in dataset["rows"][:2000]],
            "truncated": len(dataset["rows"]) > 2000,
        })
        sections.append({"kind": "text", "heading": "Provenance", "paragraphs": [
            f"Dataset {dataset['id']} - review state: {dataset['review_state']}.",
            f"Source documents: {', '.join(dataset['source_documents']) or 'none'}.",
            f"Transformations applied: {sum(len(t.get('ops', [])) for t in dataset['transformations'])}.",
        ]})
    return {"title": title, "draft": draft, "banner": DRAFT_BANNER if draft else None,
            "generated_at": now(), "sections": sections}


def answer_sections(answer: dict) -> list[dict]:
    out = []
    lit = answer.get("findings", [])
    if lit:
        out.append({"kind": "claims", "heading": "Literature findings", "claims": lit})
    if answer.get("case_interpretation"):
        out.append({"kind": "claims", "heading": "Case-specific interpretation (assistant draft, requires clinician review)",
                    "claims": answer["case_interpretation"]})
    if answer.get("limitations"):
        out.append({"kind": "text", "heading": "Limitations and uncertainty", "paragraphs": answer["limitations"]})
    if answer.get("citations"):
        out.append({"kind": "citations", "heading": "Sources", "citations": answer["citations"]})
    return out


def report_html(rep: dict) -> str:
    """Escaped HTML preview: no raw HTML from data or model is ever inserted."""
    e = html.escape
    parts = [f"<h1>{e(rep['title'])}</h1>"]
    if rep.get("banner"):
        parts.append(f'<div class="draft-banner">{e(rep["banner"])}</div>')
    for s in rep["sections"]:
        parts.append(f"<h2>{e(s['heading'])}</h2>")
        if s["kind"] == "text":
            parts += [f"<p>{e(str(p))}</p>" for p in s["paragraphs"]]
        elif s["kind"] == "table":
            head = "".join(f"<th>{e(str(c))}</th>" for c in s["columns"])
            body = "".join("<tr>" + "".join(f"<td>{e('' if v is None else str(v))}</td>" for v in row) + "</tr>" for row in s["rows"][:500])
            parts.append(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
            if len(s["rows"]) > 500 or s.get("truncated"):
                parts.append("<p><em>Preview truncated; the file contains all rows.</em></p>")
        elif s["kind"] == "claims":
            items = "".join(f"<li>{e(c['text'])} <span class=\"refs\">{e(' '.join('[' + r + ']' for r in c.get('refs', [])))}</span></li>" for c in s["claims"])
            parts.append(f"<ul>{items}</ul>")
        elif s["kind"] == "citations":
            for c in s["citations"]:
                parts.append(f"<p>[{e(c['ref'])}] {e(c['title'])}. {e(c.get('journal') or '')} {e(str(c.get('year') or ''))}. "
                             f"PMID {e(str(c.get('pmid') or '-'))}. {e(c.get('access') or '')}</p>")
    return "\n".join(parts)


def report_markdown(rep: dict) -> str:
    def md(s: Any) -> str:
        return str(s).replace("|", "\\|").replace("<", "&lt;")
    out = [f"# {md(rep['title'])}", ""]
    if rep.get("banner"):
        out += [f"> **{md(rep['banner'])}**", ""]
    for s in rep["sections"]:
        out += [f"## {md(s['heading'])}", ""]
        if s["kind"] == "text":
            out += [md(p) + "\n" for p in s["paragraphs"]]
        elif s["kind"] == "table":
            out.append("| " + " | ".join(md(c) for c in s["columns"]) + " |")
            out.append("|" + "---|" * len(s["columns"]))
            for row in s["rows"]:
                out.append("| " + " | ".join("" if v is None else md(v) for v in row) + " |")
            out.append("")
        elif s["kind"] == "claims":
            out += [f"- {md(c['text'])} " + " ".join(f"[{r}]" for r in c.get("refs", [])) for c in s["claims"]] + [""]
        elif s["kind"] == "citations":
            for c in s["citations"]:
                link = f"https://pubmed.ncbi.nlm.nih.gov/{c['pmid']}/" if c.get("pmid") else ""
                out.append(f"- [{c['ref']}] {md(c['title'])}. {md(c.get('journal') or '')} {c.get('year') or ''}. {link}")
            out.append("")
    return "\n".join(out)


# ---- writers ---------------------------------------------------------------------

def write_xlsx(ds: dict, rep: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Data"
    row0 = 1
    if rep.get("banner"):
        ws.cell(1, 1, rep["banner"]).font = Font(bold=True, color="9C0006")
        ws.cell(1, 1).fill = PatternFill("solid", fgColor="FFC7CE")
        row0 = 3
    cols = [c["name"] for c in ds["columns"]]
    for j, name in enumerate(cols, start=1):
        c = ws.cell(row0, j, escape_formula(name))
        c.font = Font(bold=True)
    for i, r in enumerate(ds["rows"], start=row0 + 1):
        for j, name in enumerate(cols, start=1):
            v = _cell_text(r["cells"].get(name))
            cell = ws.cell(i, j)
            if isinstance(v, str):
                cell.value = escape_formula(v)
                cell.data_type = "s"  # never let openpyxl interpret text as a formula
            else:
                cell.value = v
    prov = wb.create_sheet("Provenance")
    prov.append(["dataset_id", ds["id"]])
    prov.append(["version", ds["version"]])
    prov.append(["review_state", ds["review_state"]])
    prov.append(["source_documents", ", ".join(ds["source_documents"])])
    prov.append(["generated_at_unix", now()])
    prov.append([])
    prov.append(["row_id", "column", "raw_text", "source", "review_state"])
    for r in ds["rows"][:20000]:
        for name in cols:
            c = r["cells"].get(name) or {}
            raw = c.get("r", c.get("v"))
            prov.append([r["id"], escape_formula(name), escape_formula(str(raw)) if raw is not None else None,
                         json.dumps(c.get("src")) if c.get("src") else None, c.get("rs")])
    for row in prov.iter_rows():
        for cell in row:
            if isinstance(cell.value, str):
                cell.data_type = "s"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def write_csv(ds: dict, rep: dict) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    if rep.get("banner"):
        w.writerow([rep["banner"]])
    cols = [c["name"] for c in ds["columns"]]
    w.writerow([escape_formula(c) for c in cols])
    for r in ds["rows"]:
        w.writerow(["" if (v := _cell_text(r["cells"].get(c))) is None else escape_formula(v) for c in cols])
    return buf.getvalue().encode("utf-8-sig")


def write_json(ds: dict, rep: dict) -> bytes:
    out = {
        "dataset_id": ds["id"], "version": ds["version"], "review_state": ds["review_state"],
        "draft_warning": rep.get("banner"), "columns": ds["columns"],
        "rows": [{"id": r["id"], "cells": r["cells"]} for r in ds["rows"]],
    }
    return json.dumps(out, ensure_ascii=False, indent=2).encode()


def write_docx(rep: dict) -> bytes:
    from docx import Document
    from docx.shared import RGBColor

    doc = Document()
    doc.add_heading(rep["title"], 0)
    if rep.get("banner"):
        p = doc.add_paragraph()
        run = p.add_run(rep["banner"])
        run.bold = True
        run.font.color.rgb = RGBColor(0x9C, 0x00, 0x06)
    for s in rep["sections"]:
        doc.add_heading(s["heading"], 1)
        if s["kind"] == "text":
            for para in s["paragraphs"]:
                doc.add_paragraph(str(para))
        elif s["kind"] == "table":
            t = doc.add_table(rows=1, cols=len(s["columns"]))
            t.style = "Table Grid"
            for j, c in enumerate(s["columns"]):
                t.rows[0].cells[j].text = str(c)
            for row in s["rows"]:
                cells = t.add_row().cells
                for j, v in enumerate(row):
                    cells[j].text = "" if v is None else str(v)
        elif s["kind"] == "claims":
            for c in s["claims"]:
                doc.add_paragraph(c["text"] + " " + " ".join(f"[{r}]" for r in c.get("refs", [])), style="List Bullet")
        elif s["kind"] == "citations":
            for c in s["citations"]:
                doc.add_paragraph(f"[{c['ref']}] {c['title']}. {c.get('journal') or ''} {c.get('year') or ''}. "
                                  f"PMID {c.get('pmid') or '-'}. {c.get('access') or ''}")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _font_path() -> str | None:
    for p in [settings.pdf_font_path, "/usr/share/fonts/google-noto-vf/NotoSans[wght].ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf"]:
        if p and Path(p).exists():
            return p
    return None


def write_pdf(rep: dict) -> bytes:
    from fpdf import FPDF

    pdf = FPDF(orientation="L" if any(s["kind"] == "table" and len(s["columns"]) > 6 for s in rep["sections"]) else "P")
    pdf.set_auto_page_break(True, 15)
    pdf.add_page()
    fp = _font_path()
    if fp:
        pdf.add_font("Body", "", fp)
        fam = "Body"

        def t(s: Any) -> str:
            return str(s)
    else:
        fam = "Helvetica"

        def t(s: Any) -> str:
            return str(s).encode("latin-1", "replace").decode("latin-1")
    pdf.set_font(fam, size=16)
    pdf.multi_cell(0, 9, t(rep["title"]), new_x="LMARGIN", new_y="NEXT")
    if rep.get("banner"):
        pdf.set_text_color(156, 0, 6)
        pdf.set_font(fam, size=10)
        pdf.multi_cell(0, 6, t(rep["banner"]), border=1, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
    for s in rep["sections"]:
        pdf.ln(3)
        pdf.set_font(fam, size=13)
        pdf.multi_cell(0, 8, t(s["heading"]), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font(fam, size=9)
        if s["kind"] == "text":
            for p in s["paragraphs"]:
                pdf.multi_cell(0, 5, t(p), new_x="LMARGIN", new_y="NEXT")
        elif s["kind"] == "table":
            from fpdf.fonts import FontFace

            with pdf.table(text_align="LEFT", line_height=5, headings_style=FontFace(emphasis="", fill_color=(230, 230, 230))) as table:
                hr = table.row()
                for c in s["columns"]:
                    hr.cell(t(c))
                for row in s["rows"]:
                    rr = table.row()
                    for v in row:
                        rr.cell(t("" if v is None else v))
        elif s["kind"] == "claims":
            for c in s["claims"]:
                pdf.multi_cell(0, 5, t("- " + c["text"] + " " + " ".join(f"[{r}]" for r in c.get("refs", []))), new_x="LMARGIN", new_y="NEXT")
        elif s["kind"] == "citations":
            for c in s["citations"]:
                pdf.multi_cell(0, 5, t(f"[{c['ref']}] {c['title']}. {c.get('journal') or ''} {c.get('year') or ''}. PMID {c.get('pmid') or '-'}. {c.get('access') or ''}"),
                               new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


# ---- create / preview -----------------------------------------------------------

def create(case_id: str, fmt: str, dataset_id: str | None, title: str | None = None, answer: dict | None = None,
           lineage_id: str | None = None) -> str:
    fmt = fmt.lower()
    if fmt not in FORMATS:
        raise ArtifactError(f"Unsupported format '{fmt}'. Supported: {sorted(FORMATS)}")
    ds = datasets.load(dataset_id) if dataset_id else None
    if ds is not None and ds["case_id"] != case_id:
        raise ArtifactError("dataset not in this case")
    if ds is None and answer is None:
        raise ArtifactError("an artifact needs a dataset or an answer to render")
    if ds is None and fmt in {"xlsx", "csv", "json"}:
        raise ArtifactError("tabular formats need a dataset")
    draft = (ds is not None and ds["review_state"] == "draft") or bool(answer)
    title = title or (ds["label"] if ds else "Evidence summary")
    rep = build_report(title, ds, answer, draft)
    if answer and not (ds is not None and ds["review_state"] == "draft"):
        rep["banner"] = "DRAFT for clinician review - assistant-generated summary of retrieved literature; not a diagnosis or prescription."
    data = {
        "xlsx": lambda: write_xlsx(ds, rep), "csv": lambda: write_csv(ds, rep), "json": lambda: write_json(ds, rep),
        "docx": lambda: write_docx(rep), "pdf": lambda: write_pdf(rep), "md": lambda: report_markdown(rep).encode(),
    }[fmt]()
    ref = storage.put(case_id, data, suffix=f".{fmt}", kind="art")
    preview = {"kind": "table" if fmt in {"xlsx", "csv", "json"} else ("pdf" if fmt == "pdf" else "report"),
               "dataset_id": dataset_id, "size": len(data)}
    if fmt in {"docx", "md", "pdf"}:
        preview["html"] = report_html(rep)
        preview["markdown"] = report_markdown(rep) if fmt == "md" else None
    if fmt == "pdf":
        from .ingest.pdf import page_count
        preview["page_count"] = page_count(data)
    version = 1
    if lineage_id:
        r = db.q1("SELECT MAX(version) v FROM artifacts WHERE lineage_id=? AND case_id=?", (lineage_id, case_id))
        version = (r["v"] or 0) + 1
    aid = new_id("art")
    db.ex(
        "INSERT INTO artifacts(id, case_id, dataset_id, lineage_id, version, format, title, storage_ref, preview, params, review_state, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (aid, case_id, dataset_id, lineage_id or aid, version, fmt, title, ref, dumps(preview),
         dumps({"template": "standard-1", "has_answer": bool(answer)}), "draft" if draft else (ds["review_state"] if ds else "draft"), now()),
    )
    db.index_text(case_id, "artifact", aid, title)
    return aid


def summary(r: dict) -> dict:
    p = loads(r["preview"], {})
    return {"id": r["id"], "title": r["title"], "format": r["format"], "version": r["version"], "lineage_id": r["lineage_id"],
            "dataset_id": r["dataset_id"], "review_state": r["review_state"], "created_at": r["created_at"],
            "preview_kind": p.get("kind"), "size": p.get("size")}
