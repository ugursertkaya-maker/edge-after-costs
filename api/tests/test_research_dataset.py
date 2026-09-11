"""Offline tests for the tape cache and the multi-timeframe bar builder.

No network anywhere: the downloader is driven through an ``httpx.MockTransport``
that serves a fixed aggTrades tape and records every request, which is how the
"only fetch the missing tail" behaviour is proved rather than asserted.

The bar rules tested here are the ones the whole research stack rests on:
absolute bucket boundaries, empty bars across gaps, taker-buy accumulation, and
the incomplete final bucket never being emitted as closed.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.exchange.rest import BinanceUSRest
from app.research import dataset as ds
from app.types import Trade

SECOND = 1_000
MINUTE = 60_000
#: 2023-11-14T22:13:20Z — a round epoch that is NOT on a minute boundary at the
#: second level, so bucket-boundary bugs cannot hide behind a lucky alignment.
T0 = 1_700_000_000_000
SYM = "BTCUSD"


def trade(ts_ms: int, price: float, qty: float = 1.0, *, maker: bool = True) -> Trade:
    return Trade(symbol=SYM, price=price, qty=qty, ts_ms=ts_ms, buyer_is_maker=maker)


# --------------------------------------------------------------------------- #
# build_bars — bucketing
# --------------------------------------------------------------------------- #


def test_buckets_are_absolute_epoch_floors_not_relative_to_first_trade():
    # First trade lands 17 seconds into a minute.  The bar must still open on
    # the minute, or two runs over different windows would disagree.
    base = (T0 // MINUTE) * MINUTE
    trades = [
        trade(base + 17 * SECOND, 100.0),
        trade(base + 59 * SECOND, 101.0),
        trade(base + MINUTE + 1, 102.0),
        trade(base + 2 * MINUTE + 1, 103.0),
    ]
    bars = ds.build_bars(trades, "1m")

    assert [b.open_ms for b in bars] == [base, base + MINUTE]
    assert [b.close_ms for b in bars] == [base + MINUTE - 1, base + 2 * MINUTE - 1]


def test_sub_minute_intervals_bucket_on_their_own_grid():
    base = (T0 // MINUTE) * MINUTE
    trades = [trade(base + i * SECOND, 100.0 + i) for i in range(0, 31)]
    bars = ds.build_bars(trades, "5s")

    assert bars[0].open_ms == base
    assert all(b.open_ms % 5_000 == 0 for b in bars)
    assert all(b.close_ms - b.open_ms == 4_999 for b in bars)


def test_ohlcv_is_the_bucket_not_the_tape():
    base = (T0 // MINUTE) * MINUTE
    trades = [
        trade(base + 1, 100.0, 2.0),
        trade(base + 2, 105.0, 1.0),
        trade(base + 3, 95.0, 3.0),
        trade(base + 4, 99.0, 4.0),
        trade(base + MINUTE + 1, 120.0),  # next bucket: forces the first to close
    ]
    bar = ds.build_bars(trades, "1m")[0]

    assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 105.0, 95.0, 99.0)
    assert bar.volume == pytest.approx(10.0)
    assert bar.quote_volume == pytest.approx(100.0 * 2 + 105.0 + 95.0 * 3 + 99.0 * 4)
    assert bar.trades == 4


def test_interval_ms_rejects_anything_outside_the_supported_set():
    assert ds.interval_ms("15s") == 15_000
    assert ds.interval_ms("15m") == 900_000
    with pytest.raises(ValueError):
        ds.interval_ms("1h")


# --------------------------------------------------------------------------- #
# build_bars — the empty-bar rule
# --------------------------------------------------------------------------- #


def test_gap_minutes_are_emitted_as_empty_bars_carrying_the_previous_close():
    # 55% of BTC/USD minutes contain no trade.  Skipping them would turn "ten
    # silent minutes" into "two consecutive prints" and corrupt every return.
    base = (T0 // MINUTE) * MINUTE
    trades = [
        trade(base + 1, 100.0),
        trade(base + 10 * MINUTE + 1, 110.0),
        trade(base + 11 * MINUTE + 1, 111.0),
    ]
    bars = ds.build_bars(trades, "1m")

    assert len(bars) == 11  # minutes 0..10; minute 11 is still open
    gaps = bars[1:10]
    assert all(b.trades == 0 for b in gaps)
    assert all(b.volume == 0.0 and b.quote_volume == 0.0 for b in gaps)
    assert all(b.taker_buy_quote == 0.0 for b in gaps)
    assert all(
        (b.open, b.high, b.low, b.close) == (100.0, 100.0, 100.0, 100.0) for b in gaps
    )
    # No missing minutes anywhere in the series.
    assert [b.open_ms for b in bars] == [base + i * MINUTE for i in range(11)]


def test_empty_bar_close_is_the_previous_close_not_zero():
    base = (T0 // MINUTE) * MINUTE
    trades = [trade(base + 1, 100.0), trade(base + 3 * MINUTE + 1, 100.0)]
    bars = ds.build_bars(trades, "1m")
    assert [b.close for b in bars] == [100.0, 100.0, 100.0]


# --------------------------------------------------------------------------- #
# build_bars — taker_buy_quote and the open bucket
# --------------------------------------------------------------------------- #


def test_taker_buy_quote_accumulates_only_buyer_aggressor_notional():
    base = (T0 // MINUTE) * MINUTE
    trades = [
        trade(base + 1, 100.0, 2.0, maker=False),  # aggressor was the BUYER
        trade(base + 2, 100.0, 3.0, maker=True),   # aggressor was the SELLER
        trade(base + 3, 50.0, 4.0, maker=False),
        trade(base + MINUTE + 1, 100.0),
    ]
    bar = ds.build_bars(trades, "1m")[0]

    assert bar.taker_buy_quote == pytest.approx(100.0 * 2 + 50.0 * 4)
    assert bar.quote_volume == pytest.approx(100.0 * 2 + 100.0 * 3 + 50.0 * 4)
    assert bar.taker_buy_quote < bar.quote_volume


def test_the_incomplete_last_bucket_is_dropped():
    base = (T0 // MINUTE) * MINUTE
    trades = [trade(base + 1, 100.0), trade(base + MINUTE + 1, 101.0)]
    bars = ds.build_bars(trades, "1m")

    # The second minute holds a print but has not closed; emitting it would
    # hand the feature engine a partially-observed bar.
    assert len(bars) == 1
    assert bars[0].open_ms == base


def test_a_tape_inside_one_bucket_produces_no_closed_bars():
    base = (T0 // MINUTE) * MINUTE
    trades = [trade(base + i, 100.0 + i) for i in range(5)]
    assert ds.build_bars(trades, "1m") == []


def test_empty_tape_produces_no_bars():
    assert ds.build_bars([], "1m") == []


def test_unsorted_input_is_sorted_rather_than_mis_bucketed():
    base = (T0 // MINUTE) * MINUTE
    ordered = [trade(base + i * 20 * SECOND, 100.0 + i) for i in range(7)]
    shuffled = [ordered[3], ordered[0], ordered[6], ordered[1], ordered[5], ordered[2], ordered[4]]
    assert ds.build_bars(shuffled, "1m") == ds.build_bars(ordered, "1m")


def test_bucket_stats_agrees_with_build_bars():
    base = (T0 // MINUTE) * MINUTE
    trades = [trade(base + 1, 100.0), trade(base + 7 * MINUTE + 1, 101.0), trade(base + 9 * MINUTE + 1, 102.0)]
    total, empty = ds.bucket_stats(trades, "1m")
    bars = ds.build_bars(trades, "1m")

    assert total == len(bars)
    assert empty == sum(1 for b in bars if b.trades == 0)


# --------------------------------------------------------------------------- #
# The cache
# --------------------------------------------------------------------------- #


def test_cache_round_trip_preserves_every_print(tmp_path):
    rows = [
        ds.AggTrade(agg_id=1, price=100.5, qty=0.25, ts_ms=T0, buyer_is_maker=True),
        ds.AggTrade(agg_id=2, price=100.75, qty=1.5, ts_ms=T0 + 5, buyer_is_maker=False),
    ]
    path = tmp_path / "BTCUSD" / "day.ndjson"
    ds._write_day(path, rows)
    assert ds._read_day(path) == rows


def test_a_truncated_final_line_costs_one_print_not_the_day(tmp_path):
    path = tmp_path / "BTCUSD" / "day.ndjson"
    path.parent.mkdir(parents=True)
    good = json.dumps({"a": 1, "p": 100.0, "q": 1.0, "T": T0, "m": 1})
    path.write_text(good + "\n" + '{"a":2,"p":10', encoding="utf-8")

    rows = ds._read_day(path)
    assert [r.agg_id for r in rows] == [1]


def test_coverage_merge_refuses_to_claim_a_hole():
    assert ds._merge_coverage((0, 100), (101, 200)) == (0, 200)   # touching
    assert ds._merge_coverage((0, 100), (50, 200)) == (0, 200)    # overlapping
    # Disjoint: claiming (0, 400) would assert we hold 101..299, which we do
    # not.  The new window wins and the gap is re-fetched next time.
    assert ds._merge_coverage((0, 100), (300, 400)) == (300, 400)


# --------------------------------------------------------------------------- #
# The downloader
# --------------------------------------------------------------------------- #


class FakeAggTape:
    """Serves a fixed aggTrades tape and records every request it answered.

    Mimics the two Binance.US paging modes that matter: a ``startTime``/
    ``endTime`` window (capped at one hour by the caller, not by us) and dense
    ``fromId`` paging.
    """

    def __init__(self, rows: list[dict]) -> None:
        self.rows = sorted(rows, key=lambda r: r["a"])
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/v3/aggTrades":
            return httpx.Response(404, json={"msg": "unexpected path"})
        params = {k: v for k, v in request.url.params.items()}
        self.requests.append(params)
        limit = int(params.get("limit", 500))
        if "fromId" in params:
            from_id = int(params["fromId"])
            page = [r for r in self.rows if r["a"] >= from_id][:limit]
        else:
            start = int(params["startTime"])
            end = int(params["endTime"])
            page = [r for r in self.rows if start <= r["T"] <= end][:limit]
        return httpx.Response(200, json=page)

    def rest(self, *, max_attempts: int = 5) -> BinanceUSRest:
        return BinanceUSRest(
            client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)),
            max_attempts=max_attempts,
        )


def make_rows(count: int, *, start_ms: int = T0, step_ms: int = MINUTE) -> list[dict]:
    return [
        {
            "a": 5_000 + i,
            "p": 100.0 + i * 0.01,
            "q": 0.5,
            "T": start_ms + i * step_ms,
            "m": bool(i % 2),
        }
        for i in range(count)
    ]


async def test_download_writes_a_day_file_and_returns_the_window(tmp_path):
    rows = make_rows(30)
    tape = FakeAggTape(rows)
    start, end = T0, T0 + 29 * MINUTE

    trades = await ds.download_agg_trades(
        tape.rest(), SYM, start, end, cache_dir=tmp_path, sleep_s=0.0
    )

    assert len(trades) == 30
    assert [t.ts_ms for t in trades] == [r["T"] for r in rows]
    assert trades[0].buyer_is_maker is False  # i=0 -> m False
    day_file = tmp_path / SYM / f"{ds.day_key(T0)}.ndjson"
    assert day_file.exists()
    assert (tmp_path / SYM / f"{ds.day_key(T0)}.meta.json").exists()


async def test_first_window_is_time_bounded_then_paging_switches_to_from_id(tmp_path):
    tape = FakeAggTape(make_rows(40))
    await ds.download_agg_trades(
        tape.rest(), SYM, T0, T0 + 39 * MINUTE, cache_dir=tmp_path, sleep_s=0.0
    )

    first = tape.requests[0]
    assert "startTime" in first and "fromId" not in first
    # The startTime window must never exceed the exchange's one-hour cap.
    assert int(first["endTime"]) - int(first["startTime"]) < ds.WINDOW_MS
    assert any("fromId" in r for r in tape.requests[1:])


async def test_rerunning_fetches_nothing_when_the_window_is_already_cached(tmp_path):
    tape = FakeAggTape(make_rows(30))
    start, end = T0, T0 + 29 * MINUTE

    first = await ds.download_agg_trades(
        tape.rest(), SYM, start, end, cache_dir=tmp_path, sleep_s=0.0
    )
    calls_after_first = len(tape.requests)
    assert calls_after_first > 0

    second = await ds.download_agg_trades(
        tape.rest(), SYM, start, end, cache_dir=tmp_path, sleep_s=0.0
    )

    assert len(tape.requests) == calls_after_first  # zero new requests
    assert [t.ts_ms for t in second] == [t.ts_ms for t in first]


async def test_resume_fetches_only_the_missing_tail(tmp_path):
    rows = make_rows(120)
    tape = FakeAggTape(rows)
    head_end = T0 + 59 * MINUTE
    full_end = T0 + 119 * MINUTE

    await ds.download_agg_trades(
        tape.rest(), SYM, T0, head_end, cache_dir=tmp_path, sleep_s=0.0
    )
    tape.requests.clear()

    full = await ds.download_agg_trades(
        tape.rest(), SYM, T0, full_end, cache_dir=tmp_path, sleep_s=0.0
    )

    assert len(full) == 120
    assert tape.requests, "the tail should still have been fetched"
    # Nothing may be requested from before the cached coverage ended.
    for req in tape.requests:
        if "startTime" in req:
            assert int(req["startTime"]) > head_end


async def test_a_quiet_window_advances_by_the_hour_cap_and_terminates(tmp_path):
    # An hour with no prints is normal here; the downloader must step over it
    # rather than spin, and must not invent coverage it does not have.
    tape = FakeAggTape([])
    trades = await ds.download_agg_trades(
        tape.rest(), SYM, T0, T0 + 3 * ds.WINDOW_MS, cache_dir=tmp_path, sleep_s=0.0
    )

    assert trades == []
    assert 1 <= len(tape.requests) <= 5
    assert all("startTime" in r for r in tape.requests)


async def test_use_cache_false_never_touches_disk(tmp_path):
    tape = FakeAggTape(make_rows(10))
    trades = await ds.download_agg_trades(
        tape.rest(),
        SYM,
        T0,
        T0 + 9 * MINUTE,
        cache_dir=tmp_path,
        sleep_s=0.0,
        use_cache=False,
    )
    assert len(trades) == 10
    assert not (tmp_path / SYM).exists()


# --------------------------------------------------------------------------- #
# load_dataset
# --------------------------------------------------------------------------- #


async def test_load_dataset_assembles_every_interval_from_the_cache(tmp_path):
    tape = FakeAggTape(make_rows(240, step_ms=15 * SECOND))
    start, end = T0, T0 + 240 * 15 * SECOND
    await ds.download_agg_trades(
        tape.rest(), SYM, start, end, cache_dir=tmp_path, sleep_s=0.0
    )
    ds.clear_dataset_cache()

    dataset = ds.load_dataset(
        SYM, start, end, intervals=("15s", "1m", "5m"), cache_dir=tmp_path
    )

    assert dataset.symbol == SYM
    assert set(dataset.bars) == {"15s", "1m", "5m"}
    assert len(dataset.trades) == 240
    assert len(dataset.bars["15s"]) > len(dataset.bars["1m"]) > len(dataset.bars["5m"])
    assert dataset.bars["1m"][0].open_ms % MINUTE == 0


def test_load_dataset_memoises_so_the_tournament_reloads_instantly(tmp_path):
    ds.clear_dataset_cache()
    first = ds.load_dataset(SYM, T0, T0 + MINUTE, intervals=("1m",), cache_dir=tmp_path)
    second = ds.load_dataset(SYM, T0, T0 + MINUTE, intervals=("1m",), cache_dir=tmp_path)
    assert first is second
    ds.clear_dataset_cache()
    assert ds.load_dataset(SYM, T0, T0 + MINUTE, intervals=("1m",), cache_dir=tmp_path) is not first


def test_load_dataset_rejects_an_unsupported_interval(tmp_path):
    with pytest.raises(ValueError):
        ds.load_dataset(SYM, T0, T0 + MINUTE, intervals=("1h",), cache_dir=tmp_path)


class ExplodingTape(FakeAggTape):
    """Serves ``fail_after`` requests, then dies — a killed download."""

    def __init__(self, rows: list[dict], fail_after: int) -> None:
        super().__init__(rows)
        self.fail_after = fail_after

    def handler(self, request: httpx.Request) -> httpx.Response:
        if len(self.requests) >= self.fail_after:
            raise httpx.ConnectError("boom", request=request)
        return super().handler(request)


async def test_a_killed_download_keeps_the_chunks_it_already_finished(tmp_path):
    # 6 days of prints, flushed every 2.  The kill lands after the first
    # chunks, and the point of chunked flushing is that those survive.
    rows = make_rows(6 * 24 * 6, step_ms=10 * MINUTE)
    end = T0 + 6 * ds.DAY_MS - MINUTE
    tape = ExplodingTape(rows, fail_after=6)

    with pytest.raises(httpx.ConnectError):
        await ds.download_agg_trades(
            tape.rest(max_attempts=1), SYM, T0, end,
            cache_dir=tmp_path, sleep_s=0.0, chunk_days=2,
        )

    salvaged = ds.load_cached_trades(SYM, T0, end, cache_dir=tmp_path)
    assert salvaged, "a killed download must not throw away completed chunks"

    # And the resume picks up from the hole, not from the start.
    fresh = FakeAggTape(rows)
    full = await ds.download_agg_trades(
        fresh.rest(), SYM, T0, end, cache_dir=tmp_path, sleep_s=0.0, chunk_days=2
    )
    assert len(full) == len(rows)
    first_start = min(int(r["startTime"]) for r in fresh.requests if "startTime" in r)
    assert first_start > T0
