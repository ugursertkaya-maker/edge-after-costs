#!/usr/bin/env python3
"""Run the Phase 5 three-profile comparison and the missed-opportunity audit.

    BACKTEST_CACHE_DIR=/tmp/bt-cache python scripts/run_profiles.py \
        --symbols BTCUSD,ETHUSD,SOLUSD,ADAUSD --period 120d \
        --profiles standard,aggressive,high_activity \
        --deploy-bands 60,70,80,85 --fee-bps 1 --offline --out text

Two questions, one run:

1. **Which profile should the paper account use?**  Twelve arms (three total
   open-risk budgets crossed with four deployment bands), one engine, and a
   selection rule that picks the most *aggressive* arm clearing four floors —
   or says "none qualify" and means it.
2. **Were we right to refuse?**  Every rejection recorded during the chosen
   arm, replayed against what the market actually did next, split by rejection
   reason.

Memory
------
Bars are loaded **one symbol at a time**, sliced to the requested period, and
the full-year series is dropped before the next symbol is read; the cache holds
a year of 1m bars per symbol (~525k) and materialising four of those whole is
what exhausted this machine twice (see ``scripts/phase3_search.py``).  The arms
themselves then share one book, because the metric the comparison exists to
produce — simultaneous positions — cannot be measured per symbol.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.backtest.engine import BacktestConfig  # noqa: E402
from app.research import missed as M  # noqa: E402
from app.research import profiles as P  # noqa: E402

MS_PER_DAY = 86_400_000
_PERIOD = re.compile(r"^\s*(\d+)\s*([dhw]?)\s*$", re.I)


def parse_period_days(text: str) -> float:
    m = _PERIOD.match(text)
    if not m:
        raise argparse.ArgumentTypeError(f"bad period {text!r} — use e.g. 120d, 12h, 4w")
    n = float(m.group(1))
    unit = (m.group(2) or "d").lower()
    return {"d": n, "h": n / 24.0, "w": n * 7.0}[unit]


def load_bars(symbol: str, start_ms: int, end_ms: int, *, offline: bool) -> list:
    """1m bars for one symbol, sliced to the window, with the rest freed.

    ``offline`` is honoured strictly: a cache miss is reported and skipped, not
    silently downloaded.  A research run that quietly reaches the network is a
    research run whose period nobody can reproduce.
    """
    from app.backtest.data import load_cached

    full = load_cached(symbol, "1m")
    if not full and not offline:
        from app.backtest.data import fetch_bars  # type: ignore[attr-defined]

        full = fetch_bars(symbol, "1m", start_ms, end_ms)
    window = [b for b in full if start_ms <= b.open_ms < end_ms]
    del full
    gc.collect()
    return window


def main() -> int:
    ap = argparse.ArgumentParser(prog="run_profiles")
    ap.add_argument("--symbols", default="BTCUSD,ETHUSD,SOLUSD,ADAUSD")
    ap.add_argument("--period", default="120d")
    ap.add_argument("--profiles", default=",".join(P.PROFILES))
    ap.add_argument("--deploy-bands", default="60,70,80,85")
    ap.add_argument("--fee-bps", type=float, default=1.0)
    ap.add_argument("--slippage-bps", type=float, default=2.0)
    ap.add_argument("--equity", type=float, default=500.0)
    ap.add_argument("--offline", action="store_true", default=False,
                    help="never touch the network; a cache miss is an empty symbol")
    ap.add_argument("--out", choices=("text", "json"), default="text")
    ap.add_argument("--out-dir", default=None,
                    help="also write profiles.json / profiles.txt here")
    ap.add_argument("--cache-dir", default=os.environ.get("BACKTEST_CACHE_DIR", "/tmp/bt-cache"))
    ap.add_argument("--tape-cache", default="/tmp/tape-cache",
                    help="aggTrades cache root. Stated for reproducibility and "
                         "exported as RESEARCH_TAPE_DIR; this comparison runs off "
                         "1m bars, so nothing here reads the tape today.")
    ap.add_argument("--audit-horizon", type=int, default=60,
                    help="minutes of forward tape the missed audit is allowed to see")
    ap.add_argument("--idle-threshold", type=float, default=M.DEFAULT_IDLE_THRESHOLD_PCT)
    ap.add_argument("--oos-fraction", type=float, default=P.DEFAULT_OOS_FRACTION)
    ap.add_argument("--min-trades", type=int, default=P.DEFAULT_MIN_TRADES)
    ap.add_argument("--max-drawdown-pct", type=float, default=P.DEFAULT_MAX_DRAWDOWN_PCT)
    ap.add_argument("--max-rejections", type=int, default=250_000,
                    help="cap on recorded rejections handed to the audit")
    args = ap.parse_args()

    os.environ["BACKTEST_CACHE_DIR"] = args.cache_dir
    os.environ["RESEARCH_TAPE_DIR"] = args.tape_cache

    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    profiles = tuple(s.strip().lower() for s in args.profiles.split(",") if s.strip())
    bands = tuple(float(s) for s in args.deploy_bands.split(",") if s.strip())
    for p in profiles:
        if p not in P.PROFILE_RISK_PCT:
            print(f"unknown profile {p!r}; known: {', '.join(P.PROFILES)}", file=sys.stderr)
            return 2

    days = parse_period_days(args.period)
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(days * MS_PER_DAY)

    log = (lambda m: None) if args.out == "json" else (lambda m: print(m, flush=True))

    log(f"[profiles] {len(symbols)} symbols, {args.period}, "
        f"{len(profiles)} profiles x {len(bands)} bands = "
        f"{len(profiles) * len(bands)} arms, fee {args.fee_bps} bps/side")

    bars_by_symbol: dict[str, list] = {}
    for sym in symbols:
        t0 = time.time()
        series = load_bars(sym, start_ms, end_ms, offline=args.offline)
        if not series:
            log(f"[{sym}] no cached 1m bars in window — skipped")
            continue
        bars_by_symbol[sym] = series
        log(f"[{sym}] {len(series):,} 1m bars ({time.time() - t0:.1f}s)")
        gc.collect()

    if not bars_by_symbol:
        print("no bars for any symbol — nothing to compare. Warm the cache first "
              f"({args.cache_dir}).", file=sys.stderr)
        return 1

    cfg = BacktestConfig(
        starting_equity=args.equity,
        fee_bps_per_side=args.fee_bps,
        slippage_bps=args.slippage_bps,
    )

    t0 = time.time()
    results = P.compare_profiles(
        bars_by_symbol,
        cfg=cfg,
        profiles=profiles,
        deploy_bands=bands,
        oos_fraction=args.oos_fraction,
        on_progress=log,
    )
    log(f"[profiles] {len(results)} arms in {time.time() - t0:.0f}s")

    selection = P.select_profile(
        results, min_trades=args.min_trades, max_drawdown_pct=args.max_drawdown_pct
    )

    # -- the audit ---------------------------------------------------------- #
    # One arm's refusals, replayed.  The chosen arm, or — when nothing
    # qualifies, which is the likely outcome — the least aggressive one, since
    # every arm refuses for the same reasons and the smallest budget is the
    # cleanest read of the *gate* rather than of the budget.
    #
    # The grid above deliberately did not keep any refusals: twelve arms over
    # 120 days of four symbols produce them by the million, and pooling twelve
    # arms' refusals would also count the same market moment twelve times.
    focus_arm = selection.chosen or (
        min(results, key=lambda r: r.aggression) if results else None
    )
    focus = focus_arm.name if focus_arm else ""
    rejections: list = []
    if focus_arm is not None:
        log(f"[audit] replaying {focus} to record its refusals ...")
        t0 = time.time()
        rejections = P.rejections_for_arm(
            bars_by_symbol, cfg=cfg, arm=focus_arm, oos_fraction=args.oos_fraction
        )
        log(f"[audit] {len(rejections):,} refusals in {time.time() - t0:.0f}s")
    assert all(_arm_of(r) == focus for r in rejections)
    if len(rejections) > args.max_rejections:
        step = len(rejections) // args.max_rejections + 1
        rejections = rejections[::step]
        log(f"[audit] thinned to every {step}th rejection ({len(rejections):,})")

    round_trip_bps = 2.0 * args.fee_bps + args.slippage_bps
    t0 = time.time()
    audit = M.audit_rejections(
        bars_by_symbol,
        rejections,
        horizon_minutes=args.audit_horizon,
        round_trip_bps=round_trip_bps,
        idle_threshold_pct=args.idle_threshold,
    )
    log(f"[audit] {audit.rejections_examined:,} examined in {time.time() - t0:.0f}s")

    text = (
        P.format_comparison(results, selection)
        + "\n\n"
        + M.format_audit(audit, title=f"MISSED-OPPORTUNITY AUDIT — {focus}")
    )
    payload = {
        "generated_ms": int(time.time() * 1000),
        "period": args.period,
        "symbols": list(bars_by_symbol),
        "fee_bps_per_side": args.fee_bps,
        "slippage_bps": args.slippage_bps,
        "round_trip_bps": round_trip_bps,
        "starting_equity": args.equity,
        "profiles": [r.as_dict() for r in results],
        "selection": selection.as_dict(),
        "missed_audit": {**audit.as_dict(), "arm": focus},
    }

    if args.out == "json":
        print(json.dumps(payload, indent=1))
    else:
        print()
        print(text)

    if args.out_dir:
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "profiles.txt").write_text(text)
        (out / "profiles.json").write_text(json.dumps(payload, indent=1))
        log(f"\n[out] {out}/profiles.txt, profiles.json")

    return 0


def _arm_of(rejection) -> str:
    """Which arm refused this candidate.

    ``compare_profiles`` pools every arm's refusals into one sink, so the audit
    has to filter: labelling twelve arms' refusals together would count the
    same market moment up to twelve times and make an over-sampled gate look
    like a well-calibrated one.
    """
    return getattr(rejection, "arm", "")


if __name__ == "__main__":
    raise SystemExit(main())
