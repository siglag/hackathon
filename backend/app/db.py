"""SQLite storage with FTS5. One connection per thread; WAL mode.

SQLite does not encrypt data: deployment must use full-disk encryption
(spec section 14).
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, user_id TEXT, case_id TEXT, pairing_id TEXT,
  csrf TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pairings (
  id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, case_id TEXT NOT NULL, state TEXT NOT NULL,
  device_label TEXT, created_at REAL NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cases (
  id TEXT PRIMARY KEY, owner TEXT NOT NULL, label TEXT NOT NULL, retention_policy TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), title TEXT NOT NULL,
  model TEXT, runtime_version TEXT, selected_documents TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, case_id TEXT NOT NULL, role TEXT NOT NULL,
  content TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}', job_id TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL, filename TEXT NOT NULL, mime TEXT NOT NULL, kind TEXT NOT NULL,
  sha256 TEXT NOT NULL, size INTEGER NOT NULL, original_ref TEXT, working_ref TEXT, state TEXT NOT NULL,
  page_count INTEGER, source TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}', parent_document_id TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS extractions (
  document_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, version INTEGER NOT NULL, schema TEXT NOT NULL,
  canonical TEXT NOT NULL, rows TEXT NOT NULL, review_state TEXT NOT NULL, parser_version TEXT NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS datasets (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL, label TEXT NOT NULL, parent_id TEXT, lineage_id TEXT NOT NULL,
  version INTEGER NOT NULL, columns TEXT NOT NULL, rows TEXT NOT NULL, source_documents TEXT NOT NULL,
  transformations TEXT NOT NULL, review_state TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL, dataset_id TEXT, lineage_id TEXT NOT NULL, version INTEGER NOT NULL,
  format TEXT NOT NULL, title TEXT NOT NULL, storage_ref TEXT NOT NULL, preview TEXT NOT NULL,
  params TEXT NOT NULL, review_state TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_queries (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL, conversation_id TEXT NOT NULL, question_message_id TEXT,
  request TEXT NOT NULL, request_hash TEXT NOT NULL, search_string TEXT NOT NULL, held_terms TEXT NOT NULL,
  status TEXT NOT NULL, policy_version TEXT NOT NULL, provider TEXT NOT NULL, outcome TEXT,
  approved_at REAL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_records (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL, query_id TEXT NOT NULL, pmid TEXT, doi TEXT, pmcid TEXT,
  metadata TEXT NOT NULL, passages TEXT NOT NULL, retrieval_time REAL NOT NULL, content_hash TEXT NOT NULL,
  access_level TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, stage TEXT,
  payload TEXT NOT NULL, result TEXT, error TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
  attempt INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS job_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, ts REAL NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS job_events_job ON job_events(job_id, seq);
CREATE TABLE IF NOT EXISTS idempotency (
  scope TEXT NOT NULL, key TEXT NOT NULL, result TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(scope, key)
);
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT NOT NULL, case_id TEXT,
  operation TEXT NOT NULL, ids TEXT NOT NULL, result TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
  content, case_id UNINDEXED, ref_type UNINDEXED, ref_id UNINDEXED, tokenize='unicode61'
);
"""

_local = threading.local()


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(settings.db_path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        _local.conn = conn
    return conn


def init_db() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    try:
        settings.data_dir.chmod(0o700)
    except OSError:
        pass
    connect().executescript(SCHEMA)


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def q(sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, params).fetchall()


def q1(sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
    return connect().execute(sql, params).fetchone()


def ex(sql: str, params: tuple | dict = ()) -> int:
    cur = connect().execute(sql, params)
    return cur.rowcount


def row_dict(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


def index_text(case_id: str, ref_type: str, ref_id: str, content: str) -> None:
    ex("DELETE FROM search_fts WHERE ref_type=? AND ref_id=?", (ref_type, ref_id))
    if content.strip():
        ex("INSERT INTO search_fts(content, case_id, ref_type, ref_id) VALUES (?,?,?,?)", (content, case_id, ref_type, ref_id))
