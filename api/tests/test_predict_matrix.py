"""Offline tests for the predictive feature matrix.

Two properties decide whether anything downstream is worth reading, and each
gets a dedicated test rather than a passing assertion inside a bigger one:

* **No leakage.**  A row at time T must be byte-identical whether or not the
  dataset also contains bars from after T.  Every model, every calibration
  curve and every EV gate in Phase 4 is worthless if this is false, and the
  failure is silent, so it is asserted directly and on the raw bytes.
* **No divide-by-zero artefacts.**  This venue leaves 51% of 1-minute and 90%
  of 5-second buckets empty (``docs/ECONOMICS.md``).  An earlier volume ratio
  divided by a baseline that was usually zero and reported **1291x**, which
  made every volume gate fire on the first print after a quiet spell.  That
  exact scenario is reconstructed here and the neutral is asserted.

The rest of the file is the arithmetic of each feature on a hand-built series,
the half-open ``slice_ms`` boundaries, the cyclical time encoding across
midnight and across Sunday to Monday, the group/column bijection, and the cache
round trip including the feature-version bump that must invalidate it.
"""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pytest

from app.predict import matrix as mx
from app.predict.matrix import (
    BOOK_NAMES,
    COST_NAMES,
    DEFAULT_SPREAD_BPS,
    FEATURE_GROUPS,
    GROUP_NAMES,
    MAX_RATIO,
    NEUTRALS,
    RANGE_WINDOW,
    SCORE_NAMES,
    SLOW_WINDOW,
    TIME_NAMES,
    FeatureMatrix,
    build_matrix,
    clear_matrix_cache,
    feature_group,
    group_neutral_fractions,
    neutral_fractions,
)
from app.research.dataset import INTERVALS, Dataset
from app.types import Bar, BookLevel, OrderBook, Trade

MINUTE = 60_000
T0 = 1_700_000_000_000
BASE = (T0 // MINUTE) * MINUTE
SYM = "BTCUSD"


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def make_bars(
    closes,
    *,
    symbol: str = SYM,
    step_ms: int = MINUTE,
    start_ms: int = BASE,
    quote_volume=10_000.0,
    trades=5,
    taker_buy_frac=0.5,
    spread_frac: float = 0.001,
) -> list[Bar]:
    """A clean OHLC series whose high/low straddle the close."""
    out: list[Bar] = []
    for i, close in enumerate(closes):
        qv = quote_volume[i] if isinstance(quote_volume, (list, tuple)) else quote_volume
        tc = trades[i] if isinstance(trades, (list, tuple)) else trades
        frac = (
            taker_buy_frac[i]
            if isinstance(taker_buy_frac, (list, tuple))
            else taker_buy_frac
        )
        out.append(
            Bar(
                symbol=symbol,
                open_ms=start_ms + i * step_ms,
                close_ms=start_ms + (i + 1) * step_ms - 1,
                open=close,
                high=close * (1.0 + spread_frac),
                low=close * (1.0 - spread_frac),
                close=close,
                volume=(qv / close) if close > 0 else 0.0,
                quote_volume=qv,
                trades=int(tc),
                taker_buy_quote=qv * frac,
            )
        )
    return out


def make_dataset(bars, *, symbol: str = SYM, trades=(), books=()) -> Dataset:
    return Dataset(
        symbol=symbol,
        bars={"1m": list(bars)},
        trades=list(trades),
        start_ms=bars[0].open_ms,
        end_ms=bars[-1].close_ms,
        books=tuple(books),
    )


class LabelDouble:
    """A stand-in for ``app.predict.labels.Label``.

    Only the frozen contract's fields are used, and only by name, so this
    module builds against the contract rather than against another agent's
    in-flight implementation.
    """

    __slots__ = (
        "symbol", "ts_ms", "spec_key", "outcome", "net_return_bps", "hit_ms",
        "entry_price", "tp_price", "sl_price", "label_end_ms",
    )

    def __init__(
        self,
        ts_ms: int,
        *,
        symbol: str = SYM,
        spec_key: str = "x=net_fixed:12|s=atr:1|h=25|rt=3",
        outcome: str = "TP_BEFORE_SL",
        net_return_bps: float = 12.0,
        horizon_ms: int = 25 * MINUTE,
    ) -> None:
        self.symbol = symbol
        self.ts_ms = int(ts_ms)
        self.spec_key = spec_key
        self.outcome = outcome
        self.net_return_bps = net_return_bps
        self.hit_ms = 0
        self.entry_price = 100.0
        self.tp_price = 101.0
        self.sl_price = 99.0
        self.label_end_ms = int(ts_ms) + horizon_ms


def labels_for(bars, *, first: int = 0, **kw) -> list[LabelDouble]:
    return [LabelDouble(bar.close_ms, **kw) for bar in bars[first:]]


def make_book(
    ts_ms: int,
    mid: float,
    *,
    spread_bps: float = 2.0,
    bid_qty: float = 1.0,
    ask_qty: float = 1.0,
    levels: int = 6,
    symbol: str = SYM,
) -> OrderBook:
    half = mid * spread_bps / 20_000.0
    bids = [
        BookLevel(price=mid - half - i * mid * 2e-4, qty=bid_qty) for i in range(levels)
    ]
    asks = [
        BookLevel(price=mid + half + i * mid * 2e-4, qty=ask_qty) for i in range(levels)
    ]
    return OrderBook(
        symbol=symbol, bids=bids, asks=asks, last_update_id=ts_ms, last_update_ms=ts_ms
    )


def wavy(n: int, *, level: float = 100.0, amp: float = 3.0) -> list[float]:
    return [level + math.sin(i / 4.0) * amp for i in range(n)]


@pytest.fixture(autouse=True)
def _clean_memo():
    clear_matrix_cache()
    yield
    clear_matrix_cache()


def column(fm: FeatureMatrix, name: str) -> np.ndarray:
    return fm.column(name)


# --------------------------------------------------------------------------- #
# The catalogue
# --------------------------------------------------------------------------- #


def test_feature_groups_are_the_eight_the_spec_names():
    assert tuple(FEATURE_GROUPS) == GROUP_NAMES
    assert set(GROUP_NAMES) == {
        "momentum", "volatility", "volume", "book",
        "regime", "cost", "time", "score",
    }


def test_no_feature_name_appears_in_two_groups():
    seen: dict[str, str] = {}
    for group, names in FEATURE_GROUPS.items():
        for name in names:
            assert name not in seen, f"{name} in both {seen.get(name)} and {group}"
            seen[name] = group


def test_every_emitted_column_belongs_to_a_group():
    bars = make_bars(wavy(120))
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=60))
    for name in fm.feature_names:
        assert feature_group(name) in GROUP_NAMES
        assert name in FEATURE_GROUPS[feature_group(name)]


def test_a_full_dataset_emits_exactly_the_whole_catalogue():
    """The other direction: with every interval and a book, nothing is missing."""
    closes = wavy(400)
    books = [make_book(BASE + i * MINUTE, closes[i % len(closes)]) for i in range(50)]
    bars_by_interval = {}
    for interval in INTERVALS:
        step = {"5s": 5_000, "15s": 15_000, "30s": 30_000, "1m": 60_000,
                "3m": 180_000, "5m": 300_000, "15m": 900_000}[interval]
        bars_by_interval[interval] = make_bars(closes, step_ms=step)
    ds = Dataset(
        symbol=SYM,
        bars=bars_by_interval,
        trades=[],
        start_ms=BASE,
        end_ms=BASE + 400 * 900_000,
        books=tuple(books),
    )
    minute_bars = bars_by_interval["1m"]
    fm = build_matrix(ds, labels_for(minute_bars, first=200))

    catalogue = {n for names in FEATURE_GROUPS.values() for n in names}
    assert set(fm.feature_names) == catalogue
    assert len(fm.feature_names) == len(set(fm.feature_names))


def test_every_catalogue_name_has_a_neutral_and_a_cap():
    for names in FEATURE_GROUPS.values():
        for name in names:
            assert name in NEUTRALS
            assert mx._CAPS[name] > 0


# --------------------------------------------------------------------------- #
# Leakage — the test that decides whether the module is trustworthy
# --------------------------------------------------------------------------- #


def test_rows_are_byte_identical_when_future_bars_are_appended():
    past = make_bars(wavy(300))
    # A violent, obvious future: if anything at all leaks, these bars move it.
    future = past + make_bars(
        [500.0 + i * 25.0 for i in range(120)],
        start_ms=past[-1].close_ms + 1,
        quote_volume=5_000_000.0,
        trades=900,
        taker_buy_frac=1.0,
    )
    labels = labels_for(past, first=150)

    a = build_matrix(make_dataset(past), labels)
    clear_matrix_cache()
    b = build_matrix(make_dataset(future), labels)

    assert a.feature_names == b.feature_names
    assert a.X.shape == b.X.shape
    # Byte-identical, not merely close: a tolerance here would hide exactly the
    # slow drift a leak produces.
    assert a.X.tobytes() == b.X.tobytes()
    assert np.array_equal(a.ts_ms, b.ts_ms)


def test_leakage_check_is_not_answered_out_of_the_cache(tmp_path):
    """Appending bars must produce a different cache key, or the test above lies."""
    past = make_bars(wavy(200))
    future = past + make_bars(
        [900.0] * 40, start_ms=past[-1].close_ms + 1, quote_volume=9e6, trades=999
    )
    labels = labels_for(past, first=120)

    a = build_matrix(make_dataset(past), labels, cache_dir=tmp_path)
    b = build_matrix(make_dataset(future), labels, cache_dir=tmp_path)

    assert b.meta["cache_hits"] == 0
    assert a.X.tobytes() == b.X.tobytes()


def test_no_bar_that_had_not_closed_is_ever_folded():
    bars = make_bars([100.0 + i for i in range(60)])
    ds = make_dataset(bars)
    # One millisecond before bar 40 closes, only bars 0..39 exist.
    a = build_matrix(ds, [LabelDouble(bars[40].close_ms - 1)])
    clear_matrix_cache()
    b = build_matrix(ds, [LabelDouble(bars[39].close_ms)])
    # The clock genuinely differs between the two instants, so the time group
    # is expected to differ; everything derived from the tape must not.
    keep = [i for i, n in enumerate(a.feature_names) if feature_group(n) != "time"]
    assert a.X[:, keep].tobytes() == b.X[:, keep].tobytes()


def test_build_matrix_does_not_scale_anything():
    """No full-sample normalisation: the columns are raw, and provably so."""
    closes = [100.0 * (1.004 ** i) for i in range(120)]
    bars = make_bars(closes)
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=60))

    ret = column(fm, "ret_1m")
    # A steady +40 bps per bar geometric drift stays +40 bps in the matrix.  A
    # standardised column would centre near zero with unit variance.
    assert ret.mean() == pytest.approx(40.0, abs=0.5)
    assert ret.std() < 0.5
    fees = column(fm, "fee_taker_bps")
    assert fees.std() == 0.0 and fees[0] > 0.0  # a constant stays a constant


# --------------------------------------------------------------------------- #
# Divide-by-zero: the 1291x artefact must not be reproducible
# --------------------------------------------------------------------------- #


def test_a_quiet_spell_then_one_print_does_not_report_a_volume_surge():
    """The exact shape of the reported 1291x bug, asserted at the neutral."""
    quiet = [0.0] * 60
    volumes = quiet + [5_000_000.0] + [0.0] * 5
    bars = make_bars(
        [100.0] * len(volumes),
        quote_volume=volumes,
        trades=[0] * 60 + [400] + [0] * 5,
    )
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=40))

    accel = column(fm, "vol_accel_1m")
    trade_accel = column(fm, "trade_accel_1m")
    assert accel.max() <= MAX_RATIO
    assert trade_accel.max() <= MAX_RATIO
    # The bar right after the quiet spell reports the neutral, not a surge.
    idx = int(np.where(fm.ts_ms == bars[60].close_ms)[0][0])
    assert accel[idx] == 1.0
    assert trade_accel[idx] == 1.0
    assert column(fm, "vol_pct_1m")[idx] == 0.5


def test_an_all_empty_volume_window_degrades_to_the_documented_neutral():
    bars = make_bars([100.0] * 120, quote_volume=0.0, trades=0)
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=60))

    # Ratios go to 1.0 ("same as its own baseline"), never 0.0 ("collapsed")
    # and never inf.
    assert set(np.unique(column(fm, "vol_accel_1m"))) == {1.0}
    assert set(np.unique(column(fm, "trade_accel_1m"))) == {1.0}
    # Percentiles go to 0.5, never 0.0.
    assert set(np.unique(column(fm, "vol_pct_1m"))) == {0.5}
    # Signed flow goes to 0.0 — which is the documented coincidence, and the
    # guard mask is what keeps "no flow" separable from "balanced flow".
    assert set(np.unique(column(fm, "cum_flow_1m"))) == {0.0}
    # ... and the feature whose whole job is to say the others are meaningless.
    assert set(np.unique(column(fm, "active_frac_1m"))) == {0.0}
    assert np.isfinite(fm.X).all()


def test_nothing_in_X_is_nan_inf_or_absurd():
    volumes = [0.0 if i % 3 else 5_000.0 for i in range(300)]
    bars = make_bars(
        [100.0] * 40 + wavy(200) + [100.0] * 60,
        quote_volume=volumes,
        trades=[0 if v == 0 else 7 for v in volumes],
    )
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=30))
    assert np.isfinite(fm.X).all()
    assert np.abs(fm.X).max() < mx.MAX_ABS_FEATURE
    for i, name in enumerate(fm.feature_names):
        cap = mx._CAPS[name]
        assert np.abs(fm.X[:, i]).max() <= cap + 1e-9, name


def test_cold_start_produces_no_nan_and_no_inf():
    """The first handful of bars, where every window is still empty."""
    bars = make_bars([100.0, 100.5, 99.5, 101.0, 100.2])
    fm = build_matrix(make_dataset(bars), labels_for(bars))
    assert len(fm) == 5
    assert np.isfinite(fm.X).all()
    # Everything that cannot be measured yet sits at its documented neutral.
    for i, name in enumerate(fm.feature_names):
        if name.startswith(("vol_accel", "trade_accel", "range_compress", "vol_expand")):
            assert fm.X[0, i] == 1.0
        if name.startswith(("rvol_pct", "vol_pct", "range_pos")):
            assert fm.X[0, i] == 0.5


def test_a_single_bar_dataset_still_builds():
    bars = make_bars([100.0, 100.0])
    fm = build_matrix(make_dataset(bars), [LabelDouble(bars[0].close_ms)])
    assert len(fm) == 1
    assert np.isfinite(fm.X).all()


# --------------------------------------------------------------------------- #
# Per-feature arithmetic on a hand-built series
# --------------------------------------------------------------------------- #


def _at(fm: FeatureMatrix, name: str, ts: int) -> float:
    row = int(np.where(fm.ts_ms == ts)[0][0])
    return float(fm.X[row, fm.feature_names.index(name)])


def test_distance_from_the_recent_high_and_low():
    # A clean ramp up then a two-bar pullback: the high is the peak, the low is
    # the start of the window.
    closes = [100.0 + i for i in range(30)] + [128.0, 126.0]
    bars = make_bars(closes, spread_frac=0.0)
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=RANGE_WINDOW))
    ts = bars[-1].close_ms

    window = closes[-RANGE_WINDOW:]
    hi, lo = max(window), min(window)
    close = closes[-1]
    assert _at(fm, "dist_high_1m", ts) == pytest.approx((close - hi) / hi * 10_000.0)
    assert _at(fm, "dist_low_1m", ts) == pytest.approx((close - lo) / lo * 10_000.0)
    # Sign convention: never above the high, never below the low.
    assert _at(fm, "dist_high_1m", ts) <= 0.0
    assert _at(fm, "dist_low_1m", ts) >= 0.0


def test_distance_from_high_and_low_are_zero_at_a_fresh_extreme():
    closes = [100.0 + i for i in range(40)]
    bars = make_bars(closes, spread_frac=0.0)
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=RANGE_WINDOW))
    assert _at(fm, "dist_high_1m", bars[-1].close_ms) == pytest.approx(0.0)


def test_range_compression_halves_when_the_bars_halve():
    # 20 wide bars, then 5 bars at half the range: fast/slow must be < 1.
    wide = make_bars([100.0] * 20, spread_frac=0.004)
    tight = make_bars(
        [100.0] * 5, start_ms=wide[-1].close_ms + 1, spread_frac=0.002
    )
    bars = wide + tight
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=SLOW_WINDOW))
    value = _at(fm, "range_compress_1m", bars[-1].close_ms)
    # fast = 40 bps, slow = mean(15 x 80 + 5 x 40) = 70 bps.
    assert value == pytest.approx(40.0 / 70.0, rel=1e-6)


def test_volatility_expansion_rises_when_the_recent_moves_grow():
    quiet = [100.0 + (0.01 if i % 2 else -0.01) for i in range(24)]
    loud = [100.0 + (1.5 if i % 2 else -1.5) for i in range(6)]
    bars = make_bars(quiet + loud)
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=SLOW_WINDOW))
    assert _at(fm, "vol_expand_1m", bars[-1].close_ms) > 1.5
    assert _at(fm, "vol_expand_1m", bars[-1].close_ms) <= MAX_RATIO


def test_cumulative_signed_flow_is_plus_one_when_every_print_is_a_buy():
    bars = make_bars([100.0] * 40, taker_buy_frac=1.0)
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=25))
    assert _at(fm, "cum_flow_1m", bars[-1].close_ms) == pytest.approx(1.0)

    bars = make_bars([100.0] * 40, taker_buy_frac=0.0)
    clear_matrix_cache()
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=25))
    assert _at(fm, "cum_flow_1m", bars[-1].close_ms) == pytest.approx(-1.0)


def test_active_fraction_counts_the_bars_that_actually_traded():
    volumes = [1_000.0 if i % 4 == 0 else 0.0 for i in range(60)]
    bars = make_bars([100.0] * 60, quote_volume=volumes, trades=1)
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=SLOW_WINDOW))
    assert _at(fm, "active_frac_1m", bars[-1].close_ms) == pytest.approx(0.25)


def test_trade_count_acceleration_sees_a_burst():
    counts = [2] * 20 + [40] * 5
    bars = make_bars([100.0] * 25, trades=counts, quote_volume=1_000.0)
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=SLOW_WINDOW))
    value = _at(fm, "trade_accel_1m", bars[-1].close_ms)
    # fast = 40, slow = mean(15 x 2 + 5 x 40) = 11.5.
    assert value == pytest.approx(40.0 / 11.5, rel=1e-6)


def test_breakout_strength_is_the_breakout_distance_in_atrs():
    closes = [100.0] * 40 + [104.0]
    bars = make_bars(closes, spread_frac=0.0005)
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=30))
    ts = bars[-1].close_ms
    atr = _at(fm, "atr_1m", ts)
    dist = _at(fm, "breakout_dist_1m", ts)
    assert atr > 0
    assert _at(fm, "breakout_strength_1m", ts) == pytest.approx(
        min(dist / atr, MAX_RATIO), rel=1e-9
    )


def test_volume_price_response_never_divides_by_an_unmeasured_baseline():
    bars = make_bars(wavy(60), quote_volume=0.0, trades=0)
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=30))
    resp = column(fm, "vol_price_resp_1m")
    ret = np.abs(column(fm, "ret_1m"))
    # vol_accel sits at its neutral 1.0, so the response is exactly |ret|.
    assert np.allclose(resp, ret)
    assert np.isfinite(resp).all()


# --------------------------------------------------------------------------- #
# Book: present only where L2 is
# --------------------------------------------------------------------------- #


def test_book_features_are_absent_not_zero_when_no_l2_is_supplied():
    bars = make_bars(wavy(120))
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=60))
    for name in BOOK_NAMES:
        assert name not in fm.feature_names
    assert "book" not in fm.groups()
    assert "book" not in fm.meta["groups"]
    assert fm.meta["book_available"] is False


def test_book_features_light_up_when_l2_exists():
    closes = wavy(120)
    bars = make_bars(closes)
    books = [
        make_book(bars[i].close_ms, closes[i], spread_bps=1.0 + (i % 5) * 0.5,
                  bid_qty=1.0 + (i % 7), ask_qty=1.0 + (i % 3))
        for i in range(len(bars))
    ]
    fm = build_matrix(make_dataset(bars, books=books), labels_for(bars, first=60))
    for name in BOOK_NAMES:
        assert name in fm.feature_names
    assert column(fm, "spread_bps").min() > 0
    assert column(fm, "depth_slope").std() >= 0.0
    assert np.abs(column(fm, "book_pressure")).max() <= 1.0
    assert np.isfinite(fm.X).all()


def test_book_group_is_dropped_when_only_some_symbols_have_l2():
    """One matrix, one column set: a half-populated book group is a fabrication."""
    closes = wavy(120)
    btc_bars = make_bars(closes, symbol="BTCUSD")
    eth_bars = make_bars(closes, symbol="ETHUSD")
    books = [make_book(b.close_ms, closes[i], symbol="BTCUSD") for i, b in enumerate(btc_bars)]
    datasets = {
        "BTCUSD": make_dataset(btc_bars, symbol="BTCUSD", books=books),
        "ETHUSD": make_dataset(eth_bars, symbol="ETHUSD"),
    }
    labels = labels_for(btc_bars, first=60) + labels_for(eth_bars, first=60, symbol="ETHUSD")
    fm = build_matrix(datasets, labels)
    for name in BOOK_NAMES:
        assert name not in fm.feature_names


def test_requesting_the_book_group_without_l2_is_not_an_error():
    bars = make_bars(wavy(80))
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=40), groups=["book", "time"])
    assert set(fm.feature_names) == set(TIME_NAMES)


# --------------------------------------------------------------------------- #
# Regime
# --------------------------------------------------------------------------- #


def test_risk_on_is_neutral_with_one_symbol_and_real_with_several():
    closes = wavy(160)
    solo_bars = make_bars(closes)
    solo = build_matrix(make_dataset(solo_bars), labels_for(solo_bars, first=80))
    # Breadth across one symbol is that symbol's own sign, which is not breadth.
    assert set(np.unique(column(solo, "risk_on"))) == {0.0}
    assert neutral_fractions(solo)["risk_on"] == 1.0

    clear_matrix_cache()
    up = make_bars([100.0 + i * 0.4 for i in range(160)], symbol="BTCUSD")
    down = make_bars([100.0 - i * 0.4 for i in range(160)], symbol="ETHUSD")
    datasets = {
        "BTCUSD": make_dataset(up, symbol="BTCUSD"),
        "ETHUSD": make_dataset(down, symbol="ETHUSD"),
    }
    pair = build_matrix(datasets, labels_for(up, first=80))
    assert neutral_fractions(pair)["risk_on"] < 1.0
    # One up, one down: breadth is exactly balanced.
    assert column(pair, "risk_on")[-1] == pytest.approx(0.0)

    clear_matrix_cache()
    both_up = {
        "BTCUSD": make_dataset(up, symbol="BTCUSD"),
        "ETHUSD": make_dataset(make_bars([100.0 + i * 0.4 for i in range(160)],
                                         symbol="ETHUSD"), symbol="ETHUSD"),
    }
    rally = build_matrix(both_up, labels_for(up, first=80))
    assert column(rally, "risk_on")[-1] == pytest.approx(1.0)


def test_btc_direction_follows_btc_and_is_neutral_without_it():
    up = make_bars([100.0 + i * 0.5 for i in range(160)], symbol="BTCUSD")
    fm = build_matrix(make_dataset(up, symbol="BTCUSD"), labels_for(up, first=80))
    assert column(fm, "btc_dir")[-1] == 1.0
    assert column(fm, "btc_ret_bps")[-1] > 0

    clear_matrix_cache()
    sol = make_bars(wavy(160), symbol="SOLUSD")
    other = build_matrix(
        make_dataset(sol, symbol="SOLUSD"), labels_for(sol, first=80, symbol="SOLUSD")
    )
    assert set(np.unique(column(other, "btc_dir"))) == {0.0}
    assert neutral_fractions(other)["btc_dir"] == 1.0


def test_regime_columns_are_the_small_integers_a_tree_can_split():
    bars = make_bars(wavy(300, amp=6.0))
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=150))
    assert set(np.unique(column(fm, "regime_trend"))) <= {-1.0, 0.0, 1.0}
    assert set(np.unique(column(fm, "regime_is_range"))) <= {0.0, 1.0}
    assert set(np.unique(column(fm, "regime_vol_state"))) <= {-1.0, 0.0, 1.0}
    conf = column(fm, "regime_confidence")
    assert conf.min() >= 0.0 and conf.max() <= 1.0


# --------------------------------------------------------------------------- #
# Cost — the group that makes the model cost-aware
# --------------------------------------------------------------------------- #


def test_cost_group_carries_the_break_even_move_and_it_is_positive():
    bars = make_bars(wavy(160))
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=80))
    for name in COST_NAMES:
        assert name in fm.feature_names
    breakeven = column(fm, "breakeven_bps")
    assert breakeven.min() > 0.0
    # Tier 0 is 0% maker / 0.01% taker; a round trip on this venue is a few bps,
    # never tens of them (docs/ECONOMICS.md).
    assert breakeven.max() < 50.0


def test_break_even_is_the_round_trip_of_the_predicted_execution_method():
    bars = make_bars(wavy(160))
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=80))
    maker_rt = column(fm, "maker_rt_bps")
    taker_rt = column(fm, "taker_rt_bps")
    is_maker = column(fm, "exec_is_maker")
    breakeven = column(fm, "breakeven_bps")
    expected = np.where(is_maker > 0, maker_rt, taker_rt)
    assert np.allclose(breakeven, expected)
    # Resting must never be modelled as costlier than crossing.
    assert (maker_rt <= taker_rt + 1e-9).all()


def test_the_fee_columns_are_the_symbols_real_schedule(settings):
    bars = make_bars(wavy(80))
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=40))
    assert column(fm, "fee_maker_bps")[0] == pytest.approx(settings.fees.maker_bps(SYM))
    assert column(fm, "fee_taker_bps")[0] == pytest.approx(settings.fees.taker_bps(SYM))


def test_spread_falls_back_to_the_documented_default_with_no_book_and_no_prints():
    bars = make_bars(wavy(80))
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=40))
    assert set(np.unique(column(fm, "cost_spread_bps"))) == {DEFAULT_SPREAD_BPS}
    assert neutral_fractions(fm)["cost_spread_bps"] == 1.0


def test_a_bouncing_tape_produces_a_measured_spread_not_the_default():
    """Roll's estimator: the bid-ask bounce is what the covariance sees.

    Roll assumes the trade indicator is an i.i.d. coin flip, so the tape here is
    one.  A *perfectly alternating* tape is not i.i.d. and would over-state the
    spread by exactly 2x — which is the estimator behaving as specified, not a
    bug, and is why the fixture flips a seeded coin instead.
    """
    rng = np.random.default_rng(7)
    trades = []
    for i in range(400):
        price = 100.0 + (0.02 if rng.random() < 0.5 else -0.02)
        trades.append(
            Trade(symbol=SYM, price=price, qty=0.01,
                  ts_ms=BASE + i * 1_000, buyer_is_maker=bool(i % 2))
        )
    bars = make_bars([100.0] * 20, step_ms=MINUTE)
    ds = Dataset(symbol=SYM, bars={"1m": bars}, trades=trades,
                 start_ms=BASE, end_ms=bars[-1].close_ms)
    fm = build_matrix(ds, labels_for(bars, first=10))
    spread = column(fm, "cost_spread_bps")
    # A 0.04-wide bounce on a 100 mid is ~4 bps.  The estimate is taken over a
    # 50-print window, so it is a measurement with sampling error rather than an
    # identity; what matters is that it lands on the spread and not on the
    # fallback default.
    assert spread[-1] == pytest.approx(4.0, abs=1.5)
    assert spread[-1] != DEFAULT_SPREAD_BPS


def test_the_spread_estimator_refuses_a_trending_window():
    """Positive autocovariance means Roll does not apply; the default is honest."""
    prices = [100.0 + i * 0.01 for i in range(200)]
    value, guarded = mx._roll_spread_bps(prices)
    assert guarded is True
    assert value == DEFAULT_SPREAD_BPS


def test_break_even_over_atr_is_capped_rather_than_zero_during_warm_up():
    bars = make_bars([100.0] * 6)
    fm = build_matrix(make_dataset(bars), labels_for(bars))
    # "We cannot measure the move" must not read as "the cost is free".
    assert column(fm, "breakeven_over_atr")[0] == MAX_RATIO
    assert column(fm, "breakeven_over_rv")[0] == MAX_RATIO


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #


def test_hour_and_weekday_match_the_calendar():
    ts = int(dt.datetime(2026, 9, 11, 14, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    values: dict[str, float] = {}
    guards: dict[str, bool] = {}
    mx._SymbolBuilder.__dict__["_emit_time"](_time_only_builder(), ts, values, guards)
    assert values["hour_sin"] == pytest.approx(math.sin(2 * math.pi * 14 / 24))
    assert values["hour_cos"] == pytest.approx(math.cos(2 * math.pi * 14 / 24))
    # 2026-09-11 is a Friday, so not a weekend, and dow index 4.
    assert values["is_weekend"] == 0.0
    assert values["dow_sin"] == pytest.approx(math.sin(2 * math.pi * (4 + 14 / 24) / 7))


def _time_only_builder():
    """A builder stub: ``_emit_time`` only touches the activity accumulators."""

    class Stub:
        _act_last = None
        _act_hours: list = []

    return Stub()


def test_weekend_flag_matches_the_calendar_for_a_full_week():
    for day in range(7, 14):  # 2026-09-07 Monday .. 2026-09-13 Sunday
        ts = int(dt.datetime(2026, 9, day, 12, tzinfo=dt.timezone.utc).timestamp() * 1000)
        values: dict[str, float] = {}
        mx._SymbolBuilder.__dict__["_emit_time"](_time_only_builder(), ts, values, {})
        expected = dt.datetime(2026, 9, day, tzinfo=dt.timezone.utc).weekday() >= 5
        assert values["is_weekend"] == (1.0 if expected else 0.0), day


def test_cyclical_hour_encoding_is_continuous_across_midnight():
    before = int(dt.datetime(2026, 9, 11, 23, 59, 59, tzinfo=dt.timezone.utc).timestamp() * 1000)
    after = before + 2_000  # two seconds later: a new day
    a, b = {}, {}
    mx._SymbolBuilder.__dict__["_emit_time"](_time_only_builder(), before, a, {})
    mx._SymbolBuilder.__dict__["_emit_time"](_time_only_builder(), after, b, {})
    step = math.hypot(a["hour_sin"] - b["hour_sin"], a["hour_cos"] - b["hour_cos"])
    # Two seconds apart on a unit circle: essentially zero distance.  A raw
    # integer hour would jump from 23 to 0 and a tree would split it as a
    # 23-unit gap.
    assert step < 1e-3


def test_cyclical_weekday_encoding_is_continuous_across_sunday_to_monday():
    sunday = int(dt.datetime(2026, 9, 13, 23, 59, 59, tzinfo=dt.timezone.utc).timestamp() * 1000)
    monday = sunday + 2_000
    a, b = {}, {}
    mx._SymbolBuilder.__dict__["_emit_time"](_time_only_builder(), sunday, a, {})
    mx._SymbolBuilder.__dict__["_emit_time"](_time_only_builder(), monday, b, {})
    step = math.hypot(a["dow_sin"] - b["dow_sin"], a["dow_cos"] - b["dow_cos"])
    assert step < 1e-3
    assert a["is_weekend"] == 1.0 and b["is_weekend"] == 0.0


def test_time_columns_stay_on_the_unit_circle():
    bars = make_bars(wavy(200))
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=100))
    radius = column(fm, "hour_sin") ** 2 + column(fm, "hour_cos") ** 2
    assert np.allclose(radius, 1.0)
    radius = column(fm, "dow_sin") ** 2 + column(fm, "dow_cos") ** 2
    assert np.allclose(radius, 1.0)


def test_activity_regime_uses_only_completed_hours():
    # 30 hours of bars whose print counts climb, so the last completed hour is
    # the busiest the trailing day has seen.
    counts = [1 + i // 60 for i in range(30 * 60)]
    bars = make_bars([100.0] * len(counts), trades=counts, quote_volume=1_000.0)
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=60))
    activity = column(fm, "activity_regime")
    assert activity.min() >= 0.0 and activity.max() <= 1.0
    assert activity[0] == 0.5              # warm-up: the documented neutral
    assert activity[-1] > 0.9              # the busiest hour of the trailing day


# --------------------------------------------------------------------------- #
# Score
# --------------------------------------------------------------------------- #


def test_the_deterministic_score_survives_as_one_feature_among_many():
    bars = make_bars(wavy(400, amp=5.0))
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=200))
    for name in SCORE_NAMES:
        assert name in fm.feature_names
    assert column(fm, "score_long").max() <= 1.0
    assert column(fm, "score_short").max() <= 1.0
    signed = column(fm, "score_signed")
    assert np.allclose(signed, column(fm, "score_long") - column(fm, "score_short"))
    # It is one of well over a hundred columns, and it authorises nothing.
    assert len(fm.feature_names) > 40


# --------------------------------------------------------------------------- #
# slice_ms
# --------------------------------------------------------------------------- #


def test_slice_ms_is_half_open():
    bars = make_bars(wavy(60))
    labels = labels_for(bars, first=20)
    fm = build_matrix(make_dataset(bars), labels)
    lo = int(fm.ts_ms[5])
    hi = int(fm.ts_ms[10])

    window = fm.slice_ms(lo, hi)
    assert int(window.ts_ms[0]) == lo          # the lower bound is included
    assert int(window.ts_ms[-1]) == int(fm.ts_ms[9])
    assert hi not in set(window.ts_ms.tolist())  # the upper bound is excluded
    assert len(window) == 5


def test_abutting_slices_partition_without_overlap():
    bars = make_bars(wavy(80))
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=20))
    cut = int(fm.ts_ms[len(fm) // 2])
    left = fm.slice_ms(int(fm.ts_ms[0]), cut)
    right = fm.slice_ms(cut, int(fm.ts_ms[-1]) + 1)
    assert len(left) + len(right) == len(fm)
    assert not set(left.ts_ms.tolist()) & set(right.ts_ms.tolist())


def test_slice_ms_returns_a_view_and_never_recomputes():
    bars = make_bars(wavy(80))
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=20))
    window = fm.slice_ms(int(fm.ts_ms[3]), int(fm.ts_ms[9]))
    # A view, not a copy: a recomputation is the one place leakage could sneak
    # back in, so the rows must provably be the same buffer.
    assert np.shares_memory(window.X, fm.X)
    assert window.X.tobytes() == fm.X[3:9].tobytes()
    assert window.feature_names == fm.feature_names


def test_slice_ms_outside_the_range_is_empty_not_an_error():
    bars = make_bars(wavy(40))
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=10))
    empty = fm.slice_ms(int(fm.ts_ms[-1]) + 10_000, int(fm.ts_ms[-1]) + 20_000)
    assert len(empty) == 0
    assert empty.X.shape[1] == fm.X.shape[1]
    assert empty.symbols == []


def test_slice_ms_carries_every_aligned_column():
    bars = make_bars(wavy(60))
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=20))
    window = fm.slice_ms(int(fm.ts_ms[2]), int(fm.ts_ms[8]))
    n = len(window)
    assert len(window.y) == n
    assert len(window.symbols) == n
    assert len(window.spec_keys) == n
    assert len(window.label_end_ms) == n
    assert window.net_return_bps is not None and len(window.net_return_bps) == n
    assert window.guarded is not None and window.guarded.shape[0] == n


# --------------------------------------------------------------------------- #
# Rows, labels and sharing across specs
# --------------------------------------------------------------------------- #


def test_one_row_per_label_in_chronological_order():
    bars = make_bars(wavy(60))
    labels = labels_for(bars, first=20)
    fm = build_matrix(make_dataset(bars), labels)
    assert len(fm) == len(labels)
    assert (np.diff(fm.ts_ms) >= 0).all()


def test_y_is_one_only_for_tp_before_sl():
    bars = make_bars(wavy(40))
    labels = [
        LabelDouble(bars[20].close_ms, outcome="TP_BEFORE_SL", spec_key="a"),
        LabelDouble(bars[21].close_ms, outcome="SL_BEFORE_TP", spec_key="a"),
        LabelDouble(bars[22].close_ms, outcome="TIMEOUT", spec_key="a"),
        LabelDouble(bars[23].close_ms, outcome="NO_EDGE", spec_key="a"),
    ]
    fm = build_matrix(make_dataset(bars), labels)
    assert fm.y.tolist() == [1, 0, 0, 0]


def test_specs_sharing_an_instant_share_its_features():
    """The specs differ in their barriers, not in what the market looked like."""
    bars = make_bars(wavy(80))
    stamps = [b.close_ms for b in bars[40:]]
    a = [LabelDouble(ts, spec_key="h=25") for ts in stamps]
    b = [LabelDouble(ts, spec_key="h=60", outcome="TIMEOUT") for ts in stamps]
    fm = build_matrix(make_dataset(bars), {"h=25": a, "h=60": b})

    assert len(fm) == 2 * len(stamps)
    for ts in stamps:
        rows = fm.X[fm.ts_ms == ts]
        assert rows.shape[0] == 2
        assert rows[0].tobytes() == rows[1].tobytes()


def test_labels_for_an_unknown_symbol_are_rejected():
    bars = make_bars(wavy(40))
    with pytest.raises(ValueError, match="no dataset"):
        build_matrix(make_dataset(bars), [LabelDouble(bars[20].close_ms, symbol="DOGEUSD")])


def test_an_unknown_group_is_rejected():
    bars = make_bars(wavy(40))
    with pytest.raises(ValueError, match="unknown feature group"):
        build_matrix(make_dataset(bars), labels_for(bars, first=20), groups=["moementum"])


def test_selecting_groups_emits_only_those_groups():
    bars = make_bars(wavy(80))
    fm = build_matrix(
        make_dataset(bars), labels_for(bars, first=40), groups=["cost", "time"]
    )
    assert set(fm.groups()) == {"cost", "time"}
    assert set(fm.feature_names) == set(COST_NAMES) | set(TIME_NAMES)


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #


def test_the_cache_round_trips(tmp_path):
    bars = make_bars(wavy(200))
    labels = labels_for(bars, first=100)
    ds = make_dataset(bars)

    cold = build_matrix(ds, labels, cache_dir=tmp_path)
    assert cold.meta["cache_hits"] == 0
    assert list((tmp_path / "predict-matrix" / SYM).glob("*.npz"))

    clear_matrix_cache()  # force the read to come off disk, not the memo
    warm = build_matrix(ds, labels, cache_dir=tmp_path)
    assert warm.meta["cache_hits"] == 1
    assert warm.X.tobytes() == cold.X.tobytes()
    assert warm.feature_names == cold.feature_names
    assert np.array_equal(warm.guarded, cold.guarded)


def test_a_feature_version_bump_invalidates_the_cache(tmp_path, monkeypatch):
    bars = make_bars(wavy(120))
    labels = labels_for(bars, first=60)
    ds = make_dataset(bars)

    build_matrix(ds, labels, cache_dir=tmp_path)
    clear_matrix_cache()
    assert build_matrix(ds, labels, cache_dir=tmp_path).meta["cache_hits"] == 1

    clear_matrix_cache()
    monkeypatch.setattr(mx, "FEATURE_VERSION", "matrix-test-bumped")
    # A maths change must re-derive rather than silently serve rows built by
    # the old code.
    assert build_matrix(ds, labels, cache_dir=tmp_path).meta["cache_hits"] == 0


def test_a_different_group_selection_does_not_collide_in_the_cache(tmp_path):
    bars = make_bars(wavy(120))
    labels = labels_for(bars, first=60)
    ds = make_dataset(bars)
    full = build_matrix(ds, labels, cache_dir=tmp_path)
    clear_matrix_cache()
    just_time = build_matrix(ds, labels, cache_dir=tmp_path, groups=["time"])
    assert just_time.meta["cache_hits"] == 0
    assert just_time.X.shape[1] == len(TIME_NAMES)
    assert full.X.shape[1] > just_time.X.shape[1]


def test_the_in_process_memo_serves_a_second_spec_without_refolding():
    bars = make_bars(wavy(120))
    stamps = [b.close_ms for b in bars[60:]]
    ds = make_dataset(bars)
    build_matrix(ds, [LabelDouble(ts, spec_key="a") for ts in stamps])
    again = build_matrix(ds, [LabelDouble(ts, spec_key="b") for ts in stamps])
    assert again.meta["cache_hits"] == 1


def test_a_corrupt_cache_file_costs_time_not_correctness(tmp_path):
    bars = make_bars(wavy(120))
    labels = labels_for(bars, first=60)
    ds = make_dataset(bars)
    good = build_matrix(ds, labels, cache_dir=tmp_path)
    for path in (tmp_path / "predict-matrix" / SYM).glob("*.npz"):
        path.write_bytes(b"not an npz")
    clear_matrix_cache()
    rebuilt = build_matrix(ds, labels, cache_dir=tmp_path)
    assert rebuilt.meta["cache_hits"] == 0
    assert rebuilt.X.tobytes() == good.X.tobytes()


# --------------------------------------------------------------------------- #
# Neutral-fallback reporting
# --------------------------------------------------------------------------- #


def test_a_group_that_is_always_neutral_is_reported_as_such():
    bars = make_bars([100.0] * 200, quote_volume=0.0, trades=0)
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=100))
    per_group = group_neutral_fractions(fm)
    # A dead tape has no volume information at all, and the report must say so
    # rather than let a tree split on a constant.  Seven of the eight volume
    # features are at their neutral; the eighth is ``active_frac``, whose whole
    # job is to report that the other seven are meaningless.
    assert per_group["volume"] > 0.8
    assert mx.group_all_neutral_fractions(fm)["volume"] == 0.0
    assert neutral_fractions(fm)["vol_accel_1m"] == 1.0
    assert neutral_fractions(fm)["active_frac_1m"] == 0.0
    # The clock features are always measurable; ``activity_regime`` is not, and
    # on a 200-minute dead tape it correctly reports the neutral because it has
    # not yet seen the eight completed hours its percentile needs.
    per_feature = neutral_fractions(fm)
    assert all(per_feature[n] == 0.0 for n in TIME_NAMES if n != "activity_regime")
    assert per_feature["activity_regime"] == 1.0


def test_neutral_fractions_cover_every_column():
    bars = make_bars(wavy(120))
    fm = build_matrix(make_dataset(bars), labels_for(bars, first=60))
    fractions = neutral_fractions(fm)
    assert set(fractions) == set(fm.feature_names)
    assert all(0.0 <= v <= 1.0 for v in fractions.values())


# --------------------------------------------------------------------------- #
# The real labels module, when it is available
# --------------------------------------------------------------------------- #


def test_build_matrix_consumes_real_labels():
    labels_mod = pytest.importorskip("app.predict.labels")
    closes = [100.0 + math.sin(i / 7.0) * 2.0 for i in range(400)]
    bars = make_bars(closes)
    spec = labels_mod.BarrierSpec(
        net_target_pct=0.12, stop_kind="fixed_net", stop_value=12.0,
        horizon_minutes=25, round_trip_bps=3.0,
    )
    labels = labels_mod.build_labels(bars, spec=spec, symbol=SYM)
    assert labels
    fm = build_matrix(make_dataset(bars), labels)
    assert len(fm) == len(labels)
    assert set(fm.y.tolist()) <= {0, 1}
    assert np.isfinite(fm.X).all()
    assert fm.spec_keys[0] == spec.key()


def test_build_matrix_consumes_the_columnar_label_form():
    labels_mod = pytest.importorskip("app.predict.labels")
    if not hasattr(labels_mod, "resolve_specs"):
        pytest.skip("columnar form not exposed")
    closes = [100.0 + math.sin(i / 7.0) * 2.0 for i in range(300)]
    bars = make_bars(closes)
    spec = labels_mod.BarrierSpec(
        net_target_pct=0.12, stop_kind="fixed_net", stop_value=12.0,
        horizon_minutes=25, round_trip_bps=3.0,
    )
    arrays = labels_mod.resolve_specs(bars, specs=[spec])
    fm = build_matrix(make_dataset(bars), arrays)
    objects = build_matrix(make_dataset(bars), labels_mod.build_labels(bars, spec=spec, symbol=SYM))
    assert len(fm) == len(objects)
    assert fm.X.tobytes() == objects.X.tobytes()
    assert fm.y.tolist() == objects.y.tolist()
