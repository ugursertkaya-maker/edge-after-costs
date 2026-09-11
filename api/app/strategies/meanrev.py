"""Mean reversion — fade a two-sigma stretch that has run out of sellers.

Fading an extension only works when the pressure that caused it is dying.  So
the entry needs three independent confirmations, one from each data source we
have: price (beyond the 2-sigma band and at the edge of the 20-bar range),
trades (the aggressive flow that pushed price out is *decelerating*), and the
book (the microprice has already tilted back toward our side).

Without the flow and book confirmations this is just "sell what went up", which
is the classic way to be run over in a trending market — hence the regime gate
to ``RANGING`` / ``LOW_VOLATILITY`` only.
"""

from __future__ import annotations

from ..types import Features, Regime, RegimeState, Side, Signal
from .base import Strategy, clamp, scale

RANGE_POS_HIGH = 0.85
RANGE_POS_LOW = 0.15
MIN_EXTENSION_ATR = 0.6         # floor when the Bollinger band is degenerate
FLOW_PRESSURE_MIN = 0.05        # the move was driven by aggressive flow...
FLOW_EXHAUSTION_MIN = 0.03      # ...which is now fading

TARGET_ATR_MULT_MIN = 0.6
TARGET_ATR_MULT_MAX = 1.4
REVERSION_CAPTURE = 0.6         # share of the stretch we expect to get back
STOP_ATR_MULT = 0.9


class MeanReversion(Strategy):
    name = "mean_reversion"
    allowed_regimes = frozenset({Regime.RANGING, Regime.LOW_VOLATILITY})
    min_score = 0.38
    default_horizon_seconds = 900

    def _evaluate(self, f: Features, regime: RegimeState) -> Signal | None:
        atr = f.atr_bps
        # bb_width_bps spans mean +/- 2 sigma, so half of it *is* the 2-sigma
        # extension we are looking for.
        threshold = max(f.bb_width_bps / 2.0, MIN_EXTENSION_ATR * atr)
        if threshold <= 0:
            return None

        if f.vwap_dev_bps <= -threshold and f.range_pos <= RANGE_POS_LOW:
            direction = 1                      # stretched down -> buy
        elif f.vwap_dev_bps >= threshold and f.range_pos >= RANGE_POS_HIGH:
            direction = -1                     # stretched up -> sell
        else:
            return None

        # The aggressors that caused the stretch must be losing steam:
        # flow still points the way price went, acceleration points back.
        if -direction * f.trade_flow_imbalance < FLOW_PRESSURE_MIN:
            return None
        if direction * f.trade_flow_accel < FLOW_EXHAUSTION_MIN:
            return None

        # The book has to agree: microprice tilted back toward our side.
        if f.mid <= 0:
            return None
        micro_off_bps = (f.microprice - f.mid) / f.mid * 10_000.0
        if direction * micro_off_bps < 0:
            return None

        stretch_atr = abs(f.vwap_dev_bps) / atr
        score = clamp(
            0.30 * scale(stretch_atr, MIN_EXTENSION_ATR, 3.0)
            + 0.25 * scale(direction * f.trade_flow_accel, FLOW_EXHAUSTION_MIN, 0.5)
            + 0.20 * scale(-direction * f.trade_flow_imbalance, FLOW_PRESSURE_MIN, 0.6)
            + 0.15 * scale(direction * micro_off_bps, 0.0, max(0.5 * f.spread_bps, 0.5))
            + 0.10 * (1.0 - f.trend_strength)
        )

        # Aim for part of the way back to value, bounded by ATR either side.
        target = clamp(
            abs(f.vwap_dev_bps) * REVERSION_CAPTURE,
            atr * TARGET_ATR_MULT_MIN,
            atr * TARGET_ATR_MULT_MAX,
        )

        side = Side.LONG if direction > 0 else Side.SHORT
        return self._signal(
            f,
            side,
            score=score,
            target_bps=target,
            stop_bps=atr * STOP_ATR_MULT,
            reasons=[
                f"{stretch_atr:.1f} ATR from VWAP ({f.vwap_dev_bps:+.0f} bps)",
                f"range pos {f.range_pos:.2f}",
                f"flow {f.trade_flow_imbalance:+.2f} fading ({f.trade_flow_accel:+.2f})",
            ],
            features_used={
                "vwap_dev_bps": f.vwap_dev_bps,
                "bb_width_bps": f.bb_width_bps,
                "trade_flow_accel": f.trade_flow_accel,
                "micro_off_bps": micro_off_bps,
            },
        )
