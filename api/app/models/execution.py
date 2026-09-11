"""Simulated execution: orders, fills, open positions and closed round trips.

The split between ``paper_orders``/``paper_fills`` and ``positions``/``trades``
mirrors reality: an order is an *attempt* (it can be rejected or partially
filled), a position is what that attempt actually produced.  Only ``trades``
rows are treated as durable P&L history — retention never touches them.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, TsMixin

# SQLite has no BIGSERIAL; a plain INTEGER PK is what makes ``rowid`` aliasing
# (and therefore autoincrement) work there, so foreign keys mirror that.
_FKINT = sa.BigInteger().with_variant(sa.Integer, "sqlite")


class PaperOrder(IdMixin, TsMixin, Base):
    """One simulated order attempt, entry or exit leg."""

    __tablename__ = "paper_orders"
    __table_args__ = (
        sa.Index("ix_paper_orders_symbol_ts_ms", "symbol", "ts_ms"),
    )

    position_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    side: Mapped[str] = mapped_column(sa.String(8), nullable=False, default="LONG")
    order_type: Mapped[str] = mapped_column(sa.String(8), nullable=False, default="MARKET")

    requested_qty: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    filled_qty: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    avg_price: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    fees_usd: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    slippage_bps: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    latency_ms: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    accepted: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False, index=True)
    reject_reason: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    leg: Mapped[str] = mapped_column(sa.String(8), nullable=False, default="ENTRY", index=True)


class PaperFill(IdMixin, Base):
    """A single price level consumed by a simulated order.

    Stored per level (not aggregated) so slippage can be re-derived from the
    actual book walk when a fill looks suspicious.
    """

    __tablename__ = "paper_fills"

    order_id: Mapped[int] = mapped_column(
        _FKINT, sa.ForeignKey("paper_orders.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    price: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    qty: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    fee_usd: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    is_maker: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    ts_ms: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0, index=True)


class Position(IdMixin, Base):
    """An open paper position, flipped to CLOSED when its trade is recorded.

    ``position_id`` is the in-memory ``Position.id`` from ``types.py`` and is
    the join key used by ``paper_orders`` and by ``trades.trade_id``.
    """

    __tablename__ = "positions"
    __table_args__ = (
        sa.Index("ix_positions_ledger_status", "ledger", "status"),
    )

    position_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    ledger: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="base", index=True)
    symbol: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    side: Mapped[str] = mapped_column(sa.String(8), nullable=False, default="LONG")
    strategy: Mapped[str] = mapped_column(sa.String(48), nullable=False, default="", index=True)
    regime: Mapped[str] = mapped_column(sa.String(24), nullable=False, default="UNKNOWN")

    qty: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    entry_price: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    entry_ms: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0, index=True)
    notional_usd: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    risk_usd: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    take_profit: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    stop_loss: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)

    entry_fees_usd: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    entry_slippage_bps: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    expected_net_edge_bps: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)

    status: Mapped[str] = mapped_column(sa.String(8), nullable=False, default="OPEN", index=True)
    closed_ms: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    meta: Mapped[Any] = mapped_column(sa.JSON, nullable=True)


class Trade(IdMixin, Base):
    """A completed round trip with every cost itemised (§20, §30).

    This table is the ground truth for expectancy, so it is append-only and
    exempt from retention.
    """

    __tablename__ = "trades"
    __table_args__ = (
        sa.Index("ix_trades_ledger_exit_ms", "ledger", "exit_ms"),
        sa.Index("ix_trades_strategy_symbol_regime", "strategy", "symbol", "regime"),
    )

    trade_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    ledger: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="base", index=True)
    symbol: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    side: Mapped[str] = mapped_column(sa.String(8), nullable=False, default="LONG")
    strategy: Mapped[str] = mapped_column(sa.String(48), nullable=False, default="", index=True)
    regime: Mapped[str] = mapped_column(sa.String(24), nullable=False, default="UNKNOWN")

    qty: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    entry_price: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    exit_price: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    entry_ms: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0, index=True)
    exit_ms: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0, index=True)

    notional_usd: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    gross_pnl_usd: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    entry_fee_usd: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    exit_fee_usd: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    slippage_usd: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    net_pnl_usd: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)

    exit_reason: Mapped[str] = mapped_column(sa.String(24), nullable=False, default="", index=True)
    risk_usd: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    expected_net_edge_bps: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    holding_seconds: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    r_multiple: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    simulator_version: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="")


__all__ = ["PaperOrder", "PaperFill", "Position", "Trade"]
