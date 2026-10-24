"""Audit events and content-free operational logging (spec section 14).

Audit entries and log lines carry operation names and opaque IDs only: never
record text, prompts, tokens, filenames or image contents.
"""
from __future__ import annotations

import logging

from . import db
from .util import dumps, now

log = logging.getLogger("medassist")


def audit(actor: str, operation: str, case_id: str | None = None, ids: list[str] | None = None, result: str = "ok") -> None:
    db.ex(
        "INSERT INTO audit(ts, actor, case_id, operation, ids, result) VALUES (?,?,?,?,?,?)",
        (now(), actor, case_id, operation, dumps(ids or []), result),
    )
    log.info("op=%s case=%s ids=%s result=%s", operation, case_id, ",".join(ids or []), result)
