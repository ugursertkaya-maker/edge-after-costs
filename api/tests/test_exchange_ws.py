"""Market-data feed: stream naming, message ingest, health, reconnect."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.exchange import ws as ws_mod
from app.exchange.book import BookManager
from app.exchange.ws import DataHealth, MarketDataFeed
from app.types import OrderBook, BookLevel

T0 = 1_725_000_000_000


def feed(**kwargs) -> MarketDataFeed:
    return MarketDataFeed(books=kwargs.pop("books", BookManager()), **kwargs)


def envelope(stream: str, data: dict) -> dict:
    return {"stream": stream, "data": data}


def kline(symbol="BTCUSD", open_ms=T0, close=61000.0, closed=False) -> dict:
    return {
        "e": "kline",
        "E": open_ms + 1000,
        "s": symbol,
        "k": {
            "t": open_ms, "T": open_ms + 59_999, "s": symbol, "i": "1m",
            "o": "60950.00", "h": "61100.00", "l": "60900.00", "c": f"{close:.2f}",
            "v": "12.5", "q": "762750.0", "n": 850, "x": closed,
            "V": "6.1", "Q": "372220.0",
        },
    }


# --------------------------------------------------------------------------- #
# subscriptions
# --------------------------------------------------------------------------- #


def test_stream_names_are_lowercase_and_tier_specific():
    f = feed()
    f.set_universe(["BTCUSD"], ["adausd"])

    names = f.stream_names()
    assert names[:4] == [
        "btcusd@depth@100ms", "btcusd@aggTrade", "btcusd@kline_1m", "btcusd@bookTicker"
    ]
    assert names[4:] == ["adausd@bookTicker", "adausd@kline_1m", "adausd@depth20@1000ms"]

    url = f.stream_url()
    assert url.startswith("wss://stream.binance.us:9443/stream?streams=")
    assert "btcusd@depth@100ms/btcusd@aggTrade" in url
    assert f.universe == ["BTCUSD", "ADAUSD"]


def test_set_universe_drops_state_for_symbols_that_left():
    f = feed()
    f.set_universe(["BTCUSD"], [])
    f.ingest_message(kline(closed=True), T0)
    assert f.bars("BTCUSD")

    f.set_universe(["ETHUSD"], [])
    assert f.bars("BTCUSD") == []
    assert f.stream_names()[0] == "ethusd@depth@100ms"


# --------------------------------------------------------------------------- #
# ingest_message must never raise
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "msg",
    [
        None, [], {}, "", "garbage", 42, b"bytes",
        {"stream": "btcusd@aggTrade"},
        {"stream": "btcusd@aggTrade", "data": None},
        {"stream": "btcusd@aggTrade", "data": "nope"},
        {"data": {}},
        {"e": "aggTrade"},
        {"e": "aggTrade", "s": "BTCUSD"},
        {"e": "aggTrade", "s": "BTCUSD", "p": "not-a-number", "q": "1", "T": 1, "m": False},
        {"e": "kline", "s": "BTCUSD"},
        {"e": "kline", "s": "BTCUSD", "k": {}},
        {"e": "depthUpdate", "s": "BTCUSD"},
        {"e": "depthUpdate", "s": "BTCUSD", "U": 1, "u": 2, "b": "x", "a": []},
        {"e": "unknownEvent", "s": "BTCUSD"},
        {"s": "BTCUSD"},
        {"stream": "btcusd@depth20@1000ms", "data": {"lastUpdateId": 1}},
    ],
)
def test_malformed_messages_are_counted_not_raised(msg):
    f = feed()
    f.ingest_message(msg, T0)
    assert f.dropped == 1
    assert f.messages == 0
    assert f.last_message_ms == 0


def test_dropped_accumulates_and_the_feed_keeps_working():
    f = feed()
    for bad in (None, {}, {"e": "nope"}):
        f.ingest_message(bad, T0)
    assert f.dropped == 3

    f.ingest_message(envelope("btcusd@aggTrade", {
        "e": "aggTrade", "s": "BTCUSD", "p": "61000.5", "q": "0.01", "T": T0, "m": True
    }), T0)
    assert f.dropped == 3 and f.messages == 1


# --------------------------------------------------------------------------- #
# payload handling
# --------------------------------------------------------------------------- #


def test_combined_envelope_is_unwrapped_for_agg_trades():
    f = feed()
    f.ingest_message(
        envelope("btcusd@aggTrade", {
            "e": "aggTrade", "E": T0, "s": "BTCUSD", "a": 1,
            "p": "61000.50", "q": "0.02", "f": 1, "l": 1, "T": T0 - 5, "m": True,
        }),
        T0,
    )
    trades = f.recent_trades("BTCUSD")
    assert len(trades) == 1
    assert trades[0].price == 61000.50
    assert trades[0].qty == 0.02
    assert trades[0].ts_ms == T0 - 5
    assert trades[0].buyer_is_maker is True
    assert f.last_message_ms == T0


def test_raw_payload_without_envelope_also_works():
    f = feed()
    f.ingest_message({
        "e": "aggTrade", "s": "ETHUSD", "p": "2500", "q": "1", "T": T0, "m": False
    }, T0)
    assert len(f.recent_trades("ETHUSD")) == 1


def test_trades_are_bounded_to_500():
    f = feed()
    for i in range(600):
        f.ingest_message({
            "e": "aggTrade", "s": "BTCUSD", "p": "61000", "q": "0.001",
            "T": T0 + i, "m": False,
        }, T0 + i)
    assert len(f.recent_trades("BTCUSD", limit=10_000)) == 500
    assert f.recent_trades("BTCUSD", limit=3)[0].ts_ms == T0 + 597


def test_book_ticker_is_recognised_from_the_stream_name_without_an_event_type():
    books = BookManager()
    f = feed(books=books)
    f.set_universe([], ["ADAUSD"])
    f.ingest_message(
        envelope("adausd@bookTicker", {
            "u": 400, "s": "ADAUSD", "b": "0.4000", "B": "1000", "a": "0.4010", "A": "900"
        }),
        T0,
    )
    book = books.get("ADAUSD")
    assert book.best_bid == 0.40 and book.best_ask == 0.401
    assert book.last_update_ms == T0
    assert f.dropped == 0


def test_kline_tracks_the_live_bar_then_closes_it():
    f = feed()
    f.ingest_message(kline(open_ms=T0, close=61000.0, closed=False), T0 + 10_000)
    bars = f.bars("BTCUSD")
    assert len(bars) == 1 and bars[-1].close == 61000.0

    # same minute, price moved: the partial is replaced, not appended
    f.ingest_message(kline(open_ms=T0, close=61050.0, closed=False), T0 + 30_000)
    assert len(f.bars("BTCUSD")) == 1
    assert f.bars("BTCUSD")[-1].close == 61050.0

    f.ingest_message(kline(open_ms=T0, close=61070.0, closed=True), T0 + 60_000)
    closed_only = f.bars("BTCUSD", include_partial=False)
    assert len(closed_only) == 1 and closed_only[-1].close == 61070.0
    assert closed_only[-1].quote_volume == 762750.0
    assert closed_only[-1].trades == 850
    assert closed_only[-1].taker_buy_quote == 372220.0

    f.ingest_message(kline(open_ms=T0 + 60_000, close=61080.0, closed=False), T0 + 70_000)
    bars = f.bars("BTCUSD")
    assert len(bars) == 2
    assert bars[-1].open_ms == T0 + 60_000, "the live bar is the last element"
    assert f.bars("BTCUSD", include_partial=False)[-1].open_ms == T0


def test_bars_are_bounded_to_240_closed_candles():
    f = feed()
    for i in range(300):
        f.ingest_message(kline(open_ms=T0 + i * 60_000, closed=True), T0)
    assert len(f.bars("BTCUSD", limit=10_000)) == 240
    assert f.bars("BTCUSD")[0].open_ms == T0 + 60 * 60_000


def test_depth_update_drives_the_book_manager():
    books = BookManager()
    books.apply_snapshot("BTCUSD", 100, [(61000.0, 1.0)], [(61001.0, 1.0)], T0)
    f = feed(books=books)
    f.set_universe(["BTCUSD"], [])

    f.ingest_message(
        envelope("btcusd@depth@100ms", {
            "e": "depthUpdate", "E": T0 + 100, "s": "BTCUSD", "U": 101, "u": 105,
            "b": [["60999.00", "2.0"], ["61000.00", "0"]], "a": [["61002.00", "3.0"]],
        }),
        T0 + 120,
    )
    book = books.get("BTCUSD")
    assert book.last_update_id == 105
    assert book.last_update_ms == T0 + 100, "event time wins over arrival time"
    assert [lv.price for lv in book.bids] == [60999.0]
    assert [lv.price for lv in book.asks] == [61001.0, 61002.0]
    assert f.dropped == 0


def test_partial_depth_takes_its_symbol_from_the_stream_name():
    books = BookManager()
    f = feed(books=books)
    f.ingest_message(
        envelope("adausd@depth20@1000ms", {
            "lastUpdateId": 900,
            "bids": [["0.4000", "1000"], ["0.3990", "2000"]],
            "asks": [["0.4010", "900"]],
        }),
        T0,
    )
    book = books.get("ADAUSD")
    assert book is not None
    assert book.last_update_id == 900
    assert len(book.bids) == 2 and book.best_ask == 0.401
    assert f.dropped == 0


def test_unknown_symbols_are_capped_so_memory_cannot_grow():
    f = feed()
    for i in range(ws_mod.MAX_TRACKED_SYMBOLS + 20):
        f.ingest_message({
            "e": "aggTrade", "s": f"JUNK{i}USD", "p": "1", "q": "1", "T": T0, "m": False
        }, T0)
    assert f.messages == ws_mod.MAX_TRACKED_SYMBOLS
    assert f.dropped == 20


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #


def test_health_reports_rate_staleness_and_desyncs():
    books = BookManager()
    f = feed(books=books)
    f.set_universe(["BTCUSD"], ["ADAUSD"])

    for i in range(60):  # two per second across 30 seconds
        ts = T0 + i * 500
        f.ingest_message({
            "e": "aggTrade", "s": "BTCUSD", "p": "61000", "q": "0.01", "T": ts, "m": False
        }, ts)

    books.apply_snapshot("BTCUSD", 100, [(61000.0, 1.0)], [(61001.0, 1.0)], T0 + 29_500)
    books.apply_book_ticker("ADAUSD", 0.4, 1.0, 0.401, 1.0, T0)  # 29.5s old => stale

    h = f.health(ref_ms=T0 + 29_500)
    assert isinstance(h, DataHealth)
    assert h.updates_per_sec == pytest.approx(2.0)
    assert h.streams == 7
    assert h.dropped == 0
    assert h.last_message_ms == T0 + 29_500
    assert h.stale_symbols == ["ADAUSD"]
    assert h.desynced_symbols == []

    books.apply_diff("BTCUSD", 500, 600, [], [], T0 + 29_600)  # gap
    assert f.health(ref_ms=T0 + 29_500).desynced_symbols == ["BTCUSD"]


def test_health_counts_a_symbol_with_no_book_as_stale():
    f = feed()
    f.set_universe(["BTCUSD"], [])
    h = f.health(ref_ms=T0)
    assert h.stale_symbols == ["BTCUSD"]
    assert h.connected is False
    assert h.updates_per_sec == 0.0


def test_rate_window_forgets_old_messages():
    f = feed()
    f.ingest_message({
        "e": "aggTrade", "s": "BTCUSD", "p": "1", "q": "1", "T": T0, "m": False
    }, T0)
    assert f.health(ref_ms=T0).updates_per_sec > 0
    assert f.health(ref_ms=T0 + 31_000).updates_per_sec == 0.0


# --------------------------------------------------------------------------- #
# run(): connect, resubscribe, reconnect
# --------------------------------------------------------------------------- #


class _FakeSocket:
    def __init__(self, fail: bool = False) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.fail = fail

    async def recv(self):
        if self.fail:
            raise ConnectionError("socket died")
        return await self.queue.get()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeRest:
    def __init__(self) -> None:
        self.snapshots: list[str] = []

    async def depth(self, symbol: str, limit: int = 100) -> OrderBook:
        self.snapshots.append(symbol)
        book = OrderBook(symbol=symbol)
        book.bids = [BookLevel(61000.0, 1.0)]
        book.asks = [BookLevel(61001.0, 1.0)]
        book.last_update_id = 100
        book.last_update_ms = T0
        return book

    async def close(self) -> None:
        return None


async def _until(predicate, timeout: float = 3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.01)


@pytest.fixture()
def fast_ws(monkeypatch):
    monkeypatch.setattr(ws_mod, "RECV_TIMEOUT_S", 0.02)
    monkeypatch.setattr(ws_mod, "RECONNECT_BASE_S", 0.01)
    monkeypatch.setattr(ws_mod, "RESYNC_INTERVAL_S", 0.05)


def test_run_snapshots_hot_symbols_and_resubscribes_on_universe_change(fast_ws):
    async def main():
        urls: list[str] = []
        sockets: list[_FakeSocket] = []

        def connect(url: str):
            urls.append(url)
            sock = _FakeSocket()
            sockets.append(sock)
            return sock

        rest = _FakeRest()
        books = BookManager()
        f = MarketDataFeed(books=books, rest=rest, connect=connect)
        f.set_universe(["BTCUSD"], [])
        stop = asyncio.Event()
        task = asyncio.create_task(f.run(stop))

        await _until(lambda: len(urls) == 1)
        assert rest.snapshots == ["BTCUSD"], "a REST snapshot follows every connect"
        assert f.connected is True

        await sockets[0].queue.put(json.dumps(envelope("btcusd@aggTrade", {
            "e": "aggTrade", "s": "BTCUSD", "p": "61000", "q": "0.01", "T": T0, "m": False
        })))
        await _until(lambda: f.messages == 1)

        f.set_universe(["ETHUSD"], ["ADAUSD"])
        await _until(lambda: len(urls) == 2)
        assert "ethusd@depth@100ms" in urls[1] and "adausd@bookTicker" in urls[1]
        assert rest.snapshots == ["BTCUSD", "ETHUSD"]
        assert f.reconnects == 0, "a planned resubscribe is not a reconnect"

        stop.set()
        await asyncio.wait_for(task, timeout=3)
        assert f.connected is False

    asyncio.run(asyncio.wait_for(main(), 15))


def test_run_reconnects_after_a_socket_error(fast_ws):
    async def main():
        sockets = [_FakeSocket(fail=True), _FakeSocket()]
        opened: list[str] = []

        def connect(url: str):
            opened.append(url)
            return sockets[min(len(opened) - 1, len(sockets) - 1)]

        f = MarketDataFeed(books=BookManager(), rest=_FakeRest(), connect=connect)
        f.set_universe(["BTCUSD"], [])
        stop = asyncio.Event()
        task = asyncio.create_task(f.run(stop))

        await _until(lambda: f.reconnects >= 1 and len(opened) >= 2)
        assert "socket died" in f.last_error

        stop.set()
        await asyncio.wait_for(task, timeout=3)

    asyncio.run(asyncio.wait_for(main(), 15))


def test_run_waits_quietly_when_the_universe_is_empty(fast_ws):
    async def main():
        opened: list[str] = []

        def connect(url: str):
            opened.append(url)
            return _FakeSocket()

        f = MarketDataFeed(books=BookManager(), rest=_FakeRest(), connect=connect)
        stop = asyncio.Event()
        task = asyncio.create_task(f.run(stop))
        await asyncio.sleep(0.05)
        assert opened == []
        stop.set()
        await asyncio.wait_for(task, timeout=3)

    asyncio.run(asyncio.wait_for(main(), 15))


def test_backoff_is_jittered_and_capped():
    f = feed()
    delays = [f._jitter(d) for d in (1, 2, 4, 8, 16, 32, 64)]
    assert all(0 < d for d in delays)
    assert max(delays) <= ws_mod.RECONNECT_CAP_S
    assert delays[-1] >= ws_mod.RECONNECT_CAP_S * 0.5
