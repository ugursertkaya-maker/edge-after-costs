"""Expected value — the module that decides whether a probability is tradeable.

Phase 3 tuned exits around a weak entry and Phase 4 replaces the entry decision
with a probability.  This file is the layer between the two: it takes a
predicted probability plus a payoff and answers, in **net dollars after every
cost**, whether the trade is worth opening at all.

The single rule everything else follows from:

```
EV = P_win · avg_net_win − P_loss · avg_net_loss − P_timeout · timeout_penalty − cost
```

**Win rate is not the objective.**  A model that is right 70% of the time on
trades that win $0.10 and lose $0.40 has an expected value of −$0.05 and is a
losing strategy.  :func:`expected_value` says so, by name
(``NEGATIVE_EV``), and :func:`threshold_search` selects on out-of-sample net
profit rather than on win rate for the same reason.

Four ideas are load-bearing and are implemented rather than assumed.

1.  **Everything is net.**  ``avg_net_win_usd`` / ``avg_net_loss_usd`` are the
    realised *net* outcomes of the label — entry fee, exit fee, spread and
    slippage already removed (``docs/ECONOMICS.md``: the round trip is 3–6 bps
    and slippage is ~3× fees, so a gross number here would be off by most of
    the edge).  ``cost_usd`` is whatever is *not* already inside them.

2.  **Uncertainty is subtracted, not mentioned.**  ``margin_usd`` is the edge
    *minus* the execution/model uncertainty, and eligibility needs a
    configurable safety margin on top of ``EV > 0``.  A +0.08% edge against a
    ±0.07% uncertainty is not a trade; the same edge at ±0.01% might be.  The
    three honest sources of that uncertainty are enumerated in
    :class:`UncertaintyBudget`.

3.  **Small samples are shrunk, not trusted.**  A 100% win rate over three
    trades is not a 100% probability.  :func:`shrink_probability` puts a Beta
    prior on it and the shrunken value is what the gate sees; the posterior
    spread feeds straight back into the uncertainty budget.

4.  **Refusal is countable.**  Every rejection returns exactly one of the nine
    reasons in :class:`EVReject`, and :func:`count_rejections` always reports
    all nine — including the zeros.  "The model found no edge" and "the model
    refuses everything" are identical in the P&L and completely different
    diagnoses; the only way to tell them apart is the histogram of reasons.

The Phase 3 trap this module must not re-enact, in its own words: raising the
entry score threshold from 0.0 to 0.9 cut the trade count 24-fold — 22,131
trades to 918 — and moved expectancy from −$0.158 to −$0.145, i.e. **$0.013**.
A threshold sweep in a new coordinate system can buy exactly the same silence,
so :class:`ThresholdReport` carries the count/expectancy trade-off explicitly
(``trade_count_ratio``, ``expectancy_gain_usd``, ``buying_silence``) and
:func:`select_threshold` refuses a threshold whose out-of-sample trade count is
too small to mean anything.

Capital preservation outranks growth: the account is $500.  :func:`position_size`
defaults to fixed fractional risk, scales *within* that budget with edge and
confidence, and is bounded above by the position clamps.  Fractional Kelly
exists only as a research comparison and is hard-capped at
:data:`MAX_KELLY_FRACTION`; full Kelly, aggressive Kelly and martingale are not
reachable from any code path here — nothing in the sizing path can even see a
previous trade's outcome.

PAPER ONLY.  Nothing in this file places, prices or signs an order.
"""

from __future__ import annotations

import enum
import math
from collections.abc import Sequence
from dataclasses import dataclass, field

__all__ = [
    "EVReject",
    "REJECT_REASONS",
    "EVInputs",
    "EVResult",
    "EVGate",
    "EVContext",
    "DEFAULT_GATE",
    "expected_value",
    "count_rejections",
    "format_rejection_counts",
    "ProbabilityEstimate",
    "shrink_probability",
    "UncertaintyBudget",
    "uncertainty_budget",
    "ThresholdReport",
    "ThresholdSelection",
    "threshold_search",
    "select_threshold",
    "format_threshold_reports",
    "EVGateReport",
    "ev_gate_sweep",
    "format_ev_gate_reports",
    "SizingConfig",
    "SizingResult",
    "position_size",
    "kelly_fraction_of_equity",
    "DEFAULT_SIZING",
    "DEFAULT_THRESHOLDS",
    "EV_GATE_RETURNS_PCT",
    "MIN_THRESHOLD_TRADES",
    "MAX_KELLY_FRACTION",
    "PRIOR_STRENGTH",
    "SILENCE_COUNT_RATIO",
    "SILENCE_EXPECTANCY_USD",
]

_BPS = 10_000.0
_TOL = 1e-9


# --------------------------------------------------------------------------- #
# Constants — spelled out here so loosening one is a visible diff.
# --------------------------------------------------------------------------- #

#: Probability thresholds swept out of sample (item 13).
DEFAULT_THRESHOLDS: tuple[float, ...] = (
    0.55,
    0.575,
    0.60,
    0.625,
    0.65,
    0.675,
    0.70,
    0.75,
)

#: Minimum expected **net return**, in percent of notional, swept by the EV
#: gate (item 14).  0.02% of a $100 position is $0.02; 0.20% is $0.20.
EV_GATE_RETURNS_PCT: tuple[float, ...] = (0.02, 0.05, 0.10, 0.15, 0.20)

#: A threshold with fewer out-of-sample trades than this is not selectable.
#: Matches ``robustness.MIN_OOS_TRADES`` — a promotion gate that demands 30
#: out-of-sample trades cannot be fed by a threshold chosen on 4 of them.
MIN_THRESHOLD_TRADES = 30

#: Beta prior strength for :func:`shrink_probability`.  20 pseudo-observations
#: at p=0.5 means a 3/3 record comes back at 0.565, not 1.0, and a 300-trade
#: record is essentially unshrunk.
PRIOR_STRENGTH = 20.0

#: Phase 3's trap, parameterised.  A threshold that cuts the trade count by
#: this factor or more while adding less than
#: :data:`SILENCE_EXPECTANCY_USD` of expectancy is buying silence, not edge.
#: Phase 3's own numbers were 24× and $0.013.
SILENCE_COUNT_RATIO = 5.0
SILENCE_EXPECTANCY_USD = 0.02

#: Hard cap on the Kelly fraction reachable anywhere in this module.  Full
#: Kelly (1.0) and "aggressive" Kelly (>0.25) raise ``ValueError``.
MAX_KELLY_FRACTION = 0.25

#: Hard cap on the fraction of equity a *research* Kelly size may put at risk.
MAX_KELLY_RISK_PCT = 2.0


class EVReject(str, enum.Enum):
    """The nine rejection reasons (item 34).  Exactly one is ever returned."""

    LOW_PROBABILITY = "LOW_PROBABILITY"
    NEGATIVE_EV = "NEGATIVE_EV"
    COST_TOO_HIGH = "COST_TOO_HIGH"
    MODEL_UNCERTAIN = "MODEL_UNCERTAIN"
    CORRELATED_RISK = "CORRELATED_RISK"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    NO_LIQUIDITY = "NO_LIQUIDITY"
    STALE_DATA = "STALE_DATA"
    REGIME_NOT_SUPPORTED = "REGIME_NOT_SUPPORTED"


#: Every reason, in the order :func:`expected_value` tests them.  This order is
#: the contract: a setup that trips several is attributed to the first, so the
#: counts read as "what stopped it", not "what was wrong with it".
REJECT_REASONS: tuple[str, ...] = (
    EVReject.STALE_DATA.value,
    EVReject.SPREAD_TOO_WIDE.value,
    EVReject.NO_LIQUIDITY.value,
    EVReject.REGIME_NOT_SUPPORTED.value,
    EVReject.CORRELATED_RISK.value,
    EVReject.LOW_PROBABILITY.value,
    EVReject.NEGATIVE_EV.value,
    EVReject.COST_TOO_HIGH.value,
    EVReject.MODEL_UNCERTAIN.value,
)


# --------------------------------------------------------------------------- #
# Probability shrinkage
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProbabilityEstimate:
    """A win probability that knows how little it is based on.

    ``raw`` is the sample frequency, ``shrunk`` is the posterior mean of a
    ``Beta(a, b)`` prior updated by the sample, and ``std`` is the posterior
    standard deviation — the honest width of the estimate, which
    :func:`uncertainty_budget` converts into dollars.
    """

    wins: float
    trials: float
    raw: float
    shrunk: float
    std: float
    prior_mean: float
    prior_strength: float

    @property
    def shrinkage(self) -> float:
        """How far the prior pulled the estimate, in probability points."""
        return self.shrunk - self.raw


def shrink_probability(
    wins: float,
    trials: float,
    *,
    prior_mean: float = 0.5,
    prior_strength: float = PRIOR_STRENGTH,
) -> ProbabilityEstimate:
    """Bayesian shrinkage of a win rate estimated from ``trials`` samples.

    ``3 wins from 3 trades`` is the case this exists for.  The raw frequency is
    1.0; with the default ``Beta(10, 10)`` prior the posterior mean is
    ``(3 + 10) / (3 + 20) = 0.565`` — an estimate the EV gate can act on
    without pretending three observations settled anything.  At 300 trades the
    prior is worth 6% of the evidence and the shrunken value tracks the raw
    one.

    The posterior standard deviation is
    ``sqrt(p(1 − p) / (n + prior_strength + 1))``, the exact Beta posterior
    spread, and it is the third term of the uncertainty budget.
    """
    if trials < 0 or wins < 0:
        raise ValueError("wins and trials must be non-negative")
    if wins > trials + _TOL:
        raise ValueError(f"wins ({wins}) cannot exceed trials ({trials})")
    if not 0.0 <= prior_mean <= 1.0:
        raise ValueError("prior_mean must be in [0, 1]")
    if prior_strength < 0:
        raise ValueError("prior_strength must be non-negative")

    alpha = prior_mean * prior_strength
    beta = (1.0 - prior_mean) * prior_strength
    raw = (wins / trials) if trials > 0 else prior_mean
    shrunk = (wins + alpha) / (trials + alpha + beta)
    std = math.sqrt(shrunk * (1.0 - shrunk) / (trials + prior_strength + 1.0))
    return ProbabilityEstimate(
        wins=float(wins),
        trials=float(trials),
        raw=float(raw),
        shrunk=float(shrunk),
        std=float(std),
        prior_mean=float(prior_mean),
        prior_strength=float(prior_strength),
    )


# --------------------------------------------------------------------------- #
# Uncertainty
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class UncertaintyBudget:
    """Where ``EVInputs.uncertainty_usd`` comes from, itemised.

    Three sources, and only three, because these are the ones that can be
    measured rather than asserted:

    ``slippage_usd``
        **Execution dispersion.**  The realised slippage of a fill is a
        distribution, not a constant; its standard deviation in bps times the
        notional is the dollar spread of the execution.  ``docs/ECONOMICS.md``
        measures slippage at ~3× fees, so this is normally the largest term.
    ``calibration_usd``
        **Calibration error.**  If the reliability curve says a 0.70 bucket
        actually wins 0.64, the probability driving the EV is wrong by 0.06.
        That mis-statement moves EV by ``error × (avg_net_win + avg_net_loss)``
        — the full payoff span, because probability mass moves from the win
        leg to the loss leg.
    ``sample_usd``
        **Sample-size shrinkage.**  The posterior standard deviation from
        :func:`shrink_probability`, converted the same way.  Three trades and
        three hundred trades produce very different numbers here, which is the
        whole point.

    They are combined in quadrature (independent sources) and scaled by
    ``z``, the number of standard deviations the gate insists on covering.
    Quadrature, not a sum, because adding them assumes all three go wrong
    together and in the same direction; that is a worst case, not an estimate.
    Pass ``combine="sum"`` to get the worst case when that is what is wanted.
    """

    slippage_usd: float
    calibration_usd: float
    sample_usd: float
    z: float
    combine: str
    total_usd: float

    @property
    def parts(self) -> dict[str, float]:
        return {
            "slippage": self.slippage_usd,
            "calibration": self.calibration_usd,
            "sample": self.sample_usd,
        }


def uncertainty_budget(
    *,
    notional_usd: float,
    slippage_std_bps: float = 0.0,
    calibration_error: float = 0.0,
    probability_std: float = 0.0,
    avg_net_win_usd: float = 0.0,
    avg_net_loss_usd: float = 0.0,
    z: float = 1.0,
    combine: str = "quadrature",
) -> UncertaintyBudget:
    """Build an uncertainty figure from its three defensible sources.

    ``avg_net_loss_usd`` is a positive magnitude, matching :class:`EVInputs`.
    """
    if notional_usd < 0:
        raise ValueError("notional_usd must be non-negative")
    if slippage_std_bps < 0 or calibration_error < 0 or probability_std < 0:
        raise ValueError("uncertainty components must be non-negative")
    if avg_net_loss_usd < 0:
        raise ValueError("avg_net_loss_usd is a positive magnitude")
    if z < 0:
        raise ValueError("z must be non-negative")
    if combine not in ("quadrature", "sum"):
        raise ValueError("combine must be 'quadrature' or 'sum'")

    payoff_span = abs(avg_net_win_usd) + abs(avg_net_loss_usd)
    slippage = slippage_std_bps / _BPS * notional_usd
    calibration = calibration_error * payoff_span
    sample = probability_std * payoff_span

    if combine == "sum":
        total = slippage + calibration + sample
    else:
        total = math.sqrt(slippage**2 + calibration**2 + sample**2)
    return UncertaintyBudget(
        slippage_usd=slippage,
        calibration_usd=calibration,
        sample_usd=sample,
        z=float(z),
        combine=combine,
        total_usd=total * z,
    )


# --------------------------------------------------------------------------- #
# The EV contract
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EVInputs:
    """Everything the EV arithmetic needs, all of it net of costs.

    The first seven fields are the frozen contract from ``docs/PHASE4_SPEC.md``
    and keep their order; the rest are additive with defaults, so
    ``EVInputs(0.68, 0.32, 0.0, 0.62, 0.31, 0.0, 0.0)`` still constructs.

    * ``p_win``/``p_loss``/``p_timeout`` must sum to 1.  A model that emits only
      a win probability should use :meth:`from_probability`.
    * ``avg_net_win_usd`` is positive, ``avg_net_loss_usd`` is a **positive
      magnitude** (the minus sign is in the formula, not the data) — the same
      convention as ``PerfStats.avg_loss``.
    * ``cost_usd`` is any per-trade cost *not already inside* the two averages.
      When the averages come from cost-aware triple-barrier labels this is
      normally 0, and double-subtracting the round trip here is the easiest way
      to make the whole module wrong.
    * ``timeout_penalty_usd`` defaults to ``cost_usd``: a timeout exits flat in
      gross terms and still pays the round trip, so it costs exactly the costs.
    """

    p_win: float
    p_loss: float
    p_timeout: float
    avg_net_win_usd: float
    avg_net_loss_usd: float
    cost_usd: float
    uncertainty_usd: float
    notional_usd: float = 100.0
    timeout_penalty_usd: float | None = None
    sample_trades: int = 0

    def __post_init__(self) -> None:
        for name in ("p_win", "p_loss", "p_timeout"):
            value = getattr(self, name)
            if not 0.0 - _TOL <= value <= 1.0 + _TOL:
                raise ValueError(f"{name}={value!r} is not a probability")
        total = self.p_win + self.p_loss + self.p_timeout
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"p_win + p_loss + p_timeout must be 1.0, got {total:.9f}; "
                "use EVInputs.from_probability() to fill the remainder"
            )
        if self.avg_net_loss_usd < 0:
            raise ValueError(
                "avg_net_loss_usd is a positive magnitude; "
                f"got {self.avg_net_loss_usd!r}"
            )
        if self.uncertainty_usd < 0:
            raise ValueError("uncertainty_usd must be non-negative")
        if self.notional_usd < 0:
            raise ValueError("notional_usd must be non-negative")

    @classmethod
    def from_probability(
        cls,
        p_win: float,
        *,
        avg_net_win_usd: float,
        avg_net_loss_usd: float,
        p_timeout: float = 0.0,
        cost_usd: float = 0.0,
        uncertainty_usd: float = 0.0,
        notional_usd: float = 100.0,
        timeout_penalty_usd: float | None = None,
        sample_trades: int = 0,
    ) -> EVInputs:
        """Build inputs from a single win probability; ``p_loss`` is the rest."""
        p_loss = 1.0 - p_win - p_timeout
        if p_loss < -_TOL:
            raise ValueError("p_win + p_timeout exceeds 1.0")
        return cls(
            p_win=p_win,
            p_loss=max(0.0, p_loss),
            p_timeout=p_timeout,
            avg_net_win_usd=avg_net_win_usd,
            avg_net_loss_usd=avg_net_loss_usd,
            cost_usd=cost_usd,
            uncertainty_usd=uncertainty_usd,
            notional_usd=notional_usd,
            timeout_penalty_usd=timeout_penalty_usd,
            sample_trades=sample_trades,
        )

    @property
    def timeout_cost_usd(self) -> float:
        """The resolved timeout/execution penalty (defaults to ``cost_usd``)."""
        if self.timeout_penalty_usd is None:
            return self.cost_usd
        return self.timeout_penalty_usd


@dataclass(frozen=True)
class EVResult:
    """The verdict.  ``eligible`` and ``reject`` are mutually exclusive.

    ``margin_usd`` is the number the brief is about: the edge **minus** the
    uncertainty.  A positive ``expected_value_usd`` with a negative
    ``margin_usd`` is a trade whose edge is smaller than the error bar on it,
    and this module calls that ``MODEL_UNCERTAIN`` rather than a trade.
    """

    expected_value_usd: float
    expected_value_bps: float
    margin_usd: float
    eligible: bool
    reject: str | None
    gross_expected_value_usd: float = 0.0
    gross_expected_value_bps: float = 0.0
    margin_bps: float = 0.0
    uncertainty_usd: float = 0.0
    notional_usd: float = 0.0
    detail: str = ""

    @property
    def reject_reason(self) -> EVReject | None:
        return EVReject(self.reject) if self.reject is not None else None


@dataclass(frozen=True)
class EVGate:
    """The thresholds eligibility is measured against.

    Defaults are deliberately conservative for a $500 account on a venue whose
    round trip is 3–6 bps:

    * ``min_probability`` = 0.55 — the bottom of the swept grid.
    * ``min_expected_value_usd`` = $0.02 — mirrors ``Settings.min_net_edge_usd``.
    * ``min_expected_return_bps`` = 5 bps (0.05%) — the second gate value in
      :data:`EV_GATE_RETURNS_PCT`; below this the edge is inside the round trip.
    * ``safety_margin_usd`` = $0.02 — the margin demanded *after* uncertainty is
      subtracted.  This is what rejects "+0.08% edge against ±0.07%": the edge
      clears EV > 0 and clears 5 bps, and still leaves only $0.01 of margin.
    * ``uncertainty_multiple`` = 1.0 — EV must cover one full uncertainty unit.
      Raise it to 2.0 to demand two.
    """

    min_probability: float = 0.55
    min_expected_value_usd: float = 0.02
    min_expected_return_bps: float = 5.0
    safety_margin_usd: float = 0.02
    uncertainty_multiple: float = 1.0
    max_spread_bps: float = 5.0
    min_depth_usd: float = 1_000.0
    max_data_age_ms: int = 5_000
    max_correlated_risk_pct: float = 2.0
    supported_regimes: tuple[str, ...] = ()

    def scaled_min_ev_usd(self, notional_usd: float) -> float:
        """The dollar floor, scaled to the position (item 14).

        "Do not open a $100 position chasing an expected +$0.01" is this line:
        the floor is the larger of the absolute minimum and the minimum return
        applied to the notional, so a bigger position must produce a
        proportionally bigger expected dollar edge, never the same pennies.
        """
        return max(
            self.min_expected_value_usd,
            self.min_expected_return_bps / _BPS * notional_usd,
        )


DEFAULT_GATE = EVGate()


@dataclass(frozen=True)
class EVContext:
    """Market/portfolio conditions that can veto a trade before EV is even read.

    All fields are optional.  A field left ``None`` is *not checked* — this
    module never invents a spread it was not given, and an unchecked condition
    is never reported as passed.
    """

    spread_bps: float | None = None
    depth_usd: float | None = None
    data_age_ms: int | None = None
    regime: str | None = None
    correlated_risk_pct: float | None = None


def expected_value(
    inputs: EVInputs,
    *,
    gate: EVGate = DEFAULT_GATE,
    context: EVContext | None = None,
) -> EVResult:
    """``EV = P_win·win − P_loss·loss − P_timeout·penalty − cost``, in net USD.

    Worked against the brief's own example: a 68% win probability with a +$0.62
    expected win and a −$0.31 expected loss gives
    ``0.68 × 0.62 − 0.32 × 0.31 = 0.4216 − 0.0992 = +$0.3224``.

    And against the case this module exists to catch: a **70% win rate** paying
    +$0.10 against −$0.40 gives ``0.07 − 0.12 = −$0.05``.  High win rate, losing
    strategy, ``NEGATIVE_EV``.

    Exactly one rejection reason comes back, chosen in the order given by
    :data:`REJECT_REASONS`: the conditions that make the *inputs* meaningless
    first (stale data, an untradeable book, an unsupported regime, correlated
    exposure), then the model (probability below the floor), then the
    arithmetic (EV below the gate — split into ``NEGATIVE_EV`` when the payoff
    itself does not clear it and ``COST_TOO_HIGH`` when the payoff clears it and
    the costs take it away), and last the error bar (``MODEL_UNCERTAIN``).
    """
    ctx = context or EVContext()

    gross = (
        inputs.p_win * inputs.avg_net_win_usd
        - inputs.p_loss * inputs.avg_net_loss_usd
        - inputs.p_timeout * inputs.timeout_cost_usd
    )
    ev = gross - inputs.cost_usd
    notional = inputs.notional_usd
    ev_bps = (ev / notional * _BPS) if notional > 0 else 0.0
    gross_bps = (gross / notional * _BPS) if notional > 0 else 0.0
    margin = ev - gate.uncertainty_multiple * inputs.uncertainty_usd
    margin_bps = (margin / notional * _BPS) if notional > 0 else 0.0

    def verdict(reject: EVReject | None, detail: str) -> EVResult:
        return EVResult(
            expected_value_usd=ev,
            expected_value_bps=ev_bps,
            margin_usd=margin,
            eligible=reject is None,
            reject=None if reject is None else reject.value,
            gross_expected_value_usd=gross,
            gross_expected_value_bps=gross_bps,
            margin_bps=margin_bps,
            uncertainty_usd=inputs.uncertainty_usd,
            notional_usd=notional,
            detail=detail,
        )

    # 1. Is the observation even usable?
    if ctx.data_age_ms is not None and ctx.data_age_ms > gate.max_data_age_ms:
        return verdict(
            EVReject.STALE_DATA,
            f"book is {ctx.data_age_ms}ms old, limit {gate.max_data_age_ms}ms",
        )

    # 2. Can the trade be executed at anything like the modelled cost?
    if ctx.spread_bps is not None and ctx.spread_bps > gate.max_spread_bps:
        return verdict(
            EVReject.SPREAD_TOO_WIDE,
            f"spread {ctx.spread_bps:.2f} bps > {gate.max_spread_bps:.2f} bps",
        )
    if ctx.depth_usd is not None and ctx.depth_usd < gate.min_depth_usd:
        return verdict(
            EVReject.NO_LIQUIDITY,
            f"depth ${ctx.depth_usd:,.0f} < ${gate.min_depth_usd:,.0f}",
        )

    # 3. Was the model trained anywhere near this state of the world?
    if (
        gate.supported_regimes
        and ctx.regime is not None
        and ctx.regime not in gate.supported_regimes
    ):
        return verdict(
            EVReject.REGIME_NOT_SUPPORTED,
            f"regime {ctx.regime!r} not in {list(gate.supported_regimes)}",
        )

    # 4. Would this trade double an exposure the book already carries?
    if (
        ctx.correlated_risk_pct is not None
        and ctx.correlated_risk_pct > gate.max_correlated_risk_pct
    ):
        return verdict(
            EVReject.CORRELATED_RISK,
            f"correlated risk {ctx.correlated_risk_pct:.2f}% > "
            f"{gate.max_correlated_risk_pct:.2f}%",
        )

    # 5. The model's own confidence floor.
    if inputs.p_win < gate.min_probability - _TOL:
        return verdict(
            EVReject.LOW_PROBABILITY,
            f"p_win {inputs.p_win:.4f} < {gate.min_probability:.4f}",
        )

    # 6. The arithmetic.  Both floors, and the diagnosis of which side failed.
    min_ev = gate.scaled_min_ev_usd(notional)
    if ev < min_ev - _TOL or ev_bps < gate.min_expected_return_bps - _TOL:
        cost_is_the_problem = (
            gross >= min_ev - _TOL and gross_bps >= gate.min_expected_return_bps - _TOL
        )
        if cost_is_the_problem:
            return verdict(
                EVReject.COST_TOO_HIGH,
                f"gross EV ${gross:.4f} clears the gate, costs ${inputs.cost_usd:.4f} "
                f"leave ${ev:.4f} ({ev_bps:.2f} bps)",
            )
        return verdict(
            EVReject.NEGATIVE_EV,
            f"EV ${ev:.4f} ({ev_bps:.2f} bps) below the gate "
            f"(${min_ev:.4f} / {gate.min_expected_return_bps:.2f} bps)",
        )

    # 7. Is the edge bigger than the error bar on it?
    if margin < gate.safety_margin_usd - _TOL:
        return verdict(
            EVReject.MODEL_UNCERTAIN,
            f"EV ${ev:.4f} against ±${inputs.uncertainty_usd:.4f} leaves "
            f"${margin:.4f} of margin, below ${gate.safety_margin_usd:.4f}",
        )

    return verdict(
        None,
        f"EV ${ev:.4f} ({ev_bps:.2f} bps), margin ${margin:.4f} "
        f"after ±${inputs.uncertainty_usd:.4f}",
    )


def count_rejections(results: Sequence[EVResult]) -> dict[str, int]:
    """Histogram of refusals, **with every reason present including zeros**.

    The key on ``"ELIGIBLE"`` counts what got through.  This is the only way to
    distinguish "the model found no edge" (few candidates, mostly NEGATIVE_EV)
    from "the model refuses everything" (everything piled on STALE_DATA or
    MODEL_UNCERTAIN) — in the P&L both are a flat line.
    """
    counts: dict[str, int] = {reason: 0 for reason in REJECT_REASONS}
    counts["ELIGIBLE"] = 0
    for result in results:
        if result.eligible:
            counts["ELIGIBLE"] += 1
        else:
            key = result.reject
            if key is None:  # pragma: no cover - EVResult forbids it
                raise ValueError("ineligible result carried no rejection reason")
            counts[key] += 1
    return counts


def format_rejection_counts(counts: dict[str, int]) -> str:
    """One line per reason, zeros included, ordered by the decision order."""
    total = sum(counts.values())
    lines = [f"{total} candidate(s) evaluated"]
    eligible = counts.get("ELIGIBLE", 0)
    share = (eligible / total * 100.0) if total else 0.0
    lines.append(f"  ELIGIBLE              {eligible:6d}  ({share:5.1f}%)")
    for reason in REJECT_REASONS:
        n = counts.get(reason, 0)
        share = (n / total * 100.0) if total else 0.0
        lines.append(f"  {reason:<21} {n:6d}  ({share:5.1f}%)")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Threshold search (item 13)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ThresholdReport:
    """Out-of-sample performance at one probability threshold.

    ``trade_count_ratio`` and ``expectancy_gain_usd`` are measured against the
    **lowest** threshold in the sweep and exist so a reader can see, without
    doing arithmetic, whether a threshold is buying edge or buying silence.
    Phase 3's 0.9 score threshold would print here as ``trade_count_ratio
    24.1``, ``expectancy_gain_usd 0.013``, ``buying_silence True``.
    """

    threshold: float
    trades: int
    wins: int
    win_rate: float
    net_profit_usd: float
    expectancy_usd: float
    profit_factor: float
    max_drawdown_usd: float
    calibration_error: float
    reliability: tuple[tuple[float, float, int], ...] = ()
    avg_win_usd: float = 0.0
    avg_loss_usd: float = 0.0
    trade_count_ratio: float = 1.0
    expectancy_gain_usd: float = 0.0
    buying_silence: bool = False
    enough_trades: bool = True

    @property
    def label_win_rate(self) -> float:
        """Win rate of the *label*, which is what calibration is about."""
        return self.win_rate


@dataclass(frozen=True)
class ThresholdSelection:
    """What :func:`select_threshold` chose, and what it refused to choose."""

    threshold: float | None
    report: ThresholdReport | None
    refused: bool
    reason: str
    min_trades: int
    considered: tuple[float, ...] = ()
    too_few_trades: tuple[float, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def selected(self) -> bool:
        return not self.refused


def _drawdown_usd(pnls: Sequence[float]) -> float:
    """Peak-to-trough of the closed-trade equity curve, in USD.

    Blind to what a position did while open, so this is a floor — the same
    caveat ``research.robustness.compute_stats`` states about its own number.
    """
    curve = 0.0
    peak = 0.0
    worst = 0.0
    for pnl in pnls:
        curve += pnl
        peak = max(peak, curve)
        worst = max(worst, peak - curve)
    return worst


def _reliability(
    probabilities: Sequence[float], outcomes: Sequence[float], *, bins: int
) -> tuple[tuple[tuple[float, float, int], ...], float]:
    """Equal-width reliability bins and the maximum absolute error.

    Mirrors the shape ``calibrate.CalibrationReport.bins`` uses —
    ``(predicted, actual, n)`` — without importing that module, which belongs to
    another workstream.
    """
    if not probabilities:
        return (), 0.0
    buckets: list[list[tuple[float, float]]] = [[] for _ in range(bins)]
    for p, y in zip(probabilities, outcomes, strict=True):
        idx = min(bins - 1, max(0, int(p * bins)))
        buckets[idx].append((p, y))
    rows: list[tuple[float, float, int]] = []
    worst = 0.0
    for bucket in buckets:
        if not bucket:
            continue
        predicted = sum(p for p, _ in bucket) / len(bucket)
        actual = sum(y for _, y in bucket) / len(bucket)
        rows.append((predicted, actual, len(bucket)))
        worst = max(worst, abs(predicted - actual))
    return tuple(rows), worst


def threshold_search(
    p: Sequence[float],
    y: Sequence[float],
    net_returns: Sequence[float],
    *,
    thresholds: Sequence[float] = DEFAULT_THRESHOLDS,
    cost_bps: float = 0.0,
    notional_usd: float = 100.0,
    bins: int = 10,
    min_trades: int = MIN_THRESHOLD_TRADES,
) -> list[ThresholdReport]:
    """Sweep probability thresholds and report, **out of sample**, what each buys.

    ``p`` are predicted probabilities, ``y`` the realised binary label, and
    ``net_returns`` the realised **net return in bps** of each candidate had it
    been taken.  ``cost_bps`` is any round-trip cost not already inside
    ``net_returns`` and is subtracted per trade; pass ``0.0`` when the labels
    are already cost-aware, which the Phase 4 triple-barrier labels are.

    Every row carries trade count, net profit, profit factor, drawdown and
    reliability side by side, because the Phase 3 finding — 24× fewer trades for
    $0.013 of expectancy — is invisible in any one of those columns alone.

    This function *reports*.  It does not choose; :func:`select_threshold` does,
    and it chooses on net profit.
    """
    if len(p) != len(y) or len(p) != len(net_returns):
        raise ValueError("p, y and net_returns must be the same length")
    if not thresholds:
        raise ValueError("thresholds must not be empty")

    pnl_usd = [(r - cost_bps) / _BPS * notional_usd for r in net_returns]
    ordered = sorted(float(t) for t in thresholds)

    reports: list[ThresholdReport] = []
    baseline_trades = 0
    baseline_expectancy = 0.0

    for i, threshold in enumerate(ordered):
        idx = [j for j, prob in enumerate(p) if prob >= threshold]
        taken = [pnl_usd[j] for j in idx]
        labels = [float(y[j]) for j in idx]
        probs = [float(p[j]) for j in idx]

        wins = [v for v in taken if v > 0]
        losses = [v for v in taken if v < 0]
        trades = len(taken)
        net = sum(taken)
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        if gross_loss > 0:
            pf = gross_win / gross_loss
        else:
            pf = float("inf") if gross_win > 0 else 0.0
        reliability, calib_error = _reliability(probs, labels, bins=bins)
        expectancy = (net / trades) if trades else 0.0

        if i == 0:
            baseline_trades = trades
            baseline_expectancy = expectancy
        count_ratio = (baseline_trades / trades) if trades else float("inf")
        gain = expectancy - baseline_expectancy
        silence = (
            trades < baseline_trades
            and count_ratio >= SILENCE_COUNT_RATIO
            and gain < SILENCE_EXPECTANCY_USD
        )

        reports.append(
            ThresholdReport(
                threshold=threshold,
                trades=trades,
                wins=len(wins),
                win_rate=(sum(labels) / trades) if trades else 0.0,
                net_profit_usd=net,
                expectancy_usd=expectancy,
                profit_factor=pf,
                max_drawdown_usd=_drawdown_usd(taken),
                calibration_error=calib_error,
                reliability=reliability,
                avg_win_usd=(gross_win / len(wins)) if wins else 0.0,
                avg_loss_usd=(gross_loss / len(losses)) if losses else 0.0,
                trade_count_ratio=count_ratio,
                expectancy_gain_usd=gain,
                buying_silence=silence,
                enough_trades=trades >= min_trades,
            )
        )
    return reports


def select_threshold(
    reports: Sequence[ThresholdReport],
    *,
    min_trades: int = MIN_THRESHOLD_TRADES,
    require_positive: bool = True,
) -> ThresholdSelection:
    """Pick a threshold on **out-of-sample net profit**, never on win rate.

    Two refusals are first-class results, not errors:

    * every threshold has fewer than ``min_trades`` out-of-sample trades — the
      sweep cannot distinguish edge from noise, so it declines to pick;
    * the best qualifying threshold does not make money (``require_positive``).

    Ties break toward *more* trades, then toward the lower threshold: given two
    equal net results, the one built on more evidence and less selectivity is
    the safer claim.
    """
    considered = tuple(r.threshold for r in reports)
    too_few = tuple(r.threshold for r in reports if r.trades < min_trades)
    eligible = [r for r in reports if r.trades >= min_trades]

    if not eligible:
        best_n = max((r.trades for r in reports), default=0)
        return ThresholdSelection(
            threshold=None,
            report=None,
            refused=True,
            reason=(
                f"no threshold reached {min_trades} out-of-sample trades "
                f"(best was {best_n}); the sample cannot tell edge from noise"
            ),
            min_trades=min_trades,
            considered=considered,
            too_few_trades=too_few,
        )

    best = max(
        eligible,
        key=lambda r: (r.net_profit_usd, r.trades, -r.threshold),
    )

    if require_positive and best.net_profit_usd <= 0:
        return ThresholdSelection(
            threshold=None,
            report=best,
            refused=True,
            reason=(
                f"best qualifying threshold {best.threshold:.3f} lost "
                f"${abs(best.net_profit_usd):.2f} out of sample; no threshold is "
                "worth trading"
            ),
            min_trades=min_trades,
            considered=considered,
            too_few_trades=too_few,
        )

    warnings: list[str] = []
    if best.buying_silence:
        warnings.append(
            f"threshold {best.threshold:.3f} cuts the trade count "
            f"{best.trade_count_ratio:.1f}x for {best.expectancy_gain_usd:+.4f} "
            "of expectancy — that is buying silence, not edge (Phase 3, §15)"
        )
    if best.calibration_error > 0.10:
        warnings.append(
            f"reliability error {best.calibration_error:.3f} exceeds the 0.10 "
            "promotion gate"
        )
    best_by_win_rate = max(reports, key=lambda r: (r.win_rate, -r.threshold))
    if best_by_win_rate.threshold != best.threshold:
        warnings.append(
            f"win rate peaks at {best_by_win_rate.threshold:.3f} "
            f"({best_by_win_rate.win_rate:.1%}, net "
            f"${best_by_win_rate.net_profit_usd:+.2f}) — not selected; selection "
            "is on net profit"
        )

    return ThresholdSelection(
        threshold=best.threshold,
        report=best,
        refused=False,
        reason=(
            f"threshold {best.threshold:.3f}: ${best.net_profit_usd:+.2f} net over "
            f"{best.trades} out-of-sample trades (PF {best.profit_factor:.2f})"
        ),
        min_trades=min_trades,
        considered=considered,
        too_few_trades=too_few,
        warnings=tuple(warnings),
    )


def format_threshold_reports(
    reports: Sequence[ThresholdReport],
    *,
    min_trades: int = MIN_THRESHOLD_TRADES,
) -> str:
    """A table whose count/expectancy trade-off is readable without arithmetic.

    ``vs base`` is the count ratio against the lowest threshold and ``Δ exp`` the
    expectancy it bought.  A row where the first is large and the second is not
    is the Phase 3 failure, and it is flagged in words.
    """
    header = (
        f"{'thresh':>7} {'trades':>7} {'win%':>6} {'net $':>9} {'exp $':>8} "
        f"{'PF':>6} {'DD $':>8} {'calib':>6} {'vs base':>8} {'Δ exp $':>9}  note"
    )
    lines = [header, "-" * len(header)]
    for r in reports:
        pf = "inf" if r.profit_factor == float("inf") else f"{r.profit_factor:.2f}"
        if r.trade_count_ratio == float("inf"):
            ratio = "inf"
        else:
            ratio = f"{r.trade_count_ratio:.1f}x"
        notes = []
        if not r.enough_trades:
            notes.append(f"<{min_trades} trades")
        if r.buying_silence:
            notes.append("buying silence")
        lines.append(
            f"{r.threshold:>7.3f} {r.trades:>7d} {r.win_rate * 100:>6.1f} "
            f"{r.net_profit_usd:>+9.2f} {r.expectancy_usd:>+8.4f} {pf:>6} "
            f"{r.max_drawdown_usd:>8.2f} {r.calibration_error:>6.3f} {ratio:>8} "
            f"{r.expectancy_gain_usd:>+9.4f}  {', '.join(notes)}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The EV gate sweep (item 14)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EVGateReport:
    """One row of the EV-gate sweep: what a minimum expected return costs and buys."""

    min_return_pct: float
    min_return_bps: float
    min_ev_usd: float
    notional_usd: float
    trades: int
    net_profit_usd: float
    expectancy_usd: float
    profit_factor: float
    max_drawdown_usd: float
    win_rate: float
    rejections: dict[str, int] = field(default_factory=dict)
    enough_trades: bool = True


def ev_gate_sweep(
    p: Sequence[float],
    y: Sequence[float],
    net_returns: Sequence[float],
    *,
    avg_net_win_usd: float,
    avg_net_loss_usd: float,
    returns_pct: Sequence[float] = EV_GATE_RETURNS_PCT,
    notional_usd: float = 100.0,
    cost_usd: float = 0.0,
    cost_bps: float = 0.0,
    uncertainty_usd: float = 0.0,
    gate: EVGate = DEFAULT_GATE,
    contexts: Sequence[EVContext] | None = None,
    min_trades: int = MIN_THRESHOLD_TRADES,
) -> list[EVGateReport]:
    """Sweep the minimum expected net return, in percent of the position.

    ``avg_net_win_usd`` / ``avg_net_loss_usd`` are the payoff estimated **on
    training data** — deriving them from the same ``net_returns`` being scored
    would leak the answer into the gate.

    The dollar floor is not a separate axis: it is
    :meth:`EVGate.scaled_min_ev_usd`, the percentage applied to ``notional_usd``
    and floored at ``min_expected_value_usd``.  That is the rule the brief
    states — a $100 position chasing +$0.01 (1 bps) fails even the loosest
    0.02% row, and it fails harder when the uncertainty is larger than the edge,
    because :func:`expected_value` then returns ``MODEL_UNCERTAIN``.

    Each row carries its own full rejection histogram, so a row with no trades
    can be read as "no edge" or "refused on spread" rather than guessed at.
    """
    if len(p) != len(y) or len(p) != len(net_returns):
        raise ValueError("p, y and net_returns must be the same length")
    if contexts is not None and len(contexts) != len(p):
        raise ValueError("contexts must be the same length as p")

    pnl_usd = [(r - cost_bps) / _BPS * notional_usd for r in net_returns]
    reports: list[EVGateReport] = []

    for pct in returns_pct:
        min_bps = pct * 100.0  # 0.05% == 5 bps
        row_gate = EVGate(
            min_probability=gate.min_probability,
            min_expected_value_usd=gate.min_expected_value_usd,
            min_expected_return_bps=min_bps,
            safety_margin_usd=gate.safety_margin_usd,
            uncertainty_multiple=gate.uncertainty_multiple,
            max_spread_bps=gate.max_spread_bps,
            min_depth_usd=gate.min_depth_usd,
            max_data_age_ms=gate.max_data_age_ms,
            max_correlated_risk_pct=gate.max_correlated_risk_pct,
            supported_regimes=gate.supported_regimes,
        )
        results: list[EVResult] = []
        taken: list[float] = []
        labels: list[float] = []
        for i, prob in enumerate(p):
            inputs = EVInputs.from_probability(
                float(prob),
                avg_net_win_usd=avg_net_win_usd,
                avg_net_loss_usd=avg_net_loss_usd,
                cost_usd=cost_usd,
                uncertainty_usd=uncertainty_usd,
                notional_usd=notional_usd,
            )
            ctx = contexts[i] if contexts is not None else None
            result = expected_value(inputs, gate=row_gate, context=ctx)
            results.append(result)
            if result.eligible:
                taken.append(pnl_usd[i])
                labels.append(float(y[i]))

        wins = [v for v in taken if v > 0]
        losses = [v for v in taken if v < 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        if gross_loss > 0:
            pf = gross_win / gross_loss
        else:
            pf = float("inf") if gross_win > 0 else 0.0
        net = sum(taken)
        reports.append(
            EVGateReport(
                min_return_pct=pct,
                min_return_bps=min_bps,
                min_ev_usd=row_gate.scaled_min_ev_usd(notional_usd),
                notional_usd=notional_usd,
                trades=len(taken),
                net_profit_usd=net,
                expectancy_usd=(net / len(taken)) if taken else 0.0,
                profit_factor=pf,
                max_drawdown_usd=_drawdown_usd(taken),
                win_rate=(sum(labels) / len(labels)) if labels else 0.0,
                rejections=count_rejections(results),
                enough_trades=len(taken) >= min_trades,
            )
        )
    return reports


def format_ev_gate_reports(reports: Sequence[EVGateReport]) -> str:
    """One line per gate, with the dollar floor the percentage actually implies."""
    header = (
        f"{'min ret':>8} {'min EV $':>9} {'trades':>7} {'net $':>9} {'exp $':>8} "
        f"{'PF':>6} {'DD $':>8}  top rejection"
    )
    lines = [header, "-" * len(header)]
    for r in reports:
        pf = "inf" if r.profit_factor == float("inf") else f"{r.profit_factor:.2f}"
        rejects = {k: v for k, v in r.rejections.items() if k != "ELIGIBLE" and v}
        top = max(rejects.items(), key=lambda kv: kv[1])[0] if rejects else "none"
        lines.append(
            f"{r.min_return_pct:>7.2f}% {r.min_ev_usd:>9.3f} {r.trades:>7d} "
            f"{r.net_profit_usd:>+9.2f} {r.expectancy_usd:>+8.4f} {pf:>6} "
            f"{r.max_drawdown_usd:>8.2f}  {top}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Position sizing from edge (item 16)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SizingConfig:
    """Bounds for edge-scaled sizing on a $500 account.

    Defaults mirror ``app.config``: ``risk_per_trade_pct`` 0.5%,
    ``max_position_usd`` $125, ``max_position_pct_equity`` 35%.  Sizing scales
    *within* the fixed-risk budget — the multiplier is capped at
    ``max_edge_multiplier`` = 1.0 — so a strong signal can never buy more risk
    than the fixed-risk rule allows, only less.  That asymmetry is deliberate:
    on $500, being too small costs some upside and being too large ends the
    experiment.
    """

    equity_usd: float = 500.0
    risk_per_trade_pct: float = 0.5
    max_position_usd: float = 125.0
    max_position_pct_equity: float = 35.0
    min_notional_usd: float = 10.0
    edge_scale_bps: float = 20.0
    min_edge_multiplier: float = 0.25
    max_edge_multiplier: float = 1.0
    confidence_floor: float = 0.50
    confidence_ceiling: float = 0.75

    def __post_init__(self) -> None:
        if self.max_edge_multiplier > 1.0 + _TOL:
            raise ValueError(
                "max_edge_multiplier > 1.0 would size above the fixed-risk "
                "budget; that is leverage on edge, not scaling within it"
            )
        if not 0.0 <= self.min_edge_multiplier <= self.max_edge_multiplier:
            raise ValueError("min_edge_multiplier must be in [0, max_edge_multiplier]")
        if self.risk_per_trade_pct <= 0 or self.risk_per_trade_pct > 2.0:
            raise ValueError("risk_per_trade_pct must be in (0, 2]")
        if self.confidence_ceiling <= self.confidence_floor:
            raise ValueError("confidence_ceiling must exceed confidence_floor")


DEFAULT_SIZING = SizingConfig()


@dataclass(frozen=True)
class SizingResult:
    """A size, the budget it came from, and which bound decided it."""

    notional_usd: float
    risk_usd: float
    qty_multiplier: float
    method: str
    bound_by: str
    notes: tuple[str, ...] = ()

    def risk_pct_of(self, equity_usd: float) -> float:
        """Risk at the stop as a percentage of the equity it was sized from."""
        return (self.risk_usd / equity_usd * 100.0) if equity_usd > 0 else 0.0


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def edge_confidence_multiplier(
    edge_bps: float, confidence: float, *, config: SizingConfig = DEFAULT_SIZING
) -> float:
    """A bounded multiplier in ``[min_edge_multiplier, max_edge_multiplier]``.

    Linear in edge up to ``edge_scale_bps`` and linear in confidence between the
    floor and the ceiling, multiplied together and clamped.  Both terms saturate:
    a 200 bps predicted edge sizes exactly like a 20 bps one, because the extra
    180 bps is almost certainly estimation error and sizing on it is how a $500
    account dies.

    Either term at zero returns **zero**, not the floor — no edge and a coin-flip
    confidence buy no position at all.  The floor applies only once both terms
    are positive, so a real but small signal is not rounded down to dust.
    """
    if edge_bps <= 0:
        return 0.0
    edge_term = _clamp(edge_bps / config.edge_scale_bps, 0.0, 1.0)
    span = config.confidence_ceiling - config.confidence_floor
    conf_term = _clamp((confidence - config.confidence_floor) / span, 0.0, 1.0)
    raw = edge_term * conf_term
    if raw <= 0:
        return 0.0
    return _clamp(raw, config.min_edge_multiplier, config.max_edge_multiplier)


def kelly_fraction_of_equity(
    p_win: float, payoff_ratio: float, *, fraction: float = MAX_KELLY_FRACTION
) -> float:
    """Fractional Kelly, **research comparison only**, hard-capped.

    ``f* = (b·p − q) / b`` for a payoff ratio ``b``; the returned value is
    ``fraction × f*`` clipped to ``[0, 1]``.  ``fraction`` above
    :data:`MAX_KELLY_FRACTION` raises — full Kelly and "aggressive" Kelly are
    not reachable through any code path in this module, by construction rather
    than by convention.  Kelly assumes the probability and the payoff are known;
    here both are estimates from a few hundred trades, and Kelly's drawdown
    under a mis-estimated ``p`` is exactly the failure a $500 account cannot
    absorb.
    """
    if not 0.0 <= p_win <= 1.0:
        raise ValueError("p_win must be a probability")
    if payoff_ratio <= 0:
        raise ValueError("payoff_ratio must be positive")
    if fraction <= 0:
        raise ValueError("fraction must be positive")
    if fraction > MAX_KELLY_FRACTION + _TOL:
        raise ValueError(
            f"fraction={fraction} exceeds MAX_KELLY_FRACTION={MAX_KELLY_FRACTION}; "
            "full and aggressive Kelly are refused here on purpose"
        )
    edge = (payoff_ratio * p_win - (1.0 - p_win)) / payoff_ratio
    return _clamp(fraction * edge, 0.0, 1.0)


def position_size(
    *,
    edge_bps: float,
    confidence: float,
    stop_distance_bps: float,
    config: SizingConfig = DEFAULT_SIZING,
    method: str = "fixed_risk",
    payoff_ratio: float | None = None,
    kelly_fraction: float = MAX_KELLY_FRACTION,
) -> SizingResult:
    """Size a position from predicted edge and confidence.  Bounded, memoryless.

    Default method is ``"fixed_risk"``: risk ``risk_per_trade_pct`` of equity at
    the stop, scaled by :func:`edge_confidence_multiplier`, then converted to a
    notional through the stop distance and clamped by ``max_position_usd`` and
    ``max_position_pct_equity``.  ``"fractional_kelly"`` is available as a
    research comparison and is capped by the same clamps *and* by
    :data:`MAX_KELLY_RISK_PCT`.

    **There is no parameter here, and no field on :class:`SizingConfig`, that
    can see a previous trade.**  No loss streak, no realised P&L, no "recover
    the last one".  A loss can shrink the next position — through ``equity_usd``
    — and can never grow it.  That is what makes martingale unreachable rather
    than merely discouraged.
    """
    if stop_distance_bps <= 0:
        raise ValueError("stop_distance_bps must be positive")
    if method not in ("fixed_risk", "fractional_kelly"):
        raise ValueError("method must be 'fixed_risk' or 'fractional_kelly'")

    notes: list[str] = []
    multiplier = edge_confidence_multiplier(edge_bps, confidence, config=config)
    if multiplier <= 0:
        return SizingResult(
            notional_usd=0.0,
            risk_usd=0.0,
            qty_multiplier=0.0,
            method=method,
            bound_by="no_edge",
            notes=("edge_bps <= 0",),
        )

    fixed_risk_usd = config.equity_usd * config.risk_per_trade_pct / 100.0
    if method == "fixed_risk":
        risk_usd = fixed_risk_usd * multiplier
        bound_by = "risk_per_trade"
    else:
        if payoff_ratio is None:
            raise ValueError("fractional_kelly needs a payoff_ratio")
        # ``confidence`` is the calibrated win probability; Kelly wants exactly
        # that, and it is the same number the fixed-risk path scales with.
        f = kelly_fraction_of_equity(confidence, payoff_ratio, fraction=kelly_fraction)
        kelly_risk = config.equity_usd * f
        cap = config.equity_usd * MAX_KELLY_RISK_PCT / 100.0
        risk_usd = min(kelly_risk, cap)
        bound_by = "kelly_risk_cap" if kelly_risk > cap else "fractional_kelly"
        notes.append(f"kelly fraction of equity {f:.4f} (capped at {cap:.2f})")

    notional = risk_usd / (stop_distance_bps / _BPS)

    pct_cap = config.equity_usd * config.max_position_pct_equity / 100.0
    if notional > config.max_position_usd:
        notional = config.max_position_usd
        bound_by = "max_position_usd"
        notes.append("clamp:max_position_usd")
    if notional > pct_cap:
        notional = pct_cap
        bound_by = "max_position_pct_equity"
        notes.append("clamp:max_position_pct_equity")

    risk_usd = min(risk_usd, notional * stop_distance_bps / _BPS)

    if notional < config.min_notional_usd:
        return SizingResult(
            notional_usd=0.0,
            risk_usd=0.0,
            qty_multiplier=multiplier,
            method=method,
            bound_by="below_min_notional",
            notes=tuple(
                [*notes, f"notional ${notional:.2f} below the exchange minimum"]
            ),
        )

    return SizingResult(
        notional_usd=notional,
        risk_usd=risk_usd,
        qty_multiplier=multiplier,
        method=method,
        bound_by=bound_by,
        notes=tuple(notes),
    )
