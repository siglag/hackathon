"""Local evidence gateway: the only process with internet egress (spec 3, 4).

Run separately from the API (``uvicorn app.gateway_service:app --port 8601``),
bound to loopback, authenticated with a shared secret.

It accepts only an *approved, structured* request; it re-validates it with the
same policy as the API, recomputes the approval hash, generates the search
syntax itself, and fetches only article IDs it previously returned for that
approved request. Local case identifiers arrive only as SHA-256 hashes.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import threading
import time

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .config import settings
from .evidence import policy
from .evidence.mcp_client import MCPClient, MCPError

log = logging.getLogger("medassist.gateway")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

app = FastAPI(title="medassist evidence gateway", docs_url=None, redoc_url=None, openapi_url=None)

_client: MCPClient | None = None
_client_lock = threading.Lock()
_returned: dict[str, tuple[float, set[str]]] = {}  # request_hash -> (expiry, PMIDs returned)
RETURN_TTL = 6 * 3600
PROVIDER = "cyanheads/pubmed-mcp-server (hosted)"


def client() -> MCPClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = MCPClient(settings.mcp_url, settings.mcp_allowed_hosts, settings.gateway_timeout_s,
                                settings.gateway_max_response_bytes)
        return _client


def _auth(key: str | None) -> None:
    if not key or not hmac.compare_digest(key, settings.gateway_secret()):
        raise HTTPException(401, "unauthorized")


class SearchIn(BaseModel):
    request: dict
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    identifier_hashes: list[str] = Field(default_factory=list, max_length=500)


class FetchIn(BaseModel):
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    pmids: list[str] = Field(min_length=1, max_length=8)
    fulltext: bool = False


def _hashed_identifier_hit(norm: dict, hashes: set[str]) -> bool:
    if not hashes:
        return False
    for c in norm["concepts"]:
        for w in re.split(r"[\s,;:/()\-]+", c.lower()):
            w = re.sub(r"[^a-z0-9؀-ۿ]", "", w)
            if w and hashlib.sha256(w.encode()).hexdigest() in hashes:
                return True
    return False


@app.get("/v1/health")
def health(x_gateway_key: str | None = Header(None)):
    _auth(x_gateway_key)
    return {"ok": True, "provider": PROVIDER, "endpoint_host": settings.mcp_allowed_hosts, "policy": policy.POLICY_VERSION}


@app.post("/v1/probe")
def probe(x_gateway_key: str | None = Header(None)):
    """Connectivity check: initialize + tools/list only (no search sent)."""
    _auth(x_gateway_key)
    c = client()
    try:
        c.connect()
    except (MCPError, Exception) as e:  # noqa: BLE001
        return {"ok": False, "error": type(e).__name__ if not isinstance(e, MCPError) else str(e)}
    return {"ok": True, "server": c.server_info, "approved_tools_available": sorted(c._tools or {})}


@app.post("/v1/search")
def search(body: SearchIn, x_gateway_key: str | None = Header(None)):
    _auth(x_gateway_key)
    try:
        req = policy.EvidenceRequest(**{k: body.request.get(k) for k in ("concepts", "population", "study_types", "year_from", "year_to")
                                        if body.request.get(k) is not None})
    except Exception:
        raise HTTPException(422, "request does not match the evidence schema") from None
    res = policy.evaluate(req)
    if not res.ok or res.held_terms or res.request != body.request:
        raise HTTPException(422, "request failed gateway policy validation")
    if policy.request_hash(res.request) != body.request_hash:
        raise HTTPException(409, "request changed after approval")
    if _hashed_identifier_hit(res.request, set(body.identifier_hashes)):
        raise HTTPException(422, "request contains a local case identifier")
    args = policy.build_search(res.request)
    args["summaryCount"] = args["maxResults"]
    try:
        sc = client().call("pubmed_search_articles", args)
    except MCPError as e:
        raise HTTPException(502, f"evidence provider unavailable: {e}") from None
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"evidence provider unreachable ({type(e).__name__})") from None
    pmids = [str(p) for p in sc.get("pmids", []) if re.fullmatch(r"\d{1,9}", str(p))]
    now = time.time()
    for k in [k for k, (exp, _) in _returned.items() if exp < now]:
        _returned.pop(k, None)
    _returned[body.request_hash] = (now + RETURN_TTL, set(pmids))
    summaries = [{k: s.get(k) for k in ("pmid", "title", "authors", "source", "pubDate", "doi", "pmcId")}
                 for s in sc.get("summaries", []) if str(s.get("pmid")) in pmids]
    return {"provider": PROVIDER, "search_args": args, "effective_query": sc.get("effectiveQuery") or args["query"],
            "total": sc.get("totalCount"), "pmids": pmids, "summaries": summaries, "retrieved_at": now}


_CORRECTION_TYPES = {
    "RetractionIn": "retracted", "RetractionOf": "retraction_notice", "ErratumIn": "corrected",
    "CorrectedandRepublishedIn": "corrected", "ExpressionOfConcernIn": "expression_of_concern", "UpdateIn": "updated",
}


def _norm_article(a: dict) -> dict:
    flags = set()
    for cc in a.get("commentsCorrections", []) or []:
        f = _CORRECTION_TYPES.get(cc.get("refType", ""))
        if f:
            flags.add(f)
    pts = a.get("publicationTypes", []) or []
    if "Retracted Publication" in pts:
        flags.add("retracted")
    ji = a.get("journalInfo") or {}
    pd = ji.get("publicationDate") or {}
    authors = []
    for au in (a.get("authors") or [])[:30]:
        if isinstance(au, dict):
            authors.append(" ".join(x for x in (au.get("lastName"), au.get("initials")) if x) or au.get("collectiveName", ""))
        else:
            authors.append(str(au))
    return {
        "pmid": str(a.get("pmid")), "doi": a.get("doi"), "pmcid": a.get("pmcId"), "title": a.get("title"),
        "authors": authors, "journal": ji.get("isoAbbreviation") or ji.get("title"), "year": pd.get("year"),
        "publication_date": "-".join(str(x) for x in (pd.get("year"), pd.get("month"), pd.get("day")) if x),
        "publication_types": pts, "abstract": a.get("abstractText") or "", "flags": sorted(flags),
        "mesh": [m.get("descriptorName") for m in (a.get("meshTerms") or [])[:40] if isinstance(m, dict)],
    }


@app.post("/v1/fetch")
def fetch(body: FetchIn, x_gateway_key: str | None = Header(None)):
    _auth(x_gateway_key)
    entry = _returned.get(body.request_hash)
    if entry is None or entry[0] < time.time():
        raise HTTPException(409, "no approved search results for this request; search again")
    pmids = [p for p in body.pmids if re.fullmatch(r"\d{1,9}", p)]
    if not pmids or not set(pmids) <= entry[1]:
        raise HTTPException(422, "only article IDs returned by the approved search may be fetched")
    try:
        sc = client().call("pubmed_fetch_articles", {"pmids": pmids, "includeMesh": True, "includeGrants": False,
                                                     "includeInvestigators": False, "maxResponseCharacters": 400_000})
    except MCPError as e:
        raise HTTPException(502, f"evidence provider unavailable: {e}") from None
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"evidence provider unreachable ({type(e).__name__})") from None
    arts = [_norm_article(a) for a in sc.get("articles", []) if str(a.get("pmid")) in pmids]
    if body.fulltext:
        ft_ids = [a["pmid"] for a in arts if a.get("pmcid")][:5]
        if ft_ids:
            try:
                ft = client().call("pubmed_fetch_fulltext", {"pmids": ft_ids, "maxCharacters": 15000, "includeTables": False,
                                                             "includeAssets": False, "includeReferences": False})
                by = {str(x.get("pmid")): x for x in ft.get("articles", [])}
                for a in arts:
                    x = by.get(a["pmid"])
                    if x and x.get("content"):
                        a["fulltext"] = str(x["content"])[:15000]
                        a["fulltext_source"] = x.get("source")
                        a["fulltext_license"] = x.get("license")
            except Exception as e:  # noqa: BLE001
                log.warning("fulltext unavailable: %s", type(e).__name__)
    return {"provider": PROVIDER, "articles": arts, "unavailable": sc.get("unavailablePmids", []), "retrieved_at": time.time()}
