"""Historical tape download, a resumable cache, and multi-timeframe bars.

Why this module exists at all
-----------------------------

On a normal venue the full trade tape is far too large to keep on a laptop, so
research is done on bars and sub-minute behaviour is *synthesised* from them —
which is exactly the assumption that makes short-horizon backtests lie.

Binance.US is not a normal venue.  ``docs/ECONOMICS.md`` measured ~3,000 trades
a day on BTC/USD and 55% of one-minute bars containing **no trade at all**.  A
year of the entire public tape for four pairs is therefore a few hundred
megabytes of NDJSON.  That thinness is a research *advantage*: we can work from
the real prints instead of inventing them.

Three behaviours in here matter more than the rest:

* **Empty bars are emitted.**  On a venue where most minutes are silent,
  skipping silent buckets renames "nothing happened for twenty minutes" into
  "the last twenty prints were consecutive", which corrupts every return series
  and every rolling window downstream.  A gap bar carries the previous close as
  O/H/L/C with zero volume and zero trades.
* **Bucketing is by ``floor(ts / interval_ms)``.**  Absolute epoch buckets, not
  buckets relative to the first trade, so two runs over different windows of the
  same tape produce byte-identical bars on their overlap.
* **The last bucket is dropped.**  The bucket containing the final trade is
  still open; emitting it as closed would leak a partially-observed bar into
  the feature engine.

Paging aggTrades
----------------

``/api/v3/aggTrades`` caps a ``startTime``/``endTime`` window at one hour, but
aggregate-trade ids are dense and monotonic per symbol, so after the first
window we page with ``fromId`` and cover many hours per request.  That is the
difference between ~24 requests per day and ~4.

The cache is newline-delimited JSON, one file per symbol per **UTC day**, with a
sidecar recording the time range that file is known to cover.  Coverage is
tracked explicitly because "the file has no trades in hour 7" and "we never
downloaded hour 7" look identical on a tape this quiet, and guessing wrong in
either direction is a silent data bug.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..config import Settings
from ..types import Bar, OrderBook, Trade, now_ms

__all__ = [
    "CHUNK_DAYS",
    "DAY_MS",
    "DEFAULT_TAPE_DIR",
    "INTERVALS",
    "MAX_PAGES",
    "PAGE_LIMIT",
    "PAGE_SLEEP_SECONDS",
    "WINDOW_MS",
    "AggTrade",
    "Dataset",
    "bucket_stats",
    "build_bars",
    "clear_dataset_cache",
    "day_bounds",
    "day_key",
    "download_agg_trades",
    "interval_ms",
    "load_cached_trades",
    "load_dataset",
    "tape_root",
]

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Every interval the research stack is allowed to ask for.  Deliberately a
#: closed set: an interval nobody tested is an interval nobody should trust.
INTERVALS: tuple[str, ...] = ("5s", "15s", "30s", "1m", "3m", "5m", "15m")

_INTERVAL_MS: dict[str, int] = {
    "5s": 5_000,
    "15s": 15_000,
    "30s": 30_000,
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
}

DAY_MS = 86_400_000
#: Binance caps an aggTrades startTime/endTime window at exactly one hour.
WINDOW_MS = 3_600_000
#: Rows per page; Binance's hard maximum.
PAGE_LIMIT = 1000
#: Guard against a pathological paging loop.  A 365-day BTC/USD pull is roughly
#: 1,100 full pages, so this leaves generous headroom without being unbounded.
MAX_PAGES = 20_000
#: UTC days fetched before progress is flushed to disk.  Small enough that
#: a killed 365-day pull loses minutes, large enough that chunk boundaries
#: cost almost nothing.
CHUNK_DAYS = 3

#: Politeness pause between pages.  A research download is never latency
#: critical and we would much rather never see a 429 than save five minutes.
PAGE_SLEEP_SECONDS = 0.2

DEFAULT_TAPE_DIR = "tape"


def interval_ms(interval: str) -> int:
    """``"15s"`` -> 15000.  Raises for anything outside :data:`INTERVALS`."""
    try:
        return _INTERVAL_MS[interval]
    except KeyError:
        raise ValueError(
            f"unsupported interval {interval!r}; expected one of {INTERVALS}"
        ) from None


# --------------------------------------------------------------------------- #
# The cross-module contract
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Dataset:
    """One symbol's research data: every timeframe plus the raw tape.

    ``books`` is an optional extension: most of the historical tape has no
    order-book history (Binance publishes no depth archive), so the feature
    engine *omits* book-derived features rather than fabricating them.  When a
    caller does have book snapshots — the live recorder writes them — they can
    be attached here and the book features light up.
    """

    symbol: str
    bars: dict[str, list[Bar]]
    trades: list[Trade]
    start_ms: int
    end_ms: int
    books: tuple[OrderBook, ...] = ()

    def interval_bars(self, interval: str) -> list[Bar]:
        return self.bars.get(interval, [])


@dataclass(frozen=True)
class AggTrade:
    """One raw aggTrades row, *with* its id — which ``Trade`` does not carry.

    The id is what makes paging and deduplication exact, so the cache stores it
    and only converts to the frozen :class:`~app.types.Trade` at the boundary.
    """

    agg_id: int
    price: float
    qty: float
    ts_ms: int
    buyer_is_maker: bool

    def to_trade(self, symbol: str) -> Trade:
        return Trade(
            symbol=symbol,
            price=self.price,
            qty=self.qty,
            ts_ms=self.ts_ms,
            buyer_is_maker=self.buyer_is_maker,
        )


# --------------------------------------------------------------------------- #
# build_bars — pure, and the piece everything else rests on
# --------------------------------------------------------------------------- #


def _is_sorted(trades: Sequence[Trade]) -> bool:
    prev = -(2**63)
    for t in trades:
        if t.ts_ms < prev:
            return False
        prev = t.ts_ms
    return True


def build_bars(trades: list[Trade], interval: str) -> list[Bar]:
    """Aggregate a time-ordered tape into closed OHLCV bars.  Pure.

    Rules, all of which are load-bearing:

    * Buckets are ``floor(ts_ms / interval_ms) * interval_ms`` — absolute epoch
      buckets, so boundaries are stable across runs and across windows.
    * **Every** bucket between the first and the last trade is emitted, gaps
      included.  A gap bar is ``O=H=L=C=previous close``, zero volume, zero
      trades, zero ``taker_buy_quote``.
    * ``taker_buy_quote`` accumulates the notional of trades where
      ``buyer_is_maker`` is False (the aggressor was the *buyer*).
    * The bucket containing the last trade is still open and is **dropped**.

    An empty tape yields no bars.  A tape entirely inside one bucket yields no
    bars either — which is correct, not a bug: nothing has closed yet.
    """
    step = interval_ms(interval)
    if not trades:
        return []

    ordered: Sequence[Trade] = (
        trades if _is_sorted(trades) else sorted(trades, key=lambda t: t.ts_ms)
    )
    symbol = ordered[0].symbol
    first_bucket = (ordered[0].ts_ms // step) * step
    open_bucket = (ordered[-1].ts_ms // step) * step  # incomplete -> never emitted

    bars: list[Bar] = []
    prev_close = float(ordered[0].price)
    i = 0
    n = len(ordered)
    bucket = first_bucket

    while bucket < open_bucket:
        end = bucket + step
        o = h = l = c = 0.0
        volume = 0.0
        quote_volume = 0.0
        taker_buy_quote = 0.0
        count = 0
        while i < n and ordered[i].ts_ms < end:
            t = ordered[i]
            price = float(t.price)
            qty = float(t.qty)
            notional = price * qty
            if count == 0:
                o = h = l = price
            elif price > h:
                h = price
            elif price < l:
                l = price
            c = price
            volume += qty
            quote_volume += notional
            if not t.buyer_is_maker:
                taker_buy_quote += notional
            count += 1
            i += 1

        if count == 0:
            o = h = l = c = prev_close

        bars.append(
            Bar(
                symbol=symbol,
                open_ms=bucket,
                close_ms=end - 1,
                open=o,
                high=h,
                low=l,
                close=c,
                volume=volume,
                quote_volume=quote_volume,
                trades=count,
                taker_buy_quote=taker_buy_quote,
            )
        )
        prev_close = c
        bucket = end

    return bars


def bucket_stats(trades: Sequence[Trade], interval: str) -> tuple[int, int]:
    """``(closed buckets, empty buckets)`` without materialising any ``Bar``.

    The download CLI reports the empty fraction for every interval and that is
    itself a finding; doing it through :func:`build_bars` would allocate two
    million objects for a 5-second view of a four-month tape.
    """
    step = interval_ms(interval)
    if not trades:
        return 0, 0
    first = min(t.ts_ms for t in trades) // step
    last = max(t.ts_ms for t in trades) // step
    total = max(0, last - first)  # the final, open bucket is not counted
    if total == 0:
        return 0, 0
    occupied = set()
    for t in trades:
        b = t.ts_ms // step
        if b < last:
            occupied.add(b)
    return total, total - len(occupied)


# --------------------------------------------------------------------------- #
# Cache layout
# --------------------------------------------------------------------------- #


def day_key(ms: int) -> str:
    """UTC day label for an epoch-ms instant, e.g. ``"2026-05-04"``."""
    import datetime as _dt

    return _dt.datetime.fromtimestamp(
        (ms // DAY_MS) * DAY_MS / 1000.0, tz=_dt.timezone.utc
    ).strftime("%Y-%m-%d")


def day_bounds(ms: int) -> tuple[int, int]:
    """``(inclusive start, inclusive end)`` of the UTC day containing ``ms``."""
    start = (ms // DAY_MS) * DAY_MS
    return start, start + DAY_MS - 1


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def tape_root(cache_dir: str | Path | None = None) -> Path:
    """Directory holding the tape cache, falling back to a temp dir.

    Mirrors ``app.backtest.data.cache_root``: the configured ``/data`` path does
    not exist on a laptop, and a research download that re-fetches is merely
    slow while one that crashes is useless.
    """
    if cache_dir is not None:
        candidate = Path(cache_dir).expanduser()
    else:
        configured = Settings().backtest.cache_dir or "/data/backtest-cache"
        candidate = Path(configured).expanduser() / DEFAULT_TAPE_DIR
    if not _writable(candidate):
        candidate = Path(tempfile.gettempdir()) / "crypto-trader-tape-cache"
        candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def _day_paths(root: Path, symbol: str, day_start_ms: int) -> tuple[Path, Path]:
    folder = root / symbol.upper()
    label = day_key(day_start_ms)
    return folder / f"{label}.ndjson", folder / f"{label}.meta.json"


# --------------------------------------------------------------------------- #
# Pure file operations
# --------------------------------------------------------------------------- #


def _read_day(path: Path) -> list[AggTrade]:
    """Read one day file.  A truncated final line is skipped, never raised.

    A killed download leaves a partial line behind; losing one print is much
    better than losing the day.
    """
    if not path.exists():
        return []
    rows: list[AggTrade] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                rows.append(
                    AggTrade(
                        agg_id=int(obj["a"]),
                        price=float(obj["p"]),
                        qty=float(obj["q"]),
                        ts_ms=int(obj["T"]),
                        buyer_is_maker=bool(obj["m"]),
                    )
                )
            except (ValueError, KeyError, TypeError):
                continue
    rows.sort(key=lambda r: (r.ts_ms, r.agg_id))
    return rows


def _write_day(path: Path, rows: Sequence[AggTrade]) -> None:
    """Atomically replace one day file (temp file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(
                json.dumps(
                    {
                        "a": r.agg_id,
                        "p": r.price,
                        "q": r.qty,
                        "T": r.ts_ms,
                        "m": 1 if r.buyer_is_maker else 0,
                    },
                    separators=(",", ":"),
                )
            )
            fh.write("\n")
    tmp.replace(path)


def _read_meta(path: Path) -> tuple[int, int] | None:
    """``(covered_from_ms, covered_to_ms)`` for a day file, or None."""
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        lo = int(obj["covered_from_ms"])
        hi = int(obj["covered_to_ms"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return (lo, hi) if hi >= lo else None


def _write_meta(path: Path, coverage: tuple[int, int], trades: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(
            {
                "covered_from_ms": int(coverage[0]),
                "covered_to_ms": int(coverage[1]),
                "trades": int(trades),
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    tmp.replace(path)


def _dedupe(rows: Iterable[AggTrade]) -> list[AggTrade]:
    by_id: dict[int, AggTrade] = {}
    for r in rows:
        by_id[r.agg_id] = r
    return sorted(by_id.values(), key=lambda r: (r.ts_ms, r.agg_id))


def _merge_coverage(
    old: tuple[int, int] | None, new: tuple[int, int]
) -> tuple[int, int]:
    """Union of two coverage intervals, but only when they actually touch.

    Two disjoint intervals have a hole between them, and claiming the hole is
    covered is precisely the bug this whole sidecar exists to prevent — so a
    disjoint merge keeps the *new* interval and forgets the old claim.  The
    rows themselves are never dropped, so the cost is a future re-fetch.
    """
    if old is None:
        return new
    if new[0] <= old[1] + 1 and old[0] <= new[1] + 1:
        return (min(old[0], new[0]), max(old[1], new[1]))
    return new


# --------------------------------------------------------------------------- #
# Reading the cache back
# --------------------------------------------------------------------------- #


def load_cached_trades(
    symbol: str,
    start_ms: int,
    end_ms: int,
    *,
    cache_dir: str | Path | None = None,
) -> list[Trade]:
    """Every cached print for ``symbol`` in ``[start_ms, end_ms]``.  No network."""
    root = tape_root(cache_dir)
    sym = symbol.upper()
    out: list[Trade] = []
    day = (int(start_ms) // DAY_MS) * DAY_MS
    while day <= int(end_ms):
        path, _ = _day_paths(root, sym, day)
        for row in _read_day(path):
            if start_ms <= row.ts_ms <= end_ms:
                out.append(row.to_trade(sym))
        day += DAY_MS
    out.sort(key=lambda t: t.ts_ms)
    return out


def cached_coverage(
    symbol: str,
    start_ms: int,
    end_ms: int,
    *,
    cache_dir: str | Path | None = None,
) -> int:
    """First millisecond of ``[start_ms, end_ms]`` we do **not** already hold.

    Walks day sidecars forward from ``start_ms`` and stops at the first hole, so
    a resumed download fetches exactly the missing tail.  Returns
    ``end_ms + 1`` when the whole window is cached.
    """
    root = tape_root(cache_dir)
    sym = symbol.upper()
    cursor = int(start_ms)
    end = int(end_ms)
    while cursor <= end:
        day_start, day_end = day_bounds(cursor)
        _, meta_path = _day_paths(root, sym, day_start)
        cov = _read_meta(meta_path)
        if cov is None or cov[0] > cursor or cov[1] < cursor:
            return cursor
        cursor = cov[1] + 1
    return cursor


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #


async def _sleep(seconds: float) -> None:
    """Indirection so tests can neutralise the politeness pause."""
    if seconds > 0:
        await asyncio.sleep(seconds)


def _parse_row(row: Any) -> AggTrade | None:
    try:
        return AggTrade(
            agg_id=int(row["a"]),
            price=float(row["p"]),
            qty=float(row["q"]),
            ts_ms=int(row["T"]),
            buyer_is_maker=bool(row["m"]),
        )
    except (TypeError, ValueError, KeyError, IndexError):
        return None


async def _get_page(rest: Any, params: dict[str, Any]) -> list[dict]:
    """One raw ``/api/v3/aggTrades`` page.

    We go through the client's own ``_get`` rather than its public
    :meth:`~app.exchange.rest.BinanceUSRest.agg_trades`, because that helper
    maps rows onto :class:`~app.types.Trade` and drops the aggregate-trade id —
    and the id is the only thing that makes paging past the one-hour window cap
    possible.  Reusing ``_get`` keeps the retry, ``Retry-After`` handling and
    concurrency semaphore that module already implements.
    """
    getter = getattr(rest, "agg_trades_raw", None)
    if getter is not None:
        rows = await getter(params)
    else:
        rows = await rest._get("/api/v3/aggTrades", params)
    return [r for r in (rows or []) if isinstance(r, dict)]


async def _fetch_range(
    rest: Any,
    symbol: str,
    from_ms: int,
    to_ms: int,
    *,
    sleep_s: float,
    max_pages: int,
    on_page: Any = None,
) -> list[AggTrade]:
    """Page ``[from_ms, to_ms]`` of the tape, newest-first paging by id.

    The first request uses a one-hour ``startTime``/``endTime`` window because
    that is all the endpoint allows without an id.  Once a single print is in
    hand we switch to ``fromId``, which is not time-capped and therefore covers
    as many hours as it takes to fill a page — the difference between 24
    requests per day and about 4 on BTC/USD, and far more on a quiet pair.
    """
    sym = symbol.upper()
    out: list[AggTrade] = []
    cursor = int(from_ms)
    end = int(to_ms)
    from_id: int | None = None
    pages = 0

    while pages < max_pages:
        id_mode = from_id is not None
        if id_mode:
            params: dict[str, Any] = {
                "symbol": sym,
                "fromId": from_id,
                "limit": PAGE_LIMIT,
            }
        else:
            params = {
                "symbol": sym,
                "startTime": cursor,
                "endTime": min(cursor + WINDOW_MS - 1, end),
                "limit": PAGE_LIMIT,
            }

        rows = await _get_page(rest, params)
        pages += 1
        parsed = [p for p in (_parse_row(r) for r in rows) if p is not None]

        if not parsed:
            if id_mode:
                break  # tip of the tape
            cursor += WINDOW_MS
            if cursor > end:
                break
            await _sleep(sleep_s)
            continue

        past_end = False
        for p in parsed:
            if p.ts_ms > end:
                past_end = True
                break
            if p.ts_ms >= from_ms:
                out.append(p)

        if on_page is not None:
            on_page(len(out), parsed[-1].ts_ms)

        if past_end:
            break

        next_id = parsed[-1].agg_id + 1
        if from_id is not None and next_id <= from_id:
            break  # the exchange refused to advance; stop rather than spin
        from_id = next_id
        if id_mode and len(rows) < PAGE_LIMIT:
            break  # a short id page means we reached the live tip
        await _sleep(sleep_s)

    return _dedupe(out)


def _persist(
    root: Path,
    symbol: str,
    rows: Sequence[AggTrade],
    from_ms: int,
    to_ms: int,
) -> None:
    """Write ``rows`` into their UTC-day files and record what is now covered.

    Coverage is clamped to "now": a window whose end is in the future is not
    missing data, it is data that has not happened yet, and claiming it would
    make a later re-run skip the real tail.
    """
    horizon = min(int(to_ms), now_ms())
    by_day: dict[int, list[AggTrade]] = {}
    for r in rows:
        by_day.setdefault((r.ts_ms // DAY_MS) * DAY_MS, []).append(r)

    day = (int(from_ms) // DAY_MS) * DAY_MS
    while day <= int(to_ms):
        day_start, day_end = day_bounds(day)
        covered_lo = max(day_start, int(from_ms))
        covered_hi = min(day_end, horizon)
        if covered_hi >= covered_lo:
            path, meta_path = _day_paths(root, symbol, day_start)
            merged = _dedupe(_read_day(path) + by_day.get(day_start, []))
            _write_day(path, merged)
            _write_meta(
                meta_path,
                _merge_coverage(_read_meta(meta_path), (covered_lo, covered_hi)),
                len(merged),
            )
        day += DAY_MS


async def download_agg_trades(
    rest: Any,
    symbol: str,
    start_ms: int,
    end_ms: int,
    *,
    cache_dir: str | Path | None = None,
    use_cache: bool = True,
    sleep_s: float | None = None,
    max_pages: int = MAX_PAGES,
    chunk_days: int = CHUNK_DAYS,
    on_progress: Any = None,
) -> list[Trade]:
    """Fetch ``[start_ms, end_ms]`` of the public tape, caching per UTC day.

    Resumable in both directions:

    * Before fetching, day coverage sidecars are walked forward from
      ``start_ms`` and only the first hole onward is requested — re-running a
      120-day pull that died on day 97 costs 23 days of requests, not 120.
    * While fetching, progress is flushed to disk every ``chunk_days``, so a
      pull that dies part-way keeps everything it had already downloaded.  A
      chunk boundary costs exactly one extra time-windowed request, which is a
      rounding error against re-downloading a month.
    """
    sym = symbol.upper()
    start_ms = int(start_ms)
    end_ms = int(end_ms)
    if end_ms < start_ms:
        return []

    pause = PAGE_SLEEP_SECONDS if sleep_s is None else sleep_s
    root = tape_root(cache_dir)
    span = max(1, int(chunk_days)) * DAY_MS

    cursor = (
        cached_coverage(sym, start_ms, end_ms, cache_dir=cache_dir)
        if use_cache
        else start_ms
    )

    fetched: list[AggTrade] = []
    while cursor <= end_ms:
        chunk_end = min(end_ms, ((cursor // DAY_MS) * DAY_MS) + span - 1)
        rows = await _fetch_range(
            rest,
            sym,
            cursor,
            chunk_end,
            sleep_s=pause,
            max_pages=max_pages,
            on_page=on_progress,
        )
        if use_cache:
            _persist(root, sym, rows, cursor, chunk_end)
        else:
            fetched.extend(rows)
        cursor = chunk_end + 1

    if use_cache:
        return load_cached_trades(sym, start_ms, end_ms, cache_dir=cache_dir)

    return [r.to_trade(sym) for r in _dedupe(fetched) if start_ms <= r.ts_ms <= end_ms]


# --------------------------------------------------------------------------- #
# Dataset assembly
# --------------------------------------------------------------------------- #

#: Small in-process memo so the tournament can reload a dataset between arms
#: without re-bucketing a hundred million prints.  Bounded, because a 5-second
#: view of a 120-day tape is roughly two million ``Bar`` objects.
_DATASET_MEMO: dict[tuple, Dataset] = {}
_MEMO_MAX = 4


def clear_dataset_cache() -> None:
    _DATASET_MEMO.clear()


def load_dataset(
    symbol: str,
    start_ms: int,
    end_ms: int,
    *,
    intervals: Sequence[str] = INTERVALS,
    cache_dir: str | Path | None = None,
    books: Sequence[OrderBook] = (),
    trades: Sequence[Trade] | None = None,
) -> Dataset:
    """Assemble a :class:`Dataset` from the cached tape.  No network, ever.

    ``trades`` is an escape hatch for tests and for callers that already hold a
    tape in memory; normally the prints come from the day cache written by
    :func:`download_agg_trades`.

    Memory note: ``"5s"`` over a 120-day window materialises ~2M ``Bar``
    objects.  Pass a narrower ``intervals`` (or a shorter window) when the
    sub-minute view is not what is being studied.
    """
    sym = symbol.upper()
    ivs = tuple(intervals)
    for iv in ivs:
        interval_ms(iv)  # validate early, with a useful message

    key = (sym, int(start_ms), int(end_ms), ivs, str(cache_dir or ""), trades is None)
    if trades is None:
        memo = _DATASET_MEMO.get(key)
        if memo is not None:
            return memo

    tape = (
        list(trades)
        if trades is not None
        else load_cached_trades(sym, start_ms, end_ms, cache_dir=cache_dir)
    )
    tape = [t for t in tape if start_ms <= t.ts_ms <= end_ms]
    tape.sort(key=lambda t: t.ts_ms)

    ds = Dataset(
        symbol=sym,
        bars={iv: build_bars(tape, iv) for iv in ivs},
        trades=tape,
        start_ms=int(start_ms),
        end_ms=int(end_ms),
        books=tuple(sorted(books, key=lambda b: b.last_update_ms)),
    )

    if trades is None:
        if len(_DATASET_MEMO) >= _MEMO_MAX:
            _DATASET_MEMO.pop(next(iter(_DATASET_MEMO)))
        _DATASET_MEMO[key] = ds
    return ds
