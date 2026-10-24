"""LLM facade: DeepSeek via OpenRouter by default (cloud, explicit opt-in).

``use("ollama"|"openrouter")`` overrides the provider for the current thread,
which the A/B harness uses to run identical tasks against both.
"""
from __future__ import annotations

import contextlib
import contextvars
from typing import Any, Iterator

from ..config import settings
from . import ollama, openrouter
from .ollama import ModelUnavailable  # noqa: F401 - re-exported

_override: contextvars.ContextVar[str | None] = contextvars.ContextVar("llm_provider", default=None)
PROVIDERS = {"ollama": ollama, "openrouter": openrouter}


def provider_name() -> str:
    name = _override.get() or settings.llm_provider
    if name == "openrouter" and not settings.allow_cloud_model:
        raise ModelUnavailable("Cloud model disabled: set MEDASSIST_ALLOW_CLOUD_MODEL=1 to send case content to OpenRouter.")
    return name


def _mod():
    return PROVIDERS[provider_name()]


@contextlib.contextmanager
def use(name: str) -> Iterator[None]:
    token = _override.set(name)
    try:
        yield
    finally:
        _override.reset(token)


def model_label() -> str:
    return settings.openrouter_model if provider_name() == "openrouter" else settings.model


def status() -> dict:
    try:
        name = provider_name()
    except ModelUnavailable as e:
        return {"ok": False, "provider": settings.llm_provider, "cloud": True, "error": str(e), "model": settings.openrouter_model}
    s = PROVIDERS[name].status()
    s.setdefault("provider", name)
    s.setdefault("cloud", name != "ollama")
    return s


def chat(*args: Any, **kwargs: Any) -> dict:
    return _mod().chat(*args, **kwargs)


def chat_json(system: str, user: str, max_tokens: int = 2000) -> Any:
    return _mod().chat_json(system, user, max_tokens=max_tokens)
