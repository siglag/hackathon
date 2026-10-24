from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any


def new_id(prefix: str) -> str:
    """Opaque IDs: random, carry no patient or file information."""
    return f"{prefix}_{secrets.token_hex(8)}"


def now() -> float:
    return time.time()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def loads(s: str | None, default: Any = None) -> Any:
    if s is None or s == "":
        return default
    return json.loads(s)


def strip_think(text: str) -> str:
    """Qwen3 may emit reasoning before '</think>' even with thinking disabled."""
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    return text.replace("<think>", "").strip()
