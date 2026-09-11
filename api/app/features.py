"""Incremental feature computation.

The worker calls :meth:`FeatureEngine.compute` for every symbol on every
evaluation tick (~1 Hz, 12 symbols, on a 2 vCPU box).  Recomputing indicators
over the whole bar history at that rate would burn the CPU budget for no
benefit, so all bar-derived state is folded forward *once per closed bar* and
cached on a small per-symbol state object:

* EMAs and Wilder's ATR are pure accumulators (O(1) per bar).
* Windowed statistics use bounded ``deque``s, so their cost is a function of
  the window (<= 240), never of how long the bot has been running.
* :meth:`compute` itself only touches the order book, the recent-trade tail and
  the cached bar state, so repeated calls between bars are nearly free.

Cold start is handled by *neutrality*, never by exceptions: a feature whose
window is not filled yet reports a value that means "no information"
(0.0 for signed quantities, 0.5 for percentile-style ones, 1.0 for ratios).
No field is ever NaN or infinite — :func:`assert_features_finite` enforces it.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field, fields
from typing import Deque, Sequence

from .config import Settings, get_settings
from .types import Bar, Features, MarketSnapshot, Trade

# --------------------------------------------------------------------------- #
# Window sizes.  Module constants so tests and the dashboard can quote them.
# --------------------------------------------------------------------------- #

ATR_PERIOD = 14
RV_WINDOW = 30                 # 1m log returns in the realized-vol estimate
VOL_PCT_WINDOW = 240           # trailing realized-vol observations (~4 h)
MIN_VOL_PCT_OBS = 30
EMA_FAST_PERIOD = 9
EMA_SLOW_PERIOD = 30
RANGE_WINDOW = 20
BB_WINDOW = 20
BB_SIGMA = 2.0
COMPRESSION_WINDOW = 120
MIN_COMPRESSION_OBS = 20
VOLUME_WINDOW = 20             # *previous* bars the last bar is compared to
VWAP_WINDOW = 60
RET_LOOKBACKS = (1, 5, 15)

FLOW_WINDOW_MS = 60_000
FLOW_FAST_WINDOW_MS = 15_000

SPREAD_BASELINE_ALPHA = 1.0 / 50.0
# Reported when the book is unusable.  Finite on purpose: every downstream
# consumer compares against thresholds, and `inf` would poison arithmetic.
NO_BOOK_SPREAD_BPS = 10_000.0
NO_BOOK_AGE_MS = 10 * 60 * 1000

_DEPTH_BPS = 10.0


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _finite(x: float, default: float = 0.0) -> float:
    """Collapse NaN/inf to ``default`` so a bad tick cannot poison a Features."""
    if x is None:  # pragma: no cover - defensive
        return default
    xf = float(x)
    return default if (math.isnan(xf) or math.isinf(xf)) else xf


def _percentile_rank(window: Sequence[float], x: float) -> float:
    """Fraction of ``window`` below ``x``; ties count half.  Range [0, 1]."""
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


def _stdev(values: Sequence[float]) -> float:
    """Sample standard deviation (ddof=1).  0.0 for fewer than two points."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(var) if var > 0 else 0.0


# --------------------------------------------------------------------------- #
# Per-symbol state
# --------------------------------------------------------------------------- #


@dataclass
class _SymbolState:
    """Everything the engine remembers about one symbol between ticks.

    Each bar-derived feature is cached here at ingest time; :meth:`compute`
    reads the cache rather than recomputing.
    """

    last_bar_ms: int = -1
    bars_seen: int = 0
    prev_close: float = 0.0
    last_close: float = 0.0

    # Wilder ATR
    atr: float = 0.0                      # price units
    atr_ready: bool = False
    _tr_seed: list[float] = field(default_factory=list)

    # EMA accumulators
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    ema_fast_prev: float = 0.0

    # Rolling windows
    log_returns: Deque[float] = field(default_factory=lambda: deque(maxlen=RV_WINDOW))
    vol_hist: Deque[float] = field(default_factory=lambda: deque(maxlen=VOL_PCT_WINDOW))
    highs: Deque[float] = field(default_factory=lambda: deque(maxlen=RANGE_WINDOW))
    lows: Deque[float] = field(default_factory=lambda: deque(maxlen=RANGE_WINDOW))
    bb_closes: Deque[float] = field(default_factory=lambda: deque(maxlen=BB_WINDOW))
    bb_hist: Deque[float] = field(default_factory=lambda: deque(maxlen=COMPRESSION_WINDOW))
    quote_vols: Deque[float] = field(
        default_factory=lambda: deque(maxlen=VOLUME_WINDOW + 1)
    )
    closes: Deque[float] = field(
        default_factory=lambda: deque(maxlen=max(RET_LOOKBACKS) + 1)
    )
    vwap_pv: Deque[float] = field(default_factory=lambda: deque(maxlen=VWAP_WINDOW))
    vwap_vol: Deque[float] = field(default_factory=lambda: deque(maxlen=VWAP_WINDOW))

    # Cached bar-derived features
    atr_bps: float = 0.0
    realized_vol_bps: float = 0.0
    vol_percentile: float = 0.5
    ema_slope_bps: float = 0.0
    trend_strength: float = 0.0
    range_pos: float = 0.5
    bb_width_bps: float = 0.0
    bb_width_prev_bps: float = 0.0
    compression: float = 0.5
    compression_prev: float = 0.5
    volume_ratio: float = 1.0
    ret_bps: dict[int, float] = field(default_factory=dict)
    vwap: float = 0.0
    range_bps: float = 0.0

    # Book state
    spread_baseline_bps: float = 0.0


class FeatureEngine:
    """Turns a :class:`MarketSnapshot` into a fully populated :class:`Features`.

    One instance serves every symbol; state is keyed by ``snap.symbol``.
    """

    def __init__(self, *, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._state: dict[str, _SymbolState] = {}

    # -- public API -------------------------------------------------------- #

    def reset(self, symbol: str | None = None) -> None:
        """Forget state for one symbol, or all of them."""
        if symbol is None:
            self._state.clear()
        else:
            self._state.pop(symbol, None)

    def compute(self, snap: MarketSnapshot) -> Features:
        st = self._state.get(snap.symbol)
        if st is None:
            st = _SymbolState()
            self._state[snap.symbol] = st

        new_bars = self._ingest_bars(st, snap.bars_1m)

        book = snap.book
        usable = book is not None and book.is_valid()
        if usable:
            mid = _finite(book.mid)
            microprice = _finite(book.microprice, mid)
            spread_bps = _finite(book.spread_bps, NO_BOOK_SPREAD_BPS)
            spread_bps = _clamp(spread_bps, 0.0, NO_BOOK_SPREAD_BPS)
            depth_bid = _finite(book.depth_usd("bid", _DEPTH_BPS))
            depth_ask = _finite(book.depth_usd("ask", _DEPTH_BPS))
            imbalance = _clamp(_finite(book.imbalance(_DEPTH_BPS)), -1.0, 1.0)
            book_age_ms = max(0, int(book.age_ms(snap.ts_ms)))
            if book.last_update_ms <= 0:
                book_age_ms = NO_BOOK_AGE_MS
        else:
            mid = 0.0
            microprice = 0.0
            spread_bps = NO_BOOK_SPREAD_BPS
            depth_bid = 0.0
            depth_ask = 0.0
            imbalance = 0.0
            book_age_ms = NO_BOOK_AGE_MS
        book_age_ms = min(book_age_ms, NO_BOOK_AGE_MS)

        # The baseline is what "normal" spread means for this symbol; the regime
        # classifier compares against it.  Advance it once per closed bar so the
        # value does not depend on how often compute() happens to be called, and
        # never fold an already-abnormal spread into it.
        baseline = st.spread_baseline_bps if st.spread_baseline_bps > 0 else spread_bps
        if usable and new_bars:
            if st.spread_baseline_bps <= 0:
                st.spread_baseline_bps = spread_bps
            elif spread_bps <= 3.0 * st.spread_baseline_bps:
                st.spread_baseline_bps += SPREAD_BASELINE_ALPHA * (
                    spread_bps - st.spread_baseline_bps
                )
        if not usable:
            baseline = st.spread_baseline_bps

        price = _finite(mid) or _finite(st.last_close)
        price = _finite(snap.price, price) or price

        flow, flow_accel = self._trade_flow(snap.recent_trades, snap.ts_ms)

        vwap = st.vwap if st.vwap > 0 else price
        vwap_dev_bps = 0.0
        if vwap > 0 and price > 0:
            vwap_dev_bps = (price - vwap) / vwap * 10_000.0

        f = Features(
            symbol=snap.symbol,
            ts_ms=int(snap.ts_ms),
            price=_finite(price),
            mid=_finite(mid),
            microprice=_finite(microprice),
            spread_bps=_finite(spread_bps, NO_BOOK_SPREAD_BPS),
            book_age_ms=int(book_age_ms),
            depth_bid_usd=_finite(depth_bid),
            depth_ask_usd=_finite(depth_ask),
            imbalance=_finite(imbalance),
            atr_bps=_finite(st.atr_bps),
            realized_vol_bps=_finite(st.realized_vol_bps),
            vol_percentile=_clamp(_finite(st.vol_percentile, 0.5), 0.0, 1.0),
            ret_1m_bps=_finite(st.ret_bps.get(1, 0.0)),
            ret_5m_bps=_finite(st.ret_bps.get(5, 0.0)),
            ret_15m_bps=_finite(st.ret_bps.get(15, 0.0)),
            ema_fast=_finite(st.ema_fast or price),
            ema_slow=_finite(st.ema_slow or price),
            ema_slope_bps=_finite(st.ema_slope_bps),
            trend_strength=_clamp(_finite(st.trend_strength), 0.0, 1.0),
            range_pos=_clamp(_finite(st.range_pos, 0.5), 0.0, 1.0),
            bb_width_bps=_finite(st.bb_width_bps),
            compression=_clamp(_finite(st.compression, 0.5), 0.0, 1.0),
            volume_ratio=_finite(st.volume_ratio, 1.0),
            trade_flow_imbalance=_clamp(_finite(flow), -1.0, 1.0),
            trade_flow_accel=_clamp(_finite(flow_accel), -2.0, 2.0),
            vwap=_finite(vwap),
            vwap_dev_bps=_finite(vwap_dev_bps),
            quote_volume_24h=_finite(snap.quote_volume_24h),
            trades_24h=int(snap.trades_24h),
            bars_available=int(st.bars_seen),
            extras={
                # Regime/strategy inputs that do not have a first-class field.
                "spread_baseline_bps": _finite(baseline),
                "compression_prev": _clamp(_finite(st.compression_prev, 0.5), 0.0, 1.0),
                "bb_width_prev_bps": _finite(st.bb_width_prev_bps),
                "range_bps": _finite(st.range_bps),
                "new_bars": float(new_bars),
            },
        )
        assert_features_finite(f)
        return f

    # -- bar folding ------------------------------------------------------- #

    def _ingest_bars(self, st: _SymbolState, bars: Sequence[Bar] | None) -> int:
        """Fold every *unseen* bar into ``st``.  Returns how many were new.

        The caller hands us the whole visible history every tick, but bars are
        ascending by close time, so we scan backwards and stop at the first one
        we have already folded.  That makes the steady-state cost proportional
        to the number of *new* bars (usually zero), not to the history length.
        """
        if not bars:
            return 0
        first_new = len(bars)
        for i in range(len(bars) - 1, -1, -1):
            if bars[i].close_ms <= st.last_bar_ms:
                break
            first_new = i
        for bar in bars[first_new:]:
            self._fold_bar(st, bar)
        return len(bars) - first_new

    def _fold_bar(self, st: _SymbolState, bar: Bar) -> None:
        close = float(bar.close)
        if close <= 0 or math.isnan(close) or math.isinf(close):
            return

        # --- Wilder ATR(14) ------------------------------------------------ #
        if st.bars_seen > 0 and st.prev_close > 0:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - st.prev_close),
                abs(bar.low - st.prev_close),
            )
            tr = max(0.0, _finite(tr))
            if st.atr_ready:
                st.atr += (tr - st.atr) / ATR_PERIOD
            else:
                st._tr_seed.append(tr)
                if len(st._tr_seed) >= ATR_PERIOD:
                    # Seed with the simple average of the first 14 true ranges,
                    # then smooth.  A flat market seeds a *zero* ATR, which is
                    # correct and must not restart the seeding loop.
                    st.atr = sum(st._tr_seed) / ATR_PERIOD
                    st.atr_ready = True
                    st._tr_seed.clear()
        st.atr_bps = (st.atr / close * 10_000.0) if st.atr > 0 else 0.0

        # --- EMA(9) / EMA(30) --------------------------------------------- #
        st.ema_fast_prev = st.ema_fast
        if st.bars_seen == 0:
            st.ema_fast = close
            st.ema_slow = close
            st.ema_fast_prev = close
        else:
            a_fast = 2.0 / (EMA_FAST_PERIOD + 1)
            a_slow = 2.0 / (EMA_SLOW_PERIOD + 1)
            st.ema_fast += a_fast * (close - st.ema_fast)
            st.ema_slow += a_slow * (close - st.ema_slow)

        if st.bars_seen + 1 >= EMA_SLOW_PERIOD:
            st.ema_slope_bps = (st.ema_fast - st.ema_fast_prev) / close * 10_000.0
            st.trend_strength = (
                _clamp(abs(st.ema_fast - st.ema_slow) / st.atr, 0.0, 1.0)
                if st.atr > 0
                else 0.0
            )
        else:
            st.ema_slope_bps = 0.0
            st.trend_strength = 0.0

        # --- realized vol + its percentile --------------------------------- #
        if st.prev_close > 0:
            st.log_returns.append(math.log(close / st.prev_close))
        if len(st.log_returns) >= RV_WINDOW:
            st.realized_vol_bps = _stdev(st.log_returns) * 10_000.0
            st.vol_hist.append(st.realized_vol_bps)
            st.vol_percentile = (
                _percentile_rank(st.vol_hist, st.realized_vol_bps)
                if len(st.vol_hist) >= MIN_VOL_PCT_OBS
                else 0.5
            )
        else:
            st.realized_vol_bps = 0.0
            st.vol_percentile = 0.5

        # --- range position ------------------------------------------------ #
        st.highs.append(float(bar.high))
        st.lows.append(float(bar.low))
        if len(st.highs) >= RANGE_WINDOW:
            hi = max(st.highs)
            lo = min(st.lows)
            st.range_pos = (close - lo) / (hi - lo) if hi > lo else 0.5
            st.range_pos = _clamp(st.range_pos, 0.0, 1.0)
        else:
            st.range_pos = 0.5
        st.range_bps = (bar.high - bar.low) / close * 10_000.0

        # --- Bollinger width + compression --------------------------------- #
        st.bb_closes.append(close)
        st.bb_width_prev_bps = st.bb_width_bps
        st.compression_prev = st.compression
        if len(st.bb_closes) >= BB_WINDOW:
            ma = sum(st.bb_closes) / len(st.bb_closes)
            sigma = _stdev(st.bb_closes)
            st.bb_width_bps = (2.0 * BB_SIGMA * sigma) / ma * 10_000.0 if ma > 0 else 0.0
            st.bb_hist.append(st.bb_width_bps)
            st.compression = (
                1.0 - _percentile_rank(st.bb_hist, st.bb_width_bps)
                if len(st.bb_hist) >= MIN_COMPRESSION_OBS
                else 0.5
            )
        else:
            st.bb_width_bps = 0.0
            st.compression = 0.5

        # --- volume ratio --------------------------------------------------- #
        prev_vols = list(st.quote_vols)          # the *previous* bars
        st.quote_vols.append(float(bar.quote_volume))
        if len(prev_vols) >= VOLUME_WINDOW:
            base = sum(prev_vols[-VOLUME_WINDOW:]) / VOLUME_WINDOW
            st.volume_ratio = (bar.quote_volume / base) if base > 0 else 1.0
        else:
            st.volume_ratio = 1.0

        # --- multi-horizon returns ------------------------------------------ #
        st.closes.append(close)
        cl = st.closes
        for lb in RET_LOOKBACKS:
            if len(cl) >= lb + 1 and cl[-lb - 1] > 0:
                st.ret_bps[lb] = (close / cl[-lb - 1] - 1.0) * 10_000.0
            else:
                st.ret_bps[lb] = 0.0

        # --- rolling VWAP ---------------------------------------------------- #
        typical = (bar.high + bar.low + bar.close) / 3.0
        vol = float(bar.volume)
        if vol <= 0:
            # No base volume reported: fall back to a price-only average so the
            # VWAP stays meaningful instead of collapsing to zero.
            vol = 1.0
        st.vwap_pv.append(typical * vol)
        st.vwap_vol.append(vol)
        total_vol = sum(st.vwap_vol)
        st.vwap = (sum(st.vwap_pv) / total_vol) if total_vol > 0 else close

        st.prev_close = close
        st.last_close = close
        st.last_bar_ms = int(bar.close_ms)
        st.bars_seen += 1

    # -- trade flow -------------------------------------------------------- #

    @staticmethod
    def _trade_flow(trades: Sequence[Trade] | None, ts_ms: int) -> tuple[float, float]:
        """(imbalance over 60 s, 15 s imbalance minus the 60 s one)."""
        if not trades:
            return 0.0, 0.0
        buy_60 = sell_60 = buy_15 = sell_15 = 0.0
        for t in trades:
            age = ts_ms - t.ts_ms
            if age < 0 or age > FLOW_WINDOW_MS:
                continue
            notional = _finite(t.notional)
            if notional <= 0:
                continue
            if t.buyer_is_maker:          # aggressor was the seller
                sell_60 += notional
                if age <= FLOW_FAST_WINDOW_MS:
                    sell_15 += notional
            else:
                buy_60 += notional
                if age <= FLOW_FAST_WINDOW_MS:
                    buy_15 += notional
        total_60 = buy_60 + sell_60
        imb_60 = (buy_60 - sell_60) / total_60 if total_60 > 0 else 0.0
        total_15 = buy_15 + sell_15
        if total_15 <= 0:
            return imb_60, 0.0
        imb_15 = (buy_15 - sell_15) / total_15
        return imb_60, imb_15 - imb_60


# --------------------------------------------------------------------------- #
# Guard
# --------------------------------------------------------------------------- #

_NUMERIC_TYPES = (int, float)


def assert_features_finite(f: Features) -> None:
    """Raise ``ValueError`` if any numeric field (or extra) is NaN/inf.

    Called on every :meth:`FeatureEngine.compute` result.  A NaN that reaches
    the ranker silently poisons every comparison it takes part in, so it is far
    better to fail loudly here, where the offending symbol is still known.
    """
    bad: list[str] = []
    for fld in fields(f):
        value = getattr(f, fld.name)
        if isinstance(value, bool) or not isinstance(value, _NUMERIC_TYPES):
            continue
        if math.isnan(value) or math.isinf(value):
            bad.append(fld.name)
    for key, value in f.extras.items():
        if isinstance(value, _NUMERIC_TYPES) and (
            math.isnan(float(value)) or math.isinf(float(value))
        ):
            bad.append(f"extras[{key}]")
    if bad:
        raise ValueError(f"non-finite features for {f.symbol}: {', '.join(bad)}")
