"""Strategy-specific position management (Phase 6 §7-§10).

The two mistakes this file exists to catch are opposite, and both of them are
cheap to make:

* timing a `flow_impulse` out at a fixed number of seconds, or leaving it in for
  25 minutes after the impulse it was built on has gone;
* churning a `momentum_scalp` on ten seconds of feature noise, or closing it
  because its *entry* score slipped — a question nobody asked.

So the tests below are mostly pairs: the thing that must happen, next to the
superficially similar thing that must not.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.config import HoldingConfig, Settings
from app.exits import ExitEngine
from app.holding import (
    DEFAULT_PROFILE,
    PROFILES,
    HoldingPolicy,
    arm,
    decay_spent,
    decay_tolerance,
    profile_for,
    signal_decay,
    state_of,
    trigger_value,
)
from app.types import BookLevel, ExitReason, OrderBook, Position, Side
from fixtures.market import BASE_MS, make_features

SYMBOL = "BTCUSD"
ENTRY = 100.0
CHECKPOINT_S = Settings().horizon.checkpoint_seconds      # 1500
TIME_STOP_S = Settings().horizon.time_stop_seconds        # 3600


def at(seconds: float) -> int:
    return BASE_MS + int(seconds * 1000)


def book(bid: float = 100.05, ask: float | None = None, qty: float = 200.0) -> OrderBook:
    return OrderBook(
        symbol=SYMBOL,
        bids=[BookLevel(bid, qty)],
        asks=[BookLevel(ask if ask is not None else bid + 0.02, qty)],
        last_update_ms=BASE_MS,
    )


def position(strategy: str = "flow_impulse", **over: object) -> Position:
    """A V2-shaped position: it carries a checkpoint, so it has a horizon.

    Levels are deliberately far away — every test here is about the reasons to
    leave that are *not* a price level.
    """
    base: dict[str, object] = dict(
        id="h1",
        symbol=SYMBOL,
        side=Side.LONG,
        strategy=strategy,
        qty=1.0,
        entry_price=ENTRY,
        entry_ms=BASE_MS,
        notional_usd=100.0,
        risk_usd=0.2,
        take_profit=101.0,
        stop_loss=99.0,
        entry_fees_usd=0.01,
        entry_slippage_bps=1.0,
        target_bps=100.0,
        stop_bps=100.0,
        checkpoint_ms=at(CHECKPOINT_S),
        time_stop_ms=at(TIME_STOP_S),
    )
    base.update(over)
    return Position(**base)  # type: ignore[arg-type]


def engine(**holding_over: object) -> ExitEngine:
    s = Settings()
    if holding_over:
        s = dataclasses.replace(s, holding=HoldingConfig(**holding_over))  # type: ignore[arg-type]
    return ExitEngine(settings=s)


#: The impulse that opened a `flow_impulse` position, and the same market with
#: the impulse spent.  Only the trigger feature differs.
IMPULSE = make_features(trade_flow_imbalance=0.60, trade_flow_accel=0.30)
SPENT = make_features(trade_flow_imbalance=0.05, trade_flow_accel=0.0)
FLIPPED = make_features(trade_flow_imbalance=-0.40, trade_flow_accel=-0.20)


def armed(strategy: str = "flow_impulse", features=IMPULSE, **over: object) -> Position:
    pos = position(strategy, **over)
    arm(pos, features, book=book(), entry_score=0.8, ts_ms=BASE_MS)
    return pos


# --------------------------------------------------------------------------- #
# The profile table is a contract
# --------------------------------------------------------------------------- #


# (strategy, min, typical, max, decay_exit, halflife, reeval_entry_score)
SPEC_TABLE = [
    ("flow_impulse", 0.0, 30.0, 180.0, True, 20.0, True),
    ("book_imbalance", 0.0, 120.0, 600.0, True, 90.0, True),
    ("momentum_scalp", 60.0, 600.0, 2_400.0, False, 0.0, False),
    ("breakout_scalp", 60.0, 900.0, 3_600.0, False, 0.0, False),
    ("trend_pullback", 120.0, 1_500.0, 3_600.0, False, 0.0, False),
    ("mean_reversion", 60.0, 600.0, 2_400.0, False, 0.0, False),
]


@pytest.mark.parametrize(
    "name,lo,typical,hi,decay,halflife,reeval", SPEC_TABLE
)
def test_the_profile_table_is_exactly_the_spec(
    name, lo, typical, hi, decay, halflife, reeval
) -> None:
    p = profile_for(name)
    assert p.strategy == name
    assert (p.min_hold_seconds, p.typical_hold_seconds, p.max_hold_seconds) == (lo, typical, hi)
    assert p.decay_exit is decay
    assert p.decay_halflife_seconds == halflife
    assert p.reeval_entry_score is reeval
    assert p.min_hold_seconds < p.typical_hold_seconds < p.max_hold_seconds


def test_there_is_no_universal_minimum_hold() -> None:
    """The flow strategies have none at all; only the slow ones do."""
    assert PROFILES["flow_impulse"].min_hold_seconds == 0.0
    assert PROFILES["book_imbalance"].min_hold_seconds == 0.0
    assert all(
        PROFILES[s].min_hold_seconds >= 60.0
        for s in ("momentum_scalp", "breakout_scalp", "trend_pullback", "mean_reversion")
    )


def test_slow_strategies_never_re_evaluate_the_entry_score() -> None:
    """Entry scoring and position management are different questions."""
    for name in ("momentum_scalp", "breakout_scalp", "trend_pullback", "mean_reversion"):
        assert PROFILES[name].reeval_entry_score is False


def test_an_unknown_strategy_gets_an_inert_profile() -> None:
    """It must not inherit another strategy's timing."""
    p = profile_for("something_new")
    assert p is DEFAULT_PROFILE
    assert p.max_hold_seconds == 0.0
    assert p.decay_exit is False
    assert p.reeval_entry_score is False


# --------------------------------------------------------------------------- #
# Triggers
# --------------------------------------------------------------------------- #


def test_the_trigger_is_signed_toward_the_position() -> None:
    f = make_features(trade_flow_imbalance=0.5)
    assert trigger_value("flow_impulse", Side.LONG, f) == pytest.approx(0.5)
    assert trigger_value("flow_impulse", Side.SHORT, f) == pytest.approx(-0.5)


def test_mean_reversion_is_triggered_by_the_stretch_not_by_the_trend() -> None:
    """A LONG mean reversion is taken *below* value, so a negative deviation is
    the premise being true."""
    below = make_features(vwap_dev_bps=-30.0)
    assert trigger_value("mean_reversion", Side.LONG, below) == pytest.approx(30.0)
    assert trigger_value("mean_reversion", Side.SHORT, below) == pytest.approx(-30.0)


def test_an_unmapped_strategy_has_no_trigger_and_no_features_is_not_decay() -> None:
    assert trigger_value("something_new", Side.LONG, IMPULSE) == 0.0
    assert trigger_value("flow_impulse", Side.LONG, None) == 0.0
    assert signal_decay(0.0, 0.0) == 0.0


# --------------------------------------------------------------------------- #
# Signal decay — a measurement, not a timer
# --------------------------------------------------------------------------- #


def test_decay_is_the_fraction_of_the_trigger_given_back() -> None:
    assert signal_decay(0.6, 0.6) == pytest.approx(0.0)
    assert signal_decay(0.6, 0.3) == pytest.approx(0.5)
    assert signal_decay(0.6, 0.0) == pytest.approx(1.0)
    assert signal_decay(0.6, -0.9) == pytest.approx(1.0)  # flipped is fully gone


def test_a_trigger_that_grew_has_decayed_by_nothing() -> None:
    assert signal_decay(0.6, 1.2) == pytest.approx(0.0)


def test_tolerance_halves_every_halflife_and_starts_at_everything() -> None:
    assert decay_tolerance(0.0, 20.0) == pytest.approx(1.0)
    assert decay_tolerance(20.0, 20.0) == pytest.approx(0.5)
    assert decay_tolerance(40.0, 20.0) == pytest.approx(0.25)
    # No decay model configured: nothing is ever spent.
    assert decay_tolerance(9_999.0, 0.0) == pytest.approx(1.0)


def test_nothing_can_decay_out_at_the_instant_of_entry() -> None:
    assert decay_spent(1.0, 0.0, 20.0) is False


def test_an_intact_signal_never_decays_out_at_any_age() -> None:
    """This is the difference between decay and a timer."""
    for held in (10.0, 30.0, 120.0, 3_600.0):
        assert decay_spent(signal_decay(0.6, 0.6), held, 20.0) is False


def test_a_spent_signal_decays_out_as_soon_as_the_tolerance_falls_below_it() -> None:
    decay = signal_decay(0.6, 0.3)                 # half given back
    assert decay_spent(decay, 19.0, 20.0) is False  # tolerance still > 0.5
    assert decay_spent(decay, 21.0, 20.0) is True


# --------------------------------------------------------------------------- #
# flow_impulse: out when the impulse is, not when a clock says so
# --------------------------------------------------------------------------- #


def test_a_thirty_second_flow_trade_is_correct_when_the_impulse_is_spent() -> None:
    eng = engine()
    pos = armed()
    assert eng.check(pos, book(), SPENT, at(30)) is ExitReason.SIGNAL_DECAY


def test_a_flow_trade_is_not_timed_out_at_thirty_seconds() -> None:
    """30 s is the *typical* hold, and typical is not a deadline."""
    eng = engine()
    pos = armed()
    assert eng.check(pos, book(), IMPULSE, at(30)) is None
    assert eng.check(pos, book(), IMPULSE, at(120)) is None


def test_a_flow_trade_is_never_held_for_twenty_five_minutes() -> None:
    """Even at full strength it hits its own ceiling long before the checkpoint."""
    eng = engine()
    pos = armed()
    assert at(181) < pos.checkpoint_ms
    assert eng.check(pos, book(), IMPULSE, at(179)) is None
    assert eng.check(pos, book(), IMPULSE, at(181)) is ExitReason.TIME_STOP


def test_a_dead_impulse_leaves_immediately_rather_than_waiting_for_the_ceiling() -> None:
    eng = engine()
    pos = armed()
    assert eng.check(pos, book(), SPENT, at(45)) is ExitReason.SIGNAL_DECAY


def test_order_flow_reversing_is_a_reversal_not_a_decay() -> None:
    eng = engine()
    pos = armed()
    assert eng.check(pos, book(), FLIPPED, at(5)) is ExitReason.SIGNAL_REVERSAL


def test_a_stop_still_beats_the_holding_policy() -> None:
    eng = engine()
    pos = armed()
    assert eng.check(pos, book(bid=98.90), SPENT, at(45)) is ExitReason.STOP_LOSS


def test_the_net_target_still_beats_the_holding_policy() -> None:
    eng = engine()
    pos = armed()
    assert eng.check(pos, book(bid=101.20), SPENT, at(45)) is ExitReason.TAKE_PROFIT


def test_a_position_nobody_armed_cannot_decay_out() -> None:
    """Decay is relative; with no baseline the honest answer is 'unknown'.

    The first observation becomes the baseline, so the *second* one can decay.
    """
    eng = engine()
    pos = position()
    assert eng.check(pos, book(), SPENT, at(10)) is None
    assert state_of(pos) is not None
    assert eng.check(pos, book(), make_features(trade_flow_imbalance=0.0), at(60)) is (
        ExitReason.SIGNAL_DECAY
    )


def test_arming_is_idempotent_so_decay_is_never_re_baselined() -> None:
    pos = position()
    first = arm(pos, IMPULSE, book=book(), entry_score=0.8, ts_ms=BASE_MS)
    second = arm(pos, SPENT, book=book(), entry_score=0.1, ts_ms=at(60))
    assert first is not None and second is not None
    assert second.entry_trigger == pytest.approx(0.60)
    assert second.entry_score == pytest.approx(0.8)


def test_the_entry_snapshot_round_trips_through_position_meta() -> None:
    pos = armed()
    assert isinstance(pos.meta["holding"], dict)
    restored = state_of(pos)
    assert restored is not None
    assert restored.entry_trigger == pytest.approx(0.60)
    assert restored.strategy == "flow_impulse"


# --------------------------------------------------------------------------- #
# The slow strategies: no churn, and no re-scoring
# --------------------------------------------------------------------------- #


def test_momentum_does_not_exit_on_signal_decay_at_all() -> None:
    """Its profile says decay_exit=False; a flattening EMA is not an exit."""
    eng = engine()
    pos = armed("momentum_scalp", make_features(ema_slope_bps=4.0))
    flat = make_features(ema_slope_bps=0.2)
    assert eng.check(pos, book(), flat, at(600)) is None


def test_momentum_is_not_churned_out_in_the_first_minute() -> None:
    """min_hold_seconds is an anti-churn floor: 30 seconds of noise is noise."""
    eng = engine()
    pos = armed("momentum_scalp", make_features(ema_slope_bps=4.0))
    reversed_ = make_features(ema_slope_bps=-4.0)
    assert eng.check(pos, book(), reversed_, at(30)) is None
    assert eng.check(pos, book(), reversed_, at(90)) is ExitReason.SIGNAL_REVERSAL


def test_the_anti_churn_floor_never_blocks_a_stop() -> None:
    eng = engine()
    pos = armed("momentum_scalp", make_features(ema_slope_bps=4.0))
    assert eng.check(pos, book(bid=98.90), make_features(ema_slope_bps=-4.0), at(10)) is (
        ExitReason.STOP_LOSS
    )


def test_a_dropped_entry_score_does_not_close_a_slow_position() -> None:
    """`reeval_entry_score=False`: entry scoring is not position management."""
    eng = engine()
    pos = armed("momentum_scalp", make_features(ema_slope_bps=4.0))
    verdict = eng.check(
        pos, book(), make_features(ema_slope_bps=3.0), at(900), live_score=0.05
    )
    assert verdict is None


def test_a_dropped_entry_score_does_close_a_flow_position() -> None:
    """The same input, the opposite answer — because the profile says so."""
    eng = engine()
    pos = armed()
    assert eng.check(pos, book(), IMPULSE, at(60), live_score=0.05) is ExitReason.EXIT_WEAK


def test_a_small_score_drop_closes_nothing() -> None:
    eng = engine()
    pos = armed()
    assert eng.check(pos, book(), IMPULSE, at(60), live_score=0.75) is None


# --------------------------------------------------------------------------- #
# The strategy clock, and what volatility does to it
# --------------------------------------------------------------------------- #


def test_the_strategy_clock_tightens_the_sixty_minute_horizon() -> None:
    eng = engine()
    pos = armed("momentum_scalp", make_features(ema_slope_bps=4.0))
    steady = make_features(ema_slope_bps=4.0)
    assert eng.check(pos, book(), steady, at(2_399)) is None
    assert eng.check(pos, book(), steady, at(2_401)) is ExitReason.TIME_STOP


def test_the_horizon_still_binds_a_strategy_whose_clock_is_longer() -> None:
    """breakout_scalp's 3600 s ceiling and the horizon coincide; neither is lost."""
    eng = engine()
    pos = armed("breakout_scalp", make_features(range_pos=0.90))
    steady = make_features(range_pos=0.90)
    assert eng.check(pos, book(), steady, at(TIME_STOP_S - 1)) is None
    assert eng.check(pos, book(), steady, at(TIME_STOP_S + 1)) is ExitReason.TIME_STOP


def test_a_strategy_ceiling_is_not_extendable() -> None:
    """A strong signal at second 181 argues for a new position, not for this one."""
    eng = engine()
    pos = armed(trailing_armed=True, best_price=100.40)
    assert (
        eng.check(pos, book(bid=100.30), IMPULSE, at(200), signal_strength=1.0)
        is ExitReason.TIME_STOP
    )
    assert pos.extended is False


def test_volatility_stretches_a_quiet_clock_and_compresses_a_violent_one() -> None:
    policy = HoldingPolicy(settings=Settings())
    pos = position()
    quiet = policy.max_hold_seconds(pos, make_features(vol_percentile=0.0))
    normal = policy.max_hold_seconds(pos, make_features(vol_percentile=0.5))
    violent = policy.max_hold_seconds(pos, make_features(vol_percentile=1.0))
    assert normal == pytest.approx(180.0)
    assert quiet == pytest.approx(270.0)      # x1.5, the clamp
    assert violent == pytest.approx(90.0)     # x0.5, the clamp
    assert policy.max_hold_seconds(pos, None) == pytest.approx(180.0)


def test_a_violent_market_brings_the_deadline_forward() -> None:
    eng = engine()
    pos = armed()
    fast = make_features(trade_flow_imbalance=0.60, vol_percentile=1.0)
    assert eng.check(pos, book(), fast, at(95)) is ExitReason.TIME_STOP


# --------------------------------------------------------------------------- #
# Execution state
# --------------------------------------------------------------------------- #


def test_a_blown_out_spread_is_an_exit() -> None:
    eng = engine()
    pos = armed()
    wide = OrderBook(
        symbol=SYMBOL,
        bids=[BookLevel(100.00, 200.0)],
        asks=[BookLevel(100.08, 200.0)],
        last_update_ms=BASE_MS,
    )
    assert eng.check(pos, wide, IMPULSE, at(20)) is ExitReason.EXEC_DETERIORATED


def test_a_spread_that_doubles_from_nothing_is_not_an_exit() -> None:
    """0.3 bps to 0.6 bps has 'doubled' and costs nothing."""
    eng = engine()
    pos = position()
    tight = OrderBook(
        symbol=SYMBOL,
        bids=[BookLevel(100.000, 200.0)],
        asks=[BookLevel(100.003, 200.0)],
        last_update_ms=BASE_MS,
    )
    arm(pos, IMPULSE, book=tight, entry_score=0.8, ts_ms=BASE_MS)
    doubled = OrderBook(
        symbol=SYMBOL,
        bids=[BookLevel(100.000, 200.0)],
        asks=[BookLevel(100.006, 200.0)],
        last_update_ms=BASE_MS,
    )
    assert eng.check(pos, doubled, IMPULSE, at(20)) is None


def test_the_exit_side_of_the_book_disappearing_is_an_exit() -> None:
    eng = engine()
    pos = armed()
    thin = book(bid=100.05, qty=0.5)   # $50 of bids against a $100 position
    assert eng.check(pos, thin, IMPULSE, at(20)) is ExitReason.EXEC_DETERIORATED


def test_a_book_that_was_always_thin_was_priced_that_way_at_entry() -> None:
    eng = engine()
    pos = position()
    thin = book(bid=100.05, qty=0.5)
    arm(pos, IMPULSE, book=thin, entry_score=0.8, ts_ms=BASE_MS)
    assert eng.check(pos, thin, IMPULSE, at(20)) is None


# --------------------------------------------------------------------------- #
# Predictive EV
# --------------------------------------------------------------------------- #


def test_a_materially_negative_ev_closes_the_position() -> None:
    eng = engine()
    pos = armed()
    assert eng.check(pos, book(), IMPULSE, at(20), predicted_ev_bps=-5.0) is (
        ExitReason.NEGATIVE_EV
    )


def test_ev_noise_around_the_gate_does_not() -> None:
    eng = engine()
    pos = armed()
    assert eng.check(pos, book(), IMPULSE, at(20), predicted_ev_bps=-0.5) is None


def test_no_ev_estimate_is_not_a_negative_one() -> None:
    eng = engine()
    pos = armed()
    assert eng.check(pos, book(), IMPULSE, at(20)) is None


# --------------------------------------------------------------------------- #
# mean_reversion has its own horizon
# --------------------------------------------------------------------------- #


STRETCHED = make_features(vwap_dev_bps=-40.0)


def test_reverting_to_value_closes_a_winning_mean_reversion() -> None:
    eng = engine()
    pos = armed("mean_reversion", STRETCHED)
    back_at_value = make_features(vwap_dev_bps=-4.0)
    assert eng.check(pos, book(bid=100.30), back_at_value, at(300)) is ExitReason.TAKE_PROFIT


def test_reverting_to_value_with_nothing_to_show_for_it_is_a_weak_exit() -> None:
    eng = engine()
    pos = armed("mean_reversion", STRETCHED)
    back_at_value = make_features(vwap_dev_bps=-4.0)
    assert eng.check(pos, book(bid=99.98), back_at_value, at(300)) is ExitReason.EXIT_WEAK


def test_a_half_closed_stretch_is_the_trade_working_not_a_reason_to_leave() -> None:
    eng = engine()
    pos = armed("mean_reversion", STRETCHED)
    halfway = make_features(vwap_dev_bps=-20.0)
    assert eng.check(pos, book(bid=100.10), halfway, at(300)) is None


def test_a_stretch_that_widens_past_the_entry_structure_invalidates_the_thesis() -> None:
    eng = engine()
    pos = armed("mean_reversion", STRETCHED)
    worse = make_features(vwap_dev_bps=-80.0)
    assert eng.check(pos, book(bid=99.50), worse, at(300)) is ExitReason.SIGNAL_REVERSAL


def test_mean_reversion_does_not_inherit_the_flow_clock() -> None:
    eng = engine()
    pos = armed("mean_reversion", STRETCHED)
    halfway = make_features(vwap_dev_bps=-20.0)
    assert eng.check(pos, book(bid=100.10), halfway, at(1_000)) is None


# --------------------------------------------------------------------------- #
# Trailing, and the profiles' net floor
# --------------------------------------------------------------------------- #


def test_a_profiled_position_arms_its_trail_on_net_profit() -> None:
    """It will never travel 60% of an ATR-sized target in 30 seconds."""
    eng = engine()
    pos = armed()
    eng.update_trailing(pos, 100.06)     # +6 bps gross, +4 net, floor is 4
    assert pos.trailing_armed is True


def test_the_net_floor_does_not_arm_a_trail_that_has_nothing_to_protect() -> None:
    eng = engine()
    pos = armed()
    eng.update_trailing(pos, 100.02)     # +2 bps gross, 0 net after costs
    assert pos.trailing_armed is False


def test_an_unprofiled_position_keeps_the_v2_trailing_rule_exactly() -> None:
    eng = engine()
    pos = position("horizon_only")
    eng.update_trailing(pos, 100.06)
    assert pos.trailing_armed is False


# --------------------------------------------------------------------------- #
# Staying out of the way
# --------------------------------------------------------------------------- #


def test_an_unprofiled_strategy_is_left_entirely_alone() -> None:
    eng = engine()
    pos = position("horizon_only")
    assert eng.check(pos, book(), SPENT, at(300)) is None
    assert pos.meta == {}


def test_the_policy_can_be_switched_off_whole() -> None:
    eng = engine(enabled=False)
    pos = armed()
    assert eng.check(pos, book(), SPENT, at(45)) is None
    assert eng.check(pos, book(), IMPULSE, at(200)) is None     # horizon only now
    assert eng.check(pos, book(), IMPULSE, at(TIME_STOP_S + 1)) is ExitReason.TIME_STOP


def test_a_v1_position_never_reaches_the_holding_policy() -> None:
    eng = engine()
    pos = position(checkpoint_ms=0, time_stop_ms=0)
    assert eng.check(pos, book(), SPENT, at(45)) is None
    assert pos.meta == {}


def test_a_profiled_slow_position_still_reaches_the_checkpoint() -> None:
    """The strategy policy is a filter in front of the horizon, not a bypass."""
    eng = engine()
    pos = armed("breakout_scalp", make_features(range_pos=0.90))
    going_nowhere = make_features(range_pos=0.90, ema_slope_bps=-1.0)
    assert eng.check(pos, book(bid=100.01), going_nowhere, at(CHECKPOINT_S + 1)) is (
        ExitReason.EXIT_WEAK
    )
