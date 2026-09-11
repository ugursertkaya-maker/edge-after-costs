"""Tests for ``app.research.robustness`` — the promotion gates.

The point of this module is that it is *hard* to pass, so the tests are built
the same way round: for each of the seven gates there is an arm that fails
exactly that gate and passes the others, and a genuinely good arm that passes
all seven.  If a future change makes a gate vacuous, one of these turns green
when it should be red.

Everything is deterministic, seeded and offline.  ``ArmResult`` lives in
``app.research.tournament``, which is owned by another workstream; these tests
use a local double with the field names from the frozen contract in
``docs/PHASE3_SPEC.md`` and assert against the real class when it is importable.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields

import pytest

from app.research.robustness import (
    FRAGILITY_SCENARIO,
    MIN_OOS_TRADES,
    SCENARIOS,
    GateReport,
    MonteCarloResult,
    StressScenario,
    apply_cost_stress,
    compute_stats,
    evaluate_all,
    evaluate_gates,
    finalists,
    format_gate_report,
    format_gate_reports,
    monte_carlo,
    stress_suite,
)
from app.types import ClosedTrade, ExitReason, PerfStats, Side

EQUITY = 500.0


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def mk_trade(
    net: float,
    *,
    index: int = 0,
    fees: float = 0.01,
    slippage: float = 0.02,
    notional: float = 20.0,
) -> ClosedTrade:
    """A closed trade whose net is exactly ``net``.

    ``net_pnl_usd == gross - entry_fee - exit_fee`` exactly as ``Ledger.close``
    computes it; slippage is itemised but already inside the fill prices, which
    is the convention the stress arithmetic depends on.
    """
    entry_ms = 1_700_000_000_000 + index * 60_000
    return ClosedTrade(
        id=f"t{index}",
        symbol="BTCUSD",
        side=Side.LONG,
        strategy="test",
        regime="RANGING",
        ledger="base",
        qty=notional / 50_000.0,
        entry_price=50_000.0,
        exit_price=50_000.0 + net,
        entry_ms=entry_ms,
        exit_ms=entry_ms + 900_000,
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


def mk_trades(nets: list[float], **kw) -> list[ClosedTrade]:
    return [mk_trade(n, index=i, **kw) for i, n in enumerate(nets)]


@dataclass
class FakeCandidate:
    name: str = "momentum/BTCUSD/900s"

    def key(self) -> str:
        return self.name


@dataclass
class FakeArm:
    """Local double for ``tournament.ArmResult`` (frozen contract field names)."""

    candidate: FakeCandidate
    trades: list[ClosedTrade]
    stats: PerfStats
    fees_usd: float = 0.0
    slippage_usd: float = 0.0
    windows: list[PerfStats] = field(default_factory=list)
    in_sample: PerfStats = field(default_factory=PerfStats)
    out_of_sample: PerfStats = field(default_factory=PerfStats)


def make_arm(
    trades: list[ClosedTrade],
    *,
    windows: list[PerfStats] | None = None,
    name: str = "momentum/BTCUSD/900s",
    oos: PerfStats | None = None,
) -> FakeArm:
    stats = compute_stats(trades, equity=EQUITY)
    if windows is None:
        # Three plainly positive windows, so gate 5 is not the thing under test.
        windows = [
            PerfStats(trades=10, net_pnl=1.0, expectancy=0.1, profit_factor=2.0)
            for _ in range(3)
        ]
    return FakeArm(
        candidate=FakeCandidate(name),
        trades=trades,
        stats=stats,
        fees_usd=stats.fees,
        slippage_usd=stats.slippage,
        windows=windows,
        in_sample=stats,
        out_of_sample=oos if oos is not None else stats,
    )


def good_nets() -> list[float]:
    """40 trades, +0.20 win / -0.10 loss, 24/16 — comfortably inside every gate."""
    nets: list[float] = []
    for i in range(40):
        nets.append(0.20 if i % 5 != 0 and i % 5 != 1 else -0.10)
    return nets


def good_arm(**kw) -> FakeArm:
    return make_arm(mk_trades(good_nets()), **kw)


def gates_of(report: GateReport) -> dict[int, bool]:
    return report.gate_results


# --------------------------------------------------------------------------- #
# The contract itself
# --------------------------------------------------------------------------- #


def test_local_arm_double_matches_the_real_contract_when_it_lands():
    """The double must not drift from ``tournament.ArmResult``."""
    try:
        from app.research.tournament import ArmResult  # noqa: PLC0415
    except Exception:  # pragma: no cover - tournament.py is written in parallel
        pytest.skip("app.research.tournament is not available yet")
    real = {f.name for f in fields(ArmResult)}
    double = {f.name for f in fields(FakeArm)}
    missing = real - double
    assert not missing, f"ArmResult gained fields the double lacks: {sorted(missing)}"


# --------------------------------------------------------------------------- #
# compute_stats
# --------------------------------------------------------------------------- #


def test_compute_stats_matches_the_ledger_conventions():
    trades = mk_trades([1.0, -0.5, 0.25], fees=0.0, slippage=0.0)
    stats = compute_stats(trades, equity=EQUITY)
    assert stats.trades == 3
    assert stats.wins == 2
    assert stats.losses == 1
    assert stats.net_pnl == pytest.approx(0.75)
    assert stats.expectancy == pytest.approx(0.25)
    assert stats.avg_loss == pytest.approx(0.5)          # positive magnitude
    assert stats.profit_factor == pytest.approx(1.25 / 0.5)
    assert stats.max_drawdown == pytest.approx(0.5)      # peak 501, trough 500.5


def test_compute_stats_profit_factor_is_infinite_without_losers():
    stats = compute_stats(mk_trades([0.1, 0.2]), equity=EQUITY)
    assert stats.profit_factor == float("inf")


def test_compute_stats_on_no_trades_is_an_empty_stat_block():
    stats = compute_stats([], equity=EQUITY)
    assert stats == PerfStats()


# --------------------------------------------------------------------------- #
# Stress arithmetic — exact against hand computation
# --------------------------------------------------------------------------- #


def test_fee_stress_arithmetic_is_exact():
    # gross 1.00, fees 0.10 total, slippage 0.05, net 0.90.
    trade = mk_trade(0.90, fees=0.10, slippage=0.05, notional=100.0)
    assert trade.net_pnl_usd == pytest.approx(0.90)
    assert trade.fees_usd == pytest.approx(0.10)

    plus25 = apply_cost_stress([trade], StressScenario("fees +25%", fee_multiplier=1.25))
    # 0.90 - 0.25 * 0.10 = 0.875
    assert plus25[0].net_pnl_usd == pytest.approx(0.875)
    assert plus25[0].fees_usd == pytest.approx(0.125)

    plus50 = apply_cost_stress([trade], StressScenario("fees +50%", fee_multiplier=1.50))
    # 0.90 - 0.50 * 0.10 = 0.85
    assert plus50[0].net_pnl_usd == pytest.approx(0.85)
    assert plus50[0].fees_usd == pytest.approx(0.15)


def test_slippage_stress_charges_exactly_one_extra_helping():
    trade = mk_trade(0.90, fees=0.10, slippage=0.05, notional=100.0)
    doubled = apply_cost_stress([trade], StressScenario("slip", slippage_multiplier=2.0))
    # slippage is already inside the fill price, so x2 costs one extra 0.05.
    assert doubled[0].net_pnl_usd == pytest.approx(0.85)
    assert doubled[0].slippage_usd == pytest.approx(0.10)


def test_spread_stress_charges_both_legs_on_notional():
    trade = mk_trade(0.90, fees=0.10, slippage=0.05, notional=100.0)
    worse = apply_cost_stress(
        [trade], StressScenario("spread", spread_extra_bps_per_side=0.5)
    )
    # 100 * 0.5bps * 2 legs = 100 * 0.00005 * 2 = 0.01
    assert worse[0].net_pnl_usd == pytest.approx(0.89)


def test_apply_cost_stress_does_not_mutate_the_original_trades():
    trades = mk_trades([0.5, -0.2], fees=0.10, slippage=0.05)
    before = [t.net_pnl_usd for t in trades]
    apply_cost_stress(trades, StressScenario("fees", fee_multiplier=2.0))
    assert [t.net_pnl_usd for t in trades] == before


# --------------------------------------------------------------------------- #
# stress_suite
# --------------------------------------------------------------------------- #


def test_stress_suite_reports_unevaluated_stresses_as_not_evaluated():
    results = stress_suite(good_arm(), replay=None, equity=EQUITY)
    by_label = {r.label: r for r in results}
    assert set(by_label) == {s.label for s in SCENARIOS}

    analytic = ["fees +25%", "fees +50%", "slippage x2", "worse spread"]
    for label in analytic:
        assert by_label[label].evaluated is True
        assert by_label[label].method == "analytic"

    needs_replay = ["latency x2", "missed fills", "random execution delay"]
    for label in needs_replay:
        result = by_label[label]
        assert result.evaluated is False
        assert result.survived is False, "an unevaluated stress is never a pass"
        assert result.method == "not-evaluated"
        assert "not evaluated" in result.line


def test_stress_suite_uses_an_injected_replay_for_path_dependent_stresses():
    seen: list[str] = []

    def replay(arm, scenario):
        seen.append(scenario.label)
        return mk_trades([0.2] * 30)

    results = {r.label: r for r in stress_suite(good_arm(), replay=replay)}
    assert seen == ["latency x2", "missed fills", "random execution delay"]
    for label in seen:
        assert results[label].evaluated is True
        assert results[label].method == "replay"
        assert results[label].survived is True
    # Analytic stresses are still computed analytically — exact and cheap.
    assert results["fees +25%"].method == "analytic"


def test_stress_suite_accepts_perfstats_from_replay():
    stats = compute_stats(mk_trades([0.2] * 30), equity=EQUITY)
    results = {r.label: r for r in stress_suite(good_arm(), replay=lambda a, s: stats)}
    assert results["latency x2"].evaluated is True
    assert results["latency x2"].stats is stats


def test_a_replay_that_fails_is_reported_as_not_evaluated_not_as_a_pass():
    def broken(arm, scenario):
        raise RuntimeError("no tape for this window")

    results = {r.label: r for r in stress_suite(good_arm(), replay=broken)}
    latency = results["latency x2"]
    assert latency.evaluated is False
    assert latency.survived is False
    assert "RuntimeError" in latency.detail

    empty = {r.label: r for r in stress_suite(good_arm(), replay=lambda a, s: None)}
    assert empty["missed fills"].evaluated is False


def test_stress_suite_is_deterministic():
    arm = good_arm()
    first = [(r.label, r.survived, r.stats.net_pnl) for r in stress_suite(arm)]
    second = [(r.label, r.survived, r.stats.net_pnl) for r in stress_suite(arm)]
    assert first == second


# --------------------------------------------------------------------------- #
# Monte Carlo
# --------------------------------------------------------------------------- #


def test_monte_carlo_on_all_winners_never_ends_below_start():
    result = monte_carlo(mk_trades([0.2] * 25), equity=EQUITY, samples=300, seed=7)
    assert result.prob_below_start == 0.0
    assert result.median_drawdown == 0.0
    assert result.worst_loss_streak == 0
    assert result.samples == 300


def test_monte_carlo_on_all_losers_always_ends_below_start():
    trades = mk_trades([-0.2] * 25)
    result = monte_carlo(trades, equity=EQUITY, samples=300, seed=7)
    assert result.prob_below_start == 1.0
    assert result.worst_loss_streak == 25
    assert result.median_drawdown == pytest.approx(25 * 0.2)


def test_monte_carlo_is_reproducible_under_a_seed():
    trades = mk_trades([0.3, -0.25] * 20)
    a = monte_carlo(trades, equity=EQUITY, samples=400, seed=11)
    b = monte_carlo(trades, equity=EQUITY, samples=400, seed=11)
    c = monte_carlo(trades, equity=EQUITY, samples=400, seed=12)
    assert a == b
    assert (a.p95_drawdown, a.prob_below_start) != (c.p95_drawdown, c.prob_below_start)


def test_monte_carlo_makes_capital_survival_visible():
    """Positive expectancy, big variance: the survival probability is the point."""
    trades = mk_trades([12.0] * 15 + [-10.0] * 15)   # +$30 expected, wide
    result = monte_carlo(trades, equity=EQUITY, samples=800, seed=3)
    assert result.prob_below_start > 0.0
    assert result.p95_drawdown > result.median_drawdown
    assert result.p95_drawdown_pct == pytest.approx(
        result.p95_drawdown / EQUITY * 100.0
    )
    assert "serial correlation" in result.note


def test_monte_carlo_on_an_empty_trade_list_does_not_crash():
    result = monte_carlo([], equity=EQUITY, samples=100)
    assert result == MonteCarloResult(equity=EQUITY, note=result.note)
    assert result.samples == 0
    assert result.prob_below_start == 0.0
    assert "no trades" in result.note


def test_monte_carlo_with_no_samples_is_inert():
    assert monte_carlo(mk_trades([0.1]), equity=EQUITY, samples=0).samples == 0


# --------------------------------------------------------------------------- #
# The gates — one arm per gate, failing exactly that gate
# --------------------------------------------------------------------------- #


def test_a_genuinely_good_arm_passes_every_gate():
    def replay(arm, scenario):
        return mk_trades(good_nets())

    report = evaluate_gates(good_arm(), equity=EQUITY, replay=replay, samples=400)
    assert report.gate_results == {n: True for n in range(1, 8)}
    assert report.passed is True
    assert report.marginal is False
    assert report.verdict == "FINALIST"
    assert len(report.reasons) == 7
    assert all(r.startswith("[PASS]") for r in report.reasons)


def test_a_pass_never_comes_with_an_empty_reason_list():
    report = evaluate_gates(good_arm(), equity=EQUITY, samples=200)
    assert report.reasons
    assert len(report.reasons) == 7
    for n in range(1, 8):
        assert any(f"gate {n} " in line for line in report.reasons)


def test_gate_1_negative_expectancy_is_refused():
    # A negative edge necessarily drags gates 2 and 6 with it — that coupling is
    # real, so this asserts gate 1 is the *cause* and that the independent gates
    # (window majority, sample size) still pass.
    nets = [0.10] * 20 + [-0.20] * 20
    windows = [
        PerfStats(trades=10, net_pnl=0.5),
        PerfStats(trades=10, net_pnl=0.5),
        PerfStats(trades=20, net_pnl=-3.0),
    ]
    report = evaluate_gates(make_arm(mk_trades(nets), windows=windows), samples=200)
    assert report.gate_results[1] is False
    assert report.gate_results[5] is True
    assert report.gate_results[7] is True
    assert report.passed is False
    assert "[FAIL] gate 1" in report.reasons[0]


def test_gate_2_a_marginal_profit_factor_is_reported_but_never_promoted():
    nets = [0.20] * 20 + [-0.18] * 20            # PF = 4.00 / 3.60 = 1.11
    # Costs deliberately small, so gate 6 stays out of the way and gate 2 is
    # unambiguously the only thing that fails.
    trades = mk_trades(nets, fees=0.01, slippage=0.002)
    report = evaluate_gates(make_arm(trades), samples=200)
    assert report.gate_results[2] is False
    assert {n: ok for n, ok in report.gate_results.items() if n != 2} == {
        1: True, 3: True, 4: True, 5: True, 6: True, 7: True
    }
    assert report.marginal is True
    assert report.verdict == "MARGINAL"
    assert report.passed is False
    assert "MARGINAL" in report.reasons[1]


def test_gate_3_a_drawdown_a_500_dollar_account_cannot_take():
    nets = [-10.0] * 15 + [20.0] * 15            # $150 drawdown before recovery
    report = evaluate_gates(make_arm(mk_trades(nets)), equity=EQUITY, samples=400)
    assert report.gate_results[3] is False
    assert {n: ok for n, ok in report.gate_results.items() if n != 3} == {
        1: True, 2: True, 4: True, 5: True, 6: True, 7: True
    }
    assert "150.00" in report.reasons[2]


def test_gate_3_also_fails_on_capital_survival_alone():
    """Small drawdown, positive expectancy, coin-flip odds of ending down."""
    nets = [0.30] * 16 + [-0.29] * 16
    arm = make_arm(mk_trades(nets))
    report = evaluate_gates(arm, equity=EQUITY, samples=600, max_drawdown_pct=99.0)
    mc = report.monte_carlo
    assert mc is not None
    # Seeded and therefore exact: ~45% of resampled histories end below $500.
    assert mc.prob_below_start == pytest.approx(0.4467, abs=1e-3)
    assert mc.p95_drawdown < 5.0, "the drawdown itself is tiny — survival is the issue"
    assert report.gate_results[3] is False
    assert "capital survival fails" in report.reasons[2]
    assert "probability of ending below" in report.reasons[2]


def test_gate_4_profitable_only_because_of_one_enormous_trade():
    nets = [20.0] + [-0.20] * 39                 # net +12.20, all of it one trade
    report = evaluate_gates(make_arm(mk_trades(nets)), equity=EQUITY, samples=400)
    assert report.gate_results[4] is False
    assert {n: ok for n, ok in report.gate_results.items() if n != 4} == {
        1: True, 2: True, 3: True, 5: True, 6: True, 7: True
    }
    assert "removing the single best trade" in report.reasons[3]
    assert "one trade wearing a strategy's name" in report.reasons[3]


def test_gate_5_positive_overall_but_negative_in_most_windows():
    windows = [
        PerfStats(trades=12, net_pnl=-0.60),
        PerfStats(trades=12, net_pnl=-0.40),
        PerfStats(trades=16, net_pnl=4.00),
    ]
    report = evaluate_gates(good_arm(windows=windows), equity=EQUITY, samples=200)
    assert report.gate_results[5] is False
    assert {n: ok for n, ok in report.gate_results.items() if n != 5} == {
        1: True, 2: True, 3: True, 4: True, 6: True, 7: True
    }
    assert "1/3 walk-forward windows net positive" in report.reasons[4]
    assert "not repeatable across time" in report.reasons[4]


def test_gate_5_fails_when_no_windows_were_reported_at_all():
    report = evaluate_gates(good_arm(windows=[]), samples=100)
    assert report.gate_results[5] is False
    assert "no walk-forward windows" in report.reasons[4]


def test_gate_6_an_edge_that_evaporates_when_fees_rise_25_percent_is_fragile():
    # Fat fees, thin edge: +25% of the fee bill is larger than the whole net.
    nets = [0.30] * 20 + [-0.20] * 20            # net +2.00, PF 1.50
    trades = mk_trades(nets, fees=0.25, slippage=0.0, notional=20.0)
    report = evaluate_gates(make_arm(trades), equity=EQUITY, samples=300)
    assert report.gate_results[6] is False
    assert {n: ok for n, ok in report.gate_results.items() if n != 6} == {
        1: True, 2: True, 3: True, 4: True, 5: True, 7: True
    }
    gate6 = report.reasons[5]
    assert "fees +25%" in gate6
    assert "FRAGILE" in gate6
    assert "the edge evaporates when fees rise 25%" in gate6
    assert any("fragile" in c.lower() for c in report.caveats)


def test_gate_6_names_the_stresses_it_could_not_evaluate():
    report = evaluate_gates(good_arm(), equity=EQUITY, samples=200)
    gate6 = report.reasons[5]
    assert "3 not evaluated (no replay supplied)" in gate6
    assert "latency x2" in gate6 and "missed fills" in gate6
    assert "neither pass nor fail" in gate6
    assert any("not evaluated" in c for c in report.caveats)


def test_gate_7_too_few_out_of_sample_trades():
    report = evaluate_gates(make_arm(mk_trades(good_nets()[:20])), samples=200)
    assert report.gate_results[7] is False
    assert {n: ok for n, ok in report.gate_results.items() if n != 7} == {
        1: True, 2: True, 3: True, 4: True, 5: True, 6: True
    }
    assert f"need >= {MIN_OOS_TRADES}" in report.reasons[6]
    assert "indistinguishable from noise" in report.reasons[6]


def test_gates_run_on_an_arm_with_no_trades_without_crashing():
    arm = make_arm([])
    report = evaluate_gates(arm, equity=EQUITY, samples=200)
    assert report.passed is False
    assert len(report.reasons) == 7
    assert report.monte_carlo is None
    assert report.gate_results[4] is False
    assert "needs at least 2 trades" in report.reasons[3]
    assert format_gate_report(report)


def test_the_fragility_scenario_is_the_fee_stress_by_name():
    assert FRAGILITY_SCENARIO == "fees +25%"
    assert FRAGILITY_SCENARIO in {s.label for s in SCENARIOS}


# --------------------------------------------------------------------------- #
# finalists() and the report
# --------------------------------------------------------------------------- #


def test_finalists_returns_only_arms_that_passed():
    def replay(arm, scenario):
        return mk_trades(good_nets())

    winner = good_arm(name="good/BTCUSD/900s")
    loser = make_arm(mk_trades([0.10] * 20 + [-0.20] * 20), name="bad/BTCUSD/900s")
    passed = finalists([winner, loser], equity=EQUITY, replay=replay, samples=200)
    assert [r.candidate_key for r in passed] == ["good/BTCUSD/900s"]


def test_finalists_can_be_empty_and_that_is_a_first_class_result():
    loser = make_arm(mk_trades([0.10] * 20 + [-0.20] * 20), name="bad/BTCUSD/900s")
    assert finalists([loser], equity=EQUITY, samples=200) == []

    reports = evaluate_all([loser], equity=EQUITY, samples=200)
    rendered = format_gate_reports(reports)
    assert "NO STRATEGY PASSED THE PROMOTION GATES" in rendered
    assert "do not lower the gates" in rendered
    assert "bad/BTCUSD/900s" in rendered      # losers are still reported


def test_finalists_on_no_arms_at_all_is_clean():
    assert finalists([], equity=EQUITY) == []
    assert "NO ARMS WERE EVALUATED" in format_gate_reports([])


def test_the_report_states_the_marginal_count_when_nothing_passes():
    marginal = make_arm(
        mk_trades([0.20] * 20 + [-0.18] * 20), name="marginal/BTCUSD/900s"
    )
    rendered = format_gate_reports(evaluate_all([marginal], equity=EQUITY, samples=200))
    assert "NO STRATEGY PASSED" in rendered
    assert "1 were MARGINAL" in rendered


def test_format_gate_report_shows_every_gate_the_stresses_and_the_monte_carlo():
    rendered = format_gate_report(evaluate_gates(good_arm(), equity=EQUITY, samples=200))
    for n in range(1, 8):
        assert f"gate {n} " in rendered
    assert "stress:" in rendered
    assert "not evaluated" in rendered
    assert "P(end below $500)" in rendered
    assert "monte carlo" in rendered
