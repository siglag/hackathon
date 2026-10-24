"""Canonical document JSON and deterministic layout grouping (spec 8.4-8.5).

Spans (OCR or PDF text) are grouped into lines by vertical overlap; each line
keeps the ordered IDs of its spans so every value can be traced back to a page
and bounding box.
"""
from __future__ import annotations

PARSER_VERSION = "canonical-1"


def assign_ids(pages: list[dict]) -> None:
    for p in pages:
        for i, s in enumerate(p["spans"]):
            s["id"] = f"p{p['page']}s{i}"


def group_lines(spans: list[dict]) -> list[dict]:
    if not spans:
        return []
    items = sorted(spans, key=lambda s: ((s["bbox"][1] + s["bbox"][3]) / 2, s["bbox"][0]))
    lines: list[list[dict]] = []
    for s in items:
        y0, y1 = s["bbox"][1], s["bbox"][3]
        placed = False
        if lines:
            last = lines[-1]
            ly0 = min(x["bbox"][1] for x in last)
            ly1 = max(x["bbox"][3] for x in last)
            overlap = min(y1, ly1) - max(y0, ly0)
            if overlap > 0.5 * min(y1 - y0, ly1 - ly0):
                last.append(s)
                placed = True
        if not placed:
            lines.append([s])
    out = []
    for li, group in enumerate(lines):
        group.sort(key=lambda s: s["bbox"][0])
        out.append({
            "id": f"L{li}",
            "span_ids": [s["id"] for s in group],
            "cells": [s["text"] for s in group],
            "text": "  ".join(s["text"] for s in group),
            "bbox": [min(s["bbox"][0] for s in group), min(s["bbox"][1] for s in group),
                     max(s["bbox"][2] for s in group), max(s["bbox"][3] for s in group)],
        })
    return out


def build(document_id: str, pages: list[dict], engine: str) -> dict:
    assign_ids(pages)
    for p in pages:
        p["lines"] = group_lines(p["spans"])
        for ln in p["lines"]:
            ln["id"] = f"p{p['page']}{ln['id']}"
    return {"document_id": document_id, "engine": engine, "parser_version": PARSER_VERSION, "pages": pages}


def full_text(canonical: dict) -> str:
    return "\n".join(ln["text"] for p in canonical["pages"] for ln in p.get("lines", []))
