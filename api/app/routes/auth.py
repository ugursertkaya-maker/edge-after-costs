"""Login / logout / whoami."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, field_validator

from app.auth import (
    clear_session_cookie,
    issue_csrf_token,
    issue_session,
    require_user,
    set_csrf_cookie,
    set_session_cookie,
    verify_password,
)
from app.config import get_settings

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """Deliberately a plain string, not ``EmailStr``.

    This is an operator login for a private dashboard, not a signup form; a
    strict RFC validator only ever manages to lock someone out of their own
    box over a reserved TLD.
    """

    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _normalise(cls, v: str) -> str:
        v = v.strip().lower()
        if not v or "@" not in v:
            raise ValueError("geçersiz e-posta")
        return v


@router.post("/login")
def login(body: LoginRequest, response: Response) -> dict:
    s = get_settings()
    try:
        from app.db import get_session
        from app.models import User

        with get_session() as session:
            user = session.query(User).filter(User.email == body.email).one_or_none()
            if user is None or not verify_password(body.password, user.password_hash):
                raise HTTPException(status_code=401, detail="geçersiz kimlik bilgileri")
            role = user.role
    except HTTPException:
        raise
    except Exception:
        # No database yet (first boot): fall back to the bootstrap credentials
        # so the operator is never locked out of their own dashboard.
        if not s.bootstrap_admin_email or body.email != s.bootstrap_admin_email.lower():
            raise HTTPException(status_code=401, detail="geçersiz kimlik bilgileri")
        if body.password != s.bootstrap_admin_password:
            raise HTTPException(status_code=401, detail="geçersiz kimlik bilgileri")
        role = "admin"

    set_session_cookie(response, issue_session(body.email, role, settings=s), settings=s)
    set_csrf_cookie(response, issue_csrf_token(), settings=s)
    return {"ok": True, "email": body.email, "role": role}


@router.post("/logout")
def logout(response: Response) -> dict:
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/me")
def me(user: dict = Depends(require_user)) -> dict:
    return {"email": user["email"], "role": user.get("role", "viewer")}
