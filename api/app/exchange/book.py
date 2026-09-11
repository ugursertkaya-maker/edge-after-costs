"""Local order books, maintained per Binance's diff-depth protocol.

The bot is only allowed to trade off a book it *knows* is correct, so this
module is deliberately strict: any sequence anomaly marks the symbol desynced
and the feed re-snapshots it rather than quietly trading on a book with a hole
in it.

Two tiers share this manager:

* **hot** symbols run the full protocol — REST snapshot + ``@depth@100ms``
  diffs — and get a real multi-level book;
* **warm** symbols only get ``@bookTicker`` (top of book) and periodic
  ``@depth20@1000ms`` snapshots.  Their book is a cheap approximation and is
  never allowed to overwrite a healthy diff-maintained one.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Iterable

from ..config import Settings, get_settings
from ..types import BookLevel, OrderBook

Levels = list[tuple[float, float]]

#: Cap on diffs buffered while waiting for a snapshot (~10 s of depth@100ms).
MAX_BUFFER = 200
#: Hard cap on stored price levels per side, so a noisy book cannot grow forever.
LEVEL_HARD_CAP = 1000


@dataclass
class _Counters:
    snapshots: int = 0
    partials: int = 0
    diffs_applied: int = 0
    diffs_buffered: int = 0
    diffs_dropped: int = 0
    book_tickers: int = 0
    book_tickers_ignored: int = 0
    desyncs: int = 0


@dataclass
class _SymbolState:
    """Everything we keep for one symbol."""

    book: OrderBook
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    buffer: deque = field(default_factory=lambda: deque(maxlen=MAX_BUFFER))
    #: True once a REST snapshot has been applied and not invalidated.
    has_snapshot: bool = False
    #: True once the first diff after the snapshot passed the overlap check.
    started: bool = False
    #: "diff" (full protocol), "partial" (depth20) or "ticker" (top only).
    mode: str = "none"
    desync_reason: str = ""
    counters: _Counters = field(default_factory=_Counters)


class BookManager:
    """Owns every local order book.  Pure, synchronous and unit-testable."""

    def __init__(self, *, max_levels: int = 50, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.max_levels = max(1, int(max_levels))
        self._states: dict[str, _SymbolState] = {}
        #: Bounded audit trail for the data-quality dashboard.
        self.desync_events: deque = deque(maxlen=100)

    # ------------------------------------------------------------------ #
    # ingest
    # ------------------------------------------------------------------ #

    def apply_snapshot(
        self,
        symbol: str,
        last_update_id: int,
        bids: Levels,
        asks: Levels,
        ts_ms: int,
    ) -> None:
        """Install a REST ``/api/v3/depth`` snapshot and replay buffered diffs."""
        sym = symbol.upper()
        st = self._state(sym)
        st.bids = _to_map(bids)
        st.asks = _to_map(asks)
        st.book.last_update_id = int(last_update_id)
        st.book.last_update_ms = int(ts_ms)
        st.book.desynced = False
        st.has_snapshot = True
        st.started = False
        st.mode = "diff"
        st.desync_reason = ""
        st.counters.snapshots += 1

        # Binance's documented recipe: discard everything the snapshot already
        # contains, then replay the rest in order.
        pending = [ev for ev in st.buffer if ev[1] > st.book.last_update_id]
        st.buffer.clear()
        self._materialise(st)
        for first_id, final_id, b, a, ev_ts in pending:
            if not self._apply_event(st, first_id, final_id, b, a, ev_ts):
                break

    def apply_diff(
        self,
        symbol: str,
        first_id: int,
        final_id: int,
        bids: Levels,
        asks: Levels,
        ts_ms: int,
    ) -> bool:
        """Apply one ``depthUpdate``.

        Returns ``False`` (and marks the symbol desynced) only on a genuine
        sequence gap.  Diffs that arrive before the snapshot are buffered, and
        diffs the snapshot already covers are dropped; both count as success
        because neither means the book is wrong.
        """
        sym = symbol.upper()
        st = self._state(sym)
        if not st.has_snapshot:
            st.buffer.append((int(first_id), int(final_id), list(bids), list(asks), int(ts_ms)))
            st.counters.diffs_buffered += 1
            return True
        return self._apply_event(st, int(first_id), int(final_id), bids, asks, int(ts_ms))

    def _apply_event(
        self,
        st: _SymbolState,
        first_id: int,
        final_id: int,
        bids: Levels,
        asks: Levels,
        ts_ms: int,
    ) -> bool:
        last = st.book.last_update_id

        if final_id <= last:
            # Already contained in the snapshot / an out-of-order repeat.
            st.counters.diffs_dropped += 1
            return True

        if not st.started:
            if not (first_id <= last + 1 <= final_id):
                self.mark_desynced(
                    st.book.symbol,
                    f"first diff [{first_id},{final_id}] does not cover snapshot {last}",
                )
                return False
            st.started = True
        elif first_id != last + 1:
            self.mark_desynced(
                st.book.symbol,
                f"sequence gap: expected {last + 1}, got {first_id}",
            )
            return False

        _apply_levels(st.bids, bids)
        _apply_levels(st.asks, asks)
        _prune(st.bids, reverse=True)
        _prune(st.asks, reverse=False)
        st.book.last_update_id = final_id
        st.book.last_update_ms = ts_ms
        st.counters.diffs_applied += 1
        self._materialise(st)
        return True

    def apply_partial(
        self,
        symbol: str,
        last_update_id: int,
        bids: Levels,
        asks: Levels,
        ts_ms: int,
    ) -> None:
        """Install a ``depth20@1000ms`` snapshot for a warm symbol.

        A healthy diff-maintained book is deeper and fresher than 20 levels at
        1 Hz, so the partial is ignored for hot symbols that are in sync.
        """
        sym = symbol.upper()
        st = self._state(sym)
        if st.mode == "diff" and st.has_snapshot and not st.book.desynced:
            return
        st.bids = _to_map(bids)
        st.asks = _to_map(asks)
        st.book.last_update_id = int(last_update_id)
        st.book.last_update_ms = int(ts_ms)
        st.book.desynced = False
        st.mode = "partial"
        st.counters.partials += 1
        self._materialise(st)

    def apply_book_ticker(
        self,
        symbol: str,
        bid: float,
        bid_qty: float,
        ask: float,
        ask_qty: float,
        ts_ms: int,
    ) -> None:
        """Top-of-book update.

        For warm symbols this *is* the book's top level.  For a healthy
        diff-maintained book the bookTicker stream is a second, unsequenced
        source: it may be newer or older than the diffs, so we only use it when
        it agrees with the maintained top (same prices) and otherwise ignore it
        rather than risk corrupting a good book.
        """
        sym = symbol.upper()
        st = self._state(sym)
        try:
            bid, bid_qty = float(bid), float(bid_qty)
            ask, ask_qty = float(ask), float(ask_qty)
        except (TypeError, ValueError):
            st.counters.book_tickers_ignored += 1
            return
        if bid <= 0 or ask <= 0 or ask <= bid:
            st.counters.book_tickers_ignored += 1
            return

        healthy_diff = (
            st.mode == "diff"
            and st.has_snapshot
            and not st.book.desynced
            and bool(st.book.bids)
            and bool(st.book.asks)
        )
        if healthy_diff:
            if st.book.bids[0].price == bid and st.book.asks[0].price == ask:
                st.bids[bid] = bid_qty
                st.asks[ask] = ask_qty
                st.book.bids[0].qty = bid_qty
                st.book.asks[0].qty = ask_qty
                st.book.last_update_ms = max(st.book.last_update_ms, int(ts_ms))
                st.counters.book_tickers += 1
            else:
                st.counters.book_tickers_ignored += 1
            return

        if st.mode == "partial":
            # Keep the depth20 levels behind the new touch; they are real resting
            # size and the depth/imbalance features want them.
            st.bids = _replace_top(st.bids, bid, bid_qty, better=lambda p: p < bid)
            st.asks = _replace_top(st.asks, ask, ask_qty, better=lambda p: p > ask)
        else:
            # Quote-only symbol: the previous quote is not depth, it is history.
            st.bids = {bid: bid_qty}
            st.asks = {ask: ask_qty}
            if st.mode == "none":
                st.mode = "ticker"
        st.book.last_update_ms = int(ts_ms)
        st.counters.book_tickers += 1
        self._materialise(st)

    # ------------------------------------------------------------------ #
    # query
    # ------------------------------------------------------------------ #

    def get(self, symbol: str) -> OrderBook | None:
        st = self._states.get(symbol.upper())
        return st.book if st else None

    def mark_desynced(self, symbol: str, why: str) -> None:
        """Flag a book as untrustworthy and arm it to accept a fresh snapshot.

        Clearing ``has_snapshot`` means subsequent diffs are buffered again, so
        the re-snapshot can replay them the moment it lands.
        """
        sym = symbol.upper()
        st = self._state(sym)
        st.book.desynced = True
        st.has_snapshot = False
        st.started = False
        st.desync_reason = why
        st.buffer.clear()
        st.counters.desyncs += 1
        self.desync_events.append((sym, why, st.book.last_update_ms))

    def needs_resync(self) -> list[str]:
        """Symbols whose diff book must be re-snapshotted, in stable order."""
        out = [
            sym
            for sym, st in self._states.items()
            if st.book.desynced or (st.mode == "diff" and not st.has_snapshot)
        ]
        return sorted(out)

    def stats(self) -> dict[str, object]:
        per_symbol: dict[str, dict[str, object]] = {}
        totals = _Counters()
        for sym, st in sorted(self._states.items()):
            c = st.counters
            per_symbol[sym] = {
                "mode": st.mode,
                "desynced": st.book.desynced,
                "desync_reason": st.desync_reason,
                "last_update_id": st.book.last_update_id,
                "last_update_ms": st.book.last_update_ms,
                "bid_levels": len(st.bids),
                "ask_levels": len(st.asks),
                "buffered": len(st.buffer),
                "snapshots": c.snapshots,
                "partials": c.partials,
                "diffs_applied": c.diffs_applied,
                "diffs_buffered": c.diffs_buffered,
                "diffs_dropped": c.diffs_dropped,
                "book_tickers": c.book_tickers,
                "book_tickers_ignored": c.book_tickers_ignored,
                "desyncs": c.desyncs,
            }
            for f in totals.__dataclass_fields__:
                setattr(totals, f, getattr(totals, f) + getattr(c, f))
        return {
            "symbols": per_symbol,
            "totals": {f: getattr(totals, f) for f in totals.__dataclass_fields__},
            "tracked": len(self._states),
            "desynced_symbols": sorted(
                s for s, st in self._states.items() if st.book.desynced
            ),
        }

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _state(self, symbol: str) -> _SymbolState:
        st = self._states.get(symbol)
        if st is None:
            st = _SymbolState(book=OrderBook(symbol=symbol))
            self._states[symbol] = st
        return st

    def _materialise(self, st: _SymbolState) -> None:
        """Rebuild the public sorted/truncated view from the price maps."""
        st.book.bids = [
            BookLevel(p, q)
            for p, q in sorted(st.bids.items(), key=lambda kv: -kv[0])[: self.max_levels]
        ]
        st.book.asks = [
            BookLevel(p, q)
            for p, q in sorted(st.asks.items(), key=lambda kv: kv[0])[: self.max_levels]
        ]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _to_map(levels: Iterable) -> dict[float, float]:
    out: dict[float, float] = {}
    for row in levels or []:
        try:
            price, qty = float(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if price <= 0 or qty <= 0:
            continue
        out[price] = qty
    return out


def _apply_levels(target: dict[float, float], levels: Iterable) -> None:
    """Merge one side of a diff; a quantity of 0 deletes the level."""
    for row in levels or []:
        try:
            price, qty = float(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if price <= 0:
            continue
        if qty <= 0:
            target.pop(price, None)
        else:
            target[price] = qty


def _prune(side: dict[float, float], *, reverse: bool) -> None:
    """Keep the map bounded — diffs far from the touch are of no use to us."""
    if len(side) <= LEVEL_HARD_CAP:
        return
    keep = sorted(side, reverse=reverse)[:LEVEL_HARD_CAP]
    kept = {p: side[p] for p in keep}
    side.clear()
    side.update(kept)


def _replace_top(side: dict[float, float], price: float, qty: float, *, better) -> dict[float, float]:
    """Set a new top-of-book level, discarding stale levels beyond it."""
    out = {p: q for p, q in side.items() if better(p)}
    out[price] = qty
    return out
