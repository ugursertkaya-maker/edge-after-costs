"""Retention keeps the disk bounded without ever losing an outcome."""

from __future__ import annotations

import dataclasses

import pytest
import sqlalchemy as sa

from app.config import WorkerConfig, get_settings
from app.db import make_engine
from app.models import (
    Base,
    DataQualityEvent,
    EquitySnapshot,
    Market,
    MarketStat,
    PaperFill,
    PaperOrder,
    Position,
    RiskEvent,
    Signal,
    SignalFeature,
    StrategyDecision,
    StrategyPerformance,
    SystemEvent,
    Trade,
    User,
    apply_retention,
)

HOUR = 3_600_000
DAY = 86_400_000
NOW = 1_800_000_000_000  # fixed clock: retention must never read the wall clock

SETTINGS = dataclasses.replace(
    get_settings(),
    worker=WorkerConfig(tick_retention_hours=48, event_retention_days=30),
)


@pytest.fixture
def session():
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sa.orm.Session(engine, expire_on_commit=False) as s:
        yield s
    engine.dispose()


def _seed(session):
    """Two rows per table: one comfortably inside the window, one outside."""
    fresh_stat = NOW - 1 * HOUR
    stale_stat = NOW - 49 * HOUR
    fresh_event = NOW - 29 * DAY
    stale_event = NOW - 31 * DAY

    session.add_all(
        [
            MarketStat(ts_ms=fresh_stat, symbol="BTCUSD", price=1.0),
            MarketStat(ts_ms=stale_stat, symbol="BTCUSD", price=2.0),
            StrategyDecision(ts_ms=fresh_event, symbol="BTCUSD", strategy="a"),
            StrategyDecision(ts_ms=stale_event, symbol="BTCUSD", strategy="a"),
            SystemEvent(ts_ms=fresh_event, level="INFO", source="w", message="fresh"),
            SystemEvent(ts_ms=stale_event, level="INFO", source="w", message="stale"),
            DataQualityEvent(ts_ms=fresh_event, symbol="BTCUSD", kind="STALE"),
            DataQualityEvent(ts_ms=stale_event, symbol="BTCUSD", kind="GAP"),
            # Never-deleted tables, seeded old on purpose.
            Trade(trade_id="t-old", symbol="BTCUSD", exit_ms=stale_event, net_pnl_usd=1.0),
            Position(position_id="p-old", symbol="BTCUSD", entry_ms=stale_event),
            EquitySnapshot(ts_ms=stale_event, ledger="base", equity=500.0),
            StrategyPerformance(ts_ms=stale_event, ledger="base", strategy="a", window="all"),
            RiskEvent(ts_ms=stale_event, kind="KILL_SWITCH"),
            Market(symbol="BTCUSD", last_seen_ms=stale_event),
            PaperOrder(ts_ms=stale_event, symbol="BTCUSD"),
            User(email="keep@me.com", password_hash="x"),
        ]
    )
    fresh_sig = Signal(ts_ms=fresh_event, symbol="BTCUSD", strategy="a")
    stale_sig = Signal(ts_ms=stale_event, symbol="BTCUSD", strategy="a")
    session.add_all([fresh_sig, stale_sig])
    session.flush()
    session.add_all(
        [
            SignalFeature(signal_id=fresh_sig.id, payload={"keep": 1}),
            SignalFeature(signal_id=stale_sig.id, payload={"drop": 1}),
        ]
    )
    session.add(PaperFill(order_id=1, price=1.0, qty=1.0, ts_ms=stale_event))
    session.flush()
    return fresh_sig.id, stale_sig.id


def _count(session, model) -> int:
    return session.scalar(sa.select(sa.func.count()).select_from(model))


def test_apply_retention_deletes_exactly_the_expired_observations(session):
    fresh_sig_id, _ = _seed(session)

    counts = apply_retention(session, now_ms=NOW, settings=SETTINGS)

    assert counts == {
        "market_stats": 1,
        "signal_features": 1,
        "signals": 1,
        "strategy_decisions": 1,
        "system_events": 1,
        "data_quality_events": 1,
    }
    assert _count(session, MarketStat) == 1
    assert session.scalar(sa.select(MarketStat.price)) == 1.0
    assert _count(session, Signal) == 1
    assert session.scalar(sa.select(Signal.id)) == fresh_sig_id
    assert _count(session, SignalFeature) == 1
    assert session.scalar(sa.select(SignalFeature.payload)) == {"keep": 1}
    assert _count(session, StrategyDecision) == 1
    assert _count(session, SystemEvent) == 1
    assert session.scalar(sa.select(SystemEvent.message)) == "fresh"
    assert _count(session, DataQualityEvent) == 1


def test_apply_retention_never_touches_outcome_tables(session):
    _seed(session)
    apply_retention(session, now_ms=NOW, settings=SETTINGS)

    for model in (
        Trade, Position, EquitySnapshot, StrategyPerformance, RiskEvent,
        Market, PaperOrder, PaperFill, User,
    ):
        assert _count(session, model) >= 1, model.__tablename__


def test_no_orphan_feature_rows_survive(session):
    _seed(session)
    apply_retention(session, now_ms=NOW, settings=SETTINGS)

    orphans = session.scalar(
        sa.select(sa.func.count())
        .select_from(SignalFeature)
        .where(~SignalFeature.signal_id.in_(sa.select(Signal.id)))
    )
    assert orphans == 0


def test_retention_windows_come_from_settings(session):
    """Zero-length windows keep nothing: the cutoffs are read from settings."""
    _seed(session)
    tight = dataclasses.replace(
        get_settings(), worker=WorkerConfig(tick_retention_hours=0, event_retention_days=0)
    )
    counts = apply_retention(session, now_ms=NOW, settings=tight)

    assert counts["market_stats"] == 2
    assert counts["signals"] == 2
    assert counts["signal_features"] == 2
    assert counts["system_events"] == 2
    assert _count(session, MarketStat) == 0
    assert _count(session, Trade) == 1


def test_retention_on_an_empty_database_is_a_no_op(session):
    counts = apply_retention(session, now_ms=NOW, settings=SETTINGS)
    assert set(counts) == {
        "market_stats", "signal_features", "signals",
        "strategy_decisions", "system_events", "data_quality_events",
    }
    assert sum(counts.values()) == 0


def test_boundary_rows_exactly_at_the_cutoff_are_kept(session):
    """``<`` not ``<=``: a row landing on the cutoff ms is still in-window."""
    session.add_all(
        [
            MarketStat(ts_ms=NOW - 48 * HOUR, symbol="BTCUSD", price=1.0),
            SystemEvent(ts_ms=NOW - 30 * DAY, level="INFO", source="w", message="edge"),
        ]
    )
    session.flush()
    counts = apply_retention(session, now_ms=NOW, settings=SETTINGS)
    assert counts["market_stats"] == 0
    assert counts["system_events"] == 0
