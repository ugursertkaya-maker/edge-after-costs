#!/usr/bin/env python3
"""Was the live sample's raw edge alpha, or was it just being long in a rally?

The P&L audit found the live sample raw-positive (raw PF 4.251) and the 120-day
backtest raw-negative (raw PF 0.791), and treated the disagreement as a sample
conflict. It is not. Every one of the 139 live trades was LONG, and the 12.66
hours they span were a one-directional rally -- ETH +484 bps with a 40 bps
maximum adverse excursion. A long-only scalper in that tape shows a high win
rate whether or not it has any edge.

This separates the two. For every trade it computes the raw return in bps and
the return the *market* delivered over the identical holding interval on the
identical symbol, then reports the difference:

    ALPHA_BPS = RAW_BPS - MARKET_BPS

Buy-and-hold over the same window is the benchmark a long-only strategy has to
beat, not zero. If ALPHA is not reliably positive, the strategy is expressing
market direction it did not predict, and the raw profit factor is measuring the
tape rather than the signal.

    BACKTEST_CACHE_DIR=/tmp/bt-cache python scripts/beta_check.py \
        --trades /tmp/phase5/live_trades_full.json
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.backtest.data import load_cached  # noqa: E402


class Series:
    """Minute closes for one symbol, queryable at an arbitrary millisecond."""

    def __init__(self, symbol: str) -> None:
        bars = load_cached(symbol, "1m")
        self.ts = [b.open_ms for b in bars]
        self.close = [b.close for b in bars]
        self.open = [b.open for b in bars]

    def price_at(self, ms: int) -> float | None:
        """The price in force at ``ms``: the close of the minute containing it.

        Returns None outside the cached span rather than clamping, because a
        clamped price would silently invent a benchmark return of zero.
        """
        if not self.ts or ms < self.ts[0] or ms > self.ts[-1] + 60_000:
            return None
        i = bisect.bisect_right(self.ts, ms) - 1
        if i < 0:
            return None
        return self.close[i]


def mean_ci(xs: list[float]) -> tuple[float, float, float]:
    """Mean and a 95% normal-approximation confidence interval."""
    n = len(xs)
    if n < 2:
        return (xs[0] if xs else 0.0), float("nan"), float("nan")
    m = statistics.fmean(xs)
    se = statistics.stdev(xs) / math.sqrt(n)
    return m, m - 1.96 * se, m + 1.96 * se


def main() -> int:
    ap = argparse.ArgumentParser(prog="beta_check")
    ap.add_argument("--trades", required=True, type=Path)
    ap.add_argument("--out", default="/tmp/phase5/beta_check.json")
    args = ap.parse_args()

    rows = json.loads(args.trades.read_text())
    series: dict[str, Series] = {}
    per_strategy: dict[str, list[dict]] = defaultdict(list)
    skipped = 0

    for r in rows:
        sym = r["symbol"]
        s = series.setdefault(sym, Series(sym))
        p0 = s.price_at(int(r["entry_ms"]))
        p1 = s.price_at(int(r["exit_ms"]))
        if not p0 or not p1:
            skipped += 1
            continue
        notional = float(r["notional_usd"]) or 1.0
        raw_usd = float(r["gross_pnl_usd"]) + abs(float(r.get("slippage_usd") or 0.0))
        raw_bps = raw_usd / notional * 1e4
        market_bps = (p1 - p0) / p0 * 1e4
        if str(r.get("side", "LONG")).upper().endswith("SHORT"):
            market_bps = -market_bps
        per_strategy[r["strategy"]].append({
            "raw_bps": raw_bps,
            "market_bps": market_bps,
            "alpha_bps": raw_bps - market_bps,
            "hold_s": (int(r["exit_ms"]) - int(r["entry_ms"])) / 1000.0,
        })

    everything = [d for v in per_strategy.values() for d in v]
    if not everything:
        print("no trades could be priced against the cache", file=sys.stderr)
        return 2

    print(f"\n{'=' * 84}")
    print("IS THE RAW EDGE ALPHA, OR IS IT BEING LONG IN A RALLY?")
    print("=" * 84)
    if skipped:
        print(f"({skipped} trades outside the cached price span, excluded)")
    print(f"\n{'strategy':<17}{'n':>5}{'RAW bps':>10}{'MARKET bps':>12}"
          f"{'ALPHA bps':>11}{'95% CI':>22}{'hold s':>9}")
    print("-" * 84)

    out: dict[str, dict] = {}
    for name in sorted(per_strategy, key=lambda k: -len(per_strategy[k])):
        ds = per_strategy[name]
        raw = statistics.fmean(d["raw_bps"] for d in ds)
        mkt = statistics.fmean(d["market_bps"] for d in ds)
        a, lo, hi = mean_ci([d["alpha_bps"] for d in ds])
        hold = statistics.fmean(d["hold_s"] for d in ds)
        print(f"{name:<17}{len(ds):>5}{raw:>10.2f}{mkt:>12.2f}{a:>11.2f}"
              f"{f'[{lo:+.2f}, {hi:+.2f}]':>22}{hold:>9.0f}")
        out[name] = {"n": len(ds), "raw_bps": raw, "market_bps": mkt,
                     "alpha_bps": a, "ci95": [lo, hi], "hold_seconds": hold}

    raw = statistics.fmean(d["raw_bps"] for d in everything)
    mkt = statistics.fmean(d["market_bps"] for d in everything)
    a, lo, hi = mean_ci([d["alpha_bps"] for d in everything])
    hold = statistics.fmean(d["hold_s"] for d in everything)
    print("-" * 84)
    print(f"{'TOTAL':<17}{len(everything):>5}{raw:>10.2f}{mkt:>12.2f}{a:>11.2f}"
          f"{f'[{lo:+.2f}, {hi:+.2f}]':>22}{hold:>9.0f}")
    out["TOTAL"] = {"n": len(everything), "raw_bps": raw, "market_bps": mkt,
                    "alpha_bps": a, "ci95": [lo, hi], "hold_seconds": hold}

    share = mkt / raw * 100.0 if raw else float("nan")
    print(f"\nThe market delivered {share:.0f}% of the raw return over the same "
          f"holding intervals.")
    if lo > 0:
        print("ALPHA is positive with the confidence interval clear of zero: the "
              "strategy beat\nsimply holding the same exposure for the same time.")
    elif hi < 0:
        print("ALPHA is NEGATIVE with the interval clear of zero: holding the same "
              "exposure for\nthe same time would have done better than trading it.")
    else:
        print("ALPHA is indistinguishable from zero. The raw profit factor is "
              "measuring the\ntape, not the signal -- a long-only book in a rising "
              "market. No edge is demonstrated.")

    p = Path(args.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=1))
    print(f"\n[out] {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
