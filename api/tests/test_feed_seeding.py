"""REST bar seeding.

A cold feed needs ~30 minutes of live stream before the feature engine can
classify anything, so every restart used to cost half an hour of trading.
Seeding fixes that, but it must never overwrite or reorder live data.
"""

from __future__ import annotations

from app.exchange.book import BookManager
from app.exchange.ws import MarketDataFeed
from app.types import Bar

SYMBOL = "BTCUSD"


def bar(i: int, *, close: float = 100.0) -> Bar:
    return Bar(
        symbol=SYMBOL,
        open_ms=i * 60_000,
        close_ms=(i + 1) * 60_000 - 1,
        open=close, high=close * 1.001, low=close * 0.999, close=close,
        volume=1.0, quote_volume=close, trades=10, taker_buy_quote=close / 2,
    )


def feed() -> MarketDataFeed:
    return MarketDataFeed(books=BookManager())


def test_seeding_an_empty_feed_fills_the_history():
    f = feed()
    assert f.seed_bars(SYMBOL, [bar(i) for i in range(100)]) == 100
    out = f.bars(SYMBOL, limit=500)
    assert len(out) == 100
    assert [b.open_ms for b in out] == sorted(b.open_ms for b in out)


def test_seeding_is_a_no_op_when_history_already_covers_the_range():
    f = feed()
    f.seed_bars(SYMBOL, [bar(i) for i in range(50)])
    assert f.seed_bars(SYMBOL, [bar(i) for i in range(20, 50)]) == 0
    assert len(f.bars(SYMBOL, limit=500)) == 50


def test_seeding_never_overwrites_live_bars():
    """Live data wins: a seed only ever prepends genuinely older bars."""
    f = feed()
    live = [bar(i, close=999.0) for i in range(40, 50)]
    f.seed_bars(SYMBOL, live)
    f.seed_bars(SYMBOL, [bar(i, close=1.0) for i in range(60)])

    out = {b.open_ms: b.close for b in f.bars(SYMBOL, limit=500)}
    for b in live:
        assert out[b.open_ms] == 999.0, "a seed clobbered a live bar"
    assert out[0] == 1.0, "older history should have been prepended"


def test_seeding_respects_the_ring_buffer_bound():
    f = feed()
    f.seed_bars(SYMBOL, [bar(i) for i in range(5_000)])
    out = f.bars(SYMBOL, limit=10_000)
    assert len(out) <= 240
    # The bars kept must be the most recent ones, still in order.
    assert out[-1].open_ms == 4_999 * 60_000
    assert [b.open_ms for b in out] == sorted(b.open_ms for b in out)


def test_seeding_empty_input_is_harmless():
    f = feed()
    assert f.seed_bars(SYMBOL, []) == 0
    assert f.bars(SYMBOL) == []


def test_worker_feeds_the_engine_closed_bars_only():
    """Regression: a partial bar folded as final zeroes volume_ratio all minute
    and makes the engine skip the real closed bar for that same minute."""
    f = feed()
    f.seed_bars(SYMBOL, [bar(i) for i in range(10)])
    f._partial_bars[SYMBOL] = bar(10)

    assert f.bars(SYMBOL)[-1].open_ms == 10 * 60_000, "partial is exposed by default"
    assert f.bars(SYMBOL, include_partial=False)[-1].open_ms == 9 * 60_000

    from app.worker.main import TradingWorker

    class _Worker:
        feed = f

    got = TradingWorker._closed_bars(_Worker(), SYMBOL)
    assert got[-1].open_ms == 9 * 60_000, "the worker must not see the partial bar"


def test_closed_bars_falls_back_for_a_feed_without_the_keyword():
    class Minimal:
        def bars(self, symbol, limit=240):
            return [bar(1)]

    from app.worker.main import TradingWorker

    class _W:
        feed = Minimal()

    assert TradingWorker._closed_bars(_W(), SYMBOL)[0].open_ms == 60_000
