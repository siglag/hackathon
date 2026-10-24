"""Minimal Streamable-HTTP MCP client used only inside the evidence gateway.

Restrictions (spec 3, 4):
* Only allowlisted tools are callable; newly added remote tools are ignored.
* Discovered schemas must contain the properties the gateway maps to.
* The client declares no sampling/elicitation/roots capabilities and answers
  any server-initiated request with a JSON-RPC error.
* TLS verification on, redirects off, hostname allowlist, response-size cap,
  timeouts. No case or session identifiers are sent as tracing metadata.
"""
from __future__ import annotations

import itertools
import json
import logging
from urllib.parse import urlparse

import httpx

log = logging.getLogger("medassist.gateway.mcp")

ALLOWED_TOOLS = {
    "pubmed_search_articles": {"query"},
    "pubmed_fetch_articles": {"pmids"},
    "pubmed_fetch_fulltext": {"pmids"},
}
PROTOCOL_VERSION = "2025-06-18"


class MCPError(RuntimeError):
    pass


class MCPClient:
    def __init__(self, url: str, allowed_hosts: tuple[str, ...], timeout_s: int, max_bytes: int):
        host = urlparse(url).hostname
        if urlparse(url).scheme != "https" or host not in allowed_hosts:
            raise MCPError("MCP endpoint is not on the HTTPS hostname allowlist")
        self.url = url
        self.allowed_hosts = allowed_hosts
        self.max_bytes = max_bytes
        self._ids = itertools.count(1)
        self._session: str | None = None
        self._tools: dict[str, dict] | None = None
        self.server_info: dict = {}
        self.http = httpx.Client(timeout=httpx.Timeout(timeout_s, connect=10), follow_redirects=False, verify=True,
                                 headers={"User-Agent": "medassist-evidence-gateway/0.1"})

    def _post(self, payload: dict) -> dict | None:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                   "MCP-Protocol-Version": PROTOCOL_VERSION}
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        with self.http.stream("POST", self.url, json=payload, headers=headers) as r:
            if r.is_redirect:
                raise MCPError("redirect refused")
            if urlparse(str(r.url)).hostname not in self.allowed_hosts:
                raise MCPError("unexpected host")
            if r.status_code == 404 and self._session:
                self._session = None
                raise MCPError("session expired")
            if r.status_code >= 400:
                raise MCPError(f"provider HTTP {r.status_code}")
            sid = r.headers.get("mcp-session-id")
            if sid:
                self._session = sid
            body = b""
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) > self.max_bytes:
                    raise MCPError("provider response exceeded size limit")
            ctype = r.headers.get("content-type", "")
        if "id" not in payload:
            return None
        return self._pick_response(body, ctype, payload["id"])

    def _pick_response(self, body: bytes, ctype: str, want_id: int) -> dict:
        msgs: list[dict] = []
        if ctype.startswith("text/event-stream"):
            for block in body.decode("utf-8", "replace").split("\n\n"):
                data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
                if data:
                    try:
                        msgs.append(json.loads(data))
                    except json.JSONDecodeError:
                        continue
        elif body:
            obj = json.loads(body)
            msgs = obj if isinstance(obj, list) else [obj]
        for m in msgs:
            if "method" in m and "id" in m:
                # Server-initiated request (sampling, elicitation, roots...): refuse.
                log.warning("refused server-initiated MCP request method=%s", m.get("method"))
                self._post({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "not supported by client"}})
        for m in msgs:
            if m.get("id") == want_id and ("result" in m or "error" in m):
                if "error" in m:
                    raise MCPError(f"provider error {m['error'].get('code')}")
                return m["result"]
        raise MCPError("no response from provider")

    def connect(self) -> None:
        res = self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": "initialize", "params": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "medassist-evidence-gateway", "version": "0.1"}}})
        self.server_info = (res or {}).get("serverInfo", {})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": "tools/list"}) or {}
        tools = {}
        for t in listed.get("tools", []):
            name = t.get("name")
            if name not in ALLOWED_TOOLS:
                continue  # never expose tools outside the approved subset
            props = set((t.get("inputSchema") or {}).get("properties", {}).keys())
            if not ALLOWED_TOOLS[name] <= props:
                log.warning("tool schema changed; disabling tool=%s", name)
                continue
            tools[name] = t.get("inputSchema") or {}
        self._tools = tools

    def call(self, name: str, arguments: dict) -> dict:
        if name not in ALLOWED_TOOLS:
            raise MCPError("tool not in allowlist")
        if self._tools is None:
            self.connect()
        assert self._tools is not None
        if name not in self._tools:
            raise MCPError(f"approved tool {name} is unavailable or its schema changed")
        props = set(self._tools[name].get("properties", {}).keys())
        args = {k: v for k, v in arguments.items() if k in props}
        try:
            res = self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": "tools/call",
                              "params": {"name": name, "arguments": args}})
        except MCPError as e:
            if "session expired" not in str(e):
                raise
            self.connect()
            res = self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": "tools/call",
                              "params": {"name": name, "arguments": args}})
        if not res:
            raise MCPError("empty result")
        if res.get("isError"):
            raise MCPError("provider reported a tool error")
        sc = res.get("structuredContent")
        if not isinstance(sc, dict):
            raise MCPError("provider returned no structured content")
        return sc
