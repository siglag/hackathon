"""Local Ollama client. There is no cloud fallback of any kind (spec 4, 9)."""
from __future__ import annotations

import json
from typing import Any, Callable

import httpx

from ..config import settings
from ..util import strip_think


class ModelUnavailable(RuntimeError):
    pass


def _client() -> httpx.Client:
    return httpx.Client(base_url=settings.ollama_url, timeout=httpx.Timeout(settings.model_timeout_s, connect=5), trust_env=False)


def status() -> dict:
    try:
        with httpx.Client(base_url=settings.ollama_url, timeout=5, trust_env=False) as c:
            tags = c.get("/api/tags").json()
            ver = c.get("/api/version").json().get("version")
        names = [m["name"] for m in tags.get("models", [])]
        digest = next((m.get("digest") for m in tags.get("models", []) if m["name"] == settings.model), None)
        return {"ok": settings.model in names, "version": ver, "model": settings.model, "digest": digest, "models": names}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": type(e).__name__, "model": settings.model}


def chat(messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False,
         on_token: Callable[[str], None] | None = None, temperature: float = 0.2, max_tokens: int = 2000) -> dict:
    body: dict[str, Any] = {
        "model": settings.model,
        "messages": messages,
        "stream": on_token is not None,
        "think": settings.think,
        # Reasoning tokens count against num_predict, so budget for them.
        "options": {"num_ctx": settings.num_ctx, "temperature": temperature,
                    "num_predict": max_tokens + (4096 if settings.think else 0)},
        "keep_alive": "15m",
    }
    if tools:
        body["tools"] = tools
    if json_mode:
        body["format"] = "json"
    try:
        with _client() as c:
            if on_token is None:
                r = c.post("/api/chat", json=body)
                r.raise_for_status()
                data = r.json()
                msg = data.get("message", {})
            else:
                content, calls, data = "", [], {}
                with c.stream("POST", "/api/chat", json=body) as r:
                    r.raise_for_status()
                    in_think = False
                    for line in r.iter_lines():  # message.thinking is never streamed or stored
                        if not line:
                            continue
                        data = json.loads(line)
                        m = data.get("message", {})
                        piece = m.get("content", "")
                        if m.get("tool_calls"):
                            calls += m["tool_calls"]
                        if piece:
                            content += piece
                            # Do not stream reasoning text that Qwen may emit.
                            if "<think>" in piece:
                                in_think = True
                            if not in_think and "</think>" not in content:
                                on_token(piece)
                            if "</think>" in piece:
                                in_think = False
                msg = {"role": "assistant", "content": content, "tool_calls": calls}
    except httpx.HTTPError as e:
        raise ModelUnavailable(f"Local model unavailable ({type(e).__name__}). Check that Ollama is running.") from None
    msg["content"] = strip_think(msg.get("content") or "")
    msg["_usage"] = {"prompt_tokens": data.get("prompt_eval_count"), "output_tokens": data.get("eval_count")}
    return msg


def _chatml(system: str, user: str) -> str:
    # Qwen chat template with an empty reasoning block: the thinking-only build
    # otherwise spends its whole token budget reasoning on structured tasks.
    return (f"<|im_start|>system\n{system}<|im_end|>\n<|im_start|>user\n{user}<|im_end|>\n"
            "<|im_start|>assistant\n<think>\n\n</think>\n\n")


def chat_json(system: str, user: str, max_tokens: int = 2000) -> Any:
    """Structured (JSON) completion for classification, mapping and drafting."""
    from ..ingest.extraction import parse_json_reply

    if not settings.raw_json_no_think:
        msg = chat([{"role": "system", "content": system}, {"role": "user", "content": user}], json_mode=True,
                   temperature=0, max_tokens=max_tokens)
        return parse_json_reply(msg["content"])
    body = {"model": settings.model, "prompt": _chatml(system, user), "raw": True, "stream": False, "format": "json",
            "options": {"num_ctx": settings.num_ctx, "temperature": 0, "num_predict": max_tokens}, "keep_alive": "15m"}
    try:
        with _client() as c:
            r = c.post("/api/generate", json=body)
            r.raise_for_status()
            text = r.json().get("response", "")
    except httpx.HTTPError as e:
        raise ModelUnavailable(f"Local model unavailable ({type(e).__name__}). Check that Ollama is running.") from None
    return parse_json_reply(text)
