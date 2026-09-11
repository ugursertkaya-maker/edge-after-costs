#!/usr/bin/env python
"""Download history, replay it through the live decision stack, print a report.

    python scripts/run_backtest.py --symbols BTCUSD,ETHUSD --periods 7d,30d
    python scripts/run_backtest.py --symbols BTCUSD --periods 90d \
        --walkforward --train-days 21 --test-days 7 --out text --save

Everything degrades instead of crashing: no network falls back to the on-disk
cache, no database skips the ``--save``, a period with no bars is reported as
such.  A backtest that refuses to run teaches nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

_API_ROOT = Path(__file__).resolve().parents[1]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from app.backtest.data import cache_root, fetch_klines, load_cached  # noqa: E402
from app.backtest.engine import (  # noqa: E402
    BacktestConfig,
    BacktestResult,
    bars_span_ms,
    real_components_available,
    run_backtest,
)
from app.backtest.report import format_result, format_walkforward  # noqa: E402
from app.backtest.walkforward import WalkForwardResult, run_walk_forward  # noqa: E402
from app.types import Bar  # noqa: E402

MS_PER_DAY = 86_400_000


# --------------------------------------------------------------------------- #
# Arguments
# --------------------------------------------------------------------------- #


def parse_period_days(text: str) -> int:
    text = text.strip().lower()
    if text.endswith("d"):
        return int(text[:-1])
    if text.endswith("w"):
        return int(text[:-1]) * 7
    if text.endswith("y"):
        return int(text[:-1]) * 365
    return int(text)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_backtest",
        description="Backtest and walk-forward validate the paper-trading stack.",
    )
    p.add_argument("--symbols", default="BTCUSD,ETHUSD", help="comma separated")
    p.add_argument("--periods", default="7d,30d,90d", help="e.g. 7d,30d,90d,180d,365d")
    p.add_argument("--interval", default="1m")
    p.add_argument("--fee-bps", type=float, default=10.0, help="per side")
    p.add_argument("--spread-bps", type=float, default=None,
                   help="pin the synthetic spread; default estimates from bar range")
    p.add_argument("--slippage-bps", type=float, default=2.0)
    p.add_argument("--latency-bars", type=int, default=1, help="must be >= 1")
    p.add_argument("--equity", type=float, default=500.0)
    p.add_argument("--risk-pct", type=float, default=0.5)
    p.add_argument("--max-concurrent", type=int, default=3)
    p.add_argument("--strategies", default=None, help="comma separated; default all")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--out", choices=("json", "text"), default="text")
    p.add_argument("--trades", type=int, default=0, help="print the last N trades")
    p.add_argument("--walkforward", action="store_true")
    p.add_argument("--train-days", type=int, default=21)
    p.add_argument("--test-days", type=int, default=7)
    p.add_argument("--save", action="store_true", help="write to backtest_runs")
    p.add_argument("--offline", action="store_true", help="cache only, never fetch")
    p.add_argument("--page-sleep", type=float, default=None,
                   help="seconds to pause between kline pages (rate-limit courtesy)")
    p.add_argument("--end-ms", type=int, default=None, help="override 'now' (tests)")
    return p


def config_from_args(args: argparse.Namespace) -> BacktestConfig:
    strategies = (
        tuple(s.strip() for s in args.strategies.split(",") if s.strip())
        if args.strategies
        else None
    )
    return BacktestConfig(
        starting_equity=args.equity,
        fee_bps_per_side=args.fee_bps,
        spread_bps=args.spread_bps,
        slippage_bps=args.slippage_bps,
        latency_bars=args.latency_bars,
        risk_per_trade_pct=args.risk_pct,
        max_concurrent=args.max_concurrent,
        strategies=strategies,
        seed=args.seed,
    )


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #


async def load_history(
    symbols: list[str],
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    offline: bool,
    page_sleep: float | None = None,
    log=print,
) -> tuple[dict[str, list[Bar]], list[str]]:
    """Cache first, network for the missing tail, never fatal."""
    warnings: list[str] = []
    out: dict[str, list[Bar]] = {}

    if offline:
        for sym in symbols:
            bars = [b for b in load_cached(sym, interval) if start_ms <= b.open_ms <= end_ms]
            if bars:
                out[sym] = bars
            else:
                warnings.append(f"{sym}: no cached bars in window (offline)")
        return out, warnings

    rest = None
    try:
        from app.exchange.rest import BinanceUSRest

        rest = BinanceUSRest()
    except Exception as exc:
        warnings.append(f"exchange client unavailable ({exc!r}); using cache only")

    try:
        for sym in symbols:
            if rest is None:
                bars = [
                    b for b in load_cached(sym, interval) if start_ms <= b.open_ms <= end_ms
                ]
            else:
                try:
                    log(f"  fetching {sym} {interval} ...", file=sys.stderr)
                    bars = await fetch_klines(
                        rest, sym, interval, start_ms, end_ms, sleep_s=page_sleep
                    )
                except Exception as exc:
                    warnings.append(f"{sym}: download failed ({exc!r}); falling back to cache")
                    bars = [
                        b
                        for b in load_cached(sym, interval)
                        if start_ms <= b.open_ms <= end_ms
                    ]
            if bars:
                out[sym] = bars
            else:
                warnings.append(f"{sym}: no bars available for this window")
    finally:
        if rest is not None:
            try:
                await rest.close()
            except Exception:
                pass
    return out, warnings


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #


def result_to_dict(result: BacktestResult, *, curve_points: int = 500) -> dict[str, Any]:
    curve = result.equity_curve
    if len(curve) > curve_points:
        step = len(curve) // curve_points + 1
        curve = curve[::step] + [curve[-1]]
    stats = asdict(result.stats)
    stats["win_rate"] = result.stats.win_rate
    return {
        "label": result.label,
        "start_ms": result.start_ms,
        "end_ms": result.end_ms,
        "symbols": list(result.symbols),
        "starting_equity": result.starting_equity,
        "ending_equity": result.ending_equity,
        "stats": stats,
        "per_strategy": {
            name: {**asdict(s), "win_rate": s.win_rate}
            for name, s in result.per_strategy.items()
        },
        "equity_curve": curve,
        "trade_count": len(result.trades),
        "bars_processed": result.bars_processed,
        "signals_generated": result.signals_generated,
        "entries_approved": result.entries_approved,
        "entries_filled": result.entries_filled,
        "rejections": result.rejections,
        "open_at_end": result.open_at_end,
        "notes": list(dict.fromkeys(result.notes)),
    }


def walkforward_to_dict(wf: WalkForwardResult) -> dict[str, Any]:
    return {
        "train_days": wf.train_days,
        "test_days": wf.test_days,
        "windows_planned": len(wf.splits),
        "windows_run": len(wf.windows),
        "in_sample": {**asdict(wf.in_sample), "win_rate": wf.in_sample.win_rate},
        "out_of_sample": {
            **asdict(wf.out_of_sample),
            "win_rate": wf.out_of_sample.win_rate,
        },
        "selected_strategies": [list(s) for s in wf.selected],
        "per_window": [
            {
                "in_sample": result_to_dict(a, curve_points=50),
                "out_of_sample": result_to_dict(b, curve_points=50),
            }
            for a, b in wf.windows
        ],
        "notes": list(dict.fromkeys(wf.notes)),
    }


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def save_runs(runs: list[tuple[str, str, Any]], config: Any, *, log=print) -> list[int]:
    """Best-effort write to ``backtest_runs``.  A missing DB is not an error.

    ``runs`` is ``(label, kind, result)``; ``result`` is the real
    ``BacktestResult`` / ``WalkForwardResult`` object, which is what
    ``app.repo.save_backtest_run`` expects — not a pre-serialised dict.
    """
    try:
        from app.db import get_session, healthcheck
        from app.repo import save_backtest_run
    except Exception as exc:
        log(f"[save] persistence layer unavailable ({exc!r}) — skipped", file=sys.stderr)
        return []
    if not healthcheck():
        log("[save] database unreachable — results not persisted", file=sys.stderr)
        return []

    ids: list[int] = []
    try:
        with get_session() as session:
            for label, kind, result in runs:
                ids.append(
                    save_backtest_run(
                        session, result, label=label, kind=kind, config=config
                    )
                )
    except Exception as exc:
        log(f"[save] write failed ({exc!r}) — results not persisted", file=sys.stderr)
        return ids
    log(f"[save] wrote {len(ids)} row(s) to backtest_runs", file=sys.stderr)
    return ids


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


async def run(args: argparse.Namespace, log=print) -> dict[str, Any]:
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    periods = [p.strip() for p in args.periods.split(",") if p.strip()]
    cfg = config_from_args(args)
    end_ms = args.end_ms if args.end_ms is not None else int(time.time() * 1000)

    if not real_components_available():
        log(
            "[warn] the production decision modules are not importable; "
            "this run cannot produce meaningful numbers",
            file=sys.stderr,
        )

    log(f"[cache] {cache_root()}", file=sys.stderr)
    payload: dict[str, Any] = {
        "generated_ms": end_ms,
        "config": asdict(cfg),
        "symbols": symbols,
        "periods": {},
    }
    text_blocks: list[str] = []
    to_save: list[tuple[str, str, Any]] = []

    for period in periods:
        days = parse_period_days(period)
        start_ms = end_ms - days * MS_PER_DAY
        log(f"[{period}] loading {len(symbols)} symbols ...", file=sys.stderr)
        bars, warnings = await load_history(
            symbols,
            args.interval,
            start_ms,
            end_ms,
            offline=args.offline,
            page_sleep=args.page_sleep,
            log=log,
        )
        for w in warnings:
            log(f"  ! {w}", file=sys.stderr)

        if not bars:
            payload["periods"][period] = {"error": "no bars", "warnings": warnings}
            text_blocks.append(f"=== {period}: no bars available, skipped ===")
            continue

        span_start, span_end = bars_span_ms(bars)
        result = run_backtest(bars, cfg, label=f"{period} ({len(bars)} symbols)")
        entry: dict[str, Any] = {
            "days": days,
            "warnings": warnings,
            "span_ms": [span_start, span_end],
            "backtest": result_to_dict(result),
        }
        text_blocks.append(format_result(result, include_trades=args.trades))
        to_save.append((f"cli-{period}", "backtest", result))

        if args.walkforward:
            wf = run_walk_forward(
                bars, cfg, train_days=args.train_days, test_days=args.test_days
            )
            entry["walkforward"] = walkforward_to_dict(wf)
            text_blocks.append(format_walkforward(wf))
            to_save.append((f"cli-{period}-wf", "walkforward", wf))

        payload["periods"][period] = entry

    if args.out == "json":
        print(json.dumps(payload, indent=2, default=str))
    else:
        print("\n\n".join(text_blocks))

    if args.save:
        payload["saved_ids"] = save_runs(to_save, asdict(cfg), log=log)

    return payload


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.latency_bars < 1:
        print("--latency-bars must be >= 1 (same-bar fills are not real)", file=sys.stderr)
        return 2
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
