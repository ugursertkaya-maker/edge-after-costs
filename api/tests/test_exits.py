"""Exit engine: levels that clear costs, and exits at prices that exist."""

from __future__ import annotations

import dataclasses

import pytest

from app.config import ExitConfig, Settings
from app.exits import ExitEngine
from app.types import (
    BookLevel,
    ExitReason,
    Features,
    OrderBook,
    Position,
    Side,
    Signal,
    SymbolInfo,
)

INFO = SymbolInfo(
    symbol="BTCUSD",
    base_asset="BTC",
    quote_asset="USD",
    status="TRADING",
    tick_size=0.01,
    step_size=0.00001,
    min_qty=0.00001,
    min_notional=1.0,
)


def features(**over: float) -> Features:
    base: dict[str, object] = dict(
        symbol="BTCUSD",
        ts_ms=1_700_000_000_000,
        price=100.0,
        mid=100.0,
        microprice=100.0,
        spread_bps=2.0,
        book_age_ms=50,
        depth_bid_usd=50_000.0,
        depth_ask_usd=50_000.0,
        imbalance=0.0,
        atr_bps=20.0,
        realized_vol_bps=18.0,
        vol_percentile=0.5,
        ret_1m_bps=3.0,
        ret_5m_bps=6.0,
        ret_15m_bps=9.0,
        ema_fast=100.1,
        ema_slow=99.9,
        ema_slope_bps=1.0,
        trend_strength=0.6,
        range_pos=0.5,
        bb_width_bps=60.0,
        compression=0.3,
        volume_ratio=1.2,
        trade_flow_imbalance=0.2,
        trade_flow_accel=0.1,
        vwap=100.0,
        vwap_dev_bps=1.0,
        quote_volume_24h=50_000_000.0,
        trades_24h=100_000,
        bars_available=240,
    )
    base.update(over)
    return Features(**base)  # type: ignore[arg-type]


def signal(**over: object) -> Signal:
    base: dict[str, object] = dict(
        strategy="momentum_scalp",
        symbol="BTCUSD",
        side=Side.LONG,
        score=0.7,
        target_bps=40.0,
        stop_bps=20.0,
        ts_ms=1_700_000_000_000,
    )
    base.update(over)
    return Signal(**base)  # type: ignore[arg-type]


def book(bid: float, ask: float) -> OrderBook:
    return OrderBook(
        symbol="BTCUSD",
        bids=[BookLevel(bid, 10.0)],
        asks=[BookLevel(ask, 10.0)],
        last_update_ms=1_700_000_000_000,
    )


def position(**over: object) -> Position:
    base: dict[str, object] = dict(
        id="p1",
        symbol="BTCUSD",
        side=Side.LONG,
        strategy="momentum_scalp",
        qty=1.0,
        entry_price=100.0,
        entry_ms=1_700_000_000_000,
        notional_usd=100.0,
        risk_usd=0.2,
        take_profit=100.28,
        stop_loss=99.80,
        entry_fees_usd=0.1,
        entry_slippage_bps=1.0,
        target_bps=28.0,
        stop_bps=20.0,
    )
    base.update(over)
    return Position(**base)  # type: ignore[arg-type]


def engine(**exit_over: object) -> ExitEngine:
    settings = Settings()
    if exit_over:
        settings = dataclasses.replace(settings, exits=ExitConfig(**exit_over))  # type: ignore[arg-type]
    return ExitEngine(settings=settings)


def bps(entry: float, level: float) -> float:
    return abs(level - entry) / entry * 10_000.0


# --------------------------------------------------------------------------- #
# Initial levels
# --------------------------------------------------------------------------- #


def test_adaptive_tp_clears_the_round_trip_cost() -> None:
    eng = engine()
    cost = 22.0
    tp, sl = eng.initial_levels(
        Side.LONG, 100.0, features(), signal(), round_trip_cost_bps=cost, info=INFO
    )
    # 1.1 * 20 bps ATR = 22 bps, capped by the strategy target (40) = 22 bps,
    # then floored by cost + tp_min_bps_over_cost = 28 bps.
    assert tp == pytest.approx(100.28)
    assert bps(100.0, tp) >= cost + Settings().exits.tp_min_bps_over_cost
    assert sl == pytest.approx(99.80)


def test_tp_is_widened_when_costs_are_high() -> None:
    eng = engine()
    cheap_tp, _ = eng.initial_levels(
        Side.LONG, 100.0, features(), signal(), round_trip_cost_bps=2.0, info=INFO
    )
    pricey_tp, _ = eng.initial_levels(
        Side.LONG, 100.0, features(), signal(), round_trip_cost_bps=60.0, info=INFO
    )
    assert pricey_tp > cheap_tp
    assert bps(100.0, pricey_tp) == pytest.approx(66.0)


def test_adaptive_tp_respects_the_strategy_target_as_an_upper_bound() -> None:
    eng = engine()
    tp, _ = eng.initial_levels(
        Side.LONG,
        100.0,
        features(atr_bps=200.0),  # 1.1 * 200 = 220 bps of ATR-scaled target
        signal(target_bps=30.0),
        round_trip_cost_bps=4.0,
        info=INFO,
    )
    assert bps(100.0, tp) == pytest.approx(30.0)


def test_atr_tp_model() -> None:
    eng = engine(tp_model="atr", tp_atr_mult=2.0)
    tp, _ = eng.initial_levels(
        Side.LONG, 100.0, features(atr_bps=25.0), signal(), round_trip_cost_bps=4.0, info=INFO
    )
    assert bps(100.0, tp) == pytest.approx(50.0)


def test_fixed_pct_tp_model() -> None:
    eng = engine(tp_model="fixed_pct", tp_fixed_pct=0.5)
    tp, _ = eng.initial_levels(
        Side.LONG, 100.0, features(), signal(), round_trip_cost_bps=4.0, info=INFO
    )
    assert bps(100.0, tp) == pytest.approx(50.0)


def test_structure_stop_uses_the_swing_when_available() -> None:
    eng = engine(sl_model="structure")
    f = features()
    f.extras["swing_low"] = 99.50
    _, sl = eng.initial_levels(
        Side.LONG, 100.0, f, signal(), round_trip_cost_bps=4.0, info=INFO
    )
    assert sl == pytest.approx(99.50)


def test_structure_stop_falls_back_to_the_range_then_to_atr() -> None:
    eng = engine(sl_model="structure")
    # No swing in extras: distance down to the bottom of the range.
    _, sl = eng.initial_levels(
        Side.LONG,
        100.0,
        features(range_pos=0.5, bb_width_bps=80.0),
        signal(),
        round_trip_cost_bps=4.0,
        info=INFO,
    )
    assert bps(100.0, sl) == pytest.approx(40.0)

    # No range information either: ATR.
    _, sl_atr = eng.initial_levels(
        Side.LONG,
        100.0,
        features(range_pos=0.0, bb_width_bps=0.0, atr_bps=33.0),
        signal(),
        round_trip_cost_bps=4.0,
        info=INFO,
    )
    assert bps(100.0, sl_atr) == pytest.approx(33.0)


def test_emergency_stop_caps_a_wide_model_stop() -> None:
    eng = engine(sl_model="fixed_pct", sl_fixed_pct=5.0, emergency_stop_pct=2.5)
    _, sl = eng.initial_levels(
        Side.LONG, 100.0, features(), signal(), round_trip_cost_bps=4.0, info=INFO
    )
    assert sl == pytest.approx(97.50)  # 2.5%, not 5%


def test_levels_are_rounded_to_the_tick() -> None:
    coarse = dataclasses.replace(INFO, tick_size=0.5)
    eng = engine()
    tp, sl = eng.initial_levels(
        Side.LONG, 100.0, features(), signal(), round_trip_cost_bps=22.0, info=coarse
    )
    assert tp % 0.5 == pytest.approx(0.0)
    assert sl % 0.5 == pytest.approx(0.0)
    assert tp > 100.0 > sl


def test_short_levels_are_mirrored() -> None:
    eng = engine()
    tp, sl = eng.initial_levels(
        Side.SHORT, 100.0, features(), signal(side=Side.SHORT), round_trip_cost_bps=22.0, info=INFO
    )
    assert tp == pytest.approx(99.72)
    assert sl == pytest.approx(100.20)


def test_reward_risk_below_the_floor_is_visible_to_the_risk_engine() -> None:
    """A wide stop against a capped target is a bad trade — and it shows."""
    eng = engine()
    entry = 100.0
    tp, sl = eng.initial_levels(
        Side.LONG,
        entry,
        features(atr_bps=60.0),
        signal(target_bps=40.0),
        round_trip_cost_bps=4.0,
        info=INFO,
    )
    reward_risk = (tp - entry) / (entry - sl)
    assert reward_risk == pytest.approx(40.0 / 60.0, rel=1e-3)
    assert reward_risk < Settings().exits.min_reward_risk


def test_healthy_setup_clears_the_reward_risk_floor() -> None:
    eng = engine()
    entry = 100.0
    tp, sl = eng.initial_levels(
        Side.LONG, entry, features(), signal(), round_trip_cost_bps=22.0, info=INFO
    )
    assert (tp - entry) / (entry - sl) >= Settings().exits.min_reward_risk


# --------------------------------------------------------------------------- #
# check()
# --------------------------------------------------------------------------- #


def test_a_long_exits_on_the_bid_not_the_mid() -> None:
    """The mid is still above the stop; the bid is not.  The bid decides."""
    eng = engine()
    pos = position()
    b = book(bid=99.79, ask=100.30)
    assert b.mid > pos.stop_loss
    assert eng.check(pos, b, None, pos.entry_ms + 1_000) is ExitReason.STOP_LOSS
    assert eng.exit_price(pos, b) == pytest.approx(99.79)


def test_a_long_take_profit_needs_the_bid_through_the_target() -> None:
    eng = engine()
    pos = position()
    # Ask is through the TP but the bid is not: no fill would happen there.
    assert eng.check(pos, book(bid=100.27, ask=100.40), None, pos.entry_ms + 1_000) is None
    assert (
        eng.check(pos, book(bid=100.28, ask=100.40), None, pos.entry_ms + 1_000)
        is ExitReason.TAKE_PROFIT
    )


def test_stop_loss_outranks_take_profit_when_both_would_trigger() -> None:
    """Priority is fixed: the cheapest mistake to make is to take the loss."""
    eng = engine()
    # A gapped book can put price both through the stop and through the TP;
    # 100.40 is <= the (pathologically high) stop and >= the target.
    pos = position(stop_loss=100.50, take_profit=100.28)
    assert eng.check(pos, book(bid=100.40, ask=100.45), None, pos.entry_ms + 1) is ExitReason.STOP_LOSS


def test_take_profit_outranks_trailing_and_reversal() -> None:
    eng = engine()
    pos = position(trailing_armed=True, best_price=100.50)
    assert (
        eng.check(pos, book(100.28, 100.40), None, pos.entry_ms + 1, reversal=True)
        is ExitReason.TAKE_PROFIT
    )


def test_trailing_outranks_reversal_and_time_stop() -> None:
    eng = engine()
    pos = position(trailing_armed=True, best_price=100.24, take_profit=101.0)
    late = pos.entry_ms + 3_000_000  # well past the time stop
    assert (
        eng.check(pos, book(100.10, 100.20), None, late, reversal=True)
        is ExitReason.TRAILING_PROFIT
    )


def test_reversal_outranks_time_stop() -> None:
    eng = engine()
    pos = position(take_profit=101.0)
    late = pos.entry_ms + 3_000_000
    assert eng.check(pos, book(100.05, 100.10), None, late, reversal=True) is ExitReason.SIGNAL_REVERSAL
    assert eng.check(pos, book(100.05, 100.10), None, late) is ExitReason.TIME_STOP


def test_no_exit_when_nothing_is_hit() -> None:
    eng = engine()
    pos = position()
    assert eng.check(pos, book(100.05, 100.10), None, pos.entry_ms + 1_000) is None


def test_no_exit_invented_from_an_empty_book() -> None:
    eng = engine()
    pos = position()
    empty = OrderBook(symbol="BTCUSD", bids=[], asks=[], last_update_ms=1)
    assert eng.check(pos, empty, None, pos.entry_ms + 1_000) is None


# --------------------------------------------------------------------------- #
# Trailing
# --------------------------------------------------------------------------- #


def test_trailing_arms_at_the_configured_fraction_and_then_fires() -> None:
    eng = engine()
    pos = position(take_profit=101.0, stop_loss=99.0)  # 1.00 of TP distance
    eng.update_trailing(pos, 100.50)  # 50% of the way — below arm_frac 0.6
    assert pos.trailing_armed is False
    assert pos.best_price == pytest.approx(100.50)

    eng.update_trailing(pos, 100.80)  # 80% of the way — arms
    assert pos.trailing_armed is True
    assert pos.best_price == pytest.approx(100.80)

    # Give-back threshold is 0.35 * excursion (0.80) = 0.28 -> 100.52.
    assert eng.check(pos, book(100.55, 100.60), None, pos.entry_ms + 1_000) is None
    assert (
        eng.check(pos, book(100.52, 100.57), None, pos.entry_ms + 1_000)
        is ExitReason.TRAILING_PROFIT
    )


def test_trailing_tracks_only_the_best_excursion() -> None:
    eng = engine()
    pos = position(take_profit=101.0)
    for price in (100.9, 100.4, 100.7):
        eng.update_trailing(pos, price)
    assert pos.best_price == pytest.approx(100.9)


def test_trailing_disabled_never_arms_or_fires() -> None:
    eng = engine(trailing_enabled=False)
    pos = position(take_profit=101.0)
    eng.update_trailing(pos, 100.95)
    assert pos.trailing_armed is False
    pos.trailing_armed = True  # even if something else armed it
    pos.best_price = 100.95
    assert eng.check(pos, book(100.10, 100.15), None, pos.entry_ms + 1) is None


def test_check_does_not_mutate_the_position() -> None:
    """check() is a pure query; the worker calls update_trailing explicitly."""
    eng = engine()
    pos = position(take_profit=101.0)
    before = (pos.best_price, pos.trailing_armed)
    eng.check(pos, book(100.90, 100.95), None, pos.entry_ms + 1_000)
    assert (pos.best_price, pos.trailing_armed) == before


# --------------------------------------------------------------------------- #
# Time / staleness
# --------------------------------------------------------------------------- #


def test_time_stop_fires_past_the_configured_horizon() -> None:
    eng = engine()
    pos = position(take_profit=101.0)
    horizon_ms = Settings().exits.time_stop_seconds * 1_000
    assert eng.check(pos, book(100.20, 100.25), None, pos.entry_ms + horizon_ms - 1) is None
    assert (
        eng.check(pos, book(100.20, 100.25), None, pos.entry_ms + horizon_ms + 1)
        is ExitReason.TIME_STOP
    )


def test_position_time_stop_ms_overrides_the_default_horizon() -> None:
    eng = engine()
    pos = position(take_profit=101.0, time_stop_ms=position().entry_ms + 60_000)
    assert eng.check(pos, book(100.20, 100.25), None, pos.entry_ms + 59_000) is None
    assert eng.check(pos, book(100.20, 100.25), None, pos.entry_ms + 61_000) is ExitReason.TIME_STOP


def test_stale_exit_leaves_a_trade_going_nowhere_at_half_time() -> None:
    eng = engine()
    pos = position(take_profit=101.0, target_bps=100.0)
    half_ms = Settings().exits.time_stop_seconds * 1_000 // 2 + 1_000
    # 10 bps of progress against a 100 bps target = 0.10 < stale_exit_frac 0.25.
    assert eng.check(pos, book(100.10, 100.15), None, pos.entry_ms + half_ms) is ExitReason.TIME_STOP


def test_a_trade_making_progress_survives_half_time() -> None:
    eng = engine()
    pos = position(take_profit=101.0, target_bps=100.0)
    half_ms = Settings().exits.time_stop_seconds * 1_000 // 2 + 1_000
    # 40 bps of progress against a 100 bps target = 0.40 > 0.25.
    assert eng.check(pos, book(100.40, 100.45), None, pos.entry_ms + half_ms) is None


def test_stale_exit_target_falls_back_to_the_take_profit_distance() -> None:
    eng = engine()
    pos = position(take_profit=101.0, target_bps=0.0)
    half_ms = Settings().exits.time_stop_seconds * 1_000 // 2 + 1_000
    assert eng.check(pos, book(100.10, 100.15), None, pos.entry_ms + half_ms) is ExitReason.TIME_STOP
