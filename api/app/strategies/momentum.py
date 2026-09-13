"""Momentum scalp — ride an accelerating, volume-backed leg of a trend.

The edge this tries to capture is short-lived continuation: price is already
moving, the EMAs agree with the move, the last minute accelerated rather than
faded, and aggressive flow is on the same side.  It deliberately refuses to
buy a move that has run on thinning participation, because that is the shape
of an exhaustion tick, not a continuation.
"""

from __future__ import annotations

from ..types import Features, Regime, RegimeState, Side, Signal
from .base import Strategy, clamp, scale, horizon_move_bps

SLOPE_TO_ATR_MIN = 0.06        # EMA slope per bar, as a fraction of ATR
RET_TO_ATR_MIN = 0.15          # last-minute move, as a fraction of ATR
VOLUME_RATIO_MIN = 1.15
FLOW_MIN = 0.05

TARGET_ATR_MULT = 0.9
STOP_ATR_MULT = 0.7


class MomentumScalp(Strategy):
    name = "momentum_scalp"
    allowed_regimes = frozenset({Regime.TRENDING, Regime.HIGH_VOLATILITY})
    min_score = 0.40
    default_horizon_seconds = 300

    def _evaluate(self, f: Features, regime: RegimeState) -> Signal | None:
        atr = f.atr_bps
        gap = f.ema_fast - f.ema_slow
        if gap == 0.0:
            return self._no("ema_gap", 0.0, 0.0)
        if atr <= 0.0:
            # Every ratio below divides by ATR. A zero ATR is a market with no
            # measurable movement, not a setup with infinite conviction.
            return self._no("atr_zero", atr, 1e-9)
        direction = 1 if gap > 0 else -1

        slope_ratio = f.ema_slope_bps / atr
        ret_ratio = f.ret_1m_bps / atr
        ret5_ratio = f.ret_5m_bps / atr

        # Everything must point the same way as the EMA structure.
        if direction * slope_ratio < self.relaxed(SLOPE_TO_ATR_MIN):
            return self._no("ema_slope", direction * slope_ratio, SLOPE_TO_ATR_MIN)
        if direction * ret_ratio < self.relaxed(RET_TO_ATR_MIN):
            return self._no("ret_1m", direction * ret_ratio, RET_TO_ATR_MIN)
        if direction * ret5_ratio <= 0:
            return self._no("ret_5m_agree", direction * ret5_ratio, 0.0)
        if f.volume_ratio < self.relaxed(VOLUME_RATIO_MIN):
            return self._no("volume_ratio", f.volume_ratio, VOLUME_RATIO_MIN)
        if direction * f.trade_flow_imbalance < self.relaxed(FLOW_MIN):
            return self._no("trade_flow", direction * f.trade_flow_imbalance, FLOW_MIN)

        score = clamp(
            0.30 * scale(direction * slope_ratio, SLOPE_TO_ATR_MIN, 0.45)
            + 0.25 * scale(direction * ret_ratio, RET_TO_ATR_MIN, 1.0)
            + 0.20 * scale(f.volume_ratio, VOLUME_RATIO_MIN, 2.5)
            + 0.15 * scale(direction * f.trade_flow_imbalance, FLOW_MIN, 0.6)
            + 0.10 * f.trend_strength
        )
        # Accelerating flow is a bonus, decelerating flow a haircut.
        score *= clamp(1.0 + 0.25 * direction * f.trade_flow_accel, 0.7, 1.2)

        side = Side.LONG if direction > 0 else Side.SHORT
        return self._signal(
            f,
            side,
            score=score,
            target_bps=horizon_move_bps(f, self.horizon_seconds)
            * self.target_mult(TARGET_ATR_MULT),
            stop_bps=horizon_move_bps(f, self.horizon_seconds)
            * self.stop_mult(STOP_ATR_MULT),
            reasons=[
                f"EMA {'up' if direction > 0 else 'down'}, slope {f.ema_slope_bps:+.1f} bps",
                f"1m {f.ret_1m_bps:+.0f} bps on x{f.volume_ratio:.1f} volume",
                f"flow {f.trade_flow_imbalance:+.2f}",
            ],
            features_used={
                "ema_slope_bps": f.ema_slope_bps,
                "ret_1m_bps": f.ret_1m_bps,
                "trade_flow_imbalance": f.trade_flow_imbalance,
            },
        )
