"""The paper book: cash, open positions, closed trades, statistics.

The single most important behaviour in this file is **capital recycling**
(§19).  A $500 account that locks capital until end of day gets three trades a
day; one that returns the proceeds the instant a position closes gets thirty.
So :meth:`Ledger.close` credits ``exit notional - exit fees`` back to ``cash``
immediately and synchronously — before any bookkeeping, before any statistics —
and the very next :meth:`can_afford` sees it.  There is no settlement delay, no
"pending" bucket, and a test exists purely to keep it that way.

Accounting conventions:

* ``cash`` is unencumbered USD.  Opening debits notional **plus** the entry fee;
  closing credits proceeds **net** of the exit fee.
* ``equity`` is ``cash + marked value of open positions`` — the identity that
  makes the equity curve continuous across an open and a close.
* ``PerfStats.max_drawdown`` is in **USD** (peak-to-trough on the equity
  points), matching every other money field on that dataclass.
* ``PerfStats.avg_loss`` is a positive magnitude; ``expectancy`` is mean net
  P&L per trade, which is identically ``win_rate*avg_win - loss_rate*avg_loss``.
"""

from __future__ import annotations

from app.types import (
    ClosedTrade,
    ExitReason,
    OrderResult,
    PerfStats,
    Position,
    Side,
)

_BPS = 10_000.0
_MS_PER_DAY = 86_400_000.0


class Ledger:
    """One paper account.  Several may run side by side ($500/$1k/$5k)."""

    def __init__(self, name: str, starting_equity: float, simulator_version: str) -> None:
        self.name = name
        self.starting_equity = float(starting_equity)
        self.simulator_version = simulator_version
        self.cash = float(starting_equity)
        self.open_positions: dict[str, Position] = {}
        self.closed_trades: list[ClosedTrade] = []
        self._marks: dict[str, float] = {}
        self._equity_points: list[tuple[int, float]] = []

    # ------------------------------------------------------------------ #
    # Cash / positions
    # ------------------------------------------------------------------ #

    def can_afford(self, notional_usd: float) -> bool:
        return notional_usd > 0 and self.cash >= notional_usd - 1e-9

    def open(self, pos: Position, entry: OrderResult) -> None:
        """Book a filled entry: debit notional + fee, take custody of the position."""
        if not entry.accepted or entry.filled_qty <= 0:
            raise ValueError("cannot open a position from an unfilled order")
        if pos.id in self.open_positions:
            raise ValueError(f"position {pos.id} is already open")

        notional = entry.avg_price * entry.filled_qty
        self.cash -= notional + entry.fees_usd

        pos.qty = entry.filled_qty
        pos.entry_price = entry.avg_price
        pos.notional_usd = notional
        pos.entry_fees_usd = entry.fees_usd
        pos.entry_slippage_bps = entry.slippage_bps
        pos.last_price = entry.avg_price
        if pos.best_price <= 0:
            pos.best_price = entry.avg_price
        pos.ledger = self.name

        self.open_positions[pos.id] = pos
        self._marks[pos.symbol] = entry.avg_price

    def close(
        self, pos: Position, exit_: OrderResult, reason: ExitReason, ts_ms: int
    ) -> ClosedTrade:
        """Book an exit and **return the capital to cash immediately**.

        A partial exit closes only the filled part: the remainder stays open
        with its cost basis reduced pro rata, because pretending the rest
        vanished would silently invent P&L.
        """
        if not exit_.accepted or exit_.filled_qty <= 0:
            raise ValueError("cannot close a position from an unfilled order")

        qty = min(exit_.filled_qty, pos.qty)
        exit_price = exit_.avg_price
        proceeds = exit_price * qty
        exit_fee = exit_.fees_usd * (qty / exit_.filled_qty)

        # --- capital recycling: unconditional, immediate, before anything else
        self.cash += proceeds - exit_fee

        share = qty / pos.qty if pos.qty > 0 else 1.0
        entry_fee = pos.entry_fees_usd * share
        entry_notional = pos.entry_price * qty
        risk_usd = pos.risk_usd * share

        if pos.side is Side.LONG:
            gross = (exit_price - pos.entry_price) * qty
        else:
            gross = (pos.entry_price - exit_price) * qty

        slippage_usd = (
            entry_notional * max(pos.entry_slippage_bps, 0.0) / _BPS
            + proceeds * max(exit_.slippage_bps, 0.0) / _BPS
        )

        trade = ClosedTrade(
            id=pos.id,
            symbol=pos.symbol,
            side=pos.side,
            strategy=pos.strategy,
            regime=pos.regime,
            ledger=self.name,
            qty=qty,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            entry_ms=pos.entry_ms,
            exit_ms=ts_ms,
            notional_usd=entry_notional,
            gross_pnl_usd=gross,
            entry_fee_usd=entry_fee,
            exit_fee_usd=exit_fee,
            slippage_usd=slippage_usd,
            net_pnl_usd=gross - entry_fee - exit_fee,
            exit_reason=reason,
            risk_usd=risk_usd,
            expected_net_edge_bps=pos.expected_net_edge_bps,
            simulator_version=self.simulator_version,
        )
        self.closed_trades.append(trade)

        # Relative tolerance: dust left by lot rounding is not a position.
        if qty >= pos.qty * (1.0 - 1e-9):
            self.open_positions.pop(pos.id, None)
        else:
            pos.qty -= qty
            pos.notional_usd -= entry_notional
            pos.entry_fees_usd -= entry_fee
            pos.risk_usd -= risk_usd

        self._marks[pos.symbol] = exit_price
        pos.last_price = exit_price
        self._equity_points.append((ts_ms, self.equity))
        return trade

    def mark(self, prices: dict[str, float], ts_ms: int) -> None:
        """Mark open positions to the supplied prices and stamp the equity curve."""
        for symbol, price in prices.items():
            if price > 0:
                self._marks[symbol] = price
        for pos in self.open_positions.values():
            price = self._marks.get(pos.symbol)
            if price and price > 0:
                pos.last_price = price
        self._equity_points.append((ts_ms, self.equity))

    # ------------------------------------------------------------------ #
    # Valuation
    # ------------------------------------------------------------------ #

    def _mark_price(self, pos: Position) -> float:
        return self._marks.get(pos.symbol) or pos.last_price or pos.entry_price

    @property
    def equity(self) -> float:
        return self.cash + sum(
            self._mark_price(p) * p.qty for p in self.open_positions.values()
        )

    @property
    def exposure_usd(self) -> float:
        return sum(self._mark_price(p) * p.qty for p in self.open_positions.values())

    @property
    def unrealized_pnl(self) -> float:
        return sum(
            p.unrealized_usd(self._mark_price(p)) for p in self.open_positions.values()
        )

    @property
    def realized_pnl(self) -> float:
        return sum(t.net_pnl_usd for t in self.closed_trades)

    def realized_since(self, since_ms: int) -> float:
        return sum(t.net_pnl_usd for t in self.closed_trades if t.exit_ms >= since_ms)

    def loss_streak(self) -> int:
        """Consecutive losing closed trades, counted back from the most recent."""
        streak = 0
        for trade in reversed(self.closed_trades):
            if trade.net_pnl_usd < 0:
                streak += 1
            else:
                break
        return streak

    def equity_points(self) -> list[tuple[int, float]]:
        return list(self._equity_points)

    # ------------------------------------------------------------------ #
    # Statistics
    # ------------------------------------------------------------------ #

    def stats(
        self, *, strategy: str | None = None, since_ms: int | None = None
    ) -> PerfStats:
        trades = [
            t
            for t in self.closed_trades
            if (strategy is None or t.strategy == strategy)
            and (since_ms is None or t.exit_ms >= since_ms)
        ]
        stats = PerfStats()
        if not trades:
            return stats

        wins = [t for t in trades if t.net_pnl_usd > 0]
        losses = [t for t in trades if t.net_pnl_usd < 0]

        stats.trades = len(trades)
        stats.wins = len(wins)
        stats.losses = len(losses)
        stats.gross_pnl = sum(t.gross_pnl_usd for t in trades)
        stats.fees = sum(t.fees_usd for t in trades)
        stats.slippage = sum(t.slippage_usd for t in trades)
        stats.net_pnl = sum(t.net_pnl_usd for t in trades)
        stats.avg_win = (sum(t.net_pnl_usd for t in wins) / len(wins)) if wins else 0.0
        stats.avg_loss = (
            abs(sum(t.net_pnl_usd for t in losses)) / len(losses) if losses else 0.0
        )
        gross_win = sum(t.net_pnl_usd for t in wins)
        gross_loss = abs(sum(t.net_pnl_usd for t in losses))
        if gross_loss > 0:
            stats.profit_factor = gross_win / gross_loss
        else:
            stats.profit_factor = float("inf") if gross_win > 0 else 0.0
        stats.expectancy = stats.net_pnl / stats.trades
        stats.avg_holding_seconds = sum(t.holding_seconds for t in trades) / len(trades)
        stats.max_drawdown = self._max_drawdown(since_ms)
        stats.capital_turnover = self._turnover(trades, since_ms)
        return stats

    def _max_drawdown(self, since_ms: int | None) -> float:
        points = [
            (ts, eq)
            for ts, eq in self._equity_points
            if since_ms is None or ts >= since_ms
        ]
        if not points:
            return 0.0
        peak = points[0][1]
        worst = 0.0
        for _, eq in points:
            peak = max(peak, eq)
            worst = max(worst, peak - eq)
        return worst

    def _turnover(self, trades: list[ClosedTrade], since_ms: int | None) -> float:
        """Closed notional / starting equity — per day once a window is given.

        Turnover is the engine of a micro-profit book: the same $500 doing ten
        round trips a day is $5,000 of traded notional, and it is the number
        that tells us whether capital is actually being recycled.
        """
        if self.starting_equity <= 0:
            return 0.0
        traded = sum(t.notional_usd for t in trades) / self.starting_equity
        if since_ms is None:
            return traded
        end_ms = max(t.exit_ms for t in trades)
        days = (end_ms - since_ms) / _MS_PER_DAY
        if days <= 0:
            return traded
        return traded / days
