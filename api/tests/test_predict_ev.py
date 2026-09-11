"""Tests for ``app.predict.ev`` — the expected-value gate.

The module's whole job is to say **no** correctly, so the tests are built the
same way round: every rejection reason gets a case that trips it and only it,
and the headline test is not "a good trade is accepted" but "a 70% win rate with
a bad payoff is refused".

Every number below is checked by hand in the test that uses it.  Nothing is
random, nothing is seeded from the clock, nothing touches the network.
"""

from __future__ import annotations

import dataclasses
import inspect
import math

import pytest

from app.predict.ev import (
    DEFAULT_GATE,
    DEFAULT_SIZING,
    DEFAULT_THRESHOLDS,
    EV_GATE_RETURNS_PCT,
    MAX_KELLY_FRACTION,
    MIN_THRESHOLD_TRADES,
    REJECT_REASONS,
    SILENCE_COUNT_RATIO,
    EVContext,
    EVGate,
    EVInputs,
    EVReject,
    EVResult,
    SizingConfig,
    count_rejections,
    edge_confidence_multiplier,
    ev_gate_sweep,
    expected_value,
    format_ev_gate_reports,
    format_rejection_counts,
    format_threshold_reports,
    kelly_fraction_of_equity,
    position_size,
    select_threshold,
    shrink_probability,
    threshold_search,
    uncertainty_budget,
)

NOTIONAL = 100.0


# --------------------------------------------------------------------------- #
# The frozen contract
# --------------------------------------------------------------------------- #


def test_frozen_contract_fields_and_order():
    """docs/PHASE4_SPEC.md pins these names and this order; extras are additive."""
    names = [f.name for f in dataclasses.fields(EVInputs)]
    assert names[:7] == [
        "p_win",
        "p_loss",
        "p_timeout",
        "avg_net_win_usd",
        "avg_net_loss_usd",
        "cost_usd",
        "uncertainty_usd",
    ]
    # The seven contract fields still construct positionally.
    inputs = EVInputs(0.68, 0.32, 0.0, 0.62, 0.31, 0.0, 0.0)
    assert inputs.p_win == 0.68

    result_names = [f.name for f in dataclasses.fields(EVResult)]
    assert result_names[:5] == [
        "expected_value_usd",
        "expected_value_bps",
        "margin_usd",
        "eligible",
        "reject",
    ]


def test_nine_rejection_reasons_exactly():
    assert len(EVReject) == 9
    assert set(REJECT_REASONS) == {r.value for r in EVReject}
    assert len(REJECT_REASONS) == 9
    assert set(REJECT_REASONS) == {
        "LOW_PROBABILITY",
        "NEGATIVE_EV",
        "COST_TOO_HIGH",
        "MODEL_UNCERTAIN",
        "CORRELATED_RISK",
        "SPREAD_TOO_WIDE",
        "NO_LIQUIDITY",
        "STALE_DATA",
        "REGIME_NOT_SUPPORTED",
    }


def test_swept_grids_match_the_brief():
    assert DEFAULT_THRESHOLDS == (0.55, 0.575, 0.60, 0.625, 0.65, 0.675, 0.70, 0.75)
    assert EV_GATE_RETURNS_PCT == (0.02, 0.05, 0.10, 0.15, 0.20)


# --------------------------------------------------------------------------- #
# The EV formula
# --------------------------------------------------------------------------- #


def test_briefs_worked_example():
    """0.68 × 0.62 − 0.32 × 0.31 = 0.4216 − 0.0992 = +$0.3224."""
    inputs = EVInputs(0.68, 0.32, 0.0, 0.62, 0.31, 0.0, 0.0)
    result = expected_value(inputs)

    assert result.expected_value_usd == pytest.approx(0.3224, abs=1e-12)
    assert round(result.expected_value_usd, 2) == 0.32
    # $0.3224 on a $100 position is 32.24 bps.
    assert result.expected_value_bps == pytest.approx(32.24, abs=1e-9)
    assert result.margin_usd == pytest.approx(0.3224, abs=1e-12)
    assert result.eligible is True
    assert result.reject is None


def test_timeout_leg_and_cost_are_subtracted():
    """EV = 0.6×0.50 − 0.3×0.40 − 0.1×0.05 − 0.03 = 0.30 − 0.12 − 0.005 − 0.03."""
    inputs = EVInputs(
        p_win=0.60,
        p_loss=0.30,
        p_timeout=0.10,
        avg_net_win_usd=0.50,
        avg_net_loss_usd=0.40,
        cost_usd=0.03,
        uncertainty_usd=0.0,
        timeout_penalty_usd=0.05,
    )
    result = expected_value(inputs)
    assert result.expected_value_usd == pytest.approx(0.145, abs=1e-12)
    assert result.gross_expected_value_usd == pytest.approx(0.175, abs=1e-12)


def test_timeout_penalty_defaults_to_the_round_trip_cost():
    """A timeout exits flat in gross terms and still pays the costs."""
    inputs = EVInputs(0.5, 0.3, 0.2, 0.40, 0.40, 0.04, 0.0)
    assert inputs.timeout_cost_usd == 0.04
    # 0.5*0.40 - 0.3*0.40 - 0.2*0.04 - 0.04 = 0.20 - 0.12 - 0.008 - 0.04
    assert expected_value(inputs).expected_value_usd == pytest.approx(0.032, abs=1e-12)


def test_probabilities_must_sum_to_one():
    with pytest.raises(ValueError, match="must be 1.0"):
        EVInputs(0.6, 0.3, 0.0, 0.5, 0.5, 0.0, 0.0)


def test_average_loss_is_a_positive_magnitude():
    with pytest.raises(ValueError, match="positive magnitude"):
        EVInputs(0.6, 0.4, 0.0, 0.5, -0.5, 0.0, 0.0)


def test_from_probability_fills_the_remainder():
    inputs = EVInputs.from_probability(
        0.68, avg_net_win_usd=0.62, avg_net_loss_usd=0.31
    )
    assert inputs.p_loss == pytest.approx(0.32)
    assert expected_value(inputs).expected_value_usd == pytest.approx(0.3224)


# --------------------------------------------------------------------------- #
# The headline: win rate is not the objective
# --------------------------------------------------------------------------- #


def test_high_win_rate_with_a_bad_payoff_is_rejected():
    """Right 70% of the time, +$0.10 per win and −$0.40 per loss.

    0.70 × 0.10 − 0.30 × 0.40 = 0.07 − 0.12 = **−$0.05**.  The win rate clears
    the probability floor comfortably; the strategy still loses money, and the
    module has to say so rather than wave it through.
    """
    inputs = EVInputs.from_probability(
        0.70, avg_net_win_usd=0.10, avg_net_loss_usd=0.40
    )
    result = expected_value(inputs)

    assert inputs.p_win > DEFAULT_GATE.min_probability  # not a probability problem
    assert result.expected_value_usd == pytest.approx(-0.05, abs=1e-12)
    assert result.eligible is False
    assert result.reject == EVReject.NEGATIVE_EV.value


def test_lower_win_rate_with_a_good_payoff_is_accepted():
    """The mirror image: 58% right, +$0.62 / −$0.31, and it is a trade."""
    inputs = EVInputs.from_probability(
        0.58, avg_net_win_usd=0.62, avg_net_loss_usd=0.31, uncertainty_usd=0.05
    )
    result = expected_value(inputs)
    # 0.58*0.62 - 0.42*0.31 = 0.3596 - 0.1302 = 0.2294
    assert result.expected_value_usd == pytest.approx(0.2294, abs=1e-12)
    assert result.eligible is True


# --------------------------------------------------------------------------- #
# Uncertainty
# --------------------------------------------------------------------------- #


def test_edge_smaller_than_its_error_bar_is_not_a_trade():
    """+0.08% against ±0.07% on a $100 position leaves $0.01 of margin."""
    inputs = EVInputs.from_probability(
        0.60,
        avg_net_win_usd=0.50,
        avg_net_loss_usd=0.55,
        uncertainty_usd=0.07,
        notional_usd=NOTIONAL,
    )
    result = expected_value(inputs)

    # 0.6*0.50 - 0.4*0.55 = 0.30 - 0.22 = 0.08  →  +0.08% of $100
    assert result.expected_value_usd == pytest.approx(0.08, abs=1e-12)
    assert result.expected_value_bps == pytest.approx(8.0, abs=1e-9)
    assert result.margin_usd == pytest.approx(0.01, abs=1e-12)
    assert result.eligible is False
    assert result.reject == EVReject.MODEL_UNCERTAIN.value
    # The EV itself was positive and cleared the return floor — only the
    # uncertainty stopped it.
    assert result.expected_value_usd > 0


def test_a_large_edge_against_a_small_error_bar_is_a_trade():
    """+0.40% against ±0.05% leaves $0.35 of margin."""
    inputs = EVInputs.from_probability(
        0.70,
        avg_net_win_usd=0.70,
        avg_net_loss_usd=0.30,
        uncertainty_usd=0.05,
        notional_usd=NOTIONAL,
    )
    result = expected_value(inputs)

    # 0.7*0.70 - 0.3*0.30 = 0.49 - 0.09 = 0.40  →  +0.40% of $100
    assert result.expected_value_usd == pytest.approx(0.40, abs=1e-12)
    assert result.expected_value_bps == pytest.approx(40.0, abs=1e-9)
    assert result.margin_usd == pytest.approx(0.35, abs=1e-12)
    assert result.eligible is True
    assert result.reject is None


def test_the_same_edge_flips_on_the_uncertainty_alone():
    """Identical payoff, two error bars, two different answers."""
    build = lambda unc: EVInputs.from_probability(  # noqa: E731
        0.62,
        avg_net_win_usd=0.60,
        avg_net_loss_usd=0.40,
        uncertainty_usd=unc,
        notional_usd=NOTIONAL,
    )
    # 0.62*0.60 - 0.38*0.40 = 0.372 - 0.152 = 0.22
    tight = expected_value(build(0.01))
    loose = expected_value(build(0.21))
    assert tight.expected_value_usd == pytest.approx(loose.expected_value_usd)
    assert tight.eligible is True
    assert loose.eligible is False
    assert loose.reject == EVReject.MODEL_UNCERTAIN.value


def test_uncertainty_multiple_demands_more_cover():
    inputs = EVInputs.from_probability(
        0.70, avg_net_win_usd=0.70, avg_net_loss_usd=0.30, uncertainty_usd=0.30
    )
    assert expected_value(inputs).eligible is True  # 0.40 - 0.30 = 0.10 margin
    strict = EVGate(uncertainty_multiple=2.0)
    result = expected_value(inputs, gate=strict)
    assert result.margin_usd == pytest.approx(-0.20)
    assert result.reject == EVReject.MODEL_UNCERTAIN.value


def test_uncertainty_budget_names_its_three_sources():
    budget = uncertainty_budget(
        notional_usd=NOTIONAL,
        slippage_std_bps=3.0,
        calibration_error=0.05,
        probability_std=0.05,
        avg_net_win_usd=0.70,
        avg_net_loss_usd=0.30,
    )
    assert set(budget.parts) == {"slippage", "calibration", "sample"}
    # slippage: 3 bps of $100 = $0.03
    assert budget.slippage_usd == pytest.approx(0.03)
    # calibration / sample: error × payoff span (0.70 + 0.30 = 1.00)
    assert budget.calibration_usd == pytest.approx(0.05)
    assert budget.sample_usd == pytest.approx(0.05)
    assert budget.total_usd == pytest.approx(math.sqrt(0.03**2 + 0.05**2 + 0.05**2))
    # Quadrature is smaller than the all-go-wrong-together worst case.
    worst = uncertainty_budget(
        notional_usd=NOTIONAL,
        slippage_std_bps=3.0,
        calibration_error=0.05,
        probability_std=0.05,
        avg_net_win_usd=0.70,
        avg_net_loss_usd=0.30,
        combine="sum",
    )
    assert worst.total_usd == pytest.approx(0.13)
    assert budget.total_usd < worst.total_usd


def test_uncertainty_budget_scales_with_z():
    one = uncertainty_budget(notional_usd=NOTIONAL, slippage_std_bps=4.0)
    two = uncertainty_budget(notional_usd=NOTIONAL, slippage_std_bps=4.0, z=2.0)
    assert two.total_usd == pytest.approx(2.0 * one.total_usd)


# --------------------------------------------------------------------------- #
# Bayesian shrinkage
# --------------------------------------------------------------------------- #


def test_three_wins_from_three_trades_is_not_a_certainty():
    """The whole reason this function exists."""
    est = shrink_probability(3, 3)
    assert est.raw == 1.0
    # (3 + 10) / (3 + 20) = 0.5652...
    assert est.shrunk == pytest.approx(13 / 23)
    assert est.shrunk < 0.60
    assert est.shrinkage < 0  # pulled down, hard
    # And it is nowhere near good enough to trade a thin payoff on.
    inputs = EVInputs.from_probability(
        est.shrunk, avg_net_win_usd=0.20, avg_net_loss_usd=0.20
    )
    # 0.5652*0.20 - 0.4348*0.20 = $0.026 — below the 5 bps floor on $100.
    assert expected_value(inputs).eligible is False


def test_raw_frequency_would_have_passed_where_the_shrunk_one_fails():
    raw = EVInputs.from_probability(
        1.0, avg_net_win_usd=0.20, avg_net_loss_usd=0.20, notional_usd=NOTIONAL
    )
    assert expected_value(raw).eligible is True  # $0.20, if you believe 3 trades
    est = shrink_probability(3, 3)
    shrunk = EVInputs.from_probability(
        est.shrunk,
        avg_net_win_usd=0.20,
        avg_net_loss_usd=0.20,
        notional_usd=NOTIONAL,
    )
    assert expected_value(shrunk).eligible is False


def test_shrinkage_fades_as_the_sample_grows():
    small = shrink_probability(30, 50)
    large = shrink_probability(300, 500)
    assert small.raw == large.raw == 0.6
    assert abs(small.shrinkage) > abs(large.shrinkage)
    assert large.shrunk == pytest.approx(310 / 520)
    # The posterior narrows with n, which is what feeds the uncertainty budget.
    assert large.std < small.std


def test_shrinkage_rejects_impossible_samples():
    with pytest.raises(ValueError):
        shrink_probability(4, 3)
    with pytest.raises(ValueError):
        shrink_probability(-1, 3)
    assert shrink_probability(0, 0).shrunk == 0.5  # no evidence → the prior


# --------------------------------------------------------------------------- #
# Rejection reasons: each reachable, exactly one returned
# --------------------------------------------------------------------------- #


def _good_inputs(**kw) -> EVInputs:
    """A setup that passes everything, so a single change isolates one reason."""
    base = dict(
        avg_net_win_usd=0.70,
        avg_net_loss_usd=0.30,
        uncertainty_usd=0.02,
        notional_usd=NOTIONAL,
    )
    base.update(kw)
    p_win = base.pop("p_win", 0.70)
    return EVInputs.from_probability(p_win, **base)


def _good_context(**kw) -> EVContext:
    base = dict(
        spread_bps=0.5,
        depth_usd=50_000.0,
        data_age_ms=200,
        regime="VOLATILITY_EXPANSION",
        correlated_risk_pct=0.5,
    )
    base.update(kw)
    return EVContext(**base)


GATE_WITH_REGIMES = EVGate(supported_regimes=("VOLATILITY_EXPANSION", "TREND"))


def test_the_baseline_setup_is_eligible():
    result = expected_value(
        _good_inputs(), gate=GATE_WITH_REGIMES, context=_good_context()
    )
    assert result.eligible is True
    assert result.reject is None


@pytest.mark.parametrize(
    ("reason", "inputs_kw", "context_kw"),
    [
        (EVReject.STALE_DATA, {}, {"data_age_ms": 30_000}),
        (EVReject.SPREAD_TOO_WIDE, {}, {"spread_bps": 12.0}),
        (EVReject.NO_LIQUIDITY, {}, {"depth_usd": 100.0}),
        (EVReject.REGIME_NOT_SUPPORTED, {}, {"regime": "DEAD_TAPE"}),
        (EVReject.CORRELATED_RISK, {}, {"correlated_risk_pct": 3.0}),
        (EVReject.LOW_PROBABILITY, {"p_win": 0.51}, {}),
        (
            EVReject.NEGATIVE_EV,
            {"p_win": 0.70, "avg_net_win_usd": 0.10, "avg_net_loss_usd": 0.40},
            {},
        ),
        (
            EVReject.COST_TOO_HIGH,
            {
                "p_win": 0.65,
                "avg_net_win_usd": 0.50,
                "avg_net_loss_usd": 0.20,
                "cost_usd": 0.25,
            },
            {},
        ),
        (EVReject.MODEL_UNCERTAIN, {"uncertainty_usd": 0.39}, {}),
    ],
)
def test_every_rejection_reason_is_reachable_and_unique(reason, inputs_kw, context_kw):
    result = expected_value(
        _good_inputs(**inputs_kw),
        gate=GATE_WITH_REGIMES,
        context=_good_context(**context_kw),
    )
    assert result.eligible is False
    assert result.reject == reason.value
    assert result.reject_reason is reason
    assert result.detail  # a rejection always explains itself
    # "Exactly one" is structural: ``reject`` is a single field, and it is a
    # member of the frozen vocabulary.
    assert result.reject in REJECT_REASONS


def test_cost_too_high_is_distinguished_from_negative_ev():
    """Same payoff, one with costs.  Different diagnoses, both refusals."""
    free = _good_inputs(p_win=0.65, avg_net_win_usd=0.50, avg_net_loss_usd=0.20)
    # 0.65*0.50 - 0.35*0.20 = 0.325 - 0.070 = 0.255
    assert expected_value(free).expected_value_usd == pytest.approx(0.255)
    assert expected_value(free).eligible is True

    costly = _good_inputs(
        p_win=0.65, avg_net_win_usd=0.50, avg_net_loss_usd=0.20, cost_usd=0.25
    )
    result = expected_value(costly)
    assert result.expected_value_usd == pytest.approx(0.005, abs=1e-12)
    assert result.gross_expected_value_usd == pytest.approx(0.255)
    assert result.reject == EVReject.COST_TOO_HIGH.value


def test_precedence_is_fixed_when_several_reasons_apply():
    """Everything is wrong at once; the first binding cause is what is reported."""
    broken = expected_value(
        _good_inputs(
            p_win=0.51,
            avg_net_win_usd=0.01,
            avg_net_loss_usd=0.90,
            uncertainty_usd=5.0,
        ),
        gate=GATE_WITH_REGIMES,
        context=_good_context(
            data_age_ms=60_000,
            spread_bps=40.0,
            depth_usd=1.0,
            regime="DEAD_TAPE",
            correlated_risk_pct=9.0,
        ),
    )
    assert broken.reject == EVReject.STALE_DATA.value
    assert REJECT_REASONS[0] == EVReject.STALE_DATA.value

    # Remove the staleness and the next reason in the order takes over.
    next_up = expected_value(
        _good_inputs(p_win=0.51, avg_net_win_usd=0.01, avg_net_loss_usd=0.90),
        gate=GATE_WITH_REGIMES,
        context=_good_context(spread_bps=40.0, depth_usd=1.0, regime="DEAD_TAPE"),
    )
    assert next_up.reject == EVReject.SPREAD_TOO_WIDE.value


def test_low_probability_outranks_a_spectacular_payoff():
    """A coin flip on a 50:1 payoff is still refused for being a coin flip."""
    result = expected_value(
        _good_inputs(p_win=0.50, avg_net_win_usd=5.00, avg_net_loss_usd=0.10)
    )
    assert result.expected_value_usd > 2.0
    assert result.reject == EVReject.LOW_PROBABILITY.value


def test_unchecked_context_fields_are_never_reported_as_passed():
    """A ``None`` field is not checked, and not silently treated as fine."""
    result = expected_value(_good_inputs(), context=EVContext())
    assert result.eligible is True
    # With no supported-regime list configured, a regime cannot be rejected.
    result = expected_value(_good_inputs(), context=EVContext(regime="ANYTHING"))
    assert result.eligible is True


def test_rejections_are_countable_with_every_reason_present():
    results = [
        expected_value(_good_inputs()),
        expected_value(_good_inputs(p_win=0.52)),
        expected_value(_good_inputs(p_win=0.52)),
        expected_value(
            _good_inputs(avg_net_win_usd=0.10, avg_net_loss_usd=0.40)
        ),
        expected_value(_good_inputs(), context=_good_context(data_age_ms=99_999)),
    ]
    counts = count_rejections(results)

    assert set(counts) == set(REJECT_REASONS) | {"ELIGIBLE"}
    assert counts["ELIGIBLE"] == 1
    assert counts["LOW_PROBABILITY"] == 2
    assert counts["NEGATIVE_EV"] == 1
    assert counts["STALE_DATA"] == 1
    # The zeros are the point: they are what separates "found no edge" from
    # "refuses everything".
    assert counts["NO_LIQUIDITY"] == 0
    assert counts["CORRELATED_RISK"] == 0
    assert sum(counts.values()) == len(results)

    text = format_rejection_counts(counts)
    for reason in REJECT_REASONS:
        assert reason in text
    assert "ELIGIBLE" in text


def test_the_two_failure_modes_look_different_in_the_counts():
    no_edge = count_rejections(
        [
            expected_value(_good_inputs(avg_net_win_usd=0.10, avg_net_loss_usd=0.40))
            for _ in range(20)
        ]
    )
    refuses_everything = count_rejections(
        [
            expected_value(_good_inputs(), context=_good_context(data_age_ms=99_999))
            for _ in range(20)
        ]
    )
    assert no_edge["NEGATIVE_EV"] == 20 and no_edge["STALE_DATA"] == 0
    assert refuses_everything["STALE_DATA"] == 20
    assert refuses_everything["NEGATIVE_EV"] == 0
    assert no_edge != refuses_everything


# --------------------------------------------------------------------------- #
# Threshold search (item 13)
# --------------------------------------------------------------------------- #


def _win_rate_disagrees_with_profit():
    """Two populations whose ranking flips depending on what you select on.

    * 60 candidates at p = 0.56: 30 wins of +40 bps, 30 losses of −20 bps.
      Win rate 50%, net **+600 bps = +$6.00** on a $100 position.
    * 40 candidates at p = 0.76: 36 wins of +2 bps, 4 losses of −10 bps.
      Win rate 90%, net **+32 bps = +$0.32**.

    Selecting on win rate picks 0.75.  Selecting on net profit picks 0.55.
    """
    p: list[float] = []
    y: list[float] = []
    rets: list[float] = []
    for i in range(60):
        p.append(0.56)
        win = i < 30
        y.append(1.0 if win else 0.0)
        rets.append(40.0 if win else -20.0)
    for i in range(40):
        p.append(0.76)
        win = i < 36
        y.append(1.0 if win else 0.0)
        rets.append(2.0 if win else -10.0)
    return p, y, rets


def test_threshold_search_reports_every_column_the_reader_needs():
    p, y, rets = _win_rate_disagrees_with_profit()
    reports = threshold_search(p, y, rets, notional_usd=NOTIONAL)

    assert [r.threshold for r in reports] == sorted(DEFAULT_THRESHOLDS)
    base = reports[0]
    assert base.threshold == 0.55
    assert base.trades == 100
    assert base.net_profit_usd == pytest.approx(6.32, abs=1e-9)
    assert base.win_rate == pytest.approx(0.66)
    # gross win 30*0.40 + 36*0.02 = 12.72 ; gross loss 30*0.20 + 4*0.10 = 6.40
    assert base.profit_factor == pytest.approx(12.72 / 6.40)
    assert base.max_drawdown_usd >= 0.0
    assert 0.0 <= base.calibration_error <= 1.0
    assert base.reliability  # reliability bins are populated

    top = reports[-1]
    assert top.threshold == 0.75
    assert top.trades == 40
    assert top.win_rate == pytest.approx(0.90)
    assert top.net_profit_usd == pytest.approx(0.32, abs=1e-9)


def test_selection_is_on_net_profit_not_win_rate():
    p, y, rets = _win_rate_disagrees_with_profit()
    reports = threshold_search(p, y, rets, notional_usd=NOTIONAL)

    top_win_rate = max(r.win_rate for r in reports)
    win_rate_leaders = {r.threshold for r in reports if r.win_rate == top_win_rate}
    best_by_net = max(reports, key=lambda r: r.net_profit_usd)

    # Win rate peaks at 90% on the thin, near-worthless population...
    assert top_win_rate == pytest.approx(0.90)
    assert 0.75 in win_rate_leaders
    # ...and net profit peaks somewhere else entirely.  They disagree.
    assert best_by_net.threshold == 0.55
    assert best_by_net.threshold not in win_rate_leaders
    assert best_by_net.win_rate == pytest.approx(0.66)

    choice = select_threshold(reports)
    assert choice.refused is False
    assert choice.threshold == 0.55  # net profit wins
    assert choice.threshold not in win_rate_leaders
    assert any("win rate peaks" in w for w in choice.warnings)


def test_selection_refuses_a_threshold_with_too_few_trades():
    """20 candidates, all above every threshold: nothing reaches 30 trades."""
    p = [0.80] * 20
    y = [1.0] * 14 + [0.0] * 6
    rets = [50.0] * 14 + [-10.0] * 6
    reports = threshold_search(p, y, rets, notional_usd=NOTIONAL)

    assert all(r.trades == 20 for r in reports)
    assert all(r.net_profit_usd > 0 for r in reports)  # it *looks* great
    assert all(not r.enough_trades for r in reports)

    choice = select_threshold(reports)
    assert choice.refused is True
    assert choice.threshold is None
    assert str(MIN_THRESHOLD_TRADES) in choice.reason
    assert set(choice.too_few_trades) == set(DEFAULT_THRESHOLDS)


def test_a_thin_but_gorgeous_threshold_loses_to_a_thick_ordinary_one():
    """29 trades at +$1.00 each cannot be selected; 40 at +$0.10 can."""
    p = [0.80] * 29 + [0.60] * 40
    y = [1.0] * 29 + [1.0] * 40
    rets = [100.0] * 29 + [10.0] * 40
    reports = threshold_search(
        p, y, rets, thresholds=(0.55, 0.75), notional_usd=NOTIONAL
    )
    thin = next(r for r in reports if r.threshold == 0.75)
    thick = next(r for r in reports if r.threshold == 0.55)
    assert thin.trades == 29 and thin.net_profit_usd == pytest.approx(29.0)
    assert thick.trades == 69 and thick.net_profit_usd == pytest.approx(33.0)

    choice = select_threshold(reports)
    assert choice.threshold == 0.55
    assert 0.75 in choice.too_few_trades


def _phase3_shaped():
    """The Phase 3 trap, reproduced exactly, in probability coordinates.

    * 160 candidates at p = 0.56, in repeating blocks of four summing to
      −64.5 bps → expectancy −16.125 bps.
    * 40 candidates at p = 0.76, in blocks of four summing to −58.0 bps →
      expectancy −14.5 bps = **−$0.145**.

    Pooled at threshold 0.55: 200 trades, −3160 bps → **−$0.158**.  Raising the
    threshold to 0.75 cuts the count **5×** and buys **$0.013** of expectancy —
    the same shape as Phase 3's 24× for $0.013.
    """
    p: list[float] = []
    y: list[float] = []
    rets: list[float] = []
    for _ in range(40):
        for r in (30.0, -30.0, -30.0, -34.5):
            p.append(0.56)
            y.append(1.0 if r > 0 else 0.0)
            rets.append(r)
    for _ in range(10):
        for r in (30.0, -30.0, -30.0, -28.0):
            p.append(0.76)
            y.append(1.0 if r > 0 else 0.0)
            rets.append(r)
    return p, y, rets


def test_the_count_expectancy_tradeoff_is_reported_explicitly():
    p, y, rets = _phase3_shaped()
    reports = threshold_search(
        p, y, rets, thresholds=(0.55, 0.75), notional_usd=NOTIONAL
    )
    base = next(r for r in reports if r.threshold == 0.55)
    strict = next(r for r in reports if r.threshold == 0.75)

    assert base.trades == 200
    assert base.expectancy_usd == pytest.approx(-0.158, abs=1e-9)
    assert strict.trades == 40
    assert strict.expectancy_usd == pytest.approx(-0.145, abs=1e-9)

    # The two numbers a reader needs, side by side, without doing arithmetic.
    assert strict.trade_count_ratio == pytest.approx(5.0)
    assert strict.expectancy_gain_usd == pytest.approx(0.013, abs=1e-9)
    assert strict.trade_count_ratio >= SILENCE_COUNT_RATIO
    assert strict.buying_silence is True
    assert base.buying_silence is False

    table = format_threshold_reports(reports)
    assert "buying silence" in table
    assert "Δ exp $" in table


def test_selection_refuses_when_no_threshold_makes_money():
    p, y, rets = _phase3_shaped()
    reports = threshold_search(
        p, y, rets, thresholds=(0.55, 0.75), notional_usd=NOTIONAL
    )
    choice = select_threshold(reports)
    assert choice.refused is True
    assert choice.threshold is None
    assert "lost" in choice.reason
    # And it still names the least-bad row, so the report can quote it.
    assert choice.report is not None and choice.report.threshold == 0.75


def test_a_buying_silence_winner_is_flagged_even_when_selected():
    """Positive money, but bought by refusing almost everything."""
    p = [0.56] * 400 + [0.76] * 50
    y = [1.0, 0.0] * 200 + [1.0] * 40 + [0.0] * 10
    rets = [2.0, -1.0] * 200 + [2.2] * 40 + [-1.0] * 10
    reports = threshold_search(
        p, y, rets, thresholds=(0.55, 0.75), notional_usd=NOTIONAL
    )
    base = next(r for r in reports if r.threshold == 0.55)
    strict = next(r for r in reports if r.threshold == 0.75)
    assert base.trades == 450 and strict.trades == 50
    assert strict.trade_count_ratio == pytest.approx(9.0)
    assert strict.buying_silence is True

    choice = select_threshold(reports)
    assert choice.threshold == 0.55  # more money, and it is the honest row
    assert choice.refused is False


def test_cost_bps_is_charged_per_trade():
    p = [0.70] * 40
    y = [1.0] * 40
    rets = [10.0] * 40
    free = threshold_search(p, y, rets, thresholds=(0.55,), notional_usd=NOTIONAL)[0]
    charged = threshold_search(
        p, y, rets, thresholds=(0.55,), cost_bps=4.0, notional_usd=NOTIONAL
    )[0]
    assert free.net_profit_usd == pytest.approx(40 * 0.10)
    assert charged.net_profit_usd == pytest.approx(40 * 0.06)
    # 40 trades × 4 bps × $100 = $1.60 of cost, exactly.
    assert free.net_profit_usd - charged.net_profit_usd == pytest.approx(1.60)


def test_threshold_search_validates_its_inputs():
    with pytest.raises(ValueError, match="same length"):
        threshold_search([0.6, 0.7], [1.0], [10.0, 10.0])
    with pytest.raises(ValueError, match="must not be empty"):
        threshold_search([0.6], [1.0], [10.0], thresholds=())


# --------------------------------------------------------------------------- #
# The EV gate sweep (item 14)
# --------------------------------------------------------------------------- #


def test_ev_gate_refuses_a_hundred_dollar_position_for_an_expected_penny():
    """0.60 × $0.21 − 0.40 × $0.29 = $0.126 − $0.116 = **$0.01** on $100.

    One basis point.  Every gate in the sweep — even the loosest, +0.02% —
    demands more than that, so no gate takes the trade.
    """
    single = EVInputs.from_probability(
        0.60,
        avg_net_win_usd=0.21,
        avg_net_loss_usd=0.29,
        notional_usd=100.0,
        uncertainty_usd=0.05,
    )
    result = expected_value(single)
    assert result.expected_value_usd == pytest.approx(0.01, abs=1e-12)
    assert result.expected_value_bps == pytest.approx(1.0, abs=1e-9)
    assert result.eligible is False
    assert result.reject == EVReject.NEGATIVE_EV.value
    # The execution uncertainty ($0.05) is five times the edge ($0.01).
    assert single.uncertainty_usd > result.expected_value_usd

    reports = ev_gate_sweep(
        [0.60] * 50,
        [1.0] * 30 + [0.0] * 20,
        [5.0] * 30 + [-5.0] * 20,
        avg_net_win_usd=0.21,
        avg_net_loss_usd=0.29,
        notional_usd=100.0,
        uncertainty_usd=0.05,
    )
    assert [r.min_return_pct for r in reports] == list(EV_GATE_RETURNS_PCT)
    assert all(r.trades == 0 for r in reports)
    assert all(r.rejections["NEGATIVE_EV"] == 50 for r in reports)
    # The loosest gate is still $0.02 on a $100 position — twice the edge.
    assert reports[0].min_ev_usd == pytest.approx(0.02)


def test_the_dollar_floor_scales_with_the_position():
    gate = EVGate(min_expected_return_bps=10.0)  # 0.10%
    assert gate.scaled_min_ev_usd(100.0) == pytest.approx(0.10)
    assert gate.scaled_min_ev_usd(500.0) == pytest.approx(0.50)
    # A bigger position must earn proportionally more, not the same pennies.
    assert gate.scaled_min_ev_usd(1000.0) == pytest.approx(1.00)
    # ...and never less than the absolute floor.
    assert gate.scaled_min_ev_usd(1.0) == pytest.approx(gate.min_expected_value_usd)


def test_ev_gate_sweep_tightens_monotonically():
    p = [0.56] * 40 + [0.62] * 40 + [0.72] * 40
    y = [1.0] * 120
    rets = [12.0] * 120
    reports = ev_gate_sweep(
        p,
        y,
        rets,
        avg_net_win_usd=0.60,
        avg_net_loss_usd=0.30,
        notional_usd=NOTIONAL,
        uncertainty_usd=0.01,
    )
    counts = [r.trades for r in reports]
    assert counts == sorted(counts, reverse=True)  # a tighter gate never takes more
    assert counts[0] > 0
    # Each row carries its own full histogram.
    for r in reports:
        assert set(r.rejections) == set(REJECT_REASONS) | {"ELIGIBLE"}
        assert r.rejections["ELIGIBLE"] == r.trades
        assert sum(r.rejections.values()) == len(p)
    assert "min EV $" in format_ev_gate_reports(reports)


def test_ev_gate_sweep_respects_market_context():
    p = [0.70] * 40
    y = [1.0] * 40
    rets = [30.0] * 40
    wide = [EVContext(spread_bps=25.0)] * 40
    reports = ev_gate_sweep(
        p,
        y,
        rets,
        avg_net_win_usd=0.70,
        avg_net_loss_usd=0.30,
        notional_usd=NOTIONAL,
        contexts=wide,
    )
    assert all(r.trades == 0 for r in reports)
    # And the histogram says *why* — not "no edge".
    assert all(r.rejections["SPREAD_TOO_WIDE"] == 40 for r in reports)
    assert all(r.rejections["NEGATIVE_EV"] == 0 for r in reports)


# --------------------------------------------------------------------------- #
# Position sizing (item 16)
# --------------------------------------------------------------------------- #


def test_default_method_is_fixed_risk():
    assert (
        inspect.signature(position_size).parameters["method"].default == "fixed_risk"
    )
    result = position_size(edge_bps=15.0, confidence=0.65, stop_distance_bps=200.0)
    assert result.method == "fixed_risk"


def test_size_scales_with_edge_and_confidence_but_stays_bounded():
    weak = position_size(edge_bps=4.0, confidence=0.58, stop_distance_bps=400.0)
    strong = position_size(edge_bps=20.0, confidence=0.75, stop_distance_bps=400.0)
    assert 0 < weak.notional_usd < strong.notional_usd
    assert strong.qty_multiplier == pytest.approx(1.0)
    # The multiplier saturates: 10× the edge does not buy 10× the size.
    huge = position_size(edge_bps=2000.0, confidence=0.99, stop_distance_bps=400.0)
    assert huge.notional_usd == pytest.approx(strong.notional_usd)
    assert huge.qty_multiplier <= DEFAULT_SIZING.max_edge_multiplier


def test_size_never_exceeds_its_bounds_anywhere_on_the_grid():
    cfg = DEFAULT_SIZING
    pct_cap = cfg.equity_usd * cfg.max_position_pct_equity / 100.0
    fixed_risk_budget = cfg.equity_usd * cfg.risk_per_trade_pct / 100.0
    for edge in (0.5, 2.0, 8.0, 20.0, 100.0, 5_000.0):
        for confidence in (0.51, 0.55, 0.60, 0.70, 0.80, 0.99):
            for stop in (5.0, 25.0, 100.0, 400.0, 2_000.0):
                r = position_size(
                    edge_bps=edge, confidence=confidence, stop_distance_bps=stop
                )
                assert r.notional_usd <= cfg.max_position_usd + 1e-9
                assert r.notional_usd <= pct_cap + 1e-9
                assert r.risk_usd <= fixed_risk_budget + 1e-9
                assert r.qty_multiplier <= cfg.max_edge_multiplier + 1e-9
                assert r.notional_usd >= 0.0


def test_no_edge_means_no_position():
    assert position_size(
        edge_bps=0.0, confidence=0.90, stop_distance_bps=100.0
    ).notional_usd == 0.0
    assert position_size(
        edge_bps=-5.0, confidence=0.90, stop_distance_bps=100.0
    ).notional_usd == 0.0
    # A coin flip buys nothing either, however loud the edge estimate is.
    assert edge_confidence_multiplier(500.0, 0.50) == 0.0


def test_sizing_cannot_see_a_previous_trade():
    """Martingale is unreachable because the information is not in scope.

    Nothing in the signature or the config can carry a loss streak, a realised
    P&L or a previous outcome.  A loss can shrink the next position — through
    ``equity_usd`` — and can never grow it.
    """
    forbidden = (
        "streak",
        "consecutive",
        "martingale",
        "previous",
        "last_",
        "history",
        "realised",
        "realized",
        "drawdown",
        "recover",
        "double",
    )
    names = list(inspect.signature(position_size).parameters)
    names += [f.name for f in dataclasses.fields(SizingConfig)]
    names += [f.name for f in dataclasses.fields(DEFAULT_SIZING.__class__)]
    for name in names:
        assert not any(token in name.lower() for token in forbidden), name


def test_size_never_increases_after_a_loss():
    equities = [500.0, 497.5, 490.0, 470.0, 401.0]
    sizes = []
    for equity in equities:
        cfg = SizingConfig(equity_usd=equity)
        sizes.append(
            position_size(
                edge_bps=20.0,
                confidence=0.75,
                stop_distance_bps=1_000.0,
                config=cfg,
            ).notional_usd
        )
    assert sizes == sorted(sizes, reverse=True)
    assert sizes[0] > sizes[-1]  # the shrink is real, not a flat clamp
    # Identical inputs give an identical size — no path-dependence at all.
    repeat = position_size(edge_bps=20.0, confidence=0.75, stop_distance_bps=1_000.0)
    assert repeat.notional_usd == sizes[0]


def test_full_and_aggressive_kelly_are_not_reachable():
    assert MAX_KELLY_FRACTION == 0.25
    with pytest.raises(ValueError, match="MAX_KELLY_FRACTION"):
        kelly_fraction_of_equity(0.70, 2.0, fraction=1.0)  # full Kelly
    with pytest.raises(ValueError, match="MAX_KELLY_FRACTION"):
        kelly_fraction_of_equity(0.70, 2.0, fraction=0.50)  # "aggressive"
    with pytest.raises(ValueError, match="MAX_KELLY_FRACTION"):
        position_size(
            edge_bps=20.0,
            confidence=0.70,
            stop_distance_bps=400.0,
            method="fractional_kelly",
            payoff_ratio=2.0,
            kelly_fraction=1.0,
        )
    # And no admissible call can return more than a quarter of the full number.
    for p_win in (0.55, 0.60, 0.70, 0.80, 0.95, 1.0):
        for b in (0.5, 1.0, 2.0, 5.0):
            full = max(0.0, (b * p_win - (1 - p_win)) / b)
            got = kelly_fraction_of_equity(p_win, b)
            assert got <= MAX_KELLY_FRACTION * full + 1e-12
            assert got <= MAX_KELLY_FRACTION + 1e-12


def test_research_kelly_is_still_bounded_by_the_position_clamps():
    cfg = DEFAULT_SIZING
    result = position_size(
        edge_bps=50.0,
        confidence=0.90,
        stop_distance_bps=500.0,
        method="fractional_kelly",
        payoff_ratio=3.0,
    )
    assert result.method == "fractional_kelly"
    assert result.notional_usd <= cfg.max_position_usd + 1e-9
    assert result.bound_by in ("max_position_usd", "max_position_pct_equity")
    assert any("kelly fraction" in note for note in result.notes)


def test_unknown_sizing_method_is_refused():
    with pytest.raises(ValueError, match="fixed_risk"):
        position_size(
            edge_bps=10.0,
            confidence=0.70,
            stop_distance_bps=100.0,
            method="full_kelly",
        )
    with pytest.raises(ValueError, match="payoff_ratio"):
        position_size(
            edge_bps=10.0,
            confidence=0.70,
            stop_distance_bps=100.0,
            method="fractional_kelly",
        )


def test_sizing_config_refuses_to_scale_above_the_fixed_risk_budget():
    with pytest.raises(ValueError, match="leverage on edge"):
        SizingConfig(max_edge_multiplier=2.0)
    with pytest.raises(ValueError, match="risk_per_trade_pct"):
        SizingConfig(risk_per_trade_pct=5.0)


def test_a_position_below_the_exchange_minimum_is_no_position():
    tiny = SizingConfig(equity_usd=500.0, min_notional_usd=10.0)
    result = position_size(
        edge_bps=1.0, confidence=0.55, stop_distance_bps=9_000.0, config=tiny
    )
    assert result.notional_usd == 0.0
    assert result.bound_by == "below_min_notional"


def test_risk_reported_matches_the_sized_position():
    result = position_size(edge_bps=20.0, confidence=0.75, stop_distance_bps=200.0)
    implied = result.notional_usd * 200.0 / 10_000.0
    assert result.risk_usd == pytest.approx(min(2.50, implied))
    assert result.risk_pct_of(500.0) <= DEFAULT_SIZING.risk_per_trade_pct + 1e-9


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def test_everything_is_deterministic():
    p, y, rets = _win_rate_disagrees_with_profit()
    first = threshold_search(p, y, rets, notional_usd=NOTIONAL)
    second = threshold_search(p, y, rets, notional_usd=NOTIONAL)
    assert first == second
    assert select_threshold(first) == select_threshold(second)

    inputs = _good_inputs()
    assert expected_value(inputs) == expected_value(inputs)
