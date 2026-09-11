"""Offline tests for the multi-timeframe feature engine.

Three properties are worth more than the rest and each has a dedicated test:

* **No leakage.**  A feature computed at time T must not change when bars after
  T are appended.  Every other result in Phase 3 is worthless if this is false,
  and the failure is silent, so it is asserted directly.
* **No NaN/inf, ever.**  Checked at cold start, on an entirely empty window (the
  normal state of this venue) and on a dead-flat series, which is where every
  guarded denominator actually gets exercised.
* **Documented neutrals.**  An unmeasurable ratio reports 1.0, an unmeasurable
  percentile reports 0.5, an unmeasurable RSI reports 50.0 — never 0.0, which
  already means "collapsed" and would be a different lie.
"""

from __future__ import annotations

import math

import pytest

from app.research.dataset import Dataset, build_bars
from app.research.features_mtf import (
    ATR_PERIOD,
    FEATURE_BASES,
    FEATURE_NAMES,
    MR_WINDOW,
    RANGE_WINDOW,
    RSI_PERIOD,
    VOL_SLOW_WINDOW,
    MTFFeatureEngine,
    assert_mtf_finite,
    feature_name,
)
from app.types import Bar, BookLevel, OrderBook, Trade

MINUTE = 60_000
T0 = 1_700_000_000_000
BASE = (T0 // MINUTE) * MINUTE
SYM = "BTCUSD"


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def bars_from(
    closes: list[float],
    *,
    interval_ms_: int = MINUTE,
    volume: float | list[float] = 10.0,
    taker_buy_frac: float | list[float] = 0.5,
    trades: int = 3,
) -> list[Bar]:
    """A clean OHLC series: each bar's high/low straddle its close by 0.1%."""
    out: list[Bar] = []
    for i, c in enumerate(closes):
        vol = volume[i] if isinstance(volume, list) else volume
        frac = taker_buy_frac[i] if isinstance(taker_buy_frac, list) else taker_buy_frac
        o = closes[i - 1] if i else c
        qv = c * vol
        out.append(
            Bar(
                symbol=SYM,
                open_ms=BASE + i * interval_ms_,
                close_ms=BASE + (i + 1) * interval_ms_ - 1,
                open=o,
                high=max(o, c) * 1.001,
                low=min(o, c) * 0.999,
                close=c,
                volume=vol,
                quote_volume=qv,
                trades=trades if vol > 0 else 0,
                taker_buy_quote=qv * frac,
            )
        )
    return out


def dataset_from(bars: list[Bar], *, interval: str = "1m", books=()) -> Dataset:
    return Dataset(
        symbol=SYM,
        bars={interval: bars},
        trades=[],
        start_ms=bars[0].open_ms if bars else BASE,
        end_ms=bars[-1].close_ms if bars else BASE,
        books=tuple(books),
    )


def engine(interval: str = "1m") -> MTFFeatureEngine:
    return MTFFeatureEngine(intervals=(interval,))


def at_last(bars: list[Bar], *, interval: str = "1m"):
    eng = engine(interval)
    ds = dataset_from(bars, interval=interval)
    return eng.update(ds, bars[-1].close_ms)


# --------------------------------------------------------------------------- #
# Naming contract
# --------------------------------------------------------------------------- #


def test_every_emitted_key_is_declared_in_feature_names():
    f = at_last(bars_from([100.0 + i for i in range(120)]))
    assert set(f.values) <= set(FEATURE_NAMES)
    assert set(f.values) == {feature_name(b, "1m") for b in FEATURE_BASES}


def test_intervals_disambiguate_identically_named_features():
    closes = [100.0 + i * 0.1 for i in range(400)]
    bars_1m = bars_from(closes)
    bars_5m = bars_from(closes[::5], interval_ms_=5 * MINUTE)
    ds = Dataset(
        symbol=SYM,
        bars={"1m": bars_1m, "5m": bars_5m},
        trades=[],
        start_ms=BASE,
        end_ms=bars_1m[-1].close_ms,
    )
    eng = MTFFeatureEngine(intervals=("1m", "5m"))
    f = eng.update(ds, bars_1m[-1].close_ms)

    assert "ret_1m" in f.values and "ret_5m" in f.values
    assert f.get("ret_1m") != f.get("ret_5m")


# --------------------------------------------------------------------------- #
# The maths, on hand-built series
# --------------------------------------------------------------------------- #


def test_returns_are_bps_over_the_stated_number_of_bars():
    closes = [100.0 * (1.001**i) for i in range(60)]
    f = at_last(bars_from(closes))

    assert f.get("ret_1m") == pytest.approx(10.0, abs=1e-6)          # +0.1% = 10 bps
    assert f.get("ret5_1m") == pytest.approx(
        (closes[-1] / closes[-6] - 1.0) * 10_000.0, rel=1e-9
    )


def test_momentum_slope_is_positive_up_negative_down_and_zero_flat():
    up = at_last(bars_from([100.0 + i for i in range(60)]))
    down = at_last(bars_from([200.0 - i for i in range(60)]))
    flat = at_last(bars_from([100.0] * 60))

    assert up.get("mom_slope_1m") > 0
    assert down.get("mom_slope_1m") < 0
    assert flat.get("mom_slope_1m") == pytest.approx(0.0, abs=1e-9)


def test_acceleration_is_the_change_in_slope():
    # The slope is measured in bps/bar, so "accelerating" means the *relative*
    # slope is rising.  A quadratic ramp does that; a straight line does not,
    # and a straight line at a rising price is very slightly decelerating in
    # percentage terms — which is the correct reading, not a bug.
    accelerating = at_last(bars_from([100.0 + 0.001 * i * i for i in range(60)]))
    steady = at_last(bars_from([100.0 + 0.01 * i for i in range(60)]))

    assert accelerating.get("accel_1m") > 0
    assert steady.get("accel_1m") == pytest.approx(0.0, abs=1e-2)


def test_atr_matches_a_hand_computed_wilder_average_on_a_flat_series():
    # A perfectly flat close series still has a true range, because the builder
    # gives each bar a +-0.1% wick: TR is a constant 0.2% of price.
    f = at_last(bars_from([100.0] * (ATR_PERIOD * 4)))
    expected_tr_bps = (100.0 * 1.001 - 100.0 * 0.999) / 100.0 * 10_000.0
    assert f.get("atr_1m") == pytest.approx(expected_tr_bps, rel=1e-6)


def test_rsi_is_100_on_an_unbroken_rally_and_0_on_an_unbroken_selloff():
    up = at_last(bars_from([100.0 + i for i in range(RSI_PERIOD * 4)]))
    down = at_last(bars_from([200.0 - i for i in range(RSI_PERIOD * 4)]))

    assert up.get("rsi_1m") == pytest.approx(100.0)
    assert down.get("rsi_1m") == pytest.approx(0.0)


def test_rsi_on_a_dead_flat_series_is_the_neutral_50_not_a_divide_by_zero():
    f = at_last(bars_from([100.0] * (RSI_PERIOD * 4)))
    assert f.get("rsi_1m") == pytest.approx(50.0)


def test_zscore_is_signed_distance_from_the_trailing_mean():
    closes = [100.0] * (MR_WINDOW * 2) + [110.0]
    f = at_last(bars_from(closes))
    assert f.get("zscore_1m") > 2.0

    closes_low = [100.0] * (MR_WINDOW * 2) + [90.0]
    assert at_last(bars_from(closes_low)).get("zscore_1m") < -2.0


def test_zscore_on_a_zero_variance_window_is_zero_not_infinite():
    f = at_last(bars_from([100.0] * (MR_WINDOW * 3)))
    assert f.get("zscore_1m") == 0.0


def test_zscore_is_clipped_so_one_outlier_cannot_dominate_a_ranking():
    closes = [100.0] * (MR_WINDOW * 2) + [100.0001] * (MR_WINDOW - 1) + [500.0]
    f = at_last(bars_from(closes))
    assert abs(f.get("zscore_1m")) <= 10.0


def test_breakout_distance_is_zero_inside_the_range_and_signed_outside():
    inside = at_last(bars_from([100.0 + (i % 3) for i in range(RANGE_WINDOW * 3)]))
    assert inside.get("breakout_dist_1m") == 0.0

    up = at_last(bars_from([100.0] * (RANGE_WINDOW * 2) + [130.0]))
    assert up.get("breakout_dist_1m") > 0

    down = at_last(bars_from([100.0] * (RANGE_WINDOW * 2) + [70.0]))
    assert down.get("breakout_dist_1m") < 0


def test_breakout_uses_the_range_before_the_bar_not_including_it():
    # If the current bar were inside its own window, a new high could never be
    # detected: the bar would always be at the top of the range it defined.
    f = at_last(bars_from([100.0] * (RANGE_WINDOW * 2) + [101.0]))
    assert f.get("breakout_dist_1m") > 0


def test_range_position_spans_zero_to_one():
    top = at_last(bars_from([100.0 + i for i in range(RANGE_WINDOW * 2)]))
    bottom = at_last(bars_from([200.0 - i for i in range(RANGE_WINDOW * 2)]))

    assert top.get("range_pos_1m") > 0.9
    assert bottom.get("range_pos_1m") < 0.1


def test_vwap_distance_is_positive_above_the_rolling_vwap():
    rising = at_last(bars_from([100.0 + i for i in range(80)]))
    falling = at_last(bars_from([200.0 - i for i in range(80)]))

    assert rising.get("vwap_dist_1m") > 0
    assert falling.get("vwap_dist_1m") < 0


def test_aggressor_imbalance_maps_taker_buy_share_onto_minus_one_to_one():
    all_buy = at_last(bars_from([100.0] * 40, taker_buy_frac=1.0))
    all_sell = at_last(bars_from([100.0] * 40, taker_buy_frac=0.0))
    balanced = at_last(bars_from([100.0] * 40, taker_buy_frac=0.5))

    assert all_buy.get("aggr_imb_1m") == pytest.approx(1.0)
    assert all_sell.get("aggr_imb_1m") == pytest.approx(-1.0)
    assert balanced.get("aggr_imb_1m") == pytest.approx(0.0, abs=1e-9)


def test_trade_count_is_the_last_closed_bars_count():
    f = at_last(bars_from([100.0] * 40, trades=7))
    assert f.get("trades_1m") == 7.0


# --------------------------------------------------------------------------- #
# The thin-tape rule: volume features degrade to neutral, never to nonsense
# --------------------------------------------------------------------------- #


def test_volume_acceleration_is_neutral_before_the_baseline_window_fills():
    f = at_last(bars_from([100.0] * (VOL_SLOW_WINDOW - 1)))
    assert f.get("vol_accel_1m") == 1.0
    assert f.get("vol_pct_1m") == 0.5


def test_volume_features_stay_neutral_when_the_baseline_is_mostly_empty():
    # The venue's normal state: one print after a long silence.  A naive ratio
    # reports a 1,200x "surge" here; the neutral answer is that the baseline is
    # not measurable.  See docs/ECONOMICS.md and app/features._volume_ratio.
    vols = [0.0] * 40 + [50.0]
    f = at_last(bars_from([100.0] * 41, volume=vols))

    assert f.get("vol_accel_1m") == 1.0
    assert f.get("vol_pct_1m") == 0.5


def test_volume_acceleration_reacts_once_the_baseline_is_real():
    vols = [10.0] * 40 + [60.0] * 3
    f = at_last(bars_from([100.0] * 43, volume=vols))
    assert f.get("vol_accel_1m") > 1.5


def test_volume_acceleration_is_capped_so_a_ratio_cannot_dominate_a_score():
    vols = [1.0] * 40 + [10_000_000.0]
    f = at_last(bars_from([100.0] * 41, volume=vols))
    assert f.get("vol_accel_1m") == 20.0


# --------------------------------------------------------------------------- #
# Finiteness at every edge
# --------------------------------------------------------------------------- #


def _assert_all_finite(f):
    assert_mtf_finite(f)
    for name, value in f.values.items():
        assert not math.isnan(value), name
        assert not math.isinf(value), name


def test_cold_start_has_no_bars_and_reports_documented_neutrals():
    ds = dataset_from(bars_from([100.0] * 10))
    f = engine().update(ds, BASE - 1)  # before anything closed

    _assert_all_finite(f)
    assert f.price == 0.0
    assert f.get("vol_accel_1m") == 1.0
    assert f.get("rsi_1m") == 50.0
    assert f.get("rvol_pct_1m") == 0.5
    assert f.get("range_pos_1m") == 0.5
    assert f.get("ret_1m") == 0.0


def test_an_entirely_empty_window_is_finite_and_neutral():
    # Every bar a gap bar: the state of most of this tape.
    bars = bars_from([100.0] * 200, volume=0.0, trades=0)
    f = at_last(bars)

    _assert_all_finite(f)
    assert f.get("vol_accel_1m") == 1.0
    assert f.get("vol_pct_1m") == 0.5
    assert f.get("aggr_imb_1m") == 0.0
    assert f.get("trades_1m") == 0.0


def test_a_constant_series_is_finite_everywhere():
    f = at_last(bars_from([100.0] * 300))
    _assert_all_finite(f)
    assert f.get("rv_1m") == pytest.approx(0.0)
    assert f.get("ret_1m") == pytest.approx(0.0)


def test_every_step_of_a_real_shaped_tape_is_finite():
    # Built from actual prints through build_bars, gaps and all.
    trades = []
    price = 100.0
    for i in range(600):
        if i % 3:  # two thirds of minutes are silent
            continue
        price *= 1.0 + ((i % 7) - 3) * 0.0005
        trades.append(
            Trade(
                symbol=SYM,
                price=price,
                qty=0.01 * (1 + i % 4),
                ts_ms=BASE + i * MINUTE + 100,
                buyer_is_maker=bool(i % 2),
            )
        )
    bars = build_bars(trades, "1m")
    ds = dataset_from(bars)
    eng = engine()
    for f in eng.stream(ds, step="1m"):
        _assert_all_finite(f)


def test_a_zero_priced_bar_is_ignored_rather_than_poisoning_the_state():
    bars = bars_from([100.0] * 40)
    broken = bars[20]
    bars[20] = Bar(
        symbol=broken.symbol,
        open_ms=broken.open_ms,
        close_ms=broken.close_ms,
        open=0.0,
        high=0.0,
        low=0.0,
        close=0.0,
        volume=0.0,
        quote_volume=0.0,
        trades=0,
    )
    _assert_all_finite(at_last(bars))


# --------------------------------------------------------------------------- #
# Leakage
# --------------------------------------------------------------------------- #


def test_features_at_T_are_unchanged_when_future_bars_are_appended():
    past = bars_from([100.0 + math.sin(i / 4.0) * 3 for i in range(200)])
    future = past + bars_from(
        [500.0 + i * 10 for i in range(200, 260)]
    )[200:]
    index_ms = past[-1].close_ms

    a = MTFFeatureEngine(intervals=("1m",)).update(dataset_from(past), index_ms)
    b = MTFFeatureEngine(intervals=("1m",)).update(dataset_from(future), index_ms)

    assert a.values == b.values
    assert a.price == b.price


def test_no_bar_that_had_not_closed_is_ever_folded():
    bars = bars_from([100.0 + i for i in range(50)])
    ds = dataset_from(bars)
    eng = engine()

    # One millisecond before bar 30 closes, its close price must be invisible.
    before = eng.update(ds, bars[30].close_ms - 1)
    assert before.price == bars[29].close

    after = engine().update(ds, bars[30].close_ms)
    assert after.price == bars[30].close


def test_streaming_matches_a_fresh_engine_at_every_instant():
    # The incremental path and the from-scratch path must not diverge, or the
    # tournament's cheap iteration would silently disagree with a spot check.
    bars = bars_from([100.0 + math.sin(i / 5.0) * 2 + i * 0.01 for i in range(300)])
    ds = dataset_from(bars)

    streamed = list(engine().stream(ds, step="1m"))
    assert len(streamed) == len(bars)

    for i in (60, 150, 299):
        fresh = MTFFeatureEngine(intervals=("1m",)).update(ds, bars[i].close_ms)
        assert streamed[i].values == fresh.values


def test_a_backwards_seek_rebuilds_instead_of_reporting_stale_state():
    bars = bars_from([100.0 + i for i in range(120)])
    ds = dataset_from(bars)
    eng = engine()

    eng.update(ds, bars[-1].close_ms)
    rewound = eng.update(ds, bars[60].close_ms)
    fresh = MTFFeatureEngine(intervals=("1m",)).update(ds, bars[60].close_ms)

    assert rewound.values == fresh.values


# --------------------------------------------------------------------------- #
# Book features: present when there is a book, absent when there is not
# --------------------------------------------------------------------------- #


def make_book(ts_ms: int, *, bid=100.0, ask=100.1, bid_qty=5.0, ask_qty=1.0) -> OrderBook:
    b = OrderBook(symbol=SYM)
    b.bids = [BookLevel(bid, bid_qty)]
    b.asks = [BookLevel(ask, ask_qty)]
    b.last_update_ms = ts_ms
    b.last_update_id = ts_ms
    return b


def test_book_features_are_omitted_when_the_dataset_carries_no_book():
    f = at_last(bars_from([100.0] * 60))
    assert "spread_bps" not in f.values
    assert "book_imbalance" not in f.values


def test_book_features_appear_when_a_book_is_attached():
    bars = bars_from([100.0] * 60)
    ds = dataset_from(bars, books=[make_book(bars[10].close_ms)])
    f = engine().update(ds, bars[-1].close_ms)

    assert f.get("spread_bps") > 0
    assert f.get("book_imbalance") > 0        # more resting bid than ask
    assert f.get("microprice_dist_bps") > 0   # microprice leans to the thin side
    assert f.get("depth_bid_usd") > 0


def test_a_book_published_after_the_instant_is_not_visible():
    bars = bars_from([100.0] * 60)
    ds = dataset_from(bars, books=[make_book(bars[-1].close_ms + 10_000)])
    f = engine().update(ds, bars[-1].close_ms)
    assert "spread_bps" not in f.values
