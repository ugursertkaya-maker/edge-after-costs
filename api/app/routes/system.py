"""Operational endpoints: the kill switch, and the paper-only proof."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app.auth import require_csrf, require_user
from app.config import get_settings
from app.runtime import RuntimeStore
from app.safety import source_tree_is_paper_only

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/kill-switch")
def read_kill_switch(_user: dict = Depends(require_user)) -> dict:
    return {"enabled": RuntimeStore().kill_switch()}


@router.post("/kill-switch")
def set_kill_switch(
    enabled: bool,
    user: dict = Depends(require_user),
    _csrf: None = Depends(require_csrf),
) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="yetki yok")
    RuntimeStore().set_kill_switch(enabled)
    return {"enabled": enabled}


@router.get("/safety")
def safety(_user: dict = Depends(require_user)) -> dict:
    """Live proof that no code path in this container can place a real order."""
    s = get_settings()
    root = Path(__file__).resolve().parent.parent
    violations = source_tree_is_paper_only(root)
    return {
        "mode": s.mode,
        "live_orders_enabled": False,
        "exchange_credentials_configured": False,
        "withdrawal_permission": False,
        "source_scan_violations": violations,
        "clean": not violations,
    }


@router.get("/events")
def events(limit: int = 100, _user: dict = Depends(require_user)) -> dict:
    try:
        from app.db import get_session
        from app.models import SystemEvent

        with get_session() as session:
            rows = (
                session.query(SystemEvent)
                .order_by(SystemEvent.ts_ms.desc())
                .limit(min(limit, 500))
                .all()
            )
            return {
                "events": [
                    {
                        "ts_ms": r.ts_ms, "level": r.level, "source": r.source,
                        "message": r.message, "detail": r.detail,
                    }
                    for r in rows
                ]
            }
    except Exception:
        return {"events": []}
