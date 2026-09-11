"""RegimeClassifier: every regime is reachable, ABNORMAL wins over everything,
and the reasons are populated because the dashboard renders them."""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.features import NO_BOOK_SPREAD_BPS, FeatureEngine
from app.regime import (
    BREAKOUT_COMPRESSION,
    BREAKOUT_VOLUME_RATIO,
    HIGH_VOL_PCT,
    LOW_VOL_PCT,
    MIN_BARS_FOR_REGIME,
    TREND_STRENGTH_MIN,
    RegimeClassifier,
)
from app.types import Regime

from fixtures.market import (
    empty_book,
    flat_closes,
    make_bars,
    make_features,
    make_snapshot,
    trend_closes,
    zigzag_closes,
)

S = get_settings()


@pytest.fixture()
def clf() -> RegimeClassifier:
    return RegimeClassifier()


# --------------------------------------------------------------------------- #
# ABNORMAL — the trading-blocking state
# --------------------------------------------------------------------------- #


def test_abnormal_on_extreme_vol_percentile(clf):
    state = clf.classify(make_features(vol_percentile=0.99))
    assert state.regime is Regime.ABNORMAL
    assert state.reasons


def test_abnormal_on_absolute_volatility_over_the_universe_cap(clf):
    f = make_features(realized_vol_bps=S.universe.max_volatility_bps + 1.0)
    assert clf.classify(f).regime is Regime.ABNORMAL


def test_abnormal_on_a_spread_blowout_versus_its_own_baseline(clf):
    f = make_features(spread_bps=7.0, extras={"spread_baseline_bps": 2.0})
    state = clf.classify(f)
    assert state.regime is Regime.ABNORMAL
    assert any("spread" in r for r in state.reasons)

    # 3x is the threshold, so exactly 3x is still normal.
    ok = make_features(spread_bps=6.0, extras={"spread_baseline_bps": 2.0})
    assert clf.classify(ok).regime is not Regime.ABNORMAL


def test_abnormal_on_a_stale_book(clf):
    f = make_features(book_age_ms=S.risk.max_book_age_ms + 1)
    state = clf.classify(f)
    assert state.regime is Regime.ABNORMAL
    assert any("stale" in r for r in state.reasons)


def test_abnormal_beats_every_other_signal(clf):
    """A textbook trend that arrives on stale data is still untradable."""
    f = make_features(
        trend_strength=0.9,
        ema_fast=101.0,
        ema_slow=100.0,
        ema_slope_bps=5.0,
        book_age_ms=S.risk.max_book_age_ms * 10,
    )
    assert clf.classify(f).regime is Regime.ABNORMAL


def test_missing_book_lands_in_abnormal(clf):
    f = FeatureEngine().compute(
        make_snapshot(bars=make_bars(flat_closes(60)), book=empty_book())
    )
    assert f.spread_bps == NO_BOOK_SPREAD_BPS
    assert clf.classify(f).regime is Regime.ABNORMAL


# --------------------------------------------------------------------------- #
# Warm-up
# --------------------------------------------------------------------------- #


def test_unknown_while_warming_up(clf):
    f = make_features(bars_available=MIN_BARS_FOR_REGIME - 1, atr_bps=0.0)
    state = clf.classify(f)
    assert state.regime is Regime.UNKNOWN
    assert state.confidence == 0.0


# --------------------------------------------------------------------------- #
# The tradable regimes
# --------------------------------------------------------------------------- #


def test_low_volatility(clf):
    state = clf.classify(make_features(vol_percentile=LOW_VOL_PCT - 0.05))
    assert state.regime is Regime.LOW_VOLATILITY
    assert 0.0 <= state.confidence <= 1.0


def test_high_volatility(clf):
    state = clf.classify(make_features(vol_percentile=HIGH_VOL_PCT + 0.05))
    assert state.regime is Regime.HIGH_VOLATILITY
    assert 0.0 <= state.confidence <= 1.0


def test_breakout_needs_compression_expansion_and_volume(clf):
    base = dict(
        vol_percentile=0.6,
        volume_ratio=BREAKOUT_VOLUME_RATIO + 0.5,
        bb_width_bps=120.0,
        extras={"compression_prev": BREAKOUT_COMPRESSION + 0.1,
                "bb_width_prev_bps": 40.0},
    )
    assert clf.classify(make_features(**base)).regime is Regime.BREAKOUT

    # ...without the volume it is not a breakout
    no_vol = dict(base)
    no_vol["volume_ratio"] = 1.0
    assert clf.classify(make_features(**no_vol)).regime is not Regime.BREAKOUT

    # ...without the prior squeeze either
    no_squeeze = dict(base)
    no_squeeze["extras"] = {"compression_prev": 0.1, "bb_width_prev_bps": 40.0}
    assert clf.classify(make_features(**no_squeeze)).regime is not Regime.BREAKOUT

    # ...nor without expansion
    no_expansion = dict(base)
    no_expansion["bb_width_bps"] = 40.0
    no_expansion["extras"] = {"compression_prev": BREAKOUT_COMPRESSION + 0.1,
                              "bb_width_prev_bps": 40.0, "range_bps": 1.0}
    assert clf.classify(make_features(**no_expansion)).regime is not Regime.BREAKOUT


def test_trending_requires_ema_alignment(clf):
    aligned = make_features(
        vol_percentile=0.5,
        trend_strength=TREND_STRENGTH_MIN + 0.2,
        ema_fast=101.0,
        ema_slow=100.0,
        ema_slope_bps=8.0,
    )
    state = clf.classify(aligned)
    assert state.regime is Regime.TRENDING
    assert state.confidence > 0.5

    # strong gap but the fast EMA is rolling over -> not a trend we will chase
    conflicted = make_features(
        vol_percentile=0.5,
        trend_strength=TREND_STRENGTH_MIN + 0.2,
        ema_fast=101.0,
        ema_slow=100.0,
        ema_slope_bps=-8.0,
    )
    assert clf.classify(conflicted).regime is Regime.RANGING


def test_trending_down_is_symmetric(clf):
    state = clf.classify(
        make_features(
            vol_percentile=0.5,
            trend_strength=0.8,
            ema_fast=99.0,
            ema_slow=100.0,
            ema_slope_bps=-8.0,
        )
    )
    assert state.regime is Regime.TRENDING
    assert any("down" in r for r in state.reasons)


def test_ranging_is_the_residual(clf):
    state = clf.classify(make_features(vol_percentile=0.5, trend_strength=0.1))
    assert state.regime is Regime.RANGING
    assert 0.0 <= state.confidence <= 1.0


@pytest.mark.parametrize(
    "over,expected",
    [
        ({"vol_percentile": 0.99}, Regime.ABNORMAL),
        ({"vol_percentile": 0.05}, Regime.LOW_VOLATILITY),
        ({"vol_percentile": 0.95}, Regime.HIGH_VOLATILITY),
        (
            {
                "vol_percentile": 0.6,
                "volume_ratio": 3.0,
                "bb_width_bps": 200.0,
                "extras": {"compression_prev": 0.9, "bb_width_prev_bps": 50.0},
            },
            Regime.BREAKOUT,
        ),
        (
            {
                "vol_percentile": 0.5,
                "trend_strength": 0.9,
                "ema_fast": 102.0,
                "ema_slow": 100.0,
                "ema_slope_bps": 10.0,
            },
            Regime.TRENDING,
        ),
        ({"vol_percentile": 0.5, "trend_strength": 0.05}, Regime.RANGING),
    ],
)
def test_every_regime_is_reachable(clf, over, expected):
    assert clf.classify(make_features(**over)).regime is expected


def test_confidence_is_always_a_probability(clf):
    for over in (
        {"vol_percentile": 1.0},
        {"vol_percentile": 0.0},
        {"vol_percentile": 0.9},
        {"trend_strength": 1.0, "ema_fast": 110.0, "ema_slope_bps": 50.0},
        {"trend_strength": 0.0, "range_pos": 1.0},
    ):
        state = clf.classify(make_features(**over))
        assert 0.0 <= state.confidence <= 1.0
        assert state.reasons


# --------------------------------------------------------------------------- #
# End-to-end against the real FeatureEngine
# --------------------------------------------------------------------------- #


def test_real_features_from_a_trending_series_classify_as_trending(clf):
    bars = make_bars(trend_closes(120, step_bps=20.0))
    f = FeatureEngine().compute(make_snapshot(bars=bars))
    state = clf.classify(f)
    assert state.regime in (Regime.TRENDING, Regime.HIGH_VOLATILITY,
                            Regime.LOW_VOLATILITY)
    assert state.regime is not Regime.ABNORMAL


def test_real_features_from_a_choppy_series_are_not_trending(clf):
    bars = make_bars(zigzag_closes(120, amp_bps=8.0))
    f = FeatureEngine().compute(make_snapshot(bars=bars))
    assert clf.classify(f).regime is not Regime.TRENDING
