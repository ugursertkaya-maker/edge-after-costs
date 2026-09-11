"""Tests for the trade-set bookkeeping in ``app.research.tournament``.

The tournament's job here is narrow and it got it wrong: an arm's
``out_of_sample_trades`` must be the *same* trades its ``out_of_sample``
statistics were computed from, because three of the seven promotion gates read
that list and report their answer underneath those statistics.

The defect these tests pin down, in the order it happened:

1. ``_run_task`` discarded the trades of every window whose out-of-sample net
   P&L was ``<= 0``.  That is a survivorship filter, and it left each arm
   holding only its *winning* windows.
2. ``_merge`` then treated whatever arrived as the arm's out-of-sample trades,
   with no check that it added up.
3. ``robustness.py`` gated on it.  The published finalist's gates 3, 4 and 6
   ran over 45 trades netting +$14.72 while the headline said 79 trades netting
   +$5.51, which is why its "fees +25%" row reported a *higher* net than the
   unstressed result.

Everything below is deterministic, in-process and needs no market data.
"""

from __future__ import annotations

import math

import pytest

from app.backtest.walkforward import WalkForwardWindow
from app.research.tournament import (
    ArmResult,
    Candidate,
    TournamentConfig,
    TradeSetIntegrityError,
    stats_from_trades,
    trade_set_mismatch,
    validate_trade_set,
)
from app.research import tournament as T
from app.types import ClosedTrade, ExitReason, PerfStats, Side

MINUTE = 60_000
DAY = 86_400_000
BASE_MS = 1_700_000_000_000


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def mk_trade(
    net: float,
    *,
    index: int = 0,
    symbol: str = "BTCUSD",
    entry_ms: int | None = None,
    hold_ms: int = 15 * MINUTE,
    regime: str = "RANGING",
    fees: float = 0.02,
    slippage: float = 0.04,
    notional: float = 20.0,
    tid: str | None = None,
) -> ClosedTrade:
    entry = BASE_MS + index * MINUTE if entry_ms is None else entry_ms
    return ClosedTrade(
        id=tid if tid is not None else f"t{index}",
        symbol=symbol,
        side=Side.LONG,
        strategy="volume_impulse",
        regime=regime,
        ledger="base",
        qty=notional / 50_000.0,
        entry_price=50_000.0,
        exit_price=50_000.0 + net,
        entry_ms=entry,
        exit_ms=entry + hold_ms,
        notional_usd=notional,
        gross_pnl_usd=net + fees,
        entry_fee_usd=fees / 2.0,
        exit_fee_usd=fees / 2.0,
        slippage_usd=slippage,
        net_pnl_usd=net,
        exit_reason=ExitReason.TAKE_PROFIT,
        risk_usd=notional * 0.01,
        expected_net_edge_bps=2.0,
        simulator_version="test",
    )


def candidate(symbol: str = "BTCUSD") -> Candidate:
    return Candidate(
        family="volume_impulse",
        symbol=symbol,
        params={"min_score": 0.35},
        horizon_seconds=3600,
        exit_profile="net_fixed:75",
        stop_profile="vol:3",
    )


def empty_arm(symbol: str = "BTCUSD") -> ArmResult:
    return ArmResult(
        candidate=candidate(symbol),
        trades=[],
        stats=PerfStats(),
        fees_usd=0.0,
        slippage_usd=0.0,
        windows=[],
        in_sample=PerfStats(),
        out_of_sample=PerfStats(),
    )


def outcome(key: str, scheme: str, index: int, trades: list[ClosedTrade]):
    """One walk-forward window's result, with statistics built from its trades."""
    stats = stats_from_trades(trades)
    return T._WindowOutcome(
        key=key,
        scheme=scheme,
        window_index=index,
        in_sample=PerfStats(trades=10, net_pnl=1.0, expectancy=0.1),
        out_of_sample=stats,
        fees_usd=stats.fees,
        slippage_usd=stats.slippage,
        best_trade_usd=max((t.net_pnl_usd for t in trades), default=0.0),
        worst_trade_usd=min((t.net_pnl_usd for t in trades), default=0.0),
        trades=list(trades),
    )


def window_group(key: str, scheme: str, nets_per_window: list[list[float]]):
    """A window per inner list, with non-overlapping trade timestamps."""
    out = []
    n = 0
    for i, nets in enumerate(nets_per_window):
        trades = []
        for net in nets:
            trades.append(mk_trade(net, index=n, tid=f"w{i}-{n}"))
            n += 1
        out.append(outcome(key, scheme, i, trades))
    return out


def cfg(**kw) -> TournamentConfig:
    base = dict(
        symbols=("BTCUSD",),
        min_trades=4,
        keep_trades_for_finalists=True,
        max_kept_trade_arms=40,
    )
    base.update(kw)
    return TournamentConfig(**base)


# --------------------------------------------------------------------------- #
# The defect itself: a losing window's trades are part of the result
# --------------------------------------------------------------------------- #


class _StubEvaluator:
    """Stands in for ``_WindowEvaluator`` so ``_run_task`` runs without data."""

    def __init__(self, *a, **kw) -> None:
        pass

    def warm_signals(self, candidates) -> None:  # noqa: ANN001
        pass

    def evaluate(self, c):  # noqa: ANN001
        # Window 0 loses, window 1 wins.  Both produced real trades.
        nets = _StubEvaluator.nets
        trades = [mk_trade(v, index=i) for i, v in enumerate(nets)]
        return outcome(c.key(), "30/7", 0, trades)


def _task(candidates, *, keep_trades: bool):
    return T._Task(
        symbol="BTCUSD",
        scheme=T.WalkForwardScheme(30, 7),
        window=WalkForwardWindow(
            index=0,
            train_start_ms=BASE_MS - 30 * DAY,
            train_end_ms=BASE_MS,
            test_start_ms=BASE_MS,
            test_end_ms=BASE_MS + 7 * DAY,
        ),
        train_bars=[],
        test_bars=[],
        candidates=list(candidates),
        cfg=cfg(),
        family_names=("volume_impulse",),
        keep_trades=keep_trades,
    )


def test_a_losing_window_still_hands_back_its_trades(monkeypatch):
    """The regression that produced the false finalist.

    ``_run_task`` used to drop ``outcome.trades`` whenever the window's net
    was ``<= 0``.  The arm then kept only the trades of its winning windows —
    a set that is profitable by construction, whatever the arm actually did.
    """
    monkeypatch.setattr(T, "load_sources", lambda names: [])
    monkeypatch.setattr(T, "_WindowEvaluator", _StubEvaluator)

    _StubEvaluator.nets = [-0.50, -0.40, 0.10]        # window net -0.80
    losing = T._run_task(_task([candidate()], keep_trades=True))
    assert [t.net_pnl_usd for t in losing[0].trades] == [-0.50, -0.40, 0.10], (
        "a losing window's trades are part of the out-of-sample result and may "
        "not be filtered out of it"
    )
    assert losing[0].out_of_sample.net_pnl < 0

    _StubEvaluator.nets = [0.50, 0.40]
    winning = T._run_task(_task([candidate()], keep_trades=True))
    assert len(winning[0].trades) == 2


def test_keep_trades_false_is_the_only_reason_trades_are_dropped(monkeypatch):
    monkeypatch.setattr(T, "load_sources", lambda names: [])
    monkeypatch.setattr(T, "_WindowEvaluator", _StubEvaluator)
    _StubEvaluator.nets = [0.50, 0.40]
    got = T._run_task(_task([candidate()], keep_trades=False))
    assert got[0].trades == []
    assert got[0].out_of_sample.trades == 2, "statistics survive, trades do not"


# --------------------------------------------------------------------------- #
# _merge: the retained list is the whole out-of-sample set, or nothing
# --------------------------------------------------------------------------- #


def test_merge_retains_every_window_including_the_losers():
    arm = empty_arm()
    arms = {arm.key: arm}
    group = window_group(arm.key, "30/7", [[0.40, 0.30], [-0.25, -0.20], [0.35, 0.10]])
    T._merge(arms, group, cfg())

    assert len(arm.out_of_sample_trades) == 6
    assert arm.out_of_sample.trades == 6
    assert math.isclose(
        math.fsum(t.net_pnl_usd for t in arm.out_of_sample_trades),
        arm.out_of_sample.net_pnl,
        abs_tol=1e-9,
    )
    assert arm.trades_complete is True
    assert trade_set_mismatch(arm) == ""


def test_merge_accumulates_across_schemes_and_stays_reconciled():
    arm = empty_arm()
    arms = {arm.key: arm}
    T._merge(arms, window_group(arm.key, "30/7", [[0.40, 0.30], [-0.25, 0.20]]), cfg())
    first = len(arm.out_of_sample_trades)
    T._merge(arms, window_group(arm.key, "60/14", [[0.15, -0.10, 0.30]]), cfg())

    assert first == 4
    assert len(arm.out_of_sample_trades) == 7 == arm.out_of_sample.trades
    assert arm.trade_windows_kept == arm.windows_evaluated == 3
    assert arm.trades_complete is True
    assert trade_set_mismatch(arm) == ""


def test_an_arm_that_only_becomes_promising_later_keeps_no_trades():
    """Half a history is not a cheap version of a whole one.

    The first scheme leaves the arm under water, so its trades are not
    retained.  When the second scheme pushes it positive the missing windows
    can no longer be recovered — so the list stays empty and the gates fall
    back to the reported aggregates, rather than being handed the tail end of
    the arm's history labelled as all of it.
    """
    arm = empty_arm()
    arms = {arm.key: arm}
    T._merge(arms, window_group(arm.key, "30/7", [[-0.50, -0.40, -0.30, -0.20]]), cfg())
    assert arm.out_of_sample_trades == []

    T._merge(arms, window_group(arm.key, "60/14", [[2.00, 1.00, 0.50, 0.40]]), cfg())
    assert arm.out_of_sample.net_pnl > 0
    assert arm.out_of_sample.trades == 8
    assert arm.out_of_sample_trades == [], "a partial list must never be published"
    assert arm.trades_complete is False
    assert trade_set_mismatch(arm) == ""


def test_an_arm_that_stops_being_promising_drops_its_trades_entirely():
    arm = empty_arm()
    arms = {arm.key: arm}
    T._merge(arms, window_group(arm.key, "30/7", [[0.50, 0.40, 0.30, 0.20]]), cfg())
    assert len(arm.out_of_sample_trades) == 4

    T._merge(arms, window_group(arm.key, "60/14", [[-1.00, -1.00, -0.50]]), cfg())
    assert arm.out_of_sample.net_pnl < 0
    assert arm.out_of_sample_trades == []
    assert arm.trades == []
    assert arm.trade_windows_kept == 0


def test_the_retention_cap_never_leaves_a_half_collected_arm():
    """Only one arm may keep trades; the other keeps none, not some."""
    a, b = empty_arm(), empty_arm()
    b.candidate = Candidate(
        family="breakout", symbol="BTCUSD", params={"min_score": 0.5},
        horizon_seconds=3600, exit_profile="net_fixed:75", stop_profile="vol:3",
    )
    arms = {a.key: a, b.key: b}
    c = cfg(max_kept_trade_arms=1)
    group = (
        window_group(a.key, "30/7", [[0.40, 0.30, 0.20, 0.10]])
        + window_group(b.key, "30/7", [[0.40, 0.30, 0.20, 0.10]])
    )
    T._merge(arms, group, c)

    kept = [arm for arm in (a, b) if arm.out_of_sample_trades]
    dropped = [arm for arm in (a, b) if not arm.out_of_sample_trades]
    assert len(kept) == 1 and len(dropped) == 1
    assert kept[0].trades_complete is True
    assert trade_set_mismatch(kept[0]) == ""
    assert trade_set_mismatch(dropped[0]) == ""

    # And the capped-out arm may not quietly start collecting on a later scheme.
    T._merge(arms, window_group(dropped[0].key, "60/14", [[0.50, 0.40]]), c)
    assert dropped[0].out_of_sample_trades == []


def test_keep_trades_for_finalists_off_keeps_statistics_only():
    arm = empty_arm()
    arms = {arm.key: arm}
    T._merge(
        arms,
        window_group(arm.key, "30/7", [[0.40, 0.30, 0.20, 0.10]]),
        cfg(keep_trades_for_finalists=False),
    )
    assert arm.out_of_sample.trades == 4
    assert arm.out_of_sample_trades == []
    assert arm.trades_complete is False


# --------------------------------------------------------------------------- #
# The assertion that makes it impossible to reintroduce quietly
# --------------------------------------------------------------------------- #


def test_trade_set_mismatch_names_a_short_list():
    arm = empty_arm()
    trades = [mk_trade(0.10, index=i) for i in range(6)]
    arm.out_of_sample = stats_from_trades(trades)
    arm.out_of_sample_trades = trades[:4]
    arm.trade_windows_kept = arm.windows_evaluated = 1
    problem = trade_set_mismatch(arm)
    assert "4 retained" in problem and "6" in problem


def test_trade_set_mismatch_names_a_net_that_does_not_add_up():
    arm = empty_arm()
    trades = [mk_trade(0.10, index=i) for i in range(6)]
    arm.out_of_sample = stats_from_trades(trades)
    arm.out_of_sample.net_pnl += 1.0          # same count, different money
    arm.out_of_sample_trades = trades
    arm.trade_windows_kept = arm.windows_evaluated = 1
    assert "net" in trade_set_mismatch(arm)


def test_an_empty_list_reconciles_because_absence_is_not_disagreement():
    arm = empty_arm()
    arm.out_of_sample = stats_from_trades([mk_trade(0.1, index=i) for i in range(5)])
    assert trade_set_mismatch(arm) == ""


def test_merge_refuses_to_publish_an_inconsistent_arm():
    """A future bug in the merge path fails on the window that caused it."""
    arm = empty_arm()
    arms = {arm.key: arm}
    group = window_group(arm.key, "30/7", [[0.40, 0.30, 0.20, 0.10]])
    # Simulate any future filter that drops trades without dropping statistics.
    group[0].trades = group[0].trades[:2]
    with pytest.raises(TradeSetIntegrityError) as exc:
        T._merge(arms, group, cfg())
    assert "2 retained out-of-sample trades against 4" in str(exc.value)


def test_expectancy_without_best_trade_agrees_with_the_retained_trades():
    arm = empty_arm()
    arms = {arm.key: arm}
    T._merge(arms, window_group(arm.key, "30/7", [[3.00, -0.20, -0.20, -0.20]]), cfg())
    tl = arm.out_of_sample_trades
    best = max(t.net_pnl_usd for t in tl)
    by_hand = (math.fsum(t.net_pnl_usd for t in tl) - best) / (len(tl) - 1)
    assert arm.expectancy_without_best_trade == pytest.approx(by_hand, abs=1e-12)
    assert arm.expectancy_without_best_trade < 0


# --------------------------------------------------------------------------- #
# validate_trade_set
# --------------------------------------------------------------------------- #


def test_validate_trade_set_is_quiet_on_a_clean_window():
    w = WalkForwardWindow(
        index=0,
        train_start_ms=BASE_MS - 30 * DAY,
        train_end_ms=BASE_MS,
        test_start_ms=BASE_MS,
        test_end_ms=BASE_MS + 7 * DAY,
    )
    trades = [mk_trade(0.1, index=i, entry_ms=BASE_MS + i * MINUTE) for i in range(5)]
    assert validate_trade_set(trades, symbol="BTCUSD", window=w) == []


def test_validate_trade_set_catches_each_class_of_violation():
    w = WalkForwardWindow(
        index=0,
        train_start_ms=BASE_MS - 30 * DAY,
        train_end_ms=BASE_MS,
        test_start_ms=BASE_MS,
        test_end_ms=BASE_MS + 7 * DAY,
    )
    trades = [
        mk_trade(0.1, index=0, entry_ms=BASE_MS, symbol="ETHUSD", tid="foreign"),
        mk_trade(0.1, index=1, entry_ms=BASE_MS - MINUTE, tid="in-train"),
        mk_trade(0.1, index=2, entry_ms=BASE_MS + 7 * DAY - MINUTE,
                 hold_ms=10 * MINUTE, tid="straddles"),
        mk_trade(0.1, index=3, entry_ms=BASE_MS + MINUTE, tid="a"),
        mk_trade(0.1, index=4, entry_ms=BASE_MS + MINUTE, tid="b"),
        mk_trade(0.1, index=5, entry_ms=BASE_MS + 2 * MINUTE, regime="PANIC",
                 tid="wrong-regime"),
    ]
    problems = validate_trade_set(
        trades,
        symbol="BTCUSD",
        window=w,
        regime_at_entry={BASE_MS + 2 * MINUTE: "VOLATILITY_EXPANSION"},
    )
    joined = " | ".join(problems)
    assert "foreign: symbol 'ETHUSD'" in joined
    assert "in-train: entry" in joined and "outside test window" in joined
    assert "straddles: exit" in joined
    assert "appears 2 times" in joined
    assert "wrong-regime: regime 'PANIC'" in joined
