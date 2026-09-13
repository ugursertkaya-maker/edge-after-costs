"""Breakout scalp — a squeeze that resolves.

Compression is the setup, not the trade.  We want a market that has been
coiling (Bollinger width in the bottom of its own 120-bar distribution), then a
bar that leaves the 20-bar range with a genuine volume expansion and aggressive
flow pushing the same way.  Volume is the part that separates a break from a
drift: a range edge taken out on average volume is where liquidity providers
fade you.
"""

from __future__ import annotations

from ..types import Features, Regime, RegimeState, Side, Signal
from .base import Strategy, clamp, scale, horizon_move_bps

COMPRESSION_MIN = 0.65
VOLUME_RATIO_MIN = 1.8
RANGE_POS_HIGH = 0.88
RANGE_POS_LOW = 0.12
FLOW_MIN = 0.10

TARGET_ATR_MULT = 1.4
STOP_ATR_MULT = 0.9


class BreakoutScalp(Strategy):
    name = "breakout_scalp"
    allowed_regimes = frozenset({Regime.BREAKOUT, Regime.LOW_VOLATILITY})
    min_score = 0.40
    default_horizon_seconds = 600

    def _evaluate(self, f: Features, regime: RegimeState) -> Signal | None:
        # The squeeze is measured *before* the breaking bar widened the bands,
        # so take whichever reading still remembers the coil.
        compression = max(f.compression, float(f.extras.get("compression_prev", 0.0)))
        if compression < self.relaxed(COMPRESSION_MIN):
            return self._no("compression", compression, COMPRESSION_MIN)
        if f.volume_ratio < self.relaxed(VOLUME_RATIO_MIN):
            return self._no("volume_ratio", f.volume_ratio, VOLUME_RATIO_MIN)

        if f.range_pos >= RANGE_POS_HIGH:
            direction = 1
        elif f.range_pos <= RANGE_POS_LOW:
            direction = -1
        else:
            return self._no("range_pos", f.range_pos, RANGE_POS_HIGH)

        if direction * f.trade_flow_imbalance < self.relaxed(FLOW_MIN):
            return self._no("trade_flow", direction * f.trade_flow_imbalance, FLOW_MIN)
        if direction * f.ret_1m_bps <= 0:
            return self._no("ret_1m_agree", direction * f.ret_1m_bps, 0.0)

        atr = f.atr_bps
        score = clamp(
            0.30 * scale(compression, COMPRESSION_MIN, 0.95)
            + 0.30 * scale(f.volume_ratio, VOLUME_RATIO_MIN, 4.0)
            + 0.20 * scale(direction * f.trade_flow_imbalance, FLOW_MIN, 0.6)
            + 0.20 * scale(direction * f.ret_1m_bps / atr, 0.1, 1.0)
        )

        side = Side.LONG if direction > 0 else Side.SHORT
        edge = "high" if direction > 0 else "low"
        return self._signal(
            f,
            side,
            score=score,
            target_bps=horizon_move_bps(f, self.horizon_seconds)
            * self.target_mult(TARGET_ATR_MULT),
            stop_bps=horizon_move_bps(f, self.horizon_seconds)
            * self.stop_mult(STOP_ATR_MULT),
            reasons=[
                f"squeeze {compression:.2f} released",
                f"broke 20-bar {edge} (pos {f.range_pos:.2f})",
                f"volume x{f.volume_ratio:.1f}, flow {f.trade_flow_imbalance:+.2f}",
            ],
            features_used={
                "compression": compression,
                "range_pos": f.range_pos,
                "volume_ratio": f.volume_ratio,
            },
        )
