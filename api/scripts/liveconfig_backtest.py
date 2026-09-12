#!/usr/bin/env python3
"""Replay 120 days through the LIVE configuration and dump every trade.

The P&L audit left one thing unresolved: a 20-hour live sample said CASE A
(raw PF 4.251) and a 120-day backtest said CASE B (raw PF 0.791). They are not
comparable, because they were not the same signal -- the backtest ran with
different fee, edge-floor and concurrency settings than the deployed worker.

This runs the 120-day replay under the *deployed* settings, read from the same
names the container's .env sets, so the two samples finally differ only in the
period they cover. It writes the raw ClosedTrade rows so `pnl_audit.py
--source json` can decompose them with the identical arithmetic used on live.

    BACKTEST_CACHE_DIR=/tmp/bt-cache python scripts/liveconfig_backtest.py \
        --symbols BTCUSD,ETHUSD,SOLUSD,ADAUSD --days 120
"""

from __future__ import annotations

import argparse

import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.backtest.data import load_cached  # noqa: E402
from app.backtest.engine import BacktestConfig, run_backtest  # noqa: E402

MS_PER_DAY = 86_400_000

# Exactly what /srv/netedge/.env sets on the box, for the knobs the
# backtest harness can honour. Anything not listed here is already identical
# because both paths read app.config defaults.
LIVE_ENV = {
    "FEE_MODEL": "tier0",
    "UNIVERSE_RESTRICT_TIER0": "true",
    "STARTING_EQUITY": "500",
    "RISK_PER_TRADE_PCT": "0.5",
    "MAX_CONCURRENT_POSITIONS": "3",
    "MAX_POSITION_PCT_EQUITY": "35",
    "MAX_TOTAL_EXPOSURE_PCT": "90",
    "MAX_CONCURRENT_RISK_PCT": "1.5",
    "DAILY_LOSS_LIMIT_PCT": "3",
    "WEEKLY_LOSS_LIMIT_PCT": "7",
    "LOSS_STREAK_LEN": "4",
    "LOSS_STREAK_COOLDOWN_MINUTES": "45",
    "MIN_NET_EDGE_BPS": "1.0",
    "MIN_NET_EDGE_USD": "0.02",
    "MIN_NOTIONAL_USD": "10",
    "SIM_LATENCY_MS": "250",
    "SIM_ADVERSE_BPS": "0.5",
    "SIM_LIMIT_MISS_RATIO": "0.35",
}


def main() -> int:
    ap = argparse.ArgumentParser(prog="liveconfig_backtest")
    ap.add_argument("--symbols", default="BTCUSD,ETHUSD,SOLUSD,ADAUSD")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--fee-bps", type=float, default=1.0,
                    help="per side; tier-0 taker is 0.01%% = 1 bps")
    ap.add_argument("--slippage-bps", type=float, default=2.0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--end-ms", type=int, default=None)
    ap.add_argument("--start-ms", type=int, default=None,
                    help="replay exactly this window instead of --days")
    ap.add_argument("--warmup-bars", type=int, default=240,
                    help="bars fed before --start-ms so features are warm")
    ap.add_argument("--out", default="/tmp/phase5/liveconfig_trades.json")
    args = ap.parse_args()

    for k, v in LIVE_ENV.items():
        os.environ[k] = v
    from app.config import reload_settings
    reload_settings()

    end_ms = args.end_ms or int(time.time() * 1000)
    if args.start_ms:
        # The feature engine needs history before the first decision, so the
        # replay starts earlier than the window and the warm-up bars simply
        # do not produce trades.
        start_ms = args.start_ms - args.warmup_bars * 60_000
    else:
        start_ms = end_ms - args.days * MS_PER_DAY
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    bars_by_symbol = {}
    for sym in symbols:
        bars = [b for b in load_cached(sym, "1m") if start_ms <= b.open_ms <= end_ms]
        if bars:
            bars_by_symbol[sym] = bars
        print(f"[load] {sym}: {len(bars):,} bars", flush=True)
    if not bars_by_symbol:
        print("no cached bars — run scripts/download_tape.py first", file=sys.stderr)
        return 2

    cfg = BacktestConfig(
        starting_equity=500.0,
        fee_bps_per_side=args.fee_bps,
        spread_bps=None,
        slippage_bps=args.slippage_bps,
        latency_bars=1,
        risk_per_trade_pct=0.5,
        max_concurrent=3,
        strategies=None,
        seed=args.seed,
    )

    result = run_backtest(bars_by_symbol, cfg, label=f"live-cfg-{args.days}d")

    rows = []
    for t in result.trades:
        d = asdict(t)
        d["trade_id"] = t.id
        d["side"] = t.side.value if hasattr(t.side, "value") else str(t.side)
        d["exit_reason"] = (t.exit_reason.value if hasattr(t.exit_reason, "value")
                            else str(t.exit_reason))
        rows.append(d)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=1, default=str))

    s = result.stats
    print(f"\n[result] {len(rows)} trades  net ${s.net_pnl:+.4f}  "
          f"PF {s.profit_factor:.3f}  win {s.win_rate * 100:.1f}%")
    print(f"[result] signals {result.signals_generated:,}  approved "
          f"{result.entries_approved:,}  filled {result.entries_filled:,}")
    print(f"[result] rejections: {dict(sorted(result.rejections.items(), key=lambda kv: -kv[1])[:6])}")
    for note in result.notes[:5]:
        print(f"[note] {note}")
    print(f"[out] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
