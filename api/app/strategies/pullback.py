"""Trend pullback — buy the dip inside an established trend, not against it.

Three things have to be true at once, and the order matters:

1. the trend is real (EMA structure plus ``trend_strength``),
2. price has actually pulled back toward the slow EMA / VWAP — and the pullback
   is *controlled*, i.e. shallow relative to ATR rather than a trend break,
3. the pullback has stopped: the most recent bar ticked back with the trend.

Entering on (1) and (2) alone is how you catch a reversal; the continuation
tick in (3) is what makes this a pullback trade rather than a falling-knife.
"""

from __future__ import annotations

from ..types import Features, Regime, RegimeState, Side, Signal
from .base import Strategy, clamp, scale

TREND_STRENGTH_MIN = 0.45
# Pullback depth, measured from the slow EMA in ATR units.
SLOW_EMA_NEAR_ATR = 1.2
SLOW_EMA_THROUGH_ATR = 0.6      # tolerated overshoot past the slow EMA
PULLBACK_MIN_ATR = 0.05         # there must have *been* a pullback
PULLBACK_MAX_ATR = 2.0          # deeper than this is a trend break
RESUME_MIN_ATR = 0.05           # continuation tick
FLOW_AGAINST_MAX = 0.25

TARGET_ATR_MULT = 1.2
STOP_ATR_MULT = 0.85


class TrendPullback(Strategy):
    name = "trend_pullback"
    allowed_regimes = frozenset({Regime.TRENDING})
    min_score = 0.38
    default_horizon_seconds = 900

    def _evaluate(self, f: Features, regime: RegimeState) -> Signal | None:
        atr = f.atr_bps
        gap = f.ema_fast - f.ema_slow
        if gap == 0.0 or f.trend_strength < TREND_STRENGTH_MIN:
            return None
        direction = 1 if gap > 0 else -1

        # Distance from the slow EMA, signed with the trend.
        if f.ema_slow <= 0:
            return None
        dist_atr = (f.price - f.ema_slow) / f.price * 10_000.0 / atr * direction
        if dist_atr > SLOW_EMA_NEAR_ATR or dist_atr < -SLOW_EMA_THROUGH_ATR:
            return None

        # A real, controlled retracement over the last five bars.
        pullback_atr = -direction * f.ret_5m_bps / atr
        if pullback_atr < PULLBACK_MIN_ATR or pullback_atr > PULLBACK_MAX_ATR:
            return None

        # ...that has stopped.
        resume_atr = direction * f.ret_1m_bps / atr
        if resume_atr < RESUME_MIN_ATR:
            return None

        if direction * f.trade_flow_imbalance < -FLOW_AGAINST_MAX:
            return None

        # VWAP confirms the same picture: price is back at value, not extended.
        vwap_atr = abs(f.vwap_dev_bps) / atr

        score = clamp(
            0.30 * f.trend_strength
            + 0.25 * (1.0 - scale(abs(dist_atr), 0.0, SLOW_EMA_NEAR_ATR))
            + 0.20 * scale(resume_atr, RESUME_MIN_ATR, 0.8)
            + 0.15 * (1.0 - scale(pullback_atr, PULLBACK_MIN_ATR, PULLBACK_MAX_ATR))
            + 0.10 * (1.0 - scale(vwap_atr, 0.0, 2.0))
        )

        side = Side.LONG if direction > 0 else Side.SHORT
        return self._signal(
            f,
            side,
            score=score,
            target_bps=atr * TARGET_ATR_MULT,
            stop_bps=atr * STOP_ATR_MULT,
            reasons=[
                f"trend {f.trend_strength:.2f} {'up' if direction > 0 else 'down'}",
                f"pullback {pullback_atr:.2f} ATR to EMA30",
                f"resumed {f.ret_1m_bps:+.0f} bps",
            ],
            features_used={
                "dist_to_ema_slow_atr": dist_atr,
                "pullback_atr": pullback_atr,
                "vwap_dev_bps": f.vwap_dev_bps,
            },
        )
