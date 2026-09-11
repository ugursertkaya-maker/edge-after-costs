#!/usr/bin/env python
"""Download the Binance.US public trade tape and report what it actually is.

    python scripts/download_tape.py --symbols BTCUSD,ETHUSD,SOLUSD,ADAUSD \
        --days 365 --out /tmp/tape-cache

Binance.US is thin enough (~3,000 trades a day on BTC/USD) that the entire
historical tape fits on a laptop, which is the one research advantage the venue
offers — real sub-minute prints instead of ticks synthesised from bars.

The summary is not decoration.  The **fraction of empty buckets per interval**
decides whether sub-minute strategies are testable here at all: an interval
where 95% of buckets contain no trade cannot support a momentum or flow
strategy, because most of the time there is nothing to measure.  That number is
a finding, so it is printed for every interval and not buried in a log.

The download is resumable — re-running fetches only the missing tail — and
polite: bounded concurrency, a pause between pages, and ``Retry-After``
honoured by the REST client underneath.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

_API_ROOT = Path(__file__).resolve().parents[1]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from app.exchange.rest import BinanceUSRest  # noqa: E402
from app.research.dataset import (  # noqa: E402
    DAY_MS,
    INTERVALS,
    bucket_stats,
    day_key,
    download_agg_trades,
    tape_root,
)
from app.types import now_ms  # noqa: E402

DEFAULT_SYMBOLS = "BTCUSD,ETHUSD,SOLUSD,ADAUSD"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    p.add_argument("--days", type=int, default=120)
    p.add_argument("--out", default=None, help="cache directory (default: config)")
    p.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="symbols downloaded at once; kept small on purpose",
    )
    p.add_argument(
        "--sleep",
        type=float,
        default=None,
        help="seconds between pages (default: the module's polite value)",
    )
    p.add_argument(
        "--intervals",
        default=",".join(INTERVALS),
        help="intervals to report empty-bucket fractions for",
    )
    return p


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{n:,} B"
        n /= 1024.0
    return f"{n:,.1f} GB"


def _dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


async def _one(
    rest: BinanceUSRest,
    symbol: str,
    start_ms: int,
    end_ms: int,
    *,
    out: str | None,
    sleep_s: float | None,
    sem: asyncio.Semaphore,
) -> dict:
    async with sem:
        t0 = time.time()
        last_report = [0.0]

        def on_page(count: int, ts_ms: int) -> None:
            now = time.time()
            if now - last_report[0] < 5.0:
                return
            last_report[0] = now
            print(
                f"  [{symbol}] {count:,} trades ... at {day_key(ts_ms)}",
                flush=True,
            )

        try:
            trades = await download_agg_trades(
                rest,
                symbol,
                start_ms,
                end_ms,
                cache_dir=out,
                sleep_s=sleep_s,
                on_progress=on_page,
            )
        except Exception as exc:  # noqa: BLE001 - a dead symbol must not kill the run
            print(f"  [{symbol}] FAILED: {exc!r}", flush=True)
            return {"symbol": symbol, "error": repr(exc), "trades": 0}

        elapsed = time.time() - t0
        print(
            f"  [{symbol}] done: {len(trades):,} trades in {elapsed:,.0f}s",
            flush=True,
        )
        return {"symbol": symbol, "trades": trades, "seconds": elapsed}


async def run(args: argparse.Namespace) -> int:
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    intervals = [s.strip() for s in args.intervals.split(",") if s.strip()]
    end_ms = now_ms()
    start_ms = end_ms - args.days * DAY_MS
    root = tape_root(args.out)

    print(f"tape cache : {root}")
    print(f"symbols    : {', '.join(symbols)}")
    print(f"range      : {day_key(start_ms)} .. {day_key(end_ms)} ({args.days} days)")
    print("")

    rest = BinanceUSRest(max_concurrency=max(1, args.concurrency))
    sem = asyncio.Semaphore(max(1, args.concurrency))
    try:
        results = await asyncio.gather(
            *[
                _one(
                    rest,
                    sym,
                    start_ms,
                    end_ms,
                    out=args.out,
                    sleep_s=args.sleep,
                    sem=sem,
                )
                for sym in symbols
            ]
        )
    finally:
        await rest.close()

    print("")
    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)
    header = f"{'symbol':<10}{'trades':>12}{'bytes':>12}  {'first':<12}{'last':<12}"
    print(header)
    print("-" * len(header))

    total_trades = 0
    for res in results:
        sym = res["symbol"]
        if res.get("error"):
            print(f"{sym:<10}{'ERROR':>12}  {res['error'][:40]}")
            continue
        trades = res["trades"]
        total_trades += len(trades)
        size = _dir_bytes(root / sym)
        first = day_key(trades[0].ts_ms) if trades else "-"
        last = day_key(trades[-1].ts_ms) if trades else "-"
        print(f"{sym:<10}{len(trades):>12,}{_fmt_bytes(size):>12}  {first:<12}{last:<12}")

    print("")
    print("EMPTY BUCKET FRACTION  (buckets with zero trades / closed buckets)")
    print("This is the finding: an interval that is mostly empty cannot support")
    print("a flow or momentum strategy, because there is nothing to measure.")
    print("")
    head = f"{'symbol':<10}" + "".join(f"{iv:>10}" for iv in intervals)
    print(head)
    print("-" * len(head))
    for res in results:
        if res.get("error"):
            continue
        row = f"{res['symbol']:<10}"
        for iv in intervals:
            total, empty = bucket_stats(res["trades"], iv)
            row += f"{(empty / total * 100.0 if total else 0.0):>9.1f}%"
        print(row)

    print("")
    print(f"total trades: {total_trades:,}   cache: {_fmt_bytes(_dir_bytes(root))}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.days < 1:
        print("--days must be >= 1", file=sys.stderr)
        return 2
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
