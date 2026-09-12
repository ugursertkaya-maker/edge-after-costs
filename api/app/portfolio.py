"""Portfolio state, correlation, and capital allocation (V2).

V1 asked *"are we already holding three positions?"*.  That question is
economically meaningless: three $125 positions with 2% stops risk $7.50, and so
do thirty $12 positions with 0.2% stops, but only one of those books is
diversified.  A count is a proxy for risk, and a bad one.

So the count is gone.  What decides whether another position may open is:

* **capital** — cash that is actually deployable after the reserve
  (:attr:`PortfolioState.available_usd`), never the last dollar;
* **risk** — the sum of modelled losses if every stop is hit at once, capped at
  ``portfolio.total_open_risk_pct`` of equity;
* **correlation** — because eight simultaneous crypto longs are *one large
  crypto long*, and a naive sum of eight small risks would report one eighth of
  the truth (see :meth:`CorrelationEngine.correlated_risk`);
* **direction** — beta-adjusted net exposure, so "many small positions" cannot
  quietly become one enormous directional bet;
* **the exchange minimum** — a position under ``min_notional`` is not a small
  position, it is a rejected order.

``portfolio.max_open_positions`` (default 20) survives only as an emergency
ceiling.  It is checked **last**, so any rejection it produces means every
economic budget still had room — which is the signal that the ceiling, not the
economics, is binding, and that the ceiling should be raised rather than the
book trusted.

**Position size is not risk.**  A $20 position with a 1% stop risks ~$0.20 plus
costs.  Every function here keeps notional and risk in separate variables for
exactly that reason.

V5 — where the money *isn't*
----------------------------

V2 answered "may we open this?".  Phase 3 then measured the book that answer
actually produced: a peak of **two** concurrent positions with **99.5% of
capital idle**, and all three allocation modes emitting byte-identical books
because there was nothing to allocate across.  A count that never binds and a
budget that is never spent look identical from the outside, and neither
explains the $350 sitting still in a $500 account.

So this module now also measures the *absence* of positions:

* :func:`attribute_idle` charges **every idle dollar to exactly one reason**
  (:class:`IdleReason`).  There is deliberately no residual bucket — the
  reconciliation is asserted and raises :class:`IdleAttributionError` rather
  than quietly absorbing a difference, because a bucket that absorbs the
  difference is how "we don't know" gets rendered as a finding.
* :class:`DeploymentPlan` gives the allocator a **target band** (60/70/80/85%
  of equity).  The target is a *stopping condition*, never a quota: when only
  two candidates qualify, two positions open and the rest of the band is
  attributed, never filled by lowering the bar.

The distinction the attribution exists to settle is narrow and worth stating:
idle cash dominated by ``NO_QUALIFIED_SIGNAL`` means the universe is too small
and expanding it is the fix; idle cash dominated by ``EXECUTION_COST`` means the
venue's round trip eats the edge and expanding the universe cannot help.

V6 — the capital comes back *now*
---------------------------------

Phase 5 measured where the money was not.  Phase 6 asks a narrower question
about the money that *is* working: when a position closes, how long is that
capital out of the game?

* :meth:`PortfolioEngine.release` is the inverse of :meth:`_commit`.  A close
  removes the position, returns the proceeds to cash, drops its risk out of the
  open-risk budget and **recomputes correlated risk and directional exposure in
  the same call** — because a book that still believes it holds a position it
  sold refuses the next trade on a budget that is already free.
* :class:`RecycleEvent` times the path: close → capital released → rescan →
  next entry.  The two published numbers are ``close_to_rescan_ms`` and
  ``close_to_entry_ms``, and they exist so that "the scheduler was the
  constraint" stops being a suspicion.
* Three of the four new :class:`IdleReason` members exist to name delays the
  old six would have mis-charged: ``COOLDOWN`` (we were not allowed to trade),
  ``SCANNER_DELAY`` (the money arrived after the last ranking, so nothing had
  looked at it yet) and ``CAPITAL_STATE_BUG`` (the books do not balance).  The
  last one is the important one: an accounting error must show up as an
  accounting error rather than be absorbed by whichever bucket happens to be
  walked last.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from app.types import (
    Allocation,
    Bar,
    CostBreakdown,
    NetTargets,
    Opportunity,
    OrderBook,
    Position,
    PortfolioState,
    RejectReason,
    RiskVerdict,
    Side,
    SymbolInfo,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.config import Settings

_BPS = 10_000.0
_EPS = 1e-9

# A symbol we have no return history for is assumed to move with everything
# else.  Assuming independence would understate the book's true risk exactly
# when we know least about it, which is the wrong direction to be wrong in.
_UNKNOWN_RHO = 1.0

# Dynamic allocation shaping constants.  Every one of these maps onto an input
# the spec names explicitly (edge, volatility, liquidity, correlation); they are
# bounded multipliers rather than tiers, so no position is ever sized by a label.
_EDGE_WEIGHT_MIN = 0.5
_EDGE_WEIGHT_MAX = 2.0
_VOL_WEIGHT_MIN = 0.5
_VOL_WEIGHT_MAX = 1.5
# Never ask the visible book for more than this fraction of the resting size
# within 10 bps of mid: past that, our own order is the slippage.
_LIQUIDITY_FRACTION = 0.10
# A candidate perfectly correlated with the open book gets at most half the
# capital an uncorrelated one would.
_CORRELATION_PENALTY = 0.5

# --------------------------------------------------------------------------- #
# Deployment targeting (V5)
# --------------------------------------------------------------------------- #

#: The bands the profile comparison sweeps.  Any float in (0, 100] is accepted;
#: these are the four that are actually reported on.
DEPLOYMENT_BANDS: tuple[float, ...] = (60.0, 70.0, 80.0, 85.0)

#: Used when nothing else says otherwise.  80% sits inside the 60–85% band and
#: is exactly what a 20% cash reserve leaves deployable, so at the default
#: reserve the band and the reserve bind at the same dollar rather than one
#: silently shadowing the other.
DEFAULT_TARGET_DEPLOYED_PCT = 80.0

#: The book shape the spec asks for: 8–15 simultaneous positions.  These are
#: *divisors* for sizing, never quotas — dividing the band budget by 8 when only
#: three candidates qualify produces three positions and a large attributed
#: idle balance, which is the honest outcome.
TARGET_BOOK_MIN = 8
TARGET_BOOK_MAX = 15

#: "Many small" expressed scale-free: one position is at most a tenth of
#: equity, so a $500 account lands in the $10–$50 the spec asks for instead of
#: the $100–$200 that ``max_position_usd`` alone would allow.
MANY_SMALL_MAX_PCT_EQUITY = 10.0


@dataclass(frozen=True)
class DeploymentPlan:
    """How much of equity this cycle is *aiming* to have working, and in how
    many pieces.

    Computed once per allocation pass and held flat for the whole walk.  A
    per-candidate recomputation (``budget / remaining``) decays geometrically —
    the first candidate gets 1/15th of the budget, the twentieth gets the
    minimum — which manufactures a long tail of floor-sized positions and makes
    the book size a function of list length rather than of capital.
    """

    target_pct: float
    target_usd: float
    budget_usd: float
    slots: int
    per_position_usd: float

    @property
    def active(self) -> bool:
        """False when the band is already full (or there is nothing to spend)."""
        return self.budget_usd > _EPS and self.per_position_usd > 0.0

    def as_dict(self) -> dict:
        return {
            "target_pct": self.target_pct,
            "target_usd": self.target_usd,
            "budget_usd": self.budget_usd,
            "slots": self.slots,
            "per_position_usd": self.per_position_usd,
        }


# --------------------------------------------------------------------------- #
# Idle-capital attribution (V5)
# --------------------------------------------------------------------------- #


class IdleReason(str, enum.Enum):
    """Why a dollar is not working.  Exactly one of these owns each idle dollar.

    The first six are Phase 5's.  The last four are Phase 6's and every one of
    them names a delay that the original six would have charged to the market:

    * ``COOLDOWN`` — we were not permitted to enter, whatever was ranked.
      Previously this arrived as ``RISK_LIMIT``, which reads as "the loss budget
      was spent" and is a different sentence from "we were sitting out".
    * ``SCANNER_DELAY`` — the capital was freed *after* the last ranking, so no
      candidate list has yet been produced that could have spent it.  Charging
      that to ``NO_QUALIFIED_SIGNAL`` would claim we looked and found nothing
      when in fact we had not looked.
    * ``CAPITAL_STATE_BUG`` — cash, exposure and equity do not reconcile.  It is
      a bucket for *our* defects, and it is deliberately loud.
    * ``OTHER`` — capital the caller states is unusable for a reason this enum
      does not model.  Reachable only through an explicit declaration
      (:class:`IdleContext`); an unparseable *rejection* is still the absence of
      a decision, i.e. ``NO_QUALIFIED_SIGNAL``.
    """

    NO_QUALIFIED_SIGNAL = "NO_QUALIFIED_SIGNAL"
    CORRELATION_LIMIT = "CORRELATION_LIMIT"
    RISK_LIMIT = "RISK_LIMIT"
    LIQUIDITY_LIMIT = "LIQUIDITY_LIMIT"
    EXECUTION_COST = "EXECUTION_COST"
    CASH_RESERVE = "CASH_RESERVE"
    # V6
    COOLDOWN = "COOLDOWN"
    SCANNER_DELAY = "SCANNER_DELAY"
    CAPITAL_STATE_BUG = "CAPITAL_STATE_BUG"
    OTHER = "OTHER"


class IdleAttributionError(RuntimeError):
    """The attribution did not reconcile to the idle balance.

    A :class:`RuntimeError` rather than an ``assert`` on purpose: ``python -O``
    strips asserts, and an attribution that silently stops reconciling is worse
    than no attribution at all — it is a number on a dashboard that nobody can
    check.
    """


#: How a *candidate-level* refusal maps onto a *dollar-level* reason.
#:
#: The mapping is opinionated and the opinions are load-bearing, so each one is
#: stated rather than left to the reader:
#:
#: * ``NEGATIVE_EDGE`` / ``EDGE_BELOW_MIN`` are ``EXECUTION_COST``.
#:   ``net_edge_bps`` is already gross edge *minus* fee, spread and slippage, so
#:   a candidate failing there is precisely one whose edge did not survive its
#:   own round trip.  This is the bucket Phase 3 predicts will dominate (§20:
#:   8,321 evaluation passes, every one rejected on those two reasons), and the
#:   whole point of the measurement is that the prediction can now be wrong.
#: * ``DUPLICATE_SYMBOL`` is ``CORRELATION_LIMIT``.  A second position in the
#:   same symbol is not merely correlated with the book, it *is* the book.
#: * ``MAX_POSITIONS`` is ``RISK_LIMIT``.  It is the emergency count ceiling,
#:   and reaching it means every economic budget still had room; the
#:   ``by_reject_reason`` breakdown keeps that fact visible instead of burying
#:   it, because the response to it is to raise the ceiling, not to trust it.
#: * ``STALE_BOOK`` / ``DATA_FAILURE`` are ``NO_QUALIFIED_SIGNAL``.  We did not
#:   decline the trade; we never had a usable look at it.
_REJECT_TO_IDLE: dict[RejectReason, IdleReason] = {
    # Nothing usable to decide on.
    RejectReason.STALE_BOOK: IdleReason.NO_QUALIFIED_SIGNAL,
    RejectReason.DATA_FAILURE: IdleReason.NO_QUALIFIED_SIGNAL,
    RejectReason.NOT_SUPPORTED_SHORT: IdleReason.NO_QUALIFIED_SIGNAL,
    # The round trip ate the edge.
    RejectReason.NEGATIVE_EDGE: IdleReason.EXECUTION_COST,
    RejectReason.EDGE_BELOW_MIN: IdleReason.EXECUTION_COST,
    RejectReason.WIDE_SPREAD: IdleReason.EXECUTION_COST,
    RejectReason.SLIPPAGE_TOO_HIGH: IdleReason.EXECUTION_COST,
    RejectReason.NET_TARGET_UNREACHABLE: IdleReason.EXECUTION_COST,
    RejectReason.BAD_REWARD_RISK: IdleReason.EXECUTION_COST,
    # The book could not absorb a position of the smallest size we may send.
    RejectReason.THIN_DEPTH: IdleReason.LIQUIDITY_LIMIT,
    RejectReason.BELOW_MIN_NOTIONAL: IdleReason.LIQUIDITY_LIMIT,
    # The loss budget, in one form or another, was spent.
    RejectReason.RISK_BUDGET: IdleReason.RISK_LIMIT,
    RejectReason.PORTFOLIO_RISK_BUDGET: IdleReason.RISK_LIMIT,
    RejectReason.ABNORMAL_VOLATILITY: IdleReason.RISK_LIMIT,
    RejectReason.DAILY_LOSS_LIMIT: IdleReason.RISK_LIMIT,
    RejectReason.WEEKLY_LOSS_LIMIT: IdleReason.RISK_LIMIT,
    RejectReason.KILL_SWITCH: IdleReason.RISK_LIMIT,
    # Sitting out is not the same statement as "the loss budget is spent": one
    # says a timer is running, the other says the book is full of risk.
    RejectReason.LOSS_STREAK_COOLDOWN: IdleReason.COOLDOWN,
    RejectReason.MAX_POSITIONS: IdleReason.RISK_LIMIT,
    # It would have duplicated exposure we already carry.
    RejectReason.DUPLICATE_SYMBOL: IdleReason.CORRELATION_LIMIT,
    RejectReason.CORRELATED_EXPOSURE: IdleReason.CORRELATION_LIMIT,
    RejectReason.DIRECTIONAL_EXPOSURE: IdleReason.CORRELATION_LIMIT,
    # Held back on purpose.
    RejectReason.CASH_RESERVE: IdleReason.CASH_RESERVE,
}


def idle_reason_for(reason: "RejectReason | str | None") -> IdleReason:
    """The idle bucket a rejection belongs to.

    An unrecognised *string* falls back to ``NO_QUALIFIED_SIGNAL`` — an
    unparseable label is the absence of a decision, not a decision.  Every
    member of :class:`~app.types.RejectReason` is mapped explicitly, and
    ``test_idle_attribution.py`` fails if a new one is ever added without one.
    """
    if reason is None:
        return IdleReason.NO_QUALIFIED_SIGNAL
    if isinstance(reason, IdleReason):
        return reason
    if isinstance(reason, RejectReason):
        return _REJECT_TO_IDLE.get(reason, IdleReason.NO_QUALIFIED_SIGNAL)
    text = str(getattr(reason, "value", reason))
    try:
        return _REJECT_TO_IDLE[RejectReason(text)]
    except (ValueError, KeyError):
        pass
    try:
        return IdleReason(text)
    except ValueError:
        return IdleReason.NO_QUALIFIED_SIGNAL


def blocked_idle_reason(reason: "RejectReason | IdleReason | str | None") -> IdleReason:
    """The bucket an *account-level block* belongs to.

    Same table as :func:`idle_reason_for` with one deliberate difference in the
    fallback.  A rejection we cannot parse is the absence of a decision, so it
    falls to ``NO_QUALIFIED_SIGNAL``.  A *block* is the opposite: the caller is
    telling us capital was withheld on purpose and only the label is unfamiliar,
    and calling that "nothing qualified" would be a straightforward lie.  So it
    falls to ``OTHER``, which is the only thing ``OTHER`` is for.
    """
    if reason is None:
        return IdleReason.OTHER
    if isinstance(reason, (RejectReason, IdleReason)):
        return idle_reason_for(reason)
    return idle_reason_for(reason) if _is_known_label(reason) else IdleReason.OTHER


def _is_known_label(reason: Any) -> bool:
    """Whether a string names a reject reason or an idle reason we model."""
    text = str(getattr(reason, "value", reason))
    for enum_cls in (RejectReason, IdleReason):
        try:
            enum_cls(text)
        except ValueError:
            continue
        return True
    return False


@dataclass(frozen=True)
class IdleContext:
    """What the *caller* knows that a :class:`PortfolioState` cannot say.

    A book snapshot records cash, exposure and risk.  It cannot record that the
    account was in a cooldown, that the money arrived after the last scan, or
    that a close failed to return what it should have — and those are exactly
    the three things Phase 6 needs the attribution to be able to name.  So they
    arrive here, explicitly, from the component that observed them.

    Supplying a context also switches on the books check (see
    ``check_books``).  That is opt-in rather than automatic because a
    hand-assembled state — a research sweep, a fixture — is not a claim about a
    real ledger, and reporting an accounting defect against a state nobody
    claimed was real would make ``CAPITAL_STATE_BUG`` the bucket everyone
    learns to ignore.
    """

    ts_ms: int = 0
    #: When the ranked list this attribution is reading was produced.
    last_rank_ms: int = 0
    #: When a close most recently returned capital to cash.
    capital_released_ms: int = 0
    #: An account-level block in force right now (``RejectReason`` or a label).
    blocked_reason: Any = None
    #: Capital a close should have returned and demonstrably did not.
    state_bug_usd: float = 0.0
    check_books: bool = True

    @property
    def scanner_is_behind(self) -> bool:
        """True when capital was freed after the most recent ranking."""
        return self.capital_released_ms > 0 and self.capital_released_ms > self.last_rank_ms


@dataclass
class IdleAttribution:
    """Every idle dollar, charged to exactly one reason.

    ``by_reason`` always carries all six keys — a dashboard whose columns
    appear and disappear with the data is a dashboard nobody reads — and always
    sums to ``total_idle_usd``.  :meth:`check` enforces that and is called by
    :func:`attribute_idle` before the object is ever returned.
    """

    total_idle_usd: float
    by_reason: dict[str, float]
    deployed_usd: float
    deployed_pct: float
    target_pct: float
    # Additions; the five fields above are the frozen contract.
    equity: float = 0.0
    by_reject_reason: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def idle_pct(self) -> float:
        """Idle cash as a percentage of the ledger's own equity."""
        return self.total_idle_usd / self.equity * 100.0 if self.equity > 0 else 0.0

    @property
    def target_gap_pct(self) -> float:
        """How far below the target band the book is, in points of equity."""
        return max(self.target_pct - self.deployed_pct, 0.0)

    @property
    def dominant_reason(self) -> IdleReason | None:
        """The bucket holding the most idle capital, or ``None`` if none is.

        Ties break by the declaration order of :class:`IdleReason`, so the
        answer is deterministic rather than dict-order-dependent.
        """
        best: IdleReason | None = None
        best_usd = _EPS
        for reason in IdleReason:
            usd = self.by_reason.get(reason.value, 0.0)
            if usd > best_usd:
                best, best_usd = reason, usd
        return best

    def usd(self, reason: "IdleReason | str") -> float:
        key = reason.value if isinstance(reason, IdleReason) else str(reason)
        return self.by_reason.get(key, 0.0)

    def pct(self, reason: "IdleReason | str") -> float:
        """A reason's share of *equity* — the number the dashboard shows."""
        return self.usd(reason) / self.equity * 100.0 if self.equity > 0 else 0.0

    def residual(self) -> float:
        return sum(self.by_reason.values()) - self.total_idle_usd

    def check(self, tolerance: float = 1e-6) -> "IdleAttribution":
        """Raise unless the buckets sum to the idle balance."""
        gap = self.residual()
        limit = tolerance * max(1.0, abs(self.total_idle_usd))
        if abs(gap) > limit:
            raise IdleAttributionError(
                f"idle attribution does not reconcile: buckets sum to "
                f"${sum(self.by_reason.values()):.6f} against ${self.total_idle_usd:.6f} "
                f"of idle cash (residual ${gap:+.6f}); by_reason={self.by_reason}"
            )
        return self

    def as_dict(self) -> dict:
        return {
            "total_idle_usd": self.total_idle_usd,
            "by_reason": dict(self.by_reason),
            "deployed_usd": self.deployed_usd,
            "deployed_pct": self.deployed_pct,
            "idle_pct": self.idle_pct,
            "target_pct": self.target_pct,
            "target_gap_pct": self.target_gap_pct,
            "equity": self.equity,
            "dominant_reason": (
                self.dominant_reason.value if self.dominant_reason else None
            ),
            "by_reject_reason": dict(self.by_reject_reason),
            "notes": list(self.notes),
        }


def attribute_idle(
    state: PortfolioState,
    *,
    ranked: Sequence[Any] = (),
    rejections: Any = None,
    target_pct: float | None = None,
    equity: float | None = None,
    context: "IdleContext | None" = None,
    settings: "Settings | None" = None,
) -> IdleAttribution:
    """Charge every idle dollar to exactly one :class:`IdleReason`.

    The accounting rule — *the marginal-dollar walk*
    ------------------------------------------------

    A rejection says a *candidate* was refused.  Idle *dollars* are a different
    quantity, and the two are not interchangeable: one refused candidate does
    not explain $400 of idle cash, and forty refused candidates do not explain
    four hundred.  So the attribution spends the idle balance the way the
    allocator would have spent it, in the order it would have spent it:

    1. **The cash reserve is charged first**, to ``CASH_RESERVE``.  It was never
       deployable, and calling it anything else would blame the market for a
       policy decision.
    2. **Capital above the target band is charged to** ``CASH_RESERVE`` **too.**
       At a 60% band on a $500 account, $200 is not idle for want of ideas — it
       is idle because we said to keep it idle.  Naming that ``CASH_RESERVE``
       keeps "deliberately held back" honest and stops the band from inflating
       ``NO_QUALIFIED_SIGNAL``.
    3. **What is left is the band budget**, and it is walked down the ranked
       list in rank order.  Each *refused* candidate is charged one position's
       worth of cash — what it would have consumed had it been taken — to the
       bucket its rejection maps to.  Capital is consumed in rank order because
       that is the order the allocator consumes it in, so the dollars a
       high-ranked refusal costs us are the dollars that were actually
       available to it.
    4. **Whatever the list does not exhaust is charged to**
       ``NO_QUALIFIED_SIGNAL``.  This is the load-bearing step: it says, in
       dollars, that even had every refusal been reversed there would still
       have been nothing to buy with the remainder.  It is what makes the
       Phase 3 shape (zero candidates → 100% ``NO_QUALIFIED_SIGNAL``) fall out
       of the arithmetic instead of being special-cased.

    V6 inserts two steps and re-labels one, all of them *inside* the same
    balance — nothing is ever added to the idle total, only taken from it
    earlier, so the reconciliation in :meth:`IdleAttribution.check` is
    untouched:

    0. **Capital that does not reconcile is charged to** ``CAPITAL_STATE_BUG``
       **before anything else** — either an amount the caller measured (a close
       that did not return what it should have) or, when a
       :class:`IdleContext` is supplied, the gap between ``cash + deployed``
       and equity.  Charging it first means a defect cannot be laundered
       through ``CASH_RESERVE`` on its way to looking deliberate.
    2b. **A declared block takes the whole band budget**, because under a
       cooldown or a kill switch no candidate could have been taken whatever it
       was ranked.  Attributing the same dollars to the refusals below it would
       claim the market refused capital that policy had already withdrawn.
    4.  **The remainder is** ``SCANNER_DELAY`` **rather than**
       ``NO_QUALIFIED_SIGNAL`` **when the capital was freed after the last
       ranking.**  Same dollars, different sentence, and the difference is the
       whole of §12: "nothing qualified" is a market fact, "we had not looked
       yet" is a loop-shape fact and is ours to fix.

    "One position's worth" is ``budget / slots`` clamped to the allowed position
    band, where ``slots`` is how many more positions the book needs to reach
    :data:`TARGET_BOOK_MIN`.  It is deliberately the *same* arithmetic the
    allocator sizes with (:meth:`PortfolioEngine.deployment_plan`), so a
    candidate's refusal is charged what taking it would have cost, not a
    notional invented for the report.

    The alternative rules, and why not
    ----------------------------------

    * *Split the idle balance evenly across the distinct reasons seen.*  One
      stale book would then be worth as much idle cash as thirty
      cost-rejections.  It measures the variety of refusals, not the money.
    * *Charge each reason its share of refused candidates.*  Same defect, and it
      can never produce a ``NO_QUALIFIED_SIGNAL`` residue, which is exactly the
      finding the module exists to be able to report.
    * *Give the remainder its own "unexplained" bucket.*  This is the one the
      spec forbids, and rightly: a residual bucket absorbs every modelling
      error, always looks plausible, and converts "we do not know" into a
      number on a dashboard.

    Parameters
    ----------
    state:
        The book **after** allocation.  ``state.cash`` is the idle balance and
        ``state.reserve_required_usd`` the reserve; both come from the ledger.
    ranked:
        This cycle's ranked candidates (``Opportunity`` objects or bare
        symbols).  Rank order is the order capital is charged in.
    rejections:
        ``{symbol: RejectReason}``, ``[(symbol, reason), ...]``, or any iterable
        of objects carrying ``.symbol`` and ``.reason``/``.reject_reason``.  A
        rejected symbol that is not in ``ranked`` is walked after it, in the
        order given, so nothing is silently dropped.
    target_pct:
        The deployment band.  Defaults to :data:`DEFAULT_TARGET_DEPLOYED_PCT`.
    equity:
        The ledger's equity.  Defaults to ``state.equity``; ``deployed_pct`` and
        ``idle_pct`` are both computed against it so the two always agree with
        the ledger and with each other.
    context:
        Optional :class:`IdleContext` — what the caller observed that the book
        snapshot cannot record (a block in force, when the last ranking ran,
        when capital was last released).  Omitting it reproduces the Phase 5
        attribution exactly.
    """
    if settings is None:
        from app.config import get_settings

        settings = get_settings()
    cfg = settings.portfolio

    eq = float(equity if equity is not None else state.equity)
    eq = max(eq, 0.0)
    target = _clamp(
        float(target_pct if target_pct is not None else DEFAULT_TARGET_DEPLOYED_PCT),
        0.0,
        100.0,
    )

    deployed = max(float(state.deployed_usd), 0.0)
    total_idle = max(float(state.cash), 0.0)

    buckets: dict[str, float] = {r.value: 0.0 for r in IdleReason}
    by_reject: dict[str, float] = {}
    notes: list[str] = []

    # 0. capital that does not reconcile, charged before any bucket that could
    # make it look deliberate.
    unreconciled = _clamp(_state_bug_usd(state, eq, context), 0.0, total_idle)
    buckets[IdleReason.CAPITAL_STATE_BUG.value] += unreconciled
    if unreconciled > _EPS:
        notes.append(
            f"books do not balance: ${unreconciled:.2f} unaccounted for "
            f"(cash ${state.cash:.2f} + deployed ${state.deployed_usd:.2f} "
            f"vs equity ${eq:.2f})"
        )

    # 1. the reserve, which was never deployable.
    reserve = _clamp(float(state.reserve_required_usd), 0.0, total_idle - unreconciled)
    buckets[IdleReason.CASH_RESERVE.value] += reserve
    deployable = total_idle - unreconciled - reserve

    # 2. capital the band deliberately leaves alone.
    room_to_band = max(eq * target / 100.0 - deployed, 0.0)
    above_band = max(deployable - room_to_band, 0.0)
    buckets[IdleReason.CASH_RESERVE.value] += above_band
    budget = deployable - above_band
    if above_band > _EPS:
        notes.append(
            f"above_band=${above_band:.2f} (target {target:.0f}% of ${eq:.2f})"
        )

    # 2b. a block withdraws the whole budget before the market gets to refuse it.
    if context is not None and context.blocked_reason is not None and budget > _EPS:
        bucket = blocked_idle_reason(context.blocked_reason)
        buckets[bucket.value] += budget
        label = str(getattr(context.blocked_reason, "value", context.blocked_reason))
        notes.append(f"blocked by {label}: ${budget:.2f} could not be deployed at all")
        budget = 0.0

    # 3. walk the refusals in the order capital would have reached them.
    order = _walk_order(ranked, _normalise_rejections(rejections))
    open_positions = len(state.positions)
    charged = 0
    for symbol, reason in order:
        if budget <= _EPS:
            notes.append(f"budget exhausted after {charged} refusal(s)")
            break
        slot_cost = _slot_cost(
            budget, open_positions + charged, equity=eq, cfg=cfg
        )
        charge = min(slot_cost, budget)
        if charge <= _EPS:
            break
        bucket = idle_reason_for(reason)
        buckets[bucket.value] += charge
        key = str(getattr(reason, "value", reason))
        by_reject[key] = by_reject.get(key, 0.0) + charge
        budget -= charge
        charged += 1

    # 4. the list ran out before the money did.  Charged unconditionally, dust
    # included: rounding the tail away is a residual bucket by another name.
    if budget > 0.0:
        behind = context is not None and context.scanner_is_behind
        tail = IdleReason.SCANNER_DELAY if behind else IdleReason.NO_QUALIFIED_SIGNAL
        buckets[tail.value] += budget
        if budget > _EPS and behind:
            notes.append(
                f"capital freed at {context.capital_released_ms} after the last "
                f"ranking at {context.last_rank_ms}: ${budget:.2f} has not been "
                f"looked at yet"
            )
        elif budget > _EPS:
            notes.append(
                f"no further candidates: ${budget:.2f} of the band had nothing to buy"
            )

    return IdleAttribution(
        total_idle_usd=total_idle,
        by_reason=buckets,
        deployed_usd=deployed,
        deployed_pct=deployed / eq * 100.0 if eq > 0 else 0.0,
        target_pct=target,
        equity=eq,
        by_reject_reason=by_reject,
        notes=notes,
    ).check()


def _state_bug_usd(
    state: PortfolioState, equity: float, context: "IdleContext | None"
) -> float:
    """Dollars the book cannot account for.

    Two sources, and the larger wins rather than the two being added: a caller
    that measured a specific failed release *and* a snapshot that does not
    balance is almost always looking at one defect through two windows, and
    double-charging it would overstate the only bucket nobody should have to
    argue about.

    The identity being checked is the ledger's own: cash plus marked exposure
    is equity.  It holds by construction for a paper spot account, so a gap is
    not a modelling approximation — it is capital that left one column without
    arriving in another.
    """
    if context is None:
        return 0.0
    declared = max(float(context.state_bug_usd), 0.0)
    observed = 0.0
    if context.check_books and equity > 0.0:
        gap = abs(float(state.cash) + float(state.deployed_usd) - equity)
        if gap > 1e-6 * max(1.0, equity):
            observed = gap
    return max(declared, observed)


def _slot_cost(budget: float, open_positions: int, *, equity: float, cfg: Any) -> float:
    """What one more position would have cost, given the book we already have.

    ``budget / slots``, where ``slots`` is how many positions short of
    :data:`TARGET_BOOK_MIN` the book is — the same divisor
    :meth:`PortfolioEngine.deployment_plan` sizes with, so the charge for a
    refusal is what taking it would actually have consumed.  A book already at
    or past the target size divides by one: the next position would simply take
    what it needed.
    """
    slots = max(TARGET_BOOK_MIN - max(open_positions, 0), 1)
    return _clamp(budget / slots, cfg.min_position_usd, _small_cap(equity, cfg))


def _small_cap(equity: float, cfg: Any) -> float:
    """The "many small" ceiling: a tenth of equity, never above the hard cap."""
    soft = max(equity, 0.0) * MANY_SMALL_MAX_PCT_EQUITY / 100.0
    return max(cfg.min_position_usd, min(cfg.max_position_usd, soft))


def _normalise_rejections(rejections: Any) -> "list[tuple[str, Any]]":
    """``{sym: reason}`` / ``[(sym, reason)]`` / objects → ordered pairs."""
    if rejections is None:
        return []
    if isinstance(rejections, Mapping):
        return [(str(k), v) for k, v in rejections.items()]
    pairs: list[tuple[str, Any]] = []
    for item in rejections:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            pairs.append((str(item[0]), item[1]))
            continue
        symbol = getattr(item, "symbol", None)
        reason = getattr(item, "reason", None)
        if reason is None:
            reason = getattr(item, "reject_reason", None)
        if symbol is not None:
            pairs.append((str(symbol), reason))
    return pairs


def _walk_order(
    ranked: Sequence[Any], rejections: "list[tuple[str, Any]]"
) -> "list[tuple[str, Any]]":
    """Refused candidates, in the order capital would have reached them.

    Rank order first (that is the order the allocator spends in), then any
    refused symbol that never appeared in the ranked list, in the order given —
    a rejection with nowhere to sit is still a rejection and is not dropped.
    """
    by_symbol: dict[str, Any] = {}
    for symbol, reason in rejections:
        by_symbol.setdefault(symbol, reason)

    out: list[tuple[str, Any]] = []
    seen: set[str] = set()
    for item in ranked:
        symbol = str(getattr(item, "symbol", item))
        if symbol in by_symbol and symbol not in seen:
            seen.add(symbol)
            out.append((symbol, by_symbol[symbol]))
    for symbol, reason in rejections:
        if symbol not in seen:
            seen.add(symbol)
            out.append((symbol, reason))
    return out


# --------------------------------------------------------------------------- #
# Capital recycling (V6)
# --------------------------------------------------------------------------- #


@dataclass
class RecycleEvent:
    """One close, timed all the way to the trade that reused its capital.

    Four timestamps and two derived intervals.  The intervals are the point:

    * ``close_to_rescan_ms`` is **ours**.  It is loop shape, nothing else, and
      a number in the hundreds of milliseconds means the close triggered the
      rescan; a number the size of the evaluation interval means it waited for
      the next tick, which is the defect §12 exists to remove.
    * ``close_to_entry_ms`` is ours *and* the market's, and is ``None`` when no
      entry followed.  ``None`` is not a missing measurement — it is the
      measurement, and ``qualified_candidate_existed`` says which of the two
      readings it is: no candidate existed (the market), or one did and we
      still did not deploy (us).

    Mutable by design: the close and the entry are observed by different calls
    at different times, so the event is opened by :meth:`CapitalRecycler.on_close`
    and completed later, if an entry ever happens.
    """

    closed_trade_id: str
    closed_ms: int
    capital_released_ms: int
    rescan_triggered_ms: int
    next_entry_ms: int | None = None
    qualified_candidate_existed: bool = False
    # Additions; the six fields above are the frozen contract.
    symbol: str = ""
    released_usd: float = 0.0
    risk_released_usd: float = 0.0
    available_usd_after: float = 0.0
    deployed_pct_after: float = 0.0
    next_entry_symbol: str | None = None
    rescan_handled: bool = False
    state_bug_usd: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def close_to_rescan_ms(self) -> int:
        """Milliseconds from the close to the rescan it triggered."""
        return max(self.rescan_triggered_ms - self.closed_ms, 0)

    @property
    def close_to_entry_ms(self) -> int | None:
        """Milliseconds from the close to the next entry, or ``None`` if none."""
        if self.next_entry_ms is None:
            return None
        return max(self.next_entry_ms - self.closed_ms, 0)

    @property
    def release_ms(self) -> int:
        """How long the capital itself took to come back.  Should be ~0."""
        return max(self.capital_released_ms - self.closed_ms, 0)

    @property
    def redeployed(self) -> bool:
        return self.next_entry_ms is not None

    def as_dict(self) -> dict:
        return {
            "closed_trade_id": self.closed_trade_id,
            "symbol": self.symbol,
            "closed_ms": self.closed_ms,
            "capital_released_ms": self.capital_released_ms,
            "rescan_triggered_ms": self.rescan_triggered_ms,
            "next_entry_ms": self.next_entry_ms,
            "next_entry_symbol": self.next_entry_symbol,
            "qualified_candidate_existed": self.qualified_candidate_existed,
            "close_to_rescan_ms": self.close_to_rescan_ms,
            "close_to_entry_ms": self.close_to_entry_ms,
            "release_ms": self.release_ms,
            "released_usd": self.released_usd,
            "risk_released_usd": self.risk_released_usd,
            "available_usd_after": self.available_usd_after,
            "deployed_pct_after": self.deployed_pct_after,
            "rescan_handled": self.rescan_handled,
            "state_bug_usd": self.state_bug_usd,
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- #
# Correlation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CorrelationView:
    """Pairwise correlation over ``portfolio.correlation_window_bars``, plus
    each symbol's beta to BTC.

    ``correlations`` is keyed by an ordered tuple ``(a, b)`` with ``a < b``;
    use :meth:`rho`, which normalises the key order for you.  A symbol absent
    from ``btc_beta`` is a symbol we had no usable history for — callers that
    care about being safe rather than optimistic treat it as unknown and assume
    the worst (see :data:`_UNKNOWN_RHO`).
    """

    correlations: dict[tuple[str, str], float] = field(default_factory=dict)
    btc_beta: dict[str, float] = field(default_factory=dict)

    def known(self, symbol: str) -> bool:
        return symbol in self.btc_beta

    def rho(self, a: str, b: str) -> float:
        """Measured correlation between two symbols; 1.0 for a symbol with itself."""
        if a == b:
            return 1.0
        key = (a, b) if a < b else (b, a)
        value = self.correlations.get(key)
        return 0.0 if value is None else value

    def beta(self, symbol: str) -> float:
        """Sensitivity to BTC.  1.0 when unmeasured — assume it moves with the market."""
        return self.btc_beta.get(symbol, 1.0)

    def cluster(self, symbols: list[str], threshold: float) -> list[list[str]]:
        """Single-linkage clusters of symbols whose |rho| reaches ``threshold``.

        Single linkage is deliberate: if A moves with B and B moves with C, then
        a stop-out cascade reaches C whether or not A and C look related in the
        sample.  Order is deterministic — clusters come back in the order their
        first member appears in ``symbols``.
        """
        unique: list[str] = []
        for s in symbols:
            if s not in unique:
                unique.append(s)

        parent = {s: s for s in unique}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i, a in enumerate(unique):
            for b in unique[i + 1 :]:
                if abs(self.rho(a, b)) >= threshold:
                    ra, rb = find(a), find(b)
                    if ra != rb:
                        parent[rb] = ra

        groups: dict[str, list[str]] = {}
        for s in unique:
            groups.setdefault(find(s), []).append(s)
        # Deterministic: order clusters by where their first member appeared.
        return sorted(groups.values(), key=lambda g: unique.index(g[0]))


class CorrelationEngine:
    """Measures how much of the book is really one bet."""

    def __init__(self, *, settings: "Settings | None" = None) -> None:
        if settings is None:
            from app.config import get_settings

            settings = get_settings()
        self.settings = settings
        self.cfg = settings.portfolio

    # ------------------------------------------------------------------ #
    # Measurement
    # ------------------------------------------------------------------ #

    def update(self, bars_by_symbol: dict[str, list[Bar]]) -> CorrelationView:
        """Recompute the pairwise correlation matrix and BTC betas.

        Returns are simple close-to-close over the trailing
        ``correlation_window_bars``; series are aligned from the *end*, because
        the most recent overlapping bars are the ones a correlated stop-out
        cascade would actually run through.
        """
        window = max(int(self.cfg.correlation_window_bars), 2)
        returns: dict[str, list[float]] = {}
        for symbol in sorted(bars_by_symbol):
            rets = _returns(bars_by_symbol[symbol], window)
            if len(rets) >= 3:  # two points make any correlation exactly +-1
                returns[symbol] = rets

        symbols = sorted(returns)
        correlations: dict[tuple[str, str], float] = {}
        for i, a in enumerate(symbols):
            for b in symbols[i + 1 :]:
                ra, rb = _align(returns[a], returns[b])
                if len(ra) >= 3:
                    correlations[(a, b)] = _pearson(ra, rb)

        btc = _btc_symbol(symbols)
        btc_beta: dict[str, float] = {}
        for s in symbols:
            if btc is None:
                btc_beta[s] = 1.0
            elif s == btc:
                btc_beta[s] = 1.0
            else:
                ra, rb = _align(returns[s], returns[btc])
                btc_beta[s] = _beta(ra, rb) if len(ra) >= 3 else 1.0

        return CorrelationView(correlations=correlations, btc_beta=btc_beta)

    # ------------------------------------------------------------------ #
    # Aggregation
    # ------------------------------------------------------------------ #

    def correlated_risk(
        self, positions: list[Position], view: CorrelationView | None
    ) -> float:
        """Risk aggregated across correlated positions — **not** a naive sum.

        The formula is the standard quadratic form::

            correlated_risk = sqrt( w' R w )
                            = sqrt( sum_i sum_j w_i * w_j * rho(i, j) )

        where ``w_i`` is position *i*'s modelled dollar loss at its stop and
        ``R`` is the correlation matrix.  Two limiting cases show why this is
        the only defensible aggregation:

        * **perfectly correlated** (rho = 1 everywhere): ``w'Rw = (sum w)^2``,
          so the result is ``sum w``.  Eight $1 risks that all move together
          aggregate to $8 — one large position, which is the truth.
        * **uncorrelated** (rho = 0 off the diagonal): ``w'Rw = sum w^2``, so
          the result is ``sqrt(8) ~ $2.83``.  Independent risks add in
          quadrature; claiming $8 there would over-reserve.

        A naive sum reports $8 in both cases (over-cautious when diversified);
        counting each position separately against a per-trade cap reports $1
        (catastrophically optimistic when concentrated).  Neither is usable.

        Negative correlations can in principle make the form negative if the
        measured matrix is not positive semi-definite; the result is floored at
        zero rather than allowed to become imaginary.
        """
        if not positions:
            return 0.0
        symbols = [p.symbol for p in positions]
        weights = [max(float(p.risk_usd), 0.0) for p in positions]
        return self.aggregate(symbols, weights, view)

    def aggregate(
        self,
        symbols: Sequence[str],
        weights: Sequence[float],
        view: CorrelationView | None,
    ) -> float:
        """:meth:`correlated_risk` over raw ``(symbol, risk_usd)`` vectors.

        Used by the allocator to price a *candidate* into the book before the
        position exists.
        """
        if not symbols:
            return 0.0
        total = 0.0
        for i, a in enumerate(symbols):
            for j, b in enumerate(symbols):
                total += weights[i] * weights[j] * _rho_safe(view, a, b)
        return math.sqrt(total) if total > 0.0 else 0.0

    def directional_exposure_pct(
        self,
        positions: list[Position],
        equity: float,
        view: CorrelationView | None,
    ) -> float:
        """Beta-adjusted net directional exposure as a percentage of equity.

        Longs count positive, shorts negative, each scaled by its beta to BTC —
        a 2-beta altcoin long is two units of market exposure, not one.  The
        magnitude is returned, because a net-short book is exactly as
        directional as a net-long one.
        """
        if equity <= 0:
            return 0.0
        net = 0.0
        for p in positions:
            sign = 1.0 if p.side is Side.LONG else -1.0
            beta = view.beta(p.symbol) if view is not None else 1.0
            net += sign * beta * float(p.notional_usd)
        return abs(net) / equity * 100.0


@dataclass
class AllocationResult:
    """What the allocator funded, what it refused and why, and what it aimed at.

    ``rejections`` is what turns :func:`attribute_idle` from a guess into a
    measurement: it is the allocator's own record of which budget refused which
    candidate, taken from the same pass that spent the money.
    """

    allocations: list[Allocation] = field(default_factory=list)
    rejections: dict[str, RejectReason] = field(default_factory=dict)
    plan: DeploymentPlan | None = None

    @property
    def deployed_usd(self) -> float:
        return sum(a.notional_usd for a in self.allocations)

    @property
    def risk_usd(self) -> float:
        return sum(a.risk_usd for a in self.allocations)

    def __len__(self) -> int:  # pragma: no cover - convenience only
        return len(self.allocations)

    def __iter__(self):  # pragma: no cover - convenience only
        return iter(self.allocations)


def _fill(
    rejections: dict[str, RejectReason],
    opportunities: Sequence[Opportunity],
    reason: RejectReason,
) -> None:
    """Record ``reason`` for every candidate the walk never reached.

    A break is a refusal of everything below it, and leaving the tail blank
    would silently move that capital into ``NO_QUALIFIED_SIGNAL`` — which is
    exactly the reading the measurement must not get wrong.
    """
    for opp in opportunities:
        rejections.setdefault(opp.symbol, reason)


# --------------------------------------------------------------------------- #
# Portfolio
# --------------------------------------------------------------------------- #


class PortfolioEngine:
    """Builds portfolio state, answers "may we open this?", and allocates."""

    def __init__(
        self,
        *,
        settings: "Settings | None" = None,
        correlation: CorrelationEngine | None = None,
    ) -> None:
        if settings is None:
            from app.config import get_settings

            settings = get_settings()
        self.settings = settings
        self.cfg = settings.portfolio
        self.correlation = correlation or CorrelationEngine(settings=settings)
        # The most recent view, so can_open()/allocate() can price a candidate
        # into the book without being handed the bars a second time.
        self.view: CorrelationView | None = None

    # ------------------------------------------------------------------ #
    # Deployment band
    # ------------------------------------------------------------------ #

    @property
    def target_deployed_pct(self) -> float:
        """The band this engine aims at, as a percentage of equity.

        Read through ``getattr`` so that adding ``target_deployed_pct`` to
        :class:`~app.config.PortfolioConfig` later needs no change here, and so
        that omitting it changes nothing today.
        """
        return float(
            getattr(self.cfg, "target_deployed_pct", DEFAULT_TARGET_DEPLOYED_PCT)
        )

    def deployment_plan(
        self,
        state: PortfolioState,
        *,
        candidates: int,
        target_pct: float | None = None,
    ) -> DeploymentPlan:
        """How much to put to work this pass, and in how many pieces.

        The budget is ``min(deployable cash, room left in the band)`` — the
        reserve and the band are both ceilings and the tighter one wins.  The
        piece count is ``clamp(candidates, 8, 15)``:

        * clamped **below** at 8 so that three qualifying candidates are sized
          as three pieces of an eight-position book (~$50 each on $500) rather
          than three pieces of a three-position book (~$133 each).  Fewer ideas
          must not mean bigger bets — that is how "deploy more capital" turns
          into "concentrate risk", which is the opposite of the ask;
        * clamped **above** at 15 so a list of forty candidates does not shave
          the book into forty floor-sized slivers, each of which pays a fixed
          round trip for a proportionally smaller edge.

        Note what this deliberately does *not* do: it never raises
        ``per_position_usd`` to make the band fill.  With two candidates the
        plan funds two positions and leaves the rest of the band idle, where
        :func:`attribute_idle` charges it to ``NO_QUALIFIED_SIGNAL``.
        """
        equity = max(state.equity, 0.0)
        target = _clamp(
            float(target_pct if target_pct is not None else self.target_deployed_pct),
            0.0,
            100.0,
        )
        target_usd = equity * target / 100.0
        room = max(target_usd - max(state.deployed_usd, 0.0), 0.0)
        budget = max(min(state.available_usd, room), 0.0)
        slots = int(_clamp(float(max(candidates, 0)), TARGET_BOOK_MIN, TARGET_BOOK_MAX))
        per = 0.0
        if budget > _EPS and slots > 0:
            per = _clamp(
                budget / slots, self.cfg.min_position_usd, _small_cap(equity, self.cfg)
            )
        return DeploymentPlan(
            target_pct=target,
            target_usd=target_usd,
            budget_usd=budget,
            slots=slots,
            per_position_usd=per,
        )

    def attribute_idle(
        self,
        state: PortfolioState,
        *,
        ranked: Sequence[Any] = (),
        rejections: Any = None,
        target_pct: float | None = None,
        equity: float | None = None,
        context: "IdleContext | None" = None,
    ) -> IdleAttribution:
        """:func:`attribute_idle` against *this* engine's settings and band."""
        return attribute_idle(
            state,
            ranked=ranked,
            rejections=rejections,
            target_pct=(
                target_pct if target_pct is not None else self.target_deployed_pct
            ),
            equity=equity,
            context=context,
            settings=self.settings,
        )

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #

    def state(
        self,
        ledger: Any,
        *,
        ts_ms: int,
        bars_by_symbol: dict[str, list[Bar]] | None = None,
    ) -> PortfolioState:
        """Snapshot the whole book at ``ts_ms``.

        The reserve is a fraction of **equity**, not of cash: as cash drains
        into positions a cash-based reserve shrinks with it and stops reserving
        anything, which is the opposite of what a reserve is for.
        """
        if bars_by_symbol:
            self.view = self.correlation.update(bars_by_symbol)

        positions = _positions_of(ledger)
        equity = float(getattr(ledger, "equity", 0.0))
        cash = float(getattr(ledger, "cash", 0.0))
        deployed = float(
            getattr(ledger, "exposure_usd", None)
            if getattr(ledger, "exposure_usd", None) is not None
            else sum(p.notional_usd for p in positions)
        )

        return PortfolioState(
            ts_ms=ts_ms,
            equity=equity,
            cash=cash,
            deployed_usd=deployed,
            reserve_required_usd=max(equity, 0.0) * self.cfg.cash_reserve_pct / 100.0,
            open_risk_usd=sum(max(float(p.risk_usd), 0.0) for p in positions),
            correlated_risk_usd=self.correlation.correlated_risk(positions, self.view),
            directional_exposure_pct=self.correlation.directional_exposure_pct(
                positions, equity, self.view
            ),
            positions=positions,
        )

    # ------------------------------------------------------------------ #
    # The gate
    # ------------------------------------------------------------------ #

    def can_open(
        self,
        opp: Opportunity,
        state: PortfolioState,
        *,
        risk_usd: float,
        notional_usd: float,
    ) -> RejectReason | None:
        """``None`` means yes.  Otherwise the first budget that refuses.

        Called twice per decision: once with zeroes, before sizing, to catch a
        budget that is *already* exhausted, and once with the real numbers
        afterwards, because correlation and directional exposure cannot be
        known until the size is.

        The emergency count ceiling is checked **last** on purpose: if it is the
        reason, every economic budget still had room, and that fact should be
        visible rather than hidden behind an arbitrary number.
        """
        cfg = self.cfg
        symbol = opp.symbol
        side = opp.signal.side
        equity = max(state.equity, 0.0)

        same_symbol = sum(1 for p in state.positions if p.symbol == symbol)
        if same_symbol >= cfg.max_positions_per_symbol:
            return RejectReason.DUPLICATE_SYMBOL

        # Cash reserve — set aside first, spent never.
        if state.available_usd <= _EPS or notional_usd > state.available_usd + _EPS:
            return RejectReason.CASH_RESERVE

        risk_budget = equity * cfg.total_open_risk_pct / 100.0
        if state.open_risk_usd + max(risk_usd, 0.0) > risk_budget + _EPS:
            return RejectReason.PORTFOLIO_RISK_BUDGET

        projected_corr = self.projected_correlated_risk(
            state, symbol=symbol, risk_usd=risk_usd
        )
        if projected_corr > equity * cfg.max_correlated_risk_pct / 100.0 + _EPS:
            return RejectReason.CORRELATED_EXPOSURE

        projected_dir = self.projected_directional_pct(
            state, symbol=symbol, side=side, notional_usd=notional_usd
        )
        if projected_dir > cfg.max_directional_exposure_pct + _EPS:
            return RejectReason.DIRECTIONAL_EXPOSURE

        if len(state.positions) >= cfg.max_open_positions:
            return RejectReason.MAX_POSITIONS

        return None

    def projected_correlated_risk(
        self, state: PortfolioState, *, symbol: str, risk_usd: float
    ) -> float:
        """Correlated risk of the book *with* a candidate risking ``risk_usd``."""
        symbols = [p.symbol for p in state.positions] + [symbol]
        weights = [max(float(p.risk_usd), 0.0) for p in state.positions] + [
            max(float(risk_usd), 0.0)
        ]
        return self.correlation.aggregate(symbols, weights, self.view)

    def projected_directional_pct(
        self, state: PortfolioState, *, symbol: str, side: Side, notional_usd: float
    ) -> float:
        """Directional exposure of the book *with* a candidate of ``notional_usd``."""
        equity = max(state.equity, 0.0)
        if equity <= 0:
            return 0.0
        net = 0.0
        for p in state.positions:
            sign = 1.0 if p.side is Side.LONG else -1.0
            beta = self.view.beta(p.symbol) if self.view is not None else 1.0
            net += sign * beta * float(p.notional_usd)
        sign = 1.0 if side is Side.LONG else -1.0
        beta = self.view.beta(symbol) if self.view is not None else 1.0
        net += sign * beta * max(float(notional_usd), 0.0)
        return abs(net) / equity * 100.0

    # ------------------------------------------------------------------ #
    # Allocation
    # ------------------------------------------------------------------ #

    def allocate(
        self,
        ranked_opportunities: list[Opportunity],
        state: PortfolioState,
        *,
        risk_engine: Any,
        infos: Mapping[str, SymbolInfo],
        books: Mapping[str, OrderBook],
        targets_by_symbol: Mapping[str, NetTargets] | None = None,
        target_pct: float | None = None,
    ) -> list[Allocation]:
        """Rank-ordered greedy allocation across **all** qualified opportunities.

        Walks the ranked list once, sizes each candidate through the real risk
        engine, shapes it by ``allocation_mode``, and checks it against a
        portfolio state that is updated as allocations accumulate — so the
        budget is genuinely consumed and the tenth candidate faces the book the
        first nine created.  Deterministic: same inputs, same list, every time.

        Account-level blocks (kill switch, loss limits, cooldown, data health)
        are the caller's business — ask ``RiskEngine.blocked_reason`` before
        calling this; there is no account state to check them against here.

        "Many *allowed*" is not "many *taken*": every candidate still passes the
        risk engine's own gates independently, so a list of twenty rubbish
        setups allocates nothing.

        The **target band** (``target_pct``, default
        :data:`DEFAULT_TARGET_DEPLOYED_PCT`) is a stopping condition and nothing
        else: the walk continues while deployed capital is below the target
        *and* candidates still qualify, and stops on whichever runs out first.
        No gate is relaxed to reach it and no candidate is up-sized to fill it —
        the band being unmet is a *measurement*, delivered by
        :func:`attribute_idle`, not a quota to satisfy.
        """
        return self.allocate_detailed(
            ranked_opportunities,
            state,
            risk_engine=risk_engine,
            infos=infos,
            books=books,
            targets_by_symbol=targets_by_symbol,
            target_pct=target_pct,
        ).allocations

    def allocate_detailed(
        self,
        ranked_opportunities: list[Opportunity],
        state: PortfolioState,
        *,
        risk_engine: Any,
        infos: Mapping[str, SymbolInfo],
        books: Mapping[str, OrderBook],
        targets_by_symbol: Mapping[str, NetTargets] | None = None,
        target_pct: float | None = None,
    ) -> "AllocationResult":
        """:meth:`allocate`, plus *why* each unfunded candidate went unfunded.

        The allocator is the only place that knows which budget refused which
        candidate, and :func:`attribute_idle` is useless without that.  Having
        the caller re-derive it would mean re-running the sizing, which is both
        slower and a second implementation to disagree with the first.
        """
        plan = self.deployment_plan(
            state, candidates=len(ranked_opportunities), target_pct=target_pct
        )
        if not ranked_opportunities:
            return AllocationResult(allocations=[], rejections={}, plan=plan)

        # The risk engine runs its own portfolio gate; make sure it is looking
        # at the same correlation view we are, or it can veto on stale data.
        peer = getattr(risk_engine, "portfolio", None)
        if isinstance(peer, PortfolioEngine) and peer is not self:
            peer.view = self.view

        working = _copy_state(state)
        allocations: list[Allocation] = []
        rejections: dict[str, RejectReason] = {}
        mode = (self.cfg.allocation_mode or "dynamic").lower()
        remaining = len(ranked_opportunities)
        ref_atr = _mean_atr_bps(ranked_opportunities)
        mean_edge = _mean_edge_bps(ranked_opportunities)

        for rank, opp in enumerate(ranked_opportunities, start=1):
            remaining -= 1
            # The operating range is 8-15 positions; `max_open_positions` is an
            # emergency ceiling, not a target. Without this stop, shrinking the
            # position size simply lets the book run to the ceiling -- more
            # positions for the same money is not the same thing as better
            # allocation, and the spec asks for the band, not the maximum.
            book_cap = min(self.cfg.max_open_positions, TARGET_BOOK_MAX)
            if len(working.positions) >= book_cap:
                _fill(rejections, ranked_opportunities[rank - 1 :], RejectReason.MAX_POSITIONS)
                break
            if working.available_usd < self._floor_notional(None) - _EPS:
                # Nothing further can clear the exchange minimum: stop walking.
                _fill(rejections, ranked_opportunities[rank - 1 :], RejectReason.CASH_RESERVE)
                break
            if plan.target_usd > 0.0 and working.deployed_usd >= plan.target_usd - _EPS:
                # The band is full.  Everything past here is idle on purpose,
                # so it carries no rejection: attribute_idle charges it to
                # CASH_RESERVE as capital held back deliberately.
                break

            info = infos.get(opp.symbol)
            book = books.get(opp.symbol)
            if info is None or book is None:
                rejections[opp.symbol] = RejectReason.DATA_FAILURE
                continue

            verdict = self._size(risk_engine, opp, working, info=info, book=book)
            if verdict is None or not verdict.approved or verdict.qty <= 0:
                rejections[opp.symbol] = (
                    getattr(verdict, "reject_reason", None) or RejectReason.RISK_BUDGET
                )
                continue

            entry = verdict.notional_usd / verdict.qty if verdict.qty > 0 else 0.0
            if entry <= 0:
                rejections[opp.symbol] = RejectReason.STALE_BOOK
                continue

            target_notional = self._mode_notional(
                mode,
                opp,
                working,
                verdict,
                book=book,
                remaining=remaining,
                ref_atr_bps=ref_atr,
                mean_edge_bps=mean_edge,
                plan=plan,
            )
            qty, notional, risk_usd, notes = self._shrink(
                verdict, target_notional, info=info, entry=entry
            )
            if qty <= 0:
                rejections[opp.symbol] = RejectReason.BELOW_MIN_NOTIONAL
                continue

            reason = self.can_open(
                opp, working, risk_usd=risk_usd, notional_usd=notional
            )
            if reason is not None:
                rejections[opp.symbol] = reason
                continue

            targets = (targets_by_symbol or {}).get(opp.symbol) or self._synth_targets(
                opp, entry=entry, notional_usd=notional, verdict=verdict
            )
            allocations.append(
                Allocation(
                    opportunity=opp,
                    qty=qty,
                    notional_usd=notional,
                    risk_usd=risk_usd,
                    targets=targets,
                    rank=rank,
                    notes=[f"mode={mode}", f"band={plan.target_pct:.0f}%", *notes,
                           *verdict.notes],
                )
            )
            working = self._commit(working, opp, entry=entry, qty=qty,
                                   notional_usd=notional, risk_usd=risk_usd)

        return AllocationResult(
            allocations=allocations, rejections=rejections, plan=plan
        )

    # -- allocation helpers --------------------------------------------- #

    def _size(
        self,
        risk_engine: Any,
        opp: Opportunity,
        working: PortfolioState,
        *,
        info: SymbolInfo,
        book: OrderBook,
    ) -> RiskVerdict | None:
        from app.risk import RiskState

        rs = RiskState(
            equity=working.equity,
            open_positions=working.positions,
            realized_pnl_today=0.0,
            realized_pnl_week=0.0,
            loss_streak=0,
            cooldown_until_ms=0,
            kill_switch=False,
            data_healthy=True,
            ts_ms=working.ts_ms,
            portfolio=working,
        )
        return risk_engine.evaluate(opp, rs, info=info, book=book)

    def _mode_notional(
        self,
        mode: str,
        opp: Opportunity,
        working: PortfolioState,
        verdict: RiskVerdict,
        *,
        book: OrderBook,
        remaining: int,
        ref_atr_bps: float,
        mean_edge_bps: float,
        plan: DeploymentPlan | None = None,
    ) -> float:
        """The notional this *mode* wants, before the risk-sized cap is applied.

        Every mode is a downward shaping of what the risk engine already
        approved — a mode can spread capital thinner, never risk more.
        """
        cfg = self.cfg
        if mode == "few_large":
            # Take everything risk allows.  Capital, not a slot count, is what
            # makes the book small: $400 deployable at $125 a position is three.
            return min(verdict.notional_usd, cfg.max_position_usd)

        if mode == "many_small":
            # Sit on the floor and let breadth do the work.
            return max(cfg.min_position_usd, 0.0)

        # "dynamic": size purely from the inputs the spec names, starting from
        # the band's flat per-position slice rather than from "whatever cash is
        # left divided by whatever candidates are left" — the latter decays
        # geometrically down the list and makes the book size a function of how
        # long the ranked list happened to be.
        if plan is not None and plan.active:
            share = plan.per_position_usd
        else:
            share = working.available_usd / max(remaining + 1, 1)
        edge_w = _clamp(
            opp.net_edge_bps / mean_edge_bps if mean_edge_bps > 0 else 1.0,
            _EDGE_WEIGHT_MIN,
            _EDGE_WEIGHT_MAX,
        )
        atr = max(opp.features.atr_bps, _EPS)
        vol_w = _clamp(ref_atr_bps / atr if ref_atr_bps > 0 else 1.0,
                       _VOL_WEIGHT_MIN, _VOL_WEIGHT_MAX)
        corr_w = 1.0 - _CORRELATION_PENALTY * self._max_rho_to_book(working, opp.symbol)
        target = share * edge_w * vol_w * corr_w

        liquidity_cap = book.depth_usd("ask") * _LIQUIDITY_FRACTION
        if liquidity_cap > 0:
            target = min(target, liquidity_cap)
        # The upper bound is the "many small" ceiling, not the hard per-position
        # cap: a 2x edge weight on a rich candidate must not quietly rebuild the
        # $125 book the band exists to break up.
        return _clamp(
            target, cfg.min_position_usd, _small_cap(max(working.equity, 0.0), cfg)
        )

    def _max_rho_to_book(self, working: PortfolioState, symbol: str) -> float:
        if not working.positions:
            return 0.0
        return max(
            abs(_rho_safe(self.view, symbol, p.symbol)) for p in working.positions
        )

    def _floor_notional(self, info: SymbolInfo | None) -> float:
        """The smallest notional we are ever allowed to send."""
        floor = max(self.cfg.min_position_usd, self.settings.risk.min_notional_usd)
        if info is not None:
            floor = max(floor, info.min_notional)
        return floor

    def _shrink(
        self,
        verdict: RiskVerdict,
        target_notional: float,
        *,
        info: SymbolInfo,
        entry: float,
    ) -> tuple[float, float, float, list[str]]:
        """Scale a risk-approved verdict **down** to the mode's target.

        Risk scales with the position, so a proportional shrink is exact: half
        the quantity is half the dollars at the stop.  We never scale up — the
        risk engine's number is a ceiling, not a suggestion.
        """
        notes: list[str] = []
        qty = verdict.qty
        floor = self._floor_notional(info)

        if target_notional < verdict.notional_usd - _EPS:
            scale = max(target_notional, 0.0) / verdict.notional_usd
            qty = info.floor_qty(verdict.qty * scale + info.step_size * 1e-9)
            notes.append(f"alloc:target_notional={target_notional:.2f}")

        if qty <= 0 or qty < info.min_qty:
            return 0.0, 0.0, 0.0, notes
        notional = qty * entry
        if notional < floor - _EPS:
            # A mode that *aims* at the minimum lands just under it once the lot
            # size is floored: target $10.00, get 0.09998 units, get $9.999.
            # Refusing there would make "many_small" allocate literally nothing.
            # Step the quantity up to the smallest lot that clears the floor —
            # never past what the risk engine approved, which stays the ceiling.
            stepped = self._step_up_to_floor(qty, floor, info=info, entry=entry)
            if stepped is not None and stepped <= verdict.qty + _EPS:
                qty = stepped
                notional = qty * entry
                notes.append("alloc:stepped_up_to_min_notional")
            else:
                # Genuinely impossible: risk will not fund even one minimum lot.
                notes.append(f"alloc:below_min_notional({floor:.2f})")
                return 0.0, 0.0, 0.0, notes

        share = qty / verdict.qty if verdict.qty > 0 else 1.0
        return qty, notional, verdict.risk_usd * share, notes

    @staticmethod
    def _step_up_to_floor(
        qty: float, floor: float, *, info: SymbolInfo, entry: float
    ) -> float | None:
        """Smallest lot-aligned quantity worth at least ``floor`` dollars."""
        import math

        if entry <= 0 or info.step_size <= 0:
            return None
        needed = floor / entry
        steps = math.ceil(needed / info.step_size - 1e-9)
        candidate = round(steps * info.step_size, 10)
        if candidate < info.min_qty or candidate * entry < floor - _EPS:
            return None
        return candidate

    def _commit(
        self,
        working: PortfolioState,
        opp: Opportunity,
        *,
        entry: float,
        qty: float,
        notional_usd: float,
        risk_usd: float,
    ) -> PortfolioState:
        """Fold an allocation into the working state so the budget is consumed."""
        pos = Position(
            id=f"alloc:{opp.symbol}",
            symbol=opp.symbol,
            side=opp.signal.side,
            strategy=opp.signal.strategy,
            qty=qty,
            entry_price=entry,
            entry_ms=working.ts_ms,
            notional_usd=notional_usd,
            risk_usd=risk_usd,
            take_profit=0.0,
            stop_loss=0.0,
            entry_fees_usd=0.0,
            entry_slippage_bps=opp.slippage_bps,
            expected_net_edge_bps=opp.net_edge_bps,
        )
        positions = [*working.positions, pos]
        return replace(
            working,
            cash=working.cash - notional_usd,
            deployed_usd=working.deployed_usd + notional_usd,
            open_risk_usd=working.open_risk_usd + risk_usd,
            correlated_risk_usd=self.correlation.correlated_risk(positions, self.view),
            directional_exposure_pct=self.correlation.directional_exposure_pct(
                positions, working.equity, self.view
            ),
            positions=positions,
        )

    def release(
        self,
        state: PortfolioState,
        position: Position,
        *,
        proceeds_usd: float | None = None,
        qty: float | None = None,
        ts_ms: int | None = None,
    ) -> PortfolioState:
        """Fold a *close* out of the book — the exact inverse of :meth:`_commit`.

        Called in the same breath as the exit fill, not on the next tick.  Every
        budget the position was consuming is returned here:

        * the cash, credited with what the exit actually fetched
          (``proceeds_usd``, net of the exit fee).  Exposure falls by what the
          position was *marked* at, and equity by the difference between the
          two — which is the realised P&L.  Crediting the cost basis instead
          would keep ``cash + exposure == equity`` only for a scratch trade and
          report every winner as an accounting defect;
        * the position, removed from the book, so ``DUPLICATE_SYMBOL`` stops
          refusing its own symbol the instant the symbol is no longer held;
        * its share of the open-risk budget;
        * **correlated risk and directional exposure, recomputed** — both are
          functions of the whole book, not sums over it, so neither can be
          repaired by subtraction.  This is the line that makes the release
          synchronous in any useful sense: a stale correlation number refuses
          the next candidate on a bet we no longer have on.

        A partial exit releases pro rata and leaves the remainder open, matching
        :meth:`app.sim.ledger.Ledger.close`.  Passing an unheld position is a
        no-op on the positions list and still credits the cash, because the
        caller's ledger has already moved the money either way.
        """
        closed_qty = float(position.qty if qty is None else qty)
        share = _clamp(closed_qty / position.qty, 0.0, 1.0) if position.qty > 0 else 1.0
        mark = position.last_price or position.entry_price
        marked = closed_qty * max(float(mark), 0.0)
        # Unknown proceeds means "assume it fetched the mark": that leaves
        # equity untouched, which is the only assumption that invents no P&L.
        cash_in = float(proceeds_usd) if proceeds_usd is not None else marked
        basis = float(position.notional_usd) * share
        risk_back = max(float(position.risk_usd), 0.0) * share

        positions: list[Position] = []
        for p in state.positions:
            if p.id != position.id:
                positions.append(p)
            elif share < 1.0:
                # Keep the remainder, with its cost basis reduced pro rata.
                positions.append(
                    replace(
                        p,
                        qty=p.qty - closed_qty,
                        notional_usd=p.notional_usd - basis,
                        risk_usd=max(p.risk_usd - risk_back, 0.0),
                    )
                )

        equity = state.equity + cash_in - marked
        return replace(
            state,
            ts_ms=int(ts_ms if ts_ms is not None else state.ts_ms),
            equity=equity,
            cash=state.cash + cash_in,
            deployed_usd=max(state.deployed_usd - marked, 0.0),
            # The reserve is a fraction of equity, so a close that moved equity
            # moves it too; leaving it stale would mis-state available_usd,
            # which is the number the next entry is sized against.
            reserve_required_usd=max(equity, 0.0) * self.cfg.cash_reserve_pct / 100.0,
            open_risk_usd=max(state.open_risk_usd - risk_back, 0.0),
            correlated_risk_usd=self.correlation.correlated_risk(positions, self.view),
            directional_exposure_pct=self.correlation.directional_exposure_pct(
                positions, state.equity, self.view
            ),
            positions=positions,
        )

    def _synth_targets(
        self,
        opp: Opportunity,
        *,
        entry: float,
        notional_usd: float,
        verdict: RiskVerdict,
    ) -> NetTargets:
        """A minimal NetTargets for callers that have no profit engine wired in.

        The real one comes from the fee-aware profit engine and is passed in via
        ``targets_by_symbol``; this stand-in keeps ``Allocation.targets``
        non-optional without inventing a better number than we have.
        """
        p = self.settings.profit
        cost_bps = (
            opp.fee_bps + opp.spread_cost_bps + opp.slippage_bps + p.latency_buffer_bps
        )
        net_tp_pct = p.target_pct()
        gross_required_bps = net_tp_pct * 100.0 + cost_bps
        long = opp.signal.side is Side.LONG
        sign = 1.0 if long else -1.0
        costs = CostBreakdown(
            entry_fee_usd=notional_usd * opp.fee_bps / 2.0 / _BPS,
            exit_fee_usd=notional_usd * opp.fee_bps / 2.0 / _BPS,
            spread_cost_usd=notional_usd * opp.spread_cost_bps / _BPS,
            entry_slippage_usd=notional_usd * opp.slippage_bps / 2.0 / _BPS,
            exit_slippage_usd=notional_usd * opp.slippage_bps / 2.0 / _BPS,
            latency_buffer_usd=notional_usd * p.latency_buffer_bps / _BPS,
        )
        return NetTargets(
            profile=p.profile,
            net_tp_pct=net_tp_pct,
            break_even_price=entry * (1.0 + sign * cost_bps / _BPS),
            net_tp_price=entry * (1.0 + sign * gross_required_bps / _BPS),
            stop_price=verdict.stop_loss,
            emergency_stop_price=entry
            * (1.0 - sign * self.settings.exits.emergency_stop_pct / 100.0),
            gross_move_required_bps=gross_required_bps,
            expected_net_profit_usd=notional_usd * net_tp_pct / 100.0,
            expected_gross_profit_usd=notional_usd * gross_required_bps / _BPS,
            costs=costs,
            notes=["synthesized: no profit engine targets supplied"],
        )


# --------------------------------------------------------------------------- #
# Maths helpers
# --------------------------------------------------------------------------- #


def _rho_safe(view: CorrelationView | None, a: str, b: str) -> float:
    """Correlation with the *conservative* fallback applied.

    With no view at all, or for a symbol we have no history for, assume the
    positions move together.  Being wrong that way over-reserves; being wrong
    the other way is how "many small positions" becomes one enormous bet.
    """
    if a == b:
        return 1.0
    if view is None or not view.known(a) or not view.known(b):
        return _UNKNOWN_RHO
    return view.rho(a, b)


def _returns(bars: list[Bar], window: int) -> list[float]:
    closes = [b.close for b in bars[-(window + 1) :] if b.close > 0]
    return [
        (closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))
    ]


def _align(a: list[float], b: list[float]) -> tuple[list[float], list[float]]:
    n = min(len(a), len(b))
    return a[-n:], b[-n:]


def _pearson(a: list[float], b: list[float]) -> float:
    n = len(a)
    if n < 2:
        return 0.0
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    if da <= _EPS or db <= _EPS:
        # A series that never moved has no correlation to measure.  Zero is the
        # honest answer; the unknown-symbol rule is what guards the risky case.
        return 0.0
    return _clamp(num / (da * db), -1.0, 1.0)


def _beta(a: list[float], market: list[float]) -> float:
    n = len(market)
    if n < 2:
        return 1.0
    mm = sum(market) / n
    var = sum((x - mm) ** 2 for x in market)
    if var <= _EPS:
        return 1.0
    ma = sum(a) / n
    cov = sum((x - ma) * (y - mm) for x, y in zip(a, market))
    return cov / var


def _btc_symbol(symbols: Iterable[str]) -> str | None:
    for s in sorted(symbols):
        if s.startswith("BTC"):
            return s
    return None


def _positions_of(ledger: Any) -> list[Position]:
    raw = getattr(ledger, "open_positions", [])
    if isinstance(raw, dict):
        return list(raw.values())
    return list(raw)


def _copy_state(state: PortfolioState) -> PortfolioState:
    return replace(state, positions=list(state.positions))


def _mean_atr_bps(opps: list[Opportunity]) -> float:
    vals = [o.features.atr_bps for o in opps if o.features.atr_bps > 0]
    return sum(vals) / len(vals) if vals else 0.0


def _mean_edge_bps(opps: list[Opportunity]) -> float:
    vals = [o.net_edge_bps for o in opps if o.net_edge_bps > 0]
    return sum(vals) / len(vals) if vals else 0.0


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
