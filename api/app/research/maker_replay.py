"""Queue-aware maker replay — does resting actually beat crossing here?

This is the one structure the project has never honestly tested.  Binance.US
charges **0% maker**, and 57% of V1's gross loss was slippage paid crossing the
spread (``docs/ECONOMICS.md``), so a passive quote that fills is strictly
cheaper than a market order that fills.  The catch is the word *fills*.

A maker order cannot be evaluated on 1-minute OHLCV, because a bar contains no
queue position.  ``high >= our_price`` says the price touched our level.  It
says nothing about whether anyone traded *through* it, or whether the size
already resting there was consumed first.  **Touched is not filled**, and a
backtest that treats them as the same thing is the single most reliable way to
manufacture a maker strategy that evaporates in production.

So this module replays the recorded tape (``app.recorder``: hourly gzip NDJSON
of the raw Binance.US stream) and answers four questions per quote:

1. **Queue ahead** — how much size was already resting at our price when we
   joined?  We are behind all of it.
2. **Trade-through** — did prints actually consume *through* our price, for at
   least the quantity ahead of us plus ours?
3. **Aging** — did we still want to be there by the time it happened, or did the
   quote outlive the signal and get cancelled?
4. **Adverse selection** — when we *did* fill, where was the mid 1, 5 and 30
   seconds later?  Maker fills cluster on the wrong side of information.  If the
   markout is negative, the "free" fee saving is being paid back through the
   price, and calling that a win is the second most reliable way to manufacture
   a maker strategy that evaporates in production.

The queue model is an estimate, and the honest reason is written here rather
than buried:

    **A public feed cannot distinguish a cancellation from a fill ahead of
    us.**  ``depthUpdate`` publishes the new *absolute* size at a price; it does
    not say why the size changed.  We attribute the shrinkage we can explain
    with a matching print to trades, and treat the unexplained remainder as
    cancellations.  Cancellations then have to be split between "in front of us"
    (good for us — our queue position improves) and "behind us" (no effect), and
    that split is genuinely unobservable.  ``cancel_ahead_ratio`` controls it
    and defaults to 0.5.

    **Which way it errs.**  At ``cancel_ahead_ratio=1.0`` every cancellation is
    assumed to be in front of us, which is *optimistic*: it reports more fills
    than reality.  At ``0.0`` no cancellation ever helps us, which is
    *pessimistic*.  The 0.5 default is deliberately mid-range, and
    ``run_execution_study.py`` reports the 0.0 fill rate alongside it as a
    floor, so no conclusion rests on the assumption.

    Two further biases, both optimistic, both deliberate and bounded:
    real queues are not FIFO-pure (iceberg refreshes, self-trade prevention),
    and **our own order is invisible to the public tape** — it never appears in
    the level sizes we replay, so a quote large enough to have moved the book is
    modelled as if it had not.  ``simulate_quote`` never fills more size than
    actually printed, which is what keeps that bias bounded.

Nothing here trades.  Nothing here touches the exchange.  It is a reader.
"""

from __future__ import annotations

import gzip
import json
import logging
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from app.types import (
    BookLevel,
    ClosedTrade,
    ExitReason,
    OrderBook,
    PerfStats,
    Side,
    Trade,
)

log = logging.getLogger("cryptobot.research.maker_replay")

_BPS = 10_000.0

#: Markout horizons, in the order the report prints them.  1s catches the
#: immediate information leak; 30s is roughly where a scalper would be out.
MARKOUT_HORIZONS_MS: dict[str, int] = {"1s": 1_000, "5s": 5_000, "30s": 30_000}

#: The horizon ``MakerOutcome.adverse_selection_bps`` is quoted at.
ADVERSE_SELECTION_HORIZON = "5s"

# --------------------------------------------------------------------------- #
# How much tape is enough tape
# --------------------------------------------------------------------------- #

#: A maker study on less than this is not a result, it is a rumour.  One full
#: day covers the Asia/Europe/US sessions, which on Binance.US differ by more
#: than an order of magnitude in flow.
MIN_TAPE_HOURS = 24.0
#: Per symbol.  Binance.US BTC/USD prints ~3k trades a day; below this there is
#: not enough trade-through to measure a fill rate against.
MIN_PRINTS_PER_SYMBOL = 500
#: Matches the Phase-3 promotion gate: fewer than 30 samples is not a sample.
MIN_QUOTES = 30

_FALLBACK_TICK = {"BTCUSD": 0.01, "ETHUSD": 0.01, "SOLUSD": 0.01, "ADAUSD": 0.0001}
_DEFAULT_TICK = 0.01


# --------------------------------------------------------------------------- #
# Tape
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TapeEvent:
    """One normalised event from the recorded stream.

    ``ts_ms`` is the **receive** time (``"r"`` in the recording), not the
    exchange time, because the gap between them is the latency a maker order
    actually faces and a replay that used exchange time would quietly give
    itself a head start.
    """

    ts_ms: int
    symbol: str
    kind: str  # "depth" | "ticker" | "trade"
    exchange_ms: int = 0
    bids: tuple[tuple[float, float], ...] = ()
    asks: tuple[tuple[float, float], ...] = ()
    price: float = 0.0
    qty: float = 0.0
    buyer_is_maker: bool = False

    @property
    def aggressor(self) -> Side:
        """LONG when a buyer lifted an ask, SHORT when a seller hit a bid."""
        return Side.SHORT if self.buyer_is_maker else Side.LONG

    def as_trade(self) -> Trade:
        return Trade(
            symbol=self.symbol,
            price=self.price,
            qty=self.qty,
            ts_ms=self.ts_ms,
            buyer_is_maker=self.buyer_is_maker,
        )


@dataclass
class TapeSlice:
    """Every event for one symbol, time-ordered, plus what it covers."""

    symbol: str
    events: list[TapeEvent] = field(default_factory=list)
    tick_size: float = _DEFAULT_TICK
    #: Files that ended mid-gzip-member (normally the hour still being written).
    truncated_files: tuple[str, ...] = ()

    @property
    def start_ms(self) -> int:
        return self.events[0].ts_ms if self.events else 0

    @property
    def end_ms(self) -> int:
        return self.events[-1].ts_ms if self.events else 0

    @property
    def seconds(self) -> float:
        return max(0.0, (self.end_ms - self.start_ms) / 1000.0)

    @property
    def hours(self) -> float:
        return self.seconds / 3600.0

    @property
    def prints(self) -> list[TapeEvent]:
        return [e for e in self.events if e.kind == "trade"]

    def counts(self) -> dict[str, int]:
        out = {"depth": 0, "ticker": 0, "trade": 0}
        for e in self.events:
            out[e.kind] = out.get(e.kind, 0) + 1
        return out


@dataclass
class TapeCoverage:
    """Is there enough tape to answer the question, and if not, how much short?

    The point of this object is that "insufficient data" is a *first-class
    result*, not an exception.  Twenty minutes of tape can produce a fill rate.
    It cannot produce a finding.
    """

    hours: float = 0.0
    symbols: dict[str, dict[str, Any]] = field(default_factory=dict)
    files: int = 0
    truncated_files: tuple[str, ...] = ()
    min_hours: float = MIN_TAPE_HOURS
    min_prints: int = MIN_PRINTS_PER_SYMBOL

    @property
    def sufficient(self) -> bool:
        if not self.symbols or self.hours < self.min_hours:
            return False
        return all(v.get("trades", 0) >= self.min_prints for v in self.symbols.values())

    @property
    def shortfall_hours(self) -> float:
        return max(0.0, self.min_hours - self.hours)

    def reasons(self) -> list[str]:
        """Exactly what is missing, and exactly how much more is needed."""
        out: list[str] = []
        if not self.symbols:
            out.append(
                "no tape found — the recorder writes tape-*.ndjson.gz into "
                f"TAPE_DIR; {self.min_hours:.0f}h are needed before any maker "
                "conclusion holds"
            )
            return out
        if self.hours < self.min_hours:
            out.append(
                f"tape spans {self.hours:.2f}h, need {self.min_hours:.0f}h — "
                f"{self.shortfall_hours:.2f}h more, i.e. about "
                f"{self.shortfall_hours:.1f} hours of further recording"
            )
        for sym, v in sorted(self.symbols.items()):
            n = int(v.get("trades", 0))
            if n < self.min_prints:
                out.append(
                    f"{sym}: {n} prints, need {self.min_prints} — {self.min_prints - n} more"
                )
        if self.truncated_files:
            out.append(
                "file(s) still being written, read up to the last complete "
                "block: " + ", ".join(self.truncated_files)
            )
        return out


def iter_tape_messages(
    directory: str | Path,
    *,
    start_ms: int = 0,
    end_ms: int = 0,
    on_truncated: Callable[[str], None] | None = None,
) -> Iterator[tuple[int, Any, str]]:
    """Yield ``(receive_ms, raw_message, filename)`` from a tape directory.

    Format-identical to :func:`app.recorder.replay` — a test pins that, because
    a reader that drifts from its writer produces a dataset nobody can use.
    Two things are added, both for the study's benefit rather than the format's:
    the **filename** each row came from, and an explicit *on_truncated* callback.

    Truncation is the normal case, not an error: the hour currently being
    recorded is an unterminated gzip member, so reading it hits EOF partway —
    and that file holds the freshest data there is.  Both readers keep what they
    read and move on; this one also *reports* which file was short, so the
    coverage summary can say so out loud instead of silently under-counting.
    """
    d = Path(directory)
    if not d.is_dir():
        return
    for path in sorted(d.glob("tape-*.ndjson.gz")):
        try:
            with gzip.open(path, "rt") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    r = int(row.get("r", 0))
                    if start_ms and r < start_ms:
                        continue
                    if end_ms and r > end_ms:
                        return
                    yield r, row.get("m"), path.name
        except (EOFError, OSError) as exc:  # truncated or unreadable member
            log.debug("tape %s ended early: %s", path.name, exc)
            if on_truncated is not None:
                on_truncated(path.name)
            continue


def _decimals(text: str) -> int:
    if "." not in text:
        return 0
    return len(text.split(".", 1)[1].rstrip("0"))


def _levels(raw: Any, precision: list[int]) -> tuple[tuple[float, float], ...]:
    out: list[tuple[float, float]] = []
    for row in raw or []:
        try:
            ptxt, qtxt = str(row[0]), str(row[1])
            price, qty = float(ptxt), float(qtxt)
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if price <= 0 or qty < 0:
            continue
        precision[0] = max(precision[0], _decimals(ptxt))
        out.append((price, qty))
    return tuple(out)


def _int_or(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(fallback)


def parse_message(
    msg: Any, receive_ms: int, precision: dict[str, list[int]] | None = None
) -> TapeEvent | None:
    """Normalise one raw stream message into a :class:`TapeEvent`.

    Mirrors ``app.exchange.ws``'s routing — including the fact that
    ``bookTicker`` frames carry no ``"e"`` field, so the kind has to be inferred
    from the stream name — without importing it.  The feed's job is to keep a
    live book; this module's job is to read history.  Coupling them would make
    every feed change a replay change.

    ``precision`` accumulates the observed price decimals per symbol, which is
    how :func:`load_tape` infers a tick size without a REST ``exchangeInfo``
    call that research runs are not allowed to make.
    """
    if precision is None:
        precision = {}
    if not isinstance(msg, dict) or not msg:
        return None
    stream = msg.get("stream")
    if isinstance(stream, str):
        data = msg.get("data")
    else:
        stream, data = "", msg
    if not isinstance(data, dict) or not data:
        return None

    kind = data.get("e")
    if not isinstance(kind, str) or not kind:
        lowered = (stream or "").lower()
        if "bookticker" in lowered:
            kind = "bookTicker"
        elif "@depth" in lowered:
            kind = "depth20"
        else:
            return None

    symbol = str(data.get("s") or "").upper()
    if not symbol and stream and "@" in stream:
        symbol = stream.split("@", 1)[0].upper()
    if not symbol:
        return None
    prec = precision.setdefault(symbol, [0])

    if kind == "depthUpdate":
        return TapeEvent(
            ts_ms=receive_ms,
            symbol=symbol,
            kind="depth",
            exchange_ms=_int_or(data.get("E"), receive_ms),
            bids=_levels(data.get("b"), prec),
            asks=_levels(data.get("a"), prec),
        )
    if kind == "depth20":
        return TapeEvent(
            ts_ms=receive_ms,
            symbol=symbol,
            kind="depth",
            exchange_ms=receive_ms,
            bids=_levels(data.get("bids"), prec),
            asks=_levels(data.get("asks"), prec),
        )
    if kind == "bookTicker":
        try:
            btxt, bid_qty = str(data["b"]), float(data["B"])
            atxt, ask_qty = str(data["a"]), float(data["A"])
            bid, ask = float(btxt), float(atxt)
        except (KeyError, TypeError, ValueError):
            return None
        if bid <= 0 or ask <= 0 or ask <= bid:
            return None
        prec[0] = max(prec[0], _decimals(btxt), _decimals(atxt))
        return TapeEvent(
            ts_ms=receive_ms,
            symbol=symbol,
            kind="ticker",
            exchange_ms=_int_or(data.get("E"), receive_ms),
            bids=((bid, bid_qty),),
            asks=((ask, ask_qty),),
        )
    if kind == "aggTrade":
        try:
            ptxt = str(data["p"])
            price, qty = float(ptxt), float(data["q"])
        except (KeyError, TypeError, ValueError):
            return None
        if price <= 0 or qty <= 0:
            return None
        prec[0] = max(prec[0], _decimals(ptxt))
        return TapeEvent(
            ts_ms=receive_ms,
            symbol=symbol,
            kind="trade",
            exchange_ms=_int_or(data.get("T"), receive_ms),
            price=price,
            qty=qty,
            buyer_is_maker=bool(data.get("m")),
        )
    return None


def load_tape(
    directory: str | Path,
    *,
    symbols: Sequence[str] | None = None,
    start_ms: int = 0,
    end_ms: int = 0,
) -> dict[str, TapeSlice]:
    """Read a tape directory into one :class:`TapeSlice` per symbol."""
    wanted = {s.upper() for s in symbols} if symbols else None
    precision: dict[str, list[int]] = {}
    slices: dict[str, TapeSlice] = {}
    truncated: list[str] = []
    files: set[str] = set()

    for receive_ms, msg, fname in iter_tape_messages(
        directory, start_ms=start_ms, end_ms=end_ms, on_truncated=truncated.append
    ):
        files.add(fname)
        ev = parse_message(msg, receive_ms, precision)
        if ev is None:
            continue
        if wanted is not None and ev.symbol not in wanted:
            continue
        sl = slices.get(ev.symbol)
        if sl is None:
            sl = slices[ev.symbol] = TapeSlice(symbol=ev.symbol)
        sl.events.append(ev)

    frozen = tuple(sorted(set(truncated)))
    for sym, sl in slices.items():
        sl.events.sort(key=lambda e: e.ts_ms)
        digits = precision.get(sym, [0])[0]
        sl.tick_size = (
            round(10.0 ** (-digits), digits + 2)
            if digits > 0
            else _FALLBACK_TICK.get(sym, _DEFAULT_TICK)
        )
        sl.truncated_files = frozen
    return slices


def tape_coverage(
    slices: dict[str, TapeSlice],
    *,
    min_hours: float = MIN_TAPE_HOURS,
    min_prints: int = MIN_PRINTS_PER_SYMBOL,
) -> TapeCoverage:
    """Summarise what the tape actually contains, honestly."""
    cov = TapeCoverage(min_hours=min_hours, min_prints=min_prints)
    if not slices:
        return cov
    starts = [s.start_ms for s in slices.values() if s.events]
    ends = [s.end_ms for s in slices.values() if s.events]
    if starts and ends:
        cov.hours = max(0.0, (max(ends) - min(starts)) / 3_600_000.0)
    for sym, sl in sorted(slices.items()):
        counts = sl.counts()
        cov.symbols[sym] = {
            "events": len(sl.events),
            "trades": counts.get("trade", 0),
            "depth": counts.get("depth", 0),
            "ticker": counts.get("ticker", 0),
            "hours": round(sl.hours, 4),
            "tick_size": sl.tick_size,
        }
        cov.truncated_files = sl.truncated_files or cov.truncated_files
    return cov


# --------------------------------------------------------------------------- #
# A book we can rebuild from the tape alone
# --------------------------------------------------------------------------- #


class ReplayBook:
    """Top-of-book plus whatever depth the tape has shown us.

    Deliberately *not* ``app.exchange.book.BookManager``: that class implements
    the full Binance diff protocol and refuses to run without a REST snapshot,
    which is exactly right for live trading and exactly wrong here — the tape
    contains no snapshot, only the diffs and quotes that followed one.  So this
    is a level map seeded from the stream itself.

    The consequence is stated rather than hidden: **a price level that has not
    been mentioned since replay began has unknown size and reads as zero.**
    Every quote therefore needs a warm-up window before it, which is why the
    probe generators skip the first seconds of tape — and why an unseeded level
    makes the queue model *optimistic*, never pessimistic.
    """

    def __init__(self, symbol: str = "") -> None:
        self.symbol = symbol
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.last_ms = 0
        self.updates = 0

    def apply(self, ev: TapeEvent) -> None:
        if ev.kind == "depth":
            _merge(self.bids, ev.bids)
            _merge(self.asks, ev.asks)
        elif ev.kind == "ticker":
            bid, bid_qty = ev.bids[0]
            ask, ask_qty = ev.asks[0]
            # Levels on the wrong side of the new touch are history, not depth;
            # keeping them would fabricate a crossed book.
            self.bids = {p: q for p, q in self.bids.items() if p < bid}
            self.asks = {p: q for p, q in self.asks.items() if p > ask}
            self.bids[bid] = bid_qty
            self.asks[ask] = ask_qty
        else:
            return
        self.last_ms = ev.ts_ms
        self.updates += 1

    # -- queries ------------------------------------------------------- #

    @property
    def best_bid(self) -> float:
        return max(self.bids) if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return min(self.asks) if self.asks else 0.0

    @property
    def mid(self) -> float:
        b, a = self.best_bid, self.best_ask
        if b <= 0 or a <= 0 or a <= b:
            return 0.0
        return (a + b) / 2.0

    @property
    def spread_bps(self) -> float:
        m = self.mid
        if m <= 0:
            return float("inf")
        return (self.best_ask - self.best_bid) / m * _BPS

    def qty_at(self, side: Side, price: float, *, tick: float = 0.0) -> float:
        """Resting size at *price* on our side of the book (0 if unseen)."""
        levels = self.bids if side is Side.LONG else self.asks
        got = levels.get(price)
        if got is not None:
            return got
        tol = _tol(price, tick)
        for p, q in levels.items():
            if abs(p - price) <= tol:
                return q
        return 0.0

    def snapshot(self, *, max_levels: int = 50) -> OrderBook:
        book = OrderBook(symbol=self.symbol, last_update_ms=self.last_ms)
        book.bids = [
            BookLevel(p, q)
            for p, q in sorted(self.bids.items(), key=lambda kv: -kv[0])[:max_levels]
            if q > 0
        ]
        book.asks = [
            BookLevel(p, q)
            for p, q in sorted(self.asks.items(), key=lambda kv: kv[0])[:max_levels]
            if q > 0
        ]
        return book

    def copy(self) -> "ReplayBook":
        out = ReplayBook(self.symbol)
        out.bids = dict(self.bids)
        out.asks = dict(self.asks)
        out.last_ms = self.last_ms
        out.updates = self.updates
        return out


def _merge(side: dict[float, float], levels: Iterable[tuple[float, float]]) -> None:
    """Binance diff semantics: the quantity is absolute, and 0 deletes."""
    for price, qty in levels:
        if qty <= 0:
            side.pop(price, None)
        else:
            side[price] = qty


def _tol(price: float, tick: float) -> float:
    """Price-equality tolerance: half a tick, never below float noise."""
    if tick > 0:
        return tick / 2.0
    return max(1e-9, abs(price) * 1e-9)


# --------------------------------------------------------------------------- #
# Outcomes
# --------------------------------------------------------------------------- #


@dataclass
class MakerOutcome:
    """What happened to one resting quote.

    Two sign conventions live here and both are deliberate:

    * ``markout_bps`` is signed **in our favour** — positive means the mid moved
      our way after the fill.  This is the one the report prints, because
      "negative markout is bad" is the sentence a reader needs.
    * ``adverse_selection_bps`` is signed **against us** — positive means we
      were adversely selected.  The frozen contract names this field, and its
      name only makes sense with that sign.

    They are negations of each other at :data:`ADVERSE_SELECTION_HORIZON`.

    ``missed_reason`` is set on a *partial* fill too (``"partial_fill"``): it
    explains why the remaining size did not fill, which is exactly the question
    a partial raises.
    """

    # -- frozen contract (docs/PHASE3_SPEC.md) -------------------------- #
    filled: bool
    fill_price: float
    fill_qty: float
    wait_ms: int
    queue_ahead_usd: float
    missed_reason: str | None
    adverse_selection_bps: float

    # -- detail the report needs ---------------------------------------- #
    symbol: str = ""
    side: Side = Side.LONG
    quote_price: float = 0.0
    requested_qty: float = 0.0
    fill_ms: int = 0
    markout_bps: dict[str, float] = field(default_factory=dict)
    #: Queue size (USD) at the moment we joined, before any decay.
    queue_ahead_start_usd: float = 0.0
    #: Did price reach our level at all?  Touched is not filled.
    touched: bool = False
    #: Notional that printed at or through our price during the quote's life.
    traded_through_usd: float = 0.0
    fee_usd: float = 0.0
    #: False when the tape ran out before the quote's fate was decided.  Such a
    #: quote is not a miss, it is an unknown, and counting it as a miss would
    #: deflate the fill rate with the recorder's start time.
    resolved: bool = True

    @property
    def partial(self) -> bool:
        return self.filled and self.fill_qty < self.requested_qty - 1e-12

    def markout(self, horizon: str) -> float | None:
        """Markout at *horizon*, or ``None`` when the tape never reached it."""
        return self.markout_bps.get(horizon)


@dataclass
class ExecutionComparison:
    """TAKER vs MAKER vs HYBRID over the same signals."""

    # -- frozen contract ------------------------------------------------ #
    taker: PerfStats
    maker: PerfStats
    hybrid: PerfStats
    maker_fill_rate: float
    hybrid_cross_rate: float
    markout_bps: dict[str, float]

    # -- detail --------------------------------------------------------- #
    signals: int = 0
    maker_fills: int = 0
    maker_partials: int = 0
    maker_unresolved: int = 0
    hybrid_crosses: int = 0
    hybrid_maker_fills: int = 0
    markout_samples: dict[str, int] = field(default_factory=dict)
    miss_reasons: dict[str, int] = field(default_factory=dict)
    #: NET dollars the taker made on signals the maker never filled.  Positive
    #: means resting cost us money by not trading; negative means it saved us.
    maker_opportunity_cost_usd: float = 0.0
    mean_queue_ahead_usd: float = 0.0
    mean_wait_ms: float = 0.0
    insufficient_data: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def maker_net_after_opportunity_cost(self) -> float:
        """Maker NET with the missed moves charged against it.

        A maker arm that skipped every loser looks brilliant until you notice it
        also skipped every winner.  This is the line that stops that.
        """
        return self.maker.net_pnl - self.maker_opportunity_cost_usd

    def verdict(self) -> str:
        """One sentence, written so the trap is readable rather than inferred.

        A high fill rate with negative markout is a *worse* outcome than a low
        fill rate with flat markout: it means someone better informed is handing
        us inventory, and the 0% maker fee is being paid back through the price.
        """
        if self.insufficient_data:
            return "INSUFFICIENT DATA — no maker/taker verdict can be drawn yet."
        if not self.signals or self.maker_fills == 0:
            return "MAKER NEVER FILLED — resting produced no trades on this tape."
        ref = self.markout_bps.get(ADVERSE_SELECTION_HORIZON)
        if ref is None:
            return (
                f"fill rate {self.maker_fill_rate:.1%}, markout unmeasurable — "
                "the tape ends before the horizons."
            )
        if ref < -0.5:
            return (
                f"ADVERSE SELECTION: fill rate {self.maker_fill_rate:.1%} but "
                f"{ADVERSE_SELECTION_HORIZON} markout {ref:+.2f} bps — the fills "
                "are on the wrong side of information and the 0% maker fee is "
                "being paid back through the price. A LOWER fill rate with flat "
                "markout would be the better outcome."
            )
        if ref > 0.5:
            return (
                f"FAVOURABLE: fill rate {self.maker_fill_rate:.1%} with "
                f"{ADVERSE_SELECTION_HORIZON} markout {ref:+.2f} bps in our favour."
            )
        return (
            f"FLAT: fill rate {self.maker_fill_rate:.1%}, "
            f"{ADVERSE_SELECTION_HORIZON} markout {ref:+.2f} bps — no measurable "
            "adverse selection, so the fee saving is real as far as this tape shows."
        )

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["maker_net_after_opportunity_cost"] = self.maker_net_after_opportunity_cost
        out["verdict"] = self.verdict()
        return out


# --------------------------------------------------------------------------- #
# Signals
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class QuoteSignal:
    """One decision to get into a position, stripped of execution style.

    ``exit_ms``/``exit_price`` are held fixed across all three arms on purpose:
    this module compares *entry execution*, and letting the exit float too would
    mix two questions whose answers can have opposite signs.
    """

    symbol: str
    side: Side
    ts_ms: int
    qty: float
    exit_ms: int
    exit_price: float
    taker_entry_price: float
    expected_net_edge_bps: float = 0.0
    strategy: str = "probe"
    regime: str = "unknown"


def signals_from_trades(trades: Sequence[ClosedTrade]) -> list[QuoteSignal]:
    """Turn an arm's realised taker trades back into execution-free signals."""
    out: list[QuoteSignal] = []
    for t in trades:
        if t.qty <= 0 or t.entry_price <= 0:
            continue
        out.append(
            QuoteSignal(
                symbol=t.symbol,
                side=t.side,
                ts_ms=int(t.entry_ms),
                qty=float(t.qty),
                exit_ms=int(t.exit_ms),
                exit_price=float(t.exit_price),
                taker_entry_price=float(t.entry_price),
                expected_net_edge_bps=float(t.expected_net_edge_bps),
                strategy=t.strategy,
                regime=t.regime,
            )
        )
    out.sort(key=lambda s: s.ts_ms)
    return out


# --------------------------------------------------------------------------- #
# The replay
# --------------------------------------------------------------------------- #


class MakerReplay:
    """Replays resting quotes against the recorded tape."""

    version = "maker_replay/1"

    def __init__(
        self,
        *,
        settings: Any = None,
        tape_dir: str | Path | None = None,
        cancel_ahead_ratio: float = 0.5,
        markout_horizons: dict[str, int] | None = None,
    ) -> None:
        if settings is None:
            from app.config import get_settings

            settings = get_settings()
        self.settings = settings
        self.fees = settings.fees
        self.tape_dir = Path(tape_dir) if tape_dir else Path(settings.recorder.directory)
        # See the module docstring: 1.0 is optimistic, 0.0 pessimistic.
        self.cancel_ahead_ratio = min(1.0, max(0.0, float(cancel_ahead_ratio)))
        self.markout_horizons = dict(markout_horizons or MARKOUT_HORIZONS_MS)

    # ------------------------------------------------------------------ #
    # Tape access
    # ------------------------------------------------------------------ #

    def load(
        self,
        *,
        symbols: Sequence[str] | None = None,
        start_ms: int = 0,
        end_ms: int = 0,
        tape_dir: str | Path | None = None,
    ) -> dict[str, TapeSlice]:
        return load_tape(
            tape_dir or self.tape_dir, symbols=symbols, start_ms=start_ms, end_ms=end_ms
        )

    # ------------------------------------------------------------------ #
    # Quote pricing
    # ------------------------------------------------------------------ #

    @staticmethod
    def quote_price(
        side: Side, book: "OrderBook | ReplayBook", offset_ticks: int, tick: float
    ) -> float:
        """Where a post-only quote of *offset_ticks* would sit.

        Offset 0 joins the touch, 1 rests one tick behind it, and so on.
        Behind, always: this never improves past the touch into the spread,
        because quoting inside the spread is a different (and much more
        adversely selected) strategy than the one being tested.
        """
        bid, ask = book.best_bid, book.best_ask
        if bid <= 0 or ask <= 0:
            return 0.0
        step = max(0, int(offset_ticks)) * max(tick, 0.0)
        price = bid - step if side is Side.LONG else ask + step
        return _round_tick(price, tick)

    # ------------------------------------------------------------------ #
    # The core: one resting quote
    # ------------------------------------------------------------------ #

    def simulate_quote(
        self,
        *,
        symbol: str,
        side: Side,
        price: float,
        qty: float,
        from_ms: int,
        timeout_ms: int,
        book_state: "OrderBook | ReplayBook | None",
        tape: "TapeSlice | Sequence[TapeEvent] | Sequence[Trade] | None",
    ) -> MakerOutcome:
        """Rest a post-only quote at *price* and see what the tape does to it.

        The model, in the order it is applied:

        * **Post-only.**  A quote that would cross the spread at ``from_ms`` is
          rejected (``missed_reason="would_cross"``) and never silently becomes
          a taker.  Re-costing it as a taker is the caller's decision and has to
          clear the NET gate again — see :meth:`compare`'s HYBRID arm.
        * **Queue ahead.**  Whatever rests at our price in ``book_state`` is in
          front of us.  It decays two ways: prints at the level consume it from
          the front, and unexplained shrinkage in the published level size is
          attributed to cancellations, of which ``cancel_ahead_ratio`` are
          assumed to have been ahead of us.  A public feed cannot tell a
          cancellation from a fill ahead of us, so this is an **estimate**; the
          module docstring says which way each setting errs.
        * **Trade-through.**  Only prints whose aggressor is on the opposite
          side count: a resting bid is filled by sellers, not by buyers.  A
          print *at* our price consumes the queue first and only then us; a
          print *through* our price (below our bid, above our ask) means the
          level was swept, so the queue is gone.  Price merely *touching* the
          level fills nothing — ``touched`` records that separately, and it is
          the whole difference between this and a naive maker backtest.
        * **Partial fills** are first-class: we fill exactly the size that
          printed through after the queue, never more.
        * **Aging.**  Past ``from_ms + timeout_ms`` the quote is cancelled and
          reports ``missed_reason="timeout"``.
        * **Markout.**  On a fill, the mid at each horizon after the fill is
          compared with the fill price.  A horizon the tape does not reach is
          omitted from ``markout_bps`` rather than reported as a confident zero.
        """
        events = _as_events(tape, symbol)
        requested = float(qty)
        out = MakerOutcome(
            filled=False,
            fill_price=0.0,
            fill_qty=0.0,
            wait_ms=0,
            queue_ahead_usd=0.0,
            missed_reason=None,
            adverse_selection_bps=0.0,
            symbol=symbol,
            side=side,
            quote_price=float(price),
            requested_qty=requested,
        )
        if requested <= 0 or price <= 0:
            out.missed_reason = "invalid_quote"
            return out

        tick = _tick_of(tape)

        # -- post-only -------------------------------------------------- #
        if book_state is not None:
            bid, ask = book_state.best_bid, book_state.best_ask
            crosses = (side is Side.LONG and ask > 0 and price >= ask - _tol(price, tick)) or (
                side is Side.SHORT and bid > 0 and price <= bid + _tol(price, tick)
            )
            if crosses:
                out.missed_reason = "would_cross"
                return out

        # -- the queue we are joining behind ---------------------------- #
        queue_ahead = _qty_at(book_state, side, price, tick) if book_state is not None else 0.0
        level_size = queue_ahead
        out.queue_ahead_start_usd = queue_ahead * price
        out.queue_ahead_usd = queue_ahead * price

        deadline = int(from_ms) + max(0, int(timeout_ms))
        remaining = requested
        filled = 0.0
        fill_ms = 0
        traded_through = 0.0
        touched = False
        aged_out = False

        # Book carried forward for markout, seeded from the quote-time book so a
        # fill in the first milliseconds still has a mid to measure against.
        book = ReplayBook(symbol)
        if book_state is not None:
            _seed(book, book_state)

        max_horizon = max(self.markout_horizons.values()) if self.markout_horizons else 0
        markout_pending: list[tuple[str, int]] = []
        markout_armed = False
        markouts: dict[str, float] = {}

        for ev in events[_index_at(events, int(from_ms)) :]:
            if ev.symbol and ev.symbol != symbol:
                continue
            past_deadline = ev.ts_ms > deadline
            if past_deadline and filled <= 0:
                aged_out = True
                break

            live = not past_deadline and remaining > 1e-12
            if ev.kind in ("depth", "ticker"):
                book.apply(ev)
                if live:
                    new_size = _event_level_qty(ev, side, price, tick)
                    if new_size is not None:
                        # Shrinkage we could not explain with a print is a
                        # cancellation.  We cannot see whose.
                        cancelled = max(0.0, level_size - new_size)
                        queue_ahead = max(
                            0.0, queue_ahead - self.cancel_ahead_ratio * cancelled
                        )
                        queue_ahead = min(queue_ahead, new_size)
                        level_size = new_size
                        out.queue_ahead_usd = queue_ahead * price
            elif ev.kind == "trade" and live:
                rel = _relation(side, ev.price, price, tick)
                # Only the opposite aggressor can lift a resting quote: a bid is
                # hit by sellers, an ask is lifted by buyers.
                if rel != "away" and ev.aggressor is not side:
                    touched = True
                    traded_through += ev.price * ev.qty
                    available = ev.qty
                    if rel == "through":
                        # The book traded past our level, so everything resting
                        # at it — the queue and us — was consumed on the way.
                        queue_ahead = 0.0
                        level_size = 0.0
                    else:  # "at"
                        eaten = min(queue_ahead, available)
                        queue_ahead -= eaten
                        available -= eaten
                        level_size = max(0.0, level_size - ev.qty)
                    out.queue_ahead_usd = queue_ahead * price
                    if available > 0 and queue_ahead <= 1e-12:
                        take = min(remaining, available)
                        if take > 0:
                            filled += take
                            remaining -= take
                            fill_ms = ev.ts_ms

            if filled > 0 and not markout_armed:
                markout_armed = True
                markout_pending = sorted(
                    (label, fill_ms + ms) for label, ms in self.markout_horizons.items()
                )
            if markout_pending:
                mid = book.mid
                still: list[tuple[str, int]] = []
                for label, at in markout_pending:
                    if ev.ts_ms >= at:
                        if mid > 0:
                            markouts[label] = mid
                    else:
                        still.append((label, at))
                markout_pending = still
            if (
                markout_armed
                and not markout_pending
                and remaining <= 1e-12
                and ev.ts_ms >= fill_ms + max_horizon
            ):
                break

        out.touched = touched
        out.traded_through_usd = traded_through
        out.fill_qty = filled
        out.queue_ahead_usd = queue_ahead * price

        if filled > 0:
            out.filled = True
            out.fill_price = float(price)
            out.fill_ms = fill_ms
            out.wait_ms = max(0, fill_ms - int(from_ms))
            out.fee_usd = price * filled * self.fees.maker_bps(symbol) / _BPS
            out.markout_bps = {
                label: _markout_bps(side, price, mid) for label, mid in markouts.items()
            }
            ref = out.markout_bps.get(ADVERSE_SELECTION_HORIZON)
            out.adverse_selection_bps = -ref if ref is not None else 0.0
            if out.partial:
                out.missed_reason = "partial_fill"
            return out

        # -- no fill ---------------------------------------------------- #
        last_ms = events[-1].ts_ms if events else int(from_ms)
        if aged_out or last_ms >= deadline:
            out.missed_reason = "timeout"
            out.wait_ms = max(0, int(timeout_ms))
            out.resolved = True
        else:
            out.missed_reason = "tape_exhausted"
            out.wait_ms = max(0, last_ms - int(from_ms))
            out.resolved = False
        return out

    # ------------------------------------------------------------------ #
    # Three execution styles over the same signals
    # ------------------------------------------------------------------ #

    def compare(
        self,
        arm: Any,
        dataset: Any = None,
        *,
        tape: Any,
        offset_ticks: int = 1,
        timeout_ms: int = 20_000,
        signals: Sequence[QuoteSignal] | None = None,
    ) -> ExecutionComparison:
        """Run TAKER, MAKER and HYBRID over one arm's signals.

        * **TAKER** — cross immediately.  Today's behaviour, taken straight from
          the arm's realised trades where they exist, so the baseline cannot
          drift away from the tournament's own number.
        * **MAKER** — post-only at ``offset_ticks`` behind the touch, never
          crossing.  An unfilled signal simply does not trade, and the NET the
          taker made on it is charged to
          :attr:`ExecutionComparison.maker_opportunity_cost_usd`.  Skipping the
          losers is only an achievement if you did not also skip the winners.
        * **HYBRID** — quote first; on a miss, recompute the expected NET edge
          as a taker using only what was knowable at the timeout (the original
          expected edge, minus the part of the move already consumed while we
          waited, minus any spread widening) and cross **only** if it still
          clears ``settings.risk.min_net_edge_bps``.

        ``arm`` is duck-typed on ``.trades``: any object carrying a list of
        :class:`~app.types.ClosedTrade` works, so this runs against a tournament
        ``ArmResult`` without importing ``tournament.py``.  ``dataset`` is
        accepted for contract compatibility and is not required — the tape is
        the source of truth about execution, and the bars cannot second it.
        """
        slices = _as_slices(tape)
        sigs = list(signals) if signals is not None else _signals_of(arm)
        realised = _realised_index(arm)
        sigs = [s for s in sigs if s.symbol in slices]
        sigs.sort(key=lambda s: s.ts_ms)

        cmp = ExecutionComparison(
            taker=PerfStats(),
            maker=PerfStats(),
            hybrid=PerfStats(),
            maker_fill_rate=0.0,
            hybrid_cross_rate=0.0,
            markout_bps={},
            signals=len(sigs),
        )
        if not sigs:
            cmp.insufficient_data = True
            cmp.notes.append("no signals fell inside the recorded tape window")
            return cmp

        taker_trades: list[ClosedTrade] = []
        maker_trades: list[ClosedTrade] = []
        hybrid_trades: list[ClosedTrade] = []
        markout_sums: dict[str, float] = {}
        markout_counts: dict[str, int] = {}
        waits: list[int] = []
        queues: list[float] = []
        gate = float(getattr(self.settings.risk, "min_net_edge_bps", 1.0))

        books = _books_at(slices, sigs)
        for sig, book in zip(sigs, books):
            sl = slices[sig.symbol]
            tick = sl.tick_size
            quote = self.quote_price(sig.side, book, offset_ticks, tick)

            taker = realised.get((sig.symbol, sig.ts_ms)) or _trade_from(
                sig, sig.taker_entry_price, sig.qty, self.fees, maker=False
            )
            taker_trades.append(taker)

            outcome = self.simulate_quote(
                symbol=sig.symbol,
                side=sig.side,
                price=quote,
                qty=sig.qty,
                from_ms=sig.ts_ms,
                timeout_ms=timeout_ms,
                book_state=book,
                tape=sl,
            )
            queues.append(outcome.queue_ahead_start_usd)

            if outcome.filled:
                cmp.maker_fills += 1
                if outcome.partial:
                    cmp.maker_partials += 1
                waits.append(outcome.wait_ms)
                mk = _trade_from(
                    sig, outcome.fill_price, outcome.fill_qty, self.fees, maker=True
                )
                maker_trades.append(mk)
                hybrid_trades.append(mk)
                cmp.hybrid_maker_fills += 1
                for label, bps in outcome.markout_bps.items():
                    markout_sums[label] = markout_sums.get(label, 0.0) + bps
                    markout_counts[label] = markout_counts.get(label, 0) + 1
                continue

            reason = outcome.missed_reason or "unknown"
            cmp.miss_reasons[reason] = cmp.miss_reasons.get(reason, 0) + 1
            if not outcome.resolved:
                cmp.maker_unresolved += 1
                continue

            # The move we did not take is a real cost of resting.
            cmp.maker_opportunity_cost_usd += taker.net_pnl_usd

            # HYBRID: does crossing still clear the NET gate?
            cross_book = _book_at(sl, sig.ts_ms + timeout_ms, seed=book)
            cross_price = self._recheck_taker(sig, book, cross_book, gate)
            if cross_price is not None:
                cmp.hybrid_crosses += 1
                hybrid_trades.append(
                    _trade_from(sig, cross_price, sig.qty, self.fees, maker=False)
                )

        resolved = len(sigs) - cmp.maker_unresolved
        cmp.maker_fill_rate = (cmp.maker_fills / resolved) if resolved > 0 else 0.0
        cmp.hybrid_cross_rate = cmp.hybrid_crosses / len(sigs)
        cmp.markout_bps = {
            label: markout_sums[label] / markout_counts[label]
            for label in sorted(markout_sums, key=lambda k: self.markout_horizons.get(k, 0))
            if markout_counts.get(label)
        }
        cmp.markout_samples = dict(markout_counts)
        cmp.mean_wait_ms = sum(waits) / len(waits) if waits else 0.0
        cmp.mean_queue_ahead_usd = sum(queues) / len(queues) if queues else 0.0
        cmp.taker = perf_stats(taker_trades)
        cmp.maker = perf_stats(maker_trades)
        cmp.hybrid = perf_stats(hybrid_trades)

        if len(sigs) < MIN_QUOTES:
            cmp.insufficient_data = True
            cmp.notes.append(
                f"{len(sigs)} signals — the Phase-3 gate needs {MIN_QUOTES}; "
                "these numbers are a machinery check, not a finding"
            )
        if cmp.maker_unresolved:
            cmp.notes.append(
                f"{cmp.maker_unresolved} quotes were still open when the tape "
                "ended; they are excluded from the fill-rate denominator rather "
                "than counted as misses"
            )
        if not cmp.markout_bps:
            cmp.notes.append(
                "no markout horizon was reached — the tape ends too close to the "
                "fills to measure adverse selection"
            )
        return cmp

    # ------------------------------------------------------------------ #

    def _recheck_taker(
        self,
        sig: QuoteSignal,
        quote_book: "OrderBook | ReplayBook",
        cross_book: ReplayBook,
        gate_bps: float,
    ) -> float | None:
        """Would crossing still clear the NET gate, judged only on the past?

        No leakage: the exit price is never consulted.  We start from the edge
        the signal expected, subtract the part of the move that already happened
        while we waited (edge consumed is edge gone), subtract any widening of
        the spread, and compare what is left against the gate.  Returns the
        price we would cross at, or ``None`` for "do not cross".
        """
        quote_mid = quote_book.mid
        if cross_book.mid <= 0 or quote_mid <= 0:
            return None
        drift = _markout_bps(sig.side, quote_mid, cross_book.mid)
        spread_now = cross_book.spread_bps
        if not math.isfinite(spread_now):
            return None
        spread_then = quote_book.spread_bps
        if not math.isfinite(spread_then):
            spread_then = spread_now
        widening = max(0.0, spread_now - spread_then)
        remaining = sig.expected_net_edge_bps - max(0.0, drift) - widening
        if remaining < gate_bps:
            return None
        price = cross_book.best_ask if sig.side is Side.LONG else cross_book.best_bid
        return price if price > 0 else None

    # ------------------------------------------------------------------ #
    # Study helpers (used by scripts/run_execution_study.py)
    # ------------------------------------------------------------------ #

    def probe_signals(
        self,
        sl: TapeSlice,
        *,
        cadence_ms: int = 60_000,
        hold_ms: int = 300_000,
        notional_usd: float = 50.0,
        warmup_ms: int = 30_000,
        limit: int = 500,
        require_exit: bool = True,
    ) -> list[QuoteSignal]:
        """Evenly spaced alternating quotes — a *probe*, not a strategy.

        No edge is claimed here and none is implied.  The point is to exercise
        the same execution machinery a real arm would, so the study can report
        fill rates and markout before ``tournament.py`` has produced an arm.
        Sides alternate so the measurement cannot inherit a directional drift in
        the tape, and a probe whose exit horizon falls past the end of the tape
        is dropped rather than marked to the last price we happen to have.
        """
        if not sl.events:
            return []
        book = ReplayBook(sl.symbol)
        next_ms = sl.start_ms + max(0, warmup_ms)
        raw: list[QuoteSignal] = []
        idx = 0
        for ev in sl.events:
            if ev.kind in ("depth", "ticker"):
                book.apply(ev)
            if ev.ts_ms >= next_ms and len(raw) < limit:
                side = Side.LONG if idx % 2 == 0 else Side.SHORT
                price = book.best_ask if side is Side.LONG else book.best_bid
                if book.mid > 0 and price > 0:
                    raw.append(
                        QuoteSignal(
                            symbol=sl.symbol,
                            side=side,
                            ts_ms=ev.ts_ms,
                            qty=notional_usd / price,
                            exit_ms=ev.ts_ms + max(1, hold_ms),
                            exit_price=0.0,  # resolved below
                            taker_entry_price=price,
                        )
                    )
                    idx += 1
                    next_ms = ev.ts_ms + max(1, cadence_ms)
        if not require_exit:
            return raw

        mids = _mids_at(sl, [s.exit_ms for s in raw])
        return [
            QuoteSignal(
                symbol=s.symbol,
                side=s.side,
                ts_ms=s.ts_ms,
                qty=s.qty,
                exit_ms=s.exit_ms,
                exit_price=mid,
                taker_entry_price=s.taker_entry_price,
            )
            for s, mid in zip(raw, mids)
            if mid > 0
        ]

    def quote_grid(
        self,
        sl: TapeSlice,
        *,
        timeouts_ms: Sequence[int],
        offsets: Sequence[int],
        cadence_ms: int = 30_000,
        notional_usd: float = 50.0,
        warmup_ms: int = 30_000,
        limit: int = 400,
    ) -> list[dict[str, Any]]:
        """Fill rate, wait and markout for each (timeout, offset) pair.

        This is the diagnostic that answers "does resting fill here?" without
        needing a strategy at all: it is purely about whether a passive quote on
        this venue gets hit, how long it waits, and what the price does to us
        immediately afterwards.
        """
        probes = self.probe_signals(
            sl,
            cadence_ms=cadence_ms,
            hold_ms=cadence_ms,
            notional_usd=notional_usd,
            warmup_ms=warmup_ms,
            limit=limit,
            require_exit=False,
        )
        books = _books_at({sl.symbol: sl}, probes)
        rows: list[dict[str, Any]] = []
        for timeout in timeouts_ms:
            for offset in offsets:
                fills = unresolved = touched = 0
                waits: list[int] = []
                queues: list[float] = []
                m_sum: dict[str, float] = {}
                m_n: dict[str, int] = {}
                reasons: dict[str, int] = {}
                for sig, book in zip(probes, books):
                    price = self.quote_price(sig.side, book, offset, sl.tick_size)
                    o = self.simulate_quote(
                        symbol=sl.symbol,
                        side=sig.side,
                        price=price,
                        qty=sig.qty,
                        from_ms=sig.ts_ms,
                        timeout_ms=timeout,
                        book_state=book,
                        tape=sl,
                    )
                    queues.append(o.queue_ahead_start_usd)
                    touched += 1 if o.touched else 0
                    if o.filled:
                        fills += 1
                        waits.append(o.wait_ms)
                        for label, bps in o.markout_bps.items():
                            m_sum[label] = m_sum.get(label, 0.0) + bps
                            m_n[label] = m_n.get(label, 0) + 1
                    else:
                        unresolved += 0 if o.resolved else 1
                        r = o.missed_reason or "unknown"
                        reasons[r] = reasons.get(r, 0) + 1
                resolved = len(probes) - unresolved
                rows.append(
                    {
                        "symbol": sl.symbol,
                        "timeout_ms": int(timeout),
                        "offset_ticks": int(offset),
                        "quotes": len(probes),
                        "resolved": resolved,
                        "unresolved": unresolved,
                        "fills": fills,
                        "touched": touched,
                        "fill_rate": (fills / resolved) if resolved > 0 else 0.0,
                        "touch_rate": (touched / len(probes)) if probes else 0.0,
                        "mean_wait_ms": (sum(waits) / len(waits)) if waits else 0.0,
                        "mean_queue_ahead_usd": (
                            sum(queues) / len(queues) if queues else 0.0
                        ),
                        "markout_bps": {k: m_sum[k] / m_n[k] for k in m_sum if m_n.get(k)},
                        "markout_samples": dict(m_n),
                        "miss_reasons": reasons,
                    }
                )
        return rows


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _round_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return price
    return round(round(price / tick) * tick, 12)


def _relation(side: Side, print_price: float, our_price: float, tick: float) -> str:
    """Where a print sits relative to our resting quote.

    For a bid: a print above us never reached us ("away"); a print at our price
    works the queue ("at"); a print below us means the level was swept
    ("through").  Mirrored for an ask.
    """
    if abs(print_price - our_price) <= _tol(our_price, tick):
        return "at"
    if side is Side.LONG:
        return "through" if print_price < our_price else "away"
    return "through" if print_price > our_price else "away"


def _markout_bps(side: Side, reference: float, later: float) -> float:
    """Signed **in our favour**: positive means the market moved our way."""
    if reference <= 0 or later <= 0:
        return 0.0
    raw = (later - reference) / reference * _BPS
    return raw if side is Side.LONG else -raw


def _event_level_qty(ev: TapeEvent, side: Side, price: float, tick: float) -> float | None:
    """New absolute size at our price in this event, or ``None`` if unmentioned.

    ``None`` means "this event says nothing about our level", which is not the
    same as "our level is empty" — inventing a zero there would wipe the queue
    every time the touch moved.
    """
    levels = ev.bids if side is Side.LONG else ev.asks
    tol = _tol(price, tick)
    for p, q in levels:
        if abs(p - price) <= tol:
            return q
    return None


def _qty_at(book: "OrderBook | ReplayBook", side: Side, price: float, tick: float) -> float:
    if isinstance(book, ReplayBook):
        return book.qty_at(side, price, tick=tick)
    levels = book.bids if side is Side.LONG else book.asks
    tol = _tol(price, tick)
    for lv in levels:
        if abs(lv.price - price) <= tol:
            return lv.qty
    return 0.0


def _seed(book: ReplayBook, src: "OrderBook | ReplayBook") -> None:
    if isinstance(src, ReplayBook):
        book.bids = dict(src.bids)
        book.asks = dict(src.asks)
        book.last_ms = src.last_ms
        return
    book.bids = {lv.price: lv.qty for lv in src.bids}
    book.asks = {lv.price: lv.qty for lv in src.asks}
    book.last_ms = src.last_update_ms


def _tick_of(tape: Any) -> float:
    return tape.tick_size if isinstance(tape, TapeSlice) else 0.0


def _as_events(tape: Any, symbol: str) -> list[TapeEvent]:
    if tape is None:
        return []
    if isinstance(tape, TapeSlice):
        return tape.events
    if isinstance(tape, dict):
        sl = tape.get(symbol) or tape.get(symbol.upper())
        return sl.events if isinstance(sl, TapeSlice) else []
    out: list[TapeEvent] = []
    for item in tape:
        if isinstance(item, TapeEvent):
            out.append(item)
        elif isinstance(item, Trade):
            out.append(
                TapeEvent(
                    ts_ms=item.ts_ms,
                    symbol=item.symbol,
                    kind="trade",
                    price=item.price,
                    qty=item.qty,
                    buyer_is_maker=item.buyer_is_maker,
                )
            )
    out.sort(key=lambda e: e.ts_ms)
    return out


def _as_slices(tape: Any) -> dict[str, TapeSlice]:
    if isinstance(tape, TapeSlice):
        return {tape.symbol: tape}
    if isinstance(tape, dict):
        return {str(k).upper(): v for k, v in tape.items()}
    if isinstance(tape, (str, Path)):
        return load_tape(tape)
    return {}


def _signals_of(arm: Any) -> list[QuoteSignal]:
    trades = list(getattr(arm, "trades", None) or [])
    if not trades:
        return []
    if isinstance(trades[0], QuoteSignal):
        return sorted(trades, key=lambda s: s.ts_ms)
    return signals_from_trades(trades)


def _realised_index(arm: Any) -> dict[tuple[str, int], ClosedTrade]:
    """The arm's own taker trades, keyed so the TAKER leg can reuse them."""
    trades = list(getattr(arm, "trades", None) or [])
    out: dict[tuple[str, int], ClosedTrade] = {}
    for t in trades:
        if isinstance(t, ClosedTrade):
            out[(t.symbol, int(t.entry_ms))] = t
    return out


def _index_at(events: Sequence[TapeEvent], ts_ms: int) -> int:
    lo, hi = 0, len(events)
    while lo < hi:
        mid = (lo + hi) // 2
        if events[mid].ts_ms < ts_ms:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _books_at(
    slices: dict[str, TapeSlice], signals: Sequence[QuoteSignal]
) -> list[ReplayBook]:
    """Book state at each signal time, built in one forward pass per symbol."""
    out: list[ReplayBook | None] = [None] * len(signals)
    by_symbol: dict[str, list[int]] = {}
    for i, s in enumerate(signals):
        by_symbol.setdefault(s.symbol, []).append(i)
    for sym, idxs in by_symbol.items():
        sl = slices.get(sym)
        if sl is None:
            for i in idxs:
                out[i] = ReplayBook(sym)
            continue
        idxs.sort(key=lambda i: signals[i].ts_ms)
        book = ReplayBook(sym)
        cursor = 0
        for i in idxs:
            target = signals[i].ts_ms
            while cursor < len(sl.events) and sl.events[cursor].ts_ms <= target:
                ev = sl.events[cursor]
                if ev.kind in ("depth", "ticker"):
                    book.apply(ev)
                cursor += 1
            out[i] = book.copy()
    return [b if b is not None else ReplayBook("") for b in out]


def _book_at(sl: TapeSlice, ts_ms: int, *, seed: ReplayBook | None = None) -> ReplayBook:
    book = ReplayBook(sl.symbol)
    start = 0
    if seed is not None:
        _seed(book, seed)
        start = _index_at(sl.events, seed.last_ms)
    for ev in sl.events[start:]:
        if ev.ts_ms > ts_ms:
            break
        if ev.kind in ("depth", "ticker"):
            book.apply(ev)
    return book


def _mids_at(sl: TapeSlice, times: Sequence[int]) -> list[float]:
    """Mid at each of *times* in one forward pass.

    A time past the end of the tape returns 0.0 — "we do not know" — rather than
    the last mid we saw, which would silently price an exit that never happened.
    """
    order = sorted(range(len(times)), key=lambda i: times[i])
    out = [0.0] * len(times)
    book = ReplayBook(sl.symbol)
    cursor = 0
    for i in order:
        target = times[i]
        if target > sl.end_ms:
            continue
        while cursor < len(sl.events) and sl.events[cursor].ts_ms <= target:
            ev = sl.events[cursor]
            if ev.kind in ("depth", "ticker"):
                book.apply(ev)
            cursor += 1
        out[i] = book.mid
    return out


def _trade_from(
    sig: QuoteSignal, entry_price: float, qty: float, fees: Any, *, maker: bool
) -> ClosedTrade:
    """Price one signal under one execution style.

    Only the *entry* varies between arms; the exit is held fixed at the signal's
    own exit and always charged the taker rate, because a maker exit is a
    separate question and mixing the two would let a fee saving on the way out
    disguise a bad fill on the way in.

    ``gross_pnl_usd`` is measured from the signal's **reference** (taker) entry
    price and the difference between that and the price actually obtained is
    booked as ``slippage_usd``, so the three lines stay additive:
    ``net = gross − fees − slippage``.  A maker entry resting behind the touch
    is price *improvement*, so its slippage is negative — which is the entire
    economic case for this module, and it should be visible as its own line
    rather than buried in a better gross.
    """
    side_sign = 1.0 if sig.side is Side.LONG else -1.0
    reference = sig.taker_entry_price if sig.taker_entry_price > 0 else entry_price
    gross = (sig.exit_price - reference) * qty * side_sign
    slippage = (entry_price - reference) * qty * side_sign
    entry_bps = fees.maker_bps(sig.symbol) if maker else fees.taker_bps(sig.symbol)
    exit_bps = fees.taker_bps(sig.symbol)
    entry_fee = entry_price * qty * entry_bps / _BPS
    exit_fee = sig.exit_price * qty * exit_bps / _BPS
    return ClosedTrade(
        id=f"{sig.symbol}-{sig.ts_ms}-{'M' if maker else 'T'}",
        symbol=sig.symbol,
        side=sig.side,
        strategy=sig.strategy,
        regime=sig.regime,
        ledger="research",
        qty=qty,
        entry_price=entry_price,
        exit_price=sig.exit_price,
        entry_ms=sig.ts_ms,
        exit_ms=sig.exit_ms,
        notional_usd=entry_price * qty,
        gross_pnl_usd=gross,
        entry_fee_usd=entry_fee,
        exit_fee_usd=exit_fee,
        slippage_usd=slippage,
        net_pnl_usd=gross - entry_fee - exit_fee - slippage,
        exit_reason=ExitReason.TIME_STOP,
        risk_usd=0.0,
        expected_net_edge_bps=sig.expected_net_edge_bps,
        simulator_version=MakerReplay.version,
    )


def perf_stats(trades: Sequence[ClosedTrade]) -> PerfStats:
    """``PerfStats`` over a bare list of closed trades.

    Same arithmetic as ``app.sim.ledger.Ledger.stats`` (including its
    positive-magnitude ``avg_loss``), with the drawdown taken from the NET
    equity path in exit order — the only honest way to get one without an
    account behind it.
    """
    stats = PerfStats()
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

    equity = peak = worst = 0.0
    for t in sorted(trades, key=lambda x: x.exit_ms):
        equity += t.net_pnl_usd
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    stats.max_drawdown = worst
    stats.capital_turnover = sum(t.notional_usd for t in trades)
    return stats


__all__ = [
    "ADVERSE_SELECTION_HORIZON",
    "MARKOUT_HORIZONS_MS",
    "MIN_PRINTS_PER_SYMBOL",
    "MIN_QUOTES",
    "MIN_TAPE_HOURS",
    "ExecutionComparison",
    "MakerOutcome",
    "MakerReplay",
    "QuoteSignal",
    "ReplayBook",
    "TapeCoverage",
    "TapeEvent",
    "TapeSlice",
    "iter_tape_messages",
    "load_tape",
    "parse_message",
    "perf_stats",
    "signals_from_trades",
    "tape_coverage",
]
