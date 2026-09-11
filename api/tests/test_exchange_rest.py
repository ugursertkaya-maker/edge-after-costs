"""Offline tests for the public Binance.US REST client.

Every request is served by an ``httpx.MockTransport``; nothing here touches the
network, the clock or randomness.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.exchange import rest as rest_mod
from app.exchange.rest import BinanceUSRest
from app.types import Bar, OrderBook, Trade

FIXTURES = Path(__file__).parent / "fixtures" / "binanceus"


def load(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)


def json_route(routes: dict[str, object]):
    """Serve a fixed JSON body per URL path."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = routes.get(request.url.path)
        if body is None:
            return httpx.Response(404, json={"code": -1121, "msg": "Invalid symbol."})
        return httpx.Response(200, json=body)

    return handler


# --------------------------------------------------------------------------- #
# exchangeInfo filter parsing
# --------------------------------------------------------------------------- #


def test_exchange_info_parses_price_lot_and_notional_filters():
    client = make_client(json_route({"/api/v3/exchangeInfo": load("exchange_info")}))
    api = BinanceUSRest(client=client)
    infos = run(api.exchange_info())

    btc = infos["BTCUSD"]
    assert (btc.base_asset, btc.quote_asset, btc.status) == ("BTC", "USD", "TRADING")
    assert btc.tick_size == 0.01
    assert btc.step_size == 0.000001
    assert btc.min_qty == 0.00001
    assert btc.min_notional == 1.0  # NOTIONAL/minNotional

    assert infos["ETHUSDT"].min_notional == 10.0  # legacy MIN_NOTIONAL filter
    assert infos["SOLUSD"].min_notional == 5.0  # NOTIONAL/notional spelling
    assert infos["HALTUSD"].status == "BREAK"


def test_exchange_info_tolerates_missing_filters_and_symbol():
    client = make_client(json_route({"/api/v3/exchangeInfo": load("exchange_info")}))
    infos = run(BinanceUSRest(client=client).exchange_info())

    # HALTUSD has no notional filter at all; the entry with no "symbol" is skipped.
    assert infos["HALTUSD"].min_notional == 0.0
    assert all(sym for sym in infos)
    assert len(infos) == 9


# --------------------------------------------------------------------------- #
# klines
# --------------------------------------------------------------------------- #


def test_klines_maps_the_twelve_element_array():
    client = make_client(json_route({"/api/v3/klines": load("klines_page1")}))
    bars = run(BinanceUSRest(client=client).klines("btcusd"))

    assert all(isinstance(b, Bar) for b in bars)
    first = bars[0]
    assert first.symbol == "BTCUSD"
    assert first.open_ms == 1725000000000
    assert first.close_ms == 1725000059999
    assert (first.open, first.high, first.low, first.close) == (61000.0, 61050.0, 60980.0, 61020.0)
    assert first.volume == 12.5
    assert first.quote_volume == 762750.0
    assert first.trades == 850
    assert first.taker_buy_quote == 372220.0


def test_klines_pages_until_a_short_page_arrives():
    page1, page2 = load("klines_page1"), load("klines_page2")
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        calls.append(params)
        start = int(params.get("startTime", 0))
        return httpx.Response(200, json=page1 if start <= 1725000000000 else page2)

    api = BinanceUSRest(client=make_client(handler))
    bars = run(
        api.klines("BTCUSD", "1m", start_ms=1725000000000, end_ms=1725000299999, limit=3)
    )

    assert len(calls) == 2, "a full page must trigger exactly one follow-up page"
    # the follow-up page resumes one millisecond after the last bar's close
    assert calls[1]["startTime"] == "1725000180000"
    assert [b.open_ms for b in bars] == [
        1725000000000, 1725000060000, 1725000120000, 1725000180000, 1725000240000
    ]
    assert len({b.open_ms for b in bars}) == len(bars)


def test_klines_without_a_range_asks_for_one_page_only():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(200, json=load("klines_page1"))

    bars = run(BinanceUSRest(client=make_client(handler)).klines("BTCUSD", limit=3))
    assert len(calls) == 1
    assert len(bars) == 3


def test_klines_stops_at_end_ms():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=load("klines_page1"))

    bars = run(
        BinanceUSRest(client=make_client(handler)).klines(
            "BTCUSD", start_ms=1725000000000, end_ms=1725000100000, limit=3
        )
    )
    assert [b.open_ms for b in bars] == [1725000000000, 1725000060000]


# --------------------------------------------------------------------------- #
# retries / backoff
# --------------------------------------------------------------------------- #


@pytest.fixture()
def no_sleep(monkeypatch):
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(rest_mod, "_sleep", fake_sleep)
    return waits


def test_429_is_retried_and_retry_after_is_honoured(no_sleep):
    responses = [
        httpx.Response(429, headers={"Retry-After": "2"}, json={"msg": "slow down"}),
        httpx.Response(200, json={"serverTime": 1757500000000}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    api = BinanceUSRest(client=make_client(handler))
    assert run(api.server_time_ms()) == 1757500000000
    assert no_sleep == [2.0]
    assert api.retries == 1


def test_5xx_backs_off_exponentially(no_sleep):
    responses = [
        httpx.Response(502, text="bad gateway"),
        httpx.Response(503, text="unavailable"),
        httpx.Response(200, json={}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    assert run(BinanceUSRest(client=make_client(handler)).ping()) is True
    assert no_sleep == [0.5, 1.0]


def test_418_gives_up_after_max_attempts(no_sleep):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(418, json={"msg": "banned"})

    api = BinanceUSRest(client=make_client(handler), max_attempts=3)
    with pytest.raises(httpx.HTTPStatusError):
        run(api.server_time_ms())
    assert attempts["n"] == 3
    assert no_sleep == [0.5, 1.0]


def test_ping_is_false_when_the_endpoint_is_broken(no_sleep):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"msg": "nope"})

    assert run(BinanceUSRest(client=make_client(handler)).ping()) is False


def test_4xx_is_not_retried(no_sleep):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(400, json={"msg": "bad symbol"})

    with pytest.raises(httpx.HTTPStatusError):
        run(BinanceUSRest(client=make_client(handler)).depth("NOPEUSD"))
    assert attempts["n"] == 1
    assert no_sleep == []


# --------------------------------------------------------------------------- #
# depth / aggTrades / concurrency
# --------------------------------------------------------------------------- #


def test_depth_builds_a_sorted_book_and_drops_empty_levels():
    client = make_client(json_route({"/api/v3/depth": load("depth")}))
    book = run(BinanceUSRest(client=client).depth("btcusd"))

    assert isinstance(book, OrderBook)
    assert book.symbol == "BTCUSD"
    assert book.last_update_id == 1027024
    assert [lv.price for lv in book.bids] == [61000.0, 60999.0, 60997.0]  # 0-qty dropped
    assert [lv.price for lv in book.asks] == [61001.0, 61002.0, 61003.0]
    assert book.mid == pytest.approx(61000.5)


def test_agg_trades_maps_p_q_T_m():
    client = make_client(json_route({"/api/v3/aggTrades": load("agg_trades")}))
    trades = run(BinanceUSRest(client=client).agg_trades("BTCUSD"))

    assert len(trades) == 2  # the malformed row is skipped, not fatal
    assert all(isinstance(t, Trade) for t in trades)
    assert trades[0].price == 61000.10
    assert trades[0].qty == 0.005
    assert trades[0].ts_ms == 1725000005000
    assert trades[0].buyer_is_maker is True
    assert trades[1].buyer_is_maker is False


def test_concurrency_is_capped_at_four():
    live = {"now": 0, "peak": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        for _ in range(5):
            await asyncio.sleep(0)
        live["now"] -= 1
        return httpx.Response(200, json=load("depth"))

    async def main():
        api = BinanceUSRest(client=make_client(handler))
        await asyncio.gather(*(api.depth(f"SYM{i}USD") for i in range(12)))
        return live["peak"]

    assert run(main()) <= 4


def test_close_leaves_an_injected_client_alone():
    client = make_client(json_route({"/api/v3/ping": {}}))

    async def main():
        api = BinanceUSRest(client=client)
        await api.close()
        return await api.ping()

    assert run(main()) is True
