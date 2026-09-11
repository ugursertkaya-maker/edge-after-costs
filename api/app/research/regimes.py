"""The seven-state regime classifier.

The tournament measures per-regime edge, so the classifier has exactly one job:
partition time into states that are *reachable*, *distinguishable* and *stable
enough to condition on*.  A classifier that answers RANGING 95% of the time is
not a classifier — it is a constant with extra steps, and it makes per-regime
tables meaningless.  ``test_research_regimes.py`` enforces that directly: every
regime must appear, and none may take more than ~70% of samples.

Precedence — first match wins, and the order is the design
----------------------------------------------------------

1. **PANIC** — a violent directional flush: realized vol in the top few percent
   of its own trailing distribution *and* a multi-bar move several standard
   deviations against the recent mean.  First because it is the one state whose
   correct response is "do nothing"; a panic also reads as a breakout and as a
   volatility expansion, and misfiling it as either would invite a strategy to
   trade into it.

2. **BREAKOUT** — price has left the prior N-bar range with volume behind it.
   Ahead of VOLATILITY_EXPANSION for the same reason ``app/regime.py`` puts it
   there: a genuine breakout necessarily shows up as a vol spike too, and the
   breakout reading is the more actionable and more specific of the two.

3. **VOLATILITY_EXPANSION** — vol is high in its own trailing distribution but
   the move has no range break behind it.  This is the "something is happening,
   we do not know what" bucket.

4. **LOW_VOLATILITY** — vol in the bottom of its own distribution.  On this
   venue this is the *majority* physical state (55% of BTC/USD minutes contain
   no trade at all), which is exactly why the threshold is a percentile rather
   than an absolute bps figure: an absolute threshold would label almost
   everything LOW_VOLATILITY and the classifier would collapse.

5. **TRENDING_UP / TRENDING_DOWN** — momentum slope, EMA slope and the
   multi-bar return all agreeing on a direction, with the move large relative
   to the bar-to-bar noise.  Requiring three agreeing measures rather than one
   is what keeps a two-bar wiggle out of the trending bucket.

6. **RANGING** — the residual.  Being last is deliberate: everything that is
   not one of the six specific states is, by definition, chop.

Why percentiles, not absolute thresholds
-----------------------------------------

Every volatility gate above is expressed as a percentile of that symbol's own
trailing distribution.  That makes the partition self-normalising: it works on
BTC/USD at 2.2 bps of 1m ATR and on SOL/USD at 3.7 bps without a per-symbol
table, it survives a regime shift in the venue itself, and it *guarantees*
non-degeneracy — a percentile threshold at 0.15 selects roughly 15% of samples
by construction, whereas an absolute one can select 0% or 100%.

Warm-up returns ``(RANGING, 0.0)``.  Confidence 0.0 is the "no opinion" signal;
the tournament should treat a zero-confidence sample as unusable rather than as
evidence about ranging markets.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass

from ..config import Settings, get_settings
from .features_mtf import MTFFeatures, feature_name

__all__ = [
    "BREAKOUT_MIN_BPS",
    "BREAKOUT_VOL_ACCEL",
    "HIGH_VOL_PCT",
    "LOW_VOL_PCT",
    "PANIC_MOVE_Z",
    "PANIC_VOL_PCT",
    "TREND_MIN_Z",
    "Regime7",
    "Regime7Classifier",
    "regime_distribution",
]


class Regime7(str, enum.Enum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    VOLATILITY_EXPANSION = "VOLATILITY_EXPANSION"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    BREAKOUT = "BREAKOUT"
    PANIC = "PANIC"


# --------------------------------------------------------------------------- #
# Thresholds.  Module constants so the tests, the tournament tables and any
# future dashboard all quote the same numbers.
# --------------------------------------------------------------------------- #

#: PANIC: realized vol this far up its own trailing distribution ...
PANIC_VOL_PCT = 0.90
#: ... and a multi-bar move at least this many sigma against us.
PANIC_MOVE_Z = 2.0

#: BREAKOUT: price at least this far outside the prior range, in bps.  Non-zero
#: so that a one-tick poke over a stale high is not promoted to a regime.
BREAKOUT_MIN_BPS = 1.0
#: ... with volume confirmation: recent volume this many times its baseline ...
BREAKOUT_VOL_ACCEL = 1.3
#: ... or this bar's volume this far up its trailing distribution.
BREAKOUT_VOL_PCT = 0.70

#: VOLATILITY_EXPANSION / LOW_VOLATILITY: percentile cuts of realized vol.
HIGH_VOL_PCT = 0.80
LOW_VOL_PCT = 0.20

#: TRENDING: the multi-bar return must be at least this many bar-sigma.
TREND_MIN_Z = 0.60

#: Bars in the multi-bar return the trend and panic tests use.  Must match
#: ``features_mtf.RET_LOOKBACK`` — the sigma scaling below depends on it.
_RET_BARS = 5
_RET_SIGMA_SCALE = math.sqrt(_RET_BARS)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


@dataclass(frozen=True)
class _Reading:
    """The handful of numbers a classification is actually made from."""

    rv: float
    rvol_pct: float
    atr: float
    ret5: float
    mom_slope: float
    ema_slope: float
    breakout_dist: float
    vol_accel: float
    vol_pct: float
    zscore: float
    move_z: float
    warm: bool


class Regime7Classifier:
    """Stateless: every input comes from one :class:`MTFFeatures`.

    ``interval`` picks which timeframe the classification is read from.  1m is
    the default because it is the finest timeframe that is not dominated by the
    venue's emptiness, and because every strategy family is evaluated against a
    1m clock.
    """

    def __init__(
        self,
        *,
        interval: str = "1m",
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.interval = interval

    # -- reading ----------------------------------------------------------- #

    def _read(self, f: MTFFeatures) -> _Reading:
        iv = self.interval

        def g(base: str, default: float = 0.0) -> float:
            return float(f.get(feature_name(base, iv), default))

        rv = g("rv")
        ret5 = g("ret5")
        # Bar-sigma scaled to the return's own horizon: a 5-bar move is compared
        # against sqrt(5) * the 1-bar stdev, not against the 1-bar stdev.  When
        # rv is zero (a dead-flat window) the move is zero sigma, not infinite.
        move_z = ret5 / (rv * _RET_SIGMA_SCALE) if rv > 0 else 0.0
        atr = g("atr")
        return _Reading(
            rv=rv,
            rvol_pct=g("rvol_pct", 0.5),
            atr=atr,
            ret5=ret5,
            mom_slope=g("mom_slope"),
            ema_slope=g("ema_slope"),
            breakout_dist=g("breakout_dist"),
            vol_accel=g("vol_accel", 1.0),
            vol_pct=g("vol_pct", 0.5),
            zscore=g("zscore"),
            move_z=move_z,
            # Nothing has closed a full volatility window yet: ATR and realized
            # vol are both still exactly zero, which only happens during warm-up
            # or on a perfectly dead series.  Either way there is no opinion to
            # have.
            warm=(atr <= 0.0 and rv <= 0.0),
        )

    # -- public API -------------------------------------------------------- #

    def classify(self, f: MTFFeatures) -> tuple[Regime7, float]:
        """``(regime, confidence)``.  Confidence 0.0 means "no opinion"."""
        r = self._read(f)
        if r.warm:
            return Regime7.RANGING, 0.0

        # 1. PANIC ---------------------------------------------------------- #
        if r.rvol_pct >= PANIC_VOL_PCT and r.move_z <= -PANIC_MOVE_Z:
            conf = _clamp(
                0.60
                + 0.20 * (r.rvol_pct - PANIC_VOL_PCT) / max(1e-9, 1.0 - PANIC_VOL_PCT)
                + 0.20 * min(1.0, (abs(r.move_z) - PANIC_MOVE_Z) / PANIC_MOVE_Z)
            )
            return Regime7.PANIC, conf

        # 2. BREAKOUT -------------------------------------------------------- #
        confirmed = r.vol_accel >= BREAKOUT_VOL_ACCEL or r.vol_pct >= BREAKOUT_VOL_PCT
        if abs(r.breakout_dist) >= BREAKOUT_MIN_BPS and confirmed:
            extent = abs(r.breakout_dist) / max(r.atr, BREAKOUT_MIN_BPS)
            conf = _clamp(0.45 + 0.30 * min(1.0, extent) + 0.25 * min(1.0, r.vol_accel / 2.0))
            return Regime7.BREAKOUT, conf

        # 3. VOLATILITY_EXPANSION -------------------------------------------- #
        if r.rvol_pct >= HIGH_VOL_PCT:
            conf = _clamp(
                0.45 + 0.55 * (r.rvol_pct - HIGH_VOL_PCT) / max(1e-9, 1.0 - HIGH_VOL_PCT)
            )
            return Regime7.VOLATILITY_EXPANSION, conf

        # 4. LOW_VOLATILITY --------------------------------------------------- #
        if r.rvol_pct <= LOW_VOL_PCT:
            conf = _clamp(0.45 + 0.55 * (LOW_VOL_PCT - r.rvol_pct) / max(1e-9, LOW_VOL_PCT))
            return Regime7.LOW_VOLATILITY, conf

        # 5. TRENDING_UP / TRENDING_DOWN -------------------------------------- #
        up = r.mom_slope > 0 and r.ema_slope > 0 and r.ret5 > 0
        down = r.mom_slope < 0 and r.ema_slope < 0 and r.ret5 < 0
        if (up or down) and abs(r.move_z) >= TREND_MIN_Z:
            conf = _clamp(
                0.40
                + 0.35 * min(1.0, (abs(r.move_z) - TREND_MIN_Z) / max(1e-9, TREND_MIN_Z))
                + 0.25 * min(1.0, abs(r.mom_slope) / max(r.atr, 1e-9))
            )
            return (Regime7.TRENDING_UP if up else Regime7.TRENDING_DOWN), conf

        # 6. RANGING (residual) ------------------------------------------------ #
        # Say *why* we are confident it is chop: little directional extension
        # and vol sitting in the middle of its own distribution.
        conf = _clamp(
            0.35
            + 0.35 * (1.0 - min(1.0, abs(r.move_z) / max(1e-9, TREND_MIN_Z)))
            + 0.30 * (1.0 - min(1.0, abs(r.zscore) / 2.0))
        )
        return Regime7.RANGING, conf


def regime_distribution(
    samples: "list[tuple[Regime7, float]] | list[Regime7]",
) -> dict[Regime7, float]:
    """Share of each of the seven regimes in ``samples``.  Always all 7 keys.

    Reporting a regime that never occurred as ``0.0`` rather than omitting it is
    the point: a missing key hides a dead branch, and a dead branch means the
    tournament can never measure edge in that state.
    """
    counts = {r: 0 for r in Regime7}
    total = 0
    for s in samples:
        regime = s[0] if isinstance(s, tuple) else s
        counts[regime] += 1
        total += 1
    if total == 0:
        return {r: 0.0 for r in Regime7}
    return {r: counts[r] / total for r in Regime7}
