"""V2 experiment-harness tests.

These do not test whether any arm makes money — the doubles have no edge.  They
test the four things an experiment harness lies about:

1. an arm that claims to vary a setting actually varies it (asserted against the
   ``Settings`` object the engine was handed, not against the arm's own dict);
2. ``winner()`` refuses when nothing is net-positive;
3. ``winner()`` refuses when the best arm's sample is too small;
4. ``winner()`` does not follow win rate.

Everything is offline, seeded and runs on a handful of synthetic bars.
"""

from __future__ import annotations

import pytest

from app.backtest.engine import BacktestConfig, BacktestResult, run_backtest
from app.backtest.experiments import (
    ALLOCATION_MODES,
    CASH_RESERVES_PCT,
    DEFAULT_MIN_TRADES,
    NET_TARGET_PROFILES,
    RISK_BUDGETS_PCT,
    ArmMetrics,
    ExperimentArm,
    ExperimentSuite,
    allocation_modes,
    apply_overrides,
    arm_settings,
    format_suites,
    net_target_profiles,
    risk_and_reserve_grid,
    run_all,
    suites_by_name,
)
from app.config import Settings
from app.types import ClosedTrade, ExitReason, PerfStats, Side
from fixtures.doubles import make_bars, make_components, trend_bars

MINUTE = 60_000
T0 = 1_700_000_000_000
DAY_MS = 86_400_000


# --------------------------------------------------------------------------- #
# Local doubles: a runner that records what each arm was given
# --------------------------------------------------------------------------- #


def make_trade(
    i: int,
    *,
    net: float,
    gross: float | None = None,
    entry_ms: int | None = None,
    holding_ms: int = 10 * MINUTE,
    notional: float = 50.0,
    symbol: str = "BTCUSD",
) -> ClosedTrade:
    entry = T0 if entry_ms is None else entry_ms
    fees = 0.01
    slip = 0.02
    return ClosedTrade(
        id=f"t{i}",
        symbol=symbol,
        side=Side.LONG,
        strategy="double",
        regime="TRENDING",
        ledger="test",
        qty=notional / 100.0,
        entry_price=100.0,
        exit_price=100.0 + net,
        entry_ms=entry,
        exit_ms=entry + holding_ms,
        notional_usd=notional,
        gross_pnl_usd=net + fees if gross is None else gross,
        entry_fee_usd=fees / 2.0,
        exit_fee_usd=fees / 2.0,
        slippage_usd=slip,
        net_pnl_usd=net,
        exit_reason=ExitReason.TAKE_PROFIT,
        risk_usd=0.25,
        expected_net_edge_bps=3.0,
        simulator_version="test",
    )


def make_result(
    label: str,
    *,
    nets: list[float],
    starting_equity: float = 500.0,
    max_drawdown: float = 1.0,
    span_days: float = 10.0,
) -> BacktestResult:
    """A ``BacktestResult`` whose stats are computed from a list of net P&Ls."""
    trades = [
        make_trade(i, net=n, entry_ms=T0 + i * 30 * MINUTE) for i, n in enumerate(nets)
    ]
    wins = [t for t in trades if t.net_pnl_usd > 0]
    losses = [t for t in trades if t.net_pnl_usd <= 0]
    win_sum = sum(t.net_pnl_usd for t in wins)
    loss_sum = abs(sum(t.net_pnl_usd for t in losses))
    stats = PerfStats(
        trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        gross_pnl=sum(t.gross_pnl_usd for t in trades),
        fees=sum(t.fees_usd for t in trades),
        slippage=sum(t.slippage_usd for t in trades),
        net_pnl=sum(t.net_pnl_usd for t in trades),
        avg_win=win_sum / len(wins) if wins else 0.0,
        avg_loss=-(loss_sum / len(losses)) if losses else 0.0,
        profit_factor=(win_sum / loss_sum) if loss_sum else (float("inf") if win_sum else 0.0),
        expectancy=(sum(t.net_pnl_usd for t in trades) / len(trades)) if trades else 0.0,
        max_drawdown=max_drawdown,
        avg_holding_seconds=600.0,
        capital_turnover=sum(t.notional_usd for t in trades) / starting_equity,
    )
    end_ms = T0 + int(span_days * DAY_MS)
    return BacktestResult(
        label=label,
        start_ms=T0,
        end_ms=end_ms,
        trades=trades,
        stats=stats,
        per_strategy={"double": stats},
        equity_curve=[(T0, starting_equity), (end_ms, starting_equity + stats.net_pnl)],
        starting_equity=starting_equity,
        ending_equity=starting_equity + stats.net_pnl,
        symbols=("BTCUSD",),
        bars_processed=1000,
    )


class RecordingRunner:
    """Stands in for ``run_backtest`` and records the Settings each arm saw."""

    def __init__(self, results: dict[str, BacktestResult] | None = None) -> None:
        self.calls: list[dict] = []
        self.results = results or {}

    def __call__(self, bars, cfg, *, label, components=None, settings=None):
        self.calls.append(
            {"bars": bars, "cfg": cfg, "label": label, "settings": settings,
             "components": components}
        )
        arm_label = label.split("/", 1)[-1]
        if arm_label in self.results:
            return self.results[arm_label]
        return make_result(label, nets=[0.01] * 5)

    def settings_for(self, arm_label: str) -> Settings:
        for call in self.calls:
            if call["label"].endswith(f"/{arm_label}"):
                return call["settings"]
        raise AssertionError(f"no call recorded for arm {arm_label!r}")


def tiny_bars() -> dict[str, list]:
    return {"BTCUSD": make_bars("BTCUSD", 30, seed=3)}


def arm(label: str, result: BacktestResult | None, overrides=None) -> ExperimentArm:
    a = ExperimentArm(label=label, settings_overrides=overrides or {})
    a.result = result
    return a


# --------------------------------------------------------------------------- #
# apply_overrides
# --------------------------------------------------------------------------- #


def test_apply_overrides_replaces_nested_frozen_config(settings):
    out = apply_overrides(settings, {"profit.profile": "fast"})
    assert out.profit.profile == "fast"
    assert settings.profit.profile != "fast" or settings is not out
    assert out.risk is settings.risk  # untouched sections are not rebuilt


def test_apply_overrides_sets_two_fields_in_one_section(settings):
    out = apply_overrides(
        settings,
        {"portfolio.total_open_risk_pct": 5.0, "portfolio.cash_reserve_pct": 30.0},
    )
    assert out.portfolio.total_open_risk_pct == 5.0
    assert out.portfolio.cash_reserve_pct == 30.0


def test_apply_overrides_rejects_unknown_path(settings):
    with pytest.raises(KeyError):
        apply_overrides(settings, {"portfolio.no_such_knob": 1})
    with pytest.raises(KeyError):
        apply_overrides(settings, {"no_such_section.x": 1})


def test_apply_overrides_of_nothing_is_identity(settings):
    assert apply_overrides(settings, {}) is settings


def test_arm_settings_lets_the_arm_override_win_over_the_backtest_config(settings):
    cfg = BacktestConfig(starting_equity=500.0, risk_per_trade_pct=0.5)
    s = arm_settings(cfg, {"risk.risk_per_trade_pct": 2.0}, base=settings)
    # backtest_settings would have pinned 0.5 from cfg; the arm's variable wins.
    assert s.risk.risk_per_trade_pct == 2.0
    assert s.risk.starting_equity == 500.0


# --------------------------------------------------------------------------- #
# 1. every arm actually receives its overrides
# --------------------------------------------------------------------------- #


def test_profile_arms_each_receive_their_own_profile(settings):
    runner = RecordingRunner()
    suite = net_target_profiles(tiny_bars(), BacktestConfig(), settings=settings, runner=runner)

    assert len(suite.arms) == len(NET_TARGET_PROFILES)
    seen = [c["settings"].profit.profile for c in runner.calls]
    assert seen == list(NET_TARGET_PROFILES)
    for a, profile in zip(suite.arms, NET_TARGET_PROFILES):
        assert a.settings_used.profit.profile == profile
        assert a.result is not None


def test_allocation_arms_each_receive_their_own_mode(settings):
    runner = RecordingRunner()
    suite = allocation_modes(tiny_bars(), BacktestConfig(), settings=settings, runner=runner)
    seen = [c["settings"].portfolio.allocation_mode for c in runner.calls]
    assert seen == list(ALLOCATION_MODES)
    assert [a.label for a in suite.arms] == list(ALLOCATION_MODES)


def test_risk_grid_covers_every_combination_with_the_right_settings(settings):
    runner = RecordingRunner()
    suite = risk_and_reserve_grid(tiny_bars(), BacktestConfig(), settings=settings, runner=runner)

    assert len(suite.arms) == len(RISK_BUDGETS_PCT) * len(CASH_RESERVES_PCT)
    pairs = {
        (c["settings"].portfolio.total_open_risk_pct, c["settings"].portfolio.cash_reserve_pct)
        for c in runner.calls
    }
    assert pairs == {(r, c) for r in RISK_BUDGETS_PCT for c in CASH_RESERVES_PCT}


def test_every_arm_gets_the_same_bars_and_config(settings):
    runner = RecordingRunner()
    bars = tiny_bars()
    cfg = BacktestConfig(fee_bps_per_side=1.0)
    net_target_profiles(bars, cfg, settings=settings, runner=runner)
    assert all(c["bars"] is bars for c in runner.calls)
    assert all(c["cfg"] is cfg for c in runner.calls)
    # Fees come from cfg and must survive the override layer on every arm.
    assert all(c["settings"].fees.stress_bps == 1.0 for c in runner.calls)


def test_components_are_rebuilt_per_arm(settings):
    """A shared FeatureEngine would leak arm N's history into arm N+1."""
    made: list[object] = []

    def factory(cfg, s):
        comps = make_components(cfg, settings=s)
        made.append(comps)
        return comps

    runner = RecordingRunner()
    net_target_profiles(
        tiny_bars(), BacktestConfig(), settings=settings,
        runner=runner, component_factory=factory,
    )
    assert len(made) == len(NET_TARGET_PROFILES)
    assert len({id(c) for c in made}) == len(NET_TARGET_PROFILES)


def test_bad_override_is_reported_not_raised(settings):
    runner = RecordingRunner()
    suite = ExperimentSuite(name="x", arms=[])
    from app.backtest.experiments import _run_suite

    suite = _run_suite(
        "broken",
        [ExperimentArm(label="nope", settings_overrides={"portfolio.not_a_field": 1})],
        tiny_bars(),
        BacktestConfig(),
        settings=settings,
        runner=runner,
        component_factory=None,
        min_trades=DEFAULT_MIN_TRADES,
        max_drawdown_pct=10.0,
    )
    assert suite.arms[0].result is None
    assert "bad override" in (suite.arms[0].error or "")
    assert "nope" in suite.table()


# --------------------------------------------------------------------------- #
# 2 & 3. winner() refuses
# --------------------------------------------------------------------------- #


def test_winner_refuses_when_no_arm_is_net_positive():
    suite = ExperimentSuite(
        name="all negative",
        arms=[
            arm("a", make_result("a", nets=[-0.05] * 60)),
            arm("b", make_result("b", nets=[-0.01] * 80)),
        ],
    )
    assert suite.winner() is None
    reason = suite.why_no_winner()
    assert "no arm is net-positive" in reason
    assert "NO WINNER" in suite.table()


def test_winner_refuses_when_the_best_arm_is_under_sampled():
    # Strongly positive but only 5 trades: an estimate, not an edge.
    suite = ExperimentSuite(
        name="thin",
        arms=[
            arm("tiny-sample", make_result("tiny", nets=[1.0] * 5)),
            arm("loser", make_result("loser", nets=[-0.02] * 200)),
        ],
    )
    assert suite.winner() is None
    assert "under the 30-trade minimum" in suite.why_no_winner()


def test_min_trades_is_configurable_and_lets_a_small_sample_win():
    suite = ExperimentSuite(
        name="thin",
        arms=[arm("tiny-sample", make_result("tiny", nets=[1.0] * 5))],
        min_trades=3,
    )
    assert suite.winner() is not None
    assert suite.winner().label == "tiny-sample"


def test_winner_refuses_when_drawdown_ceiling_is_breached():
    suite = ExperimentSuite(
        name="deep dd",
        arms=[
            arm(
                "profitable-but-violent",
                make_result("v", nets=[0.5] * 60, max_drawdown=200.0),  # 40% of $500
            )
        ],
    )
    assert suite.winner() is None
    assert "drawdown ceiling" in suite.why_no_winner()


def test_winner_refuses_on_an_empty_suite_and_table_does_not_crash():
    suite = ExperimentSuite(name="empty", arms=[])
    assert suite.winner() is None
    assert "no arms" in suite.why_no_winner()
    text = suite.table()
    assert "EXPERIMENT" in text
    assert "NO WINNER" in text
    assert suite.to_dict()["winner"] is None


def test_arms_without_results_do_not_crash_the_table():
    suite = ExperimentSuite(name="half run", arms=[arm("never-ran", None)])
    assert suite.winner() is None
    assert "no arm produced a result" in suite.why_no_winner()
    assert "never-ran" in suite.table()


# --------------------------------------------------------------------------- #
# 4. winner() is not win rate — the load-bearing test
# --------------------------------------------------------------------------- #


def test_winner_ignores_win_rate_and_picks_the_better_expectancy():
    # A: 80% win rate, but the 20% of losses are large  -> expectancy -0.04
    high_win_rate = make_result("A", nets=[0.05] * 48 + [-0.40] * 12)
    # B: 40% win rate, wins are large                   -> expectancy +0.08
    low_win_rate = make_result("B", nets=[0.35] * 24 + [-0.10] * 36)

    suite = ExperimentSuite(
        name="win rate trap",
        arms=[arm("high-win-rate", high_win_rate), arm("low-win-rate", low_win_rate)],
    )

    a, b = suite.metrics()
    assert a.win_rate_pct > b.win_rate_pct
    assert a.expectancy_usd < b.expectancy_usd

    win = suite.winner()
    assert win is not None
    assert win.label == "low-win-rate", "winner() followed win rate"

    table = suite.table()
    assert "WINNER: low-win-rate" in table
    assert "higher win rate" in table


def test_winner_ignores_raw_net_pnl_when_expectancy_is_worse():
    # A trades 10x as often at a worse per-trade expectancy and so books more
    # total money.  The rule being tested is the per-trade rule.
    many_mediocre = make_result("many", nets=[0.02] * 500)     # net +10.00
    few_good = make_result("few", nets=[0.20] * 40)            # net  +8.00
    suite = ExperimentSuite(
        name="volume trap",
        arms=[arm("many-mediocre", many_mediocre), arm("few-good", few_good)],
    )
    assert many_mediocre.stats.net_pnl > few_good.stats.net_pnl
    assert suite.winner().label == "few-good"


def test_winner_breaks_an_expectancy_tie_on_shallower_drawdown():
    calm = make_result("calm", nets=[0.10] * 50, max_drawdown=1.0)
    violent = make_result("violent", nets=[0.10] * 50, max_drawdown=40.0)
    suite = ExperimentSuite(
        name="tie", arms=[arm("violent", violent), arm("calm", calm)]
    )
    assert suite.winner().label == "calm"


# --------------------------------------------------------------------------- #
# table() renders every named metric
# --------------------------------------------------------------------------- #


_REQUIRED_HEADINGS = (
    "trades", "wins", "loss", "win%", "avg gross", "avg fees", "avg sprd+slip",
    "avg NET", "PF", "expectancy", "maxDD", "avg hold", "trd/day", "turn/day",
    "end equity",
)


def test_table_renders_every_metric_the_spec_names():
    suite = ExperimentSuite(
        name="render", arms=[arm("a", make_result("a", nets=[0.1] * 40))]
    )
    text = suite.table()
    for heading in _REQUIRED_HEADINGS:
        assert heading in text, f"metric column missing: {heading}"
    assert "render" in text
    # one row per arm in each panel
    assert text.count("\na ") >= 2 or sum(1 for ln in text.splitlines() if ln.startswith("a ")) >= 2


def test_allocation_table_adds_idle_cash_and_correlated_exposure():
    suite = ExperimentSuite(
        name="alloc",
        arms=[arm("many_small", make_result("many_small", nets=[0.1] * 40))],
        show_portfolio_columns=True,
    )
    text = suite.table()
    assert "idle cash %" in text
    assert "correlated %" in text


def test_allocation_suite_sets_the_portfolio_columns_flag(settings):
    suite = allocation_modes(tiny_bars(), BacktestConfig(), settings=settings,
                             runner=RecordingRunner())
    assert suite.show_portfolio_columns is True
    assert "idle cash %" in suite.table()


def test_table_flags_arms_that_all_produced_identical_results(settings):
    """A varied setting nothing reads yet must be called out, not glossed over."""
    same = make_result("same", nets=[0.01] * 40)
    suite = ExperimentSuite(
        name="no-op knob",
        arms=[arm("micro", same, {"profit.profile": "micro"}),
              arm("base", same, {"profit.profile": "base"})],
    )
    assert "identical result" in suite.table()


def test_format_suites_concatenates():
    s1 = ExperimentSuite(name="one", arms=[])
    s2 = ExperimentSuite(name="two", arms=[])
    text = format_suites([s1, s2])
    assert "one" in text and "two" in text


# --------------------------------------------------------------------------- #
# Metrics arithmetic
# --------------------------------------------------------------------------- #


def test_metrics_are_per_trade_averages_and_per_day_rates():
    result = make_result("m", nets=[0.10] * 40, span_days=10.0)
    m = ArmMetrics.from_result("m", result)
    assert m.trades == 40
    assert m.wins == 40
    assert m.win_rate_pct == 100.0
    assert m.avg_net_usd == pytest.approx(0.10)
    assert m.avg_fees_usd == pytest.approx(0.01)
    assert m.avg_spread_slippage_usd == pytest.approx(0.02)
    assert m.expectancy_usd == pytest.approx(0.10)
    assert m.trades_per_day == pytest.approx(4.0)
    assert m.span_days == pytest.approx(10.0)
    assert m.ending_equity == pytest.approx(504.0)
    # 40 trades x $50 notional / $500 equity / 10 days
    assert m.turnover_per_day == pytest.approx(0.4)


def test_metrics_of_a_missing_result_are_empty_not_zero_division():
    m = ArmMetrics.from_result("x", None)
    assert m.ran is False
    assert m.trades == 0
    assert m.expectancy_usd == 0.0
    assert m.to_dict()["profit_factor"] == 0.0


def test_infinite_profit_factor_is_json_safe():
    result = make_result("all wins", nets=[0.1] * 5)
    m = ArmMetrics.from_result("all wins", result)
    assert m.profit_factor == float("inf")
    assert m.to_dict()["profit_factor"] is None


def test_idle_cash_and_correlated_exposure_track_overlapping_positions():
    """Two simultaneous $50 longs on a $500 book = 20% deployed, 20% correlated."""
    trades = [
        make_trade(0, net=0.0, entry_ms=T0, holding_ms=DAY_MS, symbol="BTCUSD"),
        make_trade(1, net=0.0, entry_ms=T0, holding_ms=DAY_MS, symbol="ETHUSD"),
    ]
    result = make_result("overlap", nets=[])
    result.trades = trades
    result.end_ms = T0 + DAY_MS
    m = ArmMetrics.from_result("overlap", result)
    assert m.idle_cash_pct == pytest.approx(80.0, abs=0.5)
    assert m.correlated_exposure_pct == pytest.approx(20.0, abs=0.5)
    assert m.peak_concurrent_positions == 2


def test_no_trades_means_fully_idle():
    m = ArmMetrics.from_result("flat", make_result("flat", nets=[]))
    assert m.idle_cash_pct == 100.0
    assert m.correlated_exposure_pct == 0.0
    assert m.peak_concurrent_positions == 0


# --------------------------------------------------------------------------- #
# run_all / suite lookup / real engine wiring
# --------------------------------------------------------------------------- #


def test_run_all_runs_the_three_suites(settings):
    runner = RecordingRunner()
    suites = run_all(tiny_bars(), BacktestConfig(), settings=settings, runner=runner)
    assert [s.name for s in suites] == [
        "NET target profiles", "allocation modes", "risk budget x cash reserve"
    ]
    assert len(runner.calls) == 4 + 3 + 9


def test_run_all_honours_the_only_filter(settings):
    runner = RecordingRunner()
    suites = run_all(
        tiny_bars(), BacktestConfig(), settings=settings, runner=runner, only=["allocation"]
    )
    assert len(suites) == 1 and suites[0].name == "allocation modes"


def test_unknown_suite_name_is_rejected():
    with pytest.raises(KeyError):
        suites_by_name(["profiles", "nonsense"])


def test_no_bars_skips_every_arm_without_crashing(settings):
    runner = RecordingRunner()
    suite = net_target_profiles({}, BacktestConfig(), settings=settings, runner=runner)
    assert runner.calls == []
    assert all(a.result is None for a in suite.arms)
    assert "no bars" in suite.table()
    assert suite.winner() is None


def test_suite_runs_end_to_end_through_the_real_run_backtest(settings):
    """The default runner is the real engine; prove the wiring, not the P&L."""
    cfg = BacktestConfig(fee_bps_per_side=1.0, spread_bps=2.0, slippage_bps=0.5, seed=5)
    bars = {"BTCUSD": trend_bars("BTCUSD", 60)}
    suite = net_target_profiles(
        bars,
        cfg,
        settings=settings,
        runner=run_backtest,
        component_factory=lambda c, s: make_components(c, settings=s),
    )
    assert all(a.result is not None for a in suite.arms)
    assert all(a.result.bars_processed == 60 for a in suite.arms)
    assert isinstance(suite.table(), str)
    # Deterministic: the same seed and bars must reproduce exactly.
    again = net_target_profiles(
        bars,
        cfg,
        settings=settings,
        runner=run_backtest,
        component_factory=lambda c, s: make_components(c, settings=s),
    )
    assert [a.result.stats.net_pnl for a in again.arms] == [
        a.result.stats.net_pnl for a in suite.arms
    ]


def test_to_dict_is_json_serialisable(settings):
    import json

    suite = ExperimentSuite(
        name="json", arms=[arm("a", make_result("a", nets=[0.1] * 40), {"profit.profile": "base"})]
    )
    text = json.dumps(suite.to_dict())
    assert "profit.profile" in text


# --------------------------------------------------------------------------- #
# V2 wiring: portfolio.* settings only bind when RiskState carries a portfolio
# --------------------------------------------------------------------------- #


def test_default_factory_returns_v2_components_not_a_silent_fallback(settings):
    """The fallback is silent by design, so it needs a test of its own.

    ``_default_component_factory`` swallows any import problem and hands the
    wiring back to the V1 stack.  That is the right runtime behaviour and the
    wrong thing to discover in a report, so this asserts the V2 path is live.
    """
    from app.backtest.experiments import _default_component_factory

    cfg = BacktestConfig(fee_bps_per_side=1.0)
    comps = _default_component_factory(cfg, arm_settings(cfg, {}, base=settings))
    assert comps is not None, "V2 wiring silently fell back to the V1 count cap"
    assert comps.risk.portfolio is not None


def test_v2_wiring_attaches_a_portfolio_state_with_the_arms_reserve(settings):
    from app.backtest.experiments import v2_components

    cfg = BacktestConfig(starting_equity=500.0)
    s = arm_settings(cfg, {"portfolio.cash_reserve_pct": 30.0}, base=settings)
    comps = v2_components(cfg, s)
    comps.ledger_factory("test", 500.0, "sim")
    state = comps.risk_state_cls(
        equity=500.0,
        open_positions=[],
        realized_pnl_today=0.0,
        realized_pnl_week=0.0,
        loss_streak=0,
        cooldown_until_ms=0,
        kill_switch=False,
        data_healthy=True,
        ts_ms=T0,
    )
    assert state.portfolio is not None
    assert state.portfolio.reserve_required_usd == pytest.approx(150.0)


def test_suite_records_which_wiring_it_used(settings):
    suite = net_target_profiles(tiny_bars(), BacktestConfig(), settings=settings,
                                runner=RecordingRunner())
    assert any(n.startswith("wiring: V2") for n in suite.notes), suite.notes
