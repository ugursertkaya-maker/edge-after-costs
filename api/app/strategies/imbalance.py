"""Book imbalance — the smallest, fastest scalp in the set.

When resting depth within 10 bps of mid is heavily one-sided, the microprice
(size-weighted mid) leans toward the *thin* side, because that is where price
has least resistance to move.  Trading that lean is a queue-position bet with a
tiny edge, so it only makes sense when the spread is a small fraction of the
target — which the base class enforces — and when both sides of the book are
actually populated.

The target is intentionally the smallest of the six: this is a turnover trade,
not a directional view, which is why it is allowed in quiet regimes.
"""

from __future__ import annotations

from ..types import Features, Regime, RegimeState, Side, Signal
from .base import Strategy, clamp, scale, horizon_move_bps

IMBALANCE_MIN = 0.35
MICRO_OFFSET_FRAC_OF_SPREAD = 0.12
FLOW_AGAINST_MAX = 0.45

TARGET_ATR_MULT = 0.6
STOP_ATR_MULT = 0.55


class BookImbalance(Strategy):
    name = "book_imbalance"
    allowed_regimes = frozenset(
        {Regime.RANGING, Regime.TRENDING, Regime.LOW_VOLATILITY}
    )
    min_score = 0.38
    default_horizon_seconds = 120

    def _evaluate(self, f: Features, regime: RegimeState) -> Signal | None:
        atr = f.atr_bps
        if f.mid <= 0 or f.depth_bid_usd <= 0 or f.depth_ask_usd <= 0:
            return self._no("book_empty", min(f.depth_bid_usd, f.depth_ask_usd), 1e-9)
        if abs(f.imbalance) < self.relaxed(IMBALANCE_MIN):
            return self._no("imbalance", abs(f.imbalance), IMBALANCE_MIN)
        direction = 1 if f.imbalance > 0 else -1

        micro_off_bps = (f.microprice - f.mid) / f.mid * 10_000.0
        if direction * micro_off_bps < MICRO_OFFSET_FRAC_OF_SPREAD * f.spread_bps:
            return self._no("micro_offset", direction * micro_off_bps, MICRO_OFFSET_FRAC_OF_SPREAD * f.spread_bps)

        # Aggressive flow running hard the other way overwhelms resting depth.
        if direction * f.trade_flow_imbalance < -FLOW_AGAINST_MAX:
            return self._no("flow_against", direction * f.trade_flow_imbalance, -FLOW_AGAINST_MAX)

        score = clamp(
            0.40 * scale(abs(f.imbalance), IMBALANCE_MIN, 0.85)
            + 0.30 * scale(
                direction * micro_off_bps,
                MICRO_OFFSET_FRAC_OF_SPREAD * f.spread_bps,
                0.5 * f.spread_bps,
            )
            + 0.20 * (1.0 - scale(f.spread_bps, 0.0, max(atr * 0.3, 1e-9)))
            + 0.10 * clamp(0.5 + 0.5 * direction * f.trade_flow_imbalance)
        )

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
                f"depth imbalance {f.imbalance:+.2f}",
                f"microprice {micro_off_bps:+.2f} bps off mid",
                f"spread {f.spread_bps:.1f} bps",
            ],
            features_used={
                "imbalance": f.imbalance,
                "micro_off_bps": micro_off_bps,
                "depth_bid_usd": f.depth_bid_usd,
                "depth_ask_usd": f.depth_ask_usd,
            },
        )
