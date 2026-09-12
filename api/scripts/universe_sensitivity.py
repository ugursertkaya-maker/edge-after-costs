#!/usr/bin/env python3
"""How many tradable pairs does Binance.US actually have, at what cost?

The funnel says 94.5% of the venue is unavailable for reasons we do not
control -- halted, or trading nothing. That leaves the liquidity bar and the
Tier-0 restriction as the only two knobs we own. This measures what each of
them is worth, in pairs AND in round-trip cost, because a universe that
doubles by admitting 60 bps pairs has not improved.

The number that matters per pair is the EDGE RATIO: the pair's own expected
move over the horizon divided by what an in-and-out costs. Below 1.0 the pair
cannot pay for its own execution and adding it is negative work.

    python scripts/universe_sensitivity.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def fetch(path: str):
    with urllib.request.urlopen(f"https://api.binance.us{path}", timeout=45) as r:
        return json.load(r)


def main() -> int:
    ap = argparse.ArgumentParser(prog="universe_sensitivity")
    ap.add_argument("--out", default="/tmp/phase6/sensitivity.json")
    args = ap.parse_args()

    from app.config import reload_settings
    from app.types import SymbolInfo
    from app.universe import TierPolicy, UniverseSelector

    info_raw = fetch("/api/v3/exchangeInfo")
    tickers = fetch("/api/v3/ticker/24hr")
    infos = {}
    for s in info_raw["symbols"]:
        f = {x["filterType"]: x for x in s.get("filters", [])}
        lot, price = f.get("LOT_SIZE", {}), f.get("PRICE_FILTER", {})
        notional = f.get("NOTIONAL", f.get("MIN_NOTIONAL", {}))
        infos[s["symbol"]] = SymbolInfo(
            symbol=s["symbol"], base_asset=s["baseAsset"], quote_asset=s["quoteAsset"],
            status=s["status"], step_size=float(lot.get("stepSize", 0) or 0),
            tick_size=float(price.get("tickSize", 0) or 0),
            min_qty=float(lot.get("minQty", 0) or 0),
            min_notional=float(notional.get("minNotional", notional.get("notional", 0)) or 0),
        )

    grid = [(vol, trades, restrict)
            for vol, trades in ((100_000, 200), (50_000, 100), (10_000, 20), (1_000, 5))
            for restrict in (True, False)]

    rows = []
    print(f"{'min $vol':>10}{'min trd':>9}{'tier0':>7}{'pairs':>7}"
          f"{'tier0 n':>9}{'median trip':>13}{'ratio>=1':>10}{'ratio>=2':>10}")
    print("-" * 76)
    for vol, trades, restrict in grid:
        os.environ.update({
            "FEE_MODEL": "tier0",
            "UNIVERSE_RESTRICT_TIER0": "true" if restrict else "false",
            "UNIVERSE_MIN_QUOTE_VOLUME_24H": str(vol),
            "UNIVERSE_MIN_TRADES_24H": str(trades),
            "UNIVERSE_MAX_SYMBOLS": "500",
        })
        settings = reload_settings()
        sel = UniverseSelector(settings=settings)
        relevant = [t for t in tickers if t["symbol"] in infos
                    and infos[t["symbol"]].quote_asset in settings.exchange.quote_assets]
        chosen = sel.select_tiers(relevant, infos, policy=TierPolicy())
        if not chosen:
            continue
        trips = sorted(e.round_trip_bps for e in chosen)
        med = trips[len(trips) // 2]
        n0 = sum(1 for e in chosen if e.fee_tier0)
        r1 = sum(1 for e in chosen if e.edge_ratio >= 1.0)
        r2 = sum(1 for e in chosen if e.edge_ratio >= 2.0)
        print(f"{vol:>10,}{trades:>9}{str(restrict):>7}{len(chosen):>7}"
              f"{n0:>9}{med:>12.1f}b{r1:>10}{r2:>10}")
        rows.append({
            "min_quote_volume": vol, "min_trades": trades, "restrict_tier0": restrict,
            "pairs": len(chosen), "tier0_pairs": n0, "median_round_trip_bps": med,
            "edge_ratio_ge_1": r1, "edge_ratio_ge_2": r2,
            "symbols": [
                {"symbol": e.symbol, "tier": e.tier, "trip_bps": round(e.round_trip_bps, 2),
                 "move_bps": round(e.expected_move_bps, 1), "ratio": round(e.edge_ratio, 2),
                 "tier0": e.fee_tier0, "vol": round(e.quote_volume_24h)}
                for e in chosen
            ],
        })

    print("\nEdge ratio = the pair's expected move over the horizon divided by")
    print("its round trip. Below 1.0 the pair cannot pay for its own execution.")

    best = max(rows, key=lambda r: r["edge_ratio_ge_1"])
    print(f"\nMost pairs that can pay for themselves: "
          f"min ${best['min_quote_volume']:,} / {best['min_trades']} trades / "
          f"tier0={best['restrict_tier0']} -> {best['edge_ratio_ge_1']} pairs")
    for s in sorted(best["symbols"], key=lambda s: -s["ratio"])[:20]:
        flag = "T0" if s["tier0"] else "  "
        print(f"    {s['symbol']:<12}{s['tier']:<6}{flag}  trip {s['trip_bps']:>6.1f}b  "
              f"move {s['move_bps']:>6.1f}b  ratio {s['ratio']:>5.2f}  "
              f"${s['vol']:>12,}")

    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=1))
    print(f"\n[out] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
