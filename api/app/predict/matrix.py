"""The leakage-safe feature matrix for the predictive entry engine.

One row per label, every feature computed **as of that label's entry instant
and never later**.  Three properties decide whether anything downstream is
worth reading, and each is asserted directly rather than assumed:

1.  **No leakage.**  Every statistic is folded from bars that had already
    *closed* at the row's timestamp, from trades that had already *printed*,
    and from book snapshots that had already been *received*.  Nothing in this
    module is fitted on the full sample — in particular ``build_matrix`` does
    **no scaling at all**.  A scaler fitted over the whole matrix would carry
    the test window's mean and variance into training, which is leakage with a
    respectable name; ``models.py`` fits its scaler on the training slice only.
    ``test_predict_matrix.py`` builds a matrix, appends future bars, rebuilds,
    and asserts every row at time T is byte-identical.

2.  **No divide-by-zero artefacts.**  Binance.US leaves 51% of 1-minute and 90%
    of 5-second buckets with no trade at all (``docs/ECONOMICS.md``).  A ratio
    taken against a baseline that is mostly zeros is arithmetic, not
    information: an earlier volume ratio on this venue reported readings of
    **1291x**, which made every volume gate fire on the first print after a
    quiet spell.  ``app.features._volume_ratio`` fixed that by refusing to
    divide unless the baseline is measurable, returning a *documented neutral*
    and capping the result.  Every denominator here follows the same
    discipline, and the neutral is never ``0.0`` where ``0.0`` already carries
    meaning: ratios degrade to ``1.0``, percentiles to ``0.5``, signed
    quantities to ``0.0``.

3.  **Built once per instant.**  Label specs differ in their *barriers*, not in
    what the market looked like at entry, so features are computed once per
    ``(symbol, timestamp)`` and shared across every spec that references that
    instant.  The result is cached on disk under a key that includes
    :data:`FEATURE_VERSION`, so a change to the feature maths invalidates the
    cache rather than silently serving stale rows.

What is reused and what is new
------------------------------

``app.research.features_mtf`` already publishes 124 named multi-timeframe
features with exactly these properties, so it is the engine underneath rather
than something to re-derive.  This module adds what it does not publish —
distance from the recent high/low, range compression, volatility expansion,
trade-count acceleration, cumulative signed flow, the volume/price response,
the book's shape and its withdrawal, the cross-sectional regime, the **cost**
group, and cyclical time — and then arranges everything into the eight groups
:data:`FEATURE_GROUPS` names.

Book features exist only where live L2 does
-------------------------------------------

Binance.US publishes no depth archive, so most of the historical tape has no
book.  The ``book`` group is then **omitted entirely** — not zero-filled, not
imputed.  A fabricated book feature is worse than a missing one: the model
would learn a relationship to a constant and the live engine would then feed it
a real number it had never seen.

The cost group is the point
---------------------------

``docs/ECONOMICS.md`` is unambiguous that on this venue the cost of transacting
is the same order as the move being captured, and that NET targets below ~10
bps produced literally zero trades in 120 days.  A model that cannot see its
own costs is therefore not "slightly optimistic", it is measuring the wrong
quantity.  The ``cost`` group carries the maker and taker fee for the symbol,
the spread (measured from L2 where it exists, estimated from the trailing tape
by Roll's estimator where it does not), an expected slippage, the predicted
execution method, and the **break-even move** the trade has to make before a
single basis point of it is ours.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Mapping, Sequence

import numpy as np

from ..config import Settings, get_settings
from ..research.dataset import INTERVALS, Dataset, interval_ms
from ..research.features_mtf import (
    DEPTH_BPS,
    MAX_VOLUME_RATIO,
    MTFFeatureEngine,
    MTFFeatures,
    feature_name,
)
from ..research.regimes import Regime7, Regime7Classifier

__all__ = [
    "FEATURE_GROUPS",
    "FEATURE_VERSION",
    "GROUP_NAMES",
    "NEUTRALS",
    "FeatureMatrix",
    "build_matrix",
    "clear_matrix_cache",
    "feature_group",
    "group_all_neutral_fractions",
    "group_neutral_fractions",
    "neutral_fractions",
]


# --------------------------------------------------------------------------- #
# Version
# --------------------------------------------------------------------------- #

#: Bumped whenever the *value* of any feature changes.  It is part of every
#: cache key, so a maths change invalidates cached rows instead of silently
#: serving rows built by the old code.  Adding a feature counts; renaming one
#: counts; a comment does not.
FEATURE_VERSION = "matrix-2026.09.11-1"


# --------------------------------------------------------------------------- #
# Windows and guards.  Module constants so the tests and the report quote one
# number, and so nothing here is a magic literal buried in an expression.
# --------------------------------------------------------------------------- #

RANGE_WINDOW = 20          # trailing bars behind the high/low distance
FAST_WINDOW = 5            # "recent" leg of every acceleration ratio
SLOW_WINDOW = 20           # baseline leg of every acceleration ratio
CUMFLOW_WINDOW = 20        # bars in the cumulative signed-flow sum
ROLL_TRADES = 50           # prints in the Roll effective-spread estimate
ACTIVITY_HOURS = 24        # completed hours in the activity distribution
MIN_ACTIVITY_HOURS = 8     # fewer than this and the percentile means nothing
BOOK_RECENT = 20           # book snapshots behind the liquidity-withdrawal max
TRADE_THROUGH_WINDOW = 50  # prints behind the trade-through rate
NEAR_BPS = 2.0             # "at the touch" depth band, against DEPTH_BPS

#: A baseline window must be at least half occupied before a ratio against it
#: is a measurement rather than an artefact.  Same rule, and the same halving,
#: as ``app.features.MIN_ACTIVE_BARS`` — this is the fix that stopped the 1291x
#: volume readings and it is applied to every ratio in this module.
MIN_ACTIVE_BARS = SLOW_WINDOW // 2

#: Hard caps.  Past these a ratio has stopped carrying information and is only
#: adding leverage to whatever produced it.
MAX_RATIO = MAX_VOLUME_RATIO          # 20.0, matching the live engine
MAX_BPS = 50_000.0                    # 500%: a bps reading past this is a bug
MAX_USD = 1e9                         # a depth reading past this is a bug
MAX_ABS_FEATURE = 1e9                 # the absolute backstop, asserted on X

#: ATR floor used as a denominator, in bps.  BTC/USD's 1m ATR is 2.2 bps, so
#: 0.1 is comfortably below anything real while still refusing to divide by a
#: warm-up zero.
MIN_ATR_BPS = 0.1
MIN_VOL_ACCEL = 0.05

#: Cost model.  ``docs/ECONOMICS.md`` measures BTC/USD's spread at 0.50 bps and
#: SOL/USD's at 2.02 bps; 1.0 sits between them and is used **only** when there
#: is neither an L2 book nor enough prints for Roll's estimator.  It is a cost
#: *assumption*, declared here rather than hidden in an expression, and the
#: report says how often it was used.
DEFAULT_SPREAD_BPS = 1.0
MIN_SPREAD_BPS = 0.05
MAX_SPREAD_BPS = 200.0
#: Slippage beyond the touch, as a multiple of the half spread, when no book is
#: available to walk.  ``docs/ECONOMICS.md``: slippage ran ~3x fees in every run
#: and was 57% of the gross loss in the 30-day run, so modelling it at zero
#: would be the single most flattering error available.
SLIP_SPREAD_MULT = 1.0
#: ... scaled up as the tape thins: a bar series that is half empty gets 1.5x.
SLIP_THIN_TAPE_MULT = 1.0
MAX_SLIP_BPS = 100.0
#: Depth-walk slippage when a book *is* available: cost grows with the fraction
#: of the resting depth the order would consume.
SLIP_DEPTH_K = 0.5
#: Resting is predicted to beat crossing when the spread saved exceeds the fee
#: difference by at least this much ...
MIN_MAKER_EDGE_BPS = 0.25
#: ... and the tape is not moving fast enough to run the quote over.
MAKER_MAX_RV_MULT = 3.0

#: Reference order size for the slippage model, in USD.  Overridden from
#: ``settings.portfolio.max_position_usd`` when that is available.
DEFAULT_REF_NOTIONAL_USD = 125.0

#: Distinct regimes, cast to numbers a tree can split on without inventing an
#: ordering between, say, PANIC and BREAKOUT.
_TREND_SIGN = {Regime7.TRENDING_UP: 1.0, Regime7.TRENDING_DOWN: -1.0}
_VOL_STATE = {
    Regime7.LOW_VOLATILITY: -1.0,
    Regime7.VOLATILITY_EXPANSION: 1.0,
    Regime7.BREAKOUT: 1.0,
    Regime7.PANIC: 1.0,
}

#: The grid the cross-sectional (risk-on/risk-off, BTC direction) context is
#: sampled on.  Coarse on purpose: breadth is a slow statistic and sampling it
#: per 5-second bar would be noise with a decimal point.
CONTEXT_INTERVAL_PREFERENCE: tuple[str, ...] = ("5m", "3m", "1m", "30s", "15s", "5s")
CONTEXT_LOOKBACK_BARS = 5

BTC_SYMBOLS: tuple[str, ...] = ("BTCUSD", "BTCUSDT")

_BPS = 10_000.0
_DAY_MS = 86_400_000
_HOUR_MS = 3_600_000
#: 1970-01-01 was a Thursday; Monday is 0.
_EPOCH_WEEKDAY = 3


# --------------------------------------------------------------------------- #
# The feature catalogue
# --------------------------------------------------------------------------- #

#: Per-interval bases.  Names carrying an interval suffix are unambiguous about
#: which timeframe produced them, exactly as in ``features_mtf``.
MOMENTUM_BASES: tuple[str, ...] = (
    "ret",                # last closed bar's return, bps            [mtf]
    "ret5",               # 5-bar return, bps                        [mtf]
    "mom_slope",          # OLS slope of the last 10 closes, bps/bar [mtf]
    "accel",              # change in that slope, bps/bar^2          [mtf]
    "ema_slope",          # EMA(9) slope, bps/bar                    [mtf]
    "vwap_dist",          # distance from the rolling VWAP, bps      [mtf]
    "rsi",                # Wilder RSI, 0..100                       [mtf]
    "zscore",             # mean-reversion z-score                   [mtf]
    "range_pos",          # position inside the trailing range, 0..1 [mtf]
    "breakout_dist",      # distance outside the prior range, bps    [mtf]
    "breakout_strength",  # that distance in ATRs                    [new]
    "dist_high",          # distance below the trailing high, bps    [new]
    "dist_low",           # distance above the trailing low, bps     [new]
)

VOLATILITY_BASES: tuple[str, ...] = (
    "rv",                 # realized vol, bps                        [mtf]
    "rvol_pct",           # its percentile in its own history, 0..1  [mtf]
    "atr",                # Wilder ATR, bps of price                 [mtf]
    "range_compress",     # recent bar range / baseline range        [new]
    "vol_expand",         # recent realized vol / baseline           [new]
)

VOLUME_BASES: tuple[str, ...] = (
    "vol_accel",          # recent quote volume / baseline           [mtf]
    "vol_pct",            # percentile of this bar's volume, 0..1    [mtf]
    "trades",             # trade count of the last closed bar       [mtf]
    "aggr_imb",           # taker buy vs sell imbalance, -1..1       [mtf]
    "trade_accel",        # recent trade count / baseline            [new]
    "cum_flow",           # cumulative signed flow, -1..1            [new]
    "vol_price_resp",     # bps moved per unit of relative volume    [new]
    "active_frac",        # fraction of recent bars that traded      [new]
)

BOOK_NAMES: tuple[str, ...] = (
    "spread_bps",            # quoted spread                         [mtf]
    "depth_bid_usd",         # resting bid notional within 10 bps    [mtf]
    "depth_ask_usd",         # resting ask notional within 10 bps    [mtf]
    "book_imbalance",        # (bid-ask)/(bid+ask) within 10 bps     [mtf]
    "microprice_dist_bps",   # microprice lean against the mid       [mtf]
    "depth_slope",           # depth at 10 bps / depth at 2 bps      [new]
    "depth_change",          # total depth now / at the last book    [new]
    "book_pressure",         # imbalance at the touch, -1..1         [new]
    "trade_through_rate",    # prints outside the prevailing touch   [new]
    "liq_withdrawal",        # drop from the recent depth maximum    [new]
)

REGIME_NAMES: tuple[str, ...] = (
    "regime_trend",        # +1 trending up, -1 trending down, 0 else
    "regime_is_range",     # 1 when the classifier says RANGING
    "regime_vol_state",    # -1 quiet, +1 expanding/breaking/panicking
    "regime_confidence",   # 0.0 means the classifier has no opinion
    "btc_dir",             # BTC's direction: -1/0/+1
    "btc_ret_bps",         # BTC's trailing move over the context window
    "risk_on",             # cross-sectional breadth across the dataset, -1..1
)

COST_NAMES: tuple[str, ...] = (
    "fee_maker_bps",       # what resting costs on this symbol
    "fee_taker_bps",       # what crossing costs on this symbol
    "cost_spread_bps",     # measured from L2, else Roll's estimator
    "slip_est_bps",        # expected impact beyond the touch
    "maker_rt_bps",        # round trip: rest in, cross out
    "taker_rt_bps",        # round trip: cross both ways
    "exec_is_maker",       # 1.0 when resting is predicted to win
    "breakeven_bps",       # the gross move required to net exactly zero
    "breakeven_over_atr",  # that move in 1-bar ATRs
    "breakeven_over_rv",   # ... and in 1-bar realized vols
)

TIME_NAMES: tuple[str, ...] = (
    "hour_sin",            # cyclical: continuous across midnight
    "hour_cos",
    "dow_sin",             # cyclical: continuous across Sunday -> Monday
    "dow_cos",
    "is_weekend",
    "activity_regime",     # last completed hour's flow vs the trailing day
)

SCORE_NAMES: tuple[str, ...] = (
    "score_long",          # best deterministic long conviction, 0..1
    "score_short",         # best deterministic short conviction, 0..1
    "score_signed",        # long - short
    "score_families",      # how many families had any opinion at all
)

GROUP_NAMES: tuple[str, ...] = (
    "momentum",
    "volatility",
    "volume",
    "book",
    "regime",
    "cost",
    "time",
    "score",
)

_PER_INTERVAL_BASES: dict[str, tuple[str, ...]] = {
    "momentum": MOMENTUM_BASES,
    "volatility": VOLATILITY_BASES,
    "volume": VOLUME_BASES,
}

#: The complete catalogue: every feature this module can ever emit, across every
#: interval ``app.research.dataset`` supports.  A concrete matrix emits a
#: *subset* — the intervals the dataset actually carries, and the ``book`` group
#: only where live L2 exists — but every column it emits appears here, which is
#: what ``test_predict_matrix.py`` asserts in both directions.
FEATURE_GROUPS: dict[str, tuple[str, ...]] = {
    "momentum": tuple(
        feature_name(b, iv) for iv in INTERVALS for b in MOMENTUM_BASES
    ),
    "volatility": tuple(
        feature_name(b, iv) for iv in INTERVALS for b in VOLATILITY_BASES
    ),
    "volume": tuple(
        feature_name(b, iv) for iv in INTERVALS for b in VOLUME_BASES
    ),
    "book": BOOK_NAMES,
    "regime": REGIME_NAMES,
    "cost": COST_NAMES,
    "time": TIME_NAMES,
    "score": SCORE_NAMES,
}

_GROUP_OF: dict[str, str] = {
    name: group for group, names in FEATURE_GROUPS.items() for name in names
}


def feature_group(name: str) -> str:
    """Which of the eight groups ``name`` belongs to.  Raises if it belongs to none."""
    try:
        return _GROUP_OF[name]
    except KeyError:
        raise KeyError(f"{name!r} is not in FEATURE_GROUPS") from None


# --------------------------------------------------------------------------- #
# Documented neutrals
# --------------------------------------------------------------------------- #

#: Base -> the value reported when the quantity cannot be measured *yet*.
#: Anything absent is a signed quantity whose neutral is 0.0.  This is the same
#: table, extended, as ``features_mtf.NEUTRAL``, and the rule behind it is the
#: one from ``docs/ECONOMICS.md``: returning 0.0 where 0.0 already means
#: "collapsed" is not a safe default, it is a different lie.
_NEUTRAL_BASE: dict[str, float] = {
    # percentiles and positions
    "rvol_pct": 0.5,
    "vol_pct": 0.5,
    "range_pos": 0.5,
    "activity_regime": 0.5,
    # ratios: 1.0 is "the same as its own baseline"
    "vol_accel": 1.0,
    "trade_accel": 1.0,
    "range_compress": 1.0,
    "vol_expand": 1.0,
    "depth_slope": 1.0,
    "depth_change": 1.0,
    # RSI is undefined on a flat window; 50 is what the formula tends to
    "rsi": 50.0,
}


def _neutral_for(name: str) -> float:
    base = name
    if "_" in name:
        head, _, tail = name.rpartition("_")
        if tail in INTERVALS:
            base = head
    return _NEUTRAL_BASE.get(base, 0.0)


#: Feature name -> its neutral value, for every name in the catalogue.
NEUTRALS: dict[str, float] = {
    name: _neutral_for(name) for names in FEATURE_GROUPS.values() for name in names
}


# --------------------------------------------------------------------------- #
# Per-feature caps.  Nothing leaves this module unbounded.
# --------------------------------------------------------------------------- #

_RATIO_BASES = frozenset(
    {
        "vol_accel", "trade_accel", "range_compress", "vol_expand",
        "breakout_strength", "depth_slope", "depth_change",
        "breakeven_over_atr", "breakeven_over_rv",
    }
)
_UNIT_BASES = frozenset(
    {
        "rvol_pct", "vol_pct", "range_pos", "aggr_imb", "cum_flow",
        "active_frac", "book_imbalance", "book_pressure", "trade_through_rate",
        "liq_withdrawal", "risk_on", "regime_confidence", "regime_is_range",
        "regime_trend", "regime_vol_state", "btc_dir", "exec_is_maker",
        "is_weekend", "hour_sin", "hour_cos", "dow_sin", "dow_cos",
        "activity_regime", "score_long", "score_short", "score_signed",
    }
)


def _cap_for(name: str) -> float:
    """The magnitude past which ``name`` is a bug rather than a measurement."""
    base = name
    if "_" in name:
        head, _, tail = name.rpartition("_")
        if tail in INTERVALS:
            base = head
    if base in _UNIT_BASES:
        return 1.0 if base != "rsi" else 100.0
    if base in _RATIO_BASES:
        return MAX_RATIO
    if base.endswith("_usd"):
        return MAX_USD
    if base == "rsi":
        return 100.0
    if base == "trades" or base == "score_families":
        return 1e6
    return MAX_BPS


_CAPS: dict[str, float] = {
    name: _cap_for(name) for names in FEATURE_GROUPS.values() for name in names
}


# --------------------------------------------------------------------------- #
# Small numeric helpers
# --------------------------------------------------------------------------- #


def _finite(x: Any, default: float = 0.0) -> float:
    if x is None:
        return default
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return default
    return default if (math.isnan(xf) or math.isinf(xf)) else xf


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _stdev(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(var) if var > 0 else 0.0


def _percentile_rank(window: Sequence[float], x: float) -> float:
    n = len(window)
    if n == 0:
        return 0.5
    below = equal = 0
    for v in window:
        if v < x:
            below += 1
        elif v == x:
            equal += 1
    return (below + 0.5 * equal) / n


def _guarded_ratio(
    fast: Sequence[float],
    slow: Sequence[float],
    *,
    min_active: int = MIN_ACTIVE_BARS,
    cap: float = MAX_RATIO,
) -> tuple[float, bool]:
    """``mean(fast) / mean(slow)``, or the neutral ``1.0``.

    Returns ``(value, guarded)``.  The ratio is refused — not merely clipped —
    unless the baseline window is full *and* at least ``min_active`` of its bars
    actually carried something.  This is the ``_volume_ratio`` discipline: on a
    venue where most buckets are empty, a ratio against a wall of zeros reports
    a 1291x surge on the first print after a quiet hour.
    """
    if len(slow) < SLOW_WINDOW or not fast:
        return 1.0, True
    if sum(1 for v in slow if v > 0) < min_active:
        return 1.0, True
    base = sum(slow) / len(slow)
    if base <= 0:
        return 1.0, True
    value = (sum(fast) / len(fast)) / base
    return min(value, cap), False


# --------------------------------------------------------------------------- #
# The contract
# --------------------------------------------------------------------------- #


@dataclass
class FeatureMatrix:
    """Aligned rows: one per label, in ``(ts_ms, symbol, spec_key)`` order.

    ``X`` is **raw and unscaled, deliberately**.  Fitting a scaler here would
    mix the test window's distribution into the training rows; the model layer
    fits one on its training slice and carries the mean/scale in its artifact.
    """

    X: "np.ndarray"
    y: "np.ndarray"
    ts_ms: "np.ndarray"
    symbols: list[str]
    feature_names: list[str]
    label_end_ms: "np.ndarray"
    #: Additive, and every one of them has a default so positional construction
    #: against the frozen six-field contract keeps working.
    spec_keys: list[str] = field(default_factory=list)
    net_return_bps: "np.ndarray | None" = None
    guarded: "np.ndarray | None" = None
    feature_version: str = FEATURE_VERSION
    meta: dict = field(default_factory=dict)

    # -- shape ------------------------------------------------------------- #

    def __len__(self) -> int:
        return int(self.X.shape[0])

    @property
    def n_features(self) -> int:
        return int(self.X.shape[1])

    # -- slicing ----------------------------------------------------------- #

    def slice_ms(self, lo: int, hi: int) -> "FeatureMatrix":
        """Rows with ``lo <= ts_ms < hi``.  Half-open, and a **view**.

        Half-open because splits abut: ``[a, b)`` then ``[b, c)`` must partition
        without a row landing in both, and a closed upper bound would duplicate
        every boundary row into two folds.

        A view, because re-deriving a window would be the one place leakage
        could sneak back in — a recomputation could see bars that the full-range
        build had correctly excluded.  ``ts_ms`` is sorted, so the selection is
        a contiguous range and numpy hands back a genuine view of the same
        buffer rather than a copy.
        """
        n = len(self)
        if n == 0:
            return self._empty_like()
        i = int(np.searchsorted(self.ts_ms, int(lo), side="left"))
        j = int(np.searchsorted(self.ts_ms, int(hi), side="left"))
        if j < i:
            j = i
        return FeatureMatrix(
            X=self.X[i:j],
            y=self.y[i:j],
            ts_ms=self.ts_ms[i:j],
            symbols=self.symbols[i:j],
            feature_names=self.feature_names,
            label_end_ms=self.label_end_ms[i:j],
            spec_keys=self.spec_keys[i:j] if self.spec_keys else [],
            net_return_bps=(
                None if self.net_return_bps is None else self.net_return_bps[i:j]
            ),
            guarded=None if self.guarded is None else self.guarded[i:j],
            feature_version=self.feature_version,
            meta=self.meta,
        )

    def _empty_like(self) -> "FeatureMatrix":
        return FeatureMatrix(
            X=self.X[:0],
            y=self.y[:0],
            ts_ms=self.ts_ms[:0],
            symbols=[],
            feature_names=self.feature_names,
            label_end_ms=self.label_end_ms[:0],
            spec_keys=[],
            net_return_bps=(
                None if self.net_return_bps is None else self.net_return_bps[:0]
            ),
            guarded=None if self.guarded is None else self.guarded[:0],
            feature_version=self.feature_version,
            meta=self.meta,
        )

    # -- convenience ------------------------------------------------------- #

    def column(self, name: str) -> "np.ndarray":
        return self.X[:, self.feature_names.index(name)]

    def groups(self) -> dict[str, list[str]]:
        """The emitted columns, grouped.  Empty groups are omitted."""
        out: dict[str, list[str]] = {}
        for name in self.feature_names:
            out.setdefault(feature_group(name), []).append(name)
        return out


# --------------------------------------------------------------------------- #
# Neutral-fallback reporting
# --------------------------------------------------------------------------- #


def neutral_fractions(matrix: FeatureMatrix) -> dict[str, float]:
    """Per feature, the fraction of rows that fell back to its neutral value.

    Measured from the guard mask the builder records while computing, so it is
    the *guard actually firing*, not a value that happens to equal the neutral.
    For the features sourced straight from ``features_mtf`` the mask is derived
    by comparison against that module's documented neutral, which for a signed
    quantity cannot distinguish "still warming up" from a true reading of zero;
    that is stated wherever the number is reported.
    """
    if matrix.guarded is None or len(matrix) == 0:
        return {name: 0.0 for name in matrix.feature_names}
    frac = matrix.guarded.mean(axis=0)
    return {name: float(frac[i]) for i, name in enumerate(matrix.feature_names)}


def group_neutral_fractions(matrix: FeatureMatrix) -> dict[str, float]:
    """Per group, the share of it that fell back to a neutral on an average row.

    This is the number that decides whether a group is a feature group or a
    constant with extra steps.  A group sitting at 0.9 is not describing the
    market; it is describing the venue's emptiness, and the report should say so
    rather than let a tree split on a constant.

    It is a mean over features *and* rows rather than the stricter "every
    feature neutral" — the strict form is :func:`group_all_neutral_fractions`.
    Both are worth printing, because a group can be 7/8 dead while its eighth
    feature is precisely the one saying the other seven are meaningless
    (``active_frac`` is exactly that feature).
    """
    if matrix.guarded is None or len(matrix) == 0:
        return {g: 0.0 for g in matrix.groups()}
    out: dict[str, float] = {}
    for group, names in matrix.groups().items():
        idx = [matrix.feature_names.index(n) for n in names]
        out[group] = float(matrix.guarded[:, idx].mean())
    return out


def group_all_neutral_fractions(matrix: FeatureMatrix) -> dict[str, float]:
    """Per group, the fraction of rows where **every** one of its features was neutral."""
    if matrix.guarded is None or len(matrix) == 0:
        return {g: 0.0 for g in matrix.groups()}
    out: dict[str, float] = {}
    for group, names in matrix.groups().items():
        idx = [matrix.feature_names.index(n) for n in names]
        out[group] = float(matrix.guarded[:, idx].all(axis=1).mean())
    return out


# --------------------------------------------------------------------------- #
# Label duck-typing
# --------------------------------------------------------------------------- #

#: The one outcome that means the trade reached its NET target before its stop.
_WIN_OUTCOME = "TP_BEFORE_SL"


def _outcome_name(label: Any) -> str:
    outcome = getattr(label, "outcome", None)
    return str(getattr(outcome, "value", outcome))


def _label_y(label: Any) -> int:
    """1 when the label's barrier resolved in our favour, 0 otherwise.

    ``TIMEOUT`` and ``NO_EDGE`` are **not** wins.  A trade that ran out of clock
    without clearing its own round trip lost money on this venue (fees are paid
    on the way out regardless), so folding them in with the winners would train
    the model on a target it will not be paid for.
    """
    return 1 if _outcome_name(label) == _WIN_OUTCOME else 0


# --------------------------------------------------------------------------- #
# Per-interval extras: what features_mtf does not publish
# --------------------------------------------------------------------------- #


@dataclass
class _ExtraState:
    """Trailing windows for the per-interval features this module adds.

    Bounded deques only, folded strictly in bar-close order, exactly like
    ``features_mtf._IntervalState``: a 120-day build evaluates ~170k instants
    and re-deriving a window at each one would make the build impossible rather
    than merely slow.
    """

    prev_close: float = 0.0
    bars_seen: int = 0
    highs: Deque[float] = field(default_factory=lambda: deque(maxlen=RANGE_WINDOW))
    lows: Deque[float] = field(default_factory=lambda: deque(maxlen=RANGE_WINDOW))
    ranges: Deque[float] = field(default_factory=lambda: deque(maxlen=SLOW_WINDOW))
    log_returns: Deque[float] = field(default_factory=lambda: deque(maxlen=SLOW_WINDOW))
    trade_counts: Deque[float] = field(default_factory=lambda: deque(maxlen=SLOW_WINDOW))
    quote_vols: Deque[float] = field(default_factory=lambda: deque(maxlen=SLOW_WINDOW))
    buy_notional: Deque[float] = field(default_factory=lambda: deque(maxlen=CUMFLOW_WINDOW))
    tot_notional: Deque[float] = field(default_factory=lambda: deque(maxlen=CUMFLOW_WINDOW))
    #: ``base -> (value, guarded)``, refreshed once per folded bar.
    out: dict[str, tuple[float, bool]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.out:
            self.out = _neutral_extras()


def _neutral_extras() -> dict[str, tuple[float, bool]]:
    return {
        "dist_high": (0.0, True),
        "dist_low": (0.0, True),
        "range_compress": (1.0, True),
        "vol_expand": (1.0, True),
        "trade_accel": (1.0, True),
        "cum_flow": (0.0, True),
        "active_frac": (0.0, True),
    }


def _fold_extras(st: _ExtraState, bar: Any) -> None:
    close = _finite(getattr(bar, "close", 0.0))
    if close <= 0:
        return
    high = _finite(getattr(bar, "high", close), close)
    low = _finite(getattr(bar, "low", close), close)
    quote_volume = max(0.0, _finite(getattr(bar, "quote_volume", 0.0)))
    taker_buy = _clamp(_finite(getattr(bar, "taker_buy_quote", 0.0)), 0.0, quote_volume)
    trades = float(max(0, int(getattr(bar, "trades", 0) or 0)))

    out = _neutral_extras()

    # --- distance from the trailing high / low ---------------------------- #
    st.highs.append(high)
    st.lows.append(low)
    if len(st.highs) >= RANGE_WINDOW:
        hi = max(st.highs)
        lo = min(st.lows)
        # hi and lo come from the same bars as close, so both are > 0 here; the
        # guards are belt-and-braces against a corrupt bar rather than warm-up.
        out["dist_high"] = (
            ((close - hi) / hi * _BPS, False) if hi > 0 else (0.0, True)
        )
        out["dist_low"] = (
            ((close - lo) / lo * _BPS, False) if lo > 0 else (0.0, True)
        )

    # --- range compression ------------------------------------------------- #
    st.ranges.append((high - low) / close * _BPS if close > 0 else 0.0)
    rng = list(st.ranges)
    value, guarded = _guarded_ratio(rng[-FAST_WINDOW:], rng)
    out["range_compress"] = (value, guarded)

    # --- volatility expansion ---------------------------------------------- #
    if st.prev_close > 0:
        st.log_returns.append(math.log(close / st.prev_close))
    lr = list(st.log_returns)
    if len(lr) >= SLOW_WINDOW:
        slow = _stdev(lr)
        fast = _stdev(lr[-FAST_WINDOW:])
        # A dead-flat baseline gives no scale to expand against; 1.0 ("the same
        # as usual") is the only honest reading, and 0.0 would read as "vol
        # collapsed" which is a different claim entirely.
        out["vol_expand"] = (
            (min(fast / slow, MAX_RATIO), False) if slow > 0 else (1.0, True)
        )

    # --- trade-count acceleration ------------------------------------------ #
    st.trade_counts.append(trades)
    tc = list(st.trade_counts)
    value, guarded = _guarded_ratio(tc[-FAST_WINDOW:], tc)
    out["trade_accel"] = (value, guarded)

    # --- cumulative signed flow -------------------------------------------- #
    st.buy_notional.append(taker_buy)
    st.tot_notional.append(quote_volume)
    total = sum(st.tot_notional)
    # No flow at all is not "balanced": it is no information.  Both read 0.0,
    # which is the one place a neutral and a real measurement coincide — and a
    # strategy gating on |flow| > threshold treats them identically anyway.
    out["cum_flow"] = (
        ((2.0 * sum(st.buy_notional) - total) / total, False) if total > 0 else (0.0, True)
    )

    # --- how much of the recent tape was alive at all ----------------------- #
    st.quote_vols.append(quote_volume)
    qv = list(st.quote_vols)
    # Always measurable once a single bar exists: "none of the last k bars
    # traded" is a true reading of 0.0, not a fallback.  This is the feature
    # that tells the model when every *other* volume feature is meaningless.
    out["active_frac"] = (sum(1 for v in qv if v > 0) / len(qv), False)

    st.prev_close = close
    st.bars_seen += 1
    st.out = out


# --------------------------------------------------------------------------- #
# Cross-sectional context: BTC direction and risk-on / risk-off
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Context:
    """Trailing, grid-sampled cross-symbol state.

    ``ts`` is sorted; a row at time T reads the last grid point whose bar had
    already *closed* at or before T, so the context can never see the future
    any more than the per-symbol features can.
    """

    ts: "np.ndarray"
    breadth: "np.ndarray"     # -1..1, share of symbols advancing
    btc_ret: "np.ndarray"     # BTC's trailing move, bps
    n_symbols: int
    has_btc: bool
    signature: str


def _context_interval(datasets: Mapping[str, Dataset]) -> str | None:
    for iv in CONTEXT_INTERVAL_PREFERENCE:
        if all(ds.bars.get(iv) for ds in datasets.values()):
            return iv
    return None


def _build_context(datasets: Mapping[str, Dataset]) -> _Context:
    """Sample each symbol's trailing return on a shared grid, then aggregate."""
    interval = _context_interval(datasets)
    btc_symbol = next((s for s in datasets if s.upper() in BTC_SYMBOLS), None)
    if interval is None:
        return _Context(
            ts=np.zeros(0, dtype=np.int64),
            breadth=np.zeros(0),
            btc_ret=np.zeros(0),
            n_symbols=len(datasets),
            has_btc=btc_symbol is not None,
            signature="none",
        )

    per_symbol: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for symbol, ds in datasets.items():
        bars = ds.bars.get(interval) or []
        closes = np.array([_finite(b.close) for b in bars], dtype=np.float64)
        times = np.array([int(b.close_ms) for b in bars], dtype=np.int64)
        ret = np.zeros(closes.shape[0], dtype=np.float64)
        k = CONTEXT_LOOKBACK_BARS
        if closes.shape[0] > k:
            prev = closes[:-k]
            with np.errstate(divide="ignore", invalid="ignore"):
                r = np.where(prev > 0, (closes[k:] / np.where(prev > 0, prev, 1.0) - 1.0) * _BPS, 0.0)
            ret[k:] = np.clip(np.nan_to_num(r, nan=0.0, posinf=0.0, neginf=0.0), -MAX_BPS, MAX_BPS)
        per_symbol[symbol] = (times, ret)

    grid = np.unique(np.concatenate([t for t, _ in per_symbol.values()])) if per_symbol else np.zeros(0, dtype=np.int64)
    if grid.size == 0:
        return _Context(
            ts=grid, breadth=np.zeros(0), btc_ret=np.zeros(0),
            n_symbols=len(datasets), has_btc=btc_symbol is not None, signature="empty",
        )

    signs = np.zeros((len(per_symbol), grid.size), dtype=np.float64)
    seen = np.zeros((len(per_symbol), grid.size), dtype=bool)
    btc_ret = np.zeros(grid.size, dtype=np.float64)
    for i, (symbol, (times, ret)) in enumerate(per_symbol.items()):
        if times.size == 0:
            continue
        idx = np.searchsorted(times, grid, side="right") - 1
        valid = idx >= 0
        aligned = np.where(valid, ret[np.clip(idx, 0, max(times.size - 1, 0))], 0.0)
        signs[i] = np.sign(aligned)
        seen[i] = valid
        if symbol == btc_symbol:
            btc_ret = aligned

    counts = seen.sum(axis=0)
    with np.errstate(invalid="ignore"):
        breadth = np.where(counts > 0, (signs * seen).sum(axis=0) / np.maximum(counts, 1), 0.0)
    breadth = np.clip(np.nan_to_num(breadth), -1.0, 1.0)

    signature = f"{interval}:{'|'.join(sorted(datasets))}:{grid.size}"
    return _Context(
        ts=grid.astype(np.int64),
        breadth=breadth,
        btc_ret=btc_ret,
        n_symbols=len(datasets),
        has_btc=btc_symbol is not None,
        signature=signature,
    )


# --------------------------------------------------------------------------- #
# Roll's effective-spread estimator
# --------------------------------------------------------------------------- #


def _roll_spread_bps(prices: Sequence[float]) -> tuple[float, bool]:
    """Effective spread from the bid-ask bounce in a trailing print window.

    Roll (1984): consecutive transaction-price changes are negatively
    autocorrelated by exactly the amount the quote bounces, so
    ``spread = 2 * sqrt(-cov(dP_t, dP_{t-1}))``.  When the covariance comes out
    **positive** the model does not apply — a trending window rather than a
    bouncing one — and the honest answer is the documented default rather than
    an imaginary number.  Returns ``(bps, guarded)``.
    """
    n = len(prices)
    if n < ROLL_TRADES // 2:
        return DEFAULT_SPREAD_BPS, True
    diffs = [prices[i] - prices[i - 1] for i in range(1, n)]
    m = len(diffs)
    if m < 3:
        return DEFAULT_SPREAD_BPS, True
    mean = sum(diffs) / m
    var = sum((d - mean) ** 2 for d in diffs) / m
    cov = sum((diffs[i] - mean) * (diffs[i - 1] - mean) for i in range(1, m)) / (m - 1)
    # A trending window has a non-negative covariance and the model does not
    # apply.  The relative floor matters as much as the sign: a perfectly
    # uniform ramp has a covariance that is zero only up to floating-point
    # noise, and taking the square root of that noise would manufacture a
    # spread out of rounding error.
    if var <= 0 or cov >= -1e-9 * var:
        return DEFAULT_SPREAD_BPS, True
    mid = sum(prices) / n
    if mid <= 0:
        return DEFAULT_SPREAD_BPS, True
    bps = 2.0 * math.sqrt(-cov) / mid * _BPS
    if not math.isfinite(bps) or bps < MIN_SPREAD_BPS:
        # Below the floor there is no bounce to measure; clamping *up* to the
        # floor would report a measurement that was never made.
        return DEFAULT_SPREAD_BPS, True
    return min(bps, MAX_SPREAD_BPS), False


# --------------------------------------------------------------------------- #
# The per-symbol builder
# --------------------------------------------------------------------------- #


class _SymbolBuilder:
    """Folds one symbol's dataset forward and emits a row at each instant.

    Instants must arrive in non-decreasing order — every cursor here is
    forward-only, which is both the speed and the no-leakage guarantee.
    """

    def __init__(
        self,
        dataset: Dataset,
        *,
        intervals: Sequence[str],
        feature_names: Sequence[str],
        groups: Sequence[str],
        settings: Settings,
        context: _Context,
    ) -> None:
        self.ds = dataset
        self.symbol = dataset.symbol
        self.intervals = tuple(intervals)
        self.feature_names = list(feature_names)
        self.groups = frozenset(groups)
        self.settings = settings
        self.context = context

        self._mtf = MTFFeatureEngine(settings=settings, intervals=self.intervals)
        regime_interval = "1m" if "1m" in self.intervals else self.intervals[0]
        self._regime = Regime7Classifier(interval=regime_interval, settings=settings)
        self._regime_interval = regime_interval
        self._fine = min(self.intervals, key=interval_ms)

        self._extras: dict[str, _ExtraState] = {iv: _ExtraState() for iv in self.intervals}
        self._extra_cursor: dict[str, int] = {iv: 0 for iv in self.intervals}

        # tape cursors (Roll spread, trade-through rate)
        self._trade_cursor = 0
        self._prices: Deque[float] = deque(maxlen=ROLL_TRADES)
        self._through: Deque[float] = deque(maxlen=TRADE_THROUGH_WINDOW)

        # book cursors (depth shape, change, withdrawal)
        self._book_cursor = 0
        self._book: Any | None = None
        self._prev_depth: float | None = None
        self._depth_hist: Deque[float] = deque(maxlen=BOOK_RECENT)

        # activity regime
        self._act_cursor = 0
        self._act_hour: int | None = None
        self._act_running = 0.0
        self._act_hours: Deque[float] = deque(maxlen=ACTIVITY_HOURS)
        self._act_last: float | None = None

        self._families = _load_families() if "score" in self.groups else ()
        self._fees = settings.fees
        self._ref_notional = _ref_notional(settings)
        self._ctx_cursor = 0

    # -- public ------------------------------------------------------------ #

    def row(self, ts_ms: int) -> tuple[list[float], list[bool]]:
        ts = int(ts_ms)
        self._advance_tape(ts)
        self._advance_extras(ts)
        self._advance_activity(ts)

        mtf = self._mtf.update(self.ds, ts)
        values: dict[str, float] = {}
        guarded: dict[str, bool] = {}

        if self.groups & {"momentum", "volatility", "volume"}:
            self._emit_intervals(mtf, values, guarded)
        if "book" in self.groups:
            self._emit_book(mtf, values, guarded)
        if "regime" in self.groups:
            self._emit_regime(mtf, ts, values, guarded)
        if "cost" in self.groups:
            self._emit_cost(mtf, values, guarded)
        if "time" in self.groups:
            self._emit_time(ts, values, guarded)
        if "score" in self.groups:
            self._emit_score(mtf, values, guarded)

        out_v: list[float] = []
        out_g: list[bool] = []
        for name in self.feature_names:
            value = _finite(values.get(name, NEUTRALS.get(name, 0.0)), NEUTRALS.get(name, 0.0))
            cap = _CAPS.get(name, MAX_BPS)
            out_v.append(_clamp(value, -cap, cap))
            out_g.append(bool(guarded.get(name, True)))
        return out_v, out_g

    # -- cursors ------------------------------------------------------------ #

    def _advance_tape(self, ts: int) -> None:
        """Fold prints and book snapshots that had already happened at ``ts``.

        Trades and books are merged in time order so a print is always compared
        against the book that was prevailing *when it printed*, never against a
        later one.
        """
        trades = self.ds.trades
        books = self.ds.books
        nt, nb = len(trades), len(books)
        while True:
            t_ok = self._trade_cursor < nt and trades[self._trade_cursor].ts_ms <= ts
            b_ok = self._book_cursor < nb and books[self._book_cursor].last_update_ms <= ts
            if not t_ok and not b_ok:
                break
            take_trade = t_ok and (
                not b_ok
                or trades[self._trade_cursor].ts_ms
                <= books[self._book_cursor].last_update_ms
            )
            if take_trade:
                trade = trades[self._trade_cursor]
                self._trade_cursor += 1
                price = _finite(trade.price)
                if price > 0:
                    self._prices.append(price)
                    book = self._book
                    if book is not None:
                        bid, ask = book.best_bid, book.best_ask
                        through = 1.0 if (ask > 0 and price > ask) or (bid > 0 and price < bid) else 0.0
                        self._through.append(through)
            else:
                book = books[self._book_cursor]
                self._book_cursor += 1
                if not book.is_valid():
                    continue
                self._book = book
                depth = max(0.0, _finite(book.depth_usd("bid", DEPTH_BPS))) + max(
                    0.0, _finite(book.depth_usd("ask", DEPTH_BPS))
                )
                self._prev_depth = self._depth_hist[-1] if self._depth_hist else None
                self._depth_hist.append(depth)

    def _advance_extras(self, ts: int) -> None:
        for iv in self.intervals:
            bars = self.ds.bars.get(iv) or []
            i = self._extra_cursor[iv]
            n = len(bars)
            st = self._extras[iv]
            while i < n and bars[i].close_ms <= ts:
                _fold_extras(st, bars[i])
                i += 1
            self._extra_cursor[iv] = i

    def _advance_activity(self, ts: int) -> None:
        """Accumulate completed clock hours of trade count on the fine grid."""
        bars = self.ds.bars.get(self._fine) or []
        i = self._act_cursor
        n = len(bars)
        while i < n and bars[i].close_ms <= ts:
            bar = bars[i]
            hour = int(bar.close_ms) // _HOUR_MS
            if self._act_hour is None:
                self._act_hour = hour
            elif hour != self._act_hour:
                self._act_hours.append(self._act_running)
                self._act_last = self._act_running
                self._act_running = 0.0
                self._act_hour = hour
            self._act_running += float(max(0, int(bar.trades or 0)))
            i += 1
        self._act_cursor = i

    # -- emitters ------------------------------------------------------------ #

    def _emit_intervals(
        self, mtf: MTFFeatures, values: dict[str, float], guarded: dict[str, bool]
    ) -> None:
        want_mom = "momentum" in self.groups
        want_vol = "volatility" in self.groups
        want_volume = "volume" in self.groups
        for iv in self.intervals:
            st = self._extras[iv]
            extras = st.out

            def mtf_put(base: str) -> float:
                """Copy a published feature across, inferring its guard.

                ``features_mtf`` returns its documented neutral during warm-up
                and does not report *that* it did, so equality against the
                neutral is the only signal available.  For a signed base that
                cannot tell a warm-up apart from a true reading of zero, which
                is why the report says so next to the number.
                """
                name = feature_name(base, iv)
                neutral = NEUTRALS.get(name, 0.0)
                value = _finite(mtf.get(name, neutral), neutral)
                values[name] = value
                guarded[name] = value == neutral
                return value

            atr = _finite(mtf.get(feature_name("atr", iv), 0.0))

            if want_mom:
                for base in (
                    "ret", "ret5", "mom_slope", "accel", "ema_slope",
                    "vwap_dist", "rsi", "zscore", "range_pos", "breakout_dist",
                ):
                    mtf_put(base)
                breakout = values.get(feature_name("breakout_dist", iv), 0.0)
                name = feature_name("breakout_strength", iv)
                if atr > MIN_ATR_BPS:
                    values[name] = _clamp(breakout / atr, -MAX_RATIO, MAX_RATIO)
                    guarded[name] = False
                else:
                    # No measured ATR yet: a breakout has no scale to be strong
                    # against, and dividing by a warm-up zero is exactly the
                    # class of artefact this module exists to refuse.
                    values[name] = 0.0
                    guarded[name] = True
                for base in ("dist_high", "dist_low"):
                    v, g = extras[base]
                    values[feature_name(base, iv)] = v
                    guarded[feature_name(base, iv)] = g

            if want_vol:
                for base in ("rv", "rvol_pct", "atr"):
                    mtf_put(base)
                for base in ("range_compress", "vol_expand"):
                    v, g = extras[base]
                    values[feature_name(base, iv)] = v
                    guarded[feature_name(base, iv)] = g

            if want_volume:
                for base in ("vol_accel", "vol_pct", "trades", "aggr_imb"):
                    mtf_put(base)
                for base in ("trade_accel", "cum_flow", "active_frac"):
                    v, g = extras[base]
                    values[feature_name(base, iv)] = v
                    guarded[feature_name(base, iv)] = g
                # Price response: how far the bar moved per unit of *relative*
                # volume.  vol_accel is already neutral-guarded and capped, so
                # the denominator can never be the raw zero that produced the
                # 1291x readings; the floor keeps a 0.05x baseline from turning
                # a 1 bp move into a 20 bp one.
                ret_bps = abs(_finite(mtf.get(feature_name("ret", iv), 0.0)))
                accel = _finite(mtf.get(feature_name("vol_accel", iv), 1.0), 1.0)
                name = feature_name("vol_price_resp", iv)
                values[name] = _clamp(ret_bps / max(accel, MIN_VOL_ACCEL), -MAX_BPS, MAX_BPS)
                guarded[name] = bool(guarded.get(feature_name("vol_accel", iv), True))

    def _emit_book(
        self, mtf: MTFFeatures, values: dict[str, float], guarded: dict[str, bool]
    ) -> None:
        book = self._book
        for name in ("spread_bps", "depth_bid_usd", "depth_ask_usd",
                     "book_imbalance", "microprice_dist_bps"):
            neutral = NEUTRALS.get(name, 0.0)
            value = _finite(mtf.get(name, neutral), neutral)
            values[name] = value
            guarded[name] = book is None

        if book is None:
            # Reached only if the caller asked for ``book`` on a dataset whose
            # book history runs out mid-window; the group is dropped outright
            # when there is no L2 at all.
            values.update({
                "depth_slope": 1.0, "depth_change": 1.0, "book_pressure": 0.0,
                "trade_through_rate": 0.0, "liq_withdrawal": 0.0,
            })
            guarded.update({n: True for n in (
                "depth_slope", "depth_change", "book_pressure",
                "trade_through_rate", "liq_withdrawal")})
            return

        near = max(0.0, _finite(book.depth_usd("bid", NEAR_BPS))) + max(
            0.0, _finite(book.depth_usd("ask", NEAR_BPS))
        )
        wide = self._depth_hist[-1] if self._depth_hist else 0.0

        # How much more size sits at 10 bps than at 2 bps.  A flat or
        # unmeasurable book reports 1.0 ("no shape"), never a division by an
        # empty touch.
        if near > 0 and wide > 0:
            values["depth_slope"] = min(wide / near, MAX_RATIO)
            guarded["depth_slope"] = False
        else:
            values["depth_slope"] = 1.0
            guarded["depth_slope"] = True

        prev = self._prev_depth
        if prev is not None and prev > 0 and wide > 0:
            values["depth_change"] = min(wide / prev, MAX_RATIO)
            guarded["depth_change"] = False
        else:
            values["depth_change"] = 1.0
            guarded["depth_change"] = True

        bid_near = max(0.0, _finite(book.depth_usd("bid", NEAR_BPS)))
        ask_near = max(0.0, _finite(book.depth_usd("ask", NEAR_BPS)))
        if bid_near + ask_near > 0:
            values["book_pressure"] = _clamp(
                (bid_near - ask_near) / (bid_near + ask_near), -1.0, 1.0
            )
            guarded["book_pressure"] = False
        else:
            values["book_pressure"] = 0.0
            guarded["book_pressure"] = True

        if self._through:
            values["trade_through_rate"] = sum(self._through) / len(self._through)
            guarded["trade_through_rate"] = False
        else:
            # No prints since the first book: 0.0 reads as "no trade-throughs
            # observed", which is also what the guard means.  The collision is
            # benign — a gate on ``rate > threshold`` treats them identically —
            # and the guard mask keeps the two separable in the report.
            values["trade_through_rate"] = 0.0
            guarded["trade_through_rate"] = True

        if len(self._depth_hist) >= 2:
            peak = max(self._depth_hist)
            values["liq_withdrawal"] = (
                _clamp(1.0 - wide / peak, 0.0, 1.0) if peak > 0 else 0.0
            )
            guarded["liq_withdrawal"] = peak <= 0
        else:
            values["liq_withdrawal"] = 0.0
            guarded["liq_withdrawal"] = True

    def _emit_regime(
        self, mtf: MTFFeatures, ts: int, values: dict[str, float], guarded: dict[str, bool]
    ) -> None:
        regime, confidence = self._regime.classify(mtf)
        # Confidence 0.0 is the classifier's documented "no opinion" signal, so
        # it is exactly the condition the guard mask should record.
        warm = confidence <= 0.0
        values["regime_trend"] = _TREND_SIGN.get(regime, 0.0)
        values["regime_is_range"] = 1.0 if regime is Regime7.RANGING else 0.0
        values["regime_vol_state"] = _VOL_STATE.get(regime, 0.0)
        values["regime_confidence"] = _clamp(_finite(confidence), 0.0, 1.0)
        for name in ("regime_trend", "regime_is_range", "regime_vol_state", "regime_confidence"):
            guarded[name] = warm

        ctx = self.context
        btc_ret = 0.0
        breadth = 0.0
        have = False
        if ctx.ts.size:
            i = self._ctx_cursor
            n = ctx.ts.size
            while i < n and int(ctx.ts[i]) <= ts:
                i += 1
            self._ctx_cursor = i
            if i > 0:
                btc_ret = float(ctx.btc_ret[i - 1])
                breadth = float(ctx.breadth[i - 1])
                have = True

        values["btc_ret_bps"] = _clamp(btc_ret, -MAX_BPS, MAX_BPS) if ctx.has_btc else 0.0
        values["btc_dir"] = float(np.sign(btc_ret)) if ctx.has_btc else 0.0
        guarded["btc_ret_bps"] = guarded["btc_dir"] = not (ctx.has_btc and have)

        # Breadth across one symbol is that symbol's own sign, which is not
        # breadth.  Below two symbols the honest reading is the neutral 0.0, and
        # the report is expected to call the group a constant when it is one.
        usable = have and ctx.n_symbols >= 2
        values["risk_on"] = _clamp(breadth, -1.0, 1.0) if usable else 0.0
        guarded["risk_on"] = not usable

    def _emit_cost(
        self, mtf: MTFFeatures, values: dict[str, float], guarded: dict[str, bool]
    ) -> None:
        symbol = self.symbol
        maker = max(0.0, _finite(self._fees.maker_bps(symbol)))
        taker = max(0.0, _finite(self._fees.taker_bps(symbol)))
        values["fee_maker_bps"] = maker
        values["fee_taker_bps"] = taker
        # The fee schedule is a known constant per symbol, not a measurement,
        # so it is never "guarded" — it is simply not a market observation.
        guarded["fee_maker_bps"] = guarded["fee_taker_bps"] = False

        book = self._book
        if book is not None:
            spread = _finite(mtf.get("spread_bps", DEFAULT_SPREAD_BPS), DEFAULT_SPREAD_BPS)
            spread = _clamp(spread, MIN_SPREAD_BPS, MAX_SPREAD_BPS)
            spread_guarded = False
        else:
            spread, spread_guarded = _roll_spread_bps(list(self._prices))
        values["cost_spread_bps"] = spread
        guarded["cost_spread_bps"] = spread_guarded

        # Read the tape's liveness straight from the extras rather than from
        # ``values``: the cost group must cost correctly even when the caller
        # asked for ``cost`` without ``volume``.
        active = self._extras[self._fine].out["active_frac"][0]
        rv = _finite(mtf.get(feature_name("rv", self._regime_interval), 0.0))
        atr = _finite(mtf.get(feature_name("atr", self._regime_interval), 0.0))

        half_spread = spread / 2.0
        if book is not None:
            depth = self._depth_hist[-1] if self._depth_hist else 0.0
            if depth > 0:
                # Walking the book: impact grows with the share of resting size
                # the order would eat.  Depth is never the denominator unless it
                # is strictly positive.
                slip = half_spread + SLIP_DEPTH_K * spread * (self._ref_notional / depth)
                slip_guarded = False
            else:
                slip = half_spread * SLIP_SPREAD_MULT
                slip_guarded = True
        else:
            # No depth to walk.  docs/ECONOMICS.md measured slippage at ~3x fees
            # and at 57% of the gross loss, so it is modelled from the spread and
            # scaled up as the tape thins rather than assumed away.
            slip = half_spread * (SLIP_SPREAD_MULT + SLIP_THIN_TAPE_MULT * (1.0 - active))
            slip_guarded = spread_guarded
        slip = _clamp(_finite(slip), 0.0, MAX_SLIP_BPS)
        values["slip_est_bps"] = slip
        guarded["slip_est_bps"] = slip_guarded

        # Round trips, in the costing convention of app/ranker.py: the quoted
        # spread and the slippage are costs of *crossing*, so a resting leg pays
        # neither, and each crossing leg pays one half-spread.
        maker_rt = maker + taker + half_spread + slip * 0.5
        taker_rt = 2.0 * taker + 2.0 * half_spread + slip
        values["maker_rt_bps"] = _clamp(maker_rt, 0.0, MAX_BPS)
        values["taker_rt_bps"] = _clamp(taker_rt, 0.0, MAX_BPS)
        guarded["maker_rt_bps"] = guarded["taker_rt_bps"] = slip_guarded

        # Predicted execution method.  Resting wins when the half-spread it
        # saves beats the fee it gives up, and when the tape is not moving fast
        # enough to run the quote over before it fills.
        saves = taker_rt - maker_rt
        calm = rv <= MAKER_MAX_RV_MULT * max(spread, MIN_SPREAD_BPS)
        is_maker = 1.0 if (saves >= MIN_MAKER_EDGE_BPS and calm) else 0.0
        values["exec_is_maker"] = is_maker
        guarded["exec_is_maker"] = rv <= 0.0

        breakeven = maker_rt if is_maker > 0 else taker_rt
        values["breakeven_bps"] = _clamp(breakeven, 0.0, MAX_BPS)
        guarded["breakeven_bps"] = slip_guarded

        # The two ratios that make the number mean something: a break-even of 3
        # bps is trivial where a bar moves 20 and hopeless where it moves 2.
        if atr > MIN_ATR_BPS:
            values["breakeven_over_atr"] = min(breakeven / atr, MAX_RATIO)
            guarded["breakeven_over_atr"] = False
        else:
            values["breakeven_over_atr"] = MAX_RATIO
            # Capped, not zero: "we cannot measure the move" must not read as
            # "the cost is free relative to the move".
            guarded["breakeven_over_atr"] = True
        if rv > 0:
            values["breakeven_over_rv"] = min(breakeven / rv, MAX_RATIO)
            guarded["breakeven_over_rv"] = False
        else:
            values["breakeven_over_rv"] = MAX_RATIO
            guarded["breakeven_over_rv"] = True

    def _emit_time(self, ts: int, values: dict[str, float], guarded: dict[str, bool]) -> None:
        day = ts // _DAY_MS
        hour_frac = (ts % _DAY_MS) / 3_600_000.0        # 0 .. 24
        dow = (day + _EPOCH_WEEKDAY) % 7                # Monday = 0
        dow_frac = dow + hour_frac / 24.0               # 0 .. 7

        # Cyclical, not a raw integer.  Hour 23 and hour 0 are one hour apart
        # and a tree given the integer would have to split them as 23 units
        # apart; sin/cos keeps the distance honest across midnight and across
        # Sunday -> Monday.
        values["hour_sin"] = math.sin(2.0 * math.pi * hour_frac / 24.0)
        values["hour_cos"] = math.cos(2.0 * math.pi * hour_frac / 24.0)
        values["dow_sin"] = math.sin(2.0 * math.pi * dow_frac / 7.0)
        values["dow_cos"] = math.cos(2.0 * math.pi * dow_frac / 7.0)
        values["is_weekend"] = 1.0 if dow >= 5 else 0.0
        for name in ("hour_sin", "hour_cos", "dow_sin", "dow_cos", "is_weekend"):
            guarded[name] = False

        # Activity regime: where the last *completed* hour's print count sits in
        # the trailing day.  A partial hour is never used — it would shrink
        # toward zero at the top of every hour and read as a liquidity collapse.
        if self._act_last is not None and len(self._act_hours) >= MIN_ACTIVITY_HOURS:
            values["activity_regime"] = _clamp(
                _percentile_rank(list(self._act_hours), self._act_last), 0.0, 1.0
            )
            guarded["activity_regime"] = False
        else:
            values["activity_regime"] = 0.5
            guarded["activity_regime"] = True

    def _emit_score(
        self, mtf: MTFFeatures, values: dict[str, float], guarded: dict[str, bool]
    ) -> None:
        """The existing deterministic score, demoted to one feature among many.

        Phase 3 settled that this score ranks *how loudly a feature fired*, not
        whether the trade ends well: raising its threshold from 0.0 to 0.9 cut
        the trade count 24-fold and moved expectancy from -$0.158 to -$0.145.
        It is kept because a model should be able to see what the old system saw
        and decide for itself; it authorises nothing.
        """
        best_long = 0.0
        best_short = 0.0
        firing = 0
        for family in self._families:
            try:
                signal = family.signal(mtf, {})
            except Exception:  # pragma: no cover - a family must never break a build
                signal = None
            if signal is None:
                continue
            firing += 1
            score = _clamp(_finite(getattr(signal, "score", 0.0)), 0.0, 1.0)
            side = str(getattr(getattr(signal, "side", ""), "value", getattr(signal, "side", "")))
            if side.upper().endswith("LONG"):
                best_long = max(best_long, score)
            else:
                best_short = max(best_short, score)
        values["score_long"] = best_long
        values["score_short"] = best_short
        values["score_signed"] = best_long - best_short
        values["score_families"] = float(firing)
        # Silence is a reading — "no family had an opinion here" — but it is the
        # same 0.0 a family scoring zero would produce, so it is flagged.
        for name in SCORE_NAMES:
            guarded[name] = firing == 0


def _load_families() -> tuple[Any, ...]:
    try:
        from ..research.families import FAMILIES
    except Exception:  # pragma: no cover - research extras are optional
        return ()
    return tuple(FAMILIES)


def _ref_notional(settings: Settings) -> float:
    value = getattr(getattr(settings, "portfolio", None), "max_position_usd", None)
    v = _finite(value, DEFAULT_REF_NOTIONAL_USD)
    return v if v > 0 else DEFAULT_REF_NOTIONAL_USD


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #

#: In-process memo: one build over 120 days and a dozen label specs must fold
#: the tape once, not a dozen times.  Keyed exactly like the disk cache.
_MEMO: dict[str, tuple["np.ndarray", "np.ndarray", "np.ndarray", tuple[str, ...]]] = {}
_MEMO_MAX = 8


def clear_matrix_cache() -> None:
    """Drop the in-process memo.  The disk cache is keyed and never stale."""
    _MEMO.clear()


def _dataset_signature(ds: Dataset, intervals: Sequence[str]) -> str:
    """What makes two datasets interchangeable for feature purposes.

    Bar counts are in here on purpose: appending future bars must produce a
    *different* key, or the leakage test would be answered out of the cache and
    would prove nothing.
    """
    parts = [
        ds.symbol,
        str(int(ds.start_ms)),
        str(int(ds.end_ms)),
        str(len(ds.trades)),
        str(len(ds.books)),
    ]
    parts += [f"{iv}:{len(ds.bars.get(iv) or ())}" for iv in intervals]
    return "|".join(parts)


def _cache_key(
    *,
    ds: Dataset,
    intervals: Sequence[str],
    groups: Sequence[str],
    feature_names: Sequence[str],
    ts: "np.ndarray",
    context: _Context,
) -> str:
    h = hashlib.sha1()
    h.update(FEATURE_VERSION.encode())
    h.update(_dataset_signature(ds, intervals).encode())
    h.update(",".join(groups).encode())
    h.update(",".join(feature_names).encode())
    h.update(context.signature.encode())
    h.update(np.ascontiguousarray(ts, dtype=np.int64).tobytes())
    lo = int(ts[0]) if ts.size else 0
    hi = int(ts[-1]) if ts.size else 0
    return f"{ds.symbol}-{lo}-{hi}-{ts.size}-{h.hexdigest()[:16]}"


def _cache_path(cache_dir: str | Path, symbol: str, key: str) -> Path:
    return Path(cache_dir).expanduser() / "predict-matrix" / symbol.upper() / f"{key}.npz"


def _cache_load(path: Path, feature_names: Sequence[str]) -> tuple | None:
    if not path.is_file():
        return None
    try:
        with np.load(path, allow_pickle=False) as data:
            names = tuple(str(n) for n in data["names"])
            if names != tuple(feature_names):
                return None
            meta = json.loads(str(data["meta"].item()))
            if meta.get("feature_version") != FEATURE_VERSION:
                return None
            return data["ts"].copy(), data["X"].copy(), data["G"].copy(), names
    except Exception:  # pragma: no cover - a corrupt cache must only cost time
        return None


def _cache_store(path: Path, ts, X, G, names: Sequence[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # ``savez_compressed`` appends ".npz" unless the name already ends in
        # it, so a ".tmp" suffix here would write a *different* file and the
        # replace below would quietly fail — leaving the cache permanently empty.
        tmp = path.with_name(path.name + ".tmp.npz")
        np.savez_compressed(
            tmp,
            ts=ts,
            X=X,
            G=G,
            names=np.array(list(names), dtype=object).astype("U"),
            meta=np.array(json.dumps({"feature_version": FEATURE_VERSION})),
        )
        tmp.replace(path)
    except OSError:  # pragma: no cover - an unwritable cache is not an error
        pass


# --------------------------------------------------------------------------- #
# build_matrix
# --------------------------------------------------------------------------- #


def _as_datasets(dataset: Any) -> dict[str, Dataset]:
    if isinstance(dataset, Mapping):
        return {str(k).upper(): v for k, v in dataset.items()}
    if isinstance(dataset, Dataset):
        return {dataset.symbol.upper(): dataset}
    if isinstance(dataset, (list, tuple, set)):
        return {ds.symbol.upper(): ds for ds in dataset}
    if hasattr(dataset, "symbol") and hasattr(dataset, "bars"):
        return {str(dataset.symbol).upper(): dataset}
    raise TypeError(f"cannot read a Dataset out of {type(dataset)!r}")


@dataclass
class _LabelColumns:
    """Labels in columnar form, which is the only form that scales here.

    ``label_matrix`` over 120 days x 56 specs is ~10M ``Label`` objects and
    several GB, so ``labels.OutcomeArrays`` exists to avoid materialising them.
    This module accepts either, and converts the object form to columns once.
    """

    symbols: "np.ndarray"       # int32 index into ``symbol_names``
    symbol_names: list[str]
    ts_ms: "np.ndarray"
    spec_codes: "np.ndarray"    # int32 index into ``spec_names``
    spec_names: list[str]
    y: "np.ndarray"
    net_return_bps: "np.ndarray"
    label_end_ms: "np.ndarray"

    def __len__(self) -> int:
        return int(self.ts_ms.size)


def _is_columnar(obj: Any) -> bool:
    return (
        hasattr(obj, "ts_ms")
        and hasattr(obj, "outcome")
        and hasattr(obj, "spec_key")
        and isinstance(getattr(obj, "ts_ms", None), np.ndarray)
    )


def _columnar_block(arrays: Any, symbol: str) -> tuple:
    """One ``OutcomeArrays`` as raw columns, without building any ``Label``."""
    ts = np.asarray(arrays.ts_ms, dtype=np.int64)
    outcome = np.asarray(arrays.outcome)
    # ``labels._OUTCOME_BY_CODE`` puts TP_BEFORE_SL at code 0.  Resolve it by
    # name rather than by position so a reordering there cannot silently invert
    # the target here.
    win_code = _win_code()
    y = (outcome == win_code).astype(np.int8)
    net = np.asarray(getattr(arrays, "net_return_bps", np.zeros(ts.size)), dtype=np.float64)
    end = np.asarray(getattr(arrays, "label_end_ms", ts), dtype=np.int64)
    spec = str(arrays.spec_key)
    return ts, y, net, end, spec, symbol.upper()


def _win_code() -> int:
    try:
        from .labels import _OUTCOME_BY_CODE  # type: ignore[attr-defined]

        for i, outcome in enumerate(_OUTCOME_BY_CODE):
            if str(getattr(outcome, "value", outcome)) == _WIN_OUTCOME:
                return i
    except Exception:  # pragma: no cover - labels is optional at import time
        pass
    return 0


def _normalize_labels(labels: Any, datasets: Mapping[str, Dataset]) -> _LabelColumns:
    """Accept every shape ``labels.py`` hands out, and return columns.

    * ``[Label, ...]`` — the plain contract form.
    * ``{spec_key: [Label, ...]}`` — what ``label_matrix`` returns.
    * ``OutcomeArrays``, a sequence of them, or ``{spec_key: OutcomeArrays}`` —
      the columnar form, which carries no symbol of its own and so is only
      accepted when the dataset names exactly one, or when nested under
      ``{symbol: ...}``.
    """
    blocks: list[tuple] = []
    objects: list[Any] = []

    def absorb(value: Any, symbol_hint: str | None) -> None:
        if _is_columnar(value):
            symbol = symbol_hint or _sole_symbol(datasets, value)
            blocks.append(_columnar_block(value, symbol))
            return
        if isinstance(value, Mapping):
            for k, v in value.items():
                hint = symbol_hint
                if str(k).upper() in datasets:
                    hint = str(k).upper()
                absorb(v, hint)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                absorb(item, symbol_hint)
            return
        objects.append(value)

    absorb(labels, None)

    if objects:
        ts = np.fromiter((int(getattr(o, "ts_ms", 0)) for o in objects), dtype=np.int64, count=len(objects))
        y = np.fromiter((_label_y(o) for o in objects), dtype=np.int8, count=len(objects))
        net = np.fromiter(
            (_finite(getattr(o, "net_return_bps", 0.0)) for o in objects),
            dtype=np.float64, count=len(objects),
        )
        end = np.fromiter(
            (int(getattr(o, "label_end_ms", getattr(o, "ts_ms", 0))) for o in objects),
            dtype=np.int64, count=len(objects),
        )
        specs = [str(getattr(o, "spec_key", "")) for o in objects]
        syms = [str(getattr(o, "symbol", "")).upper() for o in objects]
    else:
        ts = np.zeros(0, dtype=np.int64)
        y = np.zeros(0, dtype=np.int8)
        net = np.zeros(0, dtype=np.float64)
        end = np.zeros(0, dtype=np.int64)
        specs = []
        syms = []

    for b_ts, b_y, b_net, b_end, b_spec, b_sym in blocks:
        ts = np.concatenate([ts, b_ts])
        y = np.concatenate([y, b_y])
        net = np.concatenate([net, b_net])
        end = np.concatenate([end, b_end])
        specs.extend([b_spec] * int(b_ts.size))
        syms.extend([b_sym] * int(b_ts.size))

    symbol_names = sorted(set(syms))
    spec_names = sorted(set(specs))
    sym_index = {s: i for i, s in enumerate(symbol_names)}
    spec_index = {s: i for i, s in enumerate(spec_names)}
    return _LabelColumns(
        symbols=np.fromiter((sym_index[s] for s in syms), dtype=np.int32, count=len(syms)),
        symbol_names=symbol_names,
        ts_ms=ts,
        spec_codes=np.fromiter((spec_index[s] for s in specs), dtype=np.int32, count=len(specs)),
        spec_names=spec_names,
        y=y,
        net_return_bps=net,
        label_end_ms=end,
    )


def _sole_symbol(datasets: Mapping[str, Dataset], value: Any) -> str:
    if len(datasets) == 1:
        return next(iter(datasets))
    raise ValueError(
        "columnar labels carry no symbol; pass them as {symbol: ...} when the "
        f"dataset holds more than one ({sorted(datasets)})"
    )


def _available_intervals(datasets: Mapping[str, Dataset]) -> tuple[str, ...]:
    """Intervals every symbol in the dataset actually carries.

    The intersection, not the union: one matrix has one column set, and a
    column that exists for BTC and not for SOL would have to be imputed for SOL
    — which is precisely the fabrication the book group refuses to do.
    """
    return tuple(iv for iv in INTERVALS if all(ds.bars.get(iv) for ds in datasets.values()))


def _has_book(datasets: Mapping[str, Dataset]) -> bool:
    return all(bool(ds.books) for ds in datasets.values())


def build_matrix(
    dataset: Any,
    labels: Any,
    *,
    groups: Sequence[str] | None = None,
    cache_dir: str | Path | None = None,
    settings: Settings | None = None,
) -> FeatureMatrix:
    """One row per label, every feature as of that label's entry instant.

    ``dataset`` is a :class:`~app.research.dataset.Dataset`, a sequence of them,
    or a ``{symbol: Dataset}`` mapping.  Passing more than one symbol is what
    lets the ``regime`` group say anything about breadth — with a single symbol
    ``risk_on`` is structurally neutral and is reported as such.

    ``labels`` is a list of :class:`~app.predict.labels.Label`, or the
    ``{spec_key: [Label, ...]}`` mapping ``label_matrix`` returns.  Specs that
    share an entry instant share its features: the market looked the same, only
    the barriers differ.

    ``groups`` selects from :data:`FEATURE_GROUPS`; ``None`` means all of them.
    The ``book`` group is dropped — not zero-filled — unless every dataset
    carries live L2.

    ``cache_dir`` enables the on-disk feature cache, keyed by symbol, feature
    version, dataset window and the instants requested.
    """
    started = time.perf_counter()
    settings = settings or get_settings()
    datasets = _as_datasets(dataset)
    if not datasets:
        raise ValueError("build_matrix needs at least one dataset")

    requested = tuple(groups) if groups is not None else GROUP_NAMES
    unknown = [g for g in requested if g not in FEATURE_GROUPS]
    if unknown:
        raise ValueError(f"unknown feature group(s): {unknown}")

    intervals = _available_intervals(datasets)
    if not intervals:
        raise ValueError(
            "no interval is present in every dataset; load at least one shared "
            f"interval from {INTERVALS}"
        )

    book_ok = _has_book(datasets)
    active_groups = tuple(g for g in GROUP_NAMES if g in requested and (g != "book" or book_ok))

    feature_names: list[str] = []
    for group in active_groups:
        bases = _PER_INTERVAL_BASES.get(group)
        if bases is None:
            feature_names.extend(FEATURE_GROUPS[group])
        else:
            feature_names.extend(feature_name(b, iv) for iv in intervals for b in bases)

    n_features = len(feature_names)
    if n_features == 0:
        raise ValueError("the selected groups produced no features")

    cols = _normalize_labels(labels, datasets)
    missing = set(cols.symbol_names) - set(datasets)
    if missing:
        raise ValueError(f"labels reference symbols with no dataset: {sorted(missing)}")

    # -- order the rows ----------------------------------------------------- #
    # Chronological first, so ``slice_ms`` is always a contiguous view; symbol
    # and spec only break ties, so the order is total and reproducible.
    order = np.lexsort((cols.spec_codes, cols.symbols, cols.ts_ms))
    sym_idx = cols.symbols[order]
    ts_ms = cols.ts_ms[order]
    spec_idx = cols.spec_codes[order]
    y = cols.y[order]
    net_bps = cols.net_return_bps[order]
    label_end_ms = cols.label_end_ms[order]

    context = _build_context(datasets)

    # -- features, once per (symbol, instant) -------------------------------- #
    # The specs differ in their barriers, not in what the market looked like at
    # entry, so one instant is folded once however many specs reference it.
    by_symbol: dict[str, np.ndarray] = {}
    for symbol in datasets:
        code = cols.symbol_names.index(symbol) if symbol in cols.symbol_names else -1
        stamps = np.unique(ts_ms[sym_idx == code]) if code >= 0 else np.zeros(0, dtype=np.int64)
        by_symbol[symbol] = stamps.astype(np.int64)

    tables: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    cache_hits = 0
    for symbol, stamps in by_symbol.items():
        if stamps.size == 0:
            tables[symbol] = (
                stamps,
                np.zeros((0, n_features)),
                np.zeros((0, n_features), dtype=bool),
            )
            continue
        ds = datasets[symbol]
        key = _cache_key(
            ds=ds, intervals=intervals, groups=active_groups,
            feature_names=feature_names, ts=stamps, context=context,
        )
        cached = _MEMO.get(key)
        if cached is not None and cached[3] == tuple(feature_names):
            tables[symbol] = (cached[0], cached[1], cached[2])
            cache_hits += 1
            continue
        if cache_dir is not None:
            loaded = _cache_load(_cache_path(cache_dir, symbol, key), feature_names)
            if loaded is not None:
                tables[symbol] = (loaded[0], loaded[1], loaded[2])
                _remember(key, loaded[0], loaded[1], loaded[2], feature_names)
                cache_hits += 1
                continue

        builder = _SymbolBuilder(
            ds, intervals=intervals, feature_names=feature_names,
            groups=active_groups, settings=settings, context=context,
        )
        X = np.empty((stamps.size, n_features), dtype=np.float64)
        G = np.empty((stamps.size, n_features), dtype=bool)
        for i, ts in enumerate(stamps):
            values, guards = builder.row(int(ts))
            X[i, :] = values
            G[i, :] = guards
        tables[symbol] = (stamps, X, G)
        _remember(key, stamps, X, G, feature_names)
        if cache_dir is not None:
            _cache_store(_cache_path(cache_dir, symbol, key), stamps, X, G, feature_names)

    # -- assemble ------------------------------------------------------------ #
    n = int(ts_ms.size)
    X = np.empty((n, n_features), dtype=np.float64)
    G = np.empty((n, n_features), dtype=bool)
    for symbol, (stamps, table, guards) in tables.items():
        if symbol not in cols.symbol_names:
            continue
        code = cols.symbol_names.index(symbol)
        mask = sym_idx == code
        if not mask.any():
            continue
        want = ts_ms[mask]
        j = np.searchsorted(stamps, want)
        if j.size and (j.max(initial=0) >= stamps.size or not np.array_equal(stamps[j], want)):
            raise AssertionError(f"no feature row built for {symbol}")  # pragma: no cover
        X[mask] = table[j]
        G[mask] = guards[j]

    symbols = [cols.symbol_names[i] for i in sym_idx]
    spec_keys = [cols.spec_names[i] for i in spec_idx]

    _assert_clean(X, feature_names)

    elapsed = time.perf_counter() - started
    return FeatureMatrix(
        X=X, y=y, ts_ms=ts_ms, symbols=symbols, feature_names=feature_names,
        label_end_ms=label_end_ms, spec_keys=spec_keys, net_return_bps=net_bps,
        guarded=G, feature_version=FEATURE_VERSION,
        meta={
            "intervals": list(intervals),
            "groups": list(active_groups),
            "book_available": book_ok,
            "symbols": sorted(datasets),
            "context_symbols": context.n_symbols,
            "cache_hits": cache_hits,
            "build_seconds": elapsed,
            "rows": n,
        },
    )


def _remember(key: str, ts, X, G, names: Sequence[str]) -> None:
    if len(_MEMO) >= _MEMO_MAX:
        _MEMO.pop(next(iter(_MEMO)))
    _MEMO[key] = (ts, X, G, tuple(names))


def _assert_clean(X: "np.ndarray", feature_names: Sequence[str]) -> None:
    """No NaN, no inf, nothing absurd — and name the column when it fails.

    A NaN reaching the model poisons every comparison it takes part in and
    silently produces a ranked nonsense winner, so it fails here where the
    feature is still identifiable.
    """
    if X.size == 0:
        return
    bad = ~np.isfinite(X)
    if bad.any():
        cols = sorted({feature_names[c] for c in np.unique(np.where(bad)[1])})
        raise ValueError(f"non-finite features: {cols}")
    huge = np.abs(X) > MAX_ABS_FEATURE
    if huge.any():
        cols = sorted({feature_names[c] for c in np.unique(np.where(huge)[1])})
        raise ValueError(f"absurd feature magnitudes (> {MAX_ABS_FEATURE:g}): {cols}")
