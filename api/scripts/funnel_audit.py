#!/usr/bin/env python3
"""Walk the whole Binance.US venue through the selection ladder, and count.

The Phase 6 brief says: do not guess, provide actual per-symbol funnel counts.
This does that against live public metadata -- exchangeInfo plus 24h tickers,
no credentials, no orders -- and reports, for every listed pair, the stage it
died at and why.

It deliberately runs the ladder TWICE:

  * as deployed   -- UNIVERSE_RESTRICT_TIER0=true, select() path, max_symbols
  * as available  -- restriction off, select_tiers() path, per-tier caps

The difference between the two columns is the answer to "why is this a
three-coin trader", and it is a configuration answer rather than a ranking one.

    python scripts/funnel_audit.py --out /tmp/phase6/funnel.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REST = "https://api.binance.us"


def fetch(path: str) -> object:
    with urllib.request.urlopen(f"{REST}{path}", timeout=45) as r:
        return json.load(r)


def main() -> int:
    ap = argparse.ArgumentParser(prog="funnel_audit")
    ap.add_argument("--out", default="/tmp/phase6/funnel.json")
    ap.add_argument("--top", type=int, default=30, help="rows to print")
    args = ap.parse_args()

    from app.config import reload_settings
    from app.research.funnel import FunnelReport
    from app.types import SymbolInfo
    from app.universe import TierPolicy, UniverseSelector

    print("[fetch] exchangeInfo + 24h tickers", flush=True)
    info_raw = fetch("/api/v3/exchangeInfo")
    tickers = fetch("/api/v3/ticker/24hr")
    by_symbol = {t["symbol"]: t for t in tickers}

    infos: dict[str, SymbolInfo] = {}
    for s in info_raw["symbols"]:
        filters = {f["filterType"]: f for f in s.get("filters", [])}
        lot = filters.get("LOT_SIZE", {})
        price = filters.get("PRICE_FILTER", {})
        notional = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {}))
        infos[s["symbol"]] = SymbolInfo(
            symbol=s["symbol"],
            base_asset=s["baseAsset"],
            quote_asset=s["quoteAsset"],
            status=s["status"],
            step_size=float(lot.get("stepSize", 0.0) or 0.0),
            tick_size=float(price.get("tickSize", 0.0) or 0.0),
            min_qty=float(lot.get("minQty", 0.0) or 0.0),
            min_notional=float(
                notional.get("minNotional", notional.get("notional", 0.0)) or 0.0
            ),
        )
    print(f"[venue] {len(infos)} listed pairs, {len(by_symbol)} tickers", flush=True)

    results: dict[str, dict] = {}
    for label, restrict in (("as_deployed", True), ("unrestricted", False)):
        os.environ["UNIVERSE_RESTRICT_TIER0"] = "true" if restrict else "false"
        os.environ["FEE_MODEL"] = "tier0"
        settings = reload_settings()
        sel = UniverseSelector(settings=settings)
        report = FunnelReport()

        relevant = [
            t for t in tickers
            if t["symbol"] in infos
            and infos[t["symbol"]].quote_asset in settings.exchange.quote_assets
        ]
        for t in relevant:
            report.for_symbol(t["symbol"]).reached("discovered")

        # select() is what the deployed worker actually calls; select_tiers() is
        # the Phase 5 path that was built, tested, and never wired in.
        if restrict:
            chosen = sel.select(relevant, infos)
        else:
            chosen = sel.select_tiers(relevant, infos, policy=TierPolicy())

        for symbol, reason in sel.rejections.items():
            if symbol in report.symbols:
                report.for_symbol(symbol).dropped("subscribed", reason)

        tier0 = settings.fees.tier0_symbols
        for t in relevant:
            sym = t["symbol"]
            f = report.for_symbol(sym)
            if restrict and sym not in tier0 and sym not in sel.rejections:
                f.dropped("subscribed", "not in the hardcoded tier0 allowlist")

        by_tier: dict[str, list[str]] = {}
        for e in chosen:
            f = report.for_symbol(e.symbol)
            for stage in ("subscribed", "data_healthy", "features",
                          "strategy_evaluated", "candidate", "cost_gate",
                          "liquidity_gate", "ranked"):
                f.reached(stage)
            # HOT is subscribed and traded from; WARM is scanned on a slower
            # cycle; COLD is a watchlist that never reaches the allocator.
            if e.tier in ("hot", "warm"):
                f.reached("selected")
            else:
                f.dropped("selected", f"tier {e.tier}: watchlist only")
            by_tier.setdefault(e.tier, []).append(e.symbol)

        results[label] = {
            "eligible": len(relevant),
            "selected": len(chosen),
            "by_tier": {k: sorted(v) for k, v in sorted(by_tier.items())},
            "report": report.as_dict(),
            "entries": [
                {
                    "symbol": e.symbol, "tier": e.tier,
                    "quote_volume_24h": round(e.quote_volume_24h),
                    "trades_24h": e.trades_24h,
                    "spread_bps": round(e.spread_bps, 2),
                    "round_trip_bps": round(e.round_trip_bps, 2),
                    "expected_move_bps": round(e.expected_move_bps, 1),
                    "edge_ratio": round(e.edge_ratio, 3),
                    "score": round(e.score, 4),
                    "net_score": round(e.net_score, 4),
                    "fee_tier0": e.fee_tier0,
                }
                for e in chosen
            ],
        }

        print(f"\n=== {label}: {len(chosen)} of {len(relevant)} eligible pairs selected")
        for tier, syms in sorted(results[label]["by_tier"].items()):
            print(f"    {tier:<5} {len(syms):>3}  {', '.join(syms[:12])}"
                  + (" ..." if len(syms) > 12 else ""))
        wall = results[label]["report"]["wall"]
        print(f"    wall: {wall}")

    # The venue-level loss, stage by stage, for the deployed configuration.
    dep = results["as_deployed"]["report"]
    rows = sorted(
        dep["symbols"].values(),
        key=lambda r: (-r["stages"]["ranked"], -r["stages"]["discovered"], r["symbol"]),
    )
    print(f"\n{'symbol':<12}{'deepest':<20}{'bottleneck':<18}reason")
    print("-" * 96)
    for r in rows[: args.top]:
        print(f"{r['symbol']:<12}{r['deepest_stage']:<20}"
              f"{r['bottleneck_stage']:<18}{r['bottleneck_reason'][:44]}")

    # Bucket the reasons. Raw strings carry the measured number ("volume 19
    # too low"), which is right for one symbol and useless for a venue-level
    # tally -- 240 distinct numbers hide one cause.
    def bucket(reason: str) -> str:
        if reason.startswith("status "):
            return f"venue halted the pair ({reason.split()[-1]})"
        if "quote volume" in reason:
            return "24h quote volume below the liquidity bar"
        if "trade count" in reason:
            return "24h trade count below the liquidity bar"
        if "volatility" in reason:
            return "volatility outside the band"
        if "spread" in reason:
            return "spread too wide"
        if "depth" in reason:
            return "book too thin"
        if "tier0 allowlist" in reason:
            return "not in the hardcoded Tier-0 allowlist"
        if "edge ratio" in reason:
            return "edge ratio below the tier floor"
        return reason

    for label in ("as_deployed", "unrestricted"):
        counts: dict[str, int] = {}
        for r in results[label]["report"]["symbols"].values():
            counts[bucket(r["bottleneck_reason"])] = counts.get(
                bucket(r["bottleneck_reason"]), 0) + 1
        total = sum(counts.values())
        print(f"\nWHY EVERY PAIR DIES -- {label} ({total} eligible pairs):")
        for reason, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {n:>4}  {n / total * 100:>5.1f}%  {reason}")
        results[label]["reason_buckets"] = counts

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=1))
    print(f"\n[out] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
