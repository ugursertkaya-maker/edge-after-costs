"""Strategy output: raw signals, their feature snapshot, and the ranked verdict.

Keeping the *rejected* decisions (``strategy_decisions``) is the point of this
group of tables — the post-mortem question is almost always "why did we not
take that move?", and a table of accepted trades alone can never answer it.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, TsMixin


class Signal(IdMixin, TsMixin, Base):
    """One strategy's opinion about one symbol at one instant."""

    __tablename__ = "signals"
    __table_args__ = (
        sa.Index("ix_signals_symbol_ts_ms", "symbol", "ts_ms"),
        sa.Index("ix_signals_strategy_ts_ms", "strategy", "ts_ms"),
    )

    symbol: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    strategy: Mapped[str] = mapped_column(sa.String(48), nullable=False, index=True)
    side: Mapped[str] = mapped_column(sa.String(8), nullable=False, default="LONG")
    score: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    target_bps: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    stop_bps: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    horizon_seconds: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    regime: Mapped[str] = mapped_column(sa.String(24), nullable=False, default="UNKNOWN")
    reasons: Mapped[Any] = mapped_column(sa.JSON, nullable=True)  # list[str]


class SignalFeature(IdMixin, Base):
    """The full ``Features`` payload behind a signal, kept separate.

    Split out of ``signals`` so the hot signal queries never drag a ~40-field
    JSON blob along, and so retention can drop features on the same sweep.
    """

    __tablename__ = "signal_features"

    signal_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer, "sqlite"),
        sa.ForeignKey("signals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    payload: Mapped[Any] = mapped_column(sa.JSON, nullable=True)


class StrategyDecision(IdMixin, TsMixin, Base):
    """The costed, ranked verdict for one signal — including the rejections.

    ``net_edge_bps = gross_edge_bps - fee_bps - spread_cost_bps - slippage_bps``
    is stored rather than recomputed so historical rows survive a change in the
    ranker's formula.
    """

    __tablename__ = "strategy_decisions"
    __table_args__ = (
        sa.Index("ix_strategy_decisions_symbol_ts_ms", "symbol", "ts_ms"),
        sa.Index("ix_strategy_decisions_strategy_ts_ms", "strategy", "ts_ms"),
    )

    symbol: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    strategy: Mapped[str] = mapped_column(sa.String(48), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="NO_TRADE")
    regime: Mapped[str] = mapped_column(sa.String(24), nullable=False, default="UNKNOWN")

    gross_edge_bps: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    fee_bps: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    spread_cost_bps: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    slippage_bps: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    net_edge_bps: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    rank_score: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)

    chosen: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False, index=True)
    reject_reason: Mapped[str | None] = mapped_column(sa.String(48), nullable=True, index=True)
    notes: Mapped[Any] = mapped_column(sa.JSON, nullable=True)


__all__ = ["Signal", "SignalFeature", "StrategyDecision"]
