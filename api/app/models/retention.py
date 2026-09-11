"""Disk-bounded retention (§26).

The VPS is shared and small, so the high-frequency tables are trimmed on a
schedule.  The rule is deliberately asymmetric: *observations* expire,
*outcomes* never do.  Trades, positions, equity snapshots, backtest runs, risk
events and users are the audit trail of what the bot actually did and are never
deleted here.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models.events import DataQualityEvent, SystemEvent
from app.models.market import MarketStat
from app.models.signal import Signal, SignalFeature, StrategyDecision

_HOUR_MS = 3_600_000
_DAY_MS = 86_400_000

#: Tables this module is forbidden to touch, stated so the invariant is
#: greppable and testable rather than implicit in the code below.
PROTECTED_TABLES = (
    "trades",
    "positions",
    "equity_snapshots",
    "strategy_performance",
    "backtest_runs",
    "risk_events",
    "markets",
    "paper_orders",
    "paper_fills",
    "users",
)


def apply_retention(
    session: Session, *, now_ms: int, settings: Settings | None = None
) -> dict[str, int]:
    """Delete expired observation rows.  Returns deleted counts per table.

    ``market_stats`` follows ``worker.tick_retention_hours``; the event-ish
    tables follow ``worker.event_retention_days``.  ``signal_features`` is
    dropped for exactly the signals being removed, before the signals go, so no
    orphan payload can survive on a backend without cascade enforcement.
    """
    s = settings or get_settings()
    stats_cutoff = now_ms - s.worker.tick_retention_hours * _HOUR_MS
    event_cutoff = now_ms - s.worker.event_retention_days * _DAY_MS

    counts: dict[str, int] = {}

    counts["market_stats"] = _delete(session, MarketStat, MarketStat.ts_ms < stats_cutoff)

    stale_signal_ids = sa.select(Signal.id).where(Signal.ts_ms < event_cutoff)
    counts["signal_features"] = _delete(
        session, SignalFeature, SignalFeature.signal_id.in_(stale_signal_ids)
    )
    counts["signals"] = _delete(session, Signal, Signal.ts_ms < event_cutoff)
    counts["strategy_decisions"] = _delete(
        session, StrategyDecision, StrategyDecision.ts_ms < event_cutoff
    )
    counts["system_events"] = _delete(session, SystemEvent, SystemEvent.ts_ms < event_cutoff)
    counts["data_quality_events"] = _delete(
        session, DataQualityEvent, DataQualityEvent.ts_ms < event_cutoff
    )

    session.flush()
    return counts


def _delete(session: Session, model: type, criteria) -> int:
    result = session.execute(
        sa.delete(model).where(criteria).execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


__all__ = ["apply_retention", "PROTECTED_TABLES"]
