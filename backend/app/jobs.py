"""Persistent background jobs with progress events (spec section 13).

* Job state and events live in SQLite, so a crash leaves a visible
  ``interrupted`` state that the doctor can retry.
* Only one OCR/model-heavy job runs at a time (spec section 15).
* Cancellation is cooperative: handlers call ``ctx.check()`` between stages;
  temporary outputs registered with ``ctx.temp()`` are removed on cancel/failure.
"""
from __future__ import annotations

import logging
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from . import db
from .util import dumps, loads, new_id, now

log = logging.getLogger("medassist.jobs")

STAGES = ("queued", "uploaded", "parsing", "ocr", "needs_review", "retrieving_evidence", "generating", "ready", "failed", "cancelled")

Handler = Callable[["JobContext"], dict | None]
_handlers: dict[str, tuple[Handler, bool]] = {}
_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="job")
_heavy = threading.Semaphore(1)


class Cancelled(Exception):
    pass


class JobError(Exception):
    """A failure whose message is safe to show the doctor."""


def handler(kind: str, heavy: bool = False):
    def deco(fn: Handler) -> Handler:
        _handlers[kind] = (fn, heavy)
        return fn
    return deco


class JobContext:
    def __init__(self, job: dict):
        self.job = job
        self.id = job["id"]
        self.case_id = job["case_id"]
        self.payload = loads(job["payload"], {})
        self._temps: list[Path] = []
        self._cleanups: list[Callable[[], None]] = []

    def emit(self, type_: str, data: dict | None = None) -> None:
        db.ex("INSERT INTO job_events(job_id, ts, type, data) VALUES (?,?,?,?)", (self.id, now(), type_, dumps(data or {})))

    def stage(self, stage: str, detail: str | None = None, progress: float | None = None) -> None:
        self.check()
        db.ex("UPDATE jobs SET stage=?, updated_at=? WHERE id=?", (stage, now(), self.id))
        self.emit("stage", {"stage": stage, "detail": detail, "progress": progress})

    def check(self) -> None:
        r = db.q1("SELECT cancel_requested FROM jobs WHERE id=?", (self.id,))
        if r and r["cancel_requested"]:
            raise Cancelled()

    def temp(self, p: Path) -> Path:
        self._temps.append(p)
        return p

    def on_cleanup(self, fn: Callable[[], None]) -> None:
        self._cleanups.append(fn)

    def cleanup(self) -> None:
        for p in self._temps:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        for fn in self._cleanups:
            try:
                fn()
            except Exception:
                log.exception("cleanup failed job=%s", self.id)


def submit(case_id: str, kind: str, payload: dict) -> str:
    jid = new_id("job")
    t = now()
    db.ex(
        "INSERT INTO jobs(id, case_id, kind, state, stage, payload, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (jid, case_id, kind, "queued", "queued", dumps(payload), t, t),
    )
    db.ex("INSERT INTO job_events(job_id, ts, type, data) VALUES (?,?,?,?)", (jid, t, "stage", dumps({"stage": "queued"})))
    _pool.submit(_run, jid)
    return jid


def _run(job_id: str) -> None:
    job = db.row_dict(db.q1("SELECT * FROM jobs WHERE id=?", (job_id,)))
    if job is None:
        return
    fn, heavy = _handlers[job["kind"]]
    ctx = JobContext(job)
    if heavy:
        ctx.emit("stage", {"stage": "queued", "detail": "waiting for the OCR/model slot"})
        _heavy.acquire()
    try:
        ctx.check()
        db.ex("UPDATE jobs SET state='running', updated_at=? WHERE id=?", (now(), job_id))
        result = fn(ctx) or {}
        final_stage = result.pop("_final_stage", "ready")
        db.ex("UPDATE jobs SET state='done', stage=?, result=?, updated_at=? WHERE id=?", (final_stage, dumps(result), now(), job_id))
        ctx.emit("stage", {"stage": final_stage})
        ctx.emit("done", result)
    except Cancelled:
        ctx.cleanup()
        db.ex("UPDATE jobs SET state='cancelled', stage='cancelled', updated_at=? WHERE id=?", (now(), job_id))
        ctx.emit("stage", {"stage": "cancelled", "detail": "Cancelled. Partial outputs were discarded."})
        ctx.emit("done", {"cancelled": True})
    except Exception as e:  # noqa: BLE001
        ctx.cleanup()
        msg = str(e) if isinstance(e, JobError) else f"{type(e).__name__}: processing failed"
        # Log type and location only; exception messages could contain record text.
        log.error("job failed id=%s kind=%s err=%s at=%s", job_id, job["kind"], type(e).__name__,
                  traceback.extract_tb(e.__traceback__)[-1:] if e.__traceback__ else "")
        db.ex("UPDATE jobs SET state='failed', stage='failed', error=?, updated_at=? WHERE id=?", (msg, now(), job_id))
        ctx.emit("stage", {"stage": "failed", "detail": msg})
        ctx.emit("done", {"error": msg})
    finally:
        if heavy:
            _heavy.release()


def cancel(job_id: str) -> None:
    db.ex("UPDATE jobs SET cancel_requested=1, updated_at=? WHERE id=? AND state IN ('queued','running')", (now(), job_id))


def retry(job_id: str) -> None:
    """Re-run a failed/interrupted job in place (same ID, so no duplicate outputs)."""
    r = db.q1("SELECT state FROM jobs WHERE id=?", (job_id,))
    if r is None or r["state"] not in {"failed", "interrupted", "cancelled"}:
        raise ValueError("job is not retryable")
    db.ex("UPDATE jobs SET state='queued', stage='queued', error=NULL, cancel_requested=0, attempt=attempt+1, updated_at=? WHERE id=?",
          (now(), job_id))
    db.ex("INSERT INTO job_events(job_id, ts, type, data) VALUES (?,?,?,?)", (job_id, now(), "stage", dumps({"stage": "queued", "detail": "retrying"})))
    _pool.submit(_run, job_id)


def recover_after_restart() -> None:
    rows = db.q("SELECT id FROM jobs WHERE state IN ('queued','running')")
    for r in rows:
        db.ex("UPDATE jobs SET state='interrupted', stage='failed', error=?, updated_at=? WHERE id=?",
              ("Interrupted by an application restart. Retry to continue.", now(), r["id"]))
        db.ex("INSERT INTO job_events(job_id, ts, type, data) VALUES (?,?,?,?)",
              (r["id"], now(), "stage", dumps({"stage": "failed", "detail": "Interrupted by restart; retry available."})))
        db.ex("INSERT INTO job_events(job_id, ts, type, data) VALUES (?,?,?,?)", (r["id"], now(), "done", dumps({"error": "interrupted"})))


def events_since(job_id: str, after_seq: int) -> list[dict[str, Any]]:
    rows = db.q("SELECT seq, ts, type, data FROM job_events WHERE job_id=? AND seq>? ORDER BY seq", (job_id, after_seq))
    return [{"seq": r["seq"], "ts": r["ts"], "type": r["type"], "data": loads(r["data"], {})} for r in rows]


def idempotent(scope: str, key: str | None, fn: Callable[[], dict]) -> dict:
    if not key:
        return fn()
    key = key[:128]
    r = db.q1("SELECT result FROM idempotency WHERE scope=? AND key=?", (scope, key))
    if r:
        return loads(r["result"])
    result = fn()
    db.ex("INSERT OR IGNORE INTO idempotency(scope, key, result, created_at) VALUES (?,?,?,?)", (scope, key, dumps(result), now()))
    return result
