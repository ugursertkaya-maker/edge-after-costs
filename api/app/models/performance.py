"""Equity curve, per-strategy performance windows and backtest results.

These three tables are what the dashboard reads and what the §22 adaptive layer
learns from, so none of them are ever pruned by retention.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, TsMixin


class EquitySnapshot(IdMixin, TsMixin, Base):
    """One mark-to-market point of a ledger's equity curve."""

    __tablename__ = "equity_snapshots"
    __table_args__ = (sa.Index("ix_equity_snapshots_ledger_ts_ms", "ledger", "ts_ms"),)

    ledger: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="base", index=True)
    cash: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    equity: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    exposure_usd: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    open_positions: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    simulator_version: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="")


class StrategyPerformance(IdMixin, TsMixin, Base):
    """A rolling performance window for one strategy on one ledger.

    ``window`` is one of ``rolling_24h`` / ``rolling_7d`` / ``all``; rows are
    appended each snapshot rather than updated so the dashboard can chart how a
    strategy's expectancy itself drifts.
    """

    __tablename__ = "strategy_performance"
    __table_args__ = (
        sa.Index("ix_strategy_performance_ledger_strategy_window_ts_ms",
                 "ledger", "strategy", "window", "ts_ms"),
    )

    ledger: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="base", index=True)
    strategy: Mapped[str] = mapped_column(sa.String(48), nullable=False, default="", index=True)
    window: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="all", index=True)

    trades: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    wins: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    win_rate: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    avg_win: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    avg_loss: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    profit_factor: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    expectancy: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    fees: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    net_pnl: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    max_drawdown: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    avg_holding_seconds: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    capital_turnover: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)


class BacktestRun(IdMixin, TsMixin, Base):
    """A stored backtest or walk-forward run.

    The whole config is kept alongside the stats because a result without the
    parameters that produced it is not evidence of anything.
    """

    __tablename__ = "backtest_runs"

    label: Mapped[str] = mapped_column(sa.String(96), nullable=False, default="", index=True)
    kind: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="backtest", index=True)
    symbols: Mapped[Any] = mapped_column(sa.JSON, nullable=True)
    start_ms: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    end_ms: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    config: Mapped[Any] = mapped_column(sa.JSON, nullable=True)
    stats: Mapped[Any] = mapped_column(sa.JSON, nullable=True)
    per_strategy: Mapped[Any] = mapped_column(sa.JSON, nullable=True)
    in_sample: Mapped[Any] = mapped_column(sa.JSON, nullable=True)
    out_of_sample: Mapped[Any] = mapped_column(sa.JSON, nullable=True)


__all__ = ["EquitySnapshot", "StrategyPerformance", "BacktestRun"]
