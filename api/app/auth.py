"""Session-cookie auth for the dashboard.

The dashboard exposes no trading controls beyond a kill switch, but it does show
the whole book, so it sits behind a login.  Sessions are signed cookies — there
is no session table to keep in sync and no token to leak into a URL.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode

import secrets

from fastapi import Cookie, Header, HTTPException, Response, status
from passlib.context import CryptContext

from app.config import Settings, get_settings

COOKIE_NAME = "cb_session"
# Readable by JS on purpose: the browser echoes it back in X-CSRF-Token so a
# cross-site form post, which cannot read the cookie, fails the comparison.
CSRF_COOKIE = "cb_csrf"
SESSION_TTL_SECONDS = 7 * 24 * 3600

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(raw: str) -> str:
    return pwd_context.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(raw, hashed)
    except Exception:
        return False


def _sign(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def issue_session(email: str, role: str, *, settings: Settings | None = None) -> str:
    s = settings or get_settings()
    body = json.dumps(
        {"email": email, "role": role, "exp": int(time.time()) + SESSION_TTL_SECONDS},
        separators=(",", ":"),
    ).encode()
    b = urlsafe_b64encode(body).decode().rstrip("=")
    return f"{b}.{_sign(body, s.secret_key)}"


def read_session(token: str | None, *, settings: Settings | None = None) -> dict | None:
    if not token or "." not in token:
        return None
    s = settings or get_settings()
    b, sig = token.rsplit(".", 1)
    try:
        body = urlsafe_b64decode(b + "=" * (-len(b) % 4))
    except Exception:
        return None
    if not hmac.compare_digest(sig, _sign(body, s.secret_key)):
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    if int(data.get("exp", 0)) < time.time():
        return None
    return data


def set_session_cookie(response: Response, token: str, *, settings: Settings | None = None) -> None:
    s = settings or get_settings()
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=s.cookie_secure,
        samesite="lax",
        path=s.base_path or "/",
    )


def issue_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def set_csrf_cookie(response: Response, token: str, *, settings: Settings | None = None) -> None:
    s = settings or get_settings()
    response.set_cookie(
        CSRF_COOKIE,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=False,
        secure=s.cookie_secure,
        samesite="lax",
        path=s.base_path or "/",
    )


def require_csrf(
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    cb_csrf: str | None = Cookie(default=None, alias=CSRF_COOKIE),
) -> None:
    """Double-submit check for state-changing requests."""
    if not cb_csrf or not x_csrf_token or not hmac.compare_digest(cb_csrf, x_csrf_token):
        raise HTTPException(status_code=403, detail="csrf doğrulaması başarısız")


def clear_session_cookie(response: Response, *, settings: Settings | None = None) -> None:
    s = settings or get_settings()
    response.delete_cookie(COOKIE_NAME, path=s.base_path or "/")
    response.delete_cookie(CSRF_COOKIE, path=s.base_path or "/")


def require_user(cb_session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict:
    data = read_session(cb_session)
    if data is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    return data
