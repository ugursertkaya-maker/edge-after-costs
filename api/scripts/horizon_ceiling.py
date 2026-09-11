#!/usr/bin/env python3
"""How much does price actually move, per horizon, and what does it cost to take it?

This is a ceiling, not a strategy. It asks what a trader with PERFECT direction
calling would capture, before any question of whether a signal can find it. If
the perfect-foresight number sits near the cost of trading, no amount of signal
quality rescues that horizon — and conversely, a horizon where the move dwarfs
the cost is a place where the problem is direction, not economics.

    BACKTEST_CACHE_DIR=/tmp/bt-cache python scripts/horizon_ceiling.py

The arithmetic that turns this table into a required hit rate: a strategy with
hit rate p and a symmetric payoff captures ``(2p - 1) * E|move| - costs``, so
breaking even needs ``p > 0.5 + costs / (2 * E|move|)``. That one line is why
the horizon matters more here than the signal does.
"""
import json, glob, math, statistics, sys

COST_TAKER_BPS = 3.0     # 1 fee + ~1 spread crossing + ~1 slippage, measured
COST_MAKER_BPS = 0.0     # 0% maker, and the spread is earned rather than paid

def load(sym):
    bars = []
    for line in open(f"/tmp/bt-cache/{sym}-1m.ndjson"):
        try: b = json.loads(line)
        except Exception: continue
        bars.append((b["close_ms"], b["close"], b.get("trades", 0)))
    bars.sort()
    return bars

print(f"{'sym':<8}{'horizon':>9}{'median |mv|':>13}{'p75':>8}{'p90':>8}"
      f"{'>3bps':>8}{'>6bps':>8}{'>17bps':>9}")
print("-" * 70)
for sym in ("BTCUSD", "ETHUSD", "SOLUSD"):
    bars = load(sym)
    closes = [c for _, c, _ in bars]
    for h in (2, 5, 10, 15, 25, 40, 60, 90, 120):
        moves = []
        for i in range(0, len(closes) - h, h):
            a, b = closes[i], closes[i + h]
            if a > 0:
                moves.append(abs(b - a) / a * 1e4)
        if len(moves) < 100:
            continue
        moves.sort()
        med = statistics.median(moves)
        p75 = moves[int(len(moves) * .75)]
        p90 = moves[int(len(moves) * .90)]
        f3 = sum(1 for m in moves if m > 3) / len(moves)
        f6 = sum(1 for m in moves if m > 6) / len(moves)
        f17 = sum(1 for m in moves if m > 17) / len(moves)
        print(f"{sym:<8}{h:>7}m{med:>13.1f}{p75:>8.1f}{p90:>8.1f}"
              f"{f3:>8.0%}{f6:>8.0%}{f17:>9.0%}")
    print()
