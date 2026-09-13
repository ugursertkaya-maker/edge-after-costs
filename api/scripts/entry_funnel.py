#!/usr/bin/env python3
"""Which entry condition is suppressing setups, and by how much?

The live screen kept reading `momentum_scalp: NO_SETUP`, which is a black box:
it says a strategy declined without saying which of its six conditions bound
or how far the market was from clearing it. This replays history through the
REAL FeatureEngine and the REAL strategies, records the FIRST binding
condition for every evaluation, and reports the observed distribution of the
value that did the refusing against the threshold it failed.

A condition whose p90 sits below its threshold is over-restrictive. A
condition whose p50 clears it is not the problem. That distinction is the
whole point, and it cannot be made from a pass rate alone.

    BACKTEST_CACHE_DIR=/tmp/bt-cache python scripts/entry_funnel.py \
        --symbols BTCUSD,ETHUSD,SOLUSD --days 30
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MS_PER_DAY = 86_400_000


def main() -> int:
    ap = argparse.ArgumentParser(prog="entry_funnel")
    ap.add_argument("--symbols", default="BTCUSD,ETHUSD,SOLUSD")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out", default="/tmp/phase8/funnel.json")
    args = ap.parse_args()

    os.environ.setdefault("FEE_MODEL", "tier0")
    from app.config import get_settings
    from app.backtest.data import load_cached
    from app.backtest.engine import BacktestConfig, synth_book, synth_trades
    from app.features import FeatureEngine
    from app.regime import RegimeClassifier
    from app.strategies import get_strategies
    from app.types import MarketSnapshot, SymbolInfo
    from random import Random

    s = get_settings()
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.days * MS_PER_DAY
    symbols = [x.strip() for x in args.symbols.split(",") if x.strip()]

    # Per strategy: evaluations, per-condition counts, and the values observed.
    counts: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    values: dict[tuple[str, str], list[float]] = collections.defaultdict(list)
    thresholds: dict[tuple[str, str], float] = {}
    signals: collections.Counter = collections.Counter()
    evaluated: collections.Counter = collections.Counter()

    rng = Random(7)
    cfg = BacktestConfig()
    for sym in symbols:
        bars = [b for b in load_cached(sym, "1m") if start_ms <= b.open_ms <= end_ms]
        if not bars:
            print(f"[skip] {sym}: no bars", flush=True)
            continue
        print(f"[run] {sym}: {len(bars):,} bars", flush=True)
        info = SymbolInfo(symbol=sym, base_asset=sym[:-3], quote_asset=sym[-3:],
                          status="TRADING", step_size=1e-6, tick_size=0.01,
                          min_qty=1e-6, min_notional=1.0)
        fe = FeatureEngine(settings=s)
        rc = RegimeClassifier(settings=s)
        strategies = get_strategies(settings=s)
        history: list = []
        for bar in bars:
            history.append(bar)
            if len(history) < 40:
                continue
            book = synth_book(bar, price=bar.close, ts_ms=bar.close_ms)
            snap = MarketSnapshot(
                symbol=sym, ts_ms=bar.close_ms, book=book,
                bars_1m=history[-240:], recent_trades=synth_trades(bar, rng),
                info=info,
                quote_volume_24h=bar.quote_volume * 1440,
                trades_24h=max(1, bar.trades * 1440),
            )
            try:
                f = fe.compute(snap)
                regime = rc.classify(f)
            except Exception:
                continue
            for st in strategies:
                st.last_decline = None
                evaluated[st.name] += 1
                sig = st.evaluate(f, regime)
                if sig is not None:
                    signals[st.name] += 1
                    counts[st.name]["SIGNAL"] += 1
                    continue
                d = st.last_decline
                cond = d[0] if d else "unknown"
                counts[st.name][cond] += 1
                if d:
                    values[(st.name, cond)].append(d[1])
                    thresholds[(st.name, cond)] = d[2]

    out = {}
    print(f"\n{'='*84}")
    print("GİRİŞ HUNİSİ — İLK BAĞLAYICI KOŞUL")
    print("="*84)
    for name in sorted(evaluated, key=lambda k: -evaluated[k]):
        ev = evaluated[name]
        sg = signals[name]
        print(f"\n{name}   değerlendirme {ev:,}   sinyal {sg:,} "
              f"({sg/ev*100:.3f}%)")
        print(f"  {'koşul':<20}{'kez':>9}{'pay':>8}{'eşik':>10}"
              f"{'medyan':>10}{'p75':>10}{'p90':>10}{'p95':>10}{'geçme':>8}")
        print("  " + "-"*93)
        rows = []
        for cond, n in counts[name].most_common():
            if cond == "SIGNAL":
                continue
            vs = sorted(values.get((name, cond), []))
            thr = thresholds.get((name, cond))
            def q(p):
                return vs[min(len(vs)-1, int(len(vs)*p))] if vs else float("nan")
            # Pass rate for this condition = how often it was NOT the binder,
            # among evaluations that reached it. Approximated by 1 - share.
            share = n / ev
            print(f"  {cond:<20}{n:>9,}{share*100:>7.1f}%"
                  f"{(thr if thr is not None else float('nan')):>10.3f}"
                  f"{(statistics.median(vs) if vs else float('nan')):>10.3f}"
                  f"{q(0.75):>10.3f}{q(0.90):>10.3f}{q(0.95):>10.3f}"
                  f"{(1-share)*100:>7.1f}%")
            rows.append({
                "condition": cond, "count": n, "share": share,
                "threshold": thr,
                "median": statistics.median(vs) if vs else None,
                "p75": q(0.75) if vs else None, "p90": q(0.90) if vs else None,
                "p95": q(0.95) if vs else None,
            })
        out[name] = {"evaluations": ev, "signals": sg, "conditions": rows}

    print("\n" + "="*84)
    print("AŞIRI KISITLAYICI KOŞULLAR (p90 gözlem eşiği geçemiyor)")
    print("="*84)
    flagged = []
    for name, d in out.items():
        for r in d["conditions"]:
            thr, p90 = r["threshold"], r["p90"]
            if thr is None or p90 is None:
                continue
            # Only meaningful for "value must exceed threshold" conditions.
            if p90 < thr and r["share"] > 0.05:
                flagged.append((name, r))
                print(f"  {name}.{r['condition']:<18} eşik {thr:>8.3f}  "
                      f"p90 {p90:>8.3f}  adayların %{r['share']*100:.1f}'ini kesiyor")
    if not flagged:
        print("  (yok)")

    p = Path(args.out); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=1, default=str))
    print(f"\n[out] {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
