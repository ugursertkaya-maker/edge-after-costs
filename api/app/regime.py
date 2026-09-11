"""Market-regime classification.

The regime is the gate every strategy is checked against, so it is deliberately
blunt and explainable: a handful of thresholds on the features, evaluated in a
fixed precedence order, each producing a short human-readable reason that the
dashboard renders verbatim.

Precedence (first match wins):

1. ``ABNORMAL``  — the data or the market is unusable.  Blocks *all* trading.
2. ``BREAKOUT``  — the most specific setup: a compressed range that has just
   expanded on volume.  Checked before the volatility buckets because a genuine
   breakout also shows up as a volatility spike, and the breakout reading is
   the more actionable of the two.
3. ``LOW_VOLATILITY`` / ``HIGH_VOLATILITY`` — where realized vol sits in its own
   trailing distribution.
4. ``TRENDING``  — directional persistence with EMA alignment.
5. ``RANGING``   — everything else.
"""

from __future__ import annotations

from .config import Settings, get_settings
from .types import Features, Regime, RegimeState

# Thresholds (module level so tests and the UI quote the same numbers).
ABNORMAL_VOL_PCT = 0.98
SPREAD_ABNORMAL_MULT = 3.0
LOW_VOL_PCT = 0.15
HIGH_VOL_PCT = 0.85
BREAKOUT_COMPRESSION = 0.70
BREAKOUT_VOLUME_RATIO = 1.8
BREAKOUT_EXPANSION_MULT = 1.25      # bb width vs the previous bar's
TREND_STRENGTH_MIN = 0.55
MIN_BARS_FOR_REGIME = 30


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


class RegimeClassifier:
    """Stateless classifier — every input comes from :class:`Features`."""

    def __init__(self, *, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # -- helpers ----------------------------------------------------------- #

    def _abnormal_reasons(self, f: Features) -> list[str]:
        s = self.settings
        reasons: list[str] = []
        if f.vol_percentile > ABNORMAL_VOL_PCT:
            reasons.append(f"vol %ile {f.vol_percentile:.2f} > {ABNORMAL_VOL_PCT}")
        if f.realized_vol_bps > s.universe.max_volatility_bps:
            reasons.append(
                f"realized vol {f.realized_vol_bps:.0f} bps "
                f"> {s.universe.max_volatility_bps:.0f}"
            )
        baseline = float(f.extras.get("spread_baseline_bps", 0.0))
        if baseline <= 0:
            baseline = s.universe.max_spread_bps
        if baseline > 0 and f.spread_bps > SPREAD_ABNORMAL_MULT * baseline:
            reasons.append(
                f"spread {f.spread_bps:.1f} bps > {SPREAD_ABNORMAL_MULT:.0f}x "
                f"normal ({baseline:.1f})"
            )
        if f.book_age_ms > s.risk.max_book_age_ms:
            reasons.append(f"book {f.book_age_ms} ms stale")
        return reasons

    @staticmethod
    def _ema_aligned(f: Features) -> int:
        """+1 / -1 when EMA spread and slope agree, 0 otherwise."""
        gap = f.ema_fast - f.ema_slow
        if gap > 0 and f.ema_slope_bps > 0:
            return 1
        if gap < 0 and f.ema_slope_bps < 0:
            return -1
        return 0

    def _is_breakout(self, f: Features) -> bool:
        compression_before = max(
            float(f.extras.get("compression_prev", 0.0)), 0.0
        )
        if compression_before < BREAKOUT_COMPRESSION:
            return False
        if f.volume_ratio <= BREAKOUT_VOLUME_RATIO:
            return False
        prev_width = float(f.extras.get("bb_width_prev_bps", 0.0))
        expanded = f.bb_width_bps > BREAKOUT_EXPANSION_MULT * prev_width if prev_width > 0 else False
        if not expanded and f.atr_bps > 0:
            # A single wide bar counts as expansion even before the 20-bar
            # Bollinger window has caught up with it.
            expanded = float(f.extras.get("range_bps", 0.0)) > 1.5 * f.atr_bps
        return expanded

    # -- public API -------------------------------------------------------- #

    def classify(self, f: Features) -> RegimeState:
        abnormal = self._abnormal_reasons(f)
        if abnormal:
            # Confidence is deliberately high: this state must be obeyed.
            return RegimeState(
                regime=Regime.ABNORMAL,
                confidence=_clamp(0.75 + 0.1 * len(abnormal)),
                reasons=abnormal,
            )

        if f.bars_available < MIN_BARS_FOR_REGIME or f.atr_bps <= 0:
            return RegimeState(
                regime=Regime.UNKNOWN,
                confidence=0.0,
                reasons=[f"warming up ({f.bars_available} bars)"],
            )

        if self._is_breakout(f):
            conf = _clamp(
                0.45
                + 0.3 * float(f.extras.get("compression_prev", 0.0))
                + 0.15 * min(2.0, f.volume_ratio / BREAKOUT_VOLUME_RATIO)
            )
            return RegimeState(
                regime=Regime.BREAKOUT,
                confidence=conf,
                reasons=[
                    f"compression {float(f.extras.get('compression_prev', 0.0)):.2f} "
                    "then expansion",
                    f"volume x{f.volume_ratio:.1f}",
                ],
            )

        if f.vol_percentile < LOW_VOL_PCT:
            conf = _clamp(0.5 + 0.5 * (LOW_VOL_PCT - f.vol_percentile) / LOW_VOL_PCT)
            return RegimeState(
                regime=Regime.LOW_VOLATILITY,
                confidence=conf,
                reasons=[
                    f"vol %ile {f.vol_percentile:.2f} < {LOW_VOL_PCT}",
                    f"BB width {f.bb_width_bps:.0f} bps",
                ],
            )

        if f.vol_percentile > HIGH_VOL_PCT:
            conf = _clamp(
                0.5 + 0.5 * (f.vol_percentile - HIGH_VOL_PCT) / (1.0 - HIGH_VOL_PCT)
            )
            return RegimeState(
                regime=Regime.HIGH_VOLATILITY,
                confidence=conf,
                reasons=[
                    f"vol %ile {f.vol_percentile:.2f} > {HIGH_VOL_PCT}",
                    f"ATR {f.atr_bps:.0f} bps",
                ],
            )

        aligned = self._ema_aligned(f)
        if f.trend_strength > TREND_STRENGTH_MIN and aligned != 0:
            conf = _clamp(
                0.5
                + 0.5 * (f.trend_strength - TREND_STRENGTH_MIN) / (1.0 - TREND_STRENGTH_MIN)
            )
            direction = "up" if aligned > 0 else "down"
            return RegimeState(
                regime=Regime.TRENDING,
                confidence=conf,
                reasons=[
                    f"trend {f.trend_strength:.2f} {direction}",
                    f"EMA slope {f.ema_slope_bps:+.1f} bps/bar",
                ],
            )

        # Ranging is the residual, so state *why* we are confident it is one.
        conf = _clamp(
            0.35
            + 0.35 * (1.0 - f.trend_strength)
            + 0.30 * (1.0 - abs(f.range_pos - 0.5) * 2.0)
        )
        return RegimeState(
            regime=Regime.RANGING,
            confidence=conf,
            reasons=[
                f"trend {f.trend_strength:.2f} weak",
                f"range pos {f.range_pos:.2f}",
            ],
        )
