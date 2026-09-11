#!/usr/bin/env python
"""Run the Phase-3 strategy tournament and print an honest report.

    BACKTEST_CACHE_DIR=/tmp/bt-cache python scripts/run_tournament.py \
        --symbols BTCUSD,ETHUSD,SOLUSD,ADAUSD --period 120d --offline \
        --walkforward 30/7,60/14,90/30 --fee-bps 1 --jobs 4 --save

The headline is out-of-sample.  Every arm evaluated is printed, losers
included, and when nothing clears the gates the report says so rather than
promoting the least-bad arm.

Everything degrades instead of crashing: no tape cache falls back to 1-minute
bars (and says so), a period with no bars is reported as such, and a missing
``--save`` directory is created.  A tournament that refuses to run teaches
nothing.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import multiprocessing
import os
import sys
import time
from pathlib import Path
from typing import Any

_API_ROOT = Path(__file__).resolve().parents[1]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from app.backtest.data import cache_root, load_cached  # noqa: E402
from app.research.tournament import (  # noqa: E402
    DEFAULT_EXIT_PROFILES,
    DEFAULT_HORIZONS,
    DEFAULT_MIN_SCORES,
    DEFAULT_STOP_PROFILES,
    TournamentConfig,
    WalkForwardScheme,
    format_tournament,
    load_sources,
    research_families_available,
    run_tournament,
    to_json,
)
from app.types import Bar  # noqa: E402

MS_PER_DAY = 86_400_000
DEFAULT_TAPE_CACHE = "/tmp/tape-cache"


# --------------------------------------------------------------------------- #
# Arguments
# --------------------------------------------------------------------------- #


def parse_period_days(text: str) -> int:
    text = str(text).strip().lower()
    if text.endswith("d"):
        return int(text[:-1])
    if text.endswith("w"):
        return int(text[:-1]) * 7
    if text.endswith("y"):
        return int(text[:-1]) * 365
    return int(text)


def _csv(text: str | None) -> list[str]:
    if not text:
        return []
    return [p.strip() for p in str(text).split(",") if p.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_tournament",
        description="Walk-forward strategy tournament over the Phase-3 candidate space.",
    )
    p.add_argument("--symbols", default="BTCUSD,ETHUSD,SOLUSD,ADAUSD")
    p.add_argument("--period", default="120d", help="e.g. 120d, 365d")
    p.add_argument("--families", default="all", help="all, or a comma separated subset")
    p.add_argument(
        "--horizons",
        default=",".join(str(h) for h in DEFAULT_HORIZONS),
        help="minutes; the horizon is the time stop, not a minimum hold",
    )
    p.add_argument("--walkforward", default="30/7,60/14,90/30", help="train/test day pairs")
    p.add_argument("--exit-profiles", default=",".join(DEFAULT_EXIT_PROFILES))
    p.add_argument("--stop-profiles", default=",".join(DEFAULT_STOP_PROFILES))
    p.add_argument("--min-scores", default=",".join(f"{v:g}" for v in DEFAULT_MIN_SCORES))
    p.add_argument("--regimes", default="all", help="all, none, or a Regime7 subset")

    p.add_argument("--fee-bps", type=float, default=1.0,
                   help="per side; 1 = Tier 0, 10 = the standard-tier stress")
    p.add_argument("--slippage-bps", type=float, default=2.0)
    p.add_argument("--spread-bps", type=float, default=None,
                   help="pin the synthetic spread; default estimates from bar range")
    p.add_argument("--equity", type=float, default=500.0)
    p.add_argument("--risk-pct", type=float, default=0.5)
    p.add_argument("--max-concurrent", type=int, default=3)
    p.add_argument("--min-net-edge-bps", type=float, default=None,
                   help="override the ranker gate; default is the live setting")

    p.add_argument("--jobs", type=int, default=1)
    p.add_argument("--max-candidates", type=int, default=2000)
    p.add_argument("--max-params", type=int, default=4, help="param combos per family")
    p.add_argument("--min-trades", type=int, default=30)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--top", type=int, default=25)

    p.add_argument("--tape-cache", default=DEFAULT_TAPE_CACHE,
                   help="aggTrades cache for the multi-timeframe block; "
                        "'none' forces the 1-minute-bars fallback")
    p.add_argument("--offline", action="store_true", help="cache only, never fetch")
    p.add_argument("--out", choices=("text", "json"), default="text")
    p.add_argument("--save", action="store_true",
                   help="write the report and the JSON beside the repo, under artifacts/")
    p.add_argument("--save-dir", default=None)
    p.add_argument("--quiet", action="store_true", help="no progress on stderr")
    p.add_argument("--end-ms", type=int, default=None, help="override 'now' (tests)")
    return p


def config_from_args(args: argparse.Namespace) -> TournamentConfig:
    regimes: tuple[Any, ...]
    raw_regimes = str(args.regimes).strip().lower()
    if raw_regimes == "none":
        regimes = (None,)
    elif raw_regimes == "all":
        from app.research.tournament import Regime7

        regimes = (None,) + tuple(Regime7)
    else:
        from app.research.tournament import Regime7

        regimes = (None,) + tuple(Regime7(r.strip().upper()) for r in _csv(args.regimes))

    tape = None if str(args.tape_cache).lower() in ("", "none", "off") else args.tape_cache
    if tape and not Path(tape).is_dir():
        tape = None

    return TournamentConfig(
        symbols=tuple(_csv(args.symbols)),
        families=None if str(args.families).lower() == "all" else tuple(_csv(args.families)),
        horizons_minutes=tuple(int(h) for h in _csv(args.horizons)),
        exit_profiles=tuple(_csv(args.exit_profiles)),
        stop_profiles=tuple(_csv(args.stop_profiles)),
        min_scores=tuple(float(v) for v in _csv(args.min_scores)),
        regimes=regimes,
        schemes=tuple(WalkForwardScheme.parse(s) for s in _csv(args.walkforward)),
        fee_bps_per_side=args.fee_bps,
        slippage_bps=args.slippage_bps,
        spread_bps=args.spread_bps,
        starting_equity=args.equity,
        risk_per_trade_pct=args.risk_pct,
        max_concurrent=args.max_concurrent,
        min_net_edge_bps=args.min_net_edge_bps,
        tape_cache_dir=tape,
        max_candidates=args.max_candidates,
        max_params_per_family=args.max_params,
        jobs=args.jobs,
        seed=args.seed,
        min_trades=args.min_trades,
        top_n=args.top,
    )


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #


def load_bars(
    symbols: list[str], start_ms: int, end_ms: int, *, offline: bool, log=print
) -> tuple[dict[str, list[Bar]], list[str]]:
    """Cache first.  ``--offline`` never reaches the network at all."""
    warnings: list[str] = []
    out: dict[str, list[Bar]] = {}
    for sym in symbols:
        bars = [b for b in load_cached(sym, "1m") if start_ms <= b.open_ms <= end_ms]
        if bars:
            out[sym] = bars
        else:
            warnings.append(f"{sym}: no cached 1m bars in window")
    if not offline and len(out) < len(symbols):
        warnings.append(
            "some symbols are missing and downloading was not attempted here — "
            "run scripts/run_backtest.py to refresh the kline cache"
        )
    return out, warnings


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    quiet = bool(args.quiet)

    def log(msg: str) -> None:
        if not quiet:
            print(msg, file=sys.stderr, flush=True)

    cfg = config_from_args(args)
    if not cfg.symbols:
        print("no symbols requested", file=sys.stderr)
        return 2

    days = parse_period_days(args.period)
    end_ms = args.end_ms if args.end_ms is not None else int(time.time() * 1000)
    start_ms = end_ms - days * MS_PER_DAY

    log(f"cache: {cache_root()}")
    bars, warnings = load_bars(list(cfg.symbols), start_ms, end_ms, offline=args.offline,
                               log=log)
    for w in warnings:
        log(f"[warn] {w}")
    if not bars:
        print("no bars available for any requested symbol — nothing to search",
              file=sys.stderr)
        return 1

    # The window really available, which is what the report must quote.
    actual_start = min(b[0].open_ms for b in bars.values())
    actual_end = max(b[-1].close_ms for b in bars.values())
    span_days = (actual_end - actual_start) / MS_PER_DAY
    cfg = _with_symbols(cfg, tuple(sorted(bars)))

    sources = load_sources(cfg.families)
    source_kind = (
        "app.research.families (the ten Phase-3 families)"
        if research_families_available()
        else "app.strategies (the six live strategies — research families unavailable)"
    )
    if cfg.tape_cache_dir:
        mtf_note = f"tape at {cfg.tape_cache_dir}"
    else:
        mtf_note = "1-minute bars only (no tape cache; sub-minute features read as missing)"
    log(f"families : {', '.join(s.name for s in sources)}")
    log(f"source   : {source_kind}")
    log(f"mtf input: {mtf_note}")
    log(f"span     : {span_days:.1f} days, {sum(len(b) for b in bars.values())} bars")

    started = time.time()
    results = run_tournament(bars, config=cfg, on_progress=log)
    elapsed = time.time() - started

    evaluated = sum(1 for r in results if r.windows_evaluated > 0)
    meta = {
        "period": f"{args.period} requested, {span_days:.1f}d available "
                  f"({_ts(actual_start)} -> {_ts(actual_end)})",
        "families": ", ".join(s.name for s in sources),
        "source": f"{source_kind}; MTF from {mtf_note}",
        "elapsed": f"{elapsed:.1f}s",
        "candidates_evaluated": evaluated,
        "candidates_enumerated": len(results),
        "warnings": warnings,
    }

    if args.out == "json":
        payload = to_json(results, config=cfg, meta=meta)
        text = json.dumps(payload, indent=2, sort_keys=True)
    else:
        text = format_tournament(results, config=cfg, meta=meta)
    print(text)

    log(
        f"evaluated {evaluated} of {len(results)} candidates in {elapsed:.1f}s "
        f"with {cfg.jobs} job(s)"
    )

    if args.save:
        path = _save(results, cfg, meta, args)
        log(f"[save] wrote {path}")
    return 0


def _with_symbols(cfg: TournamentConfig, symbols: tuple[str, ...]) -> TournamentConfig:
    from dataclasses import replace

    return replace(cfg, symbols=symbols)


def _ts(ms: int) -> str:
    return _dt.datetime.fromtimestamp(ms / 1000, _dt.UTC).strftime("%Y-%m-%d %H:%M")


def _save(results, cfg, meta, args) -> Path:
    stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    root = Path(args.save_dir) if args.save_dir else (_API_ROOT.parent / "artifacts"
                                                     / "tournament")
    root.mkdir(parents=True, exist_ok=True)
    text_path = root / f"tournament-{stamp}.txt"
    json_path = root / f"tournament-{stamp}.json"
    text_path.write_text(format_tournament(results, config=cfg, meta=meta), encoding="utf-8")
    json_path.write_text(
        json.dumps(to_json(results, config=cfg, meta=meta), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return text_path


if __name__ == "__main__":
    # ``--jobs`` uses a process pool; on macOS the default start method is
    # "spawn", which re-imports this file in every worker.  The guard above is
    # what makes that safe, and it is why the search must be started from this
    # script rather than from an interactive session.
    multiprocessing.freeze_support()
    raise SystemExit(main())
