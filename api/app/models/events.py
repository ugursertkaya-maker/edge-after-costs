"""Operational event log: risk trips, system messages, data-quality faults.

``data_quality_events`` exists because §2 forbids trading on stale data — when
the bot refuses to trade we want the evidence, not a silent gap in the chart.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, TsMixin


class RiskEvent(IdMixin, TsMixin, Base):
    """A circuit-breaker trip, veto or cooldown (§21).  Never auto-pruned."""

    __tablename__ = "risk_events"

    kind: Mapped[str] = mapped_column(sa.String(48), nullable=False, index=True)
    symbol: Mapped[str | None] = mapped_column(sa.String(32), nullable=True, index=True)
    strategy: Mapped[str | None] = mapped_column(sa.String(48), nullable=True)
    detail: Mapped[Any] = mapped_column(sa.JSON, nullable=True)


class SystemEvent(IdMixin, TsMixin, Base):
    """Structured application log line (INFO/WARN/ERROR) with a JSON payload."""

    __tablename__ = "system_events"
    __table_args__ = (sa.Index("ix_system_events_level_ts_ms", "level", "ts_ms"),)

    level: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="INFO", index=True)
    source: Mapped[str] = mapped_column(sa.String(48), nullable=False, default="", index=True)
    message: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    detail: Mapped[Any] = mapped_column(sa.JSON, nullable=True)


class DataQualityEvent(IdMixin, TsMixin, Base):
    """A feed fault: STALE / DESYNC / GAP / RECONNECT / DROP."""

    __tablename__ = "data_quality_events"
    __table_args__ = (sa.Index("ix_data_quality_events_symbol_ts_ms", "symbol", "ts_ms"),)

    KINDS = ("STALE", "DESYNC", "GAP", "RECONNECT", "DROP")

    symbol: Mapped[str | None] = mapped_column(sa.String(32), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(sa.String(16), nullable=False, index=True)
    detail: Mapped[Any] = mapped_column(sa.JSON, nullable=True)


__all__ = ["RiskEvent", "SystemEvent", "DataQualityEvent"]
