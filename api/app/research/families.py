"""Ten independent, parameterised strategy families for the Phase 3 tournament.

A family is a *hypothesis*, not a strategy: "short-term acceleration persists",
"a compressed range breaks decisively", "an over-extended run snaps back".  The
tournament's job is to find out which of those, if any, survives costs
out-of-sample on this venue.  Ours is only to state each hypothesis precisely,
in as few parameters as will express it honestly.

What a family may see
---------------------
Exactly one thing: an :class:`~app.research.features_mtf.MTFFeatures` block.
No fees, no capital, no positions, no risk budget, no exchange.  A family that
peeked at any of those would silently duplicate logic that lives in the ranker,
the risk engine and the replay, and its backtest would stop meaning anything.

It returns a :class:`ResearchSignal` — direction plus graded conviction — or
``None``.  It never sizes, never prices a stop, never decides to trade.

Design rules the tests enforce
------------------------------
* **``score`` is graded, in (0, 1].**  A family that answers ``1.0`` or nothing
  gives the tournament nothing to threshold on, and the whole "10 excellent
  trades may beat 150 mediocre ones" question becomes untestable.  Every score
  is a weighted sum of clipped ramps whose lower edge is the family's own
  entry threshold, so conviction is always measured *relative to the bar the
  parameters set* rather than against a constant.
* **``allowed_regimes`` is ``None`` for all ten.**  Which family works in which
  regime is precisely what the tournament measures; hardcoding it here would be
  answering the question with an assumption.  The attribute exists so a family
  that is *definitionally* inapplicable somewhere can say so — none of these
  ten is, so none of them does.
* **Small, defensible grids.**  Three to five values per parameter, at most
  three parameters per family, 444 combinations across all ten.  The reasoning
  for every range is in a comment beside it.  A large grid does not find edge,
  it finds noise: at 444 arms the best one by luck alone is already worth about
  three sigma, and that is *with* a grid this small.
* **Both sides.**  Every family is symmetric and emits LONG and SHORT, and the
  tests assert that a mirrored setup produces a mirrored signal of identical
  conviction.  Spot is long-only and the risk layer gates that — but the short
  statistics are the cheapest available test of whether a signal is real or is
  just a long-only proxy for drift.
* **One parameter is the horizon.**  Eight of the ten take ``interval`` as a
  parameter, because both prior reports concluded the same thing: *the horizon,
  not the signal, is what is wrong* (``docs/V2_REPORT.md``, result 2 — losses
  shrink monotonically as holding time grows).  Making the timeframe a search
  dimension rather than a constant is the single most valuable thing this grid
  can spend its combinations on.

The venue, which is why several of these are built to refuse
------------------------------------------------------------
Binance.US BTC/USD: **55% of 1-minute bars contain zero trades**, ~$753 of
volume per minute, 1m ATR ≈ **2.2 bps**, a typical 60-minute move ≈ **17 bps**
(``docs/ECONOMICS.md``).  Three consequences are wired in:

1. Every size threshold is in **units of that interval's own ATR**, and
   multi-bar moves are compared against ``atr * sqrt(bars)``.  That is the same
   diffusion scaling that turns 2.2 bps at one minute into 17 bps at sixty, so
   a "2 ATR" threshold means the same thing on a 1m and a 15m view.
2. The tape families (E ``volume_impulse``, F ``orderflow_imbalance``) gate on
   ``trades_*`` and **return ``None`` when the bar had no prints**.  They never
   degrade into "always fires" on an empty tape.  ``aggr_imb`` is ``0.0`` both
   when flow is balanced and when there is no flow at all, so without that gate
   a threshold on ``|imbalance|`` would be reading silence as information.
3. ``features_mtf`` fills unmeasurable statistics with documented *neutral*
   values rather than NaN — ``rvol_pct`` and ``vol_pct`` 0.5, ``vol_accel``
   1.0, ``rsi`` 50.  Any gate here that could be satisfied by a neutral reading
   would fire on cold start, so none of them can be: B's compression ceiling is
   never above 0.45 and H's expansion floor is never below 0.6, both strictly
   on the informative side of the 0.5 neutral, and E's volume floor is strictly
   above the neutral 1.0.  This is deliberate and the tests pin it.

Feature vocabulary
------------------
Names come from ``features_mtf.FEATURE_NAMES``: ``{base}_{interval}`` for the
bar-derived bases (``ret``, ``ret5``, ``atr``, ``zscore``, ``vwap_dist``,
``breakout_dist``, ``rvol_pct``, ``vol_accel``, ``trades``, ``aggr_imb``,
``ema_slope``, ``accel``, ``rsi``), unsuffixed for the book.  A family reads a
name through :func:`_num`, which treats *absent* and *non-finite* as "no
reading" and returns ``None`` — so a family asked about a timeframe the engine
was not configured for stays silent instead of assuming a zero.
``REQUIRED_FEATURES`` lists every base/interval pair any family may read, and
``test_research_families.py`` cross-checks it against ``FEATURE_NAMES``.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterator, Mapping, Sequence

from ..types import Side

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime dependency
    from .features_mtf import MTFFeatures
    from .regimes import Regime7


# --------------------------------------------------------------------------- #
# Contracts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ParamSpace:
    """A family's search grid: parameter name -> candidate values."""

    name: str
    grid: dict[str, tuple]

    def combinations(self) -> Iterator[dict]:
        """Every point in the grid, in a deterministic order."""
        keys = list(self.grid.keys())
        for combo in itertools.product(*(self.grid[k] for k in keys)):
            yield dict(zip(keys, combo))

    def size(self) -> int:
        n = 1
        for values in self.grid.values():
            n *= len(values)
        return n

    def defaults(self) -> dict:
        """First value of every parameter — the fallback for a partial dict."""
        return {k: v[0] for k, v in self.grid.items()}


@dataclass
class ResearchSignal:
    """One family's opinion at one instant.  Direction and conviction only."""

    family: str
    symbol: str
    side: Side
    score: float
    ts_ms: int
    reasons: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Constants and numeric helpers
# --------------------------------------------------------------------------- #

_NAN = float("nan")

#: The timeframes every family is allowed to ask for.  Matches
#: ``features_mtf.DEFAULT_INTERVALS``: the 5s/15s/30s views are opt-in there
#: because they are expensive, and on a venue that leaves 55% of *minutes*
#: empty a five-second bar is almost always a bar about nothing.  A test pins
#: this tuple against the engine's default so the two cannot drift apart.
INTERVALS: tuple[str, ...] = ("1m", "5m", "15m")

#: ``ret5`` spans this many bars (``features_mtf.RET_LOOKBACK``).
RET5_BARS = 5

#: The next timeframe up, used to turn two aggressor readings into an
#: acceleration.  ``aggr_imb_1m`` covers five 1m bars and ``aggr_imb_5m``
#: covers five 5m bars, so their difference is "flow over the last five
#: minutes against flow over the last twenty-five".
SLOWER_INTERVAL: dict[str, str] = {"1m": "5m", "5m": "15m"}

#: EMA advancing this fraction of an ATR per bar counts as a full-strength
#: trend.  Chosen, not fitted: it is the rate at which a trend outruns its own
#: noise within a handful of bars.
TREND_SLOPE_ATR = 0.30
#: RSI this far from 50 counts as full-strength.  75/25 is the textbook edge.
TREND_RSI_SPAN = 25.0


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if not math.isfinite(x):
        return lo
    return lo if x < lo else hi if x > hi else x


def scale(x: float, lo: float, hi: float) -> float:
    """Map ``x`` from [lo, hi] onto [0, 1], clipped.  0 when the range is bad."""
    if not (math.isfinite(x) and math.isfinite(lo) and math.isfinite(hi)):
        return 0.0
    if hi <= lo:
        return 0.0
    return clamp((x - lo) / (hi - lo))


def _sign(x: float) -> int:
    return 1 if x > 0 else -1 if x < 0 else 0


def _num(f: "MTFFeatures", name: str) -> float | None:
    """A feature's value, or ``None`` when it is absent or non-finite.

    Missing is deliberately *not* zero.  Zero is a reading ("no momentum");
    missing means the engine was never configured for that timeframe, and a
    family that cannot read its own input must stay silent rather than assume
    a convenient default.
    """
    value = f.get(name, _NAN)
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _feat(f: "MTFFeatures", base: str, interval: str) -> float | None:
    return _num(f, f"{base}_{interval}")


def _opt(f: "MTFFeatures", base: str, interval: str, default: float = 0.0) -> float:
    """A *bonus* input, which must never be able to block a signal."""
    v = _feat(f, base, interval)
    return default if v is None else v


def _expected_move(atr_bps: float, bars: float) -> float:
    """Diffusion-scaled typical move over ``bars`` bars, in bps.

    2.2 bps at one minute becomes 2.2*sqrt(60) = 17 bps at sixty, which is what
    this venue actually does (``docs/V2_REPORT.md``), so the square-root law is
    the measured yardstick here rather than a convenient one.
    """
    if atr_bps <= 0 or bars <= 0:
        return 0.0
    return atr_bps * math.sqrt(bars)


def _trend_strength(f: "MTFFeatures", interval: str, atr: float) -> float:
    """A 0..1 trend reading built from the two features that carry one.

    ``features_mtf`` publishes no ``trend_strength``, so it is derived here
    from the EMA slope (in ATR units) and RSI's distance from 50, averaged.
    Both degrade to 0 at their own neutral values (slope 0, RSI 50), so an
    unready feature block reads as "no trend" rather than as a trend.
    """
    slope = _opt(f, "ema_slope", interval)
    rsi = _opt(f, "rsi", interval, 50.0)
    slope_term = clamp(abs(slope) / (TREND_SLOPE_ATR * atr)) if atr > 0 else 0.0
    rsi_term = clamp(abs(rsi - 50.0) / TREND_RSI_SPAN)
    return clamp(0.5 * slope_term + 0.5 * rsi_term)


def _flow_accel(f: "MTFFeatures", interval: str) -> float:
    """Fast aggressor imbalance minus the slower one: is the flow building?"""
    fast = _feat(f, "aggr_imb", interval)
    if fast is None:
        return 0.0
    slower = SLOWER_INTERVAL.get(interval)
    slow = _feat(f, "aggr_imb", slower) if slower else None
    return fast - (0.0 if slow is None else slow)


#: Every base/interval pair any family may read.  Cross-checked against
#: ``features_mtf.FEATURE_NAMES`` by the tests, so a rename over there fails
#: here loudly instead of turning ten families silent.
REQUIRED_FEATURES: dict[str, tuple[str, ...]] = {
    "atr": INTERVALS,
    "ret": INTERVALS,
    "ret5": INTERVALS,
    "accel": INTERVALS,
    "ema_slope": INTERVALS,
    "rsi": INTERVALS,
    "zscore": INTERVALS,
    "vwap_dist": INTERVALS,
    "breakout_dist": INTERVALS,
    "rvol_pct": INTERVALS,
    "vol_accel": INTERVALS,
    "trades": INTERVALS,
    "aggr_imb": INTERVALS,
}


# --------------------------------------------------------------------------- #
# Base class
# --------------------------------------------------------------------------- #


class StrategyFamily:
    """Base for the ten.  Owns readiness, parameter defaults and emission."""

    name: str = "base"
    param_space: ParamSpace
    #: ``None`` = every regime.  Decided by the tournament, not by us.
    allowed_regimes: "frozenset[Regime7] | None" = None

    # -- public API -------------------------------------------------------- #

    def signal(
        self, f: "MTFFeatures", params: Mapping[str, Any] | None = None
    ) -> ResearchSignal | None:
        if not self._ready(f):
            return None
        return self._signal(f, self._merged(params))

    # -- subclass hook ----------------------------------------------------- #

    def _signal(self, f: "MTFFeatures", p: dict) -> ResearchSignal | None:
        raise NotImplementedError

    # -- shared plumbing --------------------------------------------------- #

    def _merged(self, params: Mapping[str, Any] | None) -> dict:
        merged = self.param_space.defaults()
        if params:
            merged.update({k: v for k, v in params.items() if k in merged})
        return merged

    @staticmethod
    def _ready(f: "MTFFeatures") -> bool:
        """No price, no opinion.  Per-interval readiness is the ATR check."""
        price = getattr(f, "price", 0.0)
        try:
            p = float(price)
        except (TypeError, ValueError):
            return False
        return math.isfinite(p) and p > 0.0

    @staticmethod
    def _atr(f: "MTFFeatures", interval: str) -> float | None:
        """That timeframe's ATR in bps, or ``None`` if it is not measurable yet.

        This is the cold-start gate: ``atr`` stays 0.0 until Wilder's fourteen
        bars exist, and every threshold in this module is denominated in it.
        """
        atr = _feat(f, "atr", interval)
        if atr is None or atr <= 0.0:
            return None
        return atr

    def _emit(
        self,
        f: "MTFFeatures",
        direction: int,
        score: float,
        reasons: list[str],
    ) -> ResearchSignal | None:
        """Clamp, sanity-check and build.  Never emits NaN, inf or zero."""
        if direction == 0 or not math.isfinite(score):
            return None
        score = clamp(score)
        if score <= 0.0:
            return None
        return ResearchSignal(
            family=self.name,
            symbol=str(getattr(f, "symbol", "")),
            side=Side.LONG if direction > 0 else Side.SHORT,
            score=score,
            ts_ms=int(getattr(f, "ts_ms", 0)),
            reasons=reasons,
        )


# --------------------------------------------------------------------------- #
# A. momentum_continuation
# --------------------------------------------------------------------------- #


class MomentumContinuation(StrategyFamily):
    """A: a leg that is *accelerating* keeps going for a little longer.

    Three things must all hold: the last bar is large against its own ATR, the
    five-bar leg agrees with it, and the momentum slope is still steepening.
    The acceleration test is what separates this from "buy what just went up" —
    a move that has already spent itself has a positive return and a negative
    ``accel``, and that is the shape of an exhaustion print.
    """

    name = "momentum_continuation"
    param_space = ParamSpace(
        name="momentum_continuation",
        grid={
            # The horizon, which both prior reports named as the lever.
            "interval": INTERVALS,
            # Impulse size in that interval's own ATR.  0.5 is barely a bar,
            # 2.0 is a genuine outlier; past that the sample empties out on a
            # venue whose 1m ATR is 2.2 bps.
            "min_impulse_atr": (0.5, 1.0, 1.5, 2.0),
            # Slope change per bar, in ATR.  0.0 means "merely not
            # decelerating", which is the loosest honest version of the claim.
            "min_accel_atr": (0.0, 0.1, 0.25, 0.5),
        },
    )

    def _signal(self, f: "MTFFeatures", p: dict) -> ResearchSignal | None:
        interval = str(p["interval"])
        atr = self._atr(f, interval)
        if atr is None:
            return None

        ret = _feat(f, "ret", interval)
        leg = _feat(f, "ret5", interval)
        if ret is None or leg is None:
            return None

        strength = abs(ret) / atr
        floor = float(p["min_impulse_atr"])
        if strength < floor:
            return None

        direction = _sign(ret)
        if direction * leg <= 0.0:               # the five-bar leg must agree
            return None

        accel = _opt(f, "accel", interval) * direction / atr
        accel_floor = float(p["min_accel_atr"])
        if accel < accel_floor:
            return None

        trend = _trend_strength(f, interval, atr)
        score = (
            0.45 * scale(strength, floor, floor + 2.5)
            + 0.30 * scale(accel, accel_floor, accel_floor + 1.0)
            + 0.25 * trend
        )
        return self._emit(
            f,
            direction,
            score,
            [
                f"{interval} bar {ret:+.1f} bps = {strength:.1f} ATR",
                f"slope steepening {accel:+.2f} ATR/bar",
                f"{RET5_BARS}-bar leg {leg:+.1f} bps agrees",
            ],
        )


# --------------------------------------------------------------------------- #
# B. breakout
# --------------------------------------------------------------------------- #


class Breakout(StrategyFamily):
    """B: a quiet range, then a decisive exit from it.

    ``breakout_dist`` is signed bps *outside* the prior twenty-bar range and is
    exactly 0.0 inside it, so the trigger needs no threshold of its own — only
    a size.  Compression is the setup and the break is the trigger; neither is
    a breakout alone.  A break out of a range that was never quiet is just a
    trend, and a quiet range nobody left is just a quiet range.
    """

    name = "breakout"
    param_space = ParamSpace(
        name="breakout",
        grid={
            "interval": INTERVALS,
            # Compression, as a realized-vol percentile ceiling.  Every value is
            # strictly below the 0.5 that `rvol_pct` reports when it cannot
            # measure yet, so no arm of this grid can fire on a neutral reading.
            "max_rvol_pct": (0.1, 0.2, 0.3, 0.45),
            # How far outside the range, in ATR.  Below ~1 ATR the break is
            # inside the spread-plus-slippage cost of taking it.
            "break_atr": (0.5, 1.0, 1.5, 2.5),
        },
    )

    def _signal(self, f: "MTFFeatures", p: dict) -> ResearchSignal | None:
        interval = str(p["interval"])
        atr = self._atr(f, interval)
        if atr is None:
            return None

        dist = _feat(f, "breakout_dist", interval)
        rvol = _feat(f, "rvol_pct", interval)
        if dist is None or rvol is None or dist == 0.0:
            return None

        ceiling = float(p["max_rvol_pct"])
        if rvol > ceiling:
            return None

        size = abs(dist) / atr
        floor = float(p["break_atr"])
        if size < floor:
            return None

        direction = _sign(dist)
        flow = clamp(direction * _opt(f, "aggr_imb", interval))
        score = (
            0.45 * scale(size, floor, floor + 2.0)
            + 0.35 * scale(ceiling - rvol, 0.0, ceiling)
            + 0.20 * flow
        )
        return self._emit(
            f,
            direction,
            score,
            [
                f"{interval} broke {dist:+.1f} bps = {size:.1f} ATR out of range",
                f"prior vol percentile {rvol:.2f} <= {ceiling:.2f}",
            ],
        )


# --------------------------------------------------------------------------- #
# C. trend_pullback
# --------------------------------------------------------------------------- #

#: A retrace deeper than this multiple of the entry threshold is a reversal,
#: not a pullback.  Fixed rather than tuned: letting both ends of the band
#: float turns one hypothesis into a two-parameter curve fit.
PULLBACK_MAX_MULT = 3.0


class TrendPullback(StrategyFamily):
    """C: an established trend, a controlled retrace, continuation.

    The hard part is telling a pullback from a top, so the retrace is bounded
    *above* as well as below, and the five-bar leg must still point the way the
    trend does while the last bar goes the other way.
    """

    name = "trend_pullback"
    param_space = ParamSpace(
        name="trend_pullback",
        grid={
            "interval": INTERVALS,
            # Derived trend strength (EMA slope in ATR, blended with RSI's
            # distance from 50).  0.3 admits weak drift, 0.75 demands a trend
            # that is obvious.
            "trend_min": (0.3, 0.45, 0.6, 0.75),
            # Retrace depth of the last bar, in ATR.  The accepted band is
            # [x, 3x]: shallower is not a pullback, deeper is a reversal.
            "pullback_atr": (0.5, 1.0, 1.5, 2.0),
        },
    )

    def _signal(self, f: "MTFFeatures", p: dict) -> ResearchSignal | None:
        interval = str(p["interval"])
        atr = self._atr(f, interval)
        if atr is None:
            return None

        slope = _feat(f, "ema_slope", interval)
        ret = _feat(f, "ret", interval)
        leg = _feat(f, "ret5", interval)
        if slope is None or ret is None or leg is None:
            return None

        direction = _sign(slope)
        if direction == 0:
            return None

        trend = _trend_strength(f, interval, atr)
        trend_min = float(p["trend_min"])
        if trend < trend_min:
            return None

        if direction * leg <= 0.0:               # the trend leg must be intact
            return None

        depth = -direction * ret / atr           # > 0 means it retraced
        floor = float(p["pullback_atr"])
        if depth < floor or depth > PULLBACK_MAX_MULT * floor:
            return None

        # The sweet spot sits inside the band; its edges are "hasn't really
        # pulled back" and "this may not be a pullback at all".
        span = max(PULLBACK_MAX_MULT * floor - floor, 1e-9)
        centre = floor + 0.45 * span
        depth_quality = clamp(1.0 - abs(depth - centre) / (0.55 * span))

        leg_strength = abs(leg) / _expected_move(atr, RET5_BARS)
        score = (
            0.35 * scale(trend, trend_min, 1.0)
            + 0.40 * depth_quality
            + 0.25 * scale(leg_strength, 0.0, 2.0)
        )
        return self._emit(
            f,
            direction,
            score,
            [
                f"{interval} trend {'up' if direction > 0 else 'down'} ({trend:.2f})",
                f"retrace {depth:.1f} ATR inside [{floor:.1f}, {PULLBACK_MAX_MULT * floor:.1f}]",
                f"{RET5_BARS}-bar leg {leg:+.1f} bps intact",
            ],
        )


# --------------------------------------------------------------------------- #
# D. mean_reversion
# --------------------------------------------------------------------------- #


class MeanReversion(StrategyFamily):
    """D: an extension beyond a trailing band reverts toward the mean.

    The statistical version: distance in standard deviations of the last twenty
    closes, so it adapts to a 2.2 bps venue without a constant in it.  G
    (``vwap_reversion``) asks a related but different question — distance from
    a *volume-weighted* fair price, in ATR — and whether those are the same
    trade is for the tournament to say, not for us to assume by merging them.
    """

    name = "mean_reversion"
    param_space = ParamSpace(
        name="mean_reversion",
        grid={
            "interval": INTERVALS,
            # Extension in sigma.  1.5 fires often; 3.0 is rare by construction
            # and rarer still on a fat-tailed series than Gaussian arithmetic
            # suggests.
            "extension_sigma": (1.5, 2.0, 2.5, 3.0),
            # Do not fade a strong trend.  Whether the strict version is worth
            # the lost sample is exactly what a tournament is for.
            "max_trend": (0.4, 0.6, 0.8),
        },
    )

    def _signal(self, f: "MTFFeatures", p: dict) -> ResearchSignal | None:
        interval = str(p["interval"])
        atr = self._atr(f, interval)
        if atr is None:
            return None

        z = _feat(f, "zscore", interval)
        if z is None:
            return None
        threshold = float(p["extension_sigma"])
        if abs(z) < threshold:
            return None

        max_trend = float(p["max_trend"])
        trend = _trend_strength(f, interval, atr)
        if trend > max_trend:
            return None

        direction = -_sign(z)                    # fade the stretch
        if direction == 0:
            return None

        # Flow turning back toward us says the push that caused the stretch is
        # spent; flow still pressing against us says it is not.
        accel = clamp(direction * _flow_accel(f, interval))
        score = (
            0.45 * scale(abs(z), threshold, threshold + 2.0)
            + 0.30 * (1.0 - clamp(trend / max_trend))
            + 0.25 * accel
        )
        return self._emit(
            f,
            direction,
            score,
            [
                f"{abs(z):.1f} sigma from the {interval} mean",
                f"trend {trend:.2f} <= {max_trend:.2f}",
                f"flow turning back {accel:.2f}",
            ],
        )


# --------------------------------------------------------------------------- #
# E. volume_impulse
# --------------------------------------------------------------------------- #

#: E and J are pinned to the minute: it is the timeframe the venue's emptiness
#: was actually measured on, and ``vol_accel``'s baseline is twenty bars, so a
#: 15m view would be ranking this bar against five hours of a different day.
IMPULSE_INTERVAL = "1m"


class VolumeImpulse(StrategyFamily):
    """E: abnormal volume *with* a price response.

    Volume alone is not a signal; volume that moved the price is.  The trade
    count floor is what keeps this honest here — V2 reported volume ratios of
    1291x because the baseline window is mostly zeros, so ``vol_accel`` is
    already capped and neutral-guarded upstream, and this family additionally
    refuses to look at a bar that had almost no prints in it.
    """

    name = "volume_impulse"
    param_space = ParamSpace(
        name="volume_impulse",
        grid={
            # Prints in the minute.  The venue averages 2.19, so 3 is "anything
            # at all" and 20 is a burst by local standards.  This parameter
            # doubles as the measurement of where the tape stops existing.
            "min_trades": (3, 5, 10, 20),
            # Volume acceleration (fast 5 bars / trailing 20, capped at 20x).
            # Every value is strictly above the neutral 1.0, so an unmeasurable
            # baseline can never satisfy this gate.
            "min_vol_accel": (1.5, 2.5, 4.0, 8.0),
            # The accompanying move, in ATR.
            "response_atr": (0.5, 1.0, 1.5),
        },
    )

    def _signal(self, f: "MTFFeatures", p: dict) -> ResearchSignal | None:
        interval = IMPULSE_INTERVAL
        atr = self._atr(f, interval)
        if atr is None:
            return None

        trades = _feat(f, "trades", interval)
        if trades is None:                       # no tape reading at all
            return None
        min_trades = float(p["min_trades"])
        if trades < min_trades:
            return None

        accel = _feat(f, "vol_accel", interval)
        if accel is None:
            return None
        min_accel = float(p["min_vol_accel"])
        if accel < min_accel:
            return None

        ret = _feat(f, "ret", interval)
        if ret is None:
            return None
        move = abs(ret) / atr
        floor = float(p["response_atr"])
        if move < floor:
            return None

        direction = _sign(ret)
        score = (
            0.40 * scale(accel, min_accel, min_accel + 4.0)
            + 0.35 * scale(move, floor, floor + 2.0)
            + 0.25 * scale(trades, min_trades, min_trades * 4.0)
        )
        return self._emit(
            f,
            direction,
            score,
            [
                f"volume {accel:.1f}x baseline on {trades:.0f} prints",
                f"price response {ret:+.1f} bps = {move:.1f} ATR",
            ],
        )


# --------------------------------------------------------------------------- #
# F. orderflow_imbalance
# --------------------------------------------------------------------------- #


class OrderflowImbalance(StrategyFamily):
    """F: aggressors are one-sided, so the next prints are too.

    This is the family most likely to have no data here.  ``aggr_imb`` reads
    0.0 both for balanced flow and for *no* flow, so a bare threshold on it
    would be mistaking silence for information; the trade-count floor is
    applied to the most recent bar, which is the strictest available reading of
    "is the tape alive right now".  If this family is silent for the whole
    sample, that is the finding — $753 of volume a minute is a handful of
    prints, and an imbalance computed over three trades is not an imbalance.
    """

    name = "orderflow_imbalance"
    param_space = ParamSpace(
        name="orderflow_imbalance",
        grid={
            "interval": INTERVALS,
            # Aggressor notional imbalance over the last five bars, in [-1, 1].
            # 0.25 is a tilt, 0.7 is near one-sided.
            "imb_min": (0.25, 0.4, 0.55, 0.7),
            # Prints in the most recent bar of that interval.
            "min_trades": (5, 10, 20, 40),
        },
    )

    def _signal(self, f: "MTFFeatures", p: dict) -> ResearchSignal | None:
        interval = str(p["interval"])
        if self._atr(f, interval) is None:
            return None

        trades = _feat(f, "trades", interval)
        if trades is None:
            return None
        min_trades = float(p["min_trades"])
        if trades < min_trades:
            return None

        imb = _feat(f, "aggr_imb", interval)
        if imb is None:
            return None
        imb_min = float(p["imb_min"])
        if abs(imb) < imb_min:
            return None

        direction = _sign(imb)
        accel = clamp(direction * _flow_accel(f, interval))
        score = (
            0.45 * scale(abs(imb), imb_min, 1.0)
            + 0.30 * scale(trades, min_trades, min_trades * 4.0)
            + 0.25 * accel
        )
        return self._emit(
            f,
            direction,
            score,
            [
                f"{interval} aggressor imbalance {imb:+.2f}",
                f"{trades:.0f} prints in the last bar",
                f"flow building {accel:.2f}",
            ],
        )


# --------------------------------------------------------------------------- #
# G. vwap_reversion
# --------------------------------------------------------------------------- #


class VwapReversion(StrategyFamily):
    """G: distance from the volume-weighted fair price closes.

    The VWAP window is sixty bars, so the 1m view is roughly an hour of fair
    value — and a typical hour on this venue moves 17 bps, about 8 ATR.  That
    is why the distance thresholds run out to 8: at 2 ATR this fires on noise,
    and at 8 it is asking for a full hour's excursion in one direction.  The
    trend gate is what stops it being a knife-catcher: price far from VWAP in a
    strong trend is not stretched, it is leading.
    """

    name = "vwap_reversion"
    param_space = ParamSpace(
        name="vwap_reversion",
        grid={
            "interval": INTERVALS,
            # Distance from VWAP in ATR.
            "dist_atr": (2.0, 4.0, 6.0, 8.0),
            "max_trend": (0.25, 0.5, 0.75),
        },
    )

    def _signal(self, f: "MTFFeatures", p: dict) -> ResearchSignal | None:
        interval = str(p["interval"])
        atr = self._atr(f, interval)
        if atr is None:
            return None

        dist = _feat(f, "vwap_dist", interval)
        if dist is None:
            return None
        distance = abs(dist) / atr
        floor = float(p["dist_atr"])
        if distance < floor:
            return None

        max_trend = float(p["max_trend"])
        trend = _trend_strength(f, interval, atr)
        if trend > max_trend:
            return None

        direction = -_sign(dist)
        if direction == 0:
            return None

        accel = clamp(direction * _flow_accel(f, interval))
        score = (
            0.50 * scale(distance, floor, floor + 4.0)
            + 0.30 * (1.0 - clamp(trend / max_trend))
            + 0.20 * accel
        )
        return self._emit(
            f,
            direction,
            score,
            [
                f"{dist:+.1f} bps from the {interval} VWAP = {distance:.1f} ATR",
                f"trend {trend:.2f} <= {max_trend:.2f}",
            ],
        )


# --------------------------------------------------------------------------- #
# H. volatility_expansion
# --------------------------------------------------------------------------- #


class VolatilityExpansion(StrategyFamily):
    """H: the regime change *is* the signal.

    Not "volatility is high" — that is a state — but "volatility has just moved
    to the top of its own trailing distribution", with a direction taken from
    the bar that did it.  ``rvol_pct`` ranks realized vol against 240 of its own
    past observations, which is the measurement this family needs and the one
    thing a 1m ATR cannot give on its own.  Every floor here is above the 0.5
    that ``rvol_pct`` reports when it cannot rank yet, so cold start cannot
    look like an expansion.

    Symmetric, and honestly so: an expansion can resolve either way, and
    whether the direction of the expanding bar carries information is precisely
    the hypothesis under test.
    """

    name = "volatility_expansion"
    param_space = ParamSpace(
        name="volatility_expansion",
        grid={
            "interval": INTERVALS,
            # Realized-vol percentile floor.  0.6 is "above average", 0.97 is
            # "the most volatile bar in four hours of this timeframe".
            "min_rvol_pct": (0.6, 0.75, 0.9, 0.97),
            # Size of the bar that caused it, in ATR.
            "direction_atr": (0.5, 1.0, 2.0),
        },
    )

    def _signal(self, f: "MTFFeatures", p: dict) -> ResearchSignal | None:
        interval = str(p["interval"])
        atr = self._atr(f, interval)
        if atr is None:
            return None

        rvol = _feat(f, "rvol_pct", interval)
        if rvol is None:
            return None
        floor_pct = float(p["min_rvol_pct"])
        if rvol < floor_pct:
            return None

        ret = _feat(f, "ret", interval)
        if ret is None:
            return None
        move = abs(ret) / atr
        floor = float(p["direction_atr"])
        if move < floor:
            return None

        direction = _sign(ret)
        vol_accel = _opt(f, "vol_accel", interval, 1.0)
        score = (
            0.45 * scale(rvol, floor_pct, 1.0)
            + 0.35 * scale(move, floor, floor + 2.0)
            + 0.20 * scale(vol_accel, 1.0, 4.0)
        )
        return self._emit(
            f,
            direction,
            score,
            [
                f"{interval} realized vol at percentile {rvol:.2f}",
                f"expanding bar {ret:+.1f} bps = {move:.1f} ATR",
            ],
        )


# --------------------------------------------------------------------------- #
# I. short_term_reversal
# --------------------------------------------------------------------------- #


class ShortTermReversal(StrategyFamily):
    """I: an over-extended short run gives some of itself back.

    The direct opposite of A on the same data, which is the point: one of them
    has to be wrong on this venue, and the tournament should decide which
    rather than us picking in advance.  The stall test is what stops this being
    "fade every up-bar" — the five-bar run must have happened *and* the newest
    bar must already be going the other way.
    """

    name = "short_term_reversal"
    param_space = ParamSpace(
        name="short_term_reversal",
        grid={
            "interval": INTERVALS,
            # Extension of the five-bar run, against its diffusion expectation
            # (atr * sqrt(5)).  Deliberately higher than A's thresholds: this is
            # the tail of the same distribution.
            "ext_atr": (1.5, 2.5, 3.5, 5.0),
            # How much the newest bar has given back, in ATR.  0.0 means merely
            # "no longer extending".
            "stall_frac": (0.0, 0.25, 0.5),
        },
    )

    def _signal(self, f: "MTFFeatures", p: dict) -> ResearchSignal | None:
        interval = str(p["interval"])
        atr = self._atr(f, interval)
        if atr is None:
            return None

        leg = _feat(f, "ret5", interval)
        ret = _feat(f, "ret", interval)
        if leg is None or ret is None:
            return None

        expected = _expected_move(atr, RET5_BARS)
        if expected <= 0.0:
            return None
        extension = abs(leg) / expected
        floor = float(p["ext_atr"])
        if extension < floor:
            return None

        move_dir = _sign(leg)
        if move_dir == 0:
            return None
        stall = -move_dir * ret / atr            # > 0 means giving it back
        stall_min = float(p["stall_frac"])
        if stall < stall_min:
            return None

        direction = -move_dir                    # fade the run
        trend = _trend_strength(f, interval, atr)
        score = (
            0.50 * scale(extension, floor, floor + 3.0)
            + 0.30 * scale(stall, stall_min, stall_min + 1.5)
            + 0.20 * (1.0 - trend)
        )
        return self._emit(
            f,
            direction,
            score,
            [
                f"{RET5_BARS}-bar {interval} run {leg:+.1f} bps = {extension:.1f}x expected",
                f"newest bar stalling {stall:.2f} ATR",
            ],
        )


# --------------------------------------------------------------------------- #
# J. hybrid_multifactor
# --------------------------------------------------------------------------- #


class HybridMultifactor(StrategyFamily):
    """J: one explicit linear blend of trend, reversion and flow.

    It reads the same feature block everyone else does and computes its own
    three legs; it does **not** call A-I.  The spec evaluates families
    independently *before* blending, so a hybrid that reached into the others'
    internals would smuggle their parameters into its own grid and make both
    results uninterpretable.

    The blend is a single tilt between two opposed hypotheses plus a flow
    overlay, which keeps it to three parameters and keeps the output readable:
    ``momentum_tilt`` 0.0 is a pure fade, 1.0 a pure chase, and the tournament
    can see where in between — if anywhere — the edge sits.  ``min_conviction``
    is the "10 excellent trades may beat 150 mediocre ones" dial made explicit
    as a parameter, on the one family whose score is a pure, continuous
    conviction rather than a weighted sum of ramps.
    """

    name = "hybrid_multifactor"
    param_space = ParamSpace(
        name="hybrid_multifactor",
        grid={
            # Five values so both pure endpoints and the midpoint are covered.
            "momentum_tilt": (0.0, 0.25, 0.5, 0.75, 1.0),
            "min_conviction": (0.3, 0.45, 0.6, 0.75),
            # How much the tape may tilt the blend.  0.0 keeps the family
            # runnable on a venue whose tape is mostly empty.
            "flow_weight": (0.0, 0.25, 0.5),
        },
    )

    #: Legs saturate at three times their own expectation, so a genuine outlier
    #: maps to a full-strength leg and anything beyond is capped rather than
    #: allowed to dominate the blend.
    SATURATION = 3.0

    def _signal(self, f: "MTFFeatures", p: dict) -> ResearchSignal | None:
        interval = IMPULSE_INTERVAL
        atr = self._atr(f, interval)
        if atr is None:
            return None

        leg = _feat(f, "ret5", interval)
        z = _feat(f, "zscore", interval)
        if leg is None or z is None:
            return None

        expected = _expected_move(atr, RET5_BARS)
        if expected <= 0.0:
            return None
        momentum_leg = clamp(leg / (self.SATURATION * expected), -1.0, 1.0)
        reversion_leg = clamp(-z / self.SATURATION, -1.0, 1.0)

        tilt = float(p["momentum_tilt"])
        flow_weight = float(p["flow_weight"])
        flow_leg = (
            clamp(_opt(f, "aggr_imb", interval), -1.0, 1.0) if flow_weight > 0 else 0.0
        )

        blended = (
            tilt * momentum_leg
            + (1.0 - tilt) * reversion_leg
            + flow_weight * flow_leg
        ) / (1.0 + flow_weight)

        conviction = abs(blended)
        if conviction < float(p["min_conviction"]):
            return None

        return self._emit(
            f,
            _sign(blended),
            conviction,
            [
                f"blend {blended:+.2f} (tilt {tilt:.2f}, flow weight {flow_weight:.2f})",
                f"momentum {momentum_leg:+.2f}, reversion {reversion_leg:+.2f},"
                f" flow {flow_leg:+.2f}",
            ],
        )


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

FAMILIES: tuple[StrategyFamily, ...] = (
    MomentumContinuation(),
    Breakout(),
    TrendPullback(),
    MeanReversion(),
    VolumeImpulse(),
    OrderflowImbalance(),
    VwapReversion(),
    VolatilityExpansion(),
    ShortTermReversal(),
    HybridMultifactor(),
)

FAMILY_BY_NAME: dict[str, StrategyFamily] = {fam.name: fam for fam in FAMILIES}


def total_combinations(families: Sequence[StrategyFamily] = FAMILIES) -> int:
    """Total grid points across ``families`` — the size of the search space.

    Reported by the tests so the number in the write-up is measured rather than
    claimed, and so a grid that quietly grows shows up as a failing assertion
    instead of as a longer run.
    """
    return sum(fam.param_space.size() for fam in families)


def feature_names_used() -> tuple[str, ...]:
    """Every ``{base}_{interval}`` name any family may read, sorted."""
    names: set[str] = set()
    for base, intervals in REQUIRED_FEATURES.items():
        names.update(f"{base}_{iv}" for iv in intervals)
    return tuple(sorted(names))


def validate_feature_names() -> tuple[str, ...]:
    """Raise if any family reads a name ``features_mtf`` does not publish.

    ``MTFFeatures.get(name, default)`` cannot tell a typo from a legitimately
    absent feature, so a renamed key would not crash anything — it would
    quietly turn a family into a generator of neutral readings that still
    produces signals and measures nothing.  That is the worst possible failure
    here, because the tournament would rank the resulting noise.

    So the check is explicit and loud, and the tournament should call it once
    at startup.  It is a function rather than an import-time assertion only so
    that ``families.py`` stays importable without pulling in the dataset and
    settings machinery behind ``features_mtf``.
    """
    from .features_mtf import FEATURE_NAMES  # local: keeps the import optional

    published = frozenset(FEATURE_NAMES)
    unknown = tuple(n for n in feature_names_used() if n not in published)
    if unknown:
        raise ValueError(
            "families.py reads features that features_mtf.py does not publish: "
            + ", ".join(unknown)
        )
    return feature_names_used()
