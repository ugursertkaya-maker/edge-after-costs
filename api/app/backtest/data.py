"""Historical kline download + a resumable on-disk cache.

The backtester needs months of 1m bars for a dozen symbols.  Re-downloading
that every run would be both slow and rude to the exchange, so every
``(symbol, interval)`` series is kept as newline-delimited JSON on disk and a
re-run only fetches the *missing tail*.

``load_cached`` / ``save_cache`` are deliberately pure file operations with no
network and no clock dependence so they can be unit-tested offline.

The cache location comes from ``settings.backtest.cache_dir``
(``BACKTEST_CACHE_DIR`` in the environment).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from app.config import Settings
from app.types import Bar

__all__ = [
    "DEFAULT_CACHE_DIR",
    "PAGE_SLEEP_SECONDS",
    "cache_root",
    "cache_path",
    "fetch_klines",
    "interval_ms",
    "load_cached",
    "merge_bars",
    "save_cache",
]

DEFAULT_CACHE_DIR = "/data/backtest-cache"

#: Politeness pause between kline pages.  Binance.US allows far more than this,
#: but a backtest download is never latency-critical and we would rather never
#: see a 429 than shave a minute off a 365-day pull.
PAGE_SLEEP_SECONDS = 0.25

_INTERVAL_UNITS = {"s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}

# Remembers the directory we actually landed on, so a mid-run permission change
# cannot split one series across two locations.
_resolved_root: Path | None = None


def interval_ms(interval: str) -> int:
    """``"1m"`` -> 60000.  Raises on anything Binance would not accept."""
    if not interval or interval[-1] not in _INTERVAL_UNITS:
        raise ValueError(f"unsupported interval: {interval!r}")
    return int(interval[:-1]) * _INTERVAL_UNITS[interval[-1]]


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def cache_root(*, refresh: bool = False) -> Path:
    """Directory holding the cache, falling back to a temp dir when unwritable.

    A read-only ``/data`` is the normal case on a laptop, so this must never be
    fatal — a backtest that re-downloads is merely slow, one that crashes is
    useless.
    """
    global _resolved_root
    if _resolved_root is not None and not refresh:
        return _resolved_root

    # A fresh Settings() rather than the cached get_settings(): this is the one
    # place that must notice an environment change mid-process, because a test
    # (or a one-off CLI run) points the cache somewhere else without wanting to
    # reset global configuration.
    configured = Settings().backtest.cache_dir or DEFAULT_CACHE_DIR
    candidate = Path(configured).expanduser()
    if not _writable(candidate):
        candidate = Path(tempfile.gettempdir()) / "crypto-trader-backtest-cache"
        candidate.mkdir(parents=True, exist_ok=True)
    _resolved_root = candidate
    return candidate


def cache_path(symbol: str, interval: str) -> Path:
    return cache_root() / f"{symbol.upper()}-{interval}.ndjson"


# --------------------------------------------------------------------------- #
# Pure file operations
# --------------------------------------------------------------------------- #


def _bar_from_obj(obj: dict[str, Any]) -> Bar:
    return Bar(
        symbol=obj["symbol"],
        open_ms=int(obj["open_ms"]),
        close_ms=int(obj["close_ms"]),
        open=float(obj["open"]),
        high=float(obj["high"]),
        low=float(obj["low"]),
        close=float(obj["close"]),
        volume=float(obj["volume"]),
        quote_volume=float(obj["quote_volume"]),
        trades=int(obj["trades"]),
        taker_buy_quote=float(obj.get("taker_buy_quote", 0.0)),
    )


def load_cached(symbol: str, interval: str) -> list[Bar]:
    """Read a cached series.  Missing file or corrupt lines => best effort.

    A truncated final line is expected after a killed download, so a bad line
    is skipped rather than raised: losing one bar is better than losing the
    whole cache.
    """
    path = cache_path(symbol, interval)
    if not path.exists():
        return []
    bars: list[Bar] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                bars.append(_bar_from_obj(json.loads(line)))
            except (ValueError, KeyError, TypeError):
                continue
    bars.sort(key=lambda b: b.open_ms)
    return bars


def save_cache(symbol: str, interval: str, bars: list[Bar]) -> Path:
    """Replace the cached series with ``bars`` (deduped by ``open_ms``, sorted).

    Written to a sibling temp file and renamed so an interrupted save never
    leaves a half-written cache behind.
    """
    path = cache_path(symbol, interval)
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = merge_bars(bars)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for bar in ordered:
            fh.write(json.dumps(asdict(bar), separators=(",", ":")))
            fh.write("\n")
    tmp.replace(path)
    return path


def merge_bars(*series: Iterable[Bar]) -> list[Bar]:
    """Union of several series, deduped on ``open_ms``, later wins, sorted."""
    by_open: dict[int, Bar] = {}
    for bars in series:
        for bar in bars:
            by_open[bar.open_ms] = bar
    return [by_open[k] for k in sorted(by_open)]


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #


def _resume_cursor(window: list[Bar], start_ms: int, step_ms: int) -> int:
    """Where to resume fetching given what the cache already holds.

    Only a series that actually starts at ``start_ms`` can be resumed from its
    tail; a cache with a hole at the front would otherwise silently produce a
    gappy backtest, so we refetch from the beginning instead.
    """
    if not window:
        return start_ms
    if window[0].open_ms > start_ms + step_ms:
        return start_ms
    return window[-1].close_ms + 1


async def fetch_klines(
    rest: Any,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    use_cache: bool = True,
    sleep_s: float | None = None,
    page_limit: int = 1000,
    max_pages: int | None = None,
) -> list[Bar]:
    """Page ``[start_ms, end_ms]`` of klines, using and refreshing the cache.

    Returns the bars inside the requested window only, even if the cache holds
    more.  Network failure after at least one successful page is treated as a
    short read: we keep what we got, persist it, and let the caller decide.
    """
    step_ms = interval_ms(interval)
    pause = PAGE_SLEEP_SECONDS if sleep_s is None else sleep_s

    cached = load_cached(symbol, interval) if use_cache else []
    window = [b for b in cached if start_ms <= b.open_ms <= end_ms]
    cursor = _resume_cursor(window, start_ms, step_ms)

    fetched: list[Bar] = []
    pages = 0
    while cursor <= end_ms:
        if max_pages is not None and pages >= max_pages:
            break
        page = await rest.klines(
            symbol, interval, start_ms=cursor, end_ms=end_ms, limit=page_limit
        )
        pages += 1
        if not page:
            break
        fetched.extend(page)
        next_cursor = page[-1].close_ms + 1
        if next_cursor <= cursor:  # exchange refused to advance; stop rather than spin
            break
        cursor = next_cursor
        if len(page) < page_limit:
            break
        if pause > 0:
            await asyncio.sleep(pause)

    if use_cache and fetched:
        save_cache(symbol, interval, merge_bars(cached, fetched))

    combined = merge_bars(window, fetched)
    return [b for b in combined if start_ms <= b.open_ms <= end_ms]
