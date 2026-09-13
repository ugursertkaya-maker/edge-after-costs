#!/usr/bin/env python3
"""Which holding horizon does Binance.US actually pay for?

Measured over 30 days, the median absolute move is 0.00 bps at 1 minute, 4.45
at 5, 10.20 at 15 and 22.36 at 60, against a ~3 bps round trip. The
ultra-short configuration was economically mismatched to the venue. This
sweeps the 15-60 minute band on the same historical data and reports, per
horizon, what the real production stack does with it.

Every arm runs the REAL objects -- FeatureEngine, RegimeClassifier, the
strategies, OpportunityRanker, RiskEngine, ExitEngine, PaperSimulator, Ledger
-- through app.backtest.engine, so a number here is a number the live worker
would have produced.

Validation is chronological walk-forward: each window trains on nothing (there
is no fitting here) but is REPORTED separately, so a horizon that works in one
month and not the others cannot hide inside a pooled total.

    BACKTEST_CACHE_DIR=/tmp/bt-cache python scripts/horizon_research.py \
        --symbols BTCUSD,ETHUSD,SOLUSD --days 120
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MS_PER_DAY = 86_400_000

#: Research grid, expressed as MULTIPLIERS on the horizon-aware expected move
#: rather than as fixed bps. Fixed bps would be a different target on a quiet
#: day than on a busy one while claiming to be the same arm; a multiplier is
#: the same instruction in both.
#:
#: The multipliers are chosen so the resulting targets land inside the ranges
#: the brief asks for, given the measured median moves (15m 10.2 bps, 25m ~13,
#: 40m ~17, 60m 22.4):
#:
#:   TP x0.5 / x1.0 / x1.5   ->  15m: 5.1 / 10.2 / 15.3 bps
#:   SL x0.35 / x0.6 / x0.9  ->  15m: 3.6 /  6.1 /  9.2 bps
#:
#: Three TP by two SL per horizon. A finer grid over three symbols of history
#: buys overfitting, not knowledge.
#: Widened from the brief's suggested bands after measuring what a position
#: actually has to survive. Median adverse excursion on ETHUSD:
#:
#:   15m  5.58 bps (p75 17.09)   median move 10.20
#:   25m  9.34      (p75 22.93)               13.89
#:   40m 12.60      (p75 30.47)               18.03
#:   60m 17.62      (p75 38.48)               22.36
#:
#: The brief proposed 3-8 bps stops at 15 minutes; the median drawdown alone
#: is 5.58 and the p75 is 17.09, so that band stops out before the setup can
#: develop -- which is exactly what the first run showed (56 of 69 exits were
#: STOP_LOSS at a 2-minute median hold against a 40-minute horizon).
TP_MULTS = (1.0, 1.5, 2.0)
SL_MULTS = (0.6, 1.0, 1.5)
HORIZONS = (900, 1500, 2400, 3600)      # 15 / 25 / 40 / 60 minutes


def _chronological_windows(trades, start_ms: int, win_ms: int, n: int) -> list[dict]:
    """Per-window statistics, in time order.

    Nothing is fitted here, so this is a stability check rather than a
    train/test split -- and calling it that matters. What it catches is the
    failure this project has hit before: a horizon that works in one month,
    fails in the others, and hides inside a pooled total.
    """
    out = []
    for i in range(max(1, n)):
        lo = start_ms + i * win_ms
        hi = lo + win_ms
        sel = [t for t in trades if lo <= t.exit_ms < hi]
        s = _stats(sel)
        s["window"] = i + 1
        out.append(s)
    return out


def _stats(trades) -> dict:
    if not trades:
        return {"trades": 0}
    nets = [t.net_pnl_usd for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [-x for x in nets if x < 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    for t in sorted(trades, key=lambda x: x.exit_ms):
        eq += t.net_pnl_usd
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    holds = [t.holding_seconds for t in trades]
    return {
        "trades": len(trades),
        "net_pnl": sum(nets),
        "profit_factor": (sum(wins) / sum(losses)) if losses else float("inf"),
        "expectancy": statistics.fmean(nets),
        "win_rate": len(wins) / len(trades),
        "median_hold_s": statistics.median(holds),
        "max_drawdown": dd,
        "fees": sum(t.fees_usd for t in trades),
        "slippage": sum(abs(t.slippage_usd) for t in trades),
        "gross_notional": sum(t.notional_usd for t in trades),
    }


def run_arm(bars, *, horizon_s: int, tp: float, sl: float, cfg_kwargs: dict,
            label: str) -> dict:
    """One (horizon, TP-mult, SL-mult) arm through the real stack."""
    os.environ["SLOW_HORIZON_SECONDS"] = str(horizon_s)
    # Applied as the strategies' own multipliers on the horizon move, which is
    # exactly what the live path uses. Relabelling exits after the fact would
    # measure a strategy we cannot deploy.
    os.environ["STRATEGY_TARGET_MULT"] = str(tp) if tp > 0 else "0"
    os.environ["STRATEGY_STOP_MULT"] = str(sl) if sl > 0 else "0"
    os.environ["HORIZON_TIME_STOP_SECONDS"] = str(int(horizon_s * 1.5))
    os.environ["HORIZON_CHECKPOINT_SECONDS"] = str(int(horizon_s * 0.8))
    from app.config import reload_settings
    reload_settings()
    from app.backtest.engine import BacktestConfig, run_backtest

    cfg = BacktestConfig(**cfg_kwargs)
    result = run_backtest(bars, cfg, label=label)
    out = _stats(result.trades)
    out["_trades"] = result.trades
    # The realised target/stop in bps, so the multiplier grid can be read in
    # the units the brief asked for.
    # ClosedTrade carries the edge the ranker expected, not the raw target, so
    # that is what gets reported -- inventing a target column the record does
    # not hold would be worse than leaving it out.
    edges = [t.expected_net_edge_bps for t in result.trades]
    out.update({
        "horizon_s": horizon_s, "tp_mult": tp, "sl_mult": sl,
        "median_expected_edge_bps": statistics.median(edges) if edges else None,
        "signals": result.signals_generated,
        "approved": result.entries_approved,
        "filled": result.entries_filled,
        "rejections": dict(result.rejections),
    })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(prog="horizon_research")
    ap.add_argument("--symbols", default="BTCUSD,ETHUSD,SOLUSD")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--windows", type=int, default=4, help="walk-forward windows")
    ap.add_argument("--fee-bps", type=float, default=1.0)
    ap.add_argument("--equity", type=float, default=5000.0)
    ap.add_argument("--out", default="/tmp/phase7/horizons.json")
    args = ap.parse_args()

    os.environ.setdefault("FEE_MODEL", "tier0")
    from app.backtest.data import load_cached

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.days * MS_PER_DAY
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    bars_all = {}
    for sym in symbols:
        b = [x for x in load_cached(sym, "1m") if start_ms <= x.open_ms <= end_ms]
        if b:
            bars_all[sym] = b
        print(f"[load] {sym}: {len(b):,} bars", flush=True)
    if not bars_all:
        print("no cached bars", file=sys.stderr)
        return 2

    span = max(max(b.open_ms for b in v) for v in bars_all.values()) - start_ms
    win = span // max(1, args.windows)
    cfg_kwargs = dict(
        starting_equity=args.equity, fee_bps_per_side=args.fee_bps,
        slippage_bps=2.0, latency_bars=1, risk_per_trade_pct=0.5,
        max_concurrent=20, seed=7,
    )

    results = []
    # Baseline: the shipped short-horizon configuration.
    print("\n[baseline] current short horizon", flush=True)
    results.append({**run_arm(bars_all, horizon_s=0, tp=0, sl=0,
                              cfg_kwargs=cfg_kwargs, label="baseline"),
                    "arm": "baseline (mevcut)"})

    for horizon_s in HORIZONS:
        for tp in TP_MULTS:
            for sl in SL_MULTS:
                name = f"{horizon_s//60}m T{tp}/S{sl}"
                print(f"[arm] {name}", flush=True)
                r = run_arm(bars_all, horizon_s=horizon_s, tp=tp, sl=sl,
                            cfg_kwargs=cfg_kwargs, label=name)
                r["arm"] = name
                r["windows"] = _chronological_windows(
                    r.pop("_trades", []), start_ms, win, args.windows)
                results.append(r)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=1, default=str))

    print(f"\n{'arm':<22}{'trades':>7}{'NET $':>10}{'PF':>7}{'expectancy':>10}"
          f"{'hit':>8}{'median hold':>14}{'max DD':>10}")
    print("-" * 88)
    for r in results:
        if not r.get("trades"):
            print(f"{r['arm']:<22}{0:>7}{'—':>10}{'—':>7}{'—':>10}{'—':>8}{'—':>14}{'—':>10}")
            continue
        pf = r["profit_factor"]
        print(f"{r['arm']:<22}{r['trades']:>7}{r['net_pnl']:>10.2f}"
              f"{(pf if pf != float('inf') else 999):>7.2f}{r['expectancy']:>10.4f}"
              f"{r['win_rate']*100:>7.1f}%{r['median_hold_s']:>13.0f}s"
              f"{r['max_drawdown']:>10.2f}")
    print(f"\n[out] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
