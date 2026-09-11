"""Universe metadata and the aggregated microstructure series."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, TsMixin


class Market(IdMixin, Base):
    """One tradable pair as the universe selector last saw it.

    Rows are upserted, never duplicated: ``symbol`` is the business key and
    ``first_seen_ms``/``last_seen_ms`` bracket how long the pair has been in
    (or eligible for) the scanned universe.
    """

    __tablename__ = "markets"

    symbol: Mapped[str] = mapped_column(sa.String(32), nullable=False, unique=True)
    base_asset: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="")
    quote_asset: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="")
    status: Mapped[str] = mapped_column(sa.String(24), nullable=False, default="TRADING")

    tick_size: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    step_size: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    min_qty: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    min_notional: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)

    tier: Mapped[str] = mapped_column(sa.String(8), nullable=False, default="warm", index=True)
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True, index=True)

    first_seen_ms: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    last_seen_ms: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0, index=True)


class MarketStat(IdMixin, TsMixin, Base):
    """Aggregated microstructure for one symbol at one evaluation snapshot.

    This is *not* a tick table: the worker writes one row per symbol per
    ``worker.snapshot_interval_seconds`` so 12 symbols cost ~17k rows/day, which
    the retention sweep then trims to ``worker.tick_retention_hours``.
    """

    __tablename__ = "market_stats"
    __table_args__ = (sa.Index("ix_market_stats_symbol_ts_ms", "symbol", "ts_ms"),)

    symbol: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    price: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    spread_bps: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    depth_bid_usd: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    depth_ask_usd: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    imbalance: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    atr_bps: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    realized_vol_bps: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    volume_ratio: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    quote_volume_24h: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    trades_24h: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    regime: Mapped[str] = mapped_column(sa.String(24), nullable=False, default="UNKNOWN")


__all__ = ["Market", "MarketStat"]
