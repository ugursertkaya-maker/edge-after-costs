#!/usr/bin/env python3
"""Audit the P&L decomposition, trade by trade, and answer one question.

A reader challenged the Phase 5 numbers:

    gross -$34.41, fees $8.00, slippage $25.03, net -$42.40
    -34.41 - 8.00 - 25.03 = -67.44, not -42.40

The challenge is right to demand the audit and the arithmetic does reconcile,
but only because ``gross`` in this codebase is **not** P&L before execution
costs. ``Ledger.close`` computes::

    gross   = (exit_fill_price - entry_fill_price) * qty
    net     = gross - entry_fee - exit_fee

and the fill prices already contain the spread and slippage, because
``PaperSimulator`` walks the book. ``slippage_usd`` is then computed separately,
against the decision-time mid, as a **memo**. So -34.41 - 8.00 = -42.41, and
slippage is inside the first term rather than a third deduction. Reporting that
as "gross" without saying so was a presentation defect; this script replaces it
with an unambiguous decomposition.

The decomposition used here, with no term counted twice:

    RAW_MARKET_PNL   P&L at the decision-time mid, i.e. with perfect execution
                     = gross + spread_and_slippage
    SPREAD+SLIPPAGE  everything paid away from mid on both legs (the simulator
                     measures them together against the mid, so they are not
                     separable after the fact -- stated rather than invented)
    FEES             entry_fee + exit_fee
    NET_PNL          = RAW - (SPREAD+SLIPPAGE) - FEES   [identically gross - fees]

And the question that decides the next architecture step:

    Is the signal positive BEFORE execution costs?
      RAW expectancy > 0 and RAW PF > 1  ->  execution is the problem (CASE A)
      otherwise                          ->  the signal has no alpha (CASE B)

    BACKTEST_CACHE_DIR=/tmp/bt-cache python scripts/pnl_audit.py --source live
    BACKTEST_CACHE_DIR=/tmp/bt-cache python scripts/pnl_audit.py --source backtest
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TOL = 1e-6


@dataclass
class Decomposition:
    """One bucket of trades, decomposed so every term is stated once."""

    label: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    raw_market_pnl: float = 0.0
    spread_and_slippage: float = 0.0
    entry_fees: float = 0.0
    exit_fees: float = 0.0
    net_pnl: float = 0.0
    raw_gross_win: float = 0.0
    raw_gross_loss: float = 0.0
    net_gross_win: float = 0.0
    net_gross_loss: float = 0.0
    violations: list[str] = field(default_factory=list)

    @property
    def fees(self) -> float:
        return self.entry_fees + self.exit_fees

    @property
    def total_execution_cost(self) -> float:
        return self.spread_and_slippage + self.fees

    @property
    def raw_expectancy(self) -> float:
        return self.raw_market_pnl / self.trades if self.trades else 0.0

    @property
    def net_expectancy(self) -> float:
        return self.net_pnl / self.trades if self.trades else 0.0

    @property
    def raw_profit_factor(self) -> float:
        return (self.raw_gross_win / self.raw_gross_loss
                if self.raw_gross_loss > 0 else float("inf"))

    @property
    def net_profit_factor(self) -> float:
        return (self.net_gross_win / self.net_gross_loss
                if self.net_gross_loss > 0 else float("inf"))

    @property
    def alpha_destroyed_pct(self) -> float:
        """How much of the raw edge execution consumed. Only meaningful when
        the raw edge is positive — a negative raw P&L has no alpha to destroy."""
        if self.raw_market_pnl <= 0:
            return float("nan")
        return self.total_execution_cost / self.raw_market_pnl * 100.0

    def add(self, *, raw: float, slip: float, entry_fee: float,
            exit_fee: float, net: float, tid: str) -> None:
        self.trades += 1
        self.raw_market_pnl += raw
        self.spread_and_slippage += slip
        self.entry_fees += entry_fee
        self.exit_fees += exit_fee
        self.net_pnl += net
        if net > 0:
            self.wins += 1
            self.net_gross_win += net
        elif net < 0:
            self.losses += 1
            self.net_gross_loss += -net
        if raw > 0:
            self.raw_gross_win += raw
        elif raw < 0:
            self.raw_gross_loss += -raw

        # Every trade must reconcile on its own, not just in aggregate.
        expect = raw - slip - entry_fee - exit_fee
        if abs(expect - net) > max(TOL, abs(net) * 1e-9):
            self.violations.append(
                f"{tid}: raw {raw:+.6f} - slip {slip:.6f} - fees "
                f"{entry_fee + exit_fee:.6f} = {expect:+.6f}, reported {net:+.6f}"
            )

    def reconcile(self) -> str:
        derived = (self.raw_market_pnl - self.spread_and_slippage
                   - self.entry_fees - self.exit_fees)
        ok = abs(derived - self.net_pnl) <= max(TOL, abs(self.net_pnl) * 1e-9)
        return (
            f"  RAW MARKET P&L      {self.raw_market_pnl:>12.4f}\n"
            f"  - SPREAD+SLIPPAGE   {self.spread_and_slippage:>12.4f}\n"
            f"  - ENTRY FEES        {self.entry_fees:>12.4f}\n"
            f"  - EXIT FEES         {self.exit_fees:>12.4f}\n"
            f"  {'=' * 32}\n"
            f"  = NET P&L           {derived:>12.4f}   reported "
            f"{self.net_pnl:>10.4f}   {'OK' if ok else 'MISMATCH'}\n"
        )

    def as_dict(self) -> dict:
        return {
            "label": self.label, "trades": self.trades,
            "raw_market_pnl": round(self.raw_market_pnl, 6),
            "spread_and_slippage": round(self.spread_and_slippage, 6),
            "fees": round(self.fees, 6),
            "total_execution_cost": round(self.total_execution_cost, 6),
            "net_pnl": round(self.net_pnl, 6),
            "raw_expectancy": round(self.raw_expectancy, 6),
            "net_expectancy": round(self.net_expectancy, 6),
            "raw_profit_factor": round(self.raw_profit_factor, 4),
            "net_profit_factor": round(self.net_profit_factor, 4),
            "win_rate": round(self.wins / self.trades, 4) if self.trades else 0.0,
            "per_trade_violations": len(self.violations),
        }


def decompose(rows, *, label: str, buckets_by) -> tuple[Decomposition, dict]:
    """Fold trade rows into a total and a set of buckets."""
    total = Decomposition(label)
    buckets: dict[str, Decomposition] = {}
    for r in rows:
        gross = float(r["gross_pnl_usd"])
        slip = abs(float(r.get("slippage_usd", 0.0) or 0.0))
        ef = float(r.get("entry_fee_usd", 0.0) or 0.0)
        xf = float(r.get("exit_fee_usd", 0.0) or 0.0)
        net = float(r["net_pnl_usd"])
        # `gross` is measured at the FILL prices, so the spread and slippage are
        # already inside it. Adding them back recovers the P&L the strategy
        # would have had at the decision-time mid — the honest "before
        # execution costs" number the audit asks for.
        raw = gross + slip
        tid = str(r.get("trade_id") or r.get("id") or "?")
        total.add(raw=raw, slip=slip, entry_fee=ef, exit_fee=xf, net=net, tid=tid)
        key = buckets_by(r)
        buckets.setdefault(key, Decomposition(key)).add(
            raw=raw, slip=slip, entry_fee=ef, exit_fee=xf, net=net, tid=tid
        )
    return total, buckets


def verdict(d: Decomposition) -> str:
    if d.trades == 0:
        return "NO TRADES — nothing to decide on."
    raw_ok = d.raw_expectancy > 0 and d.raw_profit_factor > 1.0
    if raw_ok:
        return (
            "CASE A — the signal IS positive before execution costs "
            f"(raw expectancy {d.raw_expectancy:+.4f}/trade, raw PF "
            f"{d.raw_profit_factor:.3f}) and execution turns it negative "
            f"(net {d.net_expectancy:+.4f}/trade, net PF "
            f"{d.net_profit_factor:.3f}). Execution cost is genuinely the "
            f"primary problem; {d.alpha_destroyed_pct:.0f}% of the raw edge is "
            "consumed by it. Maker/hybrid research is the right next step."
        )
    return (
        "CASE B — the signal is NOT positive before execution costs "
        f"(raw expectancy {d.raw_expectancy:+.4f}/trade, raw PF "
        f"{d.raw_profit_factor:.3f}). There is no alpha for execution to be "
        "destroying. Maker execution cannot rescue a signal that loses money "
        "at the mid, and the honest next step is entry-model research rather "
        "than blaming the venue."
    )


def report(total: Decomposition, buckets: dict, *, title: str) -> str:
    out = [f"\n{'=' * 78}", title, "=" * 78, ""]
    out.append(f"{'bucket':<26}{'trades':>7}{'RAW':>11}{'sprd+slip':>11}"
               f"{'fees':>9}{'NET':>11}{'rawPF':>8}{'netPF':>8}")
    out.append("-" * 78)
    for key in sorted(buckets, key=lambda k: buckets[k].net_pnl):
        b = buckets[key]
        out.append(
            f"{key[:25]:<26}{b.trades:>7}{b.raw_market_pnl:>11.4f}"
            f"{b.spread_and_slippage:>11.4f}{b.fees:>9.4f}{b.net_pnl:>11.4f}"
            f"{b.raw_profit_factor:>8.3f}{b.net_profit_factor:>8.3f}"
        )
    out.append("-" * 78)
    out.append(
        f"{'TOTAL':<26}{total.trades:>7}{total.raw_market_pnl:>11.4f}"
        f"{total.spread_and_slippage:>11.4f}{total.fees:>9.4f}"
        f"{total.net_pnl:>11.4f}{total.raw_profit_factor:>8.3f}"
        f"{total.net_profit_factor:>8.3f}"
    )
    out.append("\nRECONCILIATION")
    out.append(total.reconcile())
    out.append(f"  raw expectancy/trade  {total.raw_expectancy:>+10.6f}")
    out.append(f"  net expectancy/trade  {total.net_expectancy:>+10.6f}")
    if total.violations:
        out.append(f"\n  {len(total.violations)} PER-TRADE VIOLATIONS "
                   "(a cost counted twice, or an unexplained term):")
        for v in total.violations[:10]:
            out.append(f"    {v}")
    else:
        out.append("\n  every trade reconciles individually — no cost counted twice")
    out.append("\nVERDICT")
    out.append("  " + verdict(total))
    return "\n".join(out)


def live_rows() -> list[dict]:
    from app.db import get_session
    from app.models import TradeRow

    with get_session() as s:
        rows = s.query(TradeRow).all()
        return [
            {
                "trade_id": r.trade_id, "ledger": r.ledger, "symbol": r.symbol,
                "strategy": r.strategy, "gross_pnl_usd": r.gross_pnl_usd,
                "entry_fee_usd": r.entry_fee_usd, "exit_fee_usd": r.exit_fee_usd,
                "slippage_usd": r.slippage_usd, "net_pnl_usd": r.net_pnl_usd,
            }
            for r in rows
        ]


def main() -> int:
    ap = argparse.ArgumentParser(prog="pnl_audit")
    ap.add_argument("--source", choices=("live", "json"), default="live")
    ap.add_argument("--json", type=Path, help="a file of trade dicts")
    ap.add_argument("--out", default="/tmp/phase5/pnl_audit.json")
    args = ap.parse_args()

    if args.source == "json":
        if not args.json:
            ap.error("--json is required with --source json")
        raw = json.loads(args.json.read_text())
        rows = raw["trades"] if isinstance(raw, dict) else raw
    else:
        rows = live_rows()

    if not rows:
        print("no trades to audit")
        return 0

    print(f"auditing {len(rows)} trades")
    total, by_strategy = decompose(rows, label="ALL",
                                   buckets_by=lambda r: r.get("strategy") or "?")
    print(report(total, by_strategy, title="BY STRATEGY"))

    _, by_symbol = decompose(rows, label="ALL",
                             buckets_by=lambda r: r.get("symbol") or "?")
    print(report(total, by_symbol, title="BY SYMBOL"))

    _, by_ledger = decompose(rows, label="ALL",
                             buckets_by=lambda r: r.get("ledger") or "?")
    print(report(total, by_ledger, title="BY CAPITAL PROFILE (LEDGER)"))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "total": total.as_dict(),
        "by_strategy": {k: v.as_dict() for k, v in by_strategy.items()},
        "by_symbol": {k: v.as_dict() for k, v in by_symbol.items()},
        "by_ledger": {k: v.as_dict() for k, v in by_ledger.items()},
        "verdict": verdict(total),
        "per_trade_violations": total.violations[:50],
    }, indent=1))
    print(f"\n[out] {out}")
    return 1 if total.violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
