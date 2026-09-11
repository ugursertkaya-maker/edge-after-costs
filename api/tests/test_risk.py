"""Risk engine: one test per veto, sizing worked by hand, every clamp bound.

The numbers below are chosen so they can be checked with a calculator:
equity $500, ``risk_per_trade_pct`` 0.5% -> a $2.50 risk budget per trade.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from app.config import ExitConfig, RiskConfig, Settings
from app.risk import RiskEngine, RiskState
from app.types import (
    BookLevel,
    Features,
    Opportunity,
    OrderBook,
    Position,
    Regime,
    RegimeState,
    RejectReason,
    Side,
    Signal,
    SymbolInfo,
)

TS = 1_700_000_000_000

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


def book(*, bid: float = 99.99, ask: float = 100.01, qty: float = 5.0,
         levels: int = 12, ts_ms: int = TS) -> OrderBook:
    return OrderBook(
        symbol="BTCUSD",
        bids=[BookLevel(round(bid - i * 0.01, 2), qty) for i in range(levels)],
        asks=[BookLevel(round(ask + i * 0.01, 2), qty) for i in range(levels)],
        last_update_ms=ts_ms,
    )


def features(**over: object) -> Features:
    base: dict[str, object] = dict(
        symbol="BTCUSD", ts_ms=TS, price=100.0, mid=100.0, microprice=100.0,
        spread_bps=2.0, book_age_ms=50, depth_bid_usd=6_000.0, depth_ask_usd=6_000.0,
        imbalance=0.0, atr_bps=200.0, realized_vol_bps=180.0, vol_percentile=0.5,
        ret_1m_bps=3.0, ret_5m_bps=6.0, ret_15m_bps=9.0, ema_fast=100.1, ema_slow=99.9,
        ema_slope_bps=1.0, trend_strength=0.6, range_pos=0.5, bb_width_bps=60.0,
        compression=0.3, volume_ratio=1.2, trade_flow_imbalance=0.2, trade_flow_accel=0.1,
        vwap=100.0, vwap_dev_bps=1.0, quote_volume_24h=50_000_000.0, trades_24h=100_000,
        bars_available=240,
    )
    base.update(over)
    return Features(**base)  # type: ignore[arg-type]


def opportunity(
    *,
    side: Side = Side.LONG,
    net_edge_bps: float = 8.0,
    slippage_bps: float = 2.0,
    regime: Regime = Regime.TRENDING,
    target_bps: float = 200.0,
    atr_bps: float = 200.0,
    symbol: str = "BTCUSD",
) -> Opportunity:
    sig = Signal(
        strategy="momentum_scalp",
        symbol=symbol,
        side=side,
        score=0.7,
        target_bps=target_bps,
        stop_bps=atr_bps,
        ts_ms=TS,
    )
    return Opportunity(
        signal=sig,
        features=features(atr_bps=atr_bps, symbol=symbol),
        regime=RegimeState(regime=regime, confidence=0.8),
        gross_edge_bps=net_edge_bps + 24.0,
        fee_bps=20.0,              # round trip, both legs
        spread_cost_bps=2.0,
        slippage_bps=slippage_bps,
        net_edge_bps=net_edge_bps,
        rank_score=1.0,
        expected_hold_seconds=600,
    )


def state(**over: object) -> RiskState:
    base: dict[str, object] = dict(
        equity=500.0,
        open_positions=[],
        realized_pnl_today=0.0,
        realized_pnl_week=0.0,
        loss_streak=0,
        cooldown_until_ms=0,
        kill_switch=False,
        data_healthy=True,
        ts_ms=TS,
    )
    base.update(over)
    return RiskState(**base)  # type: ignore[arg-type]


def open_position(
    pid: str, symbol: str, *, notional: float = 0.0, risk: float = 0.0
) -> Position:
    return Position(
        id=pid, symbol=symbol, side=Side.LONG, strategy="momentum_scalp",
        qty=notional / 100.0 if notional else 0.0, entry_price=100.0, entry_ms=TS,
        notional_usd=notional, risk_usd=risk, take_profit=102.0, stop_loss=98.0,
        entry_fees_usd=0.0, entry_slippage_bps=0.0,
    )


def engine(*, risk: dict | None = None, exits: dict | None = None) -> RiskEngine:
    settings = Settings()
    if risk:
        settings = dataclasses.replace(settings, risk=RiskConfig(**risk))
    if exits:
        settings = dataclasses.replace(settings, exits=ExitConfig(**exits))
    return RiskEngine(settings=settings)


def approve() -> tuple[RiskEngine, Opportunity, RiskState, OrderBook]:
    return engine(), opportunity(), state(), book()


# --------------------------------------------------------------------------- #
# The happy path, worked by hand
# --------------------------------------------------------------------------- #


def test_baseline_opportunity_is_approved_with_hand_checked_maths() -> None:
    eng, opp, st, bk = approve()
    v = eng.evaluate(opp, st, info=INFO, book=bk)
    assert v.approved, v.reject_reason

    # entry = best ask = 100.01; ATR 200 bps -> stop 98.0098 -> tick 98.01
    assert v.stop_loss == pytest.approx(98.01)
    # TP: min(1.1*200, target 200) = 200 bps, already over the 30 bps cost floor
    assert v.take_profit == pytest.approx(102.01)

    stop_distance = 100.01 - 98.01                      # 2.00
    slip_allowance = 100.01 * 2.0 / 10_000              # 0.020002
    expected_qty = 2.50 / (stop_distance + slip_allowance)
    assert expected_qty == pytest.approx(1.2376, abs=1e-4)
    assert v.qty == pytest.approx(INFO.floor_qty(expected_qty))
    assert v.notional_usd == pytest.approx(v.qty * 100.01)
    assert v.risk_usd == pytest.approx(2.50, abs=1e-4)


def test_position_size_is_not_account_risk() -> None:
    """$2.50 of risk buys a $124 position.  Conflating the two would be a 50x error."""
    eng, opp, st, bk = approve()
    v = eng.evaluate(opp, st, info=INFO, book=bk)
    assert v.risk_usd == pytest.approx(2.50, abs=1e-4)
    assert v.notional_usd > 100.0
    assert v.notional_usd / v.risk_usd > 40


def test_a_tighter_stop_buys_a_bigger_position_for_the_same_risk() -> None:
    eng = engine()
    wide = eng.evaluate(opportunity(atr_bps=200.0), state(), info=INFO, book=book())
    tight = eng.evaluate(
        opportunity(atr_bps=40.0, target_bps=60.0), state(), info=INFO, book=book()
    )
    assert tight.qty > wide.qty
    assert tight.risk_usd <= wide.risk_usd + 1e-6  # same budget, never more


def test_risk_scales_linearly_with_equity() -> None:
    eng = engine()
    small = eng.evaluate(opportunity(), state(equity=500.0), info=INFO, book=book())
    big = eng.evaluate(opportunity(), state(equity=1_000.0), info=INFO, book=book())
    assert big.risk_usd == pytest.approx(2 * small.risk_usd, rel=1e-6)


# --------------------------------------------------------------------------- #
# No martingale, ever
# --------------------------------------------------------------------------- #


def test_two_consecutive_losses_never_increase_the_next_size() -> None:
    eng = engine()
    sizes = [
        eng.evaluate(opportunity(), state(loss_streak=streak), info=INFO, book=book()).qty
        for streak in (0, 1, 2, 3)
    ]
    assert len(set(round(q, 10) for q in sizes)) == 1  # streak changes nothing

    # Losses shrink equity, and a smaller account risks fewer dollars.  Only ever down.
    after_losses = eng.evaluate(
        opportunity(), state(equity=480.0, loss_streak=2, realized_pnl_today=-20.0),
        info=INFO, book=book(),
    )
    fresh = eng.evaluate(opportunity(), state(), info=INFO, book=book())
    assert after_losses.qty < fresh.qty
    assert after_losses.risk_usd < fresh.risk_usd


def test_sizing_code_cannot_see_past_outcomes() -> None:
    """Structural proof: the sizing path never reads streaks or realised P&L."""
    for fn in (RiskEngine.evaluate, RiskEngine._apply_clamps):
        source = inspect.getsource(fn)
        sizing = source.split("# ---- sizing")[-1] if "# ---- sizing" in source else source
        assert "loss_streak" not in sizing
        assert "realized_pnl" not in sizing
    module = inspect.getsource(RiskEngine).lower()
    for forbidden in ("martingale", "double_down", "average_down", "add_to_position"):
        assert forbidden not in module


def test_a_symbol_already_open_can_never_be_added_to() -> None:
    """No averaging down: one position per symbol, full stop."""
    eng = engine()
    st = state(open_positions=[open_position("a", "BTCUSD", notional=50.0)])
    v = eng.evaluate(opportunity(), st, info=INFO, book=book())
    assert v.reject_reason is RejectReason.DUPLICATE_SYMBOL


# --------------------------------------------------------------------------- #
# One test per RejectReason
# --------------------------------------------------------------------------- #


def test_short_is_not_supported() -> None:
    eng = engine()
    v = eng.evaluate(opportunity(side=Side.SHORT), state(), info=INFO, book=book())
    assert v.reject_reason is RejectReason.NOT_SUPPORTED_SHORT
    assert v.qty == 0.0


def test_kill_switch() -> None:
    eng = engine()
    assert (
        eng.evaluate(opportunity(), state(kill_switch=True), info=INFO, book=book()).reject_reason
        is RejectReason.KILL_SWITCH
    )


def test_data_failure() -> None:
    eng = engine()
    assert (
        eng.evaluate(opportunity(), state(data_healthy=False), info=INFO, book=book()).reject_reason
        is RejectReason.DATA_FAILURE
    )


def test_loss_streak_cooldown() -> None:
    eng = engine()
    st = state(loss_streak=4, cooldown_until_ms=TS + 60_000)
    assert (
        eng.evaluate(opportunity(), st, info=INFO, book=book()).reject_reason
        is RejectReason.LOSS_STREAK_COOLDOWN
    )
    # ... and trading resumes by itself once the clock passes.
    resumed = state(loss_streak=4, cooldown_until_ms=TS - 1)
    assert eng.evaluate(opportunity(), resumed, info=INFO, book=book()).approved


def test_daily_loss_limit() -> None:
    eng = engine()
    # 3% of $500 = $15.
    assert eng.evaluate(opportunity(), state(realized_pnl_today=-14.99), info=INFO, book=book()).approved
    assert (
        eng.evaluate(opportunity(), state(realized_pnl_today=-15.0), info=INFO, book=book()).reject_reason
        is RejectReason.DAILY_LOSS_LIMIT
    )


def test_weekly_loss_limit() -> None:
    eng = engine()
    # 7% of $500 = $35; keep today inside its own limit so the weekly one is what bites.
    st = state(realized_pnl_today=-5.0, realized_pnl_week=-35.0)
    assert eng.evaluate(opportunity(), st, info=INFO, book=book()).reject_reason is RejectReason.WEEKLY_LOSS_LIMIT


def test_max_positions() -> None:
    eng = engine()
    st = state(
        open_positions=[
            open_position("a", "ETHUSD"), open_position("b", "SOLUSD"), open_position("c", "ADAUSD")
        ]
    )
    assert eng.evaluate(opportunity(), st, info=INFO, book=book()).reject_reason is RejectReason.MAX_POSITIONS


def test_duplicate_symbol() -> None:
    eng = engine()
    st = state(open_positions=[open_position("a", "BTCUSD")])
    assert eng.evaluate(opportunity(), st, info=INFO, book=book()).reject_reason is RejectReason.DUPLICATE_SYMBOL


def test_stale_book() -> None:
    eng = engine()
    stale = book(ts_ms=TS - 5_000)  # older than max_book_age_ms (2500)
    assert eng.evaluate(opportunity(), state(), info=INFO, book=stale).reject_reason is RejectReason.STALE_BOOK


def test_invalid_and_desynced_books_are_stale() -> None:
    eng = engine()
    empty = OrderBook(symbol="BTCUSD", bids=[], asks=[], last_update_ms=TS)
    assert eng.evaluate(opportunity(), state(), info=INFO, book=empty).reject_reason is RejectReason.STALE_BOOK
    desynced = book()
    desynced.desynced = True
    assert eng.evaluate(opportunity(), state(), info=INFO, book=desynced).reject_reason is RejectReason.STALE_BOOK


def test_wide_spread() -> None:
    eng = engine()
    wide = book(bid=99.95, ask=100.05)  # 10 bps > max_entry_spread_bps 6
    assert eng.evaluate(opportunity(), state(), info=INFO, book=wide).reject_reason is RejectReason.WIDE_SPREAD


def test_thin_depth() -> None:
    eng = engine()
    thin = book(qty=0.5, levels=3)  # ~$150 a side
    assert eng.evaluate(opportunity(), state(), info=INFO, book=thin).reject_reason is RejectReason.THIN_DEPTH


@pytest.mark.parametrize("edge", [0.0, -3.0])
def test_negative_edge(edge: float) -> None:
    eng = engine()
    assert (
        eng.evaluate(opportunity(net_edge_bps=edge), state(), info=INFO, book=book()).reject_reason
        is RejectReason.NEGATIVE_EDGE
    )


def test_edge_below_min_bps() -> None:
    """Pinned gate: the live default is venue-calibrated and moves."""
    eng = engine(risk={"min_net_edge_bps": 4.0})
    assert (
        eng.evaluate(opportunity(net_edge_bps=3.9), state(), info=INFO, book=book()).reject_reason
        is RejectReason.EDGE_BELOW_MIN
    )
    assert eng.evaluate(
        opportunity(net_edge_bps=4.1), state(), info=INFO, book=book()
    ).reject_reason is not RejectReason.EDGE_BELOW_MIN


def test_edge_below_min_in_dollars() -> None:
    """4.5 bps clears the bps gate but earns under a cent on a $15 position."""
    eng = engine()
    v = eng.evaluate(opportunity(net_edge_bps=4.5), state(equity=60.0), info=INFO, book=book())
    assert v.reject_reason is RejectReason.EDGE_BELOW_MIN
    assert any("expected_edge_usd" in n for n in v.notes)


def test_bad_reward_risk() -> None:
    eng = engine()
    # ATR 60 bps stop against a 40 bps target = 0.67 R:R, under the 0.9 floor.
    v = eng.evaluate(
        opportunity(atr_bps=60.0, target_bps=40.0), state(), info=INFO, book=book()
    )
    assert v.reject_reason is RejectReason.BAD_REWARD_RISK
    assert any("reward_risk" in n for n in v.notes)


def test_slippage_too_high() -> None:
    eng = engine()
    assert (
        eng.evaluate(opportunity(slippage_bps=15.0), state(), info=INFO, book=book()).reject_reason
        is RejectReason.SLIPPAGE_TOO_HIGH
    )


def test_slippage_too_high_for_the_size_the_book_would_have_to_absorb() -> None:
    """Pre-checked with the simulator's own walk, at the size we would send."""
    eng = engine(risk={"max_slippage_bps": 3.0, "min_book_depth_usd": 100.0})
    shallow = book(qty=0.25, levels=12)  # ~$25 a level: a $124 order walks five
    v = eng.evaluate(opportunity(), state(), info=INFO, book=shallow)
    assert v.reject_reason is RejectReason.SLIPPAGE_TOO_HIGH
    assert any("sized_slippage_bps" in n for n in v.notes)


def test_abnormal_volatility() -> None:
    eng = engine()
    assert (
        eng.evaluate(opportunity(regime=Regime.ABNORMAL), state(), info=INFO, book=book()).reject_reason
        is RejectReason.ABNORMAL_VOLATILITY
    )


def test_below_min_notional_from_the_exchange_filter() -> None:
    eng = engine()
    info = dataclasses.replace(INFO, min_notional=500.0)
    v = eng.evaluate(opportunity(), state(), info=info, book=book())
    assert v.reject_reason is RejectReason.BELOW_MIN_NOTIONAL


def test_below_min_notional_from_our_own_settings() -> None:
    eng = engine(risk={"min_notional_usd": 200.0})
    v = eng.evaluate(opportunity(), state(), info=INFO, book=book())
    assert v.reject_reason is RejectReason.BELOW_MIN_NOTIONAL
    assert any("min_notional" in n for n in v.notes)


def test_below_min_qty_after_lot_rounding() -> None:
    eng = engine()
    info = dataclasses.replace(INFO, step_size=1.0, min_qty=2.0)  # qty 1.24 -> 1.0 -> under min
    v = eng.evaluate(opportunity(), state(), info=info, book=book())
    assert v.reject_reason is RejectReason.BELOW_MIN_NOTIONAL
    assert "clamp:lot_size" in v.notes


def test_risk_budget_exhausted() -> None:
    eng = engine()
    st = state(open_positions=[open_position("a", "ETHUSD", notional=10.0, risk=7.5)])
    v = eng.evaluate(opportunity(), st, info=INFO, book=book())
    assert v.reject_reason is RejectReason.RISK_BUDGET
    assert any("exhausted" in n for n in v.notes)


def test_zero_equity_is_rejected() -> None:
    eng = engine()
    v = eng.evaluate(opportunity(), state(equity=0.0), info=INFO, book=book())
    assert v.reject_reason in (RejectReason.RISK_BUDGET, RejectReason.BELOW_MIN_NOTIONAL)


# --------------------------------------------------------------------------- #
# Clamps: each one demonstrated to bind, and recorded
# --------------------------------------------------------------------------- #


def test_clamp_max_position_pct_equity_binds() -> None:
    eng = engine()
    # A 20 bps stop would buy ~11 units ($1,136) on a $500 account.
    v = eng.evaluate(opportunity(atr_bps=20.0, target_bps=60.0), state(), info=INFO, book=book())
    assert v.approved
    assert "clamp:max_position_pct_equity" in v.notes
    assert v.notional_usd == pytest.approx(175.0, abs=0.05)  # 35% of $500
    assert v.risk_usd < 2.50  # clamped down: we are risking *less* than budget


def test_clamp_max_total_exposure_pct_binds() -> None:
    eng = engine()
    st = state(open_positions=[open_position("a", "ETHUSD", notional=400.0, risk=0.0)])
    v = eng.evaluate(
        opportunity(atr_bps=20.0, target_bps=60.0, net_edge_bps=40.0), st, info=INFO, book=book()
    )
    assert v.approved
    assert "clamp:max_total_exposure_pct" in v.notes
    # 90% of $500 = $450 total, $400 already on -> $50 of room.
    assert v.notional_usd == pytest.approx(50.0, abs=0.05)


def test_clamp_max_concurrent_risk_pct_binds() -> None:
    eng = engine()
    st = state(open_positions=[open_position("a", "ETHUSD", notional=50.0, risk=7.0)])
    v = eng.evaluate(opportunity(net_edge_bps=40.0), st, info=INFO, book=book())
    assert v.approved
    assert "clamp:max_concurrent_risk_pct" in v.notes
    # 1.5% of $500 = $7.50 of concurrent risk, $7.00 already at stake.
    assert v.risk_usd == pytest.approx(0.50, abs=0.01)


def test_clamp_available_cash_binds() -> None:
    eng = engine(risk={"max_total_exposure_pct": 100.0})
    st = state(open_positions=[open_position("a", "ETHUSD", notional=440.0, risk=0.0)])
    v = eng.evaluate(
        opportunity(atr_bps=20.0, target_bps=60.0, net_edge_bps=40.0), st, info=INFO, book=book()
    )
    assert v.approved
    assert "clamp:available_cash" in v.notes
    # $60 of cash, minus headroom for the taker fee — which rate that is
    # depends on the configured fee model, so derive it instead of baking in
    # a number that silently breaks when the default tier changes.
    taker = Settings().fees.taker_bps(INFO.symbol)
    assert v.notional_usd <= 60.0
    assert v.notional_usd == pytest.approx(60.0 / (1 + taker / 10_000), abs=0.05)


def test_clamp_lot_size_binds() -> None:
    eng = engine()
    info = dataclasses.replace(INFO, step_size=0.1, min_qty=0.1)
    v = eng.evaluate(opportunity(), state(), info=info, book=book())
    assert v.approved
    assert v.qty == pytest.approx(1.2)  # 1.2376 floored to the step
    assert v.notional_usd == pytest.approx(1.2 * 100.01)


def test_an_unclamped_trade_records_no_clamp() -> None:
    eng, opp, st, bk = approve()
    v = eng.evaluate(opp, st, info=INFO, book=bk)
    assert [n for n in v.notes if n.startswith("clamp:")] == []


def test_notes_explain_the_size() -> None:
    eng, opp, st, bk = approve()
    v = eng.evaluate(opp, st, info=INFO, book=bk)
    joined = " ".join(v.notes)
    assert "risk_budget_usd" in joined and "stop_distance" in joined and "notional_usd" in joined


# --------------------------------------------------------------------------- #
# blocked_reason
# --------------------------------------------------------------------------- #


def test_blocked_reason_is_none_for_a_healthy_account() -> None:
    assert engine().blocked_reason(state()) is None


@pytest.mark.parametrize(
    "over,expected",
    [
        ({"kill_switch": True}, RejectReason.KILL_SWITCH),
        ({"data_healthy": False}, RejectReason.DATA_FAILURE),
        ({"cooldown_until_ms": TS + 1}, RejectReason.LOSS_STREAK_COOLDOWN),
        ({"realized_pnl_today": -15.0}, RejectReason.DAILY_LOSS_LIMIT),
        ({"realized_pnl_week": -35.0}, RejectReason.WEEKLY_LOSS_LIMIT),
    ],
)
def test_blocked_reason_reports_each_account_level_block(over: dict, expected: RejectReason) -> None:
    eng = engine()
    st = state(**over)
    assert eng.blocked_reason(st) is expected
    # The worker can trust it: evaluate() agrees without being asked twice.
    assert eng.evaluate(opportunity(), st, info=INFO, book=book()).reject_reason is expected


def test_blocked_reason_ignores_position_level_limits() -> None:
    """Full book of positions is not an account block — other symbols may still exit."""
    st = state(open_positions=[open_position(str(i), f"S{i}USD") for i in range(3)])
    assert engine().blocked_reason(st) is None
