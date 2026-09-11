#!/usr/bin/env python3
"""Promote a tournament finalist to the live paper champion.

    python scripts/promote_champion.py --finalists /tmp/finalists.json --key <candidate key>
    python scripts/promote_champion.py --finalists /tmp/finalists.json --best
    python scripts/promote_champion.py --show
    python scripts/promote_champion.py --clear

This is deliberately a separate, explicit step. A tournament produces gate
reports; it does not deploy anything. Promotion bumps the simulator version so
live P&L before and after are never mixed, and records the out-of-sample
evidence that earned it.

It cannot promote a strategy that failed its gates, and it cannot enable live
trading — there is no code path in this repository that does.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.research.champion import Champion, ChampionStore  # noqa: E402


def load_finalists(path: Path) -> list[dict]:
    raw = json.loads(path.read_text())
    if isinstance(raw, dict):
        raw = raw.get("finalists", [])
    if not isinstance(raw, list):
        raise SystemExit(f"{path} does not contain a finalists list")
    return raw


def to_champion(entry: dict) -> Champion:
    oos = entry.get("out_of_sample") or {}
    return Champion(
        key=str(entry.get("key", "")),
        family=str(entry.get("family", "")),
        params=dict(entry.get("params") or {}),
        symbol=entry.get("symbol"),
        regime=entry.get("regime"),
        horizon_seconds=int(entry.get("horizon_seconds", 1_800)),
        exit_profile=str(entry.get("exit_profile", "net_pct")),
        stop_profile=str(entry.get("stop_profile", "atr")),
        min_score=float(entry.get("min_score", 0.0) or 0.0),
        source_run=str(entry.get("source_run", "")),
        oos_trades=int(oos.get("trades", entry.get("trades", 0)) or 0),
        oos_profit_factor=float(oos.get("profit_factor", entry.get("profit_factor", 0.0)) or 0.0),
        oos_expectancy=float(oos.get("expectancy", entry.get("expectancy", 0.0)) or 0.0),
        oos_net_pnl=float(oos.get("net_pnl", entry.get("net_pnl", 0.0)) or 0.0),
        oos_max_drawdown=float(oos.get("max_drawdown", entry.get("max_drawdown", 0.0)) or 0.0),
        gate_reasons=tuple(entry.get("reasons") or ()),
    )


def main() -> int:
    ap = argparse.ArgumentParser(prog="promote_champion")
    ap.add_argument("--finalists", type=Path, help="finalists.json from the tournament")
    ap.add_argument("--key", help="candidate key to promote")
    ap.add_argument("--best", action="store_true", help="promote the highest OOS expectancy")
    ap.add_argument("--dir", type=Path, default=None, help="research directory")
    ap.add_argument("--show", action="store_true", help="print the current champion")
    ap.add_argument("--clear", action="store_true", help="remove the current champion")
    ap.add_argument("--force", action="store_true",
                    help="promote an entry whose gates did not pass (requires --key)")
    args = ap.parse_args()

    settings = get_settings()
    directory = args.dir or Path(settings.research.directory)
    store = ChampionStore(directory)

    if args.show:
        current = store.load()
        if current is None:
            print("no champion — the live engine is running the default configuration")
            return 0
        print(json.dumps(current.as_dict(), indent=2, sort_keys=True))
        return 0

    if args.clear:
        store.clear()
        print("champion cleared")
        return 0

    if not args.finalists:
        ap.error("--finalists is required unless --show or --clear")

    entries = load_finalists(args.finalists)
    if not entries:
        print(
            "no finalists in that file.\n"
            "That is a result, not an error: nothing passed the robustness gates,\n"
            "so there is nothing to promote and the engine keeps its current\n"
            "configuration."
        )
        return 1

    passing = [e for e in entries if e.get("passed", True)]
    if args.force and args.key:
        pool = entries
    else:
        pool = passing
    if not pool:
        print("every entry in that file failed its gates — refusing to promote.")
        return 1

    if args.key:
        chosen = next((e for e in pool if e.get("key") == args.key), None)
        if chosen is None:
            print(f"no entry with key {args.key!r} (or it failed its gates; use --force)")
            return 1
    elif args.best:
        chosen = max(
            pool,
            key=lambda e: float(
                (e.get("out_of_sample") or {}).get("expectancy", e.get("expectancy", 0.0)) or 0.0
            ),
        )
    else:
        ap.error("pass --key or --best")

    champion = to_champion(chosen)
    stored = store.promote(champion, current_version=settings.simulator_version)

    print(f"promoted: {stored.describe()}")
    print(f"version:  {stored.simulator_version}")
    if stored.gate_reasons:
        print("gates:")
        for reason in stored.gate_reasons:
            print(f"  - {reason}")
    print(
        "\nRestart the worker to run it:\n"
        "  ssh user@your-server 'cd /srv/netedge && "
        "sudo -n docker compose up -d --no-deps worker'\n"
        "Live P&L from here on is recorded under the new version and is not\n"
        "mixed with the previous one. PAPER only — real orders remain 0."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
