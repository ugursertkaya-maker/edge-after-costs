#!/usr/bin/env python
"""Run the V2 paper experiments (V2_SPEC.md §H) over cached Binance.US history.

    BACKTEST_CACHE_DIR=/tmp/bt-cache python scripts/run_experiments.py \
        --symbols BTCUSD,ETHUSD,SOLUSD,ADAUSD --period 90d --offline

    # the fee stress case from docs/ECONOMICS.md
    ... --fee-bps 10 --suites profiles --out json

Nothing here reimplements the backtester: each arm is one ``run_backtest`` call
over the same bars with a different ``Settings``.  History loading, the
``--save`` path and period parsing are imported from ``run_backtest.py`` for the
same reason — two copies of "load 90 days of bars" is two things to get wrong.
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

_SCRIPTS_DIR = Path(__file__).resolve().parent
_API_ROOT = _SCRIPTS_DIR.parent
for _p in (_API_ROOT, _SCRIPTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from run_backtest import load_history, parse_period_days, save_runs  # noqa: E402

from app.backtest.data import cache_root  # noqa: E402
from app.backtest.engine import (  # noqa: E402
    BacktestConfig,
    bars_span_ms,
    real_components_available,
)
from app.backtest.experiments import (  # noqa: E402
    DEFAULT_MAX_DRAWDOWN_PCT,
    DEFAULT_MIN_TRADES,
    SUITE_BUILDERS,
    ExperimentSuite,
    format_suites,
    run_all,
)

MS_PER_DAY = 86_400_000
KNOWN_PERIODS = ("7d", "30d", "90d", "180d", "365d")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_experiments",
        description="V2 experiment harness: NET target profiles, allocation modes, "
        "risk/reserve grid.",
    )
    p.add_argument("--symbols", default="BTCUSD,ETHUSD,SOLUSD,ADAUSD", help="comma separated")
    p.add_argument("--period", default="90d", help=f"one window, e.g. {', '.join(KNOWN_PERIODS)}")
    p.add_argument(
        "--suites",
        default=",".join(SUITE_BUILDERS),
        help=f"comma separated subset of {', '.join(SUITE_BUILDERS)}",
    )
    p.add_argument("--fee-bps", type=float, default=1.0,
                   help="per side; 1 = Binance.US Tier-0, 10 = standard-tier stress case")
    p.add_argument("--spread-bps", type=float, default=None,
                   help="pin the synthetic spread; default estimates from bar range")
    p.add_argument("--slippage-bps", type=float, default=2.0)
    p.add_argument("--latency-bars", type=int, default=1, help="must be >= 1")
    p.add_argument("--equity", type=float, default=500.0)
    p.add_argument("--risk-pct", type=float, default=0.5)
    p.add_argument("--max-concurrent", type=int, default=25,
                   help="V2 removed the 1-3 cap; the count ceiling is a safety valve only")
    p.add_argument("--strategies", default=None, help="comma separated; default all")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--interval", default="1m")
    p.add_argument("--min-trades", type=int, default=DEFAULT_MIN_TRADES,
                   help="below this an arm cannot be named winner")
    p.add_argument("--max-drawdown-pct", type=float, default=DEFAULT_MAX_DRAWDOWN_PCT)
    p.add_argument("--offline", action="store_true", help="cache only, never fetch")
    p.add_argument("--save", action="store_true", help="write each arm to backtest_runs")
    p.add_argument("--out", choices=("text", "json"), default="text")
    p.add_argument("--page-sleep", type=float, default=None)
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


def suites_to_dict(suites: list[ExperimentSuite]) -> list[dict[str, Any]]:
    return [s.to_dict() for s in suites]


async def run(args: argparse.Namespace, log=print) -> dict[str, Any]:
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    names = [s.strip().lower() for s in args.suites.split(",") if s.strip()]
    cfg = config_from_args(args)
    end_ms = args.end_ms if args.end_ms is not None else int(time.time() * 1000)
    days = parse_period_days(args.period)
    start_ms = end_ms - days * MS_PER_DAY

    if not real_components_available():
        log(
            "[warn] the production decision modules are not importable; this run "
            "cannot produce meaningful numbers",
            file=sys.stderr,
        )

    log(f"[cache] {cache_root()}", file=sys.stderr)
    log(f"[{args.period}] loading {len(symbols)} symbols ...", file=sys.stderr)
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

    payload: dict[str, Any] = {
        "generated_ms": end_ms,
        "period": args.period,
        "symbols": symbols,
        "config": asdict(cfg),
        "warnings": warnings,
        "suites": [],
    }
    if not bars:
        payload["error"] = "no bars"
        if args.out == "json":
            print(json.dumps(payload, indent=2, default=str))
        else:
            print(f"=== {args.period}: no bars available, nothing was run ===")
        return payload

    span_start, span_end = bars_span_ms(bars)
    payload["span_ms"] = [span_start, span_end]
    total_bars = sum(len(v) for v in bars.values())
    log(
        f"[{args.period}] {total_bars:,} bars across {len(bars)} symbols "
        f"({(span_end - span_start) / MS_PER_DAY:.1f}d)",
        file=sys.stderr,
    )

    def progress(msg: str) -> None:
        log(f"  {msg}", file=sys.stderr)

    suites = run_all(
        bars,
        cfg,
        only=names,
        min_trades=args.min_trades,
        max_drawdown_pct=args.max_drawdown_pct,
        progress=progress,
    )
    payload["suites"] = suites_to_dict(suites)

    if args.out == "json":
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(format_suites(suites))

    if args.save:
        runs = [
            (f"exp-{args.period}-{suite.name}-{arm.label}", "backtest", arm.result)
            for suite in suites
            for arm in suite.arms
            if arm.result is not None
        ]
        payload["saved_ids"] = save_runs(runs, asdict(cfg), log=log)

    return payload


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.latency_bars < 1:
        print("--latency-bars must be >= 1 (same-bar fills are not real)", file=sys.stderr)
        return 2
    unknown = [
        s.strip().lower()
        for s in args.suites.split(",")
        if s.strip() and s.strip().lower() not in SUITE_BUILDERS
    ]
    if unknown:
        print(
            f"unknown suite(s): {', '.join(unknown)}; known: {', '.join(SUITE_BUILDERS)}",
            file=sys.stderr,
        )
        return 2
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
