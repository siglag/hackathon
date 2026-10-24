"""Deterministic local identifier detection for outbound text (spec 4.5).

This is a blocking check: a detection cannot be overridden; the query must be
rewritten. It runs on every string of an outbound request, in the API and
again in the gateway. It cannot prove absence of identifying information; the
vocabulary allowlist is the primary control and this is a second layer.
"""
from __future__ import annotations

import re
import unicodedata

PATTERNS: list[tuple[str, re.Pattern]] = [
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("url", re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+\.(?:com|org|net|gov|edu|io|sa|ae|eg|uk)\b", re.I)),
    ("filename", re.compile(r"\b[\w\-]+\.(?:pdf|jpe?g|png|heic|xlsx?|csv|json|docx?|txt|dcm)\b", re.I)),
    ("phone", re.compile(r"(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?)?\d{3,4}[\s.-]\d{3,4}(?:[\s.-]\d{2,4})?")),
    ("date", re.compile(r"\b\d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4}\b")),
    ("date", re.compile(r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?:\s+\d{2,4})?\b", re.I)),
    ("date", re.compile(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?(?:\s+\d{2,4})?\b", re.I)),
    ("record_number", re.compile(r"(?:\b(?:mrn|id|no|nr|file|record|case|patient|hospital|ref)\b|#)\s*[:#.\-]?\s*(?=[A-Z0-9\-/]*\d)[A-Z0-9][A-Z0-9\-/]{2,}\b", re.I)),
    ("number_sequence", re.compile(r"\b\d{4,}\b")),
    ("mixed_identifier", re.compile(r"\b(?=[A-Z0-9\-]*\d)(?=[A-Z0-9\-]*[A-Z])[A-Z0-9\-]{5,}\b")),
    ("exact_age", re.compile(r"\b\d{1,3}\s*(?:-|\s)?(?:year|yr|y)s?(?:[\s-]*old)?\b|\baged?\s+\d{1,3}\b", re.I)),
    ("address", re.compile(r"\b\d+\s+\w+(?:\s+\w+)?\s+(?:street|st|road|rd|avenue|ave|lane|ln|blvd|drive|dr|way)\b", re.I)),
    ("address", re.compile(r"\b(?:p\.?\s?o\.?\s+box|apartment|apt|building|floor|district)\s*\d+", re.I)),
    ("honorific_name", re.compile(r"\b(?:mr|mrs|ms|miss|dr|prof|sheikh|sayyid)\.?\s+[A-Z][a-z]+", re.I)),
    ("dob", re.compile(r"\b(?:dob|d\.o\.b|born|date of birth)\b", re.I)),
]


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    # remove zero-width and other format characters used to obfuscate
    s = "".join(ch for ch in s if unicodedata.category(ch) not in {"Cf", "Cc"})
    return s


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9؀-ۿ]", "", s.lower())


def identifier_tokens(values: list[str]) -> set[str]:
    """Tokens from local identifying fields (names, IDs, case label)."""
    toks = set()
    for v in values:
        if not v:
            continue
        v = _norm(v)
        for t in re.split(r"[\s,;:/]+", v):
            st = _squash(t)
            if len(st) >= 3:
                toks.add(st)
        sq = _squash(v)
        if len(sq) >= 4:
            toks.add(sq)
    return toks


def detect(text: str, local_identifiers: set[str] | None = None) -> list[dict]:
    t = _norm(text)
    found = []
    for kind, rx in PATTERNS:
        for m in rx.finditer(t):
            found.append({"type": kind, "match": m.group(0)})
    if t != text:
        found.append({"type": "obfuscation", "match": "hidden/format characters"})
    if local_identifiers:
        words = {_squash(w) for w in re.split(r"[\s,;:/()\[\]\"']+", t) if w}
        whole = _squash(t)
        for ident in local_identifiers:
            if ident in words or (len(ident) >= 6 and ident in whole):
                found.append({"type": "case_identifier", "match": ident})
    return found
