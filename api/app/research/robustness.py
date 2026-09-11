"""Robustness — the layer that decides whether a strategy may be called profitable.

Nothing else in the research package is allowed to promote a candidate.  A
tournament produces arms; this module tries to break them and reports, in
readable English, exactly which of the seven promotion gates in
``docs/PHASE3_SPEC.md`` each arm passed and which it failed.

Three tools, in the order they are meant to be used:

``stress_suite``
    Re-price the arm under worse conditions — fees +25% / +50%, slippage ×2,
    a worse spread, latency ×2, missed fills, a random execution delay — and
    say whether the edge survives each one.
``monte_carlo``
    Resample the trade *order* to ask the only question a $500 account cares
    about: how often does this end below where it started?
``evaluate_gates`` / ``finalists``
    Apply gates 1–7.  ``finalists`` returns only the arms that passed, and is
    fully expected to return ``[]`` — "no strategy passed" is a first-class
    result, printed as such by :func:`format_gate_reports`, not an error.

Three honesty rules are wired into the code rather than left to the reader:

1.  **A stress that could not be evaluated is never counted as a pass.**  Fees,
    slippage and spread are per-trade costs, so they can be re-applied to the
    closed trades exactly and cheaply.  Latency, missed fills and execution
    delay change *which trades happen*, not merely what they cost, so they need
    a real re-run.  Without an injected ``replay`` those three come back with
    ``evaluated=False`` and ``method="not-evaluated"``, and the report says
    "not evaluated" in those words.
2.  **Never ``passed=True`` with an empty reason list.**  ``GateReport.reasons``
    always carries one line per gate, pass or fail.
3.  **Marginal is not a finalist.**  A profit factor in [1.00, 1.20) is
    reported as MARGINAL and never promoted.

Accounting notes that matter for the arithmetic below:

* ``ClosedTrade.net_pnl_usd`` is ``gross − entry_fee − exit_fee``.  Slippage and
  spread are **already embedded in the fill prices**, which is why
  ``slippage_usd`` is itemised separately rather than subtracted again.  So
  "slippage ×2" costs one *extra* ``slippage_usd``, and "fees +25%" costs an
  extra ``0.25 × fees_usd``.  Every stress is applied as a delta to the net the
  arm actually reported, which keeps it exact against hand computation.
* ``PerfStats.max_drawdown`` is USD, peak-to-trough, matching every other money
  field on that dataclass.  Where this module recomputes it from a trade list
  it uses the **closed-trade equity curve** (starting equity plus the running
  sum of net P&L).  That curve cannot see intra-trade excursion, so a drawdown
  computed here is a floor, not a ceiling.  It is stated again in
  :func:`compute_stats`.

PAPER ONLY.  Nothing in this file places, prices or signs an order.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from app.types import ClosedTrade, PerfStats

if TYPE_CHECKING:  # pragma: no cover - import kept out of the runtime path
    # ``tournament.py`` is owned by another workstream and may not exist yet.
    # Nothing here touches it at runtime; the arm is consumed structurally
    # (``candidate``/``trades``/``windows``/``out_of_sample``), exactly as the
    # frozen contract in docs/PHASE3_SPEC.md describes it.
    from app.research.tournament import ArmResult

__all__ = [
    "StressResult",
    "StressScenario",
    "MonteCarloResult",
    "GateReport",
    "compute_stats",
    "apply_cost_stress",
    "stress_suite",
    "monte_carlo",
    "evaluate_gates",
    "finalists",
    "format_gate_report",
    "format_gate_reports",
    "SCENARIOS",
    "MIN_OOS_TRADES",
    "MIN_PROFIT_FACTOR",
    "MARGINAL_PROFIT_FACTOR",
    "MAX_DRAWDOWN_PCT",
    "MAX_PROB_BELOW_START",
    "SPREAD_STRESS_BPS_PER_SIDE",
]

_BPS = 10_000.0

# --------------------------------------------------------------------------- #
# Gate thresholds — spelled out here so lowering one is a visible diff.
# --------------------------------------------------------------------------- #

#: Gate 7.  Below this the sample is noise, however good it looks.
MIN_OOS_TRADES = 30

#: Gate 2.  At or above this an arm may be a FINALIST.
MIN_PROFIT_FACTOR = 1.20

#: Gate 2.  In [MARGINAL, MIN) an arm is reported MARGINAL and never promoted.
MARGINAL_PROFIT_FACTOR = 1.00

#: Gate 3.  Max drawdown as a percentage of the $500 account.  $100 of a $500
#: book needs a 25% gain to recover, which is larger than anything the measured
#: venue edge delivers over a research horizon — so past here the account is
#: effectively out of the game, not merely bruised.
MAX_DRAWDOWN_PCT = 20.0

#: Gate 3, capital-survival half.  A positive-expectancy strategy that ends
#: below its starting capital more than a third of the time is not deployable
#: on $500, and burying that behind a headline expectancy is the exact failure
#: mode this module exists to prevent.
MAX_PROB_BELOW_START = 0.35

#: "Worse spread" stress: one extra basis point of spread paid on *each* leg.
#: Measured Binance.US spreads are 0.50 bps (BTC), 0.53 (ETH), 2.02 (SOL), so
#: this is roughly a doubling on the majors — see docs/ECONOMICS.md.
SPREAD_STRESS_BPS_PER_SIDE = 0.5


# --------------------------------------------------------------------------- #
# Contracts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StressScenario:
    """One worse world, described in full so a ``replay`` can reproduce it.

    The analytic fields are deltas applied to the closed trades.  The
    path-dependent fields (``latency_multiplier``, ``missed_fill_rate``,
    ``exec_delay_ms``) cannot be applied to an existing trade list at all —
    they change which trades exist — and are therefore only meaningful to an
    injected ``replay`` callable.
    """

    label: str
    fee_multiplier: float = 1.0
    slippage_multiplier: float = 1.0
    spread_extra_bps_per_side: float = 0.0
    latency_multiplier: float = 1.0
    missed_fill_rate: float = 0.0
    exec_delay_ms: int = 0
    seed: int = 7

    @property
    def analytic(self) -> bool:
        """True when the scenario is purely a per-trade cost change."""
        return (
            self.latency_multiplier == 1.0
            and self.missed_fill_rate == 0.0
            and self.exec_delay_ms == 0
        )


@dataclass
class StressResult:
    """Outcome of one stress.

    ``label``/``stats``/``survived`` are the frozen contract and keep their
    constructor order.  The rest are defaulted additions, because "this stress
    could not be evaluated" and "this stress passed" must never look alike:
    an unevaluated stress carries ``survived=False`` *and* ``evaluated=False``,
    and the gate logic counts it as neither a pass nor a fail — it names it.
    """

    label: str
    stats: PerfStats
    survived: bool
    evaluated: bool = True
    method: str = "analytic"        # "analytic" | "replay" | "not-evaluated"
    detail: str = ""
    scenario: StressScenario | None = None

    @property
    def line(self) -> str:
        if not self.evaluated:
            return f"{self.label}: not evaluated — {self.detail or 'no replay supplied'}"
        verdict = "survives" if self.survived else "FAILS"
        return (
            f"{self.label}: {verdict} "
            f"(expectancy ${self.stats.expectancy:+.4f}/trade, "
            f"net ${self.stats.net_pnl:+.2f}, PF {_pf(self.stats.profit_factor)}, "
            f"via {self.method})"
        )


@dataclass
class MonteCarloResult:
    """Bootstrap of the trade sequence.  Drawdowns are **USD**, as in PerfStats."""

    median_drawdown: float = 0.0
    p95_drawdown: float = 0.0
    worst_loss_streak: int = 0
    prob_below_start: float = 0.0
    samples: int = 0
    # Defaulted extras; the frozen constructor order above is untouched.
    median_drawdown_pct: float = 0.0
    p95_drawdown_pct: float = 0.0
    median_final_equity: float = 0.0
    p05_final_equity: float = 0.0
    trades_per_sample: int = 0
    equity: float = 0.0
    note: str = ""

    @property
    def prob_below_start_pct(self) -> float:
        return self.prob_below_start * 100.0


@dataclass
class GateReport:
    """Why one candidate is, or is not, allowed to be called profitable."""

    candidate_key: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    stress: list[StressResult] = field(default_factory=list)
    monte_carlo: MonteCarloResult | None = None
    # Defaulted extras; the frozen constructor order above is untouched.
    marginal: bool = False
    caveats: list[str] = field(default_factory=list)
    gate_results: dict[int, bool] = field(default_factory=dict)

    @property
    def failed_gates(self) -> list[int]:
        return sorted(n for n, ok in self.gate_results.items() if not ok)

    @property
    def verdict(self) -> str:
        if self.passed:
            return "FINALIST"
        return "MARGINAL" if self.marginal else "REJECTED"


# --------------------------------------------------------------------------- #
# Stats recomputation
# --------------------------------------------------------------------------- #


def _pf(value: float) -> str:
    if value == float("inf"):
        return "inf"
    return f"{value:.2f}"


def compute_stats(
    trades: Sequence[ClosedTrade], *, equity: float = 500.0
) -> PerfStats:
    """Recompute :class:`PerfStats` from a list of closed trades.

    Deliberately mirrors ``Ledger.stats`` field for field — wins and losses are
    split on **net** P&L, ``avg_loss`` is a positive magnitude, ``profit_factor``
    is ``inf`` when there are no losers and ``0.0`` when there is nothing at all
    — so a stressed number can be compared straight against the number the
    tournament reported.

    The one thing it *cannot* reproduce is the ledger's equity curve.  A ledger
    marks open positions bar by bar; here the only observations are the closed
    trades, so ``max_drawdown`` is peak-to-trough on ``equity + cumulative net
    P&L``.  That curve is blind to what a position did while it was open, which
    makes the drawdown reported here a **floor**.  Treat it as the best case.
    """
    stats = PerfStats()
    trades = list(trades)
    if not trades:
        return stats

    wins = [t for t in trades if t.net_pnl_usd > 0]
    losses = [t for t in trades if t.net_pnl_usd < 0]

    stats.trades = len(trades)
    stats.wins = len(wins)
    stats.losses = len(losses)
    stats.gross_pnl = sum(t.gross_pnl_usd for t in trades)
    stats.fees = sum(t.fees_usd for t in trades)
    stats.slippage = sum(t.slippage_usd for t in trades)
    stats.net_pnl = sum(t.net_pnl_usd for t in trades)
    stats.avg_win = (sum(t.net_pnl_usd for t in wins) / len(wins)) if wins else 0.0
    stats.avg_loss = (
        abs(sum(t.net_pnl_usd for t in losses)) / len(losses) if losses else 0.0
    )
    gross_win = sum(t.net_pnl_usd for t in wins)
    gross_loss = abs(sum(t.net_pnl_usd for t in losses))
    if gross_loss > 0:
        stats.profit_factor = gross_win / gross_loss
    else:
        stats.profit_factor = float("inf") if gross_win > 0 else 0.0
    stats.expectancy = stats.net_pnl / stats.trades
    stats.avg_holding_seconds = sum(t.holding_seconds for t in trades) / len(trades)
    stats.max_drawdown = _drawdown_usd([t.net_pnl_usd for t in trades], equity)
    if equity > 0:
        stats.capital_turnover = sum(t.notional_usd for t in trades) / equity
    return stats


def _drawdown_usd(pnls: Sequence[float], equity: float) -> float:
    """Peak-to-trough, in USD, of ``equity`` plus the running sum of ``pnls``."""
    curve = equity
    peak = equity
    worst = 0.0
    for pnl in pnls:
        curve += pnl
        peak = max(peak, curve)
        worst = max(worst, peak - curve)
    return worst


# --------------------------------------------------------------------------- #
# Cost stress
# --------------------------------------------------------------------------- #


def apply_cost_stress(
    trades: Sequence[ClosedTrade], scenario: StressScenario
) -> list[ClosedTrade]:
    """Re-price closed trades under a cost scenario.  Pure, exact, cheap.

    Every change is expressed as a **delta on the reported net**, never as a
    re-derivation from gross, so the arithmetic is exactly what a reader would
    do by hand:

    * fees ×m            → ``net −= (m − 1) × fees_usd``
    * slippage ×m        → ``net −= (m − 1) × slippage_usd``
      (slippage is already inside the fill prices, so doubling it means paying
      one *extra* helping of it, not two.)
    * worse spread       → ``net −= notional × bps_per_side × 2 / 10_000``

    The itemised cost fields on the returned trades move with the net, so a
    stressed trade list stays internally consistent and can be stressed again.
    """
    out: list[ClosedTrade] = []
    fee_delta = scenario.fee_multiplier - 1.0
    slip_delta = scenario.slippage_multiplier - 1.0
    spread_bps = scenario.spread_extra_bps_per_side
    for t in trades:
        extra_fees = fee_delta * t.fees_usd
        extra_slip = slip_delta * t.slippage_usd
        extra_spread = abs(t.notional_usd) * spread_bps * 2.0 / _BPS
        extra = extra_fees + extra_slip + extra_spread
        out.append(
            replace(
                t,
                entry_fee_usd=t.entry_fee_usd * scenario.fee_multiplier,
                exit_fee_usd=t.exit_fee_usd * scenario.fee_multiplier,
                slippage_usd=t.slippage_usd * scenario.slippage_multiplier
                + extra_spread,
                net_pnl_usd=t.net_pnl_usd - extra,
            )
        )
    return out


#: The seven worse worlds of gate 6, in the order the spec lists them.
SCENARIOS: tuple[StressScenario, ...] = (
    StressScenario(label="fees +25%", fee_multiplier=1.25),
    StressScenario(label="fees +50%", fee_multiplier=1.50),
    StressScenario(label="slippage x2", slippage_multiplier=2.0),
    StressScenario(
        label="worse spread", spread_extra_bps_per_side=SPREAD_STRESS_BPS_PER_SIDE
    ),
    StressScenario(label="latency x2", latency_multiplier=2.0),
    StressScenario(label="missed fills", missed_fill_rate=0.35),
    StressScenario(label="random execution delay", exec_delay_ms=750, seed=7),
)

#: Failing this one earns the word "fragile" in the report, by name.
FRAGILITY_SCENARIO = "fees +25%"


def _survived(stats: PerfStats) -> bool:
    """A stress is survived when the *net* edge is still positive after it."""
    return stats.trades > 0 and stats.expectancy > 0 and stats.net_pnl > 0


def _as_stats(
    value: Any, *, equity: float
) -> PerfStats | None:
    """Normalise what a ``replay`` gave back into ``PerfStats``."""
    if value is None:
        return None
    if isinstance(value, PerfStats):
        return value
    if isinstance(value, (list, tuple)):
        return compute_stats(list(value), equity=equity)
    # Duck-typed: an ArmResult-shaped object carrying stats/trades.
    stats = getattr(value, "out_of_sample", None) or getattr(value, "stats", None)
    if isinstance(stats, PerfStats):
        return stats
    trades = getattr(value, "trades", None)
    if isinstance(trades, (list, tuple)):
        return compute_stats(list(trades), equity=equity)
    return None


def stress_suite(
    arm: "ArmResult",
    *,
    replay: Callable[..., Any] | None = None,
    equity: float = 500.0,
    trades: Sequence[ClosedTrade] | None = None,
    scenarios: Sequence[StressScenario] = SCENARIOS,
) -> list[StressResult]:
    """Re-run an arm under worse conditions and report what survives.

    Two mechanisms, and the output always says which one produced a number:

    * ``method="analytic"`` — fees, slippage and spread are per-trade costs
      itemised on :class:`~app.types.ClosedTrade`, so they are re-applied to the
      existing trades.  Exact, deterministic, no re-run needed.
    * ``method="replay"`` — latency, missed fills and execution delay change
      *which* trades happen.  No amount of arithmetic on a finished trade list
      can model that, so these need the tournament to hand us a ``replay``.

    ``replay`` is called as ``replay(arm, scenario)`` and may return a
    ``list[ClosedTrade]``, a ``PerfStats``, or an arm-shaped object.  If it is
    absent, returns ``None``, or raises, the scenario comes back with
    ``evaluated=False`` and ``method="not-evaluated"`` — **never** as a pass.
    Silently reporting the analytic approximation as if it covered latency would
    be the same lie this module exists to prevent.
    """
    base_trades = list(trades if trades is not None else _stress_trades(arm))
    results: list[StressResult] = []

    for scenario in scenarios:
        if scenario.analytic:
            stressed = apply_cost_stress(base_trades, scenario)
            stats = compute_stats(stressed, equity=equity)
            results.append(
                StressResult(
                    label=scenario.label,
                    stats=stats,
                    survived=_survived(stats),
                    evaluated=True,
                    method="analytic",
                    detail="re-priced from the closed trades",
                    scenario=scenario,
                )
            )
            continue

        if replay is None:
            results.append(
                StressResult(
                    label=scenario.label,
                    stats=PerfStats(),
                    survived=False,
                    evaluated=False,
                    method="not-evaluated",
                    detail=(
                        "no replay supplied; this stress changes which trades "
                        "happen and cannot be applied to a finished trade list"
                    ),
                    scenario=scenario,
                )
            )
            continue

        try:
            returned = replay(arm, scenario)
        except Exception as exc:  # noqa: BLE001 - a broken replay is a finding
            results.append(
                StressResult(
                    label=scenario.label,
                    stats=PerfStats(),
                    survived=False,
                    evaluated=False,
                    method="not-evaluated",
                    detail=f"replay raised {type(exc).__name__}: {exc}",
                    scenario=scenario,
                )
            )
            continue

        stats = _as_stats(returned, equity=equity)
        if stats is None:
            results.append(
                StressResult(
                    label=scenario.label,
                    stats=PerfStats(),
                    survived=False,
                    evaluated=False,
                    method="not-evaluated",
                    detail="replay returned nothing usable",
                    scenario=scenario,
                )
            )
            continue

        results.append(
            StressResult(
                label=scenario.label,
                stats=stats,
                survived=_survived(stats),
                evaluated=True,
                method="replay",
                detail="re-run under the scenario",
                scenario=scenario,
            )
        )

    return results


# --------------------------------------------------------------------------- #
# Monte Carlo
# --------------------------------------------------------------------------- #


def monte_carlo(
    trades: Sequence[ClosedTrade],
    *,
    equity: float = 500.0,
    samples: int = 2000,
    seed: int = 7,
) -> MonteCarloResult:
    """Bootstrap the trade sequence to size the risk to the *account*.

    We start with $500 and capital survival is the point, so the headline number
    here is ``prob_below_start``: the share of resampled histories that end
    below the starting capital.  A strategy with positive expectancy and a 30%
    chance of halving the account is not deployable, and that fact belongs in
    the report rather than behind an average.

    **Method, and its limitation.**  Each sample draws ``n`` trades *with
    replacement from the observed trades* and replays them in the drawn order.
    We resample the trade order, not returns from a fitted distribution, so the
    empirical distribution — fat tails, the real win/loss asymmetry, the one
    enormous outlier — is preserved exactly instead of being smoothed into a
    normal that never existed.

    The price of that choice: **bootstrapping destroys serial correlation.**
    Drawing independently scatters clustered losses evenly through the sample,
    so a strategy whose losses arrive in runs — every trend strategy in a chop,
    every mean-reversion strategy in a trend — will look *better* here than it
    was.  The drawdown and loss-streak figures below are therefore optimistic
    for exactly the strategies most likely to hurt a small account, and
    ``worst_loss_streak`` should be read against the streak actually observed
    in the real sequence, not instead of it.

    Two further simplifications, both stated rather than hidden: P&L is replayed
    in absolute dollars with no compounding and no position resizing as equity
    moves, and drawdown is measured on the closed-trade curve, which cannot see
    intra-trade excursion.  Both make the numbers here a floor.
    """
    result = MonteCarloResult(equity=equity)
    trades = list(trades)
    if not trades:
        result.note = "no trades — nothing to resample"
        return result
    if samples <= 0:
        result.note = "samples <= 0 — nothing to resample"
        return result

    pnls = [t.net_pnl_usd for t in trades]
    n = len(pnls)
    rng = random.Random(seed)

    drawdowns: list[float] = []
    finals: list[float] = []
    below = 0
    worst_streak = 0

    for _ in range(samples):
        draw = [pnls[rng.randrange(n)] for _ in range(n)]
        curve = equity
        peak = equity
        worst = 0.0
        streak = 0
        run = 0
        for pnl in draw:
            curve += pnl
            peak = max(peak, curve)
            worst = max(worst, peak - curve)
            if pnl < 0:
                run += 1
                streak = max(streak, run)
            else:
                run = 0
        drawdowns.append(worst)
        finals.append(curve)
        if curve < equity:
            below += 1
        worst_streak = max(worst_streak, streak)

    drawdowns.sort()
    finals.sort()
    result.median_drawdown = _quantile(drawdowns, 0.50)
    result.p95_drawdown = _quantile(drawdowns, 0.95)
    result.worst_loss_streak = worst_streak
    result.prob_below_start = below / samples
    result.samples = samples
    result.trades_per_sample = n
    result.median_final_equity = _quantile(finals, 0.50)
    result.p05_final_equity = _quantile(finals, 0.05)
    if equity > 0:
        result.median_drawdown_pct = result.median_drawdown / equity * 100.0
        result.p95_drawdown_pct = result.p95_drawdown / equity * 100.0
    result.note = (
        f"{samples} bootstrap resamples of {n} trades, seed {seed}; "
        "order resampled, serial correlation destroyed (optimistic for "
        "strategies whose losses cluster)"
    )
    return result


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile of an already-sorted sequence."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(sorted_values[int(pos)])
    frac = pos - lo
    return float(sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac)


# --------------------------------------------------------------------------- #
# Arm accessors — structural, so this module never imports tournament.py
# --------------------------------------------------------------------------- #


def _candidate_key(arm: Any) -> str:
    candidate = getattr(arm, "candidate", None)
    key = getattr(candidate, "key", None)
    if callable(key):
        try:
            return str(key())
        except Exception:  # noqa: BLE001 - a broken key must not hide the report
            pass
    if isinstance(key, str):
        return key
    return str(candidate) if candidate is not None else "<unnamed candidate>"


def _oos_stats(arm: Any) -> PerfStats:
    stats = getattr(arm, "out_of_sample", None)
    if isinstance(stats, PerfStats):
        return stats
    stats = getattr(arm, "stats", None)
    return stats if isinstance(stats, PerfStats) else PerfStats()


def _oos_trades(arm: Any) -> tuple[list[ClosedTrade], str]:
    """The out-of-sample trades, and an honest label for where they came from.

    The frozen ``ArmResult`` contract carries out-of-sample *statistics* but
    only one trade list.  If the tournament also exposes the out-of-sample
    trades (``out_of_sample_trades`` / ``oos_trades``) we use them; otherwise we
    fall back to ``trades`` and say so in the reason line, because a
    single-best-trade test run over in-sample trades is answering a slightly
    different question and the reader is entitled to know.
    """
    for name in ("out_of_sample_trades", "oos_trades"):
        candidate = getattr(arm, name, None)
        if isinstance(candidate, (list, tuple)) and candidate:
            return list(candidate), name
    candidate = getattr(arm, "trades", None)
    if isinstance(candidate, (list, tuple)):
        return list(candidate), "trades"
    return [], "trades"


def _stress_trades(arm: Any) -> list[ClosedTrade]:
    return _oos_trades(arm)[0]


def _windows(arm: Any) -> list[PerfStats]:
    windows = getattr(arm, "windows", None)
    if isinstance(windows, (list, tuple)):
        return [w for w in windows if isinstance(w, PerfStats)]
    return []


# --------------------------------------------------------------------------- #
# The gates
# --------------------------------------------------------------------------- #


def _line(number: int, ok: bool, title: str, detail: str) -> str:
    return f"[{'PASS' if ok else 'FAIL'}] gate {number} {title}: {detail}"


def evaluate_gates(
    arm: "ArmResult",
    *,
    equity: float = 500.0,
    replay: Callable[..., Any] | None = None,
    samples: int = 2000,
    seed: int = 7,
    min_trades: int = MIN_OOS_TRADES,
    min_profit_factor: float = MIN_PROFIT_FACTOR,
    max_drawdown_pct: float = MAX_DRAWDOWN_PCT,
    max_prob_below_start: float = MAX_PROB_BELOW_START,
    stress: Sequence[StressResult] | None = None,
) -> GateReport:
    """Apply the seven promotion gates of ``docs/PHASE3_SPEC.md``, out-of-sample.

    Every gate contributes exactly one line to ``reasons`` whether it passed or
    failed, so the final report can print why something did *or did not*
    qualify without re-deriving anything.  ``passed`` is the AND of all seven.
    A profit factor in [1.00, 1.20) sets ``marginal`` and is reported as
    MARGINAL — it is never a finalist, and the gate still fails.
    """
    key = _candidate_key(arm)
    oos = _oos_stats(arm)
    trades, source = _oos_trades(arm)
    windows = _windows(arm)
    reasons: list[str] = []
    caveats: list[str] = []
    gates: dict[int, bool] = {}

    mc = monte_carlo(trades, equity=equity, samples=samples, seed=seed)
    stress_results = (
        list(stress)
        if stress is not None
        else stress_suite(arm, replay=replay, equity=equity, trades=trades)
    )

    # -- gate 1: OOS net expectancy > 0 ------------------------------------- #
    g1 = oos.trades > 0 and oos.expectancy > 0
    reasons.append(
        _line(
            1,
            g1,
            "OOS net expectancy > 0",
            f"${oos.expectancy:+.4f} per trade over {oos.trades} OOS trades"
            + ("" if oos.trades else " — no out-of-sample trades to measure"),
        )
    )
    gates[1] = g1

    # -- gate 2: profit factor >= 1.20 -------------------------------------- #
    pf = oos.profit_factor
    marginal = MARGINAL_PROFIT_FACTOR <= pf < min_profit_factor
    g2 = pf >= min_profit_factor
    detail = f"OOS profit factor {_pf(pf)} (need >= {min_profit_factor:.2f})"
    if marginal:
        detail += " — MARGINAL: positive but below the bar, reported, never promoted"
        caveats.append(
            f"profit factor {_pf(pf)} is MARGINAL (1.00-1.20): reportable, not a finalist"
        )
    reasons.append(_line(2, g2, "OOS profit factor", detail))
    gates[2] = g2

    # -- gate 3: drawdown + capital survival against $500 -------------------- #
    dd_limit = equity * max_drawdown_pct / 100.0
    dd = oos.max_drawdown
    dd_ok = dd <= dd_limit
    survival_ok = mc.samples == 0 or mc.prob_below_start <= max_prob_below_start
    g3 = dd_ok and survival_ok
    detail = (
        f"max drawdown ${dd:.2f} vs ${dd_limit:.2f} "
        f"({max_drawdown_pct:.0f}% of a ${equity:.0f} account)"
    )
    if mc.samples:
        detail += (
            f"; Monte Carlo p95 drawdown ${mc.p95_drawdown:.2f} "
            f"({mc.p95_drawdown_pct:.1f}%), probability of ending below "
            f"${equity:.0f}: {mc.prob_below_start_pct:.1f}% "
            f"(limit {max_prob_below_start * 100:.0f}%)"
        )
    else:
        detail += "; Monte Carlo not run (no trades)"
    if not survival_ok:
        detail += " — capital survival fails: positive expectancy does not make this deployable"
    reasons.append(_line(3, g3, "drawdown reasonable for the account", detail))
    gates[3] = g3

    # -- gate 4: not dominated by one trade ---------------------------------- #
    if len(trades) >= 2:
        best = max(trades, key=lambda t: t.net_pnl_usd)
        remaining = [t for t in trades if t is not best]
        ex_best = compute_stats(remaining, equity=equity)
        g4 = ex_best.expectancy > 0
        share = (
            best.net_pnl_usd / sum(t.net_pnl_usd for t in trades) * 100.0
            if sum(t.net_pnl_usd for t in trades) > 0
            else float("nan")
        )
        detail = (
            f"removing the single best trade (${best.net_pnl_usd:+.2f}"
            + (f", {share:.0f}% of all net P&L" if share == share else "")
            + f") leaves expectancy ${ex_best.expectancy:+.4f} per trade"
        )
        if not g4:
            detail += " — the result is one trade wearing a strategy's name"
    else:
        g4 = False
        ex_best = PerfStats()
        detail = f"needs at least 2 trades to test; have {len(trades)}"
    if source == "trades" and trades:
        caveats.append(
            "gate 4 ran on the arm's full trade list: the tournament did not "
            "expose a separate out-of-sample trade list"
        )
    reasons.append(_line(4, g4, "not dominated by one trade", detail))
    gates[4] = g4

    # -- gate 5: majority of walk-forward windows positive ------------------- #
    scored = [w for w in windows if w.trades > 0]
    if scored:
        positive = sum(1 for w in scored if w.net_pnl > 0)
        g5 = positive * 2 > len(scored)
        detail = (
            f"{positive}/{len(scored)} walk-forward windows net positive "
            f"(need a strict majority)"
        )
        if not g5:
            detail += " — positive in aggregate but not repeatable across time"
    else:
        g5 = False
        detail = "no walk-forward windows with trades were reported"
    reasons.append(_line(5, g5, "multiple test windows positive", detail))
    gates[5] = g5

    # -- gate 6: survives cost stress ---------------------------------------- #
    evaluated = [s for s in stress_results if s.evaluated]
    unevaluated = [s for s in stress_results if not s.evaluated]
    failed = [s for s in evaluated if not s.survived]
    g6 = bool(evaluated) and not failed
    if evaluated:
        detail = (
            f"{len(evaluated) - len(failed)}/{len(evaluated)} evaluated stresses "
            "survived"
        )
    else:
        detail = "no stress could be evaluated"
    if failed:
        detail += " — fails: " + ", ".join(s.label for s in failed)
    if any(s.label == FRAGILITY_SCENARIO for s in failed):
        fragile = (
            "FRAGILE: the edge evaporates when fees rise 25% — "
            "this strategy is fragile and must not be promoted"
        )
        detail += f"; {fragile}"
        caveats.append(fragile)
    if unevaluated:
        names = ", ".join(s.label for s in unevaluated)
        detail += (
            f"; {len(unevaluated)} not evaluated (no replay supplied): {names} "
            "— counted as neither pass nor fail"
        )
        caveats.append(
            f"not evaluated, so unproven rather than passed: {names}"
        )
    reasons.append(_line(6, g6, "survives cost stress", detail))
    gates[6] = g6

    # -- gate 7: minimum out-of-sample sample size --------------------------- #
    observed = oos.trades or len(trades)
    g7 = observed >= min_trades
    reasons.append(
        _line(
            7,
            g7,
            "minimum OOS sample",
            f"{observed} out-of-sample trades (need >= {min_trades})"
            + ("" if g7 else " — below this the result is indistinguishable from noise"),
        )
    )
    gates[7] = g7

    passed = all(gates.values())
    if passed and not reasons:  # defensive: a pass must always carry its reasons
        passed = False
        reasons.append("[FAIL] no gate produced a reason — refusing to promote")

    report = GateReport(
        candidate_key=key,
        passed=passed,
        reasons=reasons,
        stress=stress_results,
        monte_carlo=mc if mc.samples else None,
        marginal=marginal,
        caveats=caveats,
        gate_results=gates,
    )
    return report


def finalists(arms: Sequence["ArmResult"], **kw: Any) -> list[GateReport]:
    """Gate every arm and return only the reports that passed — possibly none.

    An empty list is the expected outcome, not an error: on this venue, at this
    horizon, "no strategy passed" is the most likely honest answer, and
    :func:`format_gate_reports` prints it in exactly those words.
    """
    return [r for r in evaluate_all(arms, **kw) if r.passed]


def evaluate_all(arms: Sequence["ArmResult"], **kw: Any) -> list[GateReport]:
    """Gate every arm — winners *and* losers, because every arm gets reported."""
    return [evaluate_gates(arm, **kw) for arm in arms]


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def format_gate_report(report: GateReport) -> str:
    """One candidate, rendered so the verdict and its reasons cannot separate."""
    lines = [f"{report.candidate_key} — {report.verdict}"]
    lines.extend(f"  {r}" for r in report.reasons)
    if report.stress:
        lines.append("  stress:")
        lines.extend(f"    {s.line}" for s in report.stress)
    mc = report.monte_carlo
    if mc is not None:
        lines.append(
            f"  monte carlo ({mc.samples} resamples of {mc.trades_per_sample} trades): "
            f"median drawdown ${mc.median_drawdown:.2f}, p95 ${mc.p95_drawdown:.2f}, "
            f"worst loss streak {mc.worst_loss_streak}, "
            f"P(end below ${mc.equity:.0f}) = {mc.prob_below_start_pct:.1f}%"
        )
        lines.append(f"    note: {mc.note}")
    for caveat in report.caveats:
        lines.append(f"  caveat: {caveat}")
    return "\n".join(lines)


def format_gate_reports(reports: Sequence[GateReport]) -> str:
    """The whole table, with the empty case spelled out rather than implied."""
    if not reports:
        return (
            "NO ARMS WERE EVALUATED — nothing reached the promotion gates.\n"
            "That is a result, not an error."
        )
    passed = [r for r in reports if r.passed]
    marginal = [r for r in reports if not r.passed and r.marginal]
    body = "\n\n".join(format_gate_report(r) for r in reports)
    if passed:
        header = f"FINALISTS: {len(passed)} of {len(reports)} arms passed all seven gates."
    else:
        header = (
            f"NO STRATEGY PASSED THE PROMOTION GATES. "
            f"{len(reports)} arms were evaluated and every one of them failed at "
            "least one gate."
        )
        if marginal:
            header += (
                f" {len(marginal)} were MARGINAL (profit factor between 1.00 and "
                f"{MIN_PROFIT_FACTOR:.2f}) and are reported as such, not promoted."
            )
        header += (
            " This is the honest answer the search produced; do not lower the "
            "gates to manufacture a winner."
        )
    return f"{header}\n\n{body}"
