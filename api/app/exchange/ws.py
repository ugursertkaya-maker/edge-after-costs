"""Binance.US combined-stream market-data feed.

The feed owns the socket, the reconnect policy and the rolling in-memory view
of the market (books via :class:`BookManager`, plus recent trades and 1m bars).
Everything it stores is a bounded ``deque`` — the worker container has 768 MB
and shares it with the rest of the stack, so nothing here may grow with uptime.

Message handling is split in two on purpose:

* :meth:`MarketDataFeed.run` does the I/O — connect, resubscribe, back off,
  re-snapshot;
* :meth:`MarketDataFeed.ingest_message` is a pure synchronous function of
  ``(msg, ts_ms)``.  It never raises and never awaits, which is what makes the
  whole protocol testable offline.

Stream layout (§ build brief): hot symbols need a real book and full flow, warm
symbols only need a quote and a bar.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..config import Settings, get_settings
from ..types import Bar, Trade, now_ms
from .book import BookManager
from .rest import BinanceUSRest

#: Per-symbol ring buffers.
MAX_TRADES = 500
MAX_BARS = 240
#: Refuse to track an unbounded number of symbols if the exchange sends junk.
MAX_TRACKED_SYMBOLS = 64
#: Rolling window for the message-rate figure in :class:`DataHealth`.
RATE_WINDOW_S = 30

RECONNECT_BASE_S = 1.0
RECONNECT_CAP_S = 30.0
#: How often, while connected, we re-snapshot books that reported a gap.
RESYNC_INTERVAL_S = 2.0
#: Blocking recv timeout — bounds how long a stop request can take to land.
RECV_TIMEOUT_S = 1.0

HOT_STREAMS = ("depth@100ms", "aggTrade", "kline_1m", "bookTicker")
WARM_STREAMS = ("bookTicker", "kline_1m", "depth20@1000ms")


@dataclass
class DataHealth:
    """One-glance answer to 'is it safe to trade right now?'."""

    connected: bool = False
    streams: int = 0
    updates_per_sec: float = 0.0
    reconnects: int = 0
    dropped: int = 0
    last_message_ms: int = 0
    stale_symbols: list[str] = field(default_factory=list)
    desynced_symbols: list[str] = field(default_factory=list)


class MarketDataFeed:
    """Live combined-stream reader for the hot/warm universe.

    ``rest`` and ``connect`` are optional injection points: the frozen contract
    only mandates ``books``/``settings``, but the feed has to re-request REST
    depth snapshots after every (re)connect, and tests need to drive the socket.
    Both default to the real thing.
    """

    def __init__(
        self,
        *,
        books: BookManager,
        settings: Settings | None = None,
        rest: BinanceUSRest | None = None,
        connect: Callable[[str], Any] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.books = books
        self._rest = rest
        self._owns_rest = rest is None
        self._connect = connect
        self._rng = rng or random.Random(7)

        self._hot: list[str] = []
        self._warm: list[str] = []
        self._trades: dict[str, deque[Trade]] = {}
        self._bars: dict[str, deque[Bar]] = {}
        self._partial_bars: dict[str, Bar] = {}

        # Per-second message buckets: O(1) memory, exact enough for a rate.
        self._buckets: deque[list[int]] = deque(maxlen=RATE_WINDOW_S + 2)

        self.connected = False
        self.reconnects = 0
        self.dropped = 0
        self.messages = 0
        self.last_message_ms = 0
        self.last_error: str = ""

        self._resubscribe = asyncio.Event()
        self._universe_version = 0

    # ------------------------------------------------------------------ #
    # universe / subscriptions
    # ------------------------------------------------------------------ #

    def set_universe(self, hot: list[str], warm: list[str]) -> None:
        """Swap the subscription set; safe to call while :meth:`run` is live."""
        new_hot = [s.upper() for s in hot or []]
        new_warm = [s.upper() for s in warm or [] if s.upper() not in new_hot]
        if new_hot == self._hot and new_warm == self._warm:
            return
        self._hot, self._warm = new_hot, new_warm
        self._universe_version += 1
        keep = set(new_hot) | set(new_warm)
        for store in (self._trades, self._bars, self._partial_bars):
            for sym in [s for s in store if s not in keep]:
                store.pop(sym, None)
        self._resubscribe.set()

    @property
    def universe(self) -> list[str]:
        return self._hot + self._warm

    def stream_names(self) -> list[str]:
        """Combined-stream path components, lowercase as Binance requires."""
        names: list[str] = []
        for sym in self._hot:
            names.extend(f"{sym.lower()}@{s}" for s in HOT_STREAMS)
        for sym in self._warm:
            names.extend(f"{sym.lower()}@{s}" for s in WARM_STREAMS)
        return names

    def stream_url(self) -> str:
        names = self.stream_names()
        return f"{self.settings.exchange.ws_base}/stream?streams={'/'.join(names)}"

    # ------------------------------------------------------------------ #
    # the socket
    # ------------------------------------------------------------------ #

    async def run(self, stop: "asyncio.Event | None" = None) -> None:
        """Connect, read, and reconnect with jittered exponential backoff."""
        stop = stop or asyncio.Event()
        self._resubscribe = asyncio.Event()
        delay = RECONNECT_BASE_S

        try:
            while not stop.is_set():
                if not self.stream_names():
                    await self._sleep_until(stop, 0.5)
                    continue

                self._resubscribe.clear()
                planned = self._universe_version
                resubscribed = False
                try:
                    async with self._open(self.stream_url()) as ws:
                        self.connected = True
                        delay = RECONNECT_BASE_S
                        await self._snapshot_hot()
                        resync = asyncio.ensure_future(self._resync_loop(stop))
                        try:
                            await self._read_loop(ws, stop)
                        finally:
                            resync.cancel()
                            with contextlib.suppress(
                                asyncio.CancelledError, Exception
                            ):
                                await resync
                    resubscribed = self._universe_version != planned
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - any socket error reconnects
                    self.last_error = repr(exc)
                    self.reconnects += 1
                finally:
                    self.connected = False

                if stop.is_set():
                    break
                if resubscribed or self._resubscribe.is_set():
                    continue  # universe changed: reconnect immediately
                await self._sleep_until(stop, self._jitter(delay))
                delay = min(RECONNECT_CAP_S, delay * 2)
        finally:
            self.connected = False
            if self._owns_rest and self._rest is not None:
                with contextlib.suppress(Exception):
                    await self._rest.close()
                self._rest = None

    def _open(self, url: str):
        if self._connect is not None:
            return self._connect(url)
        import websockets  # imported lazily so unit tests need no ws stack

        return websockets.connect(url, ping_interval=20, ping_timeout=20, max_queue=512)

    async def _read_loop(self, ws: Any, stop: asyncio.Event) -> None:
        while not stop.is_set() and not self._resubscribe.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_S)
            except asyncio.TimeoutError:
                continue
            self._on_raw(raw)

    def _on_raw(self, raw: Any) -> None:
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8")
            except Exception:  # noqa: BLE001
                self.dropped += 1
                return
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                self.dropped += 1
                return
        self.ingest_message(raw, now_ms())

    async def _snapshot_hot(self, symbols: Iterable[str] | None = None) -> None:
        """(Re)seed diff books from REST — mandatory after every connect."""
        targets = list(symbols) if symbols is not None else list(self._hot)
        if not targets:
            return
        rest = self._ensure_rest()
        for sym in targets:
            try:
                book = await rest.depth(sym, limit=100)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"snapshot {sym}: {exc!r}"
                self.books.mark_desynced(sym, f"snapshot failed: {exc!r}")
                continue
            self.books.apply_snapshot(
                sym,
                book.last_update_id,
                [(lv.price, lv.qty) for lv in book.bids],
                [(lv.price, lv.qty) for lv in book.asks],
                book.last_update_ms or now_ms(),
            )

    async def _resync_loop(self, stop: asyncio.Event) -> None:
        """Re-snapshot any book that reported a gap while we stay connected."""
        while not stop.is_set():
            await asyncio.sleep(RESYNC_INTERVAL_S)
            hot = set(self._hot)
            pending = [s for s in self.books.needs_resync() if s in hot]
            if pending:
                await self._snapshot_hot(pending)

    def _ensure_rest(self) -> BinanceUSRest:
        if self._rest is None:
            self._rest = BinanceUSRest(settings=self.settings)
            self._owns_rest = True
        return self._rest

    async def _sleep_until(self, stop: asyncio.Event, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=max(0.0, seconds))

    def _jitter(self, delay: float) -> float:
        """Full-ish jitter in [50%, 100%] of the capped delay."""
        capped = min(RECONNECT_CAP_S, delay)
        return capped * (0.5 + 0.5 * self._rng.random())

    # ------------------------------------------------------------------ #
    # ingest — pure, synchronous, never raises
    # ------------------------------------------------------------------ #

    def ingest_message(self, msg: dict, ts_ms: int) -> None:
        """Route one decoded stream message.  Malformed input counts as dropped."""
        try:
            if not isinstance(msg, dict) or not msg:
                self.dropped += 1
                return

            stream = msg.get("stream")
            if isinstance(stream, str):
                data = msg.get("data")
            else:
                stream, data = "", msg
            if not isinstance(data, dict) or not data:
                self.dropped += 1
                return

            kind = data.get("e")
            if not isinstance(kind, str) or not kind:
                kind = _infer_kind(stream, data)

            handler = {
                "bookTicker": self._on_book_ticker,
                "aggTrade": self._on_agg_trade,
                "kline": self._on_kline,
                "depthUpdate": self._on_depth_update,
                "depth20": self._on_partial_depth,
            }.get(kind)
            if handler is None:
                self.dropped += 1
                return

            if handler(data, stream, int(ts_ms)):
                self.messages += 1
                self.last_message_ms = int(ts_ms)
                self._tick(int(ts_ms))
            else:
                self.dropped += 1
        except Exception:  # noqa: BLE001 - a bad frame must never kill the feed
            self.dropped += 1

    def _on_book_ticker(self, data: dict, stream: str, ts_ms: int) -> bool:
        sym = _symbol(data, stream)
        if not sym or not self._allow(sym):
            return False
        try:
            bid, bid_qty = float(data["b"]), float(data["B"])
            ask, ask_qty = float(data["a"]), float(data["A"])
        except (KeyError, TypeError, ValueError):
            return False
        self.books.apply_book_ticker(sym, bid, bid_qty, ask, ask_qty, ts_ms)
        return True

    def _on_agg_trade(self, data: dict, stream: str, ts_ms: int) -> bool:
        sym = _symbol(data, stream)
        if not sym or not self._allow(sym):
            return False
        try:
            trade = Trade(
                symbol=sym,
                price=float(data["p"]),
                qty=float(data["q"]),
                ts_ms=int(data["T"]),
                buyer_is_maker=bool(data["m"]),
            )
        except (KeyError, TypeError, ValueError):
            return False
        self._trade_buf(sym).append(trade)
        return True

    def _on_kline(self, data: dict, stream: str, ts_ms: int) -> bool:
        k = data.get("k")
        if not isinstance(k, dict):
            return False
        sym = _symbol(k, stream) or _symbol(data, stream)
        if not sym or not self._allow(sym):
            return False
        try:
            bar = Bar(
                symbol=sym,
                open_ms=int(k["t"]),
                close_ms=int(k["T"]),
                open=float(k["o"]),
                high=float(k["h"]),
                low=float(k["l"]),
                close=float(k["c"]),
                volume=float(k["v"]),
                quote_volume=float(k.get("q", 0.0) or 0.0),
                trades=int(k.get("n", 0) or 0),
                taker_buy_quote=float(k.get("Q", k.get("V", 0.0)) or 0.0),
            )
        except (KeyError, TypeError, ValueError):
            return False

        if bool(k.get("x")):
            bars = self._bar_buf(sym)
            if bars and bars[-1].open_ms == bar.open_ms:
                bars[-1] = bar
            else:
                bars.append(bar)
            partial = self._partial_bars.get(sym)
            if partial is not None and partial.open_ms <= bar.open_ms:
                self._partial_bars.pop(sym, None)
        else:
            self._bar_buf(sym)  # make sure the symbol is tracked/allowed
            self._partial_bars[sym] = bar
        return True

    def _on_depth_update(self, data: dict, stream: str, ts_ms: int) -> bool:
        sym = _symbol(data, stream)
        if not sym or not self._allow(sym):
            return False
        try:
            first_id = int(data["U"])
            final_id = int(data["u"])
        except (KeyError, TypeError, ValueError):
            return False
        bids, asks = data.get("b"), data.get("a")
        if not isinstance(bids, list) or not isinstance(asks, list):
            return False
        event_ms = _int_or(data.get("E"), ts_ms)
        self.books.apply_diff(sym, first_id, final_id, bids, asks, event_ms)
        return True

    def _on_partial_depth(self, data: dict, stream: str, ts_ms: int) -> bool:
        sym = _symbol(data, stream)
        if not sym or not self._allow(sym):
            return False
        bids, asks = data.get("bids"), data.get("asks")
        if not isinstance(bids, list) or not isinstance(asks, list):
            return False
        last_id = _int_or(data.get("lastUpdateId"), 0)
        self.books.apply_partial(sym, last_id, bids, asks, ts_ms)
        return True

    # ------------------------------------------------------------------ #
    # views
    # ------------------------------------------------------------------ #

    def recent_trades(self, symbol: str, limit: int = 200) -> list[Trade]:
        buf = self._trades.get(symbol.upper())
        if not buf:
            return []
        return list(buf)[-max(0, int(limit)) :]

    def bars(self, symbol: str, limit: int = 240, *, include_partial: bool = True) -> list[Bar]:
        """1m bars oldest-first.

        Every element except possibly the last is a **closed** bar.  The last
        element is the in-progress bar when one exists (and ``include_partial``
        is left on), because the feature engine wants the live candle but must
        know it can still change.
        """
        sym = symbol.upper()
        out = list(self._bars.get(sym, ()))
        partial = self._partial_bars.get(sym)
        if include_partial and partial is not None:
            if not out or partial.open_ms > out[-1].open_ms:
                out.append(partial)
        return out[-max(0, int(limit)) :]

    def health(self, *, ref_ms: int | None = None) -> DataHealth:
        ref = int(ref_ms) if ref_ms is not None else now_ms()
        max_age = self.settings.risk.max_book_age_ms
        stale: list[str] = []
        for sym in self.universe:
            book = self.books.get(sym)
            if book is None or not book.bids or not book.asks:
                stale.append(sym)
            elif ref - book.last_update_ms > max_age:
                stale.append(sym)
        return DataHealth(
            connected=self.connected,
            streams=len(self.stream_names()),
            updates_per_sec=self._rate(ref),
            reconnects=self.reconnects,
            dropped=self.dropped,
            last_message_ms=self.last_message_ms,
            stale_symbols=sorted(stale),
            desynced_symbols=self.books.needs_resync(),
        )

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _allow(self, symbol: str) -> bool:
        """Track known symbols always, unknown ones only up to a hard cap."""
        if symbol in self._trades or symbol in self._bars:
            return True
        if symbol in self._hot or symbol in self._warm:
            return True
        return len(set(self._trades) | set(self._bars)) < MAX_TRACKED_SYMBOLS

    def _trade_buf(self, symbol: str) -> deque[Trade]:
        buf = self._trades.get(symbol)
        if buf is None:
            buf = deque(maxlen=MAX_TRADES)
            self._trades[symbol] = buf
        return buf

    def seed_bars(self, symbol: str, bars: list[Bar]) -> int:
        """Pre-fill closed 1m bars from REST so the bot is not blind on boot.

        Without this the feature engine needs ~30 minutes of live stream before
        it has enough history to classify a regime, which means every restart
        costs half an hour of trading and a 7-day observation window really
        starts late. Bars already held are kept — only genuinely older ones are
        prepended, so a seed can never overwrite live data.
        """
        if not bars:
            return 0
        buf = self._bar_buf(symbol.upper())
        earliest = buf[0].open_ms if buf else None
        fresh = [b for b in bars if earliest is None or b.open_ms < earliest]
        if not fresh:
            return 0
        merged = sorted(fresh, key=lambda b: b.open_ms)[-buf.maxlen:] + list(buf)
        buf.clear()
        buf.extend(merged[-buf.maxlen:])
        return len(fresh)

    def _bar_buf(self, symbol: str) -> deque[Bar]:
        buf = self._bars.get(symbol)
        if buf is None:
            buf = deque(maxlen=MAX_BARS)
            self._bars[symbol] = buf
        return buf

    def _tick(self, ts_ms: int) -> None:
        sec = ts_ms // 1000
        if self._buckets and self._buckets[-1][0] == sec:
            self._buckets[-1][1] += 1
        else:
            self._buckets.append([sec, 1])

    def _rate(self, ref_ms: int) -> float:
        cutoff = (ref_ms // 1000) - RATE_WINDOW_S
        total = sum(count for sec, count in self._buckets if sec > cutoff)
        return total / float(RATE_WINDOW_S)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _infer_kind(stream: str, data: dict) -> str:
    """Work out the payload type when the frame carries no ``e`` field."""
    lowered = (stream or "").lower()
    if "@bookticker" in lowered:
        return "bookTicker"
    if "@depth" in lowered and "@depth@" not in lowered:
        return "depth20"  # depth5/10/20 partial-book snapshot streams
    if "lastUpdateId" in data and "bids" in data:
        return "depth20"
    if {"b", "B", "a", "A"} <= set(data):
        return "bookTicker"
    return ""


def _symbol(data: dict, stream: str) -> str:
    sym = data.get("s")
    if isinstance(sym, str) and sym:
        return sym.upper()
    if stream and "@" in stream:
        return stream.split("@", 1)[0].upper()
    return ""


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
