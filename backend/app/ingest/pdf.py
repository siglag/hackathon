"""Local PDF handling: embedded text with coordinates, and page rendering
for scanned pages (spec section 8). Active content is never executed.
"""
from __future__ import annotations

import io

import pypdfium2 as pdfium

RENDER_SCALE = 2.0  # ~144 dpi; adequate for OCR of printed text


def page_count(data: bytes) -> int:
    doc = pdfium.PdfDocument(data)
    try:
        return len(doc)
    finally:
        doc.close()


def render_page_png(data: bytes, index: int, scale: float = RENDER_SCALE) -> tuple[bytes, int, int]:
    doc = pdfium.PdfDocument(data)
    try:
        page = doc[index]
        img = page.render(scale=scale).to_pil()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), img.width, img.height
    finally:
        doc.close()


def extract_text_spans(data: bytes, scale: float = RENDER_SCALE) -> list[dict]:
    """Per page: text spans with bounding boxes in rendered-image pixel space.

    Returns a list of {page, width, height, spans:[{text, bbox}]}; pages with
    no embedded text have an empty span list (to be OCR'd).
    """
    doc = pdfium.PdfDocument(data)
    pages = []
    try:
        for i in range(len(doc)):
            page = doc[i]
            pw, ph = page.get_size()
            tp = page.get_textpage()
            spans = []
            n = tp.count_rects()
            for r in range(n):
                left, bottom, right, top = tp.get_rect(r)
                text = tp.get_text_bounded(left, bottom, right, top).strip()
                if text:
                    spans.append({
                        "text": text,
                        "bbox": [left * scale, (ph - top) * scale, right * scale, (ph - bottom) * scale],
                        "score": None,
                        "source": "pdf_text",
                    })
            tp.close()
            pages.append({"page": i + 1, "width": pw * scale, "height": ph * scale, "spans": spans})
    finally:
        doc.close()
    return pages
