"""Missed-opportunity audit — *were we right to refuse?*

For every period in which idle cash exceeded a threshold (40% by default), this
takes the candidates the bot **already rejected** and asks what the market did
next.  Each rejection is labelled ``MISSED_WINNER`` (the setup would have
reached its NET target before its stop) or ``CORRECT_REJECTION`` (it would
not), and the split is reported **by rejection reason** — which is the only
form of the answer that distinguishes "our gate is well calibrated" from "our
gate is throwing away money".

The leakage boundary
--------------------
This module uses future data **by construction**.  That is legitimate for
post-hoc analysis and fatal if it ever touches a live decision, so the boundary
is enforced in three independent ways rather than documented and hoped for:

1. **Research-only import.**  :func:`assert_research_context` runs at import
   time and raises :class:`LeakageError` if any frame on the importing stack
   belongs to ``app.worker``, ``app.risk``, ``app.predict`` or ``app.routes``.
   It is a *guard*, not the guarantee — a lazy import inside a function would
   still be caught, but an import at module scope of a module imported earlier
   by something else would not.
2. **A static import-boundary test** (``tests/test_research_missed.py``) walks
   the transitive import closure of ``app/worker/``, ``app/risk.py`` and
   ``app/predict/engine.py`` with the ``ast`` module and fails if this module
   appears anywhere in it.  That is the guarantee.
3. **No re-derivation.**  :func:`audit_rejections` takes *already-recorded*
   rejections.  It never calls a ranker, a risk engine or a strategy, and it
   imports none of them, so there is no code path in which a label could be
   computed early enough to influence an entry.  The one thing it is allowed
   to read is bars strictly *after* the rejection it is labelling.

Read ``missed_net_bps`` as the verdict.  If the gate is refusing correctly the
number is **negative** — the refused moves went on to lose, and the refusals
saved money.  Strongly positive means the gate is the problem.
"""

from __future__ import annotations

import enum
import inspect
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "DEFAULT_IDLE_THRESHOLD_PCT",
    "FORBIDDEN_IMPORTERS",
    "LeakageError",
    "MissedAudit",
    "Outcome",
    "RejectedCandidate",
    "assert_research_context",
    "audit_rejections",
    "format_audit",
    "normalise_rejection",
]

_BPS = 10_000.0
MS_PER_MINUTE = 60_000

#: Idle cash above this share of equity is what makes a period interesting.
#: Below it the bot was busy and a refusal is not evidence of anything.
DEFAULT_IDLE_THRESHOLD_PCT = 40.0

#: Packages that must never reach this module, directly or transitively.
FORBIDDEN_IMPORTERS: tuple[str, ...] = (
    "app.worker",
    "app.risk",
    "app.predict",
    "app.routes",
    "app.ranker",
    "app.strategies",
    "app.exits",
)


class LeakageError(ImportError):
    """Raised when a future-data module is pulled into a live decision path."""


def assert_research_context(stack: Sequence[Any] | None = None) -> None:
    """Refuse to be imported from anything that makes live entry decisions.

    Cheap, run once at import, and deliberately *loud*: the failure mode this
    exists to prevent is silent, and would show up as an unreasonably good
    backtest six months later rather than as an exception today.
    """
    frames = stack if stack is not None else inspect.stack()
    try:
        for frame_info in frames:
            g = getattr(frame_info, "frame", None)
            mod = (getattr(g, "f_globals", {}) or {}).get("__name__", "") if g else ""
            if not mod:
                continue
            for banned in FORBIDDEN_IMPORTERS:
                if mod == banned or mod.startswith(banned + "."):
                    raise LeakageError(
                        f"app.research.missed uses future data and must never be "
                        f"imported from {mod!r}. It is a post-hoc audit; a label it "
                        f"produces cannot be allowed anywhere near an entry decision."
                    )
    finally:
        del frames


assert_research_context()


class Outcome(str, enum.Enum):
    MISSED_WINNER = "MISSED_WINNER"
    CORRECT_REJECTION = "CORRECT_REJECTION"
    #: No forward data to judge against (end of series, unknown symbol).  Never
    #: counted as either verdict — an unjudgeable rejection is not a correct one.
    UNDECIDABLE = "UNDECIDABLE"


# --------------------------------------------------------------------------- #
# The recorded rejection
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RejectedCandidate:
    """One candidate the bot refused, as recorded *at the time*.

    Everything here is knowable at the moment of the refusal.  Nothing on this
    record comes from the future; the future arrives only in
    :func:`audit_rejections`, from the bars.

    ``target_bps`` / ``stop_bps`` are the strategy's own GROSS geometry
    (``Signal.target_bps`` / ``Signal.stop_bps``).  ``idle_pct`` is the share of
    equity sitting in cash when the refusal was made — the field that decides
    whether the rejection falls inside a high-idle window at all.
    """

    symbol: str
    ts_ms: int
    reason: str
    price: float
    target_bps: float
    stop_bps: float
    side: str = "LONG"
    idle_pct: float = 100.0
    strategy: str = ""
    net_edge_bps: float = 0.0

    @property
    def is_long(self) -> bool:
        return str(self.side).upper().endswith("LONG")


def normalise_rejection(row: Any) -> RejectedCandidate | None:
    """Coerce a recorded rejection into a :class:`RejectedCandidate`.

    Accepts the dataclass itself, a mapping (a DB row, a JSON line), or any
    object exposing the same attribute names.  Returns ``None`` for a row that
    cannot be judged — no symbol, no price, or no stop — rather than inventing
    a geometry, because a fabricated stop would decide the verdict.
    """
    if isinstance(row, RejectedCandidate):
        return row if row.price > 0 and row.stop_bps > 0 else None

    def get(name: str, default: Any = None) -> Any:
        if isinstance(row, Mapping):
            return row.get(name, default)
        return getattr(row, name, default)

    side = get("side", "LONG")
    side = getattr(side, "value", side)
    reason = get("reason", get("reject_reason", "UNKNOWN"))
    reason = getattr(reason, "value", reason)
    try:
        symbol = str(get("symbol") or "")
        price = float(get("price", get("entry_price", 0.0)) or 0.0)
        stop_bps = float(get("stop_bps", 0.0) or 0.0)
        target_bps = float(get("target_bps", 0.0) or 0.0)
        ts_ms = int(get("ts_ms", 0) or 0)
    except (TypeError, ValueError):
        return None
    if not symbol or price <= 0 or stop_bps <= 0 or target_bps <= 0:
        return None
    return RejectedCandidate(
        symbol=symbol,
        ts_ms=ts_ms,
        reason=str(reason or "UNKNOWN"),
        price=price,
        target_bps=target_bps,
        stop_bps=stop_bps,
        side=str(side or "LONG"),
        idle_pct=float(get("idle_pct", 100.0) or 0.0),
        strategy=str(get("strategy", "") or ""),
        net_edge_bps=float(get("net_edge_bps", 0.0) or 0.0),
    )


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #


@dataclass
class Labelled:
    """One rejection with its post-hoc verdict.  Kept so the audit is auditable."""

    candidate: RejectedCandidate
    outcome: Outcome
    net_bps: float
    exit_ms: int
    bars_forward: int


@dataclass
class MissedAudit:
    """Frozen contract, PHASE5_SPEC.md.

    ``missed_net_bps`` is the **sum** over every examined rejection of what that
    refused setup would have netted, in basis points of its own notional, after
    the round trip.  A sum rather than a mean because the question is what the
    refusals were worth in aggregate; ``avg_net_bps`` carries the per-rejection
    figure for comparability across runs of different sizes.
    """

    windows: int
    rejections_examined: int
    missed_winners: int
    correct_rejections: int
    missed_net_bps: float
    by_reason: dict[str, tuple[int, int]]
    # --- additive: not in the frozen block, defaulted so the constructor
    #     signature in the spec still works positionally.
    undecidable: int = 0
    rejections_supplied: int = 0
    avg_net_bps: float = 0.0
    winner_net_bps: float = 0.0
    rejection_net_bps: float = 0.0
    idle_threshold_pct: float = DEFAULT_IDLE_THRESHOLD_PCT
    horizon_minutes: int = 0
    round_trip_bps: float = 0.0
    labelled: list[Labelled] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def missed_pct(self) -> float:
        judged = self.missed_winners + self.correct_rejections
        return (self.missed_winners / judged * 100.0) if judged else 0.0

    @property
    def verdict(self) -> str:
        """The one-line reading of ``missed_net_bps``."""
        if self.rejections_examined == 0:
            return "NO EVIDENCE — no rejection fell inside a high-idle window"
        if self.missed_net_bps < 0:
            return "GATE CORRECT — the refused moves lost money; refusing saved it"
        if self.missed_net_bps == 0:
            return "GATE NEUTRAL — the refused moves netted exactly zero"
        return "GATE SUSPECT — the refused moves went on to make money"

    def as_dict(self) -> dict[str, Any]:
        return {
            "windows": self.windows,
            "rejections_supplied": self.rejections_supplied,
            "rejections_examined": self.rejections_examined,
            "missed_winners": self.missed_winners,
            "correct_rejections": self.correct_rejections,
            "undecidable": self.undecidable,
            "missed_pct": round(self.missed_pct, 2),
            "missed_net_bps": round(self.missed_net_bps, 4),
            "avg_net_bps": round(self.avg_net_bps, 4),
            "winner_net_bps": round(self.winner_net_bps, 4),
            "rejection_net_bps": round(self.rejection_net_bps, 4),
            "idle_threshold_pct": self.idle_threshold_pct,
            "horizon_minutes": self.horizon_minutes,
            "round_trip_bps": self.round_trip_bps,
            "verdict": self.verdict,
            "by_reason": {k: list(v) for k, v in sorted(self.by_reason.items())},
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- #
# Bars, from whatever the caller has
# --------------------------------------------------------------------------- #


def _bars_by_symbol(dataset: Any, interval: str = "1m") -> dict[str, list[Any]]:
    """Accept a ``Dataset``, a mapping of them, or a plain symbol -> bars dict."""
    if dataset is None:
        return {}
    # app.research.dataset.Dataset
    sym = getattr(dataset, "symbol", None)
    bars_attr = getattr(dataset, "bars", None)
    if sym and isinstance(bars_attr, Mapping):
        series = list(bars_attr.get(interval) or next(iter(bars_attr.values()), []))
        return {str(sym): series}
    if isinstance(dataset, Mapping):
        out: dict[str, list[Any]] = {}
        for key, value in dataset.items():
            inner = _bars_by_symbol(value, interval)
            if inner:
                # A nested Dataset knows its own symbol; a plain list does not.
                out.update(inner if not isinstance(value, (list, tuple)) else {str(key): list(value)})
            elif isinstance(value, (list, tuple)):
                out[str(key)] = list(value)
        return out
    if isinstance(dataset, (list, tuple)) and dataset:
        first = dataset[0]
        s = getattr(first, "symbol", None)
        if s:
            return {str(s): list(dataset)}
    return {}


def _forward_slice(bars: Sequence[Any], after_ms: int, until_ms: int) -> list[Any]:
    """Bars that opened strictly after ``after_ms`` and at or before ``until_ms``.

    Strictly after: the bar the decision was made on is the decision's own bar
    and must not be used to judge it.  This is the entire leakage boundary at
    the row level, so it is one comparison in one place.
    """
    lo, hi = 0, len(bars)
    while lo < hi:  # first index with open_ms > after_ms
        mid = (lo + hi) // 2
        if bars[mid].open_ms > after_ms:
            hi = mid
        else:
            lo = mid + 1
    out: list[Any] = []
    for bar in bars[lo:]:
        if bar.open_ms > until_ms:
            break
        out.append(bar)
    return out


# --------------------------------------------------------------------------- #
# The audit
# --------------------------------------------------------------------------- #


def _label_one(
    cand: RejectedCandidate,
    bars: Sequence[Any],
    *,
    horizon_minutes: int,
    round_trip_bps: float,
) -> Labelled:
    """Would this refused setup have reached its NET target before its stop?

    Three pessimisms, matched to ``app.backtest.engine`` so a MISSED_WINNER here
    means the same thing a win there does:

    * the target must clear the **round trip**, not just the strategy's gross
      target — a move that pays the spread and the fees and nothing else was
      not an opportunity;
    * when one bar's range contains both the stop and the target, the **stop is
      assumed first** — the same refusal to give the backtest a free win;
    * an unresolved setup is marked to the last close and is a CORRECT_REJECTION
      unless it actually reached the target, because "it did not reach its net
      target before its stop" is literally what the label says.
    """
    window = _forward_slice(bars, cand.ts_ms, cand.ts_ms + horizon_minutes * MS_PER_MINUTE)
    if not window:
        return Labelled(cand, Outcome.UNDECIDABLE, 0.0, cand.ts_ms, 0)

    long = cand.is_long
    net_target_bps = cand.target_bps + round_trip_bps
    if long:
        target_px = cand.price * (1.0 + net_target_bps / _BPS)
        stop_px = cand.price * (1.0 - cand.stop_bps / _BPS)
    else:
        target_px = cand.price * (1.0 - net_target_bps / _BPS)
        stop_px = cand.price * (1.0 + cand.stop_bps / _BPS)

    for i, bar in enumerate(window, start=1):
        hit_stop = bar.low <= stop_px if long else bar.high >= stop_px
        hit_target = bar.high >= target_px if long else bar.low <= target_px
        if hit_stop:  # adverse extreme probed first, always
            return Labelled(
                cand,
                Outcome.CORRECT_REJECTION,
                -(cand.stop_bps + round_trip_bps),
                bar.close_ms,
                i,
            )
        if hit_target:
            return Labelled(cand, Outcome.MISSED_WINNER, cand.target_bps, bar.close_ms, i)

    last = window[-1]
    move = (last.close - cand.price) / cand.price * _BPS
    if not long:
        move = -move
    return Labelled(
        cand, Outcome.CORRECT_REJECTION, move - round_trip_bps, last.close_ms, len(window)
    )


def audit_rejections(
    dataset: Any,
    rejections: Iterable[Any],
    *,
    horizon_minutes: int,
    round_trip_bps: float,
    idle_threshold_pct: float = DEFAULT_IDLE_THRESHOLD_PCT,
    window_minutes: int | None = None,
    interval: str = "1m",
) -> MissedAudit:
    """Label already-recorded rejections against what the market did next.

    ``dataset`` may be one ``research.dataset.Dataset``, a mapping of them, or a
    plain ``{symbol: [Bar, ...]}``.  ``rejections`` are records the bot wrote
    **at decision time** — this function never re-derives a rejection, never
    consults a strategy, ranker or risk engine, and never returns anything a
    live decision is allowed to read.

    Only rejections inside a *high-idle window* are examined: a window is one
    ``window_minutes`` bucket (default: the horizon) in which idle cash exceeded
    ``idle_threshold_pct``.  A refusal made while the book was already full is
    not evidence about the gate; it is evidence about the budget.

    An empty ``rejections`` produces an empty audit, not a crash.
    """
    horizon_minutes = int(max(horizon_minutes, 0))
    window_ms = int(max(window_minutes or horizon_minutes, 1)) * MS_PER_MINUTE
    bars = _bars_by_symbol(dataset, interval)
    notes: list[str] = []

    supplied = 0
    candidates: list[RejectedCandidate] = []
    for row in rejections or ():
        supplied += 1
        cand = normalise_rejection(row)
        if cand is not None:
            candidates.append(cand)
    dropped = supplied - len(candidates)
    if dropped:
        notes.append(
            f"{dropped} recorded rejection(s) carried no usable price/geometry and "
            f"were dropped rather than judged against an invented stop"
        )

    empty = MissedAudit(
        windows=0,
        rejections_examined=0,
        missed_winners=0,
        correct_rejections=0,
        missed_net_bps=0.0,
        by_reason={},
        rejections_supplied=supplied,
        idle_threshold_pct=idle_threshold_pct,
        horizon_minutes=horizon_minutes,
        round_trip_bps=round_trip_bps,
        notes=notes,
    )
    if not candidates:
        if supplied == 0:
            empty.notes.append("no rejections supplied — nothing to audit")
        return empty
    if horizon_minutes <= 0:
        empty.notes.append("horizon_minutes <= 0 — no forward window to judge against")
        return empty

    # -- windows: buckets in which the bot was sitting on its hands ---------- #
    idle_by_bucket: dict[int, float] = {}
    for cand in candidates:
        b = cand.ts_ms // window_ms
        idle_by_bucket[b] = max(idle_by_bucket.get(b, 0.0), cand.idle_pct)
    hot = {b for b, idle in idle_by_bucket.items() if idle > idle_threshold_pct}
    if not hot:
        empty.notes.append(
            f"every recorded rejection was made with idle cash at or below "
            f"{idle_threshold_pct:.0f}% — no high-idle window to audit"
        )
        return empty

    examined = [c for c in candidates if c.ts_ms // window_ms in hot]

    labelled: list[Labelled] = []
    missing_symbols: set[str] = set()
    for cand in examined:
        series = bars.get(cand.symbol)
        if not series:
            missing_symbols.add(cand.symbol)
            labelled.append(Labelled(cand, Outcome.UNDECIDABLE, 0.0, cand.ts_ms, 0))
            continue
        labelled.append(
            _label_one(
                cand,
                series,
                horizon_minutes=horizon_minutes,
                round_trip_bps=round_trip_bps,
            )
        )
    if missing_symbols:
        notes.append(
            "no forward bars for " + ", ".join(sorted(missing_symbols))
            + " — those rejections are UNDECIDABLE, never CORRECT"
        )

    by_reason: dict[str, tuple[int, int]] = {}
    winners = corrects = undecidable = 0
    total_bps = winner_bps = rejection_bps = 0.0
    for lab in labelled:
        reason = lab.candidate.reason
        m, c = by_reason.get(reason, (0, 0))
        if lab.outcome is Outcome.MISSED_WINNER:
            winners += 1
            winner_bps += lab.net_bps
            by_reason[reason] = (m + 1, c)
        elif lab.outcome is Outcome.CORRECT_REJECTION:
            corrects += 1
            rejection_bps += lab.net_bps
            by_reason[reason] = (m, c + 1)
        else:
            undecidable += 1
            by_reason.setdefault(reason, (m, c))
            continue
        total_bps += lab.net_bps

    judged = winners + corrects
    return MissedAudit(
        windows=len(hot),
        rejections_examined=len(examined),
        missed_winners=winners,
        correct_rejections=corrects,
        missed_net_bps=total_bps,
        by_reason=by_reason,
        undecidable=undecidable,
        rejections_supplied=supplied,
        avg_net_bps=(total_bps / judged) if judged else 0.0,
        winner_net_bps=winner_bps,
        rejection_net_bps=rejection_bps,
        idle_threshold_pct=idle_threshold_pct,
        horizon_minutes=horizon_minutes,
        round_trip_bps=round_trip_bps,
        labelled=labelled,
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

WIDTH = 92


def format_audit(audit: MissedAudit, *, title: str = "MISSED-OPPORTUNITY AUDIT") -> str:
    """The split by reason, which is the whole deliverable."""
    out: list[str] = ["=" * WIDTH, title, "=" * WIDTH]
    out.append(
        f"high-idle windows (idle > {audit.idle_threshold_pct:.0f}%): {audit.windows}"
        f"   horizon {audit.horizon_minutes}m   round trip {audit.round_trip_bps:.1f} bps"
    )
    out.append(
        f"rejections: {audit.rejections_supplied} recorded, {audit.rejections_examined} "
        f"in high-idle windows, {audit.undecidable} undecidable"
    )
    if audit.rejections_examined == 0:
        out.append("")
        out.append(audit.verdict)
        for n in audit.notes:
            out.append(f"  - {n}")
        out.append("=" * WIDTH)
        return "\n".join(out)

    out.append("")
    out.append(
        f"{'REJECTION REASON':<28}{'MISSED':>8}{'CORRECT':>9}{'MISSED %':>10}"
    )
    out.append("-" * WIDTH)
    for reason, (missed, correct) in sorted(
        audit.by_reason.items(), key=lambda kv: (-(kv[1][0] + kv[1][1]), kv[0])
    ):
        total = missed + correct
        pct = (missed / total * 100.0) if total else 0.0
        out.append(f"{reason:<28}{missed:>8}{correct:>9}{pct:>9.1f}%")
    out.append("-" * WIDTH)
    out.append(
        f"{'TOTAL':<28}{audit.missed_winners:>8}{audit.correct_rejections:>9}"
        f"{audit.missed_pct:>9.1f}%"
    )
    out.append("")
    out.append(
        f"missed_net_bps = {audit.missed_net_bps:+.1f} bps total "
        f"({audit.avg_net_bps:+.2f} bps per refused candidate)"
    )
    out.append(f"  winners would have paid  {audit.winner_net_bps:+.1f} bps")
    out.append(f"  refusals avoided         {audit.rejection_net_bps:+.1f} bps")
    out.append("")
    out.append(audit.verdict)
    for n in audit.notes:
        out.append(f"  - {n}")
    out.append("=" * WIDTH)
    return "\n".join(out)
