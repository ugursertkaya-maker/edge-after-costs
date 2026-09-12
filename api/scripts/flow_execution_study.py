#!/usr/bin/env python3
"""TAKER vs MAKER vs HYBRID on FLOW_IMPULSE's own signals, at its own timescale.

The Phase 6 brief, §18: run the same flow_impulse signals through all three
execution styles, and test maker wait windows appropriate to a signal whose
median holding period is 32 seconds -- 50ms to 2s, not the 5-60s grid the
general study uses. Waiting 20 seconds for a maker fill on a 30-second alpha
does not measure maker execution; it measures the alpha decaying.

The signals come from the live PAPER ledger, so this compares execution styles
on trades the system actually decided to take, with entry timestamps it actually
produced. The exit is held fixed across all three arms, because letting it float
would mix two questions whose answers can have opposite signs.

    python scripts/flow_execution_study.py \
        --tape-dir /tmp/tape-local/tape --trades /tmp/phase5/live_trades_full.json \
        --strategy flow_impulse
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser(prog="flow_execution_study")
    ap.add_argument("--tape-dir", default="/tmp/tape-local/tape")
    ap.add_argument("--trades", default="/tmp/phase5/live_trades_full.json")
    ap.add_argument("--strategy", default="flow_impulse")
    ap.add_argument("--waits-ms", default="50,100,250,500,1000,2000",
                    help="maker quote lifetimes, tuned to a fast signal")
    ap.add_argument("--offset-ticks", default="0,1")
    ap.add_argument("--out", default="/tmp/phase6/flow_execution.json")
    args = ap.parse_args()

    from app.config import get_settings
    from app.research.maker_replay import (
        MakerReplay, QuoteSignal, load_tape, tape_coverage,
    )
    from app.types import Side

    rows = [r for r in json.loads(Path(args.trades).read_text())
            if r["strategy"] == args.strategy]
    if not rows:
        print(f"no {args.strategy} trades in {args.trades}", file=sys.stderr)
        return 2
    symbols = sorted({r["symbol"] for r in rows})
    print(f"[signals] {len(rows)} {args.strategy} trades across {symbols}", flush=True)

    signals = [
        QuoteSignal(
            symbol=r["symbol"],
            side=Side.LONG if str(r["side"]).upper() == "LONG" else Side.SHORT,
            ts_ms=int(r["entry_ms"]), qty=float(r["qty"]),
            exit_ms=int(r["exit_ms"]), exit_price=float(r["exit_price"]),
            taker_entry_price=float(r["entry_price"]),
            # HYBRID re-qualifies at the timeout against the edge it expected
            # when it decided. Omitting this is not a neutral default: a zero
            # expected edge can never clear min_net_edge_bps, so hybrid would
            # report a 0% cross rate that is an artefact of the export.
            expected_net_edge_bps=float(r.get("expected_net_edge_bps") or 0.0),
            regime=str(r.get("regime") or "unknown"),
            strategy=r["strategy"],
        )
        for r in rows if float(r["qty"]) > 0 and float(r["entry_price"]) > 0
    ]
    signals.sort(key=lambda s: s.ts_ms)

    print(f"[tape] loading {args.tape_dir}", flush=True)
    tape = load_tape(Path(args.tape_dir), symbols=symbols)
    cov = tape_coverage(tape)
    print(f"[tape] {cov}", flush=True)

    class _Arm:
        """Duck-typed arm: the replay only reads ``.trades``."""
        trades: list = []

    settings = get_settings()
    replay = MakerReplay(settings=settings)

    waits = [int(x) for x in args.waits_ms.split(",") if x.strip()]
    offsets = [int(x) for x in args.offset_ticks.split(",") if x.strip()]

    print(f"\n{'wait':>7}{'off':>5}{'fill%':>8}{'cross%':>8}"
          f"{'TAKER net':>12}{'MAKER net':>12}{'MAKER-opp':>11}{'HYBRID net':>12}"
          f"{'markout':>9}")
    print("-" * 84)

    out = []
    for wait in waits:
        for off in offsets:
            try:
                cmp_ = replay.compare(
                    _Arm(), tape=tape, signals=signals,
                    offset_ticks=off, timeout_ms=wait,
                )
            except Exception as exc:      # a wait window with no tape coverage
                print(f"{wait:>7}{off:>5}   failed: {exc!r}")
                continue
            mk = cmp_.markout_bps.get("1000ms", cmp_.markout_bps.get(
                next(iter(cmp_.markout_bps), ""), 0.0)) if cmp_.markout_bps else 0.0
            print(f"{wait:>7}{off:>5}{cmp_.maker_fill_rate * 100:>7.1f}%"
                  f"{cmp_.hybrid_cross_rate * 100:>7.1f}%"
                  f"{cmp_.taker.net_pnl:>12.4f}{cmp_.maker.net_pnl:>12.4f}"
                  f"{cmp_.maker_net_after_opportunity_cost:>11.4f}"
                  f"{cmp_.hybrid.net_pnl:>12.4f}{mk:>8.2f}b")
            out.append({
                "wait_ms": wait, "offset_ticks": off,
                "signals": cmp_.signals,
                "maker_fill_rate": cmp_.maker_fill_rate,
                "hybrid_cross_rate": cmp_.hybrid_cross_rate,
                "taker_net": cmp_.taker.net_pnl,
                "maker_net": cmp_.maker.net_pnl,
                "maker_net_after_opportunity_cost":
                    cmp_.maker_net_after_opportunity_cost,
                "maker_opportunity_cost_usd": cmp_.maker_opportunity_cost_usd,
                "hybrid_net": cmp_.hybrid.net_pnl,
                "taker_pf": cmp_.taker.profit_factor,
                "maker_pf": cmp_.maker.profit_factor,
                "hybrid_pf": cmp_.hybrid.profit_factor,
                "markout_bps": dict(cmp_.markout_bps),
                "mean_wait_ms": cmp_.mean_wait_ms,
                "miss_reasons": dict(cmp_.miss_reasons),
                "insufficient_data": cmp_.insufficient_data,
                "verdict": cmp_.verdict(),
            })

    print("\nRead the markout column before the fill rate. A high fill rate with")
    print("negative markout is worse than a low one with flat markout: it means")
    print("the 0% maker fee is being handed back through the price.")
    if out:
        best = max(out, key=lambda r: r["hybrid_net"])
        print(f"\nBest HYBRID: wait {best['wait_ms']}ms offset {best['offset_ticks']} "
              f"-> net ${best['hybrid_net']:+.4f} vs taker ${best['taker_net']:+.4f}")
        print(f"  {best['verdict']}")

    p = Path(args.out); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=1, default=str))
    print(f"\n[out] {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
