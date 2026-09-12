#!/usr/bin/env python3
"""Is flow_impulse ready for PAPER champion promotion? Report, never promote.

Phase 6 §24. The brief is explicit: do NOT promote because the first 48 trades
look excellent. This computes every gate and prints the verdict, and the sample
gate is first because no other number means anything until it passes.

The concentration checks exist because 48 trades over 12.66 hours in a single
rally can be excellent for reasons that have nothing to do with the signal. A
strategy that earns everything on one coin, in one hour, in one regime has not
been measured -- it has been sampled once.

    python scripts/flow_gates.py --trades /tmp/phase5/live_trades_full.json
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPORT_AT = (100, 200, 500)
#: Promotion needs the first of these; everything below it is a progress report.
MIN_SAMPLE = REPORT_AT[0]


def _concentration(values: list[str]) -> tuple[str, float]:
    """The largest single share, and what holds it."""
    if not values:
        return ("—", 0.0)
    c = collections.Counter(values)
    top, n = c.most_common(1)[0]
    return (top, n / len(values))


def main() -> int:
    ap = argparse.ArgumentParser(prog="flow_gates")
    ap.add_argument("--trades", required=True, type=Path)
    ap.add_argument("--strategy", default="flow_impulse")
    ap.add_argument("--out", default="/tmp/phase6/flow_gates.json")
    args = ap.parse_args()

    rows = [r for r in json.loads(args.trades.read_text())
            if r["strategy"] == args.strategy]
    n = len(rows)
    net = [float(r["net_pnl_usd"]) for r in rows]
    wins = [x for x in net if x > 0]
    losses = [-x for x in net if x < 0]

    pf = sum(wins) / sum(losses) if losses else float("inf")
    expectancy = statistics.fmean(net) if net else 0.0
    win_rate = len(wins) / n if n else 0.0

    equity, peak, dd = 0.0, 0.0, 0.0
    for r in sorted(rows, key=lambda r: r["exit_ms"]):
        equity += float(r["net_pnl_usd"])
        peak = max(peak, equity)
        dd = max(dd, peak - equity)

    coin, coin_share = _concentration([r["symbol"] for r in rows])
    regime, regime_share = _concentration([str(r.get("regime") or "unknown")
                                           for r in rows])
    day, day_share = _concentration([
        str(int(r["entry_ms"]) // 86_400_000) for r in rows
    ])

    beta = None
    p = Path("/tmp/phase6/beta_check.json")
    if not p.exists():
        p = Path("/tmp/phase5/beta_check.json")
    if p.exists():
        beta = json.loads(p.read_text()).get(args.strategy)

    print(f"\n{'=' * 72}")
    print(f"PROMOTION GATES — {args.strategy}")
    print("=" * 72)
    print(f"\n  SAMPLE          {n} trades"
          f"   (report at {', '.join(str(x) for x in REPORT_AT)})")
    bar = int(min(1.0, n / MIN_SAMPLE) * 40)
    print(f"                  [{'#' * bar}{'.' * (40 - bar)}] "
          f"{n}/{MIN_SAMPLE} toward the first checkpoint")

    print(f"\n  net profit factor   {pf:>10.3f}")
    print(f"  net expectancy      {expectancy:>10.4f} $/trade")
    print(f"  win rate            {win_rate * 100:>10.1f}%")
    print(f"  median trade        {statistics.median(net):>10.4f} $")
    print(f"  max drawdown        {dd:>10.4f} $")
    print(f"  total net           {sum(net):>10.4f} $")
    if beta:
        lo, hi = beta["ci95"]
        print(f"  alpha vs drift      {beta['alpha_bps']:>10.2f} bps "
              f"[{lo:+.2f}, {hi:+.2f}]")

    print(f"\n  coin concentration    {coin_share * 100:>5.1f}%  ({coin})")
    print(f"  regime concentration  {regime_share * 100:>5.1f}%  ({regime})")
    print(f"  day concentration     {day_share * 100:>5.1f}%  (one calendar day)")

    gates = [
        (f"sample >= {MIN_SAMPLE} trades", n >= MIN_SAMPLE, f"{n}"),
        ("net profit factor > 1.30", pf > 1.30, f"{pf:.3f}"),
        ("net expectancy > 0", expectancy > 0, f"{expectancy:+.4f}"),
        ("alpha CI clear of zero", bool(beta and beta["ci95"][0] > 0),
         f"{beta['ci95'][0]:+.2f}" if beta else "not measured"),
        ("coin concentration < 60%", coin_share < 0.60, f"{coin_share * 100:.1f}%"),
        ("day concentration < 60%", day_share < 0.60, f"{day_share * 100:.1f}%"),
        ("regime concentration < 80%", regime_share < 0.80,
         f"{regime_share * 100:.1f}%"),
        ("max drawdown < 25% of net", dd < 0.25 * max(sum(net), 1e-9),
         f"{dd:.4f}"),
    ]
    print(f"\n{'gate':<34}{'result':>12}   verdict")
    print("-" * 72)
    for name, ok, value in gates:
        print(f"{name:<34}{value:>12}   {'PASS' if ok else 'FAIL'}")

    passed = all(ok for _, ok, _ in gates)
    print("\n" + "=" * 72)
    if passed:
        print("  READY FOR PAPER CHAMPION PROMOTION: YES")
    else:
        failed = [name for name, ok, _ in gates if not ok]
        print("  READY FOR PAPER CHAMPION PROMOTION: **NO**")
        print(f"  failing: {', '.join(failed)}")
        if n < MIN_SAMPLE:
            print(f"\n  The sample gate is the one that matters. At {n} trades every")
            print("  other number on this page is an estimate with a confidence")
            print("  interval wider than the effect it is measuring. Keep collecting.")
    print("=" * 72)

    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "strategy": args.strategy, "trades": n, "net_profit_factor": pf,
        "net_expectancy": expectancy, "win_rate": win_rate,
        "median_trade": statistics.median(net) if net else 0.0,
        "max_drawdown": dd, "total_net": sum(net),
        "coin_concentration": {"top": coin, "share": coin_share},
        "regime_concentration": {"top": regime, "share": regime_share},
        "day_concentration": {"share": day_share},
        "alpha": beta,
        "gates": [{"gate": g, "pass": ok, "value": v} for g, ok, v in gates],
        "ready_for_promotion": passed,
    }, indent=1))
    print(f"\n[out] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
