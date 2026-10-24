"""Validate the *actual* file type and size before any processing (spec 8.1).

The declared MIME type and filename extension are never trusted. Archive
formats are checked for decompressed size; images for decoded pixel count.
"""
from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass

from ..config import settings


class Rejected(ValueError):
    """Validation failure with a doctor-facing explanation."""


@dataclass
class FileInfo:
    kind: str  # image | pdf | xlsx | csv | json
    mime: str
    page_count: int | None = None
    notes: list[str] | None = None


def sniff(data: bytes, filename: str) -> FileInfo:
    if len(data) == 0:
        raise Rejected("The file is empty.")
    if len(data) > settings.max_upload_bytes:
        raise Rejected(f"The file exceeds the {settings.max_upload_bytes // (1024 * 1024)} MB limit. Split it into smaller files.")

    if data[:3] == b"\xff\xd8\xff":
        return _image(data, "image/jpeg")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return _image(data, "image/png")
    if data[:5] == b"%PDF-":
        return _pdf(data)
    if data[:4] == b"PK\x03\x04":
        return _xlsx(data)
    if data[:4] in (b"\xd0\xcf\x11\xe0",):
        raise Rejected("Legacy binary Office files (.xls/.doc) are not supported. Save as .xlsx or CSV.")

    text = _as_text(data)
    stripped = text.lstrip("﻿ \t\r\n")
    if stripped[:1] in ("{", "["):
        try:
            json.loads(stripped)
        except json.JSONDecodeError as e:
            raise Rejected(f"The JSON file is not valid (line {e.lineno}).") from None
        return FileInfo("json", "application/json")
    lower = filename.lower()
    if lower.endswith((".csv", ".tsv", ".txt")) or "," in text[:2000] or "\t" in text[:2000]:
        return FileInfo("csv", "text/csv")
    raise Rejected("Unsupported file type. Supported: JPEG, PNG, PDF, XLSX, CSV, JSON.")


def _as_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise Rejected("Unsupported file type. Text files must be UTF-8.")


def _image(data: bytes, mime: str) -> FileInfo:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
            if w * h > settings.max_image_pixels:
                raise Rejected("The image resolution is too large to process safely.")
            im.verify()
    except Rejected:
        raise
    except Exception:
        raise Rejected("The image file is corrupt or unreadable.") from None
    return FileInfo("image", mime, page_count=1)


def _pdf(data: bytes) -> FileInfo:
    import pypdfium2 as pdfium

    try:
        doc = pdfium.PdfDocument(data)
    except Exception:
        raise Rejected("The PDF is corrupt, encrypted, or unreadable.") from None
    try:
        n = len(doc)
    finally:
        doc.close()
    if n == 0:
        raise Rejected("The PDF has no pages.")
    if n > settings.max_pdf_pages:
        raise Rejected(f"The PDF has {n} pages; the limit is {settings.max_pdf_pages} per job. Split the file.")
    notes = []
    if b"/JavaScript" in data or b"/JS " in data or b"/Launch" in data:
        notes.append("PDF contains active content (scripts/actions); it is ignored and never executed.")
    return FileInfo("pdf", "application/pdf", page_count=n, notes=notes)


def _xlsx(data: bytes) -> FileInfo:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise Rejected("The archive is corrupt.") from None
    with zf:
        total = 0
        names = set()
        for info in zf.infolist():
            total += info.file_size
            names.add(info.filename)
            if info.compress_size and info.file_size / max(info.compress_size, 1) > 200:
                raise Rejected("The file has a suspicious compression ratio and was rejected.")
        if total > settings.max_decompressed_bytes:
            raise Rejected("The workbook is too large once decompressed.")
        if "[Content_Types].xml" not in names or not any(n.startswith("xl/") for n in names):
            if any(n.startswith("word/") for n in names):
                raise Rejected("DOCX upload is not supported as an input in this release. Export it as PDF.")
            raise Rejected("Unsupported archive. Only .xlsx workbooks are accepted.")
        notes = []
        if "xl/vbaProject.bin" in names:
            notes.append("Workbook contains macros; they are ignored and never executed.")
        if any(n.startswith("xl/externalLinks/") for n in names):
            notes.append("Workbook contains external links; they are not followed.")
    return FileInfo("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", notes=notes)
