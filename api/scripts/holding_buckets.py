#!/usr/bin/env python3
"""NET performance by holding bucket, per strategy -- never pooled.

Phase 6 §20. Pooling flow_impulse (24s median) with momentum_scalp (27s median
but a 5-minute tail) and book_imbalance (89s) produces one row that describes
none of them. The brief is explicit about this and it is right: the whole point
of strategy-specific holding policy is that the strategies have different
natural horizons, and a report that averages over them cannot show that.

Also reports the exit-reason mix, because a holding-time distribution without
it cannot distinguish "the strategy decided" from "a timer fired".

    python scripts/holding_buckets.py --trades /tmp/phase5/live_trades_full.json
"""

from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: Exactly the buckets the brief names.
BUCKETS: tuple[tuple[float, float, str], ...] = (
    (0, 30, "<30s"), (30, 60, "30-60s"), (60, 120, "1-2m"), (120, 300, "2-5m"),
    (300, 600, "5-10m"), (600, 1500, "10-25m"), (1500, 3600, "25-60m"),
    (3600, float("inf"), ">60m"),
)


def bucket_of(seconds: float) -> str:
    for lo, hi, name in BUCKETS:
        if lo <= seconds < hi:
            return name
    return BUCKETS[-1][2]


def main() -> int:
    ap = argparse.ArgumentParser(prog="holding_buckets")
    ap.add_argument("--trades", required=True, type=Path)
    ap.add_argument("--out", default="/tmp/phase6/holding_buckets.json")
    args = ap.parse_args()

    rows = json.loads(args.trades.read_text())
    for r in rows:
        r["hold_s"] = (int(r["exit_ms"]) - int(r["entry_ms"])) / 1000.0
    by_strategy: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        by_strategy[r["strategy"]].append(r)

    print(f"\nHOLDING TIME BY STRATEGY ({len(rows)} trades)\n")
    print(f"{'strategy':<17}{'n':>5}{'median':>9}{'mean':>8}{'min':>7}{'max':>8}"
          f"{'net $':>10}   exit reasons")
    print("-" * 108)
    summary: dict[str, dict] = {}
    for st in sorted(by_strategy, key=lambda k: -len(by_strategy[k])):
        ts = by_strategy[st]
        h = [r["hold_s"] for r in ts]
        net = sum(float(r["net_pnl_usd"]) for r in ts)
        reasons = dict(collections.Counter(r.get("exit_reason") for r in ts))
        print(f"{st:<17}{len(ts):>5}{statistics.median(h):>8.0f}s"
              f"{statistics.fmean(h):>7.0f}s{min(h):>6.0f}s{max(h):>7.0f}s"
              f"{net:>10.2f}   {reasons}")
        summary[st] = {
            "trades": len(ts), "median_hold_s": statistics.median(h),
            "mean_hold_s": statistics.fmean(h), "min_hold_s": min(h),
            "max_hold_s": max(h), "net_pnl": net, "exit_reasons": reasons,
        }

    print(f"\n\nNET P&L BY HOLDING BUCKET, PER STRATEGY\n")
    names = [b[2] for b in BUCKETS]
    header = f"{'strategy':<17}" + "".join(f"{n:>11}" for n in names)
    print(header)
    print("-" * len(header))
    grid: dict[str, dict[str, dict]] = {}
    for st in sorted(by_strategy, key=lambda k: -len(by_strategy[k])):
        line = f"{st:<17}"
        grid[st] = {}
        for lo, hi, name in BUCKETS:
            sel = [r for r in by_strategy[st] if lo <= r["hold_s"] < hi]
            if not sel:
                # An empty bucket renders empty, not as a zero: "no trades
                # here" and "these trades netted nothing" are different facts.
                line += f"{'—':>11}"
                continue
            net = sum(float(r["net_pnl_usd"]) for r in sel)
            line += f"{f'{net:+.2f}({len(sel)})':>11}"
            grid[st][name] = {"trades": len(sel), "net_pnl": net}
        print(line)

    empty = [n for n in names
             if not any(n in grid[st] for st in grid)]
    if empty:
        print(f"\nNo trade has ever landed in: {', '.join(empty)}.")
        print("The 25-minute checkpoint and 60-minute time stop have never fired.")

    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"by_strategy": summary, "buckets": grid}, indent=1))
    print(f"\n[out] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
