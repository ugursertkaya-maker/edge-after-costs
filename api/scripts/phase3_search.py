#!/usr/bin/env python3
"""Drive the Phase 3 profitability search end to end and publish the result.

    BACKTEST_CACHE_DIR=/tmp/bt-cache python scripts/phase3_search.py \
        --symbols BTCUSD,ETHUSD,SOLUSD,ADAUSD --days 120 --jobs 4 \
        --out-dir /tmp/phase3

Runs the tournament, puts every arm through the robustness gates, writes the
text report plus the two JSON files the dashboard reads, and says plainly when
nothing qualified — which, given everything measured so far, is a likely and
perfectly valid outcome.

Sub-minute intervals are deliberately excluded by default: on this venue 90% of
5-second buckets and 66% of 30-second buckets contain no trade at all, so those
timeframes carry no information and cost ~1.3 GB to materialise.
"""

from __future__ import annotations

import argparse
import dataclasses
import gc
import json
import sys
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.research import robustness as rb  # noqa: E402
from app.research.dataset import load_dataset  # noqa: E402
from app.research.tournament import (  # noqa: E402
    TournamentConfig,
    format_tournament,
    run_tournament,
)

DEFAULT_INTERVALS = ("1m", "5m", "15m")


def stats_dict(s) -> dict:
    if s is None:
        return {}
    return {
        "trades": int(getattr(s, "trades", 0) or 0),
        "win_rate": round(float(getattr(s, "win_rate", 0.0) or 0.0), 4),
        "profit_factor": round(float(getattr(s, "profit_factor", 0.0) or 0.0), 4),
        "expectancy": round(float(getattr(s, "expectancy", 0.0) or 0.0), 6),
        "net_pnl": round(float(getattr(s, "net_pnl", 0.0) or 0.0), 4),
        "max_drawdown": round(float(getattr(s, "max_drawdown", 0.0) or 0.0), 4),
    }


def arm_dict(arm, report) -> dict:
    c = arm.candidate
    s = arm.stats
    oos = arm.out_of_sample
    status = "FAILED"
    if report is not None:
        if getattr(report, "passed", False):
            status = "FINALIST"
        elif getattr(report, "marginal", False):
            status = "MARGINAL"
    if int(getattr(oos, "trades", 0) or 0) < 30:
        status = "INSUFFICIENT_SAMPLE"
    regime = getattr(c, "regime", None)
    return {
        "key": c.key(),
        "family": c.family,
        "symbol": c.symbol,
        "regime": getattr(regime, "value", regime),
        "horizon_seconds": int(c.horizon_seconds),
        "exit_profile": c.exit_profile,
        "stop_profile": c.stop_profile,
        "trades": int(getattr(s, "trades", 0) or 0),
        "win_rate": round(float(getattr(s, "win_rate", 0.0) or 0.0), 4),
        "avg_win": round(float(getattr(s, "avg_win", 0.0) or 0.0), 4),
        "avg_loss": round(float(getattr(s, "avg_loss", 0.0) or 0.0), 4),
        "profit_factor": round(float(getattr(s, "profit_factor", 0.0) or 0.0), 4),
        "expectancy": round(float(getattr(s, "expectancy", 0.0) or 0.0), 6),
        "fees": round(float(getattr(arm, "fees_usd", 0.0) or 0.0), 4),
        "slippage": round(float(getattr(arm, "slippage_usd", 0.0) or 0.0), 4),
        "net_pnl": round(float(getattr(s, "net_pnl", 0.0) or 0.0), 4),
        "max_drawdown": round(float(getattr(s, "max_drawdown", 0.0) or 0.0), 4),
        "in_sample": stats_dict(arm.in_sample),
        "out_of_sample": stats_dict(oos),
        "oos_status": status,
    }


def finalist_dict(arm, report) -> dict:
    base = arm_dict(arm, report)
    mc = getattr(report, "monte_carlo", None)
    return {
        **{k: base[k] for k in
           ("key", "family", "symbol", "regime", "horizon_seconds",
            "exit_profile", "stop_profile", "out_of_sample")},
        "passed": bool(getattr(report, "passed", False)),
        "reasons": list(getattr(report, "reasons", []) or []),
        "stress": [
            {
                "label": getattr(s, "label", "?"),
                "survived": bool(getattr(s, "survived", False)),
                "net_pnl": round(float(getattr(getattr(s, "stats", None), "net_pnl", 0.0) or 0.0), 4),
            }
            for s in (getattr(report, "stress", []) or [])
        ],
        "monte_carlo": (
            {
                "median_drawdown": round(float(mc.median_drawdown), 4),
                "p95_drawdown": round(float(mc.p95_drawdown), 4),
                "worst_loss_streak": int(mc.worst_loss_streak),
                "prob_below_start": round(float(mc.prob_below_start), 4),
                "samples": int(mc.samples),
            }
            if mc is not None and is_dataclass(mc) else None
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(prog="phase3_search")
    ap.add_argument("--symbols", default="BTCUSD,ETHUSD,SOLUSD,ADAUSD")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--tape-cache", default="/tmp/tape-cache")
    ap.add_argument("--out-dir", default="/tmp/phase3")
    ap.add_argument("--fee-bps", type=float, default=1.0)
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--max-candidates", type=int, default=400,
                    help="per symbol")
    ap.add_argument("--intervals", default=",".join(DEFAULT_INTERVALS))
    ap.add_argument("--equity", type=float, default=500.0)
    ap.add_argument("--families", default=None,
                    help="comma separated subset; default every family")
    ap.add_argument("--max-params", type=int, default=8,
                    help="parameter sets per family. Lower it to spend the "
                         "budget on the exit/stop/entry axes instead: the "
                         "one-factor sweeps are enumerated per (family, params), "
                         "so 8 params x 10 families exhausts a small budget on "
                         "the family axis before the exit axis is reached.")
    ap.add_argument("--baseline-horizon", type=int, default=60)
    args = ap.parse_args()

    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    intervals = tuple(s.strip() for s in args.intervals.split(",") if s.strip())
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.days * 86_400_000

    # One symbol at a time. Holding four 120-day datasets plus a process pool
    # that forks a copy of each is what killed the first run: ~400k prints and
    # ~170k bars per symbol, multiplied by the workers. Per-symbol keeps the
    # peak bounded and costs nothing, because a candidate never spans symbols.
    cfg = TournamentConfig(
        symbols=symbols,
        families=(
            tuple(f.strip() for f in args.families.split(",") if f.strip())
            if args.families else None
        ),
        fee_bps_per_side=args.fee_bps,
        starting_equity=args.equity,
        jobs=args.jobs,
        max_candidates=args.max_candidates,
        max_params_per_family=args.max_params,
        baseline_horizon_minutes=args.baseline_horizon,
        tape_cache_dir=args.tape_cache,
    )

    print(f"[search] {len(symbols)} symbols, {args.days}d, intervals={intervals}, "
          f"budget {cfg.max_candidates}/symbol, jobs={cfg.jobs}")
    arms: list = []
    t_all = time.time()
    for sym in symbols:
        t0 = time.time()
        ds = load_dataset(sym, start_ms, end_ms, intervals=intervals,
                          cache_dir=args.tape_cache)
        bars = ds.bars.get("1m") or next(iter(ds.bars.values()), [])
        print(f"[{sym}] {len(bars):,} 1m bars, {len(ds.trades):,} prints "
              f"({time.time() - t0:.1f}s)")

        sym_cfg = dataclasses.replace(cfg, symbols=(sym,))
        t0 = time.time()
        got = run_tournament({sym: ds}, config=sym_cfg,
                             on_progress=lambda m, s=sym: print(f"  [{s}] {m}"))
        arms.extend(got)
        print(f"[{sym}] {len(got)} arms in {time.time() - t0:.0f}s")

        del ds, bars, got
        gc.collect()
    wall = time.time() - t_all
    print(f"[search] {len(arms)} arms total in {wall:.0f}s")

    print("[gates] evaluating robustness")
    reports = {}
    for arm in arms:
        try:
            reports[arm.candidate.key()] = rb.evaluate_gates(arm, equity=args.equity)
        except Exception as exc:  # a gate failure must not lose the whole run
            print(f"  gate error on {arm.candidate.key()}: {exc!r}")

    passing = [a for a in arms if getattr(reports.get(a.candidate.key()), "passed", False)]

    text = format_tournament(arms, config=cfg)
    text += "\n\n" + rb.format_gate_reports(
        [reports[a.candidate.key()] for a in arms if a.candidate.key() in reports]
    )
    (out / "tournament.txt").write_text(text)

    (out / "tournament.json").write_text(json.dumps({
        "generated_ms": int(time.time() * 1000),
        "candidates_evaluated": len(arms),
        "wall_clock_seconds": round(wall, 1),
        "period": f"{args.days}d",
        "fee_bps": args.fee_bps,
        "arms": [arm_dict(a, reports.get(a.candidate.key())) for a in arms[:400]],
    }, indent=1))

    (out / "finalists.json").write_text(json.dumps({
        "finalists": [finalist_dict(a, reports[a.candidate.key()]) for a in passing],
    }, indent=1))

    print(text[-4000:])
    print(f"\n[out] {out}/tournament.txt, tournament.json, finalists.json")
    print(f"[result] {len(passing)} finalist(s) out of {len(arms)} arms")
    if not passing:
        print("No strategy passed the promotion gates. That is the search's "
              "answer, not a missing step — do not lower the gates to find one.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
