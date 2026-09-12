"""The live opportunity board, episode identity, and the close → entry loop.

Three problems, one module, because they are the same problem seen from three
sides: *the capital came back and nothing was ready to use it*.

**The board (§13).**  A ranked candidate that loses to a better one is not a bad
idea, it is a second-best idea, and V5 threw it away every cycle.  It is kept
here instead, and the moment capital frees up the best surviving entry is
considered — **after being re-costed against the current book**.  A queue of
stale prices is worse than no queue at all: it converts "we had something ready"
into "we bought yesterday's number", so :func:`revalidate` rebuilds the
opportunity from scratch on live inputs and the queued ``net_ev_bps`` is used
for nothing except ranking the board and measuring how far the idea decayed.

**Episode identity (§14).**  "One position per symbol" is too blunt — it forbids
a genuinely new BTC setup an hour later — and "cooldown after a close" is too
blunt in the other direction, forbidding every symbol for a fixed time.  What we
actually want to forbid is *re-entering the same idea*.  So each impulse gets an
id::

    episode_id = sha1(symbol | strategy | regime | signal_bucket | counter)

and the counter advances **only** on a false → true transition of the strategy's
trigger.  While the trigger stays true the id does not move, however much the
feature drifts — that is what stops a single impulse from minting a fresh
identity every second and re-entering itself.  Once an episode has been traded
it can never be traded again (:class:`EpisodeLedger`); a new impulse on the same
coin mints a new counter value and is free to trade, BTC included.

**The recycling loop (§11, §12, §15).**  :class:`CapitalRecycler` is what the
worker calls when a position closes.  It re-reads the book, triggers a rescan
*in that call* rather than waiting for the scheduler, times the whole path into
a :class:`~app.portfolio.RecycleEvent`, and emits ``CAPITAL_RELEASED``.  It
installs **no global cooldown**: closing SOL has no effect whatsoever on opening
ETH.  The only cooldown available is per symbol, and it defaults to off.

Wiring (``app/worker/main.py``, owned elsewhere) is two lines::

    self.recycler = CapitalRecycler(portfolio=self.portfolio, rescan=self.evaluate_once)
    ...
    self.recycler.on_close(ledger, pos, trade=trade, ts_ms=ts_ms)   # after ledger.close

Nothing here touches the network, the database or the clock except through an
injected ``clock``.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Sequence

from app.portfolio import IdleContext, IdleReason, PortfolioEngine, RecycleEvent
from app.ranker import OpportunityRanker
from app.types import (
    Features,
    Opportunity,
    OrderBook,
    Position,
    Regime,
    RegimeState,
    RejectReason,
    Side,
    Signal,
    now_ms,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.config import Settings

_EPS = 1e-9

#: Depth is measured over the same 10 bps window the allocator sizes against,
#: so the gate and the sizing are talking about the same dollars.
_DEPTH_WINDOW_BPS = 10.0

__all__ = [
    "CapitalRecycler",
    "Episode",
    "EpisodeLedger",
    "EpisodeReentry",
    "EpisodeTracker",
    "OpportunityQueue",
    "QueuedOpportunity",
    "episode_id",
    "revalidate",
    "revalidate_detailed",
    "signal_bucket",
]


# --------------------------------------------------------------------------- #
# Episode identity (§14)
# --------------------------------------------------------------------------- #


def signal_bucket(value: float, width: float) -> int:
    """Quantise the triggering feature into a coarse, stable bucket.

    Floor division rather than rounding: rounding puts the boundary in the
    middle of the bucket, so a feature oscillating around a bucket centre flips
    the id back and forth, which is precisely the behaviour the quantisation
    exists to prevent.  A non-positive width means "do not quantise", which is
    never what you want in production but is convenient in a test that wants
    every value distinct.
    """
    if width <= 0:
        return 0
    if not math.isfinite(value):
        return 0
    return int(math.floor(float(value) / float(width)))


def episode_id(
    symbol: str, strategy: str, regime: str, bucket: int, counter: int
) -> str:
    """``sha1(symbol | strategy | regime | signal_bucket | counter)``.

    A hash rather than a tuple because the id travels: it goes on a position, a
    queue entry, an event payload and a dashboard row, and every one of those
    wants an opaque, fixed-width, comparable string.  It is an identity, never a
    secret — collisions are what matter here and sha1 has none at this scale.
    """
    raw = "|".join((symbol, strategy, str(regime), str(bucket), str(counter)))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Episode:
    """One impulse, from the instant the trigger went true.

    ``bucket`` and ``regime`` are frozen at that instant and never recomputed
    while the episode lives.  Recomputing them per observation would let a
    drifting feature cross a bucket boundary mid-impulse and mint a second id
    for the same idea — a re-entry with a clean record, which is the exact
    defect §14 forbids.
    """

    episode_id: str
    symbol: str
    strategy: str
    regime: str
    bucket: int
    counter: int
    opened_ms: int
    trigger_value: float = 0.0

    def as_dict(self) -> dict:
        return {
            "episode_id": self.episode_id,
            "symbol": self.symbol,
            "strategy": self.strategy,
            "regime": self.regime,
            "bucket": self.bucket,
            "counter": self.counter,
            "opened_ms": self.opened_ms,
            "trigger_value": self.trigger_value,
        }


class EpisodeTracker:
    """Mints one id per *impulse*, not one per observation.

    Keyed by ``(symbol, strategy)`` because two strategies firing on the same
    coin are two ideas and deserve two episodes — one may be right while the
    other is wrong, and merging them would let the loser block the winner.

    The counter is monotone per key and never resets, not even when the regime
    changes or the process is asked about a symbol it has not seen for hours.
    That is what makes a re-used id impossible rather than merely unlikely.
    """

    def __init__(
        self,
        *,
        bucket_width: float | None = None,
        settings: "Settings | None" = None,
    ) -> None:
        if settings is None:
            from app.config import get_settings

            settings = get_settings()
        self.settings = settings
        self.bucket_width = float(
            bucket_width
            if bucket_width is not None
            else settings.recycle.signal_bucket_bps
        )
        self._counters: dict[tuple[str, str], int] = {}
        self._armed: dict[tuple[str, str], bool] = {}
        self._current: dict[tuple[str, str], Episode] = {}

    def observe(
        self,
        *,
        symbol: str,
        strategy: str,
        regime: str,
        triggered: bool,
        value: float = 0.0,
        ts_ms: int = 0,
    ) -> Episode | None:
        """Feed one observation of a strategy's trigger; get the live episode.

        Returns ``None`` while the trigger is false.  On a false → true
        transition the counter advances and a new :class:`Episode` is minted.
        While the trigger stays true the *same* episode comes back, unchanged.
        """
        key = (symbol, strategy)
        if not triggered:
            self._armed[key] = False
            self._current.pop(key, None)
            return None

        current = self._current.get(key)
        if self._armed.get(key) and current is not None:
            return current

        counter = self._counters.get(key, 0) + 1
        self._counters[key] = counter
        bucket = signal_bucket(value, self.bucket_width)
        episode = Episode(
            episode_id=episode_id(symbol, strategy, regime, bucket, counter),
            symbol=symbol,
            strategy=strategy,
            regime=str(regime),
            bucket=bucket,
            counter=counter,
            opened_ms=int(ts_ms),
            trigger_value=float(value),
        )
        self._armed[key] = True
        self._current[key] = episode
        return episode

    def observe_signal(
        self, signal: Signal, regime: RegimeState | str, *, ts_ms: int | None = None
    ) -> Episode:
        """:meth:`observe` for the common case: a strategy produced a signal.

        A signal *is* the trigger being true, so this never returns ``None``.
        The quantised feature is ``target_bps`` — the strategy's own statement
        of the move it is reacting to, the one magnitude every strategy in the
        codebase produces, and already in the basis points
        ``recycle.signal_bucket_bps`` is expressed in.
        """
        name = regime.regime.value if isinstance(regime, RegimeState) else str(regime)
        episode = self.observe(
            symbol=signal.symbol,
            strategy=signal.strategy,
            regime=name,
            triggered=True,
            value=float(signal.target_bps),
            ts_ms=int(ts_ms if ts_ms is not None else signal.ts_ms),
        )
        assert episode is not None  # triggered=True always mints or returns one
        return episode

    def stand_down(self, symbol: str, strategy: str) -> None:
        """Record the trigger going false, so the next one is a new episode."""
        self.observe(symbol=symbol, strategy=strategy, regime="", triggered=False)

    def current(self, symbol: str, strategy: str) -> Episode | None:
        return self._current.get((symbol, strategy))

    def counter(self, symbol: str, strategy: str) -> int:
        return self._counters.get((symbol, strategy), 0)


class EpisodeReentry(RuntimeError):
    """An episode that has already been traded was offered for entry again.

    Not a rejection — a **defect**.  The queue and the recycler both refuse a
    claimed episode long before this can fire, so reaching it means something
    bypassed the check, and swallowing it would hide a duplicate position
    behind a quiet log line.
    """


class EpisodeLedger:
    """Every episode that has ever been entered.  Entering one twice is impossible.

    Bounded, because a process that runs for months cannot keep every id it has
    ever seen.  Eviction is safe in a way worth stating: ids are minted from a
    **monotone** counter, so an evicted id can only be offered again if the
    tracker rewound, which it cannot do.  The bound protects memory; the counter
    protects the invariant.
    """

    def __init__(self, *, capacity: int = 50_000) -> None:
        self.capacity = max(int(capacity), 1)
        # dict rather than set: insertion-ordered, so eviction is FIFO and the
        # oldest ids — the ones no tracker can mint again — go first.
        self._entered: dict[str, tuple[str, int]] = {}

    def __len__(self) -> int:
        return len(self._entered)

    def __contains__(self, episode: str) -> bool:  # pragma: no cover - sugar
        return episode in self._entered

    def entered(self, episode: str | None) -> bool:
        """Whether this exact episode has already been traded."""
        return bool(episode) and episode in self._entered

    def claim(self, episode: str, *, symbol: str = "", ts_ms: int = 0) -> None:
        """Record an entry.  Raises :class:`EpisodeReentry` on a second claim."""
        if not episode:
            raise ValueError("an entry must carry an episode_id")
        prior = self._entered.get(episode)
        if prior is not None:
            raise EpisodeReentry(
                f"episode {episode} ({prior[0] or symbol}) was already entered at "
                f"{prior[1]}: re-entering the same episode is a defect, not a retry"
            )
        self._entered[episode] = (symbol, int(ts_ms))
        while len(self._entered) > self.capacity:
            self._entered.pop(next(iter(self._entered)))

    def entered_at(self, episode: str) -> int | None:
        row = self._entered.get(episode)
        return None if row is None else row[1]


# --------------------------------------------------------------------------- #
# The board (§13)
# --------------------------------------------------------------------------- #


@dataclass
class QueuedOpportunity:
    """A ranked candidate kept alive while there was no capital for it.

    ``net_ev_bps`` is **the number it was ranked at**, not the number it will be
    traded at.  It orders the board and it measures decay; it is never the basis
    of an entry.  :func:`revalidate` produces the number an entry is allowed to
    use, and it is computed from the live book.
    """

    symbol: str
    strategy: str
    episode_id: str
    net_ev_bps: float
    ranked_at_ms: int
    expires_ms: int
    # Additions; the six fields above are the frozen contract.
    opportunity: Opportunity | None = None
    rank_score: float = 0.0
    side: Side = Side.LONG
    regime: str = Regime.UNKNOWN.value
    refreshed: int = 0
    notes: list[str] = field(default_factory=list)

    def expired(self, ts_ms: int) -> bool:
        return ts_ms >= self.expires_ms

    def age_ms(self, ts_ms: int) -> int:
        return max(int(ts_ms) - self.ranked_at_ms, 0)

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "episode_id": self.episode_id,
            "net_ev_bps": self.net_ev_bps,
            "ranked_at_ms": self.ranked_at_ms,
            "expires_ms": self.expires_ms,
            "rank_score": self.rank_score,
            "side": self.side.value,
            "regime": self.regime,
            "refreshed": self.refreshed,
            "notes": list(self.notes),
        }


def revalidate_detailed(
    q: QueuedOpportunity,
    *,
    book: OrderBook | None,
    features: Features | None,
    fees: Any,
    settings: "Settings | None" = None,
    ranker: OpportunityRanker | None = None,
    episodes: EpisodeLedger | None = None,
    slippage_bps: float | None = None,
    ts_ms: int | None = None,
) -> tuple[Opportunity | None, RejectReason | None]:
    """:func:`revalidate`, plus *why* — so the board knows what to forget.

    The reason separates a queue entry that is finished (expired, already
    traded, not a spot-tradable side) from one that merely lost this round
    (spread widened, book thinned, edge decayed).  The first kind is dropped,
    the second stays on the board, because a spread that is 8 bps now may be
    3 bps in ten seconds and throwing the idea away would mean re-discovering
    it from nothing.
    """
    if settings is None:
        from app.config import get_settings

        settings = get_settings()
    now = int(ts_ms if ts_ms is not None else now_ms())

    if q.expired(now):
        return None, RejectReason.STALE_BOOK
    if episodes is not None and episodes.entered(q.episode_id):
        # The same idea, already traded.  This is the §14 guard doing its job.
        return None, RejectReason.DUPLICATE_SYMBOL
    if q.opportunity is None:
        # A row with no signal cannot be re-costed, and a row that cannot be
        # re-costed must never be deployed on the number it was queued with.
        return None, RejectReason.DATA_FAILURE
    if q.opportunity.signal.side is not Side.LONG:
        # Spot is long-only.  A short is recorded, never executed.
        return None, RejectReason.NOT_SUPPORTED_SHORT
    if features is None or book is None:
        return None, RejectReason.DATA_FAILURE
    if features.symbol != q.symbol or book.symbol != q.symbol:
        return None, RejectReason.DATA_FAILURE

    risk = settings.risk
    if book.desynced or not book.bids or not book.asks or book.mid <= 0:
        return None, RejectReason.STALE_BOOK
    if book.age_ms(now) > risk.max_book_age_ms:
        return None, RejectReason.STALE_BOOK
    if book.spread_bps > risk.max_entry_spread_bps:
        return None, RejectReason.WIDE_SPREAD
    depth = min(
        book.depth_usd("bid", _DEPTH_WINDOW_BPS), book.depth_usd("ask", _DEPTH_WINDOW_BPS)
    )
    if depth < risk.min_book_depth_usd:
        return None, RejectReason.THIN_DEPTH

    ranker = ranker or OpportunityRanker(settings=settings)
    maker, taker = _fee_rates(fees, q.symbol, settings=settings)
    # No fresh slippage estimate means carrying the queued one forward: it is
    # the one input that needs the simulator's book walk, and inventing a
    # second, cheaper slippage model here would mean two numbers that disagree.
    # The depth and spread gates above are what stand in for it.
    slip = (
        float(slippage_bps)
        if slippage_bps is not None
        else float(q.opportunity.slippage_bps)
    )

    fresh = ranker.build(
        replace(q.opportunity.signal, ts_ms=now),
        features,
        q.opportunity.regime,
        taker_bps=taker,
        maker_bps=maker,
        slippage_bps=slip,
    )
    if not ranker.qualifies(fresh):
        return None, (
            RejectReason.NEGATIVE_EDGE
            if fresh.net_edge_bps < 0.0
            else RejectReason.EDGE_BELOW_MIN
        )

    floor = q.net_ev_bps * (1.0 - settings.recycle.max_ev_decay_pct / 100.0)
    if fresh.net_edge_bps < floor - _EPS:
        return None, RejectReason.EDGE_BELOW_MIN

    fresh.notes.append(
        f"revalidated after {q.age_ms(now) / 1000.0:.1f}s queued: "
        f"{q.net_ev_bps:.2f} -> {fresh.net_edge_bps:.2f} net bps"
    )
    return fresh, None


def revalidate(
    q: QueuedOpportunity, *, book, features, fees, **kw
) -> Opportunity | None:
    """Re-cost a queued candidate against the live market.

    Returns a **new** :class:`~app.types.Opportunity` built from the current
    features, the current book and the current fee table, or ``None`` if it no
    longer qualifies.  The returned object shares nothing with the queued one
    except its signal, which is what makes "deployed on its old numbers"
    structurally impossible rather than merely discouraged.
    """
    return revalidate_detailed(q, book=book, features=features, fees=fees, **kw)[0]


def _fee_rates(fees: Any, symbol: str, *, settings: "Settings") -> tuple[float, float]:
    """``(maker_bps, taker_bps)`` from a fee config, a mapping or a flat rate."""
    if fees is None:
        fees = settings.fees
    maker = getattr(fees, "maker_bps", None)
    taker = getattr(fees, "taker_bps", None)
    if callable(maker) and callable(taker):
        return float(maker(symbol)), float(taker(symbol))
    if isinstance(fees, Mapping):
        return float(fees.get("maker_bps", 0.0)), float(fees.get("taker_bps", 0.0))
    if isinstance(fees, (int, float)):
        return float(fees), float(fees)
    return float(settings.fees.maker_bps(symbol)), float(settings.fees.taker_bps(symbol))


class OpportunityQueue:
    """The board: qualifying candidates that had no capital, kept ranked.

    Bounded and expiring.  Both bounds are there for the same reason — an entry
    nobody will ever deploy is not a queued opportunity, it is a log line with
    aspirations — and both are configurable on :class:`~app.config.RecycleConfig`.

    Entries are keyed by ``episode_id``, so re-offering the same idea on the
    next cycle **refreshes** it with the newer numbers instead of stacking a
    second copy.  A board full of the same coin at five different ages would
    rank by age rather than by merit.
    """

    def __init__(
        self,
        *,
        settings: "Settings | None" = None,
        ranker: OpportunityRanker | None = None,
        episodes: EpisodeLedger | None = None,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        if settings is None:
            from app.config import get_settings

            settings = get_settings()
        self.settings = settings
        self.cfg = settings.recycle
        self.ranker = ranker or OpportunityRanker(settings=settings)
        self.episodes = episodes if episodes is not None else EpisodeLedger()
        self.clock = clock
        self._entries: dict[str, QueuedOpportunity] = {}
        self.dropped: dict[str, int] = {}

    # -- board maintenance ---------------------------------------------- #

    def __len__(self) -> int:
        return len(self._entries)

    def entries(self) -> list[QueuedOpportunity]:
        """Best first, ties broken by symbol so the order is reproducible."""
        return sorted(
            self._entries.values(), key=lambda q: (-q.net_ev_bps, q.symbol, q.strategy)
        )

    def offer(
        self,
        opp: Opportunity,
        *,
        episode_id: str,
        ts_ms: int | None = None,
        ttl_seconds: float | None = None,
    ) -> QueuedOpportunity | None:
        """Retain one ranked candidate.  ``None`` if it does not belong on the board.

        Refused for exactly two reasons: the queue is switched off, or the
        episode has already been traded.  Notably *not* refused for being
        second-best — that is the entire point of the board.
        """
        if not self.cfg.queue_enabled:
            return None
        if self.episodes.entered(episode_id):
            return None
        now = int(ts_ms if ts_ms is not None else self.clock())
        ttl = float(ttl_seconds if ttl_seconds is not None else self.cfg.queue_ttl_seconds)

        existing = self._entries.get(episode_id)
        entry = QueuedOpportunity(
            symbol=opp.symbol,
            strategy=opp.signal.strategy,
            episode_id=episode_id,
            net_ev_bps=float(opp.net_edge_bps),
            ranked_at_ms=now,
            expires_ms=now + int(ttl * 1000.0),
            opportunity=opp,
            rank_score=float(opp.rank_score),
            side=opp.signal.side,
            regime=opp.regime.regime.value,
            refreshed=0 if existing is None else existing.refreshed + 1,
        )
        self._entries[episode_id] = entry
        self._evict(now)
        return entry

    def offer_ranked(
        self,
        ranked: Sequence[Opportunity],
        *,
        episodes: "Mapping[str, str] | Callable[[Opportunity], str | None]",
        ts_ms: int | None = None,
        skip: Iterable[str] = (),
    ) -> int:
        """Offer a whole ranked list; returns how many entries the board took.

        ``episodes`` maps an opportunity to its episode id — a dict keyed by
        symbol, or a callable for callers that key on ``(symbol, strategy)``.
        An opportunity with no id is skipped rather than given a synthetic one:
        an entry that cannot be identified cannot be guarded against re-entry,
        and a queue entry that can be re-entered is worse than no entry.
        """
        now = int(ts_ms if ts_ms is not None else self.clock())
        blocked = set(skip)
        taken = 0
        for opp in ranked:
            if opp.symbol in blocked:
                continue
            eid = episodes(opp) if callable(episodes) else episodes.get(opp.symbol)
            if not eid:
                continue
            if self.offer(opp, episode_id=eid, ts_ms=now) is not None:
                taken += 1
        return taken

    def prune(self, ts_ms: int | None = None) -> int:
        """Drop expired entries and anything already traded.  Returns the count."""
        now = int(ts_ms if ts_ms is not None else self.clock())
        gone = [
            eid
            for eid, q in self._entries.items()
            if q.expired(now) or self.episodes.entered(eid)
        ]
        for eid in gone:
            self._forget(eid, "EXPIRED")
        return len(gone)

    def remove(self, episode: str) -> None:
        self._entries.pop(episode, None)

    def best(
        self, ts_ms: int | None = None, *, exclude: Iterable[str] = ()
    ) -> QueuedOpportunity | None:
        """The highest queued EV that is still live, ignoring ``exclude``d symbols."""
        now = int(ts_ms if ts_ms is not None else self.clock())
        blocked = set(exclude)
        for q in self.entries():
            if q.expired(now) or q.symbol in blocked:
                continue
            if self.episodes.entered(q.episode_id):
                continue
            return q
        return None

    def take(
        self,
        *,
        books: Mapping[str, OrderBook],
        features: Mapping[str, Features],
        fees: Any = None,
        ts_ms: int | None = None,
        exclude: Iterable[str] = (),
        slippage: "Callable[[QueuedOpportunity, OrderBook], float] | None" = None,
    ) -> Opportunity | None:
        """The best queued idea that **still qualifies right now**, or ``None``.

        Walks the board best-first and returns the first entry that survives
        :func:`revalidate` against the live book.  Entries that are finished are
        dropped; entries that merely lost this round stay, because the next
        close is seconds away and the spread moves faster than the board does.

        The returned opportunity is freshly built, so the caller sizes and gates
        it exactly as it would a candidate that had just been ranked — there is
        no "queued" path through the risk engine, and there must never be one.
        """
        now = int(ts_ms if ts_ms is not None else self.clock())
        blocked = set(exclude)
        for q in self.entries():
            if q.symbol in blocked:
                continue
            book = books.get(q.symbol)
            slip = None if slippage is None or book is None else slippage(q, book)
            fresh, reason = revalidate_detailed(
                q,
                book=book,
                features=features.get(q.symbol),
                fees=fees,
                settings=self.settings,
                ranker=self.ranker,
                episodes=self.episodes,
                slippage_bps=slip,
                ts_ms=now,
            )
            if fresh is not None:
                return fresh
            if reason in _TERMINAL:
                self._forget(q.episode_id, reason.value)
        return None

    def as_dict(self) -> dict:
        return {
            "size": len(self._entries),
            "max_size": self.cfg.queue_max_size,
            "ttl_seconds": self.cfg.queue_ttl_seconds,
            "entries": [q.as_dict() for q in self.entries()],
            "dropped": dict(self.dropped),
        }

    # -- internals ------------------------------------------------------- #

    def _forget(self, episode: str, reason: str) -> None:
        if self._entries.pop(episode, None) is not None:
            self.dropped[reason] = self.dropped.get(reason, 0) + 1

    def _evict(self, ts_ms: int) -> None:
        """Keep the board inside its bound, worst EV first."""
        cap = max(int(self.cfg.queue_max_size), 1)
        if len(self._entries) <= cap:
            return
        for q in reversed(self.entries()[cap:]):
            self._forget(q.episode_id, "EVICTED")
            if len(self._entries) <= cap:
                break


#: Revalidation reasons that mean the entry is *finished*, not merely beaten.
_TERMINAL: frozenset[RejectReason] = frozenset(
    {
        RejectReason.STALE_BOOK,
        RejectReason.DUPLICATE_SYMBOL,
        RejectReason.NOT_SUPPORTED_SHORT,
        RejectReason.DATA_FAILURE,
    }
)


# --------------------------------------------------------------------------- #
# The recycling loop (§11, §12, §15)
# --------------------------------------------------------------------------- #

#: The event name the worker persists.  One string, one place.
CAPITAL_RELEASED = "CAPITAL_RELEASED"


class CapitalRecycler:
    """Close → release → rescan → redeploy, measured end to end.

    Everything here happens **in the call that observed the close**.  That is
    the whole of §11 and §12: a scheduler that gets round to it on the next tick
    leaves the money idle for an interval that has nothing to do with the market,
    and :class:`~app.portfolio.IdleReason` ``SCANNER_DELAY`` exists to make that
    delay visible when it happens anyway.

    What it deliberately does **not** do (§15): install a cooldown.  A close —
    profitable or not — has no effect on any other symbol, ever.  The single
    cooldown available is ``recycle.symbol_cooldown_seconds``, it applies to the
    closed symbol alone, and it defaults to **0**.
    """

    def __init__(
        self,
        *,
        portfolio: PortfolioEngine | None = None,
        queue: OpportunityQueue | None = None,
        episodes: EpisodeLedger | None = None,
        rescan: "Callable[[int], Sequence[Opportunity]] | None" = None,
        emit: "Callable[[str, dict], None] | None" = None,
        clock: Callable[[], int] = now_ms,
        settings: "Settings | None" = None,
    ) -> None:
        if settings is None:
            settings = (
                portfolio.settings if portfolio is not None else _default_settings()
            )
        self.settings = settings
        self.cfg = settings.recycle
        self.portfolio = portfolio
        self.episodes = episodes or (queue.episodes if queue else EpisodeLedger())
        self.queue = queue or OpportunityQueue(
            settings=settings, episodes=self.episodes, clock=clock
        )
        # One ledger, whatever the caller passed: two would mean the board
        # could retain an episode the recycler has already entered, which is
        # the §14 invariant failing quietly instead of loudly.
        self.queue.episodes = self.episodes
        self.rescan = rescan
        self.emit = emit
        self.clock = clock
        self.events: list[RecycleEvent] = []
        self._last_rank_ms = 0
        self._last_close_ms: dict[str, int] = {}

    # -- the hook the worker calls --------------------------------------- #

    def on_close(
        self,
        ledger: Any,
        position: Position,
        *,
        trade: Any = None,
        ts_ms: int | None = None,
        proceeds_usd: float | None = None,
    ) -> RecycleEvent:
        """Release the capital and trigger a rescan, now, in this call.

        ``ledger`` has already booked the exit (the ledger credits cash inside
        :meth:`~app.sim.ledger.Ledger.close`); this re-reads the book from it so
        the *portfolio* view — available cash, open risk, correlated risk,
        directional exposure — is current in the same breath rather than at the
        next state refresh.  The two are different things, and it is the second
        one that decides whether the next candidate is allowed in.
        """
        closed_ms = int(ts_ms if ts_ms is not None else self.clock())
        released = float(
            proceeds_usd
            if proceeds_usd is not None
            else getattr(trade, "notional_usd", position.notional_usd)
        )
        event = RecycleEvent(
            closed_trade_id=str(getattr(trade, "id", position.id)),
            closed_ms=closed_ms,
            capital_released_ms=closed_ms,
            rescan_triggered_ms=closed_ms,
            symbol=position.symbol,
            released_usd=released,
            risk_released_usd=max(float(position.risk_usd), 0.0),
        )

        state = None
        if self.portfolio is not None and ledger is not None:
            state = self.portfolio.state(ledger, ts_ms=closed_ms)
            event.available_usd_after = state.available_usd
            event.deployed_pct_after = state.deployed_pct
        event.capital_released_ms = int(self.clock())
        event.state_bug_usd = self._books_gap(ledger, position, state)
        if event.state_bug_usd > _EPS:
            event.notes.append(
                f"capital did not reconcile after the close: "
                f"${event.state_bug_usd:.2f} unaccounted for"
            )

        self._emit(event)
        self._last_close_ms[position.symbol] = closed_ms

        # The rescan.  Triggered *before* it is handled, because the number we
        # publish is how long the loop took to react, not how long a scan takes.
        event.rescan_triggered_ms = int(self.clock())
        ranked: Sequence[Opportunity] = ()
        if self.cfg.rescan_on_close and self.rescan is not None:
            ranked = self.rescan(event.rescan_triggered_ms) or ()
            event.rescan_handled = True
            self._last_rank_ms = event.rescan_triggered_ms
        elif self.rescan is None:
            event.notes.append("no rescan handler wired: capital waits for the tick")

        self.queue.prune(event.rescan_triggered_ms)
        event.qualified_candidate_existed = bool(ranked) or (
            self.queue.best(event.rescan_triggered_ms) is not None
        )

        self.events.append(event)
        del self.events[: max(len(self.events) - self.cfg.event_history, 0)]
        return event

    def note_rescan(self, ts_ms: int | None = None) -> None:
        """Record that a ranking happened, however it was triggered.

        The worker's own evaluation loop calls this so that ``SCANNER_DELAY``
        measures the gap to the *last* ranking rather than to the last
        close-triggered one.
        """
        self._last_rank_ms = int(ts_ms if ts_ms is not None else self.clock())

    def note_entry(
        self,
        symbol: str,
        *,
        ts_ms: int | None = None,
        episode_id: str | None = None,
    ) -> RecycleEvent | None:
        """Close the loop: an entry happened, so the capital is working again.

        Attaches to the most recent close that has not yet been followed by an
        entry, and returns it — or ``None`` when the entry did not follow a
        close at all, which is an ordinary opening trade and not a recycle.

        Claiming the episode here is the **hard** half of the §14 invariant: the
        queue and :func:`revalidate` both refuse an already-traded episode, so
        reaching :class:`EpisodeReentry` means one of those checks was bypassed
        and the right response is a stack trace, not a log line.
        """
        now = int(ts_ms if ts_ms is not None else self.clock())
        if episode_id:
            self.episodes.claim(episode_id, symbol=symbol, ts_ms=now)
            self.queue.remove(episode_id)
        for event in reversed(self.events):
            if event.next_entry_ms is None:
                event.next_entry_ms = now
                event.next_entry_symbol = symbol
                event.qualified_candidate_existed = True
                return event
        return None

    # -- §15: no global cooldown ----------------------------------------- #

    def cooling(self, symbol: str, ts_ms: int | None = None) -> bool:
        """Whether *this* symbol is inside its own anti-churn window.

        Off by default, and never consulted for any symbol other than the one
        asked about.  Closing SOL cannot make this true for ETH — there is no
        code path by which it could, which is the point.
        """
        window = float(self.cfg.symbol_cooldown_seconds)
        if window <= 0.0:
            return False
        last = self._last_close_ms.get(symbol)
        if last is None:
            return False
        now = int(ts_ms if ts_ms is not None else self.clock())
        return now - last < window * 1000.0

    def cooling_symbols(self, ts_ms: int | None = None) -> set[str]:
        now = int(ts_ms if ts_ms is not None else self.clock())
        return {s for s in self._last_close_ms if self.cooling(s, now)}

    def cooldown_rejections(self, ts_ms: int | None = None) -> dict[str, IdleReason]:
        """Cooling symbols in the shape ``attribute_idle`` takes rejections in."""
        return {s: IdleReason.COOLDOWN for s in self.cooling_symbols(ts_ms)}

    # -- what the next entry should consider ------------------------------ #

    def next_candidate(
        self,
        *,
        books: Mapping[str, OrderBook],
        features: Mapping[str, Features],
        fees: Any = None,
        ts_ms: int | None = None,
        exclude: Iterable[str] = (),
    ) -> Opportunity | None:
        """The best queued idea, re-costed, with cooling symbols held back."""
        now = int(ts_ms if ts_ms is not None else self.clock())
        blocked = set(exclude) | self.cooling_symbols(now)
        return self.queue.take(
            books=books, features=features, fees=fees, ts_ms=now, exclude=blocked
        )

    # -- reporting -------------------------------------------------------- #

    @property
    def last(self) -> RecycleEvent | None:
        return self.events[-1] if self.events else None

    def idle_context(
        self, *, ts_ms: int | None = None, blocked_reason: Any = None
    ) -> IdleContext:
        """The context :func:`~app.portfolio.attribute_idle` needs from us."""
        now = int(ts_ms if ts_ms is not None else self.clock())
        last = self.last
        return IdleContext(
            ts_ms=now,
            last_rank_ms=self._last_rank_ms,
            capital_released_ms=last.capital_released_ms if last else 0,
            blocked_reason=blocked_reason,
            state_bug_usd=last.state_bug_usd if last else 0.0,
        )

    def latency(self) -> dict:
        """Close → rescan and close → entry, summarised.

        Medians rather than means: one 30-second outlier from a restart would
        drag a mean far enough to hide an otherwise healthy loop.
        """
        rescans = [e.close_to_rescan_ms for e in self.events]
        entries = [e.close_to_entry_ms for e in self.events if e.redeployed]
        missed = [
            e for e in self.events if not e.redeployed and e.qualified_candidate_existed
        ]
        return {
            "closes": len(self.events),
            "redeployed": len(entries),
            "median_close_to_rescan_ms": _median(rescans),
            "median_close_to_entry_ms": _median([e for e in entries if e is not None]),
            "max_close_to_rescan_ms": max(rescans) if rescans else 0,
            "qualified_but_not_taken": len(missed),
            "queue_size": len(self.queue),
        }

    # -- internals -------------------------------------------------------- #

    def _emit(self, event: RecycleEvent) -> None:
        if self.emit is None:
            return
        self.emit(CAPITAL_RELEASED, event.as_dict())

    def _books_gap(self, ledger: Any, position: Position, state: Any) -> float:
        """Capital a close should have returned and demonstrably did not.

        Two questions, and the worse answer wins.  Is the position still on the
        book after we closed it?  Do cash, exposure and equity still add up?
        Either one means the next entry will be sized against a number that is
        not true, and Phase 6 would rather see that as ``CAPITAL_STATE_BUG``
        than watch it surface later as an inexplicable refusal.
        """
        gap = 0.0
        open_positions = getattr(ledger, "open_positions", None)
        if isinstance(open_positions, Mapping) and position.id in open_positions:
            gap = max(float(position.notional_usd), 0.0)
        if state is not None and state.equity > 0:
            drift = abs(state.cash + state.deployed_usd - state.equity)
            if drift > 1e-6 * max(1.0, state.equity):
                gap = max(gap, drift)
        return gap


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (float(ordered[mid - 1]) + float(ordered[mid])) / 2.0


def _default_settings() -> "Settings":
    from app.config import get_settings

    return get_settings()
