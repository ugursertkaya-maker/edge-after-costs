"""Is a trade set *what it says it is*?

Gate reconciliation (``test_research_robustness.py``) asks whether a trade list
has the right count and the right net.  A list can pass that and still be wrong
about **which** trades it holds, and every one of those ways is a route to a
result that cannot be reproduced.  This file is the audit of the five
neighbouring claims the Phase-4 brief names, one deterministic test each:

1. every trade belongs to the coin its arm names;
2. every timestamp falls inside the window the trade is attributed to;
3. no duplicated overlapping sample is counted as an independent trade;
4. no train/test overlap through a trade that straddles a split boundary;
5. the regime attributed to a trade is the regime at its **entry**, not its exit.

Nothing here needs market data, a network or a cache.  Where the real function
is cheap enough to call it is called (``plan_windows``, ``slice_bars``,
``_WindowEvaluator.evaluate``, ``_merge``); where it is not, the property is
asserted against the same validator the tournament uses on itself.

Finding 3 is the one that is currently **true of the shipped tournament** and
is asserted as such, not wished away: pooling two walk-forward schemes over the
same calendar re-counts the same round trips, so the out-of-sample trade count
of a multi-scheme arm is larger than the number of independent observations it
represents.  The test measures it rather than asserting it cannot happen.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pytest

from app.backtest.engine import slice_bars
from app.backtest.walkforward import WalkForwardWindow, plan_windows
from app.research import tournament as T
from app.research.robustness import evaluate_gates, reconcile_trade_set
from app.research.tournament import (
    TournamentConfig,
    WalkForwardScheme,
    base_settings,
    stats_from_trades,
    trade_set_mismatch,
    validate_trade_set,
)
from app.types import Bar, PerfStats, RegimeState

from tests.test_research_tournament import (
    BASE_MS,
    DAY,
    MINUTE,
    candidate,
    cfg,
    empty_arm,
    mk_trade,
    outcome,
    window_group,
)

SYMBOL = "BTCUSD"


def bars(start_ms: int, count: int) -> list[Bar]:
    return [
        Bar(
            symbol=SYMBOL,
            open_ms=start_ms + i * MINUTE,
            close_ms=start_ms + (i + 1) * MINUTE - 1,
            open=50_000.0,
            high=50_010.0,
            low=49_990.0,
            close=50_000.0,
            volume=1.0,
            quote_volume=50_000.0,
            trades=10,
            taker_buy_quote=25_000.0,
        )
        for i in range(count)
    ]


# --------------------------------------------------------------------------- #
# 1. Every trade belongs to the coin its arm names
# --------------------------------------------------------------------------- #


def test_a_foreign_coin_in_an_arms_trade_list_is_a_violation():
    arm = empty_arm("BTCUSD")
    arms = {arm.key: arm}
    T._merge(arms, window_group(arm.key, "30/7", [[0.40, 0.30, 0.20, 0.10]]), cfg())

    assert validate_trade_set(arm.out_of_sample_trades, symbol="BTCUSD") == []

    intruder = mk_trade(0.25, index=99, symbol="ETHUSD", tid="eth")
    problems = validate_trade_set(
        arm.out_of_sample_trades + [intruder], symbol="BTCUSD"
    )
    assert any("symbol 'ETHUSD', expected 'BTCUSD'" in p for p in problems)


def test_the_search_never_hands_a_window_another_coins_bars():
    """The structural reason 1 holds: a task carries one symbol's bars only."""
    btc = bars(BASE_MS, 10)
    eth = [
        Bar(**{**b.__dict__, "symbol": "ETHUSD"}) for b in bars(BASE_MS, 10)
    ]
    sliced = slice_bars({"BTCUSD": btc, "ETHUSD": eth}, BASE_MS, BASE_MS + 5 * MINUTE)
    assert set(sliced) == {"BTCUSD", "ETHUSD"}
    assert all(b.symbol == "BTCUSD" for b in sliced["BTCUSD"])
    assert all(b.symbol == "ETHUSD" for b in sliced["ETHUSD"])
    # A candidate names exactly one symbol, and that is the key it is evaluated
    # under — there is no path that mixes two coins into one arm.
    assert candidate("BTCUSD").key() != candidate("ETHUSD").key()


# --------------------------------------------------------------------------- #
# 2. Timestamps fall inside the window they are attributed to
# --------------------------------------------------------------------------- #


def test_a_window_is_only_ever_given_its_own_bars():
    every = bars(BASE_MS, 60 * 24 * 40)                       # 40 days of minutes
    windows = plan_windows(
        every[0].open_ms, every[-1].close_ms + 1, train_days=30, test_days=7
    )
    assert windows
    for w in windows:
        test = slice_bars({SYMBOL: every}, w.test_start_ms, w.test_end_ms)[SYMBOL]
        train = slice_bars({SYMBOL: every}, w.train_start_ms, w.train_end_ms)[SYMBOL]
        assert test and train
        assert all(w.test_start_ms <= b.open_ms < w.test_end_ms for b in test)
        assert all(w.train_start_ms <= b.open_ms < w.train_end_ms for b in train)
        assert max(b.open_ms for b in train) < min(b.open_ms for b in test)


def test_a_trade_outside_its_window_is_a_violation():
    w = WalkForwardWindow(
        index=0,
        train_start_ms=BASE_MS - 30 * DAY,
        train_end_ms=BASE_MS,
        test_start_ms=BASE_MS,
        test_end_ms=BASE_MS + 7 * DAY,
    )
    inside = [mk_trade(0.1, index=i, entry_ms=BASE_MS + i * MINUTE) for i in range(5)]
    assert validate_trade_set(inside, symbol=SYMBOL, window=w) == []

    early = mk_trade(0.1, index=9, entry_ms=BASE_MS - MINUTE, tid="early")
    late = mk_trade(0.1, index=10, entry_ms=BASE_MS + 7 * DAY, tid="late")
    problems = validate_trade_set(inside + [early, late], symbol=SYMBOL, window=w)
    assert any("early: entry" in p for p in problems)
    assert any("late: entry" in p for p in problems)


# --------------------------------------------------------------------------- #
# 3. No duplicated overlapping sample counted as an independent trade
# --------------------------------------------------------------------------- #


def test_test_windows_of_one_scheme_never_overlap():
    windows = plan_windows(BASE_MS, BASE_MS + 120 * DAY, train_days=30, test_days=7)
    spans = sorted((w.test_start_ms, w.test_end_ms) for w in windows)
    for (a_lo, a_hi), (b_lo, b_hi) in zip(spans, spans[1:]):
        assert a_hi <= b_lo, "one scheme's test slices tile the calendar"


def test_two_schemes_do_overlap_which_is_why_pooling_double_counts():
    """The hazard, measured on the real Phase-3 walk-forward plan.

    30/7 and 60/14 both tile the same 120 days, so any calendar minute covered
    by both is tested twice.  Pooling the schemes is what the tournament does,
    and the same round trip therefore appears in two windows.
    """
    lo, hi = BASE_MS, BASE_MS + 120 * DAY
    short = plan_windows(lo, hi, train_days=30, test_days=7)
    long = plan_windows(lo, hi, train_days=60, test_days=14)
    overlap = 0
    for a in short:
        for b in long:
            overlap += max(
                0, min(a.test_end_ms, b.test_end_ms) - max(a.test_start_ms, b.test_start_ms)
            )
    assert overlap > 0
    covered_twice_days = overlap / DAY
    assert covered_twice_days > 40, covered_twice_days


def test_pooling_two_schemes_re_counts_the_same_round_trip():
    """What that overlap does to an arm, end to end through ``_merge``."""
    arm = empty_arm()
    arms = {arm.key: arm}
    shared = [
        mk_trade(0.30, index=i, entry_ms=BASE_MS + i * MINUTE, tid=f"s{i}")
        for i in range(4)
    ]
    T._merge(arms, [outcome(arm.key, "30/7", 0, shared)], cfg())
    # The 60/14 scheme re-tests the same calendar and re-finds the same trades.
    same_again = [
        mk_trade(0.30, index=i, entry_ms=BASE_MS + i * MINUTE, tid=f"L{i}")
        for i in range(4)
    ]
    T._merge(arms, [outcome(arm.key, "60/14", 0, same_again)], cfg())

    assert arm.out_of_sample.trades == 8
    problems = validate_trade_set(arm.out_of_sample_trades, symbol=SYMBOL)
    assert len(problems) == 4
    assert all("counted as independent observations" in p for p in problems)

    distinct = {(t.symbol, t.entry_ms, t.exit_ms) for t in arm.out_of_sample_trades}
    assert len(distinct) == 4, (
        "8 reported out-of-sample trades, 4 independent round trips — the "
        "reconciliation invariant cannot see this, which is why it is its own "
        "check"
    )
    # Reconciliation still passes: duplication is a *different* defect, and the
    # two checks are deliberately independent.
    assert trade_set_mismatch(arm) == ""


def test_a_single_scheme_arm_has_no_duplicates():
    arm = empty_arm()
    arms = {arm.key: arm}
    T._merge(arms, window_group(arm.key, "30/7", [[0.4, 0.3], [-0.2, 0.1], [0.2]]), cfg())
    assert validate_trade_set(arm.out_of_sample_trades, symbol=SYMBOL) == []


# --------------------------------------------------------------------------- #
# 4. No train/test overlap through a straddling trade
# --------------------------------------------------------------------------- #


def test_a_trade_may_not_straddle_the_end_of_its_test_window():
    w = WalkForwardWindow(
        index=0,
        train_start_ms=BASE_MS - 30 * DAY,
        train_end_ms=BASE_MS,
        test_start_ms=BASE_MS,
        test_end_ms=BASE_MS + 7 * DAY,
    )
    ok = mk_trade(0.1, index=0, entry_ms=w.test_end_ms - 30 * MINUTE,
                  hold_ms=10 * MINUTE, tid="inside")
    over = mk_trade(0.1, index=1, entry_ms=w.test_end_ms - 5 * MINUTE,
                    hold_ms=60 * MINUTE, tid="over")
    assert validate_trade_set([ok], symbol=SYMBOL, window=w) == []
    problems = validate_trade_set([ok, over], symbol=SYMBOL, window=w)
    assert any("over: exit" in p and "straddles" in p for p in problems)


def test_the_evaluator_cannot_see_past_its_window_so_cannot_straddle_it():
    """Structural, not incidental: the window is run on its own bars only.

    A position still open when the test slice runs out is closed on the last
    bar of that slice — the backtester has nothing after it — so an exit can
    never land in the next window's training data.  The horizon is a time stop,
    not a licence to read ahead.
    """
    every = bars(BASE_MS, 60 * 24 * 40)
    w = plan_windows(every[0].open_ms, every[-1].close_ms + 1,
                     train_days=30, test_days=7)[0]
    test = slice_bars({SYMBOL: every}, w.test_start_ms, w.test_end_ms)[SYMBOL]
    assert max(b.close_ms for b in test) < w.test_end_ms
    later = [b for b in every if b.open_ms >= w.test_end_ms]
    assert later, "there is later data, and the window is not given it"
    assert not set(id(b) for b in test) & set(id(b) for b in later)


def test_the_next_windows_training_data_starts_after_this_windows_test_data():
    windows = plan_windows(BASE_MS, BASE_MS + 120 * DAY, train_days=30, test_days=7)
    for w in windows:
        assert w.test_start_ms == w.train_end_ms, "no bar is trained and tested"
        assert w.train_start_ms < w.train_end_ms < w.test_end_ms


# --------------------------------------------------------------------------- #
# 5. The regime on a trade is the regime at its entry
# --------------------------------------------------------------------------- #


@dataclass
class _Result:
    stats: PerfStats
    trades: list


def _evaluator(regime_at_entry: dict[int, str]):
    """A ``_WindowEvaluator`` with its backtest replaced by canned results."""
    config = TournamentConfig(symbols=(SYMBOL,))
    ev = object.__new__(T._WindowEvaluator)
    ev.symbol = SYMBOL
    ev.scheme = WalkForwardScheme(30, 7)
    ev.window_index = 0
    ev.cfg = config
    ev.settings = base_settings(config)
    ev.sources = {}
    ev._signal_cache = {}
    ev.train = T.PreparedWindow(
        symbol=SYMBOL, bars=[], features={}, regimes={}, info=None,
    )
    ev.test = T.PreparedWindow(
        symbol=SYMBOL, bars=[], features={}, regimes={}, info=None,
        regime_at_entry=dict(regime_at_entry),
    )
    return ev


def test_per_regime_buckets_by_the_regime_at_entry_not_the_one_on_the_trade():
    entry = BASE_MS
    # The trade carries the regime it *exited* in; the window knows the regime
    # it was entered in.  The second is the one that may be attributed.
    trade = mk_trade(0.50, index=0, entry_ms=entry, regime="RANGING", tid="x")
    ev = _evaluator({entry: "VOLATILITY_EXPANSION"})

    calls = []

    def fake_run(candidate, prepared, settings, bt_cfg, tag):
        calls.append(tag)
        if tag == "IN":
            return _Result(stats=PerfStats(), trades=[])
        return _Result(stats=stats_from_trades([trade]), trades=[trade])

    ev._run = fake_run
    got = ev.evaluate(candidate())

    assert calls == ["IN", "OUT"]
    assert set(got.per_regime) == {"VOLATILITY_EXPANSION"}, (
        "a trade is filed under the state the market was in when it was "
        "entered — the exit regime is an outcome, not a condition"
    )
    assert got.per_regime["VOLATILITY_EXPANSION"].trades == 1


def test_a_trade_whose_entry_the_window_does_not_know_falls_back_to_its_own_label():
    trade = mk_trade(0.50, index=0, entry_ms=BASE_MS, regime="PANIC", tid="x")
    ev = _evaluator({})                                   # nothing recorded
    ev._run = lambda c, p, s, b, tag: (
        _Result(stats=PerfStats(), trades=[])
        if tag == "IN"
        else _Result(stats=stats_from_trades([trade]), trades=[trade])
    )
    got = ev.evaluate(candidate())
    assert set(got.per_regime) == {"PANIC"}


def test_validate_trade_set_flags_a_regime_that_is_not_the_entry_regime():
    entry = BASE_MS
    trade = mk_trade(0.5, index=0, entry_ms=entry, regime="RANGING", tid="x")
    assert validate_trade_set([trade], regime_at_entry={entry: "RANGING"}) == []
    problems = validate_trade_set(
        [trade], regime_at_entry={entry: "VOLATILITY_EXPANSION"}
    )
    assert any("is not the regime at entry" in p for p in problems)


# --------------------------------------------------------------------------- #
# The two checks together, on one arm
# --------------------------------------------------------------------------- #


def test_a_clean_arm_reconciles_and_validates_and_can_be_gated():
    arm = empty_arm()
    arms = {arm.key: arm}
    nets = [[0.40, 0.30, 0.25], [-0.20, 0.35, 0.30], [0.20, -0.10, 0.40]]
    T._merge(arms, window_group(arm.key, "30/7", nets), cfg())
    arm.windows = [PerfStats(trades=3, net_pnl=0.95) for _ in range(3)]

    assert trade_set_mismatch(arm) == ""
    assert validate_trade_set(arm.out_of_sample_trades, symbol=SYMBOL) == []

    check = reconcile_trade_set(
        arm.out_of_sample_trades, arm.out_of_sample, source="out_of_sample_trades"
    )
    assert check.usable is True
    report = evaluate_gates(arm, equity=500.0, samples=200, min_trades=9)
    assert report.trade_set is not None and report.trade_set.reconciled is True
    assert report.gate_results[4] is True
    assert math.isclose(
        check.trade_net, arm.out_of_sample.net_pnl, abs_tol=1e-9
    )


def test_a_hand_corrupted_arm_is_caught_by_the_producer_and_by_the_consumer():
    """Both ends refuse independently — neither is trusting the other."""
    arm = empty_arm()
    arms = {arm.key: arm}
    T._merge(arms, window_group(arm.key, "30/7", [[0.40, 0.30, 0.25, 0.20]]), cfg())
    arm.out_of_sample_trades = arm.out_of_sample_trades[:2]
    arm.trades = arm.out_of_sample_trades

    assert trade_set_mismatch(arm) != ""                  # producer's view
    report = evaluate_gates(arm, equity=500.0, samples=100)
    assert report.trade_set is not None
    assert report.trade_set.reconciled is False           # consumer's view
    assert report.gate_results[3] is False
    assert report.gate_results[4] is False
    assert report.gate_results[6] is False


def test_the_validator_and_the_reconciler_answer_different_questions():
    """Neither check subsumes the other, which is why both exist."""
    arm = empty_arm()
    dupes = [
        mk_trade(0.30, index=0, entry_ms=BASE_MS, tid="a"),
        mk_trade(0.30, index=0, entry_ms=BASE_MS, tid="b"),
    ]
    arm.out_of_sample = stats_from_trades(dupes)
    arm.out_of_sample_trades = dupes
    arm.trade_windows_kept = arm.windows_evaluated = 1
    assert trade_set_mismatch(arm) == ""                  # adds up
    assert validate_trade_set(dupes, symbol=SYMBOL)       # but is not independent

    short = [mk_trade(0.30, index=i, entry_ms=BASE_MS + i * MINUTE) for i in range(4)]
    arm2 = empty_arm()
    arm2.out_of_sample = stats_from_trades(short)
    arm2.out_of_sample_trades = short[:3]
    arm2.trade_windows_kept = arm2.windows_evaluated = 1
    assert validate_trade_set(short[:3], symbol=SYMBOL) == []   # structurally fine
    assert trade_set_mismatch(arm2) != ""                       # but does not add up
