"""The one endpoint the dashboard polls.

The worker publishes a complete, pre-rendered payload per ledger.  If the worker
is not beating we still return the last snapshot but mark it stale, because a
frozen page that looks live is worse than an honest "no data feed".
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.auth import require_user
from app.config import get_settings
from app.runtime import RuntimeStore
from app.types import now_ms

router = APIRouter(tags=["dashboard"])

# Labels are derived from the equity each ledger holds; the hardcoded map this
# replaces still said "$500" for the primary book after it became $5,000.
from app.sim.ledger import ledger_label  # noqa: E402


def _empty_payload(ledger: str) -> dict:
    s = get_settings()
    start = s.risk.starting_equity
    if ledger != "base":
        try:
            start = float(ledger.lstrip("l"))
        except ValueError:
            start = s.risk.starting_equity
    return {
        "mode": s.mode,
        "live_orders_enabled": False,
        "simulator_version": s.simulator_version,
        "exchange": s.exchange.name,
        "generated_ms": now_ms(),
        "stale": True,
        "header": {"starting_equity": start, "data_connected": False},
        "metrics": {
            "equity": start, "cash": start, "pnl_today": 0.0, "pnl_total": 0.0,
            "open_positions": 0, "trades": 0, "win_rate": 0.0, "profit_factor": 0.0,
            "expectancy": 0.0, "max_drawdown": 0.0, "capital_turnover": 0.0,
            "avg_holding_seconds": 0.0,
        },
        "scanner": [], "positions": [], "trades": [], "strategies": [],
        "equity_curve": {"points": [], "realized": [], "version_boundaries": []},
        "health": {
            "ws_connected": False, "rest_ok": False, "book_age_ms": None,
            "updates_per_sec": 0.0, "reconnects": 0, "dropped": 0,
            "worker_cpu_pct": None, "worker_mem_mb": None, "db_ok": False,
            "last_evaluation_ms": None, "mode": s.mode,
        },
        "ledgers": [
            {"name": n, "label": ledger_label(n, v), "starting_equity": v, "equity": v}
            for n, v in (
                ("base", s.risk.starting_equity),
                *((f"l{int(e)}", e) for e in s.risk.comparison_ledgers),
            )
        ],
    }


@router.get("/dashboard")
def dashboard(
    ledger: str = Query(default="base"),
    _user: dict = Depends(require_user),
) -> dict:
    store = RuntimeStore()
    payload = store.read_dashboard(ledger)
    if payload is None:
        return _empty_payload(ledger)
    payload["stale"] = not store.worker_alive()
    return payload
