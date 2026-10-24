"""Authentication, sessions, CSRF and phone pairing (spec sections 7, 13, 14).

* Doctor sessions: HttpOnly SameSite=Strict cookie + double-submit CSRF header.
* Phone sessions: created only by exchanging a single-use pairing token and
  then confirmed on the workstation; restricted to uploading into one case.
* Tokens are stored hashed; raw tokens never reach logs (the QR carries the
  token in the URL fragment, which browsers do not send to the server).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, Response

from . import db
from .audit import audit
from .config import settings
from .util import new_id, now

SESSION_COOKIE = "ma_session"
CSRF_COOKIE = "ma_csrf"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt_hex, dk_hex = stored.split("$")
    except ValueError:
        return False
    dk = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(dk.hex(), dk_hex)


@dataclass
class Principal:
    kind: str  # doctor | phone
    user_id: str | None
    case_id: str | None  # phone sessions are bound to one case
    csrf: str
    token_hash: str
    pairing_id: str | None = None

    @property
    def actor(self) -> str:
        return f"user:{self.user_id}" if self.kind == "doctor" else f"phone:{self.pairing_id}"


def has_user() -> bool:
    return db.q1("SELECT 1 FROM users LIMIT 1") is not None


def create_user(username: str, password: str) -> str:
    if has_user():
        raise HTTPException(409, "already set up")
    if len(password) < 10:
        raise HTTPException(400, "password must be at least 10 characters")
    uid = new_id("usr")
    db.ex("INSERT INTO users(id, username, pw_hash, created_at) VALUES (?,?,?,?)", (uid, username, hash_password(password), now()))
    audit(f"user:{uid}", "setup")
    return uid


def login(username: str, password: str) -> str:
    r = db.q1("SELECT * FROM users WHERE username=?", (username,))
    if r is None or not verify_password(password, r["pw_hash"]):
        audit("anonymous", "login", result="denied")
        raise HTTPException(401, "invalid credentials")
    return r["id"]


def issue_session(response: Response, kind: str, user_id: str | None = None, case_id: str | None = None,
                  pairing_id: str | None = None, ttl_s: int | None = None) -> str:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    ttl = ttl_s if ttl_s is not None else settings.session_hours * 3600
    db.ex(
        "INSERT INTO sessions(token_hash, kind, user_id, case_id, pairing_id, csrf, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
        (_hash_token(token), kind, user_id, case_id, pairing_id, csrf, now(), now() + ttl),
    )
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", secure=settings.secure_cookies, max_age=ttl, path="/")
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, samesite="strict", secure=settings.secure_cookies, max_age=ttl, path="/")
    return csrf


def end_session(request: Request, response: Response) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        db.ex("UPDATE sessions SET revoked=1 WHERE token_hash=?", (_hash_token(token),))
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def check_origin(request: Request) -> None:
    """Reject cross-origin state-changing requests."""
    if request.method in SAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin is None:
        return  # non-browser clients; CSRF header still required for cookie sessions
    host = request.headers.get("host", "")
    allowed = set(settings.allowed_origins) | {f"http://{host}", f"https://{host}"}
    if origin not in allowed:
        raise HTTPException(403, "origin not allowed")


def _load(request: Request) -> Principal | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    r = db.q1("SELECT * FROM sessions WHERE token_hash=? AND revoked=0", (_hash_token(token),))
    if r is None or r["expires_at"] < now():
        return None
    if r["kind"] == "phone":
        p = db.q1("SELECT state, expires_at FROM pairings WHERE id=?", (r["pairing_id"],))
        if p is None or p["state"] == "revoked":
            return None
    return Principal(r["kind"], r["user_id"], r["case_id"], r["csrf"], r["token_hash"], r["pairing_id"])


def _require(request: Request, kinds: set[str]) -> Principal:
    check_origin(request)
    p = _load(request)
    if p is None or p.kind not in kinds:
        raise HTTPException(401, "authentication required")
    if request.method not in SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        if not hmac.compare_digest(sent, p.csrf):
            raise HTTPException(403, "CSRF token missing or invalid")
    return p


def doctor(request: Request) -> Principal:
    return _require(request, {"doctor"})


def phone(request: Request) -> Principal:
    p = _require(request, {"phone"})
    pr = db.q1("SELECT state FROM pairings WHERE id=?", (p.pairing_id,))
    if pr is None or pr["state"] != "confirmed":
        raise HTTPException(403, "pairing not confirmed on the workstation")
    return p


def phone_pending(request: Request) -> Principal:
    return _require(request, {"phone"})


def require_case(principal: Principal, case_id: str) -> dict:
    """Server-side case authorization, independent of model output."""
    r = db.q1("SELECT * FROM cases WHERE id=?", (case_id,))
    if r is None:
        raise HTTPException(404, "not found")
    if principal.kind == "doctor":
        if r["owner"] != principal.user_id:
            raise HTTPException(404, "not found")
    elif principal.case_id != case_id:
        raise HTTPException(404, "not found")
    return dict(r)


def owned(principal: Principal, table: str, obj_id: str) -> dict:
    """Load a case-scoped row and verify the principal may access its case."""
    assert table in {"documents", "datasets", "artifacts", "conversations", "jobs", "evidence_queries", "evidence_records"}
    r = db.q1(f"SELECT * FROM {table} WHERE id=?", (obj_id,))
    if r is None:
        raise HTTPException(404, "not found")
    require_case(principal, r["case_id"])
    return dict(r)


# ---- Pairing -----------------------------------------------------------------

def create_pairing(principal: Principal, case_id: str) -> tuple[str, str, float]:
    require_case(principal, case_id)
    token = secrets.token_urlsafe(24)
    pid = new_id("pair")
    exp = now() + settings.pairing_ttl_s
    db.ex(
        "INSERT INTO pairings(id, token_hash, case_id, state, created_at, expires_at) VALUES (?,?,?,?,?,?)",
        (pid, _hash_token(token), case_id, "issued", now(), exp),
    )
    audit(principal.actor, "pairing.issue", case_id, [pid])
    return pid, token, exp


def exchange_pairing(token: str, device_label: str, response: Response) -> str:
    th = _hash_token(token)
    with db.tx() as c:
        r = c.execute("SELECT * FROM pairings WHERE token_hash=?", (th,)).fetchone()
        if r is None or r["state"] != "issued" or r["expires_at"] < now():
            raise HTTPException(410, "pairing code expired or already used")
        # Single use: the token hash is replaced so the code cannot be replayed.
        c.execute("UPDATE pairings SET state='exchanged', token_hash=?, device_label=? WHERE id=?",
                  (secrets.token_hex(32), device_label[:60], r["id"]))
    issue_session(response, "phone", case_id=r["case_id"], pairing_id=r["id"], ttl_s=settings.phone_session_minutes * 60)
    audit(f"phone:{r['id']}", "pairing.exchange", r["case_id"], [r["id"]])
    return r["id"]


def set_pairing_state(principal: Principal, pairing_id: str, state: str) -> None:
    r = db.q1("SELECT * FROM pairings WHERE id=?", (pairing_id,))
    if r is None:
        raise HTTPException(404, "not found")
    require_case(principal, r["case_id"])
    if state == "confirmed" and r["state"] != "exchanged":
        raise HTTPException(409, "phone has not opened the pairing link yet")
    db.ex("UPDATE pairings SET state=? WHERE id=?", (state, pairing_id))
    if state == "revoked":
        db.ex("UPDATE sessions SET revoked=1 WHERE pairing_id=?", (pairing_id,))
    audit(principal.actor, f"pairing.{state}", r["case_id"], [pairing_id])


DoctorDep = Depends(doctor)
PhoneDep = Depends(phone)
