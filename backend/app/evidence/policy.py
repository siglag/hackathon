"""Outbound evidence-request policy, shared by the API and the gateway (spec 4).

The model never supplies a query string. It proposes a small structured
request; every concept must resolve against the installed vocabulary or the
administrator allowlist; the gateway then generates the actual search syntax.
"""
from __future__ import annotations

import datetime as dt
import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..config import settings
from ..util import canonical_json, sha256_bytes
from .phi import detect

POLICY_VERSION = "evidence-policy-1"
MAX_CONCEPTS = 4

Population = Literal["adults", "older adults", "children", "adolescents", "infants", "pregnant women", "women", "men"]
StudyType = Literal["systematic review", "meta-analysis", "randomized controlled trial", "guideline", "review",
                    "clinical trial", "observational study"]

POPULATION_FILTER = {
    "adults": '"adult"[MeSH Terms]',
    "older adults": '"aged"[MeSH Terms]',
    "children": '"child"[MeSH Terms]',
    "adolescents": '"adolescent"[MeSH Terms]',
    "infants": '"infant"[MeSH Terms]',
    "pregnant women": '"pregnancy"[MeSH Terms]',
    "women": '"female"[MeSH Terms]',
    "men": '"male"[MeSH Terms]',
}
STUDY_TYPE_PT = {
    "systematic review": "Systematic Review",
    "meta-analysis": "Meta-Analysis",
    "randomized controlled trial": "Randomized Controlled Trial",
    "guideline": "Practice Guideline",
    "review": "Review",
    "clinical trial": "Clinical Trial",
    "observational study": "Observational Study",
}


class EvidenceRequest(BaseModel):
    concepts: list[str] = Field(min_length=1, max_length=8)
    population: Population | None = None
    study_types: list[StudyType] = Field(default_factory=list, max_length=4)
    year_from: int | None = None
    year_to: int | None = None

    @field_validator("concepts")
    @classmethod
    def _short(cls, v: list[str]) -> list[str]:
        for c in v:
            if len(c) > 60:
                raise ValueError("concept too long; use a general medical concept")
        return v


class PolicyResult(BaseModel):
    ok: bool
    request: dict | None = None  # normalized, vocabulary-resolved request
    held_terms: list[str] = []
    blocked: list[dict] = []
    problems: list[str] = []


def _vocab_files() -> list[Path]:
    return [Path(__file__).resolve().parents[1] / "data" / "vocabulary.json", settings.data_dir / "vocabulary_additions.json"]


@lru_cache(maxsize=1)
def _vocab_cached(mtimes: tuple) -> tuple[dict[str, str], dict[str, str], str]:
    index: dict[str, str] = {}
    cats: dict[str, str] = {}
    versions = []
    for p in _vocab_files():
        if not p.exists():
            continue
        data = json.loads(p.read_text())
        versions.append(data.get("version", p.name))
        for t in data.get("terms", []):
            term = t["term"].strip().lower()
            cats[term] = t.get("category", "concept")
            index[term] = term
            for s in t.get("synonyms", []):
                index[s.strip().lower()] = term
    return index, cats, "+".join(versions)


def vocabulary() -> tuple[dict[str, str], dict[str, str], str]:
    mt = tuple(p.stat().st_mtime if p.exists() else 0 for p in _vocab_files())
    return _vocab_cached(mt)


def add_terms(terms: list[dict], added_by: str) -> int:
    p = settings.data_dir / "vocabulary_additions.json"
    data = json.loads(p.read_text()) if p.exists() else {"version": "local-additions", "terms": []}
    existing = {t["term"] for t in data["terms"]}
    n = 0
    for t in terms:
        term = t["term"].strip().lower()
        if not term or term in existing:
            continue
        if detect(term):
            raise ValueError(f"term '{term}' looks like an identifier and cannot be allowlisted")
        data["terms"].append({"term": term, "synonyms": t.get("synonyms", []), "category": t.get("category", "concept"),
                              "added_by": added_by})
        n += 1
    p.write_text(json.dumps(data, indent=1))
    return n


def resolve(concept: str) -> str | None:
    index, _, _ = vocabulary()
    return index.get(" ".join(concept.lower().replace("_", " ").split()))


def match_in_text(text: str) -> list[str]:
    """Vocabulary concepts literally present in local text (used locally only).

    A match whose text span lies inside a longer match is dropped, so
    "type 2 diabetes" does not also yield "diabetes mellitus".
    """
    import re

    index, _, _ = vocabulary()
    low = " " + re.sub(r"[^a-z0-9\-' ]", " ", text.lower()) + " "
    spans: list[tuple[int, int, str]] = []
    for key, term in index.items():
        start = 0
        while (pos := low.find(" " + key + " ", start)) >= 0:
            spans.append((pos + 1, pos + 1 + len(key), term))
            start = pos + 1
    kept = [s for s in spans if not any(o[0] <= s[0] and s[1] <= o[1] and (o[1] - o[0]) > (s[1] - s[0]) for o in spans)]
    out: list[str] = []
    for _, _, term in sorted(kept):
        if term not in out:
            out.append(term)
    return out


def evaluate(req: EvidenceRequest, local_identifiers: set[str] | None = None) -> PolicyResult:
    problems: list[str] = []
    blocked: list[dict] = []
    resolved: list[str] = []
    held: list[str] = []
    for c in req.concepts:
        r = resolve(c)
        if r is None:
            held.append(c)  # never transmitted
        elif r not in resolved:
            resolved.append(r)
    # Blocking check on exactly what would be transmitted, including the case's
    # own identifiers (a vocabulary word can coincide with a patient's name).
    for s in resolved:
        for f in detect(s, local_identifiers):
            blocked.append({**f, "field": "concept"})
    if len(resolved) > MAX_CONCEPTS:
        problems.append(f"Too many concepts ({len(resolved)}). Use at most {MAX_CONCEPTS} broad concepts; "
                        "unusual combinations can identify a person.")
    if not resolved:
        problems.append("No concept matched the approved vocabulary. Unrecognized terms stay local pending review.")
    this_year = dt.date.today().year
    for y in (req.year_from, req.year_to):
        if y is not None and not (1900 <= y <= this_year + 1):
            problems.append("Publication-year filter out of range.")
    if req.year_from and req.year_to and req.year_from > req.year_to:
        problems.append("Publication-year range is reversed.")
    if blocked:
        problems.append("Possible identifier detected. This cannot be overridden; rewrite the research topic in general terms.")
    ok = not problems
    norm = {
        "concepts": sorted(resolved),
        "population": req.population,
        "study_types": sorted(req.study_types),
        "year_from": req.year_from,
        "year_to": req.year_to,
    }
    return PolicyResult(ok=ok, request=norm if resolved else None, held_terms=held, blocked=blocked, problems=problems)


def request_hash(norm: dict) -> str:
    return sha256_bytes((POLICY_VERSION + canonical_json(norm)).encode())


def build_search(norm: dict) -> dict:
    """Generate the provider tool arguments. Only the gateway output is sent."""
    parts = [f'("{c}")' for c in norm["concepts"]]
    if norm.get("population"):
        parts.append(POPULATION_FILTER[norm["population"]])
    parts.append('"humans"[MeSH Terms]')
    query = " AND ".join(parts)
    args: dict = {"query": query, "maxResults": 8, "sort": "relevance"}
    if norm.get("study_types"):
        args["publicationTypes"] = [STUDY_TYPE_PT[s] for s in norm["study_types"]]
    if norm.get("year_from") or norm.get("year_to"):
        args["dateRange"] = {"minDate": str(norm.get("year_from") or 1900), "maxDate": str(norm.get("year_to") or 3000),
                             "dateType": "pdat"}
    return args


def describe(norm: dict) -> str:
    s = " + ".join(norm["concepts"])
    if norm.get("population"):
        s += f" | population: {norm['population']}"
    if norm.get("study_types"):
        s += f" | study types: {', '.join(norm['study_types'])}"
    if norm.get("year_from") or norm.get("year_to"):
        s += f" | published {norm.get('year_from') or '...'}-{norm.get('year_to') or '...'}"
    return s
