"""Walk-forward validation tests.

The point of walk-forward is that the out-of-sample number cannot have been
contaminated by the selection step.  These tests attack exactly that: window
geometry, bar leakage, what gets selected and on what basis, and the fact that
the report can never show one half without the other.
"""

from __future__ import annotations

import pytest

from app.backtest.engine import BacktestConfig, run_backtest
from app.backtest.report import format_result, format_walkforward
from app.backtest.walkforward import (
    MS_PER_DAY,
    WalkForwardResult,
    plan_windows,
    run_walk_forward,
)
from app.types import PerfStats
from fixtures.doubles import (
    AlwaysLongStrategy,
    NeverStrategy,
    ScriptedStrategy,
    make_bars,
    make_components,
    single_bar_series,
)

T0 = 1_700_000_000_000
TEN_MIN = 600_000


def multiday(symbol: str, days: int, *, seed: int = 11, drift_bps: float = 8.0):
    """``days`` of bars at a 10-minute step — same geometry, 1/10th the runtime."""
    return make_bars(
        symbol,
        days * 144,
        start_ms=T0,
        step_ms=TEN_MIN,
        drift_bps=drift_bps,
        noise_bps=4.0,
        seed=seed,
    )


def factory_for(strategies):
    def build(cfg):
        return make_components(cfg, strategies=[s for s in strategies])

    return build


# --------------------------------------------------------------------------- #
# Window geometry
# --------------------------------------------------------------------------- #


def test_plan_windows_test_slices_never_overlap_each_other():
    windows = plan_windows(0, 30 * MS_PER_DAY, train_days=7, test_days=3)
    assert len(windows) > 1
    for a, b in zip(windows, windows[1:]):
        assert a.test_end_ms <= b.test_start_ms


def test_plan_windows_test_slice_always_follows_its_train_slice():
    for w in plan_windows(0, 30 * MS_PER_DAY, train_days=7, test_days=3):
        assert w.train_start_ms < w.train_end_ms == w.test_start_ms < w.test_end_ms
        assert w.train_end_ms - w.train_start_ms == 7 * MS_PER_DAY
        assert w.test_end_ms - w.test_start_ms == 3 * MS_PER_DAY


def test_plan_windows_test_slices_tile_contiguously():
    windows = plan_windows(0, 30 * MS_PER_DAY, train_days=7, test_days=3)
    for a, b in zip(windows, windows[1:]):
        assert a.test_end_ms == b.test_start_ms, "a gap would silently drop history"


def test_plan_windows_drops_a_truncated_final_window():
    windows = plan_windows(0, 11 * MS_PER_DAY, train_days=7, test_days=3)
    assert all(w.test_end_ms <= 11 * MS_PER_DAY for w in windows)


def test_plan_windows_returns_nothing_when_history_is_too_short():
    assert plan_windows(0, 5 * MS_PER_DAY, train_days=7, test_days=3) == []


def test_plan_windows_rejects_non_positive_spans():
    with pytest.raises(ValueError):
        plan_windows(0, MS_PER_DAY, train_days=0, test_days=3)
    with pytest.raises(ValueError):
        plan_windows(0, MS_PER_DAY, train_days=3, test_days=0)


# --------------------------------------------------------------------------- #
# Leakage
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def wf() -> WalkForwardResult:
    bars = {"BTCUSD": multiday("BTCUSD", 6), "ETHUSD": multiday("ETHUSD", 6, seed=21)}
    cfg = BacktestConfig(fee_bps_per_side=3.0, spread_bps=3.0, slippage_bps=1.0)
    return run_walk_forward(
        bars,
        cfg,
        train_days=2,
        test_days=1,
        component_factory=factory_for([AlwaysLongStrategy("winner")]),
    )


def test_walk_forward_actually_ran_windows(wf):
    assert len(wf.windows) >= 2
    assert len(wf.selected) == len(wf.windows)


def test_out_of_sample_never_sees_a_training_bar(wf):
    for split, (in_sample, out_of_sample) in zip(wf.splits, wf.windows):
        for ts, _ in out_of_sample.equity_curve:
            assert not split.overlaps_training(ts), "out-of-sample replayed a training bar"
            assert split.test_start_ms <= ts < split.test_end_ms
        for trade in out_of_sample.trades:
            assert trade.entry_ms >= split.test_start_ms
            assert trade.entry_ms < split.test_end_ms


def test_in_sample_stays_inside_its_training_slice(wf):
    for split, (in_sample, _) in zip(wf.splits, wf.windows):
        for ts, _ in in_sample.equity_curve:
            assert split.train_start_ms <= ts < split.train_end_ms


def test_each_window_starts_from_the_same_capital(wf):
    for in_sample, out_of_sample in wf.windows:
        assert in_sample.equity_curve[0][1] == pytest.approx(in_sample.starting_equity)
        assert out_of_sample.equity_curve[0][1] == pytest.approx(
            out_of_sample.starting_equity
        )


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


def test_only_net_profitable_strategies_survive_into_the_test_slice():
    bars = {"BTCUSD": multiday("BTCUSD", 6), "ETHUSD": multiday("ETHUSD", 6, seed=31)}
    cfg = BacktestConfig(fee_bps_per_side=4.0, spread_bps=3.0, slippage_bps=1.0)
    strategies = [
        AlwaysLongStrategy("winner", target_bps=140.0, stop_bps=70.0),
        # Target it can never reach, stop it trips on almost any bar.
        AlwaysLongStrategy("loser", target_bps=5000.0, stop_bps=8.0),
    ]
    result = run_walk_forward(
        bars, cfg, train_days=2, test_days=1, component_factory=factory_for(strategies)
    )

    assert result.windows
    for (in_sample, out_of_sample), kept in zip(result.windows, result.selected):
        profitable = {n for n, s in in_sample.per_strategy.items() if s.net_pnl > 0}
        assert set(kept) == profitable, "selection was not on net in-sample P&L"
        # Nothing that was dropped may show up out of sample.
        assert set(out_of_sample.per_strategy) <= set(kept)
        assert all(name in ("winner", "loser") for name in kept)


def test_selection_uses_net_pnl_not_gross():
    """A strategy that is gross-positive but net-negative must be dropped."""
    from app.backtest.walkforward import _profitable_strategies

    result = run_backtest(
        {}, BacktestConfig(), label="x", components=make_components(BacktestConfig())
    )
    result.per_strategy = {
        "fees_ate_it": PerfStats(trades=9, gross_pnl=40.0, fees=60.0, net_pnl=-20.0),
        "genuinely_good": PerfStats(trades=9, gross_pnl=80.0, fees=20.0, net_pnl=60.0),
        "flat": PerfStats(trades=4, gross_pnl=10.0, fees=10.0, net_pnl=0.0),
    }
    assert _profitable_strategies(result) == ("genuinely_good",)


def test_nothing_profitable_in_sample_means_nothing_traded_out_of_sample():
    bars = {"BTCUSD": multiday("BTCUSD", 5, drift_bps=0.0, seed=41)}
    cfg = BacktestConfig(fee_bps_per_side=25.0, spread_bps=4.0, slippage_bps=6.0)
    strategies = [AlwaysLongStrategy("doomed", target_bps=5000.0, stop_bps=8.0)]
    result = run_walk_forward(
        bars, cfg, train_days=2, test_days=1, component_factory=factory_for(strategies)
    )

    assert result.windows
    for (in_sample, out_of_sample), kept in zip(result.windows, result.selected):
        if in_sample.per_strategy and all(
            s.net_pnl <= 0 for s in in_sample.per_strategy.values()
        ):
            assert kept == ()
            assert out_of_sample.trades == []
    assert any(kept == () for kept in result.selected)
    assert any("net-profitable" in n for n in result.notes)


def test_a_silent_strategy_is_never_selected():
    bars = {"BTCUSD": multiday("BTCUSD", 5)}
    cfg = BacktestConfig(fee_bps_per_side=3.0, spread_bps=3.0)
    result = run_walk_forward(
        bars,
        cfg,
        train_days=2,
        test_days=1,
        component_factory=factory_for([NeverStrategy("mute")]),
    )
    assert all(kept == () for kept in result.selected)
    assert result.out_of_sample.trades == 0


def test_too_short_a_history_is_reported_not_crashed():
    bars = {"BTCUSD": multiday("BTCUSD", 2)}
    cfg = BacktestConfig()
    result = run_walk_forward(
        bars,
        cfg,
        train_days=21,
        test_days=7,
        component_factory=factory_for([AlwaysLongStrategy()]),
    )
    assert result.windows == []
    assert result.in_sample == PerfStats()
    assert result.out_of_sample == PerfStats()
    assert any("too short" in n for n in result.notes)
    assert format_walkforward(result)


# --------------------------------------------------------------------------- #
# Aggregation and reporting
# --------------------------------------------------------------------------- #


def test_aggregates_match_the_sum_of_the_windows(wf):
    assert wf.in_sample.trades == sum(a.stats.trades for a, _ in wf.windows)
    assert wf.out_of_sample.trades == sum(b.stats.trades for _, b in wf.windows)
    assert wf.out_of_sample.net_pnl == pytest.approx(
        sum(b.stats.net_pnl for _, b in wf.windows)
    )
    assert wf.in_sample.fees == pytest.approx(sum(a.stats.fees for a, _ in wf.windows))


def test_in_and_out_of_sample_are_separate_objects(wf):
    assert wf.in_sample is not wf.out_of_sample


def test_the_report_always_prints_both_halves(wf):
    text = format_walkforward(wf)
    assert "IN-SAMPLE" in text
    assert "OUT-OF-SAMPLE" in text
    # They must appear on the same line, so one cannot be quoted without the other.
    header = next(line for line in text.splitlines() if "OUT-OF-SAMPLE" in line)
    assert "IN-SAMPLE" in header
    assert "Only OUT-OF-SAMPLE is" in text


def test_the_report_names_the_windows_and_the_kept_strategies(wf):
    text = format_walkforward(wf)
    assert "PER-PERIOD BREAKDOWN" in text
    assert "strategies kept per window" in text
    for i in range(len(wf.windows)):
        assert f"W{i}:" in text


def test_the_report_says_plainly_when_out_of_sample_loses():
    losing = WalkForwardResult(
        windows=[],
        in_sample=PerfStats(trades=10, wins=8, losses=2, net_pnl=50.0, expectancy=5.0),
        out_of_sample=PerfStats(trades=10, wins=3, losses=7, net_pnl=-40.0, expectancy=-4.0),
        train_days=21,
        test_days=7,
    )
    text = format_walkforward(losing)
    assert "not validated" in text
    assert "LOSS" in text


def test_a_losing_single_backtest_is_labelled_loss():
    """Hand-built so the loss is structural, not a property of a lucky seed."""
    warmup = [(100.0, 100.05, 99.95, 100.0)] * 10
    cycle = [
        (100.0, 100.2, 99.8, 100.0),    # entry fills here
        (100.0, 100.2, 96.0, 96.5),     # straight through the stop
        (96.5, 100.2, 96.3, 100.0),     # back to the start, signal again
    ]
    bars = single_bar_series("BTCUSD", warmup + cycle * 5, start_ms=T0)
    fire = [bars[9].close_ms] + [bars[i].close_ms for i in range(12, len(bars) - 3, 3)]

    cfg = BacktestConfig(fee_bps_per_side=10.0, spread_bps=3.0, slippage_bps=2.0)
    strategies = [
        ScriptedStrategy("doomed", fire_at_ms=fire, target_bps=400.0, stop_bps=200.0)
    ]
    result = run_backtest(
        {"BTCUSD": bars},
        cfg,
        label="doomed",
        components=make_components(cfg, strategies=strategies),
    )

    assert result.stats.trades >= 3
    assert result.stats.net_pnl < 0
    text = format_result(result)
    assert "LOSS" in text
    assert "net negative: doomed" in text


def test_the_report_never_prints_a_nan_delta():
    """inf - inf is nan; a nan in a P&L table is worse than a dash."""
    both_inf = WalkForwardResult(
        windows=[],
        in_sample=PerfStats(trades=5, wins=5, net_pnl=10.0, profit_factor=float("inf")),
        out_of_sample=PerfStats(trades=5, wins=5, net_pnl=8.0, profit_factor=float("inf")),
        train_days=21,
        test_days=7,
    )
    text = format_walkforward(both_inf)
    assert "nan" not in text.lower()
    assert "profit factor" in text


def test_the_report_flags_a_small_out_of_sample_sample():
    thin = WalkForwardResult(
        windows=[],
        in_sample=PerfStats(trades=40, wins=30, losses=10, net_pnl=90.0, expectancy=2.25),
        out_of_sample=PerfStats(trades=4, wins=3, losses=1, net_pnl=6.0, expectancy=1.5),
        train_days=21,
        test_days=7,
    )
    text = format_walkforward(thin)
    assert "too few to call this an edge" in text
