#!/usr/bin/env python3
"""How much activity can this venue sustain before NET expectancy turns?

Four profiles relax SETUP-GENERATION thresholds only. Fees, spread, slippage,
the NET EV gate, risk and accounting are identical in every arm -- a profile
can only add trades by finding setups that clear their own unchanged costs.

The table is the tradeoff curve, not a search for an optimum. Picking the best
cell of a 4-arm sweep on one venue is how a backtest gets fitted.

    BACKTEST_CACHE_DIR=/tmp/bt-cache python scripts/activity_profiles.py \
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

PROFILES = {
    "CURRENT":       (1.00, 1.00),
    "MODERATE":      (0.70, 0.85),
    "ACTIVE":        (0.45, 0.70),
    "HIGH_ACTIVITY": (0.25, 0.55),
}


def main() -> int:
    ap = argparse.ArgumentParser(prog="activity_profiles")
    ap.add_argument("--symbols", default="BTCUSD,ETHUSD,SOLUSD")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--equity", type=float, default=5000.0)
    ap.add_argument("--out", default="/tmp/phase8/profiles.json")
    args = ap.parse_args()

    os.environ.setdefault("FEE_MODEL", "tier0")
    from app.backtest.data import load_cached

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.days * MS_PER_DAY
    bars = {}
    for sym in [x.strip() for x in args.symbols.split(",") if x.strip()]:
        b = [x for x in load_cached(sym, "1m") if start_ms <= x.open_ms <= end_ms]
        if b:
            bars[sym] = b
        print(f"[load] {sym}: {len(b):,}", flush=True)
    if not bars:
        return 2
    span_days = max(1e-9, (max(max(x.open_ms for x in v) for v in bars.values())
                           - start_ms) / MS_PER_DAY)

    rows = []
    for name, (relax, score_relax) in PROFILES.items():
        os.environ["ENTRY_RELAX"] = str(relax)
        os.environ["ENTRY_MIN_SCORE_RELAX"] = str(score_relax)
        from app.config import reload_settings
        s = reload_settings()
        from app.backtest.engine import BacktestConfig, run_backtest

        cfg = BacktestConfig(starting_equity=args.equity, fee_bps_per_side=1.0,
                             slippage_bps=2.0, latency_bars=1,
                             risk_per_trade_pct=0.5, max_concurrent=20, seed=7)
        print(f"[arm] {name} relax={relax} score={score_relax}", flush=True)
        r = run_backtest(bars, cfg, label=name)
        t = r.trades
        nets = [x.net_pnl_usd for x in t]
        wins = [x for x in nets if x > 0]
        losses = [-x for x in nets if x < 0]
        eq = peak = dd = 0.0
        for x in sorted(t, key=lambda z: z.exit_ms):
            eq += x.net_pnl_usd
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        notional = sum(x.notional_usd for x in t)
        rows.append({
            "profile": name, "relax": relax, "score_relax": score_relax,
            "setups": r.signals_generated,
            "setups_per_day": r.signals_generated / span_days,
            "qualified": r.entries_approved,
            "trades": len(t),
            "trades_per_day": len(t) / span_days,
            "net_pnl": sum(nets),
            "profit_factor": (sum(wins) / sum(losses)) if losses else float("inf"),
            "expectancy": statistics.fmean(nets) if nets else 0.0,
            "win_rate": (len(wins) / len(t)) if t else 0.0,
            "max_drawdown": dd,
            "turnover": notional / args.equity,
            "turnover_per_day": notional / args.equity / span_days,
            "fees": sum(x.fees_usd for x in t),
            "slippage": sum(abs(x.slippage_usd) for x in t),
            "median_hold_s": statistics.median([x.holding_seconds for x in t]) if t else 0,
        })

    print(f"\n{'profile':<15}{'setups/day':>13}{'trades/day':>11}{'turnover/d':>11}"
          f"{'NET $':>11}{'PF':>7}{'expectancy':>11}{'hit':>8}{'max DD':>10}")
    print("-" * 97)
    for r in rows:
        pf = r["profit_factor"]
        print(f"{r['profile']:<15}{r['setups_per_day']:>13.1f}{r['trades_per_day']:>11.2f}"
              f"{r['turnover_per_day']:>11.3f}{r['net_pnl']:>11.2f}"
              f"{(pf if pf != float('inf') else 999):>7.2f}{r['expectancy']:>11.4f}"
              f"{r['win_rate']*100:>7.1f}%{r['max_drawdown']:>10.2f}")

    print("\nRULE: pick the most active profile among those that keep a positive\n      NET expectancy AND a profit factor above 1.")
    ok = [r for r in rows if r["expectancy"] > 0 and r["profit_factor"] > 1.0]
    if ok:
        best = max(ok, key=lambda r: r["trades_per_day"])
        print(f"  -> {best['profile']}: {best['trades_per_day']:.2f} trades/day, "
              f"PF {best['profit_factor']:.2f}, expectancy {best['expectancy']:+.4f}")
    else:
        print("  -> NONE. No profile has a positive NET expectancy; on this data,")
        print("     raising activity does nothing but enlarge the loss.")

    p = Path(args.out); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rows, indent=1, default=str))
    print(f"\n[out] {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
