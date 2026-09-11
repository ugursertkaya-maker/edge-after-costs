"""Offline tests for the kline cache and the resumable downloader.

No network: ``fetch_klines`` is driven by a fake REST client that records every
window it was asked for, which is how the "only fetch the missing tail"
behaviour is proved.
"""

from __future__ import annotations

import json

import pytest

from app.backtest import data
from app.types import Bar

MINUTE = 60_000
T0 = 1_700_000_000_000


@pytest.fixture(autouse=True)
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKTEST_CACHE_DIR", str(tmp_path / "cache"))
    data.cache_root(refresh=True)
    yield tmp_path / "cache"
    monkeypatch.delenv("BACKTEST_CACHE_DIR", raising=False)
    data.cache_root(refresh=True)


def make_bars(symbol: str, count: int, *, start_ms: int = T0, price: float = 100.0) -> list[Bar]:
    return [
        Bar(
            symbol=symbol,
            open_ms=start_ms + i * MINUTE,
            close_ms=start_ms + (i + 1) * MINUTE - 1,
            open=price + i,
            high=price + i + 0.5,
            low=price + i - 0.5,
            close=price + i + 0.25,
            volume=10.0 + i,
            quote_volume=1000.0 + i,
            trades=42 + i,
            taker_buy_quote=500.0 + i,
        )
        for i in range(count)
    ]


class FakeRest:
    """Serves a fixed series, page by page, and records every request."""

    def __init__(self, bars: list[Bar]) -> None:
        self.bars = sorted(bars, key=lambda b: b.open_ms)
        self.requests: list[tuple[int, int]] = []

    async def klines(self, symbol, interval="1m", *, start_ms=None, end_ms=None, limit=1000):
        self.requests.append((start_ms, end_ms))
        page = [
            b
            for b in self.bars
            if (start_ms is None or b.open_ms >= start_ms)
            and (end_ms is None or b.open_ms <= end_ms)
        ]
        return page[:limit]

    async def close(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# Pure file operations
# --------------------------------------------------------------------------- #


def test_load_cached_round_trips_save_cache():
    bars = make_bars("BTCUSD", 25)
    path = data.save_cache("BTCUSD", "1m", bars)
    assert path.exists()
    assert data.load_cached("BTCUSD", "1m") == bars


def test_load_cached_missing_file_is_empty():
    assert data.load_cached("NOPEUSD", "1m") == []


def test_save_cache_dedupes_and_sorts():
    bars = make_bars("ETHUSD", 5)
    shuffled = [bars[3], bars[0], bars[3], bars[1], bars[4], bars[2]]
    data.save_cache("ETHUSD", "1m", shuffled)
    assert data.load_cached("ETHUSD", "1m") == bars


def test_load_cached_skips_a_truncated_tail(cache_dir):
    bars = make_bars("SOLUSD", 4)
    path = data.save_cache("SOLUSD", "1m", bars)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"symbol": "SOLUSD", "open_ms": 17000')  # killed mid-write
    assert data.load_cached("SOLUSD", "1m") == bars


def test_save_cache_is_atomic_no_tmp_left_behind(cache_dir):
    data.save_cache("BTCUSD", "1m", make_bars("BTCUSD", 3))
    assert list(cache_dir.glob("*.tmp")) == []


def test_cache_root_falls_back_when_unwritable(monkeypatch, tmp_path):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)
    monkeypatch.setenv("BACKTEST_CACHE_DIR", str(blocked / "nested"))
    root = data.cache_root(refresh=True)
    assert root != blocked / "nested"
    assert root.is_dir()
    probe = root / "x.ndjson"
    probe.write_text("{}", encoding="utf-8")
    probe.unlink()
    blocked.chmod(0o700)


def test_interval_ms():
    assert data.interval_ms("1m") == 60_000
    assert data.interval_ms("5m") == 300_000
    assert data.interval_ms("1h") == 3_600_000
    assert data.interval_ms("1d") == 86_400_000
    with pytest.raises(ValueError):
        data.interval_ms("1fortnight")


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #


async def test_fetch_klines_populates_the_cache():
    bars = make_bars("BTCUSD", 30)
    rest = FakeRest(bars)
    got = await data.fetch_klines(
        rest, "BTCUSD", "1m", bars[0].open_ms, bars[-1].close_ms, sleep_s=0
    )
    assert got == bars
    assert data.load_cached("BTCUSD", "1m") == bars


async def test_fetch_klines_only_fetches_the_missing_tail():
    bars = make_bars("BTCUSD", 40)
    start, end = bars[0].open_ms, bars[-1].close_ms

    first = FakeRest(bars[:20])
    await data.fetch_klines(rest := first, "BTCUSD", "1m", start, end, sleep_s=0)
    assert rest.requests[0][0] == start

    second = FakeRest(bars)
    got = await data.fetch_klines(second, "BTCUSD", "1m", start, end, sleep_s=0)

    # The resume cursor must start after the last cached bar, not at the top.
    assert second.requests[0][0] == bars[19].close_ms + 1
    assert got == bars
    assert data.load_cached("BTCUSD", "1m") == bars


async def test_fetch_klines_refetches_when_the_cache_has_a_leading_hole():
    bars = make_bars("BTCUSD", 30)
    data.save_cache("BTCUSD", "1m", bars[15:])  # tail only, nothing at the front
    rest = FakeRest(bars)
    got = await data.fetch_klines(
        rest, "BTCUSD", "1m", bars[0].open_ms, bars[-1].close_ms, sleep_s=0
    )
    assert rest.requests[0][0] == bars[0].open_ms
    assert got == bars


async def test_fetch_klines_pages_and_sleeps_between_pages(monkeypatch):
    bars = make_bars("BTCUSD", 25)
    rest = FakeRest(bars)
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(data.asyncio, "sleep", fake_sleep)
    got = await data.fetch_klines(
        rest, "BTCUSD", "1m", bars[0].open_ms, bars[-1].close_ms, page_limit=10
    )
    assert got == bars
    assert len(rest.requests) == 3          # 10 + 10 + 5 (short page ends the loop)
    assert sleeps == [data.PAGE_SLEEP_SECONDS] * 2


async def test_fetch_klines_returns_only_the_requested_window():
    bars = make_bars("BTCUSD", 40)
    data.save_cache("BTCUSD", "1m", bars)
    rest = FakeRest(bars)
    got = await data.fetch_klines(
        rest, "BTCUSD", "1m", bars[10].open_ms, bars[19].close_ms, sleep_s=0
    )
    assert got == bars[10:20]


async def test_fetch_klines_survives_an_empty_exchange_response():
    rest = FakeRest([])
    got = await data.fetch_klines(rest, "BTCUSD", "1m", T0, T0 + 100 * MINUTE, sleep_s=0)
    assert got == []


async def test_fetch_klines_does_not_spin_when_the_cursor_stalls():
    """A page that does not advance the cursor must end the loop, not loop forever."""
    stuck = make_bars("BTCUSD", 1)

    class StuckRest(FakeRest):
        async def klines(self, symbol, interval="1m", *, start_ms=None, end_ms=None, limit=1000):
            self.requests.append((start_ms, end_ms))
            return stuck  # always the same first bar

    rest = StuckRest(stuck)
    got = await data.fetch_klines(
        rest, "BTCUSD", "1m", T0, T0 + 500 * MINUTE, sleep_s=0, page_limit=1
    )
    assert len(rest.requests) <= 3
    assert got == stuck


def test_cache_file_is_newline_delimited_json(cache_dir):
    bars = make_bars("BTCUSD", 3)
    path = data.save_cache("BTCUSD", "1m", bars)
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 3
    assert json.loads(lines[0])["open_ms"] == bars[0].open_ms
