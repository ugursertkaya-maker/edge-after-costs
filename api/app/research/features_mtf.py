"""Multi-timeframe features, computed incrementally and without leakage.

This generalises ``app/features.py`` from "one engine, 1-minute bars, live
snapshot" to "one engine, seven timeframes, an arbitrary evaluation instant
inside a cached dataset".  The shape of the state machine is deliberately the
same, because the same three properties have to hold and the 1m engine already
earned them:

1. **Incremental.**  A 365-day grid search evaluates millions of instants.
   Every statistic here is either an O(1) accumulator (EMA, Wilder ATR, Wilder
   RSI) or a bounded ``deque``, and bars are folded *once*.  Re-deriving a
   window per step would make the tournament impossible, not merely slow.
2. **Neutral at the edges, never NaN.**  Every denominator is guarded and every
   guard returns a documented neutral value: ``0.0`` for signed quantities,
   ``0.5`` for percentiles, ``1.0`` for ratios, ``50.0`` for RSI.  Returning
   ``0.0`` where zero already means something — "volume collapsed", "no
   momentum" — is the failure mode this rule exists to prevent.
3. **Trailing windows only.**  Bars are folded strictly while
   ``bar.close_ms <= index_ms``, so no value can ever see a bar that had not
   closed at the evaluation instant.  There is no full-sample scaler anywhere
   in this module; z-scores and percentiles are computed against the trailing
   window alone.  ``test_research_features.py`` asserts that appending future
   bars leaves a past evaluation byte-identical.

The thin-tape rule
------------------

Binance.US leaves 55% of BTC/USD minutes with no trade at all and 87% of
ADA/USD minutes (``docs/ECONOMICS.md``).  Any ratio taken against a baseline
window that is mostly zeros is arithmetic, not information — the live engine's
``_volume_ratio`` already collapses to a neutral ``1.0`` when fewer than half
the baseline bars are active, and every volume-derived feature here follows
that same precedent rather than reporting a 1,200x "surge" because one print
landed after a quiet hour.

Naming
------

Every bar-derived feature carries its interval as a suffix — ``ret_1m``,
``atr_15m``, ``rsi_5m``, ``vwap_dist_1m`` — so a value is never ambiguous about
which timeframe produced it.  Book-derived features carry no suffix: they are
instantaneous, and they are **omitted entirely** when the dataset has no book
history rather than being faked from the tape.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Iterator, Sequence

from ..config import Settings, get_settings
from ..types import Bar, OrderBook
from .dataset import INTERVALS, Dataset, interval_ms

__all__ = [
    "BOOK_FEATURE_NAMES",
    "FEATURE_BASES",
    "FEATURE_NAMES",
    "NEUTRAL",
    "MTFFeatureEngine",
    "MTFFeatures",
    "assert_mtf_finite",
    "feature_name",
]

# --------------------------------------------------------------------------- #
# Windows.  Module constants so the tests and the dashboard quote one number.
# --------------------------------------------------------------------------- #

ATR_PERIOD = 14
RSI_PERIOD = 14
RV_WINDOW = 20                 # log returns in the realized-vol estimate
RVOL_PCT_WINDOW = 240          # trailing realized-vol observations
MIN_RVOL_PCT_OBS = 30
EMA_FAST_PERIOD = 9
EMA_SLOW_PERIOD = 30
SLOPE_WINDOW = 10              # bars in the OLS momentum slope
RANGE_WINDOW = 20              # breakout / range-position lookback
MR_WINDOW = 20                 # mean-reversion z-score lookback
VWAP_WINDOW = 60
VOL_FAST_WINDOW = 5            # volume acceleration: fast leg
VOL_SLOW_WINDOW = 20           # volume acceleration: baseline leg
VOL_PCT_WINDOW = 60            # trailing distribution for the volume percentile
AGGR_WINDOW = 5                # bars in the aggressor-imbalance sum
RET_LOOKBACK = 5               # bars in the multi-bar return

#: Baseline must be measurable at all before a ratio against it means anything.
#: Same reasoning, and the same halving, as ``app.features.MIN_ACTIVE_BARS``.
MIN_ACTIVE_BARS = VOL_SLOW_WINDOW // 2
#: Beyond this a volume ratio has stopped carrying information.
MAX_VOLUME_RATIO = 20.0
#: A z-score past this is an outlier, not a measurement.
MAX_ZSCORE = 10.0
#: Depth/imbalance band, matching the live engine.
DEPTH_BPS = 10.0

#: Feature engines default to the timeframes a strategy actually decides on.
#: The 5s/15s/30s views exist for the maker workstream and for anything that
#: wants to see inside a minute; they are opt-in because they are expensive.
DEFAULT_INTERVALS: tuple[str, ...] = ("1m", "5m", "15m")

#: Every per-interval feature this engine can produce.
FEATURE_BASES: tuple[str, ...] = (
    "ret",             # last closed bar's return, bps
    "ret5",            # RET_LOOKBACK-bar return, bps
    "mom_slope",       # OLS slope of the last SLOPE_WINDOW closes, bps/bar
    "accel",           # change in that slope since the previous bar, bps/bar^2
    "rv",              # realized vol: stdev of log returns, bps
    "rvol_pct",        # percentile of rv in its own trailing window, 0..1
    "atr",             # Wilder ATR, bps of price
    "vwap_dist",       # distance from the rolling VWAP, bps
    "rsi",             # Wilder RSI, 0..100
    "ema_slope",       # EMA(fast) slope, bps/bar
    "vol_accel",       # fast mean quote volume / slow mean, ratio
    "vol_pct",         # percentile of this bar's quote volume, 0..1
    "trades",          # trade count of the last closed bar
    "aggr_imb",        # taker buy vs sell notional imbalance, -1..1
    "breakout_dist",   # distance outside the prior RANGE_WINDOW range, bps
    "zscore",          # mean-reversion z-score vs the trailing mean
    "range_pos",       # position inside the trailing range, 0..1
)

#: Instantaneous, book-derived.  Present only when the dataset carries a book.
BOOK_FEATURE_NAMES: tuple[str, ...] = (
    "book_imbalance",
    "microprice_dist_bps",
    "spread_bps",
    "depth_bid_usd",
    "depth_ask_usd",
)

#: What each base means when it cannot be measured yet.  Anything absent here
#: is a signed quantity whose neutral value is 0.0.
NEUTRAL: dict[str, float] = {
    "rvol_pct": 0.5,
    "vol_pct": 0.5,
    "range_pos": 0.5,
    "rsi": 50.0,
    "vol_accel": 1.0,
}


def feature_name(base: str, interval: str) -> str:
    return f"{base}_{interval}"


def neutral_value(base: str) -> float:
    return NEUTRAL.get(base, 0.0)


FEATURE_NAMES: tuple[str, ...] = tuple(
    [feature_name(b, iv) for iv in INTERVALS for b in FEATURE_BASES]
    + list(BOOK_FEATURE_NAMES)
)

_FEATURE_NAME_SET = frozenset(FEATURE_NAMES)


# --------------------------------------------------------------------------- #
# Small numeric helpers (mirrors of app/features.py, kept local on purpose:
# the research package must stay importable without the live engine)
# --------------------------------------------------------------------------- #


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _finite(x: Any, default: float = 0.0) -> float:
    if x is None:
        return default
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return default
    return default if (math.isnan(xf) or math.isinf(xf)) else xf


def _stdev(values: Sequence[float]) -> float:
    """Sample standard deviation (ddof=1).  0.0 for fewer than two points."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(var) if var > 0 else 0.0


def _percentile_rank(window: Sequence[float], x: float) -> float:
    """Fraction of ``window`` below ``x``; ties count half.  Neutral 0.5."""
    n = len(window)
    if n == 0:
        return 0.5
    below = 0
    equal = 0
    for v in window:
        if v < x:
            below += 1
        elif v == x:
            equal += 1
    return (below + 0.5 * equal) / n


def _ols_slope(values: Sequence[float]) -> float:
    """Least-squares slope of ``values`` against 0..n-1, in price units/bar."""
    n = len(values)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2.0
    mean_y = sum(values) / n
    var_x = n * (n * n - 1) / 12.0
    if var_x <= 0:
        return 0.0
    cov = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values))
    return cov / var_x


# --------------------------------------------------------------------------- #
# The contract
# --------------------------------------------------------------------------- #


@dataclass
class MTFFeatures:
    """Every named feature at one instant, already normalised."""

    symbol: str
    ts_ms: int
    price: float
    values: dict[str, float] = field(default_factory=dict)

    def get(self, name: str, default: float = 0.0) -> float:
        v = self.values.get(name)
        return default if v is None else v


def assert_mtf_finite(f: MTFFeatures) -> None:
    """Raise ``ValueError`` if anything in ``f`` is NaN or infinite.

    Called on every :meth:`MTFFeatureEngine.update`.  A NaN reaching the
    tournament poisons every comparison it participates in and produces a
    silently-ranked nonsense winner, so it fails here where the symbol and the
    instant are still known.
    """
    bad: list[str] = []
    for label, value in (
        ("price", f.price),
        *((k, v) for k, v in f.values.items()),
    ):
        try:
            x = float(value)
        except (TypeError, ValueError):
            bad.append(label)
            continue
        if math.isnan(x) or math.isinf(x):
            bad.append(label)
    if bad:
        raise ValueError(f"non-finite MTF features for {f.symbol}: {', '.join(bad)}")


# --------------------------------------------------------------------------- #
# Per-interval rolling state
# --------------------------------------------------------------------------- #


@dataclass
class _IntervalState:
    """Everything remembered about one timeframe between evaluation instants."""

    bars_seen: int = 0
    last_close_ms: int = -1
    prev_close: float = 0.0
    last_close: float = 0.0

    # Wilder ATR
    atr: float = 0.0
    atr_ready: bool = False
    tr_seed: list[float] = field(default_factory=list)

    # Wilder RSI
    avg_gain: float = 0.0
    avg_loss: float = 0.0
    rsi_ready: bool = False
    rsi_seed: list[float] = field(default_factory=list)

    # EMA accumulators
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    ema_fast_prev: float = 0.0

    # Bounded windows
    closes: Deque[float] = field(
        default_factory=lambda: deque(
            maxlen=max(SLOPE_WINDOW, MR_WINDOW, RET_LOOKBACK + 1) + 1
        )
    )
    highs: Deque[float] = field(default_factory=lambda: deque(maxlen=RANGE_WINDOW))
    lows: Deque[float] = field(default_factory=lambda: deque(maxlen=RANGE_WINDOW))
    log_returns: Deque[float] = field(default_factory=lambda: deque(maxlen=RV_WINDOW))
    rv_hist: Deque[float] = field(default_factory=lambda: deque(maxlen=RVOL_PCT_WINDOW))
    quote_vols: Deque[float] = field(
        default_factory=lambda: deque(maxlen=max(VOL_SLOW_WINDOW, VOL_PCT_WINDOW) + 1)
    )
    aggr_buy: Deque[float] = field(default_factory=lambda: deque(maxlen=AGGR_WINDOW))
    aggr_total: Deque[float] = field(default_factory=lambda: deque(maxlen=AGGR_WINDOW))
    vwap_pv: Deque[float] = field(default_factory=lambda: deque(maxlen=VWAP_WINDOW))
    vwap_vol: Deque[float] = field(default_factory=lambda: deque(maxlen=VWAP_WINDOW))

    prev_slope: float = 0.0

    #: Cached output, refreshed once per folded bar.
    out: dict[str, float] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #


class MTFFeatureEngine:
    """Folds a :class:`Dataset` forward and reports features at any instant."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        intervals: Sequence[str] = DEFAULT_INTERVALS,
    ) -> None:
        self.settings = settings or get_settings()
        self.intervals: tuple[str, ...] = tuple(intervals)
        for iv in self.intervals:
            interval_ms(iv)  # fail loudly, at construction, on a typo
        #: Finest configured timeframe — the one whose close is "the price".
        self._price_interval = min(self.intervals, key=interval_ms)
        self._state: dict[str, _IntervalState] = {}
        self._cursor: dict[str, int] = {}
        self._book_cursor = 0
        self._book: OrderBook | None = None
        self._ds_key: tuple | None = None
        self._last_index_ms = -1
        self.reset()

    # -- public API -------------------------------------------------------- #

    def reset(self) -> None:
        """Forget every accumulator.  Cheap; called on any backwards seek."""
        self._state = {iv: _IntervalState() for iv in self.intervals}
        for iv in self.intervals:
            self._state[iv].out = self._neutral_out(iv)
        self._cursor = {iv: 0 for iv in self.intervals}
        self._book_cursor = 0
        self._book = None
        self._ds_key = None
        self._last_index_ms = -1

    def update(self, dataset: Dataset, index_ms: int) -> MTFFeatures:
        """Features for ``dataset`` as they stood at ``index_ms``.

        Only bars that had already **closed** at ``index_ms`` are folded, which
        is the whole no-leakage guarantee.  Calls that move forward in time cost
        O(new bars); a call that moves backwards rebuilds from scratch, which is
        correct but is why ``stream`` exists.
        """
        index_ms = int(index_ms)
        key = (id(dataset), dataset.symbol, dataset.start_ms, dataset.end_ms)
        if key != self._ds_key or index_ms < self._last_index_ms:
            self.reset()
            self._ds_key = key
        self._last_index_ms = index_ms

        for iv in self.intervals:
            bars = dataset.bars.get(iv) or []
            st = self._state[iv]
            i = self._cursor[iv]
            n = len(bars)
            while i < n and bars[i].close_ms <= index_ms:
                self._fold(st, bars[i], iv)
                i += 1
            self._cursor[iv] = i

        values: dict[str, float] = {}
        for iv in self.intervals:
            values.update(self._state[iv].out)

        book = self._book_at(dataset, index_ms)
        if book is not None:
            values.update(_book_features(book))

        price = self._price()
        f = MTFFeatures(
            symbol=dataset.symbol,
            ts_ms=index_ms,
            price=price,
            values=values,
        )
        assert_mtf_finite(f)
        return f

    def stream(self, dataset: Dataset, *, step: str = "1m") -> Iterator[MTFFeatures]:
        """Yield features at every ``step`` boundary, in time order.

        Uses the dataset's own ``step`` bars when it has them so evaluation
        instants line up exactly with bar closes; otherwise it walks an absolute
        epoch grid, which keeps instants reproducible across datasets.
        """
        step_ms = interval_ms(step)
        bars = dataset.bars.get(step)
        if bars:
            for bar in bars:
                yield self.update(dataset, bar.close_ms)
            return
        t = (dataset.start_ms // step_ms) * step_ms + step_ms - 1
        while t <= dataset.end_ms:
            yield self.update(dataset, t)
            t += step_ms

    # -- internals --------------------------------------------------------- #

    def _price(self) -> float:
        st = self._state.get(self._price_interval)
        if st is not None and st.last_close > 0:
            return st.last_close
        for iv in self.intervals:
            s = self._state[iv]
            if s.last_close > 0:
                return s.last_close
        return 0.0

    def _book_at(self, dataset: Dataset, index_ms: int) -> OrderBook | None:
        books = dataset.books
        if not books:
            return None
        i = self._book_cursor
        n = len(books)
        while i < n and books[i].last_update_ms <= index_ms:
            if books[i].is_valid():
                self._book = books[i]
            i += 1
        self._book_cursor = i
        return self._book

    @staticmethod
    def _neutral_out(interval: str) -> dict[str, float]:
        return {
            feature_name(base, interval): neutral_value(base)
            for base in FEATURE_BASES
        }

    def _fold(self, st: _IntervalState, bar: Bar, interval: str) -> None:
        """Fold one closed bar into ``st`` and refresh its cached output."""
        close = _finite(bar.close)
        if close <= 0:
            return

        high = _finite(bar.high, close)
        low = _finite(bar.low, close)
        quote_volume = max(0.0, _finite(bar.quote_volume))
        taker_buy = _clamp(_finite(bar.taker_buy_quote), 0.0, quote_volume)

        out = self._neutral_out(interval)

        def put(base: str, value: float) -> None:
            out[feature_name(base, interval)] = value

        # --- breakout: measured against the range *before* this bar -------- #
        # Computed first, because the deques must not yet contain this bar.
        if len(st.highs) >= RANGE_WINDOW:
            prior_high = max(st.highs)
            prior_low = min(st.lows)
            if close > prior_high > 0:
                put("breakout_dist", (close - prior_high) / prior_high * 10_000.0)
            elif 0 < close < prior_low:
                put("breakout_dist", (close - prior_low) / prior_low * 10_000.0)
            else:
                put("breakout_dist", 0.0)  # inside the range: no breakout

        # --- Wilder ATR(14) ------------------------------------------------ #
        if st.bars_seen > 0 and st.prev_close > 0:
            tr = max(high - low, abs(high - st.prev_close), abs(low - st.prev_close))
            tr = max(0.0, _finite(tr))
            if st.atr_ready:
                st.atr += (tr - st.atr) / ATR_PERIOD
            else:
                st.tr_seed.append(tr)
                if len(st.tr_seed) >= ATR_PERIOD:
                    # A perfectly flat window seeds a *zero* ATR, which is the
                    # right answer and must not restart the seeding loop.
                    st.atr = sum(st.tr_seed) / ATR_PERIOD
                    st.atr_ready = True
                    st.tr_seed.clear()
        put("atr", (st.atr / close * 10_000.0) if st.atr > 0 else 0.0)

        # --- Wilder RSI(14) ------------------------------------------------ #
        if st.bars_seen > 0:
            change = close - st.prev_close
            gain = max(0.0, change)
            loss = max(0.0, -change)
            if st.rsi_ready:
                st.avg_gain += (gain - st.avg_gain) / RSI_PERIOD
                st.avg_loss += (loss - st.avg_loss) / RSI_PERIOD
            else:
                st.rsi_seed.append(change)
                if len(st.rsi_seed) >= RSI_PERIOD:
                    st.avg_gain = sum(max(0.0, c) for c in st.rsi_seed) / RSI_PERIOD
                    st.avg_loss = sum(max(0.0, -c) for c in st.rsi_seed) / RSI_PERIOD
                    st.rsi_ready = True
                    st.rsi_seed.clear()
        if st.rsi_ready:
            denom = st.avg_gain + st.avg_loss
            # A dead-flat window has no gains *and* no losses.  RSI is undefined
            # there; 50.0 ("neither side won") is the only honest answer, and it
            # is what the classic formula tends to as both averages vanish.
            put("rsi", 50.0 if denom <= 0 else 100.0 * st.avg_gain / denom)

        # --- EMA(9)/EMA(30) slope ------------------------------------------ #
        st.ema_fast_prev = st.ema_fast
        if st.bars_seen == 0:
            st.ema_fast = close
            st.ema_slow = close
            st.ema_fast_prev = close
        else:
            st.ema_fast += (2.0 / (EMA_FAST_PERIOD + 1)) * (close - st.ema_fast)
            st.ema_slow += (2.0 / (EMA_SLOW_PERIOD + 1)) * (close - st.ema_slow)
        if st.bars_seen + 1 >= EMA_SLOW_PERIOD:
            put("ema_slope", (st.ema_fast - st.ema_fast_prev) / close * 10_000.0)

        # --- realized vol and its own percentile ---------------------------- #
        if st.prev_close > 0:
            st.log_returns.append(math.log(close / st.prev_close))
        if len(st.log_returns) >= RV_WINDOW:
            rv = _stdev(st.log_returns) * 10_000.0
            put("rv", rv)
            st.rv_hist.append(rv)
            if len(st.rv_hist) >= MIN_RVOL_PCT_OBS:
                put("rvol_pct", _percentile_rank(st.rv_hist, rv))

        # --- returns -------------------------------------------------------- #
        st.closes.append(close)
        cl = st.closes
        if len(cl) >= 2 and cl[-2] > 0:
            put("ret", (close / cl[-2] - 1.0) * 10_000.0)
        if len(cl) >= RET_LOOKBACK + 1 and cl[-RET_LOOKBACK - 1] > 0:
            put("ret5", (close / cl[-RET_LOOKBACK - 1] - 1.0) * 10_000.0)

        # --- momentum slope and acceleration -------------------------------- #
        if len(cl) >= SLOPE_WINDOW:
            window = list(cl)[-SLOPE_WINDOW:]
            mean_close = sum(window) / SLOPE_WINDOW
            slope = (
                _ols_slope(window) / mean_close * 10_000.0 if mean_close > 0 else 0.0
            )
            put("mom_slope", slope)
            if st.bars_seen + 1 > SLOPE_WINDOW:
                put("accel", slope - st.prev_slope)
            st.prev_slope = slope

        # --- range position and mean-reversion z-score ----------------------- #
        st.highs.append(high)
        st.lows.append(low)
        if len(st.highs) >= RANGE_WINDOW:
            hi = max(st.highs)
            lo = min(st.lows)
            # hi == lo is a genuinely flat window: "mid-range" is the only
            # position that is not a lie, so 0.5 rather than a divide by zero.
            put("range_pos", _clamp((close - lo) / (hi - lo), 0.0, 1.0) if hi > lo else 0.5)
        if len(cl) >= MR_WINDOW:
            window = list(cl)[-MR_WINDOW:]
            mean_close = sum(window) / MR_WINDOW
            sigma = _stdev(window)
            # sigma == 0 means the price has not moved: the distance from the
            # mean is zero standard deviations, not infinitely many.
            put(
                "zscore",
                _clamp((close - mean_close) / sigma, -MAX_ZSCORE, MAX_ZSCORE)
                if sigma > 0
                else 0.0,
            )

        # --- volume acceleration and percentile ------------------------------ #
        prev_vols = list(st.quote_vols)
        st.quote_vols.append(quote_volume)
        put("vol_accel", _volume_accel(prev_vols, quote_volume))
        put("vol_pct", _volume_percentile(prev_vols, quote_volume))
        put("trades", float(max(0, int(bar.trades))))

        # --- aggressor imbalance --------------------------------------------- #
        st.aggr_buy.append(taker_buy)
        st.aggr_total.append(quote_volume)
        total = sum(st.aggr_total)
        # No flow at all is not "balanced selling"; it is no information.  Both
        # readings are 0.0 here, which is the one place the neutral value and a
        # real measurement coincide — and that is fine, because a strategy
        # gating on |imbalance| > threshold treats them identically.
        put("aggr_imb", ((2.0 * sum(st.aggr_buy) - total) / total) if total > 0 else 0.0)

        # --- rolling VWAP ------------------------------------------------------ #
        typical = (high + low + close) / 3.0
        vol = _finite(bar.volume)
        if vol <= 0:
            # No base volume: fall back to a price-only average so the VWAP
            # stays a price instead of collapsing to zero.  Same trick as the
            # live engine.
            vol = 1.0
        st.vwap_pv.append(typical * vol)
        st.vwap_vol.append(vol)
        total_vol = sum(st.vwap_vol)
        vwap = (sum(st.vwap_pv) / total_vol) if total_vol > 0 else close
        put("vwap_dist", ((close - vwap) / vwap * 10_000.0) if vwap > 0 else 0.0)

        st.prev_close = close
        st.last_close = close
        st.last_close_ms = int(bar.close_ms)
        st.bars_seen += 1

        st.out = {k: _finite(v, neutral_value(k.rsplit("_", 1)[0])) for k, v in out.items()}


# --------------------------------------------------------------------------- #
# Volume helpers — the thin-tape rule lives here
# --------------------------------------------------------------------------- #


def _volume_accel(prev_vols: Sequence[float], current: float) -> float:
    """Recent volume against its own baseline, or the neutral 1.0.

    Returns 1.0 — not 0.0 — whenever the baseline window is too short or too
    empty to support a ratio.  0.0 reads as "volume collapsed" and would be
    just as wrong in the other direction.  Mirrors
    ``app.features._volume_ratio``; see ``docs/ECONOMICS.md`` for why a naive
    ratio produces 1,200x readings on this venue.
    """
    if len(prev_vols) < VOL_SLOW_WINDOW:
        return 1.0
    slow = list(prev_vols)[-VOL_SLOW_WINDOW:]
    if sum(1 for v in slow if v > 0) < MIN_ACTIVE_BARS:
        return 1.0
    base = sum(slow) / VOL_SLOW_WINDOW
    if base <= 0:
        return 1.0
    fast_window = (list(prev_vols) + [current])[-VOL_FAST_WINDOW:]
    fast = sum(fast_window) / len(fast_window)
    return min(fast / base, MAX_VOLUME_RATIO)


def _volume_percentile(prev_vols: Sequence[float], current: float) -> float:
    """Where this bar's volume sits in its trailing distribution, 0..1.

    Neutral 0.5 when the window is short or too sparse to rank against — on a
    tape where most bars are empty, a percentile taken against a wall of zeros
    says far more about the venue than about this bar.
    """
    if len(prev_vols) < VOL_SLOW_WINDOW:
        return 0.5
    window = list(prev_vols)[-VOL_PCT_WINDOW:]
    if sum(1 for v in window if v > 0) < MIN_ACTIVE_BARS:
        return 0.5
    return _clamp(_percentile_rank(window, current), 0.0, 1.0)


def _book_features(book: OrderBook) -> dict[str, float]:
    """Instantaneous book features.  Only called when a book actually exists."""
    mid = _finite(book.mid)
    micro = _finite(book.microprice, mid)
    spread = _finite(book.spread_bps, 0.0)
    return {
        "book_imbalance": _clamp(_finite(book.imbalance(DEPTH_BPS)), -1.0, 1.0),
        "microprice_dist_bps": (
            (micro - mid) / mid * 10_000.0 if mid > 0 else 0.0
        ),
        "spread_bps": max(0.0, spread),
        "depth_bid_usd": max(0.0, _finite(book.depth_usd("bid", DEPTH_BPS))),
        "depth_ask_usd": max(0.0, _finite(book.depth_usd("ask", DEPTH_BPS))),
    }