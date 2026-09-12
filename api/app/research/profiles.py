"""The three-profile paper comparison — PHASE5_SPEC.md.

STANDARD (3% total open risk), AGGRESSIVE (5%) and HIGH-ACTIVITY (7.5%), each
crossed with the deployment bands 60 / 70 / 80 / 85%.  Twelve arms, one engine.

**Nothing here is a second backtester.**  Every arm is one call to
:func:`app.backtest.engine.run_backtest` over the *same* bars; the only thing
that differs is the ``Settings`` object handed to the engine — the same pattern
``app.backtest.experiments`` uses for V2.  If an arm looks better, the reason is
in the settings diff and nowhere else, and ``ProfileResult.settings_used``
records the object the engine actually saw so a reader can check.

Why Phase 3's three allocation modes produced byte-identical books
-----------------------------------------------------------------
``docs/PHASE3_REPORT.md`` measured peak concurrent opportunities of **2** with
99.5% of capital idle, and few-large / many-small / dynamic came back identical.
They had to: an allocator is a rule for dividing capital *between simultaneous
candidates*, and with at most two candidates ever alive there was nothing to
divide.  The allocation mode was never the binding constraint, so varying it
varied nothing.

That is the trap this module is built to avoid.  A risk *budget* has exactly the
same failure mode — raising the ceiling on a book that never reaches the
ceiling changes nothing — so three things are reported that make the difference
visible rather than assumed:

* ``avg_positions`` / ``max_positions`` — if ``max_positions`` is 2 on every
  arm, the profiles are once again varying a limit nothing touches, and the
  comparison says so in its own notes rather than presenting three numbers.
* ``idle_by_reason`` — *why* the money is not working.  Idle dominated by
  ``EXECUTION_COST`` means the venue is the constraint and no budget will fix
  it; dominated by ``NO_QUALIFIED_SIGNAL`` means supply is.
* the daily loss circuit-breaker counts at −3 / −5 / −7.5%.  A breaker that
  never fires tells us nothing; one that fires constantly is the real
  constraint.

The selection rule
------------------
:func:`select_profile` picks the **most aggressive** profile that still has
positive out-of-sample expectancy, positive net P&L after all costs, and an
acceptable drawdown on a $500 account — never the one with the largest gross
P&L.  It returns "none qualify" as a first-class outcome, because on the
evidence in Phase 3 that is the likely one.
"""

from __future__ import annotations

import enum
import gc
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

from app.backtest.engine import (
    BacktestConfig,
    BacktestResult,
    Components,
    backtest_settings,
    run_backtest,
)
from app.backtest.experiments import apply_overrides, v2_components
from app.config import Settings, get_settings


__all__ = [
    "AGGRESSION_ORDER",
    "BREAKER_THRESHOLDS_PCT",
    "DEFAULT_MAX_DRAWDOWN_PCT",
    "DEFAULT_MIN_TRADES",
    "DEPLOY_BANDS_PCT",
    "HARD_POSITION_CEILING",
    "PROFILES",
    "PROFILE_RISK_PCT",
    "BreakerReport",
    "IdleReason",
    "ProfileResult",
    "RecordedRejection",
    "Selection",
    "attribute_idle_usd",
    "band_overrides",
    "compare_profiles",
    "daily_breakers",
    "format_comparison",
    "idle_reason_for",
    "profile_overrides",
    "recording_components",
    "rejections_for_arm",
    "run_profile_arm",
    "select_profile",
]

MS_PER_DAY = 86_400_000

#: Frozen contract.  Ordered least to most aggressive — ``select_profile``
#: walks this list backwards.
PROFILES: tuple[str, ...] = ("standard", "aggressive", "high_activity")
PROFILE_RISK_PCT: dict[str, float] = {
    "standard": 3.0,
    "aggressive": 5.0,
    "high_activity": 7.5,
}
AGGRESSION_ORDER: dict[str, int] = {name: i for i, name in enumerate(PROFILES)}

#: Target deployed-capital bands, spec §"The ask" (60–85%).
DEPLOY_BANDS_PCT: tuple[float, ...] = (60.0, 70.0, 80.0, 85.0)

#: Spec: 8–15 simultaneous positions, hard ceiling 20.  Identical on every arm
#: so the *risk budget* is the variable and the count cap is not.
HARD_POSITION_CEILING = 20

#: The daily loss circuit breakers the spec asks to be measured.
BREAKER_THRESHOLDS_PCT: tuple[float, ...] = (3.0, 5.0, 7.5)

#: Below this many closed trades an expectancy figure is noise.  Same number
#: the walk-forward report and the V2 harness use, for the same reason.
DEFAULT_MIN_TRADES = 30
#: Peak-to-trough equity loss as a % of a $500 account, above which an arm is
#: disqualified however good its expectancy looks.
DEFAULT_MAX_DRAWDOWN_PCT = 10.0
#: Fraction of the span held back.  Selection reads the holdout, never the whole.
DEFAULT_OOS_FRACTION = 0.30


# --------------------------------------------------------------------------- #
# Idle attribution — against the frozen app.portfolio contract
# --------------------------------------------------------------------------- #


def _portfolio_idle_reason() -> Any:
    """``app.portfolio.IdleReason`` once it lands; a local double until then.

    The capital workstream owns that enum (PHASE5_SPEC.md §Frozen contracts).
    Importing it when present means the keys in ``idle_by_reason`` are the same
    strings the dashboard and the worker use; the double keeps this module
    developable while that work is in flight, and carries the identical members
    so swapping one for the other changes nothing downstream.
    """
    try:
        from app.portfolio import IdleReason as _Real  # type: ignore[attr-defined]

        return _Real
    except Exception:
        return _IdleReasonDouble


class _IdleReasonDouble(str, enum.Enum):
    NO_QUALIFIED_SIGNAL = "NO_QUALIFIED_SIGNAL"
    CORRELATION_LIMIT = "CORRELATION_LIMIT"
    RISK_LIMIT = "RISK_LIMIT"
    LIQUIDITY_LIMIT = "LIQUIDITY_LIMIT"
    EXECUTION_COST = "EXECUTION_COST"
    CASH_RESERVE = "CASH_RESERVE"


IdleReason = _portfolio_idle_reason()

#: ``RejectReason`` -> ``IdleReason``.  Spelled out rather than inferred,
#: because the whole Phase 5 question is settled by which bucket dominates:
#: EXECUTION_COST means the venue is the constraint and a wider universe cannot
#: help; NO_QUALIFIED_SIGNAL means supply is, and it can.
_REASON_TO_IDLE: dict[str, str] = {
    # cost of crossing — the venue
    "NEGATIVE_EDGE": "EXECUTION_COST",
    "EDGE_BELOW_MIN": "EXECUTION_COST",
    "NET_TARGET_UNREACHABLE": "EXECUTION_COST",
    "WIDE_SPREAD": "EXECUTION_COST",
    "SLIPPAGE_TOO_HIGH": "EXECUTION_COST",
    "RANKER_DISQUALIFIED": "EXECUTION_COST",
    # not enough book to trade against
    "THIN_DEPTH": "LIQUIDITY_LIMIT",
    "BELOW_MIN_NOTIONAL": "LIQUIDITY_LIMIT",
    "STALE_BOOK": "LIQUIDITY_LIMIT",
    # our own budgets said no
    "RISK_BUDGET": "RISK_LIMIT",
    "PORTFOLIO_RISK_BUDGET": "RISK_LIMIT",
    "MAX_POSITIONS": "RISK_LIMIT",
    "DUPLICATE_SYMBOL": "RISK_LIMIT",
    "DAILY_LOSS_LIMIT": "RISK_LIMIT",
    "WEEKLY_LOSS_LIMIT": "RISK_LIMIT",
    "LOSS_STREAK_COOLDOWN": "RISK_LIMIT",
    "ABNORMAL_VOLATILITY": "RISK_LIMIT",
    "BAD_REWARD_RISK": "RISK_LIMIT",
    "KILL_SWITCH": "RISK_LIMIT",
    # one bet, many tickets
    "CORRELATED_EXPOSURE": "CORRELATION_LIMIT",
    "DIRECTIONAL_EXPOSURE": "CORRELATION_LIMIT",
    # policy
    "CASH_RESERVE": "CASH_RESERVE",
    # nothing fired at all
    "NOT_SUPPORTED_SHORT": "NO_QUALIFIED_SIGNAL",
    "DATA_FAILURE": "NO_QUALIFIED_SIGNAL",
    "NO_SIGNAL": "NO_QUALIFIED_SIGNAL",
}


def idle_reason_for(reject_reason: Any) -> str:
    """Map one ``RejectReason`` onto the idle bucket it explains.

    An unmapped reason falls to ``NO_QUALIFIED_SIGNAL`` rather than being
    dropped: idle dollars must always add up to the idle total, and quietly
    losing a bucket is how an attribution stops being one.
    """
    key = str(getattr(reject_reason, "value", reject_reason) or "").upper()
    return _REASON_TO_IDLE.get(key, "NO_QUALIFIED_SIGNAL")


def attribute_idle_usd(
    idle_usd: float,
    weights_by_reason: Mapping[str, float],
    *,
    cash_reserve_usd: float = 0.0,
) -> dict[str, float]:
    """Split ``idle_usd`` across the six idle reasons.  Sums to ``idle_usd``.

    The model, stated plainly because it *is* a model: the policy cash reserve
    is carved out first (that money is idle by design, not by refusal), and the
    remainder is divided in proportion to ``weights_by_reason`` — each recorded
    refusal weighted by the **idle dollars sitting there when it was made**, so
    a rejection with $480 doing nothing counts for more than one with $50.

    Once ``app.portfolio.attribute_idle`` lands, the worker's per-cycle
    attribution is the authority and this becomes the backtest's approximation
    of it.  The bucket names are identical on purpose.
    """
    idle_usd = max(float(idle_usd), 0.0)
    out = {r.value: 0.0 for r in IdleReason}
    if idle_usd <= 0:
        return out

    reserve = min(max(float(cash_reserve_usd), 0.0), idle_usd)
    out["CASH_RESERVE"] = reserve
    remaining = idle_usd - reserve
    if remaining <= 0:
        return out

    positive = {k: float(v) for k, v in weights_by_reason.items() if float(v) > 0}
    total = sum(positive.values())
    if total <= 0:
        # Nothing was refused, and the money still is not working.  There was
        # no qualified signal; that is the only honest bucket left.
        out["NO_QUALIFIED_SIGNAL"] += remaining
        return out

    allocated = 0.0
    items = sorted(positive.items())
    for key, weight in items[:-1]:
        share = round(remaining * weight / total, 10)
        out[key] = out.get(key, 0.0) + share
        allocated += share
    last_key = items[-1][0]
    out[last_key] = out.get(last_key, 0.0) + (remaining - allocated)  # exact sum
    return out


# --------------------------------------------------------------------------- #
# Recording the rejections the missed-opportunity audit consumes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RecordedRejection:
    """A refusal, recorded **at the moment it was made**.

    Deliberately the same shape as
    :class:`app.research.missed.RejectedCandidate` so the audit can consume it
    without this module importing the audit — the leakage boundary runs the
    other way, and a future label must never be able to reach a live decision.
    """

    symbol: str
    ts_ms: int
    reason: str
    price: float
    target_bps: float
    stop_bps: float
    side: str = "LONG"
    idle_pct: float = 100.0
    idle_usd: float = 0.0
    strategy: str = ""
    net_edge_bps: float = 0.0
    #: Which arm refused it.  Carried on the record so a caller pooling several
    #: arms' refusals can still audit one arm at a time.
    arm: str = ""


class _RejectionRecorder:
    """Wraps the ranker and the risk engine to keep a record of every refusal.

    The engine counts rejections by reason and nothing else
    (``BacktestResult.rejections``), which is not enough to ask what the market
    did next.  This adds the context without touching ``engine.py``: the
    components are ours to wrap, and the decisions are unchanged — every call is
    forwarded and the real verdict is returned untouched.
    """

    def __init__(self, *, arm: str = "", limit: int = 200_000) -> None:
        self.rejections: list[RecordedRejection] = []
        self.arm = arm
        self.limit = limit
        self.dropped = 0
        self._ledger: Any = None

    # -- book state --------------------------------------------------------- #

    def bind_ledger(self, ledger: Any) -> None:
        self._ledger = ledger

    def _idle(self) -> tuple[float, float]:
        led = self._ledger
        if led is None:
            return 100.0, 0.0
        try:
            equity = float(getattr(led, "equity", 0.0) or 0.0)
            exposure = float(getattr(led, "exposure_usd", 0.0) or 0.0)
        except Exception:
            return 100.0, 0.0
        if equity <= 0:
            return 100.0, 0.0
        idle_usd = max(equity - exposure, 0.0)
        return idle_usd / equity * 100.0, idle_usd

    # -- recording ---------------------------------------------------------- #

    def record(self, opp: Any, reason: Any) -> None:
        if len(self.rejections) >= self.limit:
            self.dropped += 1
            return
        sig = getattr(opp, "signal", None)
        feats = getattr(opp, "features", None)
        if sig is None or feats is None:
            return
        price = float(getattr(feats, "price", 0.0) or getattr(feats, "mid", 0.0) or 0.0)
        if price <= 0:
            return
        idle_pct, idle_usd = self._idle()
        side = getattr(sig.side, "value", sig.side)
        self.rejections.append(
            RecordedRejection(
                symbol=str(sig.symbol),
                ts_ms=int(getattr(feats, "ts_ms", 0) or sig.ts_ms),
                reason=str(getattr(reason, "value", reason) or "UNKNOWN"),
                price=price,
                target_bps=float(sig.target_bps),
                stop_bps=float(sig.stop_bps),
                side=str(side),
                idle_pct=idle_pct,
                idle_usd=idle_usd,
                strategy=str(sig.strategy),
                net_edge_bps=float(getattr(opp, "net_edge_bps", 0.0) or 0.0),
                arm=self.arm,
            )
        )

    # -- aggregates --------------------------------------------------------- #

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rejections:
            out[r.reason] = out.get(r.reason, 0) + 1
        return out

    def idle_weights(self) -> dict[str, float]:
        """Idle-dollar weight per ``IdleReason``, for :func:`attribute_idle_usd`."""
        out: dict[str, float] = {}
        for r in self.rejections:
            bucket = idle_reason_for(r.reason)
            # A refusal made with nothing idle explains no idle dollars, but it
            # is still evidence; floor the weight at 1 so it is never invisible.
            out[bucket] = out.get(bucket, 0.0) + max(r.idle_usd, 1.0)
        return out


class _RecordingRanker:
    def __init__(self, inner: Any, rec: _RejectionRecorder) -> None:
        self._inner = inner
        self._rec = rec

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def qualifies(self, opp: Any) -> bool:
        ok = self._inner.qualifies(opp)
        if not ok:
            self._rec.record(opp, _ranker_reason(opp, self._inner))
        return ok


def _ranker_reason(opp: Any, ranker: Any) -> str:
    """Name the ranker's refusal, since ``qualifies`` only returns a bool."""
    regime = getattr(getattr(opp, "regime", None), "regime", None)
    if str(getattr(regime, "value", regime)) == "ABNORMAL":
        return "ABNORMAL_VOLATILITY"
    sig = getattr(opp, "signal", None)
    if sig is not None and float(getattr(sig, "stop_bps", 0.0)) <= 0:
        return "BAD_REWARD_RISK"
    net = float(getattr(opp, "net_edge_bps", 0.0) or 0.0)
    if net < 0.0:
        return "NEGATIVE_EDGE"
    return "EDGE_BELOW_MIN"


class _RecordingRisk:
    def __init__(self, inner: Any, rec: _RejectionRecorder) -> None:
        self._inner = inner
        self._rec = rec

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def evaluate(self, opp: Any, state: Any, **kw: Any) -> Any:
        verdict = self._inner.evaluate(opp, state, **kw)
        if not getattr(verdict, "approved", False):
            self._rec.record(opp, getattr(verdict, "reject_reason", None) or "RISK_REJECTED")
        return verdict


def recording_components(
    cfg: BacktestConfig,
    settings: Settings,
    *,
    arm: str = "",
    factory: Callable[[BacktestConfig, Settings], Components] | None = None,
) -> tuple[Components, _RejectionRecorder]:
    """Real V2 components, with every refusal written down.

    ``factory`` defaults to ``experiments.v2_components`` — the wiring in which
    ``portfolio.*`` budgets actually bind.  Without it the V1 count cap decides
    everything and the three profiles would produce identical books for exactly
    the reason Phase 3's three allocation modes did.
    """
    rec = _RejectionRecorder(arm=arm)
    comps = (factory or v2_components)(cfg, settings)

    base_ledger_factory = comps.ledger_factory

    def ledger_factory(*args: Any, **kwargs: Any) -> Any:
        ledger = base_ledger_factory(*args, **kwargs)
        rec.bind_ledger(ledger)
        return ledger

    return (
        replace(
            comps,
            ranker=_RecordingRanker(comps.ranker, rec),
            risk=_RecordingRisk(comps.risk, rec),
            ledger_factory=ledger_factory,
        ),
        rec,
    )


# --------------------------------------------------------------------------- #
# Daily loss circuit breakers
# --------------------------------------------------------------------------- #


@dataclass
class BreakerReport:
    """What a daily loss circuit breaker at ``threshold_pct`` would have done.

    ``trips`` counts *days*, not crossings: a breaker halts the day the first
    time it fires, so firing again an hour later is the same event.

    ``avg_after_trip_pct`` is what the equity curve did **after** the trip, to
    the end of that day, as a % of that day's opening equity.  Negative means
    the day kept getting worse and the breaker saved that much; positive means
    it cut a recovery short.  That number, not the trip count, is whether the
    breaker is worth having.
    """

    threshold_pct: float
    days: int = 0
    trips: int = 0
    trip_rate_pct: float = 0.0
    avg_after_trip_pct: float = 0.0
    total_after_trip_usd: float = 0.0
    worst_day_pct: float = 0.0
    recovered_days: int = 0
    deepened_days: int = 0

    @property
    def reading(self) -> str:
        if self.days == 0:
            return "no equity curve"
        if self.trips == 0:
            return "never fired — tells us nothing about this book"
        if self.total_after_trip_usd < 0:
            money = f"saved ${-self.total_after_trip_usd:.2f} of further loss"
        elif self.total_after_trip_usd > 0:
            money = f"cut ${self.total_after_trip_usd:.2f} of recovery short"
        else:
            money = "made no difference to the day's close"
        # One day at 100% is not "most days"; three is enough to mean it.
        if self.days >= 3 and self.trip_rate_pct >= 50.0:
            return f"fires on most days — this breaker IS the strategy; {money}"
        return money

    def as_dict(self) -> dict[str, Any]:
        return {
            "threshold_pct": self.threshold_pct,
            "days": self.days,
            "trips": self.trips,
            "trip_rate_pct": round(self.trip_rate_pct, 2),
            "avg_after_trip_pct": round(self.avg_after_trip_pct, 4),
            "total_after_trip_usd": round(self.total_after_trip_usd, 4),
            "worst_day_pct": round(self.worst_day_pct, 4),
            "recovered_days": self.recovered_days,
            "deepened_days": self.deepened_days,
            "reading": self.reading,
        }


def daily_breakers(
    equity_curve: Sequence[tuple[int, float]],
    thresholds_pct: Sequence[float] = BREAKER_THRESHOLDS_PCT,
) -> list[BreakerReport]:
    """Count what each daily loss breaker would have done, day by day.

    A day opens at the first equity point on or after its UTC midnight.  The
    breaker trips the first time equity falls to or below
    ``open * (1 - threshold/100)``.  Points are taken in the order given; the
    curve is not resorted, because a curve that is out of order is a bug in
    whatever produced it and hiding it here would hide that.
    """
    reports = [BreakerReport(threshold_pct=float(t)) for t in thresholds_pct]
    if not equity_curve:
        return reports

    by_day: dict[int, list[tuple[int, float]]] = {}
    for ts, eq in equity_curve:
        by_day.setdefault(int(ts) // MS_PER_DAY, []).append((int(ts), float(eq)))

    for rep in reports:
        limit = rep.threshold_pct / 100.0
        after_pcts: list[float] = []
        for _day, points in sorted(by_day.items()):
            rep.days += 1
            open_eq = points[0][1]
            if open_eq <= 0:
                continue
            day_low_pct = min((eq - open_eq) / open_eq * 100.0 for _, eq in points)
            rep.worst_day_pct = min(rep.worst_day_pct, day_low_pct)
            trip_eq = open_eq * (1.0 - limit)
            trip_idx = next((i for i, (_, eq) in enumerate(points) if eq <= trip_eq), None)
            if trip_idx is None:
                continue
            rep.trips += 1
            eq_at_trip = points[trip_idx][1]
            eq_at_close = points[-1][1]
            after_usd = eq_at_close - eq_at_trip
            rep.total_after_trip_usd += after_usd
            after_pcts.append(after_usd / open_eq * 100.0)
            if after_usd > 0:
                rep.recovered_days += 1
            elif after_usd < 0:
                rep.deepened_days += 1
        rep.trip_rate_pct = (rep.trips / rep.days * 100.0) if rep.days else 0.0
        rep.avg_after_trip_pct = (sum(after_pcts) / len(after_pcts)) if after_pcts else 0.0
    return reports


# --------------------------------------------------------------------------- #
# Per-arm result
# --------------------------------------------------------------------------- #


@dataclass
class ProfileResult:
    """Frozen contract, PHASE5_SPEC.md, plus the fields that make it readable.

    Everything up to ``daily_loss_events`` is the spec's block, in the spec's
    order.  Everything after it is additive and defaulted.

    ``daily_loss_events`` is the count at the **live** breaker
    (``risk.daily_loss_limit_pct``, −3% by default); ``breakers`` carries all
    three thresholds the spec asks for.
    """

    name: str
    avg_positions: float = 0.0
    max_positions: int = 0
    avg_deployed_pct: float = 0.0
    avg_idle_pct: float = 100.0
    idle_by_reason: dict[str, float] = field(default_factory=dict)
    trades_per_day: float = 0.0
    turnover_per_day: float = 0.0
    avg_position_usd: float = 0.0
    avg_hold_seconds: float = 0.0
    gross_pnl: float = 0.0
    fees: float = 0.0
    slippage: float = 0.0
    net_pnl: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    max_drawdown: float = 0.0
    largest_loss: float = 0.0
    daily_loss_events: int = 0
    # --- additive ---------------------------------------------------------- #
    profile: str = ""
    deploy_band_pct: float = 0.0
    trades: int = 0
    wins: int = 0
    win_rate_pct: float = 0.0
    starting_equity: float = 0.0
    ending_equity: float = 0.0
    span_days: float = 0.0
    target_deployed_pct: float = 0.0
    total_open_risk_pct: float = 0.0
    idle_usd: float = 0.0
    #: Holdout metrics.  Selection reads these, never the full-period ones.
    oos_trades: int = 0
    oos_net_pnl: float = 0.0
    oos_expectancy: float = 0.0
    oos_from_ms: int = 0
    breakers: list[BreakerReport] = field(default_factory=list)
    rejections: dict[str, int] = field(default_factory=dict)
    rejections_recorded: int = 0
    signals_generated: int = 0
    entries_filled: int = 0
    settings_used: Settings | None = None
    error: str | None = None
    ran: bool = False

    @property
    def max_drawdown_pct(self) -> float:
        eq = self.starting_equity or 0.0
        return (self.max_drawdown / eq * 100.0) if eq > 0 else 0.0

    @property
    def aggression(self) -> tuple[int, float]:
        """Sort key: profile rank first, then deployment band."""
        return (AGGRESSION_ORDER.get(self.profile, -1), self.deploy_band_pct)

    def as_dict(self) -> dict[str, Any]:
        pf = self.profit_factor
        return {
            "name": self.name,
            "profile": self.profile,
            "deploy_band_pct": self.deploy_band_pct,
            "total_open_risk_pct": self.total_open_risk_pct,
            "ran": self.ran,
            "error": self.error,
            "avg_positions": round(self.avg_positions, 3),
            "max_positions": self.max_positions,
            "avg_deployed_pct": round(self.avg_deployed_pct, 3),
            "avg_idle_pct": round(self.avg_idle_pct, 3),
            "target_deployed_pct": self.target_deployed_pct,
            "idle_usd": round(self.idle_usd, 2),
            "idle_by_reason": {k: round(v, 4) for k, v in sorted(self.idle_by_reason.items())},
            "trades": self.trades,
            "wins": self.wins,
            "win_rate_pct": round(self.win_rate_pct, 2),
            "trades_per_day": round(self.trades_per_day, 4),
            "turnover_per_day": round(self.turnover_per_day, 4),
            "avg_position_usd": round(self.avg_position_usd, 3),
            "avg_hold_seconds": round(self.avg_hold_seconds, 1),
            "gross_pnl": round(self.gross_pnl, 4),
            "fees": round(self.fees, 4),
            "slippage": round(self.slippage, 4),
            "net_pnl": round(self.net_pnl, 4),
            "profit_factor": None if pf == float("inf") else round(pf, 4),
            "expectancy": round(self.expectancy, 6),
            "max_drawdown": round(self.max_drawdown, 4),
            "max_drawdown_pct": round(self.max_drawdown_pct, 3),
            "largest_loss": round(self.largest_loss, 4),
            "daily_loss_events": self.daily_loss_events,
            "breakers": [b.as_dict() for b in self.breakers],
            "oos": {
                "trades": self.oos_trades,
                "net_pnl": round(self.oos_net_pnl, 4),
                "expectancy": round(self.oos_expectancy, 6),
                "from_ms": self.oos_from_ms,
            },
            "starting_equity": self.starting_equity,
            "ending_equity": round(self.ending_equity, 4),
            "span_days": round(self.span_days, 3),
            "signals_generated": self.signals_generated,
            "entries_filled": self.entries_filled,
            "rejections_recorded": self.rejections_recorded,
            "rejections": dict(sorted(self.rejections.items())),
        }


# --------------------------------------------------------------------------- #
# Settings — the only thing that differs between arms
# --------------------------------------------------------------------------- #


def profile_overrides(profile: str) -> dict[str, Any]:
    """Dotted ``Settings`` overrides for one risk profile.

    Exactly one thing varies: the **total open risk budget**.  The position
    count cap is pinned to the spec's hard ceiling on every profile so that a
    difference between arms can only come from the budget — pinning it is what
    stops this comparison repeating Phase 3's allocation result, where the
    varied knob was not the binding one.

    ``risk_per_trade_pct`` is deliberately *not* varied.  Aggressive means more
    qualified opportunities, never a lower bar, and per-trade sizing is a
    different axis from how much of the book may be at risk at once.
    """
    if profile not in PROFILE_RISK_PCT:
        raise KeyError(f"unknown profile: {profile!r} (known: {', '.join(PROFILES)})")
    risk_pct = PROFILE_RISK_PCT[profile]
    return {
        "portfolio.total_open_risk_pct": risk_pct,
        # The V1 gate reads this one; leaving it at 1.5% would clamp every
        # profile to the same ceiling and hide the variable.
        "risk.max_concurrent_risk_pct": risk_pct,
        "portfolio.max_open_positions": HARD_POSITION_CEILING,
        "risk.max_concurrent_positions": HARD_POSITION_CEILING,
    }


def band_overrides(band_pct: float) -> dict[str, Any]:
    """Dotted overrides for one deployment band.

    A 70% deployment target is a 30% cash reserve; both the portfolio reserve
    and the risk engine's exposure ceiling are set, because either one alone
    would be overridden by the other.
    """
    band = float(band_pct)
    if not 0.0 < band <= 100.0:
        raise ValueError(f"deployment band must be in (0, 100]: {band_pct!r}")
    return {
        "portfolio.cash_reserve_pct": 100.0 - band,
        "risk.max_total_exposure_pct": band,
    }


def arm_settings(
    cfg: BacktestConfig,
    profile: str,
    band_pct: float,
    *,
    base: Settings | None = None,
    extra: Mapping[str, Any] | None = None,
) -> Settings:
    """The exact ``Settings`` one arm's engine will see.

    Costs from ``cfg`` first, then the profile, then the band, then any caller
    extras — so a collision is always resolved in favour of the more specific.
    """
    s = backtest_settings(cfg, base=base or get_settings())
    s = apply_overrides(s, profile_overrides(profile))
    s = apply_overrides(s, band_overrides(band_pct))
    if extra:
        s = apply_overrides(s, dict(extra))
    return s


# --------------------------------------------------------------------------- #
# Derived portfolio metrics
# --------------------------------------------------------------------------- #


def _position_sweep(result: BacktestResult) -> tuple[float, float, int, float]:
    """Time-weighted (avg positions, deployed %, peak positions, avg notional).

    A sweep line over ``[entry_ms, exit_ms)`` of every closed trade, the same
    method ``experiments._exposure_from_trades`` uses.  Positions still open
    when the run ends are not in ``result.trades`` and are therefore not
    counted — stated rather than hidden, because it biases deployed capital
    *down* on a run that ends mid-position.
    """
    trades = result.trades
    eq0 = result.starting_equity
    span_ms = max(result.end_ms - result.start_ms, 0)
    if not trades or eq0 <= 0 or span_ms <= 0:
        return 0.0, 0.0, 0, 0.0

    events: list[tuple[int, float, int]] = []
    for t in trades:
        events.append((t.entry_ms, float(t.notional_usd), 1))
        events.append((t.exit_ms, -float(t.notional_usd), -1))
    events.sort(key=lambda e: e[0])

    deployed = 0.0
    count = 0
    peak = 0
    deployed_ms = 0.0
    count_ms = 0.0
    prev_ts = events[0][0]
    for ts, d_notional, d_count in events:
        dt = ts - prev_ts
        if dt > 0:
            deployed_ms += deployed * dt
            count_ms += count * dt
        deployed += d_notional
        count += d_count
        peak = max(peak, count)
        prev_ts = ts

    avg_positions = count_ms / span_ms
    deployed_pct = deployed_ms / span_ms / eq0 * 100.0
    avg_notional = sum(float(t.notional_usd) for t in trades) / len(trades)
    return avg_positions, deployed_pct, peak, avg_notional


def _oos_slice(result: BacktestResult, fraction: float) -> tuple[int, int, float, float]:
    """(from_ms, trades, net, expectancy) over the final ``fraction`` of the span.

    Nothing is fitted per profile — the profile *is* the parameter — so this is
    a holdout rather than a walk-forward: the selection rule is not allowed to
    read the same stretch of tape on which every arm was scored.  Trades are
    attributed to the holdout by **entry** time, so a trade that straddles the
    boundary belongs to the period in which the decision was taken.
    """
    span = max(result.end_ms - result.start_ms, 0)
    if span <= 0 or not result.trades:
        return result.end_ms, 0, 0.0, 0.0
    from_ms = result.end_ms - int(span * max(min(fraction, 1.0), 0.0))
    kept = [t for t in result.trades if t.entry_ms >= from_ms]
    if not kept:
        return from_ms, 0, 0.0, 0.0
    net = sum(float(t.net_pnl_usd) for t in kept)
    return from_ms, len(kept), net, net / len(kept)


def _largest_loss(result: BacktestResult) -> float:
    losses = [float(t.net_pnl_usd) for t in result.trades if float(t.net_pnl_usd) < 0]
    return min(losses) if losses else 0.0


# --------------------------------------------------------------------------- #
# Running one arm
# --------------------------------------------------------------------------- #

Runner = Callable[..., BacktestResult]
ComponentFactory = Callable[[BacktestConfig, Settings], Components]


def run_profile_arm(
    bars_by_symbol: Mapping[str, list[Any]],
    cfg: BacktestConfig,
    *,
    profile: str,
    band_pct: float,
    base_settings: Settings | None = None,
    runner: Runner = run_backtest,
    component_factory: ComponentFactory | None = None,
    oos_fraction: float = DEFAULT_OOS_FRACTION,
    keep_rejections: bool = True,
) -> tuple[ProfileResult, list[RecordedRejection]]:
    """One profile at one deployment band, over one book of bars.

    Returns the result and the refusals recorded while producing it.  Those
    refusals are what ``app.research.missed.audit_rejections`` consumes; they
    are produced *here*, at decision time, and handed over as data — the audit
    never re-derives them, and this module never imports the audit.
    """
    name = f"{profile}@{band_pct:.0f}%"
    settings = arm_settings(cfg, profile, band_pct, base=base_settings)
    out = ProfileResult(
        name=name,
        profile=profile,
        deploy_band_pct=float(band_pct),
        target_deployed_pct=float(band_pct),
        total_open_risk_pct=PROFILE_RISK_PCT[profile],
        starting_equity=cfg.starting_equity,
        settings_used=settings,
    )

    components, rec = recording_components(
        cfg, settings, arm=name, factory=component_factory
    )
    try:
        result = runner(
            dict(bars_by_symbol),
            cfg,
            label=f"profiles/{name}",
            components=components,
            settings=settings,
        )
    except Exception as exc:  # pragma: no cover - surfaced in the table
        out.error = repr(exc)
        return out, []

    stats = result.stats
    span_ms = max(result.end_ms - result.start_ms, 0)
    days = span_ms / MS_PER_DAY if span_ms else 0.0
    avg_pos, deployed_pct, peak, avg_notional = _position_sweep(result)
    deployed_pct = min(deployed_pct, 100.0)
    idle_pct = max(0.0, 100.0 - deployed_pct)
    idle_usd = cfg.starting_equity * idle_pct / 100.0
    reserve_usd = cfg.starting_equity * settings.portfolio.cash_reserve_pct / 100.0

    breakers = daily_breakers(result.equity_curve)
    live_threshold = float(settings.risk.daily_loss_limit_pct)
    live = next((b for b in breakers if abs(b.threshold_pct - live_threshold) < 1e-9), None)

    oos_from, oos_n, oos_net, oos_exp = _oos_slice(result, oos_fraction)

    out.avg_positions = avg_pos
    out.max_positions = peak
    out.avg_deployed_pct = deployed_pct
    out.avg_idle_pct = idle_pct
    out.idle_usd = idle_usd
    out.idle_by_reason = attribute_idle_usd(
        idle_usd, rec.idle_weights(), cash_reserve_usd=min(reserve_usd, idle_usd)
    )
    out.trades_per_day = (stats.trades / days) if days > 0 else 0.0
    out.turnover_per_day = (stats.capital_turnover / days) if days > 0 else 0.0
    out.avg_position_usd = avg_notional
    out.avg_hold_seconds = stats.avg_holding_seconds
    out.gross_pnl = stats.gross_pnl
    out.fees = stats.fees
    out.slippage = stats.slippage
    out.net_pnl = stats.net_pnl
    out.profit_factor = stats.profit_factor
    out.expectancy = stats.expectancy
    out.max_drawdown = stats.max_drawdown
    out.largest_loss = _largest_loss(result)
    out.daily_loss_events = live.trips if live is not None else 0
    out.breakers = breakers
    out.trades = stats.trades
    out.wins = stats.wins
    out.win_rate_pct = stats.win_rate * 100.0
    out.ending_equity = result.ending_equity
    out.span_days = days
    out.oos_from_ms = oos_from
    out.oos_trades = oos_n
    out.oos_net_pnl = oos_net
    out.oos_expectancy = oos_exp
    out.rejections = dict(result.rejections)
    out.rejections_recorded = len(rec.rejections)
    out.signals_generated = result.signals_generated
    out.entries_filled = result.entries_filled
    out.ran = True

    return out, (rec.rejections if keep_rejections else [])


def compare_profiles(
    datasets: Mapping[str, Any],
    *,
    cfg: BacktestConfig,
    profiles: Sequence[str] = PROFILES,
    deploy_bands: Sequence[float] = DEPLOY_BANDS_PCT,
    base_settings: Settings | None = None,
    runner: Runner = run_backtest,
    component_factory: ComponentFactory | None = None,
    oos_fraction: float = DEFAULT_OOS_FRACTION,
    on_progress: Callable[[str], None] | None = None,
    collect_rejections: bool = False,
    rejection_sink: list[RecordedRejection] | None = None,
) -> list[ProfileResult]:
    """Every profile crossed with every deployment band, over one book.

    ``datasets`` maps symbol -> 1m bars (or ``research.dataset.Dataset``).  All
    symbols run in **one** book on purpose: the metric this comparison exists
    to produce is simultaneous positions, and a per-symbol run can never show
    more than one of them.

    Results come back in the spec's order (least aggressive first) so the table
    reads down and :func:`select_profile` walks it back up.
    """
    bars = _bars_of(datasets)
    results: list[ProfileResult] = []
    sink = rejection_sink if rejection_sink is not None else []
    for profile in profiles:
        for band in deploy_bands:
            if on_progress:
                on_progress(f"profiles: running {profile}@{band:.0f}% ...")
            res, rejections = run_profile_arm(
                bars,
                cfg,
                profile=profile,
                band_pct=band,
                base_settings=base_settings,
                runner=runner,
                component_factory=component_factory,
                oos_fraction=oos_fraction,
                keep_rejections=collect_rejections,
            )
            results.append(res)
            if collect_rejections:
                sink.extend(rejections)
            del rejections
            gc.collect()
    return results


def rejections_for_arm(
    datasets: Mapping[str, Any],
    *,
    cfg: BacktestConfig,
    arm: ProfileResult,
    base_settings: Settings | None = None,
    runner: Runner = run_backtest,
    component_factory: ComponentFactory | None = None,
    oos_fraction: float = DEFAULT_OOS_FRACTION,
) -> list[RecordedRejection]:
    """Re-run exactly one arm, keeping its refusals.

    ``compare_profiles`` runs with ``collect_rejections=False`` by default for a
    reason: twelve arms over 120 days of four symbols produce refusals by the
    million, and holding all of them costs more memory than the bars do.  The
    audit only ever wants one arm's, so the cheaper shape is to find the arm
    first and then pay for that one arm's refusals.

    The engine is seeded and deterministic, so this replays the identical book.
    """
    _, rejections = run_profile_arm(
        _bars_of(datasets),
        cfg,
        profile=arm.profile,
        band_pct=arm.deploy_band_pct,
        base_settings=base_settings,
        runner=runner,
        component_factory=component_factory,
        oos_fraction=oos_fraction,
        keep_rejections=True,
    )
    return rejections


def _bars_of(datasets: Mapping[str, Any]) -> dict[str, list[Any]]:
    """Accept ``{symbol: [Bar]}`` or ``{symbol: Dataset}``, return the former."""
    out: dict[str, list[Any]] = {}
    for symbol, value in datasets.items():
        if isinstance(value, (list, tuple)):
            out[str(symbol)] = list(value)
            continue
        bars_attr = getattr(value, "bars", None)
        if isinstance(bars_attr, Mapping):
            series = bars_attr.get("1m") or next(iter(bars_attr.values()), [])
            out[str(symbol)] = list(series)
    return out


# --------------------------------------------------------------------------- #
# The selection rule — the most important function in the file
# --------------------------------------------------------------------------- #


@dataclass
class Selection:
    """The answer, including "none qualify" as a first-class answer.

    ``chosen is None`` is not an error and not a missing step.  On the evidence
    in ``docs/PHASE3_REPORT.md`` — 590,306 out-of-sample trades pooling to
    −$92,365 — it is the expected outcome, and the report prints it plainly
    rather than promoting the least bad arm to fill the slot.
    """

    chosen: ProfileResult | None
    reason: str
    qualified: list[str] = field(default_factory=list)
    disqualified: list[tuple[str, str]] = field(default_factory=list)
    min_trades: int = DEFAULT_MIN_TRADES
    max_drawdown_pct: float = DEFAULT_MAX_DRAWDOWN_PCT

    @property
    def none_qualify(self) -> bool:
        return self.chosen is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "chosen": self.chosen.name if self.chosen else None,
            "none_qualify": self.none_qualify,
            "reason": self.reason,
            "qualified": list(self.qualified),
            "disqualified": [{"arm": a, "why": w} for a, w in self.disqualified],
            "min_trades": self.min_trades,
            "max_drawdown_pct": self.max_drawdown_pct,
        }


def select_profile(
    results: Sequence[ProfileResult],
    *,
    min_trades: int = DEFAULT_MIN_TRADES,
    max_drawdown_pct: float = DEFAULT_MAX_DRAWDOWN_PCT,
) -> Selection:
    """The **most aggressive** profile that still genuinely qualifies.

    Four conditions, all required, in the order a reader should check them:

    1. **Positive out-of-sample expectancy.**  Read from the holdout slice, not
       from the period every arm was scored on.
    2. **Positive net P&L after all costs.**  Gross is not a result.  An arm
       with the largest gross P&L and a negative net is disqualified here and
       there is a test named for exactly that.
    3. **An adequate sample.**  Under ``min_trades`` closed trades the
       expectancy estimate is noise, and "we do not know" is not a pass.
    4. **An acceptable drawdown on a $500 account**, as a percentage of
       starting equity.

    Among the arms that clear all four, the most aggressive wins — profile rank
    first, then the higher deployment band, then (only as a tie-break) the
    better out-of-sample expectancy.  Aggression is the *objective*; the four
    conditions are the floor it may not go through.
    """
    ran = [r for r in results if r.ran]
    if not results:
        return Selection(None, "no profiles were compared", min_trades=min_trades,
                         max_drawdown_pct=max_drawdown_pct)
    if not ran:
        return Selection(
            None,
            "no profile produced a result — every arm errored or was skipped",
            disqualified=[(r.name, r.error or "did not run") for r in results],
            min_trades=min_trades,
            max_drawdown_pct=max_drawdown_pct,
        )

    qualified: list[ProfileResult] = []
    disqualified: list[tuple[str, str]] = []
    for r in ran:
        if r.trades == 0:
            disqualified.append((r.name, "opened no trades at all"))
            continue
        if r.net_pnl <= 0:
            disqualified.append(
                (
                    r.name,
                    f"net P&L {r.net_pnl:+.2f} after ${r.fees:.2f} fees and "
                    f"${r.slippage:.2f} slippage (gross {r.gross_pnl:+.2f})",
                )
            )
            continue
        if r.oos_trades < min_trades:
            disqualified.append(
                (r.name, f"only {r.oos_trades} out-of-sample trades, under the "
                         f"{min_trades}-trade minimum — the expectancy is noise")
            )
            continue
        if r.oos_expectancy <= 0:
            disqualified.append(
                (r.name, f"out-of-sample expectancy {r.oos_expectancy:+.4f}/trade "
                         f"over {r.oos_trades} trades")
            )
            continue
        if r.oos_net_pnl <= 0:
            disqualified.append(
                (r.name, f"out-of-sample net P&L {r.oos_net_pnl:+.2f}")
            )
            continue
        if r.max_drawdown_pct > max_drawdown_pct:
            disqualified.append(
                (r.name, f"drawdown ${r.max_drawdown:.2f} = {r.max_drawdown_pct:.1f}% "
                         f"of a ${r.starting_equity:.0f} account, over the "
                         f"{max_drawdown_pct:.0f}% ceiling")
            )
            continue
        qualified.append(r)

    if not qualified:
        best = max(ran, key=lambda r: r.oos_expectancy)
        return Selection(
            None,
            (
                f"NONE QUALIFY. Of {len(ran)} arms, not one has positive "
                f"out-of-sample expectancy, positive net P&L after costs, "
                f"{min_trades}+ out-of-sample trades and a drawdown inside "
                f"{max_drawdown_pct:.0f}%. The best out-of-sample arm ({best.name}) "
                f"expects {best.oos_expectancy:+.4f}/trade over {best.oos_trades} "
                f"trades and nets {best.net_pnl:+.2f} overall. That is the result, "
                f"not a tuning opportunity — read idle_by_reason for why the money "
                f"is not working."
            ),
            disqualified=disqualified,
            min_trades=min_trades,
            max_drawdown_pct=max_drawdown_pct,
        )

    chosen = max(qualified, key=lambda r: (r.aggression, r.oos_expectancy))
    return Selection(
        chosen,
        (
            f"{chosen.name} is the most aggressive arm that qualifies: "
            f"out-of-sample expectancy {chosen.oos_expectancy:+.4f}/trade over "
            f"{chosen.oos_trades} trades, net {chosen.net_pnl:+.2f} after "
            f"${chosen.fees + chosen.slippage:.2f} of costs, drawdown "
            f"{chosen.max_drawdown_pct:.1f}% of a ${chosen.starting_equity:.0f} account."
        ),
        qualified=[r.name for r in sorted(qualified, key=lambda r: r.aggression)],
        disqualified=disqualified,
        min_trades=min_trades,
        max_drawdown_pct=max_drawdown_pct,
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

WIDTH = 118


def _pf(v: float) -> str:
    return "inf" if v == float("inf") else f"{v:.2f}"


def _identical_note(results: Sequence[ProfileResult]) -> str | None:
    """Say out loud when the profiles varied a limit nothing ever reached.

    This is the Phase 3 allocation-mode result restated: byte-identical books
    from three different settings mean the varied knob was not the binding
    constraint.  Reporting twelve identical rows as a comparison would be the
    same mistake with a bigger table.
    """
    ran = [r for r in results if r.ran]
    if len(ran) < 2:
        return None
    keys = {(r.trades, round(r.net_pnl, 8), r.max_positions) for r in ran}
    if len(keys) > 1:
        peak = max(r.max_positions for r in ran)
        if peak <= 2:
            return (
                f"Peak concurrent positions across every arm is {peak}. The risk "
                f"budget is not the binding constraint — this is Phase 3's "
                f"allocation-mode finding again: you cannot allocate across "
                f"candidates that do not exist."
            )
        return None
    return (
        f"All {len(ran)} arms produced an identical book ({ran[0].trades} trades, "
        f"net {ran[0].net_pnl:+.2f}, peak {ran[0].max_positions} positions). The "
        f"total-open-risk budget and the deployment band are therefore NOT the "
        f"binding constraint — exactly what Phase 3 found for the three allocation "
        f"modes. Varying a ceiling nothing reaches varies nothing."
    )


def format_comparison(
    results: Sequence[ProfileResult],
    selection: Selection | None = None,
    *,
    title: str = "PHASE 5 — THREE-PROFILE PAPER COMPARISON",
) -> str:
    sel = selection if selection is not None else select_profile(results)
    out: list[str] = ["=" * WIDTH, title, "=" * WIDTH]

    out.append(
        f"{'ARM':<20}{'RISK%':>7}{'AVG POS':>9}{'MAX':>5}{'DEPL%':>8}"
        f"{'IDLE%':>8}{'TRD':>6}{'TRD/D':>8}{'NET $':>10}{'PF':>7}{'EXPECT':>10}{'DD%':>7}"
    )
    out.append("-" * WIDTH)
    for r in sorted(results, key=lambda x: x.aggression):
        if not r.ran:
            out.append(f"{r.name:<20}{'— did not run: ' + (r.error or 'skipped'):>60}")
            continue
        out.append(
            f"{r.name:<20}{r.total_open_risk_pct:>7.1f}{r.avg_positions:>9.2f}"
            f"{r.max_positions:>5}{r.avg_deployed_pct:>8.2f}{r.avg_idle_pct:>8.2f}"
            f"{r.trades:>6}{r.trades_per_day:>8.2f}{r.net_pnl:>10.2f}"
            f"{_pf(r.profit_factor):>7}{r.expectancy:>10.4f}{r.max_drawdown_pct:>7.2f}"
        )
    out.append("-" * WIDTH)

    note = _identical_note(results)
    if note:
        out += ["", "WHY THE ROWS LOOK THE WAY THEY DO", "  " + note]

    # -- idle attribution --------------------------------------------------- #
    ran = [r for r in results if r.ran]
    if ran:
        buckets = sorted({k for r in ran for k in r.idle_by_reason})
        out += ["", "IDLE CAPITAL BY REASON (USD, time-weighted; sums to idle)", "-" * WIDTH]
        out.append(f"{'ARM':<20}" + "".join(f"{b[:15]:>16}" for b in buckets))
        for r in sorted(ran, key=lambda x: x.aggression):
            out.append(
                f"{r.name:<20}"
                + "".join(f"{r.idle_by_reason.get(b, 0.0):>16.2f}" for b in buckets)
            )
        out.append("-" * WIDTH)
        dom = _dominant_bucket(ran)
        if dom:
            out.append(f"  dominant: {dom[0]} (${dom[1]:.2f}) — {_idle_reading(dom[0])}")

    # -- breakers ------------------------------------------------------------ #
    if ran:
        out += ["", "DAILY LOSS CIRCUIT BREAKERS", "-" * WIDTH]
        out.append(
            f"{'ARM':<20}{'THRESH':>8}{'DAYS':>7}{'TRIPS':>7}{'RATE%':>8}"
            f"{'AFTER TRIP $':>14}  {'READING'}"
        )
        for r in sorted(ran, key=lambda x: x.aggression):
            for b in r.breakers:
                out.append(
                    f"{r.name:<20}{-b.threshold_pct:>7.1f}%{b.days:>7}{b.trips:>7}"
                    f"{b.trip_rate_pct:>8.1f}{b.total_after_trip_usd:>14.2f}  {b.reading}"
                )
        out.append("-" * WIDTH)

    # -- the answer ---------------------------------------------------------- #
    out += ["", "=" * WIDTH, "SELECTION", "=" * WIDTH]
    if sel.chosen is None:
        out.append("NO PROFILE QUALIFIES.")
    else:
        out.append(f"SELECTED: {sel.chosen.name}")
    out.append(sel.reason)
    if sel.disqualified:
        out.append("")
        out.append("disqualified:")
        for arm, why in sel.disqualified:
            out.append(f"  - {arm}: {why}")
    out.append("=" * WIDTH)
    return "\n".join(out)


def _dominant_bucket(results: Sequence[ProfileResult]) -> tuple[str, float] | None:
    totals: dict[str, float] = {}
    for r in results:
        for k, v in r.idle_by_reason.items():
            totals[k] = totals.get(k, 0.0) + v
    non_reserve = {k: v for k, v in totals.items() if k != "CASH_RESERVE" and v > 0}
    pool = non_reserve or {k: v for k, v in totals.items() if v > 0}
    if not pool:
        return None
    key = max(pool, key=lambda k: pool[k])
    return key, pool[key] / max(len(results), 1)


def _idle_reading(bucket: str) -> str:
    return {
        "EXECUTION_COST": (
            "the venue is the constraint. A wider universe cannot help: more "
            "markets at a 20 bps round trip is more scanned opportunities and no "
            "more qualified ones."
        ),
        "NO_QUALIFIED_SIGNAL": (
            "supply is the constraint. More liquid pairs, strategies, regimes and "
            "horizons is exactly the right lever."
        ),
        "CORRELATION_LIMIT": "correlated longs are being counted as one bet, as they should be.",
        "RISK_LIMIT": "our own budgets are binding — this is the knob the profiles vary.",
        "LIQUIDITY_LIMIT": "the book is too thin to trade the size we want.",
        "CASH_RESERVE": "idle by policy, not by refusal.",
    }.get(bucket, "unclassified")
