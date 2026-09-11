"""Liveness and readiness."""

from __future__ import annotations

from fastapi import APIRouter

from app.config import get_settings
from app.runtime import RuntimeStore
from app.types import now_ms

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    s = get_settings()
    store = RuntimeStore(settings=s)
    try:
        from app.db import healthcheck

        db_ok = healthcheck()
    except Exception:
        db_ok = False

    beat = store.last_beat_ms()
    ts = now_ms()
    return {
        "status": "ok",
        "mode": s.mode,
        "live_orders_enabled": False,
        "exchange": s.exchange.name,
        "simulator_version": s.simulator_version,
        "db": db_ok,
        "redis": store.connected,
        "worker_alive": store.worker_alive(now_ms=ts),
        "worker_last_beat_ms": beat,
        "worker_lag_ms": (ts - beat) if beat else None,
        "ts_ms": ts,
    }
