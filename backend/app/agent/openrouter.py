"""OpenRouter (cloud) chat client — EXPERIMENTAL, branch `deepseek-openrouter`.

WARNING: this sends case content (record text, questions, extracted values)
to OpenRouter and the upstream model provider. That contradicts the spec's
core privacy requirement (section 4) and is only allowed when
MEDASSIST_ALLOW_CLOUD_MODEL=1 is set explicitly. Use synthetic data.

Requests ask OpenRouter to route only to providers that do not collect data
(`provider.data_collection = "deny"`); that is a provider policy, not a
technical guarantee.
"""
from __future__ import annotations

import json
import secrets
import time
from typing import Any, Callable

import httpx

from ..config import settings
from ..util import strip_think
from .ollama import ModelUnavailable

BASE = "https://openrouter.ai/api/v1"


def _key() -> str:
    k = settings.openrouter_api_key()
    if not k:
        raise ModelUnavailable("OpenRouter API key is not configured (MEDASSIST_OPENROUTER_API_KEY or openrouter.key).")
    return k


def _client() -> httpx.Client:
    return httpx.Client(base_url=BASE, timeout=httpx.Timeout(settings.model_timeout_s, connect=10), follow_redirects=False,
                        headers={"Authorization": f"Bearer {_key()}", "X-Title": "medassist-ab-test"})


def status() -> dict:
    try:
        with _client() as c:
            r = c.get("/key")
            r.raise_for_status()
        return {"ok": True, "provider": "openrouter", "model": settings.openrouter_model, "cloud": True,
                "version": "openrouter", "digest": None}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "provider": "openrouter", "model": settings.openrouter_model, "cloud": True, "error": type(e).__name__}


def _to_openai(messages: list[dict]) -> list[dict]:
    """Convert the app's (Ollama-style) messages to the OpenAI wire format."""
    out: list[dict] = []
    pending_ids: list[str] = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            calls = []
            for c in m["tool_calls"]:
                cid = c.get("id") or f"call_{secrets.token_hex(6)}"
                c["id"] = cid
                args = c.get("function", {}).get("arguments", {})
                calls.append({"id": cid, "type": "function", "function": {
                    "name": c.get("function", {}).get("name", ""),
                    "arguments": args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)}})
            pending_ids = [c["id"] for c in calls]
            out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
        elif m["role"] == "tool":
            cid = m.get("tool_call_id") or (pending_ids.pop(0) if pending_ids else f"call_{secrets.token_hex(6)}")
            if cid in pending_ids:
                pending_ids.remove(cid)
            out.append({"role": "tool", "tool_call_id": cid, "content": m.get("content", "")})
        else:
            out.append({"role": m["role"], "content": m.get("content", "")})
    return out


def _post(body: dict) -> dict:
    body = {"provider": {"data_collection": "deny"}, "usage": {"include": True}, **body}
    last: Exception | None = None
    for attempt in range(3):
        try:
            with _client() as c:
                r = c.post("/chat/completions", json=body)
            if r.status_code in (429, 502, 503) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise ModelUnavailable(f"OpenRouter HTTP {r.status_code}")
            data = r.json()
            if "error" in data:
                raise ModelUnavailable(f"OpenRouter error {data['error'].get('code')}")
            return data
        except httpx.HTTPError as e:
            last = e
    raise ModelUnavailable(f"OpenRouter unreachable ({type(last).__name__})")


def chat(messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False,
         on_token: Callable[[str], None] | None = None, temperature: float = 0.2, max_tokens: int = 2000) -> dict:
    # Mirrors the Qwen setup for a fair A/B: JSON tasks run without reasoning;
    # the tool loop may reason (hidden) with an extra token budget.
    reasoning = {"enabled": False} if json_mode else {"exclude": True}
    budget = max_tokens if json_mode else max_tokens + 4096
    body: dict[str, Any] = {"model": settings.openrouter_model, "messages": _to_openai(messages),
                            "temperature": temperature, "max_tokens": budget, "reasoning": reasoning}
    if tools:
        body["tools"] = tools
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    data = _post(body)
    msg = data["choices"][0]["message"]
    calls = []
    for c in msg.get("tool_calls") or []:
        fn = c.get("function", {})
        args = fn.get("arguments") or "{}"
        try:
            args = json.loads(args) if isinstance(args, str) else args
        except json.JSONDecodeError:
            args = {"_unparseable": args}
        calls.append({"id": c.get("id"), "function": {"name": fn.get("name", ""), "arguments": args}})
    content = strip_think(msg.get("content") or "")
    if on_token and content:
        on_token(content)
    u = data.get("usage") or {}
    return {"role": "assistant", "content": content, "tool_calls": calls,
            "_usage": {"prompt_tokens": u.get("prompt_tokens"), "output_tokens": u.get("completion_tokens"), "cost": u.get("cost")}}


def chat_json(system: str, user: str, max_tokens: int = 2000) -> Any:
    from ..ingest.extraction import parse_json_reply

    msg = chat([{"role": "system", "content": system}, {"role": "user", "content": user}], json_mode=True,
               temperature=0, max_tokens=max_tokens)
    return parse_json_reply(msg["content"])
