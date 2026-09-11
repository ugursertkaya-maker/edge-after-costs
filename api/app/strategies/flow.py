"""Flow impulse — follow a burst of aggressive taker flow that price respected.

Trade-flow imbalance on its own is noise: someone always has to be the
aggressor.  What matters is (a) the imbalance is large, (b) it is *growing* —
the last 15 seconds are more one-sided than the last 60 — and (c) price has
already moved in that direction, which tells us the resting liquidity on that
side is being consumed rather than replenished.

This is the shortest-horizon strategy in the set; it expects to be right within
a couple of minutes or not at all.
"""

from __future__ import annotations

from ..types import Features, Regime, RegimeState, Side, Signal
from .base import Strategy, clamp, scale

FLOW_MIN = 0.35
ACCEL_MIN = 0.08
PRICE_RESPONSE_ATR = 0.20

TARGET_ATR_MULT = 1.0
STOP_ATR_MULT = 0.7


class FlowImpulse(Strategy):
    name = "flow_impulse"
    allowed_regimes = frozenset(
        {Regime.TRENDING, Regime.BREAKOUT, Regime.HIGH_VOLATILITY}
    )
    min_score = 0.40
    default_horizon_seconds = 180

    def _evaluate(self, f: Features, regime: RegimeState) -> Signal | None:
        atr = f.atr_bps
        if abs(f.trade_flow_imbalance) < FLOW_MIN:
            return None
        direction = 1 if f.trade_flow_imbalance > 0 else -1

        if direction * f.trade_flow_accel < ACCEL_MIN:
            return None

        response_atr = direction * f.ret_1m_bps / atr
        if response_atr < PRICE_RESPONSE_ATR:
            return None

        # An impulse into a shrinking book is a trap, not an opportunity: we
        # need the side we would be hitting to still have resting size.
        taking_side_depth = f.depth_ask_usd if direction > 0 else f.depth_bid_usd
        if taking_side_depth <= 0:
            return None

        score = clamp(
            0.35 * scale(abs(f.trade_flow_imbalance), FLOW_MIN, 0.85)
            + 0.30 * scale(direction * f.trade_flow_accel, ACCEL_MIN, 0.5)
            + 0.25 * scale(response_atr, PRICE_RESPONSE_ATR, 1.2)
            + 0.10 * scale(f.volume_ratio, 1.0, 3.0)
        )

        side = Side.LONG if direction > 0 else Side.SHORT
        return self._signal(
            f,
            side,
            score=score,
            target_bps=atr * TARGET_ATR_MULT,
            stop_bps=atr * STOP_ATR_MULT,
            reasons=[
                f"flow {f.trade_flow_imbalance:+.2f} accelerating "
                f"({f.trade_flow_accel:+.2f})",
                f"price followed {f.ret_1m_bps:+.0f} bps",
            ],
            features_used={
                "trade_flow_imbalance": f.trade_flow_imbalance,
                "trade_flow_accel": f.trade_flow_accel,
                "ret_1m_bps": f.ret_1m_bps,
            },
        )
