"""The strategy registry.

Six independent opinion generators.  They never see each other's output, never
see capital or risk, and are combined only by the ranker, which costs each
signal and compares them on net edge.  Adding a strategy means adding a module
and one entry to :data:`_REGISTRY` — nothing else in the bot changes.
"""

from __future__ import annotations

from typing import Iterable

from ..config import Settings
from .base import Strategy
from .breakout import BreakoutScalp
from .flow import FlowImpulse
from .imbalance import BookImbalance
from .meanrev import MeanReversion
from .momentum import MomentumScalp
from .pullback import TrendPullback

_REGISTRY: dict[str, type[Strategy]] = {
    MomentumScalp.name: MomentumScalp,
    BreakoutScalp.name: BreakoutScalp,
    TrendPullback.name: TrendPullback,
    MeanReversion.name: MeanReversion,
    FlowImpulse.name: FlowImpulse,
    BookImbalance.name: BookImbalance,
}

STRATEGY_NAMES: tuple[str, ...] = (
    "momentum_scalp",
    "breakout_scalp",
    "trend_pullback",
    "mean_reversion",
    "flow_impulse",
    "book_imbalance",
)


def get_strategies(
    names: Iterable[str] | None = None,
    *,
    settings: Settings | None = None,
) -> list[Strategy]:
    """Instantiate strategies, in :data:`STRATEGY_NAMES` order.

    ``names`` selects a subset (unknown names raise, so a typo in config fails
    at start-up rather than silently disabling a strategy).
    """
    if names is None:
        wanted = list(STRATEGY_NAMES)
    else:
        wanted = list(dict.fromkeys(names))
        unknown = [n for n in wanted if n not in _REGISTRY]
        if unknown:
            raise KeyError(f"unknown strategies: {', '.join(sorted(unknown))}")
        wanted.sort(key=STRATEGY_NAMES.index)
    return [_REGISTRY[n](settings=settings) for n in wanted]


__all__ = [
    "STRATEGY_NAMES",
    "Strategy",
    "get_strategies",
    "BookImbalance",
    "BreakoutScalp",
    "FlowImpulse",
    "MeanReversion",
    "MomentumScalp",
    "TrendPullback",
]
