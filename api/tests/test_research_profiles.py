"""Phase 5 profile-comparison tests.

These do not test whether any profile makes money — the doubles have no edge.
They test the five things a profile comparison lies about:

1. an arm that claims to vary a risk budget actually varies it (asserted
   against the ``Settings`` the engine was handed, not the arm's own dict);
2. the selection rule prefers the more aggressive profile **only** when that
   profile genuinely qualifies, and refuses when none does;
3. an arm with the largest **gross** P&L and a negative net is not selected —
   ``test_largest_gross_with_negative_net_is_not_selected``;
4. the daily loss breakers count days against a hand-built equity curve;
5. idle attribution sums to the idle total and never loses a bucket.

Everything is offline, seeded and runs on a handful of synthetic bars.
"""

from __future__ import annotations

import pytest

from app.backtest.engine import BacktestConfig, BacktestResult, run_backtest
from app.config import Settings
from app.research.profiles import (
    AGGRESSION_ORDER,
    BREAKER_THRESHOLDS_PCT,
    DEFAULT_MIN_TRADES,
    DEPLOY_BANDS_PCT,
    HARD_POSITION_CEILING,
    PROFILE_RISK_PCT,
    PROFILES,
    BreakerReport,
    ProfileResult,
    RecordedRejection,
    arm_settings,
    attribute_idle_usd,
    band_overrides,
    compare_profiles,
    daily_breakers,
    format_comparison,
    idle_reason_for,
    profile_overrides,
    recording_components,
    run_profile_arm,
    select_profile,
)
from app.types import ClosedTrade, ExitReason, PerfStats, Side
from fixtures.doubles import make_bars, make_components


def doubles_factory(cfg, settings):
    """``make_components`` adapted to the ``(cfg, settings)`` factory signature."""
    return make_components(cfg, settings=settings)


MINUTE = 60_000
DAY_MS = 86_400_000
T0 = 1_700_000_000_000


# --------------------------------------------------------------------------- #
# Local doubles
# --------------------------------------------------------------------------- #


class RecordingRunner:
    """A ``run_backtest`` stand-in that records the ``Settings`` each arm saw."""

    def __init__(self, result_for=None) -> None:
        self.seen: list[tuple[str, Settings]] = []
        self._result_for = result_for

    def __call__(self, bars, cfg, *, label="", components=None, settings=None, **kw):
        self.seen.append((label, settings))
        if self._result_for is not None:
            return self._result_for(label, cfg)
        return empty_result(label, cfg)

    def settings_for(self, fragment: str) -> Settings:
        for label, s in self.seen:
            if fragment in label:
                return s
        raise AssertionError(f"no arm labelled {fragment!r} in {[l for l, _ in self.seen]}")


def empty_result(label: str, cfg: BacktestConfig) -> BacktestResult:
    return BacktestResult(
        label=label,
        start_ms=T0,
        end_ms=T0 + 10 * DAY_MS,
        trades=[],
        stats=PerfStats(),
        per_strategy={},
        equity_curve=[(T0, cfg.starting_equity)],
        starting_equity=cfg.starting_equity,
        ending_equity=cfg.starting_equity,
    )


def make_trade(
    *,
    i: int = 0,
    net: float = 1.0,
    gross: float | None = None,
    notional: float = 50.0,
    entry_ms: int | None = None,
    hold_ms: int = 30 * MINUTE,
    symbol: str = "BTCUSD",
) -> ClosedTrade:
    entry = T0 + i * MINUTE if entry_ms is None else entry_ms
    g = net + 0.10 if gross is None else gross
    return ClosedTrade(
        id=f"t{i}",
        symbol=symbol,
        side=Side.LONG,
        strategy="double",
        regime="TRENDING_UP",
        ledger="main",
        qty=1.0,
        entry_price=100.0,
        exit_price=100.0 + net,
        entry_ms=entry,
        exit_ms=entry + hold_ms,
        notional_usd=notional,
        gross_pnl_usd=g,
        entry_fee_usd=0.05,
        exit_fee_usd=0.05,
        slippage_usd=0.0,
        net_pnl_usd=net,
        exit_reason=ExitReason.TAKE_PROFIT if net > 0 else ExitReason.STOP_LOSS,
        risk_usd=2.0,
        expected_net_edge_bps=10.0,
        simulator_version="sim-test",
    )


def result_with(
    label: str,
    cfg: BacktestConfig,
    *,
    trades: list[ClosedTrade],
    gross: float | None = None,
    fees: float = 0.0,
    slippage: float = 0.0,
    max_drawdown: float = 0.0,
) -> BacktestResult:
    net = sum(t.net_pnl_usd for t in trades)
    wins = sum(1 for t in trades if t.net_pnl_usd > 0)
    stats = PerfStats(
        trades=len(trades),
        wins=wins,
        losses=len(trades) - wins,
        gross_pnl=sum(t.gross_pnl_usd for t in trades) if gross is None else gross,
        fees=fees,
        slippage=slippage,
        net_pnl=net,
        profit_factor=2.0 if net > 0 else 0.5,
        expectancy=net / len(trades) if trades else 0.0,
        max_drawdown=max_drawdown,
        avg_holding_seconds=1_800.0,
        capital_turnover=sum(t.notional_usd for t in trades),
    )
    return BacktestResult(
        label=label,
        start_ms=T0,
        end_ms=T0 + 10 * DAY_MS,
        trades=trades,
        stats=stats,
        per_strategy={},
        equity_curve=[(T0, cfg.starting_equity), (T0 + 10 * DAY_MS, cfg.starting_equity + net)],
        starting_equity=cfg.starting_equity,
        ending_equity=cfg.starting_equity + net,
    )


def profile_result(
    name: str,
    *,
    profile: str,
    band: float = 60.0,
    net: float = 1.0,
    gross: float = 2.0,
    oos_trades: int = 60,
    oos_expectancy: float = 0.01,
    oos_net: float | None = None,
    max_dd: float = 1.0,
    trades: int = 120,
) -> ProfileResult:
    return ProfileResult(
        name=name,
        profile=profile,
        deploy_band_pct=band,
        total_open_risk_pct=PROFILE_RISK_PCT[profile],
        trades=trades,
        gross_pnl=gross,
        net_pnl=net,
        expectancy=net / trades if trades else 0.0,
        max_drawdown=max_dd,
        starting_equity=500.0,
        oos_trades=oos_trades,
        oos_expectancy=oos_expectancy,
        oos_net_pnl=(oos_expectancy * oos_trades) if oos_net is None else oos_net,
        ran=True,
    )


# --------------------------------------------------------------------------- #
# 1. Each profile actually receives its overrides
# --------------------------------------------------------------------------- #


def test_profile_overrides_carry_the_spec_risk_budgets():
    assert PROFILES == ("standard", "aggressive", "high_activity")
    assert [PROFILE_RISK_PCT[p] for p in PROFILES] == [3.0, 5.0, 7.5]


@pytest.mark.parametrize("profile", PROFILES)
def test_engine_sees_the_profile_risk_budget(profile):
    """Asserted against the ``Settings`` object the engine was handed."""
    runner = RecordingRunner()
    cfg = BacktestConfig(starting_equity=500.0)
    compare_profiles(
        {"BTCUSD": make_bars("BTCUSD", 30)},
        cfg=cfg,
        profiles=[profile],
        deploy_bands=[70.0],
        runner=runner,
        component_factory=doubles_factory,
    )
    s = runner.settings_for(profile)
    assert s.portfolio.total_open_risk_pct == PROFILE_RISK_PCT[profile]
    assert s.risk.max_concurrent_risk_pct == PROFILE_RISK_PCT[profile]
    # The count cap is pinned so the budget is the only variable.
    assert s.portfolio.max_open_positions == HARD_POSITION_CEILING
    assert s.risk.max_concurrent_positions == HARD_POSITION_CEILING


@pytest.mark.parametrize("band", DEPLOY_BANDS_PCT)
def test_engine_sees_the_deployment_band(band):
    runner = RecordingRunner()
    compare_profiles(
        {"BTCUSD": make_bars("BTCUSD", 30)},
        cfg=BacktestConfig(),
        profiles=["standard"],
        deploy_bands=[band],
        runner=runner,
        component_factory=doubles_factory,
    )
    s = runner.settings_for(f"standard@{band:.0f}%")
    assert s.portfolio.cash_reserve_pct == pytest.approx(100.0 - band)
    assert s.risk.max_total_exposure_pct == pytest.approx(band)


def test_every_arm_of_the_full_grid_is_distinct():
    runner = RecordingRunner()
    compare_profiles(
        {"BTCUSD": make_bars("BTCUSD", 30)},
        cfg=BacktestConfig(),
        runner=runner,
        component_factory=doubles_factory,
    )
    assert len(runner.seen) == len(PROFILES) * len(DEPLOY_BANDS_PCT)
    seen = {
        (s.portfolio.total_open_risk_pct, s.portfolio.cash_reserve_pct)
        for _, s in runner.seen
    }
    assert len(seen) == len(PROFILES) * len(DEPLOY_BANDS_PCT)


def test_arm_settings_applies_cfg_costs_before_the_profile():
    cfg = BacktestConfig(fee_bps_per_side=1.0, slippage_bps=2.0, starting_equity=500.0)
    s = arm_settings(cfg, "high_activity", 85.0)
    assert s.fees.stress_bps == 1.0
    assert s.execution.adverse_bps == 2.0
    assert s.risk.starting_equity == 500.0
    assert s.portfolio.total_open_risk_pct == 7.5
    assert s.portfolio.cash_reserve_pct == pytest.approx(15.0)


def test_unknown_profile_and_bad_band_raise():
    with pytest.raises(KeyError):
        profile_overrides("reckless")
    with pytest.raises(ValueError):
        band_overrides(0.0)
    with pytest.raises(ValueError):
        band_overrides(140.0)


# --------------------------------------------------------------------------- #
# 2. The selection rule
# --------------------------------------------------------------------------- #


def test_selection_prefers_the_more_aggressive_profile_when_it_qualifies():
    results = [
        profile_result("standard@60%", profile="standard", oos_expectancy=0.05),
        profile_result("aggressive@60%", profile="aggressive", oos_expectancy=0.01),
    ]
    sel = select_profile(results)
    # Lower per-trade expectancy, but it qualifies and it is more aggressive.
    assert sel.chosen is not None
    assert sel.chosen.name == "aggressive@60%"


def test_selection_falls_back_when_the_aggressive_profile_does_not_qualify():
    results = [
        profile_result("standard@60%", profile="standard", oos_expectancy=0.05),
        profile_result(
            "aggressive@60%", profile="aggressive", net=-3.0, oos_expectancy=0.9
        ),
    ]
    sel = select_profile(results)
    assert sel.chosen is not None
    assert sel.chosen.name == "standard@60%"
    assert any("aggressive" in arm for arm, _ in sel.disqualified)


def test_selection_prefers_the_higher_band_within_a_profile():
    results = [
        profile_result("aggressive@60%", profile="aggressive", band=60.0),
        profile_result("aggressive@85%", profile="aggressive", band=85.0),
    ]
    assert select_profile(results).chosen.name == "aggressive@85%"


def test_selection_returns_none_qualify_as_a_first_class_outcome():
    results = [
        profile_result("standard@60%", profile="standard", net=-10.0),
        profile_result("aggressive@60%", profile="aggressive", oos_expectancy=-0.02,
                       net=5.0),
        profile_result("high_activity@60%", profile="high_activity", oos_trades=4,
                       net=5.0),
    ]
    sel = select_profile(results)
    assert sel.chosen is None
    assert sel.none_qualify is True
    assert "NONE QUALIFY" in sel.reason
    assert len(sel.disqualified) == 3
    # And it must print plainly, not as an empty table.
    assert "NO PROFILE QUALIFIES." in format_comparison(results, sel)


def test_largest_gross_with_negative_net_is_not_selected():
    """Gross is not a result.  The arm that made the most before costs and lost
    money after them must never be chosen, however large its gross."""
    winner = profile_result(
        "standard@60%", profile="standard", gross=5.0, net=0.50, oos_expectancy=0.004
    )
    gross_monster = profile_result(
        "high_activity@85%",
        profile="high_activity",
        band=85.0,
        gross=9_999.0,
        net=-120.0,
        oos_expectancy=1.0,
    )
    sel = select_profile([winner, gross_monster])
    assert sel.chosen is not None
    assert sel.chosen.name == "standard@60%"
    why = dict(sel.disqualified)["high_activity@85%"]
    assert "net P&L" in why and "gross" in why


def test_selection_refuses_an_undersampled_holdout():
    r = profile_result("high_activity@85%", profile="high_activity", oos_trades=5)
    sel = select_profile([r])
    assert sel.chosen is None
    assert "out-of-sample trades" in dict(sel.disqualified)[r.name]


def test_selection_refuses_a_drawdown_over_the_ceiling_on_a_500_account():
    r = profile_result("aggressive@80%", profile="aggressive", max_dd=75.0)
    sel = select_profile([r], max_drawdown_pct=10.0)
    assert sel.chosen is None
    assert "drawdown" in dict(sel.disqualified)[r.name]
    # $50 is exactly 10% of $500 and must pass.
    assert select_profile(
        [profile_result("aggressive@80%", profile="aggressive", max_dd=50.0)]
    ).chosen is not None


def test_selection_refuses_when_nothing_ran():
    sel = select_profile([ProfileResult(name="standard@60%", error="boom")])
    assert sel.chosen is None
    assert "no profile produced a result" in sel.reason
    assert select_profile([]).chosen is None


def test_aggression_order_is_least_to_most():
    assert AGGRESSION_ORDER["standard"] < AGGRESSION_ORDER["aggressive"]
    assert AGGRESSION_ORDER["aggressive"] < AGGRESSION_ORDER["high_activity"]


def test_default_min_trades_matches_the_rest_of_the_stack():
    assert DEFAULT_MIN_TRADES == 30


# --------------------------------------------------------------------------- #
# 3. Daily loss circuit breakers, against a hand-built equity curve
# --------------------------------------------------------------------------- #


def _curve(*days: list[float]) -> list[tuple[int, float]]:
    """One list of equity points per day, anchored to successive UTC days."""
    out: list[tuple[int, float]] = []
    for d, points in enumerate(days):
        base = (T0 // DAY_MS + d) * DAY_MS
        for i, eq in enumerate(points):
            out.append((base + i * 3600_000, eq))
    return out


def test_breaker_counts_days_not_crossings():
    # Day 1 opens at 500 and dips to 480 (-4%): trips -3%, not -5% or -7.5%.
    # It dips below -3% twice; that is one event, not two.
    curve = _curve([500.0, 484.0, 495.0, 483.0, 490.0])
    reports = {r.threshold_pct: r for r in daily_breakers(curve)}
    assert reports[3.0].days == 1
    assert reports[3.0].trips == 1
    assert reports[5.0].trips == 0
    assert reports[7.5].trips == 0


def test_breaker_reports_what_the_curve_did_after_the_trip():
    # Opens 500, trips -3% at 485, then keeps falling to 470.
    curve = _curve([500.0, 485.0, 470.0])
    rep = {r.threshold_pct: r for r in daily_breakers(curve)}[3.0]
    assert rep.trips == 1
    assert rep.total_after_trip_usd == pytest.approx(-15.0)
    assert rep.avg_after_trip_pct == pytest.approx(-3.0)
    assert rep.deepened_days == 1 and rep.recovered_days == 0
    assert "saved $15.00" in rep.reading


def test_breaker_that_recovers_is_reported_as_cutting_recovery_short():
    curve = _curve([500.0, 480.0, 505.0])
    rep = {r.threshold_pct: r for r in daily_breakers(curve)}[3.0]
    assert rep.trips == 1
    assert rep.total_after_trip_usd == pytest.approx(25.0)
    assert rep.recovered_days == 1
    assert "cut $25.00 of recovery short" in rep.reading


def test_breaker_that_never_fires_says_so():
    rep = {r.threshold_pct: r for r in daily_breakers(_curve([500.0, 501.0]))}[7.5]
    assert rep.trips == 0
    assert "never fired" in rep.reading


def test_breaker_counts_multiple_days_and_a_high_trip_rate():
    curve = _curve(
        [500.0, 470.0],   # -6%: trips 3 and 5
        [470.0, 440.0],   # -6.4%: trips 3 and 5
        [440.0, 439.0],   # flat
    )
    reports = {r.threshold_pct: r for r in daily_breakers(curve)}
    assert reports[3.0].days == 3
    assert reports[3.0].trips == 2
    assert reports[3.0].trip_rate_pct == pytest.approx(200 / 3)
    assert reports[5.0].trips == 2
    assert reports[7.5].trips == 0
    assert "fires on most days" in reports[3.0].reading


def test_breaker_thresholds_are_the_three_the_spec_asks_for():
    assert BREAKER_THRESHOLDS_PCT == (3.0, 5.0, 7.5)
    assert [r.threshold_pct for r in daily_breakers([])] == [3.0, 5.0, 7.5]
    assert daily_breakers([])[0].reading == "no equity curve"


def test_breaker_report_is_json_safe():
    d = BreakerReport(threshold_pct=5.0).as_dict()
    assert d["threshold_pct"] == 5.0 and "reading" in d


# --------------------------------------------------------------------------- #
# 4. Idle attribution
# --------------------------------------------------------------------------- #


def test_idle_attribution_sums_to_the_idle_total():
    out = attribute_idle_usd(
        400.0,
        {"EXECUTION_COST": 300.0, "RISK_LIMIT": 100.0, "NO_QUALIFIED_SIGNAL": 100.0},
        cash_reserve_usd=100.0,
    )
    assert sum(out.values()) == pytest.approx(400.0)
    assert out["CASH_RESERVE"] == 100.0
    # 300 remaining, split 3:1:1.
    assert out["EXECUTION_COST"] == pytest.approx(180.0)
    assert out["RISK_LIMIT"] == pytest.approx(60.0)
    assert out["NO_QUALIFIED_SIGNAL"] == pytest.approx(60.0)


def test_idle_with_no_rejections_is_no_qualified_signal():
    out = attribute_idle_usd(250.0, {}, cash_reserve_usd=50.0)
    assert out["CASH_RESERVE"] == 50.0
    assert out["NO_QUALIFIED_SIGNAL"] == pytest.approx(200.0)
    assert sum(out.values()) == pytest.approx(250.0)


def test_idle_attribution_carries_every_bucket_even_at_zero():
    out = attribute_idle_usd(0.0, {"EXECUTION_COST": 1.0})
    assert set(out) == {
        "NO_QUALIFIED_SIGNAL", "CORRELATION_LIMIT", "RISK_LIMIT",
        "LIQUIDITY_LIMIT", "EXECUTION_COST", "CASH_RESERVE",
    }
    assert sum(out.values()) == 0.0


def test_reserve_never_exceeds_the_idle_it_is_carved_from():
    out = attribute_idle_usd(30.0, {"RISK_LIMIT": 1.0}, cash_reserve_usd=100.0)
    assert out["CASH_RESERVE"] == 30.0
    assert sum(out.values()) == pytest.approx(30.0)


@pytest.mark.parametrize(
    "reason,bucket",
    [
        ("NEGATIVE_EDGE", "EXECUTION_COST"),
        ("EDGE_BELOW_MIN", "EXECUTION_COST"),
        ("WIDE_SPREAD", "EXECUTION_COST"),
        ("THIN_DEPTH", "LIQUIDITY_LIMIT"),
        ("PORTFOLIO_RISK_BUDGET", "RISK_LIMIT"),
        ("CORRELATED_EXPOSURE", "CORRELATION_LIMIT"),
        ("CASH_RESERVE", "CASH_RESERVE"),
        ("SOMETHING_NOBODY_MAPPED", "NO_QUALIFIED_SIGNAL"),
    ],
)
def test_reject_reason_maps_to_an_idle_bucket(reason, bucket):
    assert idle_reason_for(reason) == bucket


# --------------------------------------------------------------------------- #
# 5. End to end against the doubles
# --------------------------------------------------------------------------- #


def test_run_profile_arm_against_doubles_produces_a_populated_result():
    cfg = BacktestConfig(starting_equity=500.0, max_concurrent=5)
    bars = {
        "BTCUSD": make_bars("BTCUSD", 400, drift_bps=4.0, seed=3),
        "ETHUSD": make_bars("ETHUSD", 400, drift_bps=-2.0, seed=5, start_price=50.0),
    }
    res, rejections = run_profile_arm(
        bars,
        cfg,
        profile="aggressive",
        band_pct=80.0,
        component_factory=doubles_factory,
    )
    assert res.ran and res.error is None
    assert res.name == "aggressive@80%"
    assert res.profile == "aggressive" and res.deploy_band_pct == 80.0
    assert res.settings_used.portfolio.total_open_risk_pct == 5.0
    assert sum(res.idle_by_reason.values()) == pytest.approx(res.idle_usd, abs=1e-6)
    assert res.avg_idle_pct + res.avg_deployed_pct == pytest.approx(100.0)
    assert [b.threshold_pct for b in res.breakers] == [3.0, 5.0, 7.5]
    assert all(isinstance(r, RecordedRejection) for r in rejections)
    assert all(r.arm == "aggressive@80%" for r in rejections)
    assert res.as_dict()["profile"] == "aggressive"


def test_compare_profiles_returns_the_full_grid_in_spec_order():
    cfg = BacktestConfig(starting_equity=500.0)
    results = compare_profiles(
        {"BTCUSD": make_bars("BTCUSD", 200, seed=9)},
        cfg=cfg,
        component_factory=doubles_factory,
    )
    assert len(results) == 12
    assert [r.name for r in results[:4]] == [
        "standard@60%", "standard@70%", "standard@80%", "standard@85%"
    ]
    assert results[-1].name == "high_activity@85%"


def test_identical_books_are_called_out_not_presented_as_a_comparison():
    """Phase 3's allocation result, restated: three identical books mean the
    varied knob was not the binding constraint."""
    results = [
        profile_result(f"{p}@60%", profile=p, net=-1.0, gross=-1.0, trades=10)
        for p in PROFILES
    ]
    for r in results:
        r.max_positions = 2
    text = format_comparison(results)
    assert "identical book" in text
    assert "NOT the" in text


def test_recording_components_forwards_decisions_untouched():
    cfg = BacktestConfig(starting_equity=500.0)
    settings = arm_settings(cfg, "standard", 70.0)
    wrapped, rec = recording_components(cfg, settings, arm="x", factory=doubles_factory)
    plain = make_components(cfg, settings=settings)
    bars = {"BTCUSD": make_bars("BTCUSD", 300, seed=13)}
    a = run_backtest(dict(bars), cfg, label="a", components=wrapped, settings=settings)
    b = run_backtest(dict(bars), cfg, label="b", components=plain, settings=settings)
    assert a.entries_filled == b.entries_filled
    assert len(a.trades) == len(b.trades)
    assert a.stats.net_pnl == pytest.approx(b.stats.net_pnl)
    assert rec.arm == "x"


def test_oos_slice_reads_only_the_holdout():
    cfg = BacktestConfig(starting_equity=500.0)
    # 10 losing trades in the first half, 10 winners in the last 30%.
    trades = [make_trade(i=i, net=-1.0, entry_ms=T0 + i * MINUTE) for i in range(10)]
    trades += [
        make_trade(i=100 + i, net=2.0, entry_ms=T0 + int(9.5 * DAY_MS) + i * MINUTE)
        for i in range(10)
    ]
    runner = RecordingRunner(
        result_for=lambda label, c: result_with(label, c, trades=trades)
    )
    res, _ = run_profile_arm(
        {"BTCUSD": []},
        cfg,
        profile="standard",
        band_pct=60.0,
        runner=runner,
        component_factory=doubles_factory,
        oos_fraction=0.30,
    )
    assert res.trades == 20
    assert res.net_pnl == pytest.approx(10.0)
    assert res.oos_trades == 10
    assert res.oos_net_pnl == pytest.approx(20.0)
    assert res.oos_expectancy == pytest.approx(2.0)


def test_an_arm_that_raises_is_recorded_not_swallowed():
    def boom(*a, **kw):
        raise RuntimeError("engine exploded")

    res, rejections = run_profile_arm(
        {"BTCUSD": []},
        BacktestConfig(),
        profile="standard",
        band_pct=60.0,
        runner=boom,
        component_factory=doubles_factory,
    )
    assert res.ran is False
    assert "engine exploded" in (res.error or "")
    assert rejections == []
    assert "did not run" in format_comparison([res])


def test_compare_profiles_does_not_hoard_rejections_by_default():
    """Twelve arms' refusals over 120 days is more memory than the bars.  The
    grid keeps none; the audit replays the one arm it needs."""
    sink: list = []
    compare_profiles(
        {"BTCUSD": make_bars("BTCUSD", 200, seed=21)},
        cfg=BacktestConfig(),
        profiles=["standard"],
        deploy_bands=[60.0],
        component_factory=doubles_factory,
        rejection_sink=sink,
    )
    assert sink == []


def test_rejections_for_arm_replays_the_same_arm():
    from app.research.profiles import rejections_for_arm

    cfg = BacktestConfig(starting_equity=500.0)
    bars = {"BTCUSD": make_bars("BTCUSD", 300, seed=27)}
    results = compare_profiles(
        bars,
        cfg=cfg,
        profiles=["aggressive"],
        deploy_bands=[70.0],
        component_factory=doubles_factory,
    )
    arm = results[0]
    direct, expected = run_profile_arm(
        bars, cfg, profile="aggressive", band_pct=70.0,
        component_factory=doubles_factory,
    )
    replayed = rejections_for_arm(
        bars, cfg=cfg, arm=arm, component_factory=doubles_factory
    )
    # Deterministic engine: the replay must be the identical refusal list.
    assert replayed == expected
    assert all(r.arm == "aggressive@70%" for r in replayed)
    assert direct.net_pnl == pytest.approx(arm.net_pnl)
