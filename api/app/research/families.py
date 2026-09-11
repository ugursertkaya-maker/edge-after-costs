"""Ten independent, parameterised strategy families for the Phase 3 tournament.

A family is a *hypothesis*, not a strategy: "short-term acceleration persists",
"a compressed range breaks decisively", "an over-extended minute snaps back".
The tournament's job is to find out which of those, if any, survives costs
out-of-sample on this venue.  Ours is only to state each hypothesis precisely,
in as few parameters as will express it honestly.

What a family may see
---------------------
Exactly one thing: a :class:`~app.research.features_mtf.MTFFeatures` block.
No fees, no capital, no positions, no risk budget, no exchange.  A family that
peeked at any of those would silently duplicate logic that lives in the ranker,
the risk engine and the replay, and its backtest would stop meaning anything.

It returns a :class:`ResearchSignal` — direction plus graded conviction — or
``None``.  It never sizes, never prices a stop, never decides to trade.

Design rules that the tests enforce
-----------------------------------
* **``score`` is graded, in (0, 1].**  A family that answers ``1.0`` or nothing
  gives the tournament nothing to threshold on, and the whole "10 excellent
  trades may beat 150 mediocre ones" question becomes untestable.  Every score
  below is a weighted sum of clipped ramps, and a stronger setup always scores
  strictly higher than a weaker one.
* **``allowed_regimes`` is ``None`` for all ten.**  Which family works in which
  regime is precisely what the tournament measures; hardcoding it here would be
  answering the question with an assumption.  The attribute exists so a family
  that is *definitionally* inapplicable somewhere can say so — none of these
  ten is, so none of them does.
* **Small, defensible grids.**  Three to five values per parameter, at most
  three parameters per family.  The reasoning for every range is in a comment
  next to it.  A large grid does not find edge, it finds noise: with 444 total
  combinations and a few hundred out-of-sample trades per arm, the best arm by
  luck alone is already worth about 3 sigma, and that is with a grid this
  small.
* **Both sides.**  Every family whose logic is symmetric emits LONG and SHORT.
  Spot is long-only and the risk layer gates that — but the short statistics
  are the cheapest available test of whether a signal is real or is just a
  long-only proxy for drift.

The venue, which is the reason several of these are built to refuse
-------------------------------------------------------------------
Binance.US BTC/USD: **55% of 1-minute bars contain zero trades**, ~$753 of
volume per minute, 1m ATR ≈ **2.2 bps**, a typical 60-minute move ≈ **17 bps**
(``docs/ECONOMICS.md``, ``docs/V2_REPORT.md``).  Two consequences are wired in:

1. Sizes are expressed in units of the *diffusion-scaled* expected move,
   ``atr_1m * sqrt(minutes)`` — the same scaling that turns 2.2 bps at one
   minute into 17 bps at sixty.  A "2 ATR" threshold therefore means the same
   thing at 15 seconds and at 15 minutes.
2. The tape-dependent families (E ``volume_impulse``, F ``orderflow_imbalance``,
   and I ``short_term_reversal``, which needs sub-minute prints) **return
   ``None`` when the tape is empty or the feature is absent**.  They never
   degrade to "always fires" on no data.  V2 was bitten by exactly this: a
   ``volume_ratio`` divided by a baseline that is usually zero reported
   readings like 1291x, so every ``volume_ratio > 1.8`` gate fired on the first
   print after any quiet spell — volume *confirmation* was volume *noise*.
   If these families have nothing to work with here, that is a finding to
   report, not a reason to loosen the gate.

Feature naming
--------------
``MTFFeatures.values`` is written by ``features_mtf.py``.  Each logical input
below is resolved through a short alias list (``_num``) so a naming difference
does not silently turn into a dead family: the first name that is present and
finite wins, and if none is, the family returns ``None`` rather than treating a
missing feature as zero.  ``REQUIRED_FEATURES`` lists every name any family may
read; ``test_research_families.py`` cross-checks it against ``FEATURE_NAMES``
as soon as that module exists.
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
# Numeric helpers
# --------------------------------------------------------------------------- #

_NAN = float("nan")

# Diffusion scaling.  2.2 bps at 1m -> 2.2 * sqrt(60) = 17.0 bps at 60m, which
# is the measured 60-minute move on this venue, so the square-root law is the
# right yardstick here rather than a guess.
WINDOW_MINUTES: dict[str, float] = {
    "5s": 1.0 / 12.0,
    "15s": 0.25,
    "30s": 0.5,
    "1m": 1.0,
    "3m": 3.0,
    "5m": 5.0,
    "15m": 15.0,
    "60m": 60.0,
}


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if not math.isfinite(x):
        return lo
    return lo if x < lo else hi if x > hi else x


def scale(x: float, lo: float, hi: float) -> float:
    """Map ``x`` from [lo, hi] onto [0, 1], clipped.  0 when the range is bad."""
    if not math.isfinite(x) or not math.isfinite(lo) or not math.isfinite(hi):
        return 0.0
    if hi <= lo:
        return 0.0
    return clamp((x - lo) / (hi - lo))


def _num(f: "MTFFeatures", names: Sequence[str]) -> float | None:
    """First present, finite feature among ``names``; ``None`` if there is none.

    Missing is deliberately *not* zero.  Zero is a reading ("no flow"); missing
    is the absence of a reading, and a family that cannot read its own input
    must stay silent rather than assume a convenient default.
    """
    for name in names:
        value = f.get(name, _NAN)
        if value is None:
            continue
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            return v
    return None


def _opt(f: "MTFFeatures", names: Sequence[str], default: float = 0.0) -> float:
    """Like :func:`_num` but for a *bonus* input that must never block a signal."""
    v = _num(f, names)
    return default if v is None else v


def _ret_names(window: str) -> tuple[str, ...]:
    return (f"ret_{window}_bps", f"return_{window}_bps", f"mom_{window}_bps")


def _expected_move_bps(atr_1m_bps: float, window: str) -> float:
    """Diffusion-scaled typical move over ``window``, in bps."""
    minutes = WINDOW_MINUTES.get(window)
    if minutes is None or atr_1m_bps <= 0.0:
        return 0.0
    return atr_1m_bps * math.sqrt(minutes)


# Canonical names, with aliases, for everything the ten families read.  Kept in
# one place so a mismatch with features_mtf.py is a one-line fix and so a test
# can diff it against FEATURE_NAMES.
ATR_NAMES = ("atr_1m_bps", "atr_bps")
TREND_NAMES = ("trend_strength",)
EMA_SLOPE_NAMES = ("ema_slope_5m_bps", "ema_slope_1m_bps", "ema_slope_bps")
FLOW_NAMES = ("trade_flow_imbalance", "flow_imbalance", "aggressor_imbalance")
FLOW_ACCEL_NAMES = ("trade_flow_accel", "flow_accel")
TRADE_COUNT_NAMES = ("trade_count_1m", "trades_1m", "n_trades_1m", "trades_per_min")
NOTIONAL_NAMES = ("quote_volume_1m_usd", "quote_volume_1m", "volume_usd_1m")
VOLUME_Z_NAMES = ("volume_z_1m", "volume_zscore_1m", "vol_z_1m")
VOLUME_RATIO_NAMES = ("volume_ratio_1m", "volume_ratio")
VOL_PCT_NAMES = ("vol_percentile", "vol_percentile_1m")
VOL_EXPANSION_NAMES = (
    "vol_expansion_ratio",
    "vol_ratio_fast_slow",
    "vol_expansion",
)
ATR_5M_NAMES = ("atr_5m_bps",)

# A ratio the V2 post-mortem says to distrust: volume_ratio's baseline is zero
# in 55% of minutes here, so the ratio explodes.  It is only ever a fallback,
# and it is capped before it is used.
VOLUME_RATIO_CAP = 10.0


def _compression_names(window: str) -> tuple[str, ...]:
    return (f"compression_{window}", "compression")


def _range_pos_names(window: str) -> tuple[str, ...]:
    return (f"range_pos_{window}", "range_pos")


def _bb_width_names(window: str) -> tuple[str, ...]:
    return (f"bb_width_{window}_bps", "bb_width_bps")


def _zscore_names(window: str) -> tuple[str, ...]:
    return (f"zscore_{window}", f"z_{window}", f"bb_z_{window}")


def _mean_dev_names(window: str) -> tuple[str, ...]:
    return (f"mean_dev_{window}_bps", f"dev_from_mean_{window}_bps")


def _vwap_dev_names(window: str) -> tuple[str, ...]:
    return (f"vwap_dev_{window}_bps", "vwap_dev_bps")


REQUIRED_FEATURES: dict[str, tuple[str, ...]] = {
    "atr": ATR_NAMES,
    "atr_5m": ATR_5M_NAMES,
    "trend_strength": TREND_NAMES,
    "ema_slope": EMA_SLOPE_NAMES,
    "flow_imbalance": FLOW_NAMES,
    "flow_accel": FLOW_ACCEL_NAMES,
    "trade_count": TRADE_COUNT_NAMES,
    "notional": NOTIONAL_NAMES,
    "volume_z": VOLUME_Z_NAMES,
    "volume_ratio": VOLUME_RATIO_NAMES,
    "vol_percentile": VOL_PCT_NAMES,
    "vol_expansion": VOL_EXPANSION_NAMES,
    **{f"ret_{w}": _ret_names(w) for w in WINDOW_MINUTES if w != "60m"},
    **{f"compression_{w}": _compression_names(w) for w in ("3m", "5m", "15m")},
    **{f"range_pos_{w}": _range_pos_names(w) for w in ("3m", "5m", "15m")},
    **{f"bb_width_{w}": _bb_width_names(w) for w in ("3m", "5m", "15m")},
    **{f"zscore_{w}": _zscore_names(w) for w in ("3m", "5m", "15m")},
    **{f"mean_dev_{w}": _mean_dev_names(w) for w in ("3m", "5m", "15m")},
    **{f"vwap_dev_{w}": _vwap_dev_names(w) for w in ("5m", "15m", "60m")},
}


# --------------------------------------------------------------------------- #
# Base class
# --------------------------------------------------------------------------- #


class StrategyFamily:
    """Base for the ten.  Owns readiness, parameter defaults and emission."""

    name: str = "base"
    param_space: ParamSpace
    # None = every regime, decided by the tournament rather than by us.
    allowed_regimes: "frozenset[Regime7] | None" = None

    # -- public API -------------------------------------------------------- #

    def signal(self, f: "MTFFeatures", params: Mapping[str, Any]) -> ResearchSignal | None:
        if not self._ready(f):
            return None
        return self._signal(f, self._merged(params))

    # -- hooks ------------------------------------------------------------- #

    def _signal(self, f: "MTFFeatures", p: dict) -> ResearchSignal | None:
        raise NotImplementedError

    # -- shared plumbing --------------------------------------------------- #

    def _merged(self, params: Mapping[str, Any] | None) -> dict:
        merged = self.param_space.defaults()
        if params:
            merged.update({k: v for k, v in params.items() if k in merged})
        return merged

    def _ready(self, f: "MTFFeatures") -> bool:
        """Cold start guard.  No price, no volatility, no opinion."""
        price = getattr(f, "price", 0.0)
        if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            return False
        atr = _num(f, ATR_NAMES)
        return atr is not None and atr > 0.0

    def _atr(self, f: "MTFFeatures") -> float | None:
        atr = _num(f, ATR_NAMES)
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
        """Clamp, sanity-check and build.  Never emits NaN, inf or a zero score."""
        if direction == 0 or not math.isfinite(score):
            return None
        score = clamp(score)
        if score <= 0.0:
            return None
        return ResearchSignal(
            family=self.name,
            symbol=getattr(f, "symbol", ""),
            side=Side.LONG if direction > 0 else Side.SHORT,
            score=score,
            ts_ms=int(getattr(f, "ts_ms", 0)),
            reasons=reasons,
        )


def _sign(x: float) -> int:
    return 1 if x > 0 else -1 if x < 0 else 0


# --------------------------------------------------------------------------- #
# A. momentum_continuation
# --------------------------------------------------------------------------- #

# The confirming window for each impulse window: the impulse has to be part of
# a move on the next timeframe up, not a lone tick.
_CONFIRM_WINDOW = {"15s": "1m", "30s": "1m", "1m": "5m", "3m": "15m"}


class MomentumContinuation(StrategyFamily):
    """A: a fast leg that is *accelerating* keeps going for a little longer.

    Three things must all be true: the impulse is large against its own
    diffusion expectation, the next timeframe up agrees with it, and the
    impulse is running *faster* than that slower leg — acceleration, not a
    move that has already spent itself.
    """

    name = "momentum_continuation"
    param_space = ParamSpace(
        name="momentum_continuation",
        grid={
            # Which leg is "the impulse".  15s/30s is where a thin venue's
            # bursts actually live; 1m/3m is the horizon V1 and V2 both said
            # loses less.  Four values spans that argument end to end.
            "impulse_window": ("15s", "30s", "1m", "3m"),
            # Impulse size in units of the diffusion-scaled expected move.
            # 0.5 is "barely noticeable", 2.0 is a genuine outlier; beyond that
            # the sample empties out on a venue with 2.2 bps of 1m ATR.
            "min_impulse_atr": (0.5, 1.0, 1.5, 2.0),
            # Acceleration: impulse rate / confirming-leg rate, both per
            # sqrt-minute.  1.0 = merely keeping pace, 1.5 = clearly steepening.
            "accel_ratio": (1.0, 1.25, 1.5),
        },
    )

    def _signal(self, f: "MTFFeatures", p: dict) -> ResearchSignal | None:
        atr = self._atr(f)
        if atr is None:
            return None
        w_fast = str(p["impulse_window"])
        w_slow = _CONFIRM_WINDOW.get(w_fast)
        if w_slow is None:
            return None

        r_fast = _num(f, _ret_names(w_fast))
        r_slow = _num(f, _ret_names(w_slow))
        if r_fast is None or r_slow is None:
            return None

        exp_fast = _expected_move_bps(atr, w_fast)
        if exp_fast <= 0.0:
            return None
        strength = abs(r_fast) / exp_fast
        if strength < float(p["min_impulse_atr"]):
            return None

        direction = _sign(r_fast)
        if direction * r_slow <= 0.0:            # the slower leg must agree
            return None

        rate_fast = abs(r_fast) / math.sqrt(WINDOW_MINUTES[w_fast])
        rate_slow = abs(r_slow) / math.sqrt(WINDOW_MINUTES[w_slow])
        if rate_slow <= 0.0:
            return None
        accel = rate_fast / rate_slow
        if accel < float(p["accel_ratio"]):
            return None

        lo = float(p["min_impulse_atr"])
        trend = clamp(_opt(f, TREND_NAMES))
        score = (
            0.45 * scale(strength, lo, lo + 2.5)
            + 0.30 * scale(accel, float(p["accel_ratio"]), float(p["accel_ratio"]) + 1.5)
            + 0.25 * trend
        )
        return self._emit(
            f,
            direction,
            score,
            [
                f"{w_fast} {r_fast:+.1f} bps = {strength:.1f}x expected",
                f"accelerating {accel:.2f}x vs {w_slow}",
                f"trend strength {trend:.2f}",
            ],
        )


# --------------------------------------------------------------------------- #
# B. breakout
# --------------------------------------------------------------------------- #

# How close to the edge of the range counts as "at the edge".  Not a parameter:
# range_pos is clipped to [0, 1] so there is nothing to tune here, the
# tunable part is how compressed the range was and how hard price left it.
_BREAK_EDGE = 0.95


class Breakout(StrategyFamily):
    """B: a quiet range, then a decisive exit from it.

    Compression is the setup and the impulse is the trigger; neither alone is
    a breakout.  Price sitting at the top of a range that was never compressed
    is just a trend, and a large impulse inside a wide range is just noise.
    """

    name = "breakout"
    param_space = ParamSpace(
        name="breakout",
        grid={
            # Which range is being broken.  3m is a micro-coil, 15m is a real
            # base; on a venue where half the minutes are empty, the shorter
            # ranges are built from very few prints, so the span matters.
            "range_window": ("3m", "5m", "15m"),
            # compression in [0, 1], 1 = maximally compressed.  0.2 is barely a
            # coil, 0.65 is a genuinely tight one; above that almost nothing
            # qualifies.
            "min_compression": (0.2, 0.35, 0.5, 0.65),
            # Break size in expected 1m moves.  Below ~1x the move is inside the
            # spread-plus-slippage cost of taking it.
            "break_atr": (0.75, 1.25, 2.0, 3.0),
        },
    )

    def _signal(self, f: "MTFFeatures", p: dict) -> ResearchSignal | None:
        atr = self._atr(f)
        if atr is None:
            return None
        window = str(p["range_window"])

        compression = _num(f, _compression_names(window))
        range_pos = _num(f, _range_pos_names(window))
        impulse = _num(f, _ret_names("1m"))
        if compression is None or range_pos is None or impulse is None:
            return None

        min_comp = float(p["min_compression"])
        if compression < min_comp:
            return None

        if range_pos >= _BREAK_EDGE:
            direction = 1
        elif range_pos <= 1.0 - _BREAK_EDGE:
            direction = -1
        else:
            return None

        exp_1m = _expected_move_bps(atr, "1m")
        if exp_1m <= 0.0:
            return None
        break_size = direction * impulse / exp_1m
        if break_size < float(p["break_atr"]):
            return None

        lo = float(p["break_atr"])
        flow = clamp(direction * _opt(f, FLOW_NAMES), -1.0, 1.0)
        score = (
            0.45 * scale(break_size, lo, lo + 2.0)
            + 0.35 * scale(compression, min_comp, 1.0)
            + 0.20 * clamp(flow)
        )
        return self._emit(
            f,
            direction,
            score,
            [
                f"{window} range compression {compression:.2f}",
                f"break {break_size:.1f}x expected 1m move",
                f"range position {range_pos:.2f}",
            ],
        )


# --------------------------------------------------------------------------- #
# C. trend_pullback
# --------------------------------------------------------------------------- #

_PULLBACK_WINDOW = "3m"
_RESUME_WINDOW = "30s"
# A retrace deeper than this multiple of the entry threshold is a reversal, not
# a pullback.  Fixed rather than tuned: making both ends of the band free turns
# one hypothesis into a two-parameter curve fit.
_PULLBACK_MAX_MULT = 3.0


class TrendPullback(StrategyFamily):
    """C: an established trend, a controlled retrace, and the first resumption.

    The hard part is distinguishing a pullback from a top.  Three tests: the
    trend has to be real before the retrace, the retrace has to be bounded
    above as well as below, and something has to turn back before we act.
    """

    name = "trend_pullback"
    param_space = ParamSpace(
        name="trend_pullback",
        grid={
            # trend_strength in [0, 1].  0.3 lets in weak drift, 0.75 demands a
            # trend that is obvious to the eye.
            "trend_min": (0.3, 0.45, 0.6, 0.75),
            # Retrace depth over 3m, in expected-3m-move units.
            "pullback_atr": (0.5, 1.0, 1.5, 2.0),
            # How much the 30s leg must already have turned back.  0.0 means
            # "merely no longer retracing", which is the loosest honest version.
            "resume_atr": (0.0, 0.2, 0.4),
        },
    )

    def _signal(self, f: "MTFFeatures", p: dict) -> ResearchSignal | None:
        atr = self._atr(f)
        if atr is None:
            return None

        slope = _num(f, EMA_SLOPE_NAMES)
        trend = _num(f, TREND_NAMES)
        pull_ret = _num(f, _ret_names(_PULLBACK_WINDOW))
        resume_ret = _num(f, _ret_names(_RESUME_WINDOW))
        if slope is None or trend is None or pull_ret is None or resume_ret is None:
            return None

        direction = _sign(slope)
        if direction == 0:
            return None
        if trend < float(p["trend_min"]):
            return None

        exp_pull = _expected_move_bps(atr, _PULLBACK_WINDOW)
        exp_resume = _expected_move_bps(atr, _RESUME_WINDOW)
        if exp_pull <= 0.0 or exp_resume <= 0.0:
            return None

        depth = -direction * pull_ret / exp_pull        # >0 means it retraced
        floor = float(p["pullback_atr"])
        if depth < floor or depth > _PULLBACK_MAX_MULT * floor:
            return None

        resume = direction * resume_ret / exp_resume
        if resume < float(p["resume_atr"]):
            return None

        # Best depth sits in the middle of the allowed band; the edges are
        # "hasn't really pulled back" and "this may not be a pullback at all".
        span = max(_PULLBACK_MAX_MULT * floor - floor, 1e-9)
        centre = floor + 0.45 * span
        depth_quality = clamp(1.0 - abs(depth - centre) / (0.55 * span))

        score = (
            0.35 * scale(trend, float(p["trend_min"]), 1.0)
            + 0.35 * depth_quality
            + 0.30 * scale(resume, float(p["resume_atr"]), float(p["resume_atr"]) + 1.0)
        )
        return self._emit(
            f,
            direction,
            score,
            [
                f"trend {'up' if direction > 0 else 'down'} strength {trend:.2f}",
                f"retrace {depth:.1f}x over {_PULLBACK_WINDOW}",
                f"resuming {resume:.1f}x over {_RESUME_WINDOW}",
            ],
        )


# --------------------------------------------------------------------------- #
# D. mean_reversion
# --------------------------------------------------------------------------- #


def _extension_sigma(f: "MTFFeatures", window: str, atr: float) -> float | None:
    """How many sigma price sits from the window's mean, signed.

    Three sources in order of directness: a published z-score; a deviation in
    bps divided by the band's sigma; the VWAP deviation divided by the same
    sigma.  ``None`` when the feature block carries none of them — the family
    then stays silent instead of inventing an extension.
    """
    z = _num(f, _zscore_names(window))
    if z is not None:
        return z

    bb_width = _num(f, _bb_width_names(window))
    # A Bollinger width spans mean +/- 2 sigma, so a quarter of it is sigma.
    sigma = bb_width / 4.0 if bb_width and bb_width > 0 else _expected_move_bps(atr, window)
    if sigma <= 0.0:
        return None

    dev = _num(f, _mean_dev_names(window))
    if dev is None:
        dev = _num(f, _vwap_dev_names(window))
    if dev is None:
        return None
    return dev / sigma


class MeanReversion(StrategyFamily):
    """D: an extension beyond a trailing band reverts toward the mean.

    Deliberately the *statistical* version — distance measured in sigma of the
    window's own band, so it adapts to a 2.2 bps venue without a constant in
    it.  G (``vwap_reversion``) asks a related but distinct question: distance
    from a volume-weighted fair price, in expected-move units.  The tournament
    gets to say whether they are the same trade.
    """

    name = "mean_reversion"
    param_space = ParamSpace(
        name="mean_reversion",
        grid={
            # Which band.  Short bands mean-revert more often and pay less.
            "band_window": ("3m", "5m", "15m"),
            # Extension in sigma.  1.5 fires often, 3.0 is rare by construction
            # (and on a fat-tailed series, rarer than Gaussian arithmetic says).
            "extension_sigma": (1.5, 2.0, 2.5, 3.0),
            # Do not fade a strong trend.  0.4 is strict, 0.8 nearly permissive;
            # whether the strict version is worth the lost sample is exactly the
            # kind of thing the tournament should answer.
            "max_trend": (0.4, 0.6, 0.8),
        },
    )

    def _signal(self, f: "MTFFeatures", p: dict) -> ResearchSignal | None:
        atr = self._atr(f)
        if atr is None:
            return None
        window = str(p["band_window"])

        z = _extension_sigma(f, window, atr)
        if z is None:
            return None
        threshold = float(p["extension_sigma"])
        if abs(z) < threshold:
            return None

        max_trend = float(p["max_trend"])
        trend = clamp(_opt(f, TREND_NAMES))
        if trend > max_trend:
            return None

        direction = -_sign(z)                      # fade the stretch
        if direction == 0:
            return None

        # Flow turning back toward us is confirmation that the push is spent.
        accel = clamp(direction * _opt(f, FLOW_ACCEL_NAMES), -1.0, 1.0)
        score = (
            0.45 * scale(abs(z), threshold, threshold + 2.0)
            + 0.30 * (1.0 - clamp(trend / max_trend if max_trend > 0 else 1.0))
            + 0.25 * clamp(accel)
        )
        return self._emit(
            f,
            direction,
            score,
            [
                f"{abs(z):.1f} sigma from the {window} mean",
                f"trend strength {trend:.2f} <= {max_trend:.2f}",
                f"flow acceleration {accel:+.2f} toward reversion",
            ],
        )


# --------------------------------------------------------------------------- #
# E. volume_impulse
# --------------------------------------------------------------------------- #


def _tape_density(f: "MTFFeatures") -> tuple[float, float] | None:
    """(trade count, quote notional) for the last minute, or ``None``.

    ``None`` means the feature block does not carry tape density at all, and a
    tape family must then refuse: on a venue where 55% of minutes have zero
    trades, "assume it is fine" is the same as "fire on nothing".
    """
    trades = _num(f, TRADE_COUNT_NAMES)
    if trades is None:
        return None
    notional = _num(f, NOTIONAL_NAMES)
    return trades, (0.0 if notional is None else notional)


class VolumeImpulse(StrategyFamily):
    """E: abnormal volume *with* a price response.

    Volume alone is not a signal; volume that moved the price is.  The trade
    count floor exists because of the V2 ``volume_ratio`` blow-up: a ratio
    against a near-zero baseline is a division artefact, not participation.
    """

    name = "volume_impulse"
    param_space = ParamSpace(
        name="volume_impulse",
        grid={
            # Minimum prints in the minute.  The venue averages 2.19; 3 is
            # "anything at all", 20 is a burst by local standards.
            "min_trades": (3, 5, 10, 20),
            # Volume z-score (ratio-1, capped, as a fallback).
            "vol_z_min": (1.5, 2.5, 3.5, 5.0),
            # The accompanying move, in expected 1m moves.
            "response_atr": (0.5, 1.0, 1.5),
        },
    )

    def _signal(self, f: "MTFFeatures", p: dict) -> ResearchSignal | None:
        atr = self._atr(f)
        if atr is None:
            return None

        density = _tape_density(f)
        if density is None:
            return None
        trades, _notional = density
        min_trades = float(p["min_trades"])
        if trades < min_trades:
            return None

        z = _num(f, VOLUME_Z_NAMES)
        if z is None:
            ratio = _num(f, VOLUME_RATIO_NAMES)
            if ratio is None:
                return None
            z = min(ratio, VOLUME_RATIO_CAP) - 1.0
        z_min = float(p["vol_z_min"])
        if z < z_min:
            return None

        response = _num(f, _ret_names("1m"))
        if response is None:
            return None
        exp_1m = _expected_move_bps(atr, "1m")
        if exp_1m <= 0.0:
            return None
        move = abs(response) / exp_1m
        if move < float(p["response_atr"]):
            return None

        direction = _sign(response)
        lo = float(p["response_atr"])
        score = (
            0.40 * scale(z, z_min, z_min + 3.0)
            + 0.35 * scale(move, lo, lo + 2.0)
            + 0.25 * scale(trades, min_trades, min_trades * 4.0)
        )
        return self._emit(
            f,
            direction,
            score,
            [
                f"volume z {z:.1f} on {trades:.0f} prints",
                f"price response {response:+.1f} bps = {move:.1f}x expected",
            ],
        )


# --------------------------------------------------------------------------- #
# F. orderflow_imbalance
# --------------------------------------------------------------------------- #


class OrderflowImbalance(StrategyFamily):
    """F: aggressors are one-sided, so the next prints are too.

    This is the family most likely to have no data here — $753 of volume a
    minute is a handful of prints, and an imbalance computed over three trades
    is not an imbalance.  Both floors are therefore load-bearing, and both can
    make this family silent for the whole sample.  That is the finding.
    """

    name = "orderflow_imbalance"
    param_space = ParamSpace(
        name="orderflow_imbalance",
        grid={
            # Aggressor notional imbalance in [-1, 1].  0.25 is a tilt, 0.7 is
            # near one-sided.
            "imb_min": (0.25, 0.4, 0.55, 0.7),
            # Prints behind the imbalance.
            "min_trades": (5, 10, 20, 40),
            # Notional behind it.  $500 is about one average minute on this
            # venue; $10k is a genuine event here.
            "min_notional_usd": (500.0, 2000.0, 10000.0),
        },
    )

    def _signal(self, f: "MTFFeatures", p: dict) -> ResearchSignal | None:
        if self._atr(f) is None:
            return None

        density = _tape_density(f)
        if density is None:
            return None
        trades, notional = density
        min_trades = float(p["min_trades"])
        min_notional = float(p["min_notional_usd"])
        if trades < min_trades or notional < min_notional:
            return None

        imb = _num(f, FLOW_NAMES)
        if imb is None:
            return None
        imb_min = float(p["imb_min"])
        if abs(imb) < imb_min:
            return None

        direction = _sign(imb)
        accel = clamp(direction * _opt(f, FLOW_ACCEL_NAMES), -1.0, 1.0)
        score = (
            0.45 * scale(abs(imb), imb_min, 1.0)
            + 0.30 * scale(notional, min_notional, min_notional * 5.0)
            + 0.25 * clamp(accel)
        )
        return self._emit(
            f,
            direction,
            score,
            [
                f"aggressor imbalance {imb:+.2f} over {trades:.0f} prints",
                f"${notional:,.0f} of one-sided notional",
            ],
        )


# --------------------------------------------------------------------------- #
# G. vwap_reversion
# --------------------------------------------------------------------------- #


class VwapReversion(StrategyFamily):
    """G: distance from the volume-weighted fair price closes.

    The trend gate is what separates this from catching a falling knife: a
    price far from VWAP in a strong trend is not stretched, it is leading.
    """

    name = "vwap_reversion"
    param_space = ParamSpace(
        name="vwap_reversion",
        grid={
            # Which VWAP. 60m is the "typical 17 bps move" horizon that both
            # prior reports pointed at; 5m is the scalper's version.
            "vwap_window": ("5m", "15m", "60m"),
            # Distance in expected-move units for that window.
            "dev_atr": (1.0, 2.0, 3.0, 4.0),
            # Flatness requirement.
            "max_trend": (0.25, 0.5, 0.75),
        },
    )

    def _signal(self, f: "MTFFeatures", p: dict) -> ResearchSignal | None:
        atr = self._atr(f)
        if atr is None:
            return None
        window = str(p["vwap_window"])

        dev = _num(f, _vwap_dev_names(window))
        if dev is None:
            return None
        expected = _expected_move_bps(atr, window)
        if expected <= 0.0:
            return None
        distance = abs(dev) / expected
        threshold = float(p["dev_atr"])
        if distance < threshold:
            return None

        max_trend = float(p["max_trend"])
        trend = clamp(_opt(f, TREND_NAMES))
        if trend > max_trend:
            return None

        direction = -_sign(dev)
        if direction == 0:
            return None

        accel = clamp(direction * _opt(f, FLOW_ACCEL_NAMES), -1.0, 1.0)
        score = (
            0.50 * scale(distance, threshold, threshold + 2.0)
            + 0.30 * (1.0 - clamp(trend / max_trend if max_trend > 0 else 1.0))
            + 0.20 * clamp(accel)
        )
        return self._emit(
            f,
            direction,
            score,
            [
                f"{distance:.1f}x expected {window} move from VWAP ({dev:+.1f} bps)",
                f"trend strength {trend:.2f} <= {max_trend:.2f}",
            ],
        )


# --------------------------------------------------------------------------- #
# H. volatility_expansion
# --------------------------------------------------------------------------- #


def _expansion_ratio(f: "MTFFeatures", atr_1m: float) -> float | None:
    """Fast volatility over slow volatility.

    Preferred: a published ratio.  Fallback: ``atr_1m * sqrt(5) / atr_5m``,
    which is 1.0 when the two agree under diffusion scaling and rises when the
    one-minute series has just woken up.  ``None`` when neither is available.
    """
    ratio = _num(f, VOL_EXPANSION_NAMES)
    if ratio is not None:
        return ratio
    atr_5m = _num(f, ATR_5M_NAMES)
    if atr_5m is None or atr_5m <= 0.0:
        return None
    return (atr_1m * math.sqrt(5.0)) / atr_5m


class VolatilityExpansion(StrategyFamily):
    """H: the regime change *is* the signal.

    Not "volatility is high" — that is a state — but "volatility just became
    high", with a direction taken from the move that did it.  Symmetric, and
    honestly so: an expansion can resolve either way and the tournament will
    say whether the direction of the expanding leg carries any information.
    """

    name = "volatility_expansion"
    param_space = ParamSpace(
        name="volatility_expansion",
        grid={
            # Fast/slow volatility ratio.  1.5 is a stir, 4.0 is a dislocation.
            "expansion_ratio": (1.5, 2.0, 3.0, 4.0),
            # Current volatility percentile: the expansion has to leave vol
            # genuinely high, not merely less dead than five minutes ago.
            "min_vol_pct": (0.5, 0.7, 0.9),
            # Size of the leg that caused it, in expected 1m moves.
            "direction_atr": (0.5, 1.0, 2.0),
        },
    )

    def _signal(self, f: "MTFFeatures", p: dict) -> ResearchSignal | None:
        atr = self._atr(f)
        if atr is None:
            return None

        ratio = _expansion_ratio(f, atr)
        if ratio is None:
            return None
        min_ratio = float(p["expansion_ratio"])
        if ratio < min_ratio:
            return None

        min_pct = float(p["min_vol_pct"])
        vol_pct = _num(f, VOL_PCT_NAMES)
        if vol_pct is not None and vol_pct < min_pct:
            return None

        leg = _num(f, _ret_names("1m"))
        if leg is None:
            return None
        exp_1m = _expected_move_bps(atr, "1m")
        if exp_1m <= 0.0:
            return None
        move = abs(leg) / exp_1m
        if move < float(p["direction_atr"]):
            return None

        direction = _sign(leg)
        lo = float(p["direction_atr"])
        score = (
            0.45 * scale(ratio, min_ratio, min_ratio + 2.0)
            + 0.30 * scale(move, lo, lo + 2.0)
            + 0.25 * clamp(0.0 if vol_pct is None else scale(vol_pct, min_pct, 1.0))
        )
        return self._emit(
            f,
            direction,
            score,
            [
                f"volatility {ratio:.1f}x its slower baseline",
                f"expanding leg {leg:+.1f} bps = {move:.1f}x expected",
            ],
        )


# --------------------------------------------------------------------------- #
# I. short_term_reversal
# --------------------------------------------------------------------------- #

# The sub-window used to detect the stall.  Fixed per window rather than tuned:
# it is the definition of "has it stopped", not a free parameter.
_STALL_WINDOW = {"15s": "5s", "30s": "15s", "1m": "15s"}


class ShortTermReversal(StrategyFamily):
    """I: a very short, very stretched move gives some of itself back.

    The opposite hypothesis to A on the same data, which is the point — one of
    them has to be wrong on this venue and the tournament should find out which
    rather than us assuming.  The stall test is what stops this being "sell
    every up-tick": the move must have run *and stopped* running.
    """

    name = "short_term_reversal"
    param_space = ParamSpace(
        name="short_term_reversal",
        grid={
            # The over-extended leg.  All sub-minute: past a minute this
            # becomes D, not I.
            "window": ("15s", "30s", "1m"),
            # Extension in expected-move units.  Deliberately higher than A's
            # thresholds — this is the tail of the same distribution.
            "ext_atr": (1.5, 2.5, 3.5, 5.0),
            # How much the newest sub-leg must have given back.  0.0 = merely
            # no longer extending.
            "stall_frac": (0.0, 0.25, 0.5),
        },
    )

    def _signal(self, f: "MTFFeatures", p: dict) -> ResearchSignal | None:
        atr = self._atr(f)
        if atr is None:
            return None
        window = str(p["window"])
        sub = _STALL_WINDOW.get(window)
        if sub is None:
            return None

        leg = _num(f, _ret_names(window))
        sub_leg = _num(f, _ret_names(sub))
        if leg is None or sub_leg is None:
            return None

        expected = _expected_move_bps(atr, window)
        exp_sub = _expected_move_bps(atr, sub)
        if expected <= 0.0 or exp_sub <= 0.0:
            return None

        extension = abs(leg) / expected
        threshold = float(p["ext_atr"])
        if extension < threshold:
            return None

        move_dir = _sign(leg)
        if move_dir == 0:
            return None
        stall = -move_dir * sub_leg / exp_sub      # >0 means giving it back
        stall_min = float(p["stall_frac"])
        if stall < stall_min:
            return None

        direction = -move_dir                      # fade the move
        trend = clamp(_opt(f, TREND_NAMES))
        score = (
            0.50 * scale(extension, threshold, threshold + 3.0)
            + 0.30 * scale(stall, stall_min, stall_min + 1.5)
            + 0.20 * (1.0 - trend)
        )
        return self._emit(
            f,
            direction,
            score,
            [
                f"{window} leg {leg:+.1f} bps = {extension:.1f}x expected",
                f"stalling {stall:.2f} over {sub}",
            ],
        )


# --------------------------------------------------------------------------- #
# J. hybrid_multifactor
# --------------------------------------------------------------------------- #


class HybridMultifactor(StrategyFamily):
    """J: one explicit linear blend of trend, reversion and flow.

    It reads the same feature block everyone else does and computes its own
    three legs; it does **not** call A–I.  The spec evaluates families
    independently *before* blending, so a hybrid that reached into the others'
    internals would smuggle their parameters into its own grid and make both
    results uninterpretable.

    The blend is a single tilt between two opposed hypotheses plus a flow
    overlay, which keeps it to three parameters and makes the output readable:
    ``momentum_tilt`` 0.0 is a pure fade, 1.0 a pure chase, and the tournament
    can see where in between (if anywhere) the edge sits.
    """

    name = "hybrid_multifactor"
    param_space = ParamSpace(
        name="hybrid_multifactor",
        grid={
            # Trend vs reversion.  Five values so the two pure endpoints and
            # the midpoint are all represented.
            "momentum_tilt": (0.0, 0.25, 0.5, 0.75, 1.0),
            # Conviction gate.  This is the "10 excellent trades vs 150
            # mediocre" dial made explicit as a parameter.
            "min_conviction": (0.3, 0.45, 0.6, 0.75),
            # How much the tape is allowed to tilt the blend.  0.0 keeps the
            # family runnable on a venue whose tape is mostly empty.
            "flow_weight": (0.0, 0.25, 0.5),
        },
    )

    # Legs are squashed against three expected moves, so a 3-sigma event maps
    # to a full-strength leg and anything beyond is capped rather than dominant.
    _SATURATION = 3.0

    def _signal(self, f: "MTFFeatures", p: dict) -> ResearchSignal | None:
        atr = self._atr(f)
        if atr is None:
            return None

        r_1m = _num(f, _ret_names("1m"))
        exp_1m = _expected_move_bps(atr, "1m")
        if r_1m is None or exp_1m <= 0.0:
            return None
        momentum_leg = clamp(r_1m / (self._SATURATION * exp_1m), -1.0, 1.0)

        dev = _num(f, _vwap_dev_names("5m"))
        exp_5m = _expected_move_bps(atr, "5m")
        if dev is None or exp_5m <= 0.0:
            return None
        reversion_leg = clamp(-dev / (self._SATURATION * exp_5m), -1.0, 1.0)

        tilt = float(p["momentum_tilt"])
        flow_weight = float(p["flow_weight"])
        flow_leg = clamp(_opt(f, FLOW_NAMES), -1.0, 1.0) if flow_weight > 0 else 0.0

        blended = (
            tilt * momentum_leg
            + (1.0 - tilt) * reversion_leg
            + flow_weight * flow_leg
        ) / (1.0 + flow_weight)

        conviction = abs(blended)
        if conviction < float(p["min_conviction"]):
            return None
        direction = _sign(blended)

        return self._emit(
            f,
            direction,
            conviction,
            [
                f"blend {blended:+.2f} (tilt {tilt:.2f}, flow w {flow_weight:.2f})",
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
    claimed.
    """
    return sum(fam.param_space.size() for fam in families)


def feature_names_used() -> tuple[str, ...]:
    """Every feature name any family may read, deduplicated and sorted."""
    names: set[str] = set()
    for aliases in REQUIRED_FEATURES.values():
        names.update(aliases)
    return tuple(sorted(names))
