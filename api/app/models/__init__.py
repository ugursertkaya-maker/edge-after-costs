"""Every persisted table, re-exported from one place.

Importing this package is what registers the whole schema on ``Base.metadata``,
so ``app.db.init_db`` only needs this single import to create everything.
"""

from __future__ import annotations

from app.models.base import Base, IdMixin, TsMixin
from app.models.events import DataQualityEvent, RiskEvent, SystemEvent
from app.models.execution import PaperFill, PaperOrder, Position, Trade
from app.models.market import Market, MarketStat
from app.models.performance import BacktestRun, EquitySnapshot, StrategyPerformance
from app.models.retention import PROTECTED_TABLES, apply_retention
from app.models.signal import Signal, SignalFeature, StrategyDecision
from app.models.user import User, hash_password, verify_password

# ``Signal``/``Position``/``Trade`` share a name with the in-memory dataclasses
# in ``app.types``.  These aliases let a module that needs both import the ORM
# side unambiguously instead of renaming on every import.
SignalRow = Signal
PositionRow = Position
TradeRow = Trade

__all__ = [
    "Base",
    "IdMixin",
    "TsMixin",
    "Market",
    "MarketStat",
    "Signal",
    "SignalRow",
    "SignalFeature",
    "StrategyDecision",
    "PaperOrder",
    "PaperFill",
    "Position",
    "PositionRow",
    "Trade",
    "TradeRow",
    "EquitySnapshot",
    "StrategyPerformance",
    "BacktestRun",
    "RiskEvent",
    "SystemEvent",
    "DataQualityEvent",
    "User",
    "hash_password",
    "verify_password",
    "apply_retention",
    "PROTECTED_TABLES",
]
