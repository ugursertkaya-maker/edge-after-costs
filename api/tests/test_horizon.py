"""The 25/60 minute horizon: a checkpoint, never a lock.

Every test here is about *when* a position is allowed to still exist.  The two
failure modes being designed against are opposite and equally expensive: giving
back a profit while waiting for minute 25, and nursing a loser past minute 60
because realising it would make the number real.

Since Phase 6 the horizon is the *fallback* clock — a strategy with a holding
profile brings its own, and `momentum_scalp`'s is 40 minutes, not 60.  So the
positions here carry a strategy that has no profile, which is what leaves the
horizon in charge and keeps these tests about the horizon.  The interaction
between the two clocks is tested in ``test_holding.py``.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.config import HorizonConfig, ProfitConfig, Settings
from app.exits import CheckpointVerdict, ExitEngine
from app.profit import NetTargetEngine
from app.types import BookLevel, ExitReason, NetTargets, OrderBook, Position, Side
from fixtures.market import BASE_MS, make_book, make_features, make_info, make_signal

SYMBOL = "BTCUSD"
INFO = make_info(SYMBOL)
ENTRY = 100.0
CHECKPOINT_S = Settings().horizon.checkpoint_seconds      # 1500
TIME_STOP_S = Settings().horizon.time_stop_seconds        # 3600
EXTENSION_S = Settings().horizon.extension_seconds        # 1800


def book(bid: float, ask: float | None = None) -> OrderBook:
    return OrderBook(
        symbol=SYMBOL,
        bids=[BookLevel(bid, 10.0)],
        asks=[BookLevel(ask if ask is not None else bid + 0.05, 10.0)],
        last_update_ms=BASE_MS,
    )


def at(seconds: float) -> int:
    return BASE_MS + int(seconds * 1000)


def targets(
    *,
    profile: str = "base",
    slippage_bps: float = 0.0,
    settings: Settings | None = None,
) -> NetTargets:
    s = settings or Settings()
    return NetTargetEngine(settings=s).build(
        symbol=SYMBOL,
        side=Side.LONG,
        entry_price=ENTRY,
        notional_usd=100.0,
        qty=1.0,
        book=make_book(mid=ENTRY, spread_bps=2.0),
        info=INFO,
        features=make_features(atr_bps=80.0),
        signal=make_signal(target_bps=90.0, stop_bps=20.0),
        profile=profile,
        slippage_bps_entry=slippage_bps,
        slippage_bps_exit=slippage_bps,
    )


def position(**over: object) -> Position:
    base: dict[str, object] = dict(
        id="p2",
        symbol=SYMBOL,
        side=Side.LONG,
        strategy="horizon_only",   # no holding profile: the horizon decides
        qty=1.0,
        entry_price=ENTRY,
        entry_ms=BASE_MS,
        notional_usd=100.0,
        risk_usd=0.2,
        take_profit=100.28,
        stop_loss=99.80,
        entry_fees_usd=0.01,
        entry_slippage_bps=1.0,
        target_bps=28.0,
        stop_bps=20.0,
        targets=targets(),
        checkpoint_ms=at(CHECKPOINT_S),
    )
    base.update(over)
    pos = Position(**base)  # type: ignore[arg-type]
    if pos.targets is not None:
        pos.break_even_price = pos.targets.break_even_price
    return pos


def engine(**horizon_over: object) -> ExitEngine:
    s = Settings()
    if horizon_over:
        s = dataclasses.replace(s, horizon=HorizonConfig(**horizon_over))  # type: ignore[arg-type]
    return ExitEngine(settings=s)


RISING = make_features(ema_slope_bps=3.0, trade_flow_imbalance=0.4)
FADING = make_features(ema_slope_bps=-2.0, trade_flow_imbalance=-0.1)


# --------------------------------------------------------------------------- #
# Every verdict is reachable
# --------------------------------------------------------------------------- #


def test_the_checkpoint_is_silent_before_it_is_due() -> None:
    """25 minutes is when we review, not when we start caring."""
    eng = engine()
    pos = position()
    assert eng.checkpoint(pos, book(100.02), FADING, at(CHECKPOINT_S - 1)) is CheckpointVerdict.KEEP


def test_verdict_take_profit_when_the_net_target_is_there() -> None:
    eng = engine()
    pos = position()
    price = pos.targets.net_tp_price + 0.01
    assert (
        eng.checkpoint(pos, book(price), RISING, at(CHECKPOINT_S))
        is CheckpointVerdict.TAKE_PROFIT
    )


def test_verdict_trail_profit_when_an_open_profit_loses_its_momentum() -> None:
    eng = engine()
    pos = position()
    assert (
        eng.checkpoint(pos, book(100.30), FADING, at(CHECKPOINT_S))
        is CheckpointVerdict.TRAIL_PROFIT
    )


def test_verdict_keep_when_the_trade_is_still_doing_its_job() -> None:
    eng = engine()
    pos = position()
    assert (
        eng.checkpoint(pos, book(100.30), RISING, at(CHECKPOINT_S))
        is CheckpointVerdict.KEEP
    )


def test_verdict_exit_weak_when_it_is_going_nowhere_and_nothing_better_exists() -> None:
    eng = engine()
    pos = position()
    assert (
        eng.checkpoint(pos, book(100.02), RISING, at(CHECKPOINT_S))
        is CheckpointVerdict.EXIT_WEAK
    )


def test_verdict_rotate_capital_when_a_materially_better_idea_wants_the_cash() -> None:
    eng = engine()
    pos = position()
    remaining = pos.targets.gross_move_required_bps - 2.0
    ratio = Settings().horizon.rotate_if_edge_ratio
    assert (
        eng.checkpoint(
            pos, book(100.02), RISING, at(CHECKPOINT_S),
            best_alternative_edge_bps=ratio * remaining + 1.0,
        )
        is CheckpointVerdict.ROTATE_CAPITAL
    )


def test_a_merely_better_alternative_does_not_justify_rotating() -> None:
    """Rotation costs a round trip; it has to be worth more than a rounding
    error of extra edge."""
    eng = engine()
    pos = position()
    remaining = pos.targets.gross_move_required_bps - 2.0
    assert (
        eng.checkpoint(
            pos, book(100.02), RISING, at(CHECKPOINT_S),
            best_alternative_edge_bps=remaining * 1.1,
        )
        is CheckpointVerdict.EXIT_WEAK
    )


def test_a_position_making_progress_is_not_rotated_away_from() -> None:
    eng = engine()
    pos = position()
    assert (
        eng.checkpoint(
            pos, book(100.30), RISING, at(CHECKPOINT_S),
            best_alternative_edge_bps=10_000.0,
        )
        is CheckpointVerdict.KEEP
    )


def test_no_executable_price_produces_no_verdict_worth_acting_on() -> None:
    eng = engine()
    pos = position()
    empty = OrderBook(symbol=SYMBOL)
    assert eng.checkpoint(pos, empty, RISING, at(CHECKPOINT_S)) is CheckpointVerdict.KEEP


# --------------------------------------------------------------------------- #
# check() surfaces the verdicts as exit reasons
# --------------------------------------------------------------------------- #


def test_check_exits_weak_at_the_checkpoint() -> None:
    eng = engine()
    pos = position()
    assert eng.check(pos, book(100.02), RISING, at(CHECKPOINT_S)) is ExitReason.EXIT_WEAK


def test_check_rotates_capital_at_the_checkpoint() -> None:
    eng = engine()
    pos = position()
    remaining = pos.targets.gross_move_required_bps - 2.0
    assert (
        eng.check(
            pos, book(100.02), RISING, at(CHECKPOINT_S),
            best_alternative_edge_bps=Settings().horizon.rotate_if_edge_ratio * remaining + 1.0,
        )
        is ExitReason.ROTATE_CAPITAL
    )


def test_check_holds_on_keep_and_on_trail_profit() -> None:
    eng = engine()
    assert eng.check(position(), book(100.30), RISING, at(CHECKPOINT_S)) is None
    assert eng.check(position(), book(100.30), FADING, at(CHECKPOINT_S)) is None


def test_a_v1_position_never_reaches_the_checkpoint() -> None:
    """No targets, no checkpoint_ms: the V1 clock, exactly as before."""
    eng = engine()
    pos = position(targets=None, checkpoint_ms=0, take_profit=101.0)
    reason = eng.check(pos, book(100.02), RISING, at(CHECKPOINT_S))
    assert reason is not ExitReason.EXIT_WEAK
    assert reason is not ExitReason.ROTATE_CAPITAL


# --------------------------------------------------------------------------- #
# Early profit, early loss
# --------------------------------------------------------------------------- #


def test_a_stop_at_minute_two_beats_everything_in_a_25_minute_window() -> None:
    """A stop is honoured when it is hit, not when the review is scheduled."""
    eng = engine()
    pos = position(trailing_armed=True, best_price=100.40)
    assert eng.check(pos, book(99.70), RISING, at(120)) is ExitReason.STOP_LOSS
    # ...and it still wins at the checkpoint and at the time stop.
    assert eng.check(pos, book(99.70), RISING, at(CHECKPOINT_S)) is ExitReason.STOP_LOSS
    assert eng.check(pos, book(99.70), RISING, at(TIME_STOP_S + 1)) is ExitReason.STOP_LOSS


def test_profit_reached_at_minute_eight_is_taken_at_minute_eight() -> None:
    """NET TP hit with momentum fading: take it.  Do not wait for minute 25 and
    give it back."""
    eng = engine()
    pos = position()
    ts = at(480)
    assert pos.targets.net_tp_price > 100.0
    assert eng.checkpoint(pos, book(pos.targets.net_tp_price), FADING, ts) is CheckpointVerdict.KEEP
    assert eng.check(pos, book(pos.targets.net_tp_price), FADING, ts) is ExitReason.TAKE_PROFIT


def test_a_v2_take_profit_is_measured_against_the_net_price_not_the_gross_one() -> None:
    eng = engine()
    pos = position()
    assert pos.targets.net_tp_price > pos.take_profit
    # Through the old gross target, short of the NET one: not a profit yet.
    assert eng.check(pos, book(100.30), RISING, at(300)) is None
    assert eng.take_profit_price(pos) == pytest.approx(pos.targets.net_tp_price)


# --------------------------------------------------------------------------- #
# The 60-minute stop and the one way past it
# --------------------------------------------------------------------------- #


def test_the_time_stop_releases_the_capital_at_sixty_minutes() -> None:
    eng = engine()
    pos = position()
    assert eng.check(pos, book(100.30), RISING, at(TIME_STOP_S - 1)) is None
    assert eng.check(pos, book(100.30), RISING, at(TIME_STOP_S + 1)) is ExitReason.TIME_STOP
    assert pos.extended is False


def test_a_protected_winner_may_extend_once() -> None:
    eng = engine()
    pos = position(trailing_armed=True, best_price=100.32)
    ts = at(TIME_STOP_S + 1)

    assert eng.check(pos, book(100.30), RISING, ts, signal_strength=0.8) is None
    assert pos.extended is True

    # Still inside the extension.
    assert eng.check(pos, book(100.30), RISING, at(TIME_STOP_S + EXTENSION_S - 10),
                     signal_strength=0.9) is None
    # And the extension is granted once, not on a loop.
    assert (
        eng.check(pos, book(100.30), RISING, at(TIME_STOP_S + EXTENSION_S + 1),
                  signal_strength=0.9)
        is ExitReason.TIME_STOP
    )


def test_a_loser_is_never_extended_even_when_it_looks_green() -> None:
    """+20 bps gross on a position whose round trip costs 35 bps is a *loss*.

    Everything else about this position argues for an extension — the trail is
    armed, the signal is screaming, progress looks fine — and it is refused,
    because the only number that counts is net.
    """
    s = dataclasses.replace(Settings(), profit=ProfitConfig(latency_buffer_bps=1.0))
    eng = ExitEngine(settings=s)
    expensive = targets(profile="micro", slippage_bps=15.0, settings=s)
    pos = position(targets=expensive, trailing_armed=True, best_price=100.22)

    price = 100.20
    assert pos.unrealized_usd(price) > 0                      # green on the screen
    assert pos.unrealized_usd(price) - expensive.costs.total_usd < 0   # red in the bank

    assert eng.checkpoint(pos, book(price), RISING, at(TIME_STOP_S + 1)) is CheckpointVerdict.KEEP
    assert (
        eng.check(pos, book(price), RISING, at(TIME_STOP_S + 1), signal_strength=1.0)
        is ExitReason.TIME_STOP
    )
    assert pos.extended is False


def test_a_losing_winner_candidate_without_a_trail_is_not_extended() -> None:
    """Profit must already be *protected*, not merely present."""
    eng = engine()
    pos = position(trailing_armed=False)
    assert (
        eng.check(pos, book(100.30), RISING, at(TIME_STOP_S + 1), signal_strength=1.0)
        is ExitReason.TIME_STOP
    )
    assert pos.extended is False


def test_a_weak_signal_does_not_buy_more_time() -> None:
    eng = engine()
    pos = position(trailing_armed=True, best_price=100.32)
    assert (
        eng.check(pos, book(100.30), RISING, at(TIME_STOP_S + 1), signal_strength=0.2)
        is ExitReason.TIME_STOP
    )
    assert pos.extended is False


def test_extension_can_be_switched_off_entirely() -> None:
    eng = engine(allow_extension=False)
    pos = position(trailing_armed=True, best_price=100.32)
    assert (
        eng.check(pos, book(100.30), RISING, at(TIME_STOP_S + 1), signal_strength=1.0)
        is ExitReason.TIME_STOP
    )
    assert pos.extended is False


def test_an_explicit_position_deadline_is_a_ceiling_not_a_licence() -> None:
    """A position opened with a shorter clock keeps the shorter clock."""
    eng = engine()
    pos = position(time_stop_ms=at(1_800))
    assert eng.check(pos, book(100.30), RISING, at(1_700)) is None
    assert eng.check(pos, book(100.30), RISING, at(1_900)) is ExitReason.TIME_STOP


# --------------------------------------------------------------------------- #
# net_levels
# --------------------------------------------------------------------------- #


def test_net_levels_delegates_and_defaults_the_stop_to_the_v1_model() -> None:
    eng = engine()
    f = make_features(atr_bps=40.0)
    sig = make_signal(target_bps=90.0, stop_bps=20.0)
    t = eng.net_levels(
        Side.LONG, ENTRY, f, sig,
        symbol=SYMBOL, notional_usd=100.0, qty=1.0,
        book=make_book(mid=ENTRY, spread_bps=2.0), info=INFO,
    )
    _, v1_stop = eng.initial_levels(
        Side.LONG, ENTRY, f, sig, round_trip_cost_bps=0.0, info=INFO
    )
    assert t.stop_price == pytest.approx(v1_stop)
    assert t.net_tp_price > t.break_even_price > ENTRY
    assert t.gross_move_required_bps > t.net_tp_pct * 100.0
