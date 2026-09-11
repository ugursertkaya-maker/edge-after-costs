"""FeatureEngine: exact maths on known series, cold-start safety, and the
guarantee that folding bars in one at a time gives the same answer as folding
them all at once."""

from __future__ import annotations

import math

import pytest

from app.features import (
    ATR_PERIOD,
    EMA_FAST_PERIOD,
    EMA_SLOW_PERIOD,
    NO_BOOK_SPREAD_BPS,
    FeatureEngine,
    assert_features_finite,
)
from app.types import Features

from fixtures.market import (
    BASE_MS,
    MINUTE_MS,
    SYMBOL,
    empty_book,
    flat_closes,
    flow_trades,
    make_bar,
    make_bars,
    make_book,
    make_features,
    make_snapshot,
    trend_closes,
    zigzag_closes,
)

BAR_FIELDS = (
    "atr_bps",
    "realized_vol_bps",
    "vol_percentile",
    "ret_1m_bps",
    "ret_5m_bps",
    "ret_15m_bps",
    "ema_fast",
    "ema_slow",
    "ema_slope_bps",
    "trend_strength",
    "range_pos",
    "bb_width_bps",
    "compression",
    "volume_ratio",
    "vwap",
    "bars_available",
)


def compute(bars, **snap_kw) -> Features:
    eng = FeatureEngine()
    return eng.compute(make_snapshot(bars=bars, **snap_kw))


# --------------------------------------------------------------------------- #
# ATR
# --------------------------------------------------------------------------- #


def test_atr_is_wilder_on_a_constant_true_range():
    # Every bar: high 100.1, low 100.0, close 100.0 => TR is exactly 0.10.
    bars = [
        make_bar(index=i, open_=100.0, high=100.1, low=100.0, close=100.0)
        for i in range(40)
    ]
    f = compute(bars)
    assert f.bars_available == 40
    # ATR = 0.10 in price terms => 10 bps of a 100.00 price.
    assert f.atr_bps == pytest.approx(10.0, rel=1e-9)


def test_atr_needs_a_full_period_before_it_reports():
    bars = [
        make_bar(index=i, open_=100.0, high=100.1, low=100.0, close=100.0)
        for i in range(ATR_PERIOD)  # 14 bars => only 13 true ranges
    ]
    f = compute(bars)
    assert f.atr_bps == 0.0


def test_atr_smooths_a_shock_by_one_fourteenth():
    bars = [
        make_bar(index=i, open_=100.0, high=100.1, low=100.0, close=100.0)
        for i in range(20)
    ]
    bars.append(make_bar(index=20, open_=100.0, high=101.5, low=100.0, close=100.0))
    f = compute(bars)
    expected_atr = 0.1 + (1.5 - 0.1) / ATR_PERIOD
    assert f.atr_bps == pytest.approx(expected_atr / 100.0 * 10_000.0, rel=1e-9)


# --------------------------------------------------------------------------- #
# EMAs, slope, trend strength
# --------------------------------------------------------------------------- #


def test_emas_seed_at_the_first_close_and_track_a_step():
    closes = flat_closes(40, 100.0) + [110.0]
    bars = make_bars(closes, wick_frac=0.0005)
    f = compute(bars)

    a_fast = 2.0 / (EMA_FAST_PERIOD + 1)
    a_slow = 2.0 / (EMA_SLOW_PERIOD + 1)
    assert f.ema_fast == pytest.approx(100.0 + a_fast * 10.0, rel=1e-9)
    assert f.ema_slow == pytest.approx(100.0 + a_slow * 10.0, rel=1e-9)
    # slope is the per-bar change of the fast EMA, in bps of the close
    assert f.ema_slope_bps == pytest.approx(
        (a_fast * 10.0) / 110.0 * 10_000.0, rel=1e-9
    )


def test_trend_strength_is_ema_gap_over_atr_clipped_to_one():
    f = compute(make_bars(trend_closes(90, step_bps=25.0)))
    assert 0.0 < f.trend_strength <= 1.0
    assert f.ema_fast > f.ema_slow
    assert f.ema_slope_bps > 0.0

    flat = compute(make_bars(flat_closes(90)))
    assert flat.trend_strength == 0.0


def test_trend_strength_is_neutral_before_the_slow_ema_window_fills():
    f = compute(make_bars(trend_closes(EMA_SLOW_PERIOD - 1, step_bps=25.0)))
    assert f.trend_strength == 0.0
    assert f.ema_slope_bps == 0.0


# --------------------------------------------------------------------------- #
# Realized vol / percentile
# --------------------------------------------------------------------------- #


def test_realized_vol_is_zero_on_a_flat_path_and_positive_on_a_zigzag():
    flat = compute(make_bars(flat_closes(60)))
    assert flat.realized_vol_bps == 0.0

    noisy = compute(make_bars(zigzag_closes(60, amp_bps=10.0)))
    assert noisy.realized_vol_bps > 0.0


def test_realized_vol_matches_the_stdev_of_the_last_30_log_returns():
    closes = zigzag_closes(60, amp_bps=10.0)
    f = compute(make_bars(closes))

    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))][-30:]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    assert f.realized_vol_bps == pytest.approx(math.sqrt(var) * 10_000.0, rel=1e-9)


def test_vol_percentile_is_neutral_until_enough_observations():
    f = compute(make_bars(zigzag_closes(40, amp_bps=10.0)))
    assert f.vol_percentile == 0.5


def test_vol_percentile_is_high_when_volatility_expands():
    quiet = zigzag_closes(120, amp_bps=2.0)
    loud = [quiet[-1] * (1.0 + (0.02 if i % 2 else -0.02)) for i in range(40)]
    f = compute(make_bars(quiet + loud))
    assert f.vol_percentile > 0.85


# --------------------------------------------------------------------------- #
# Range position, Bollinger width, compression
# --------------------------------------------------------------------------- #


def test_range_pos_is_one_at_a_fresh_twenty_bar_high():
    closes = flat_closes(29, 100.0) + [101.0]
    f = compute(make_bars(closes, wick_frac=0.0))
    assert f.range_pos == pytest.approx(1.0)


def test_range_pos_is_zero_at_a_fresh_twenty_bar_low():
    closes = flat_closes(29, 100.0) + [99.0]
    f = compute(make_bars(closes, wick_frac=0.0))
    assert f.range_pos == pytest.approx(0.0)


def test_range_pos_is_neutral_on_a_degenerate_range():
    f = compute(make_bars(flat_closes(30), wick_frac=0.0))
    assert f.range_pos == 0.5


def test_bb_width_is_four_sigma_in_bps():
    closes = [99.0 if i % 2 else 101.0 for i in range(20)]
    f = compute(make_bars(closes, wick_frac=0.0))

    mean = 100.0
    var = sum((c - mean) ** 2 for c in closes) / (len(closes) - 1)
    sigma = math.sqrt(var)
    assert f.bb_width_bps == pytest.approx(4.0 * sigma / mean * 10_000.0, rel=1e-9)


def test_compression_is_high_when_bands_are_tightest_in_120_bars():
    wide = zigzag_closes(140, amp_bps=30.0)
    tight = [wide[-1]] * 30
    f = compute(make_bars(wide + tight, wick_frac=0.0))
    assert f.compression > 0.8


def test_compression_is_neutral_before_its_window_fills():
    f = compute(make_bars(zigzag_closes(25, amp_bps=10.0), wick_frac=0.0))
    assert f.compression == 0.5


# --------------------------------------------------------------------------- #
# Volume, returns, VWAP
# --------------------------------------------------------------------------- #


def test_volume_ratio_is_last_bar_over_the_mean_of_the_previous_twenty():
    closes = flat_closes(21)
    vols = [100_000.0] * 20 + [250_000.0]
    f = compute(make_bars(closes, quote_volumes=vols))
    assert f.volume_ratio == pytest.approx(2.5, rel=1e-9)


def test_volume_ratio_is_neutral_until_the_window_fills():
    f = compute(make_bars(flat_closes(10), quote_volumes=[100_000.0] * 10))
    assert f.volume_ratio == 1.0


def test_multi_horizon_returns():
    closes = [100.0] * 20 + [101.0, 102.0, 103.0, 104.0, 105.0]
    f = compute(make_bars(closes, wick_frac=0.0))
    assert f.ret_1m_bps == pytest.approx((105.0 / 104.0 - 1) * 10_000.0, rel=1e-9)
    assert f.ret_5m_bps == pytest.approx((105.0 / 100.0 - 1) * 10_000.0, rel=1e-9)
    assert f.ret_15m_bps == pytest.approx((105.0 / 100.0 - 1) * 10_000.0, rel=1e-9)


def test_vwap_is_volume_weighted_typical_price():
    bars = [
        make_bar(index=0, open_=100.0, high=102.0, low=98.0, close=100.0,
                 quote_volume=100.0),
        make_bar(index=1, open_=100.0, high=112.0, low=104.0, close=110.0,
                 quote_volume=220.0),
    ]
    f = compute(bars, book=make_book(mid=110.0, ts_ms=BASE_MS))

    pv = 0.0
    vol = 0.0
    for b in bars:
        typical = (b.high + b.low + b.close) / 3.0
        pv += typical * b.volume
        vol += b.volume
    assert f.vwap == pytest.approx(pv / vol, rel=1e-9)
    assert f.vwap_dev_bps == pytest.approx(
        (f.price - f.vwap) / f.vwap * 10_000.0, rel=1e-9
    )


# --------------------------------------------------------------------------- #
# Book-derived fields
# --------------------------------------------------------------------------- #


def test_book_fields_come_straight_off_the_snapshot():
    bars = make_bars(flat_closes(40))
    book = make_book(mid=100.0, spread_bps=4.0, bid_qty=90.0, ask_qty=10.0,
                     ts_ms=BASE_MS)
    f = compute(bars, book=book, ts_ms=BASE_MS + 750)

    assert f.mid == pytest.approx(100.0)
    assert f.spread_bps == pytest.approx(4.0, rel=1e-9)
    assert f.book_age_ms == 750
    assert f.imbalance == pytest.approx(book.imbalance(10.0), rel=1e-9)
    assert f.depth_bid_usd > f.depth_ask_usd
    # heavy bid, thin ask => microprice leans up
    assert f.microprice > f.mid


def test_spread_baseline_tracks_normal_and_ignores_a_blowout():
    eng = FeatureEngine()
    bars = make_bars(flat_closes(60))
    for i in range(len(bars)):
        f = eng.compute(
            make_snapshot(
                bars=bars[: i + 1],
                book=make_book(mid=100.0, spread_bps=2.0, ts_ms=BASE_MS),
                ts_ms=BASE_MS + 100,
            )
        )
    assert f.extras["spread_baseline_bps"] == pytest.approx(2.0, rel=1e-9)

    wide = eng.compute(
        make_snapshot(
            bars=bars + make_bars(flat_closes(1), start_ms=BASE_MS + 60 * MINUTE_MS),
            book=make_book(mid=100.0, spread_bps=60.0, ts_ms=BASE_MS),
            ts_ms=BASE_MS + 100,
        )
    )
    assert wide.spread_bps == pytest.approx(60.0, rel=1e-9)
    # the blowout must not be folded into what "normal" means
    assert wide.extras["spread_baseline_bps"] == pytest.approx(2.0, rel=1e-9)


# --------------------------------------------------------------------------- #
# Trade flow
# --------------------------------------------------------------------------- #


def test_trade_flow_imbalance_and_acceleration():
    bars = make_bars(flat_closes(40))
    ts = bars[-1].close_ms + 1
    trades = flow_trades(ts_ms=ts, imb_60=0.4, imb_15=0.8)
    f = compute(bars, trades=trades, ts_ms=ts)
    assert f.trade_flow_imbalance == pytest.approx(0.4, rel=1e-9)
    assert f.trade_flow_accel == pytest.approx(0.4, rel=1e-9)


def test_trade_flow_ignores_trades_outside_the_sixty_second_window():
    bars = make_bars(flat_closes(40))
    ts = bars[-1].close_ms + 1
    stale = flow_trades(ts_ms=ts - 120_000, imb_60=1.0)
    f = compute(bars, trades=stale, ts_ms=ts)
    assert f.trade_flow_imbalance == 0.0
    assert f.trade_flow_accel == 0.0


def test_trade_flow_is_zero_without_trades():
    f = compute(make_bars(flat_closes(40)), trades=[])
    assert f.trade_flow_imbalance == 0.0
    assert f.trade_flow_accel == 0.0


# --------------------------------------------------------------------------- #
# Cold start / robustness
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("n", [0, 1, 2, 5, 14, 20, 29, 31, 60])
def test_cold_start_is_finite_and_neutral(n):
    f = compute(make_bars(flat_closes(n)) if n else [])
    assert_features_finite(f)
    assert f.bars_available == n
    assert 0.0 <= f.vol_percentile <= 1.0
    assert 0.0 <= f.range_pos <= 1.0
    assert 0.0 <= f.compression <= 1.0
    assert f.volume_ratio > 0.0
    if n < 15:
        assert f.atr_bps == 0.0


def test_missing_book_does_not_raise_and_stays_finite():
    f = compute(make_bars(flat_closes(40)), book=empty_book())
    assert_features_finite(f)
    assert f.spread_bps == NO_BOOK_SPREAD_BPS
    assert f.mid == 0.0
    assert f.book_age_ms > 0


def test_desynced_book_is_treated_as_unusable():
    book = make_book(ts_ms=BASE_MS, desynced=True)
    f = compute(make_bars(flat_closes(40)), book=book, ts_ms=BASE_MS + 10)
    assert f.spread_bps == NO_BOOK_SPREAD_BPS
    assert f.depth_bid_usd == 0.0


def test_degenerate_bars_do_not_produce_nan():
    bars = make_bars(flat_closes(40))
    bars.append(make_bar(index=40, open_=0.0, high=0.0, low=0.0, close=0.0,
                         quote_volume=0.0))
    f = compute(bars)
    assert_features_finite(f)


def test_guard_rejects_a_non_finite_field():
    bad = make_features(atr_bps=float("nan"))
    with pytest.raises(ValueError, match="atr_bps"):
        assert_features_finite(bad)

    bad_extra = make_features(extras={"spread_baseline_bps": float("inf")})
    with pytest.raises(ValueError, match="spread_baseline_bps"):
        assert_features_finite(bad_extra)


# --------------------------------------------------------------------------- #
# Incrementality
# --------------------------------------------------------------------------- #


def test_incremental_equals_from_scratch():
    closes = trend_closes(60, step_bps=12.0) + zigzag_closes(60, start=101.0,
                                                             amp_bps=15.0)
    bars = make_bars(closes)
    book = make_book(mid=bars[-1].close, ts_ms=BASE_MS)
    ts = BASE_MS + 500

    stepwise = FeatureEngine()
    for i in range(len(bars)):
        incremental = stepwise.compute(
            make_snapshot(bars=bars[: i + 1], book=book, ts_ms=ts)
        )

    scratch = FeatureEngine().compute(make_snapshot(bars=bars, book=book, ts_ms=ts))

    for name in BAR_FIELDS:
        assert getattr(incremental, name) == pytest.approx(
            getattr(scratch, name), rel=1e-12, abs=1e-12
        ), name
    # "new_bars" is a per-call diagnostic, not a feature, so it legitimately
    # differs between the two paths; everything else must agree exactly.
    for key in scratch.extras:
        if key == "new_bars":
            continue
        assert incremental.extras[key] == pytest.approx(
            scratch.extras[key], rel=1e-12, abs=1e-12
        ), key


def test_already_seen_bars_are_not_folded_twice():
    bars = make_bars(flat_closes(40))
    eng = FeatureEngine()
    first = eng.compute(make_snapshot(bars=bars))
    again = eng.compute(make_snapshot(bars=bars))
    assert again.bars_available == first.bars_available == 40
    assert again.atr_bps == first.atr_bps


def test_reset_clears_state():
    bars = make_bars(flat_closes(40))
    eng = FeatureEngine()
    eng.compute(make_snapshot(bars=bars))
    eng.reset(SYMBOL)
    f = eng.compute(make_snapshot(bars=bars[:3]))
    assert f.bars_available == 3

    eng.compute(make_snapshot(bars=bars))
    eng.reset()
    assert eng.compute(make_snapshot(bars=bars[:2])).bars_available == 2


# --------------------------------------------------------------------------- #
# Volume ratio on a mostly-empty tape (Binance.US reality)
# --------------------------------------------------------------------------- #


def test_volume_ratio_is_neutral_when_the_baseline_is_unmeasurable():
    """55% of BTC/USD 1m bars have no trades at all. A ratio against a mean of
    ~0 is arithmetic, not information — and 1291x fires every volume gate."""
    from app.features import MAX_VOLUME_RATIO, VOLUME_WINDOW, _volume_ratio

    empty = [0.0] * VOLUME_WINDOW
    assert _volume_ratio(empty, 5_000.0) == 1.0

    mostly_empty = [0.0] * (VOLUME_WINDOW - 2) + [10.0, 10.0]
    assert _volume_ratio(mostly_empty, 5_000.0) == 1.0


def test_volume_ratio_is_capped():
    from app.features import MAX_VOLUME_RATIO, VOLUME_WINDOW, _volume_ratio

    active = [100.0] * VOLUME_WINDOW
    assert _volume_ratio(active, 1_000_000.0) == MAX_VOLUME_RATIO


def test_volume_ratio_is_exact_on_a_measurable_window():
    from app.features import VOLUME_WINDOW, _volume_ratio

    window = [100.0] * VOLUME_WINDOW
    assert _volume_ratio(window, 180.0) == pytest.approx(1.8)


def test_volume_ratio_needs_a_full_window_first():
    from app.features import VOLUME_WINDOW, _volume_ratio

    assert _volume_ratio([100.0] * (VOLUME_WINDOW - 1), 500.0) == 1.0


def test_a_quiet_spell_then_one_print_does_not_look_like_a_breakout():
    """The regression: a single trade after silence used to read as a 1291x
    volume expansion and trip every breakout gate."""
    from app.features import VOLUME_WINDOW, _volume_ratio

    quiet = [0.0] * (VOLUME_WINDOW - 1) + [50.0]
    assert _volume_ratio(quiet, 5_000.0) == 1.0
