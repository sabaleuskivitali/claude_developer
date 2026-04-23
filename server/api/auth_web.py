"""
Web session auth for cabinet users.
Uses HMAC-signed cookies — no extra dependencies beyond stdlib.
Cookie format: <user_id>:<timestamp_unix>:<hmac_hex>
"""
import hashlib
import hmac
import os
import time

from fastapi import Cookie, HTTPException, Request, status
from fastapi.responses import RedirectResponse

_SECRET = os.environ.get("SESSION_SECRET", "change-me-in-production").encode()
_COOKIE = "session"
_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


# ── password hashing ──────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    salt = os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
    return f"{salt}:{h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split(":", 1)
    except ValueError:
        return False
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
    return hmac.compare_digest(h.hex(), expected)


# ── cookie signing ─────────────────────────────────────────────────────────────

def _sign(user_id: int, ts: int) -> str:
    msg = f"{user_id}:{ts}".encode()
    return hmac.new(_SECRET, msg, hashlib.sha256).hexdigest()


def make_session_cookie(user_id: int) -> str:
    ts = int(time.time())
    sig = _sign(user_id, ts)
    return f"{user_id}:{ts}:{sig}"


def _parse_cookie(value: str) -> int | None:
    """Return user_id if cookie is valid and not expired, else None."""
    try:
        user_id_s, ts_s, sig = value.split(":", 2)
        user_id = int(user_id_s)
        ts = int(ts_s)
    except (ValueError, AttributeError):
        return None
    if not hmac.compare_digest(_sign(user_id, ts), sig):
        return None
    if time.time() - ts > _MAX_AGE:
        return None
    return user_id


# ── FastAPI dependency ─────────────────────────────────────────────────────────

async def get_current_user(request: Request, session: str | None = Cookie(default=None)):
    """Dependency: returns user row or raises 401 (redirects to /login for browser)."""
    user_id = _parse_cookie(session) if session else None
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    row = await request.app.state.db.fetchrow(
        "SELECT id, email FROM users WHERE id = $1", user_id
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    return row


def set_cookie(response, user_id: int):
    response.set_cookie(
        _COOKIE,
        make_session_cookie(user_id),
        max_age=_MAX_AGE,
        httponly=True,
        samesite="lax",
    )


def clear_cookie(response):
    response.delete_cookie(_COOKIE)
