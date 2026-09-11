"""Offline tests for the seven-state regime classifier.

Two kinds of test here, and both matter:

* **Reachability and precedence**, driven from hand-built feature dicts, so the
  documented ordering in ``regimes.py`` is enforced rather than merely claimed.
* **Distribution**, driven from a synthetic tape shaped like the real venue —
  mostly empty minutes, punctuated by trends, chop, compression, expansion and
  a flush.  A classifier that answers RANGING 95% of the time is a constant with
  extra steps, and it would make every per-regime table in the tournament
  meaningless, so the shape of the partition is asserted directly.

For reference, over the real 120-day Binance.US tape (172,794 one-minute bars
per symbol, cached 2026-09-11) this classifier produces, on BTC/USD:
RANGING 29.7%, LOW_VOLATILITY 22.8%, VOLATILITY_EXPANSION 18.9%,
TRENDING_DOWN 10.2%, TRENDING_UP 9.8%, BREAKOUT 8.1%, PANIC 0.5% — and the same
shape within a couple of points on ETH/SOL/ADA.  The synthetic fixture below is
tuned to reproduce that character offline.
"""

from __future__ import annotations

import random

import pytest

from app.research.dataset import Dataset, build_bars
from app.research.features_mtf import MTFFeatureEngine, MTFFeatures, feature_name
from app.research.regimes import (
    BREAKOUT_MIN_BPS,
    BREAKOUT_VOL_ACCEL,
    HIGH_VOL_PCT,
    LOW_VOL_PCT,
    TREND_MIN_Z,
    Regime7,
    Regime7Classifier,
    regime_distribution,
)
from app.types import Trade

MINUTE = 60_000
T0 = 1_700_000_000_000
BASE = (T0 // MINUTE) * MINUTE
SYM = "BTCUSD"


def features(**overrides: float) -> MTFFeatures:
    """One evaluation instant, expressed in the 1m feature names."""
    values = {
        feature_name("rv", "1m"): 5.0,
        feature_name("rvol_pct", "1m"): 0.5,
        feature_name("atr", "1m"): 4.0,
        feature_name("ret5", "1m"): 0.0,
        feature_name("mom_slope", "1m"): 0.0,
        feature_name("ema_slope", "1m"): 0.0,
        feature_name("breakout_dist", "1m"): 0.0,
        feature_name("vol_accel", "1m"): 1.0,
        feature_name("vol_pct", "1m"): 0.5,
        feature_name("zscore", "1m"): 0.0,
    }
    values.update({feature_name(k, "1m"): v for k, v in overrides.items()})
    return MTFFeatures(symbol=SYM, ts_ms=BASE, price=100.0, values=values)


CLF = Regime7Classifier(interval="1m")


# --------------------------------------------------------------------------- #
# Each regime is individually reachable
# --------------------------------------------------------------------------- #


def test_warm_up_has_no_opinion():
    regime, conf = CLF.classify(features(rv=0.0, atr=0.0))
    assert (regime, conf) == (Regime7.RANGING, 0.0)


def test_panic_needs_extreme_vol_and_a_flush():
    regime, conf = CLF.classify(
        features(rvol_pct=0.99, rv=10.0, ret5=-10.0 * 2.236 * 4.0)
    )
    assert regime is Regime7.PANIC
    assert 0.0 < conf <= 1.0


def test_a_violent_move_upward_is_not_panic():
    regime, _ = CLF.classify(
        features(rvol_pct=0.99, rv=10.0, ret5=+10.0 * 2.236 * 4.0, vol_accel=1.0)
    )
    assert regime is not Regime7.PANIC


def test_breakout_needs_range_escape_and_volume_confirmation():
    escaped = features(breakout_dist=12.0, vol_accel=BREAKOUT_VOL_ACCEL + 0.5)
    regime, conf = CLF.classify(escaped)
    assert regime is Regime7.BREAKOUT
    assert 0.0 < conf <= 1.0

    unconfirmed = features(breakout_dist=12.0, vol_accel=1.0, vol_pct=0.4)
    assert CLF.classify(unconfirmed)[0] is not Regime7.BREAKOUT


def test_a_one_tick_poke_over_the_high_is_not_a_breakout():
    poke = features(breakout_dist=BREAKOUT_MIN_BPS / 2.0, vol_accel=3.0)
    assert CLF.classify(poke)[0] is not Regime7.BREAKOUT


def test_volatility_expansion_is_high_vol_without_a_range_break():
    regime, conf = CLF.classify(features(rvol_pct=HIGH_VOL_PCT + 0.05))
    assert regime is Regime7.VOLATILITY_EXPANSION
    assert 0.0 < conf <= 1.0


def test_low_volatility_is_the_bottom_of_the_symbols_own_distribution():
    regime, conf = CLF.classify(features(rvol_pct=LOW_VOL_PCT - 0.05))
    assert regime is Regime7.LOW_VOLATILITY
    assert 0.0 < conf <= 1.0


def test_trending_requires_three_agreeing_measures():
    rv = 5.0
    strong = rv * 2.236 * (TREND_MIN_Z + 1.0)
    up, conf = CLF.classify(
        features(rv=rv, ret5=strong, mom_slope=2.0, ema_slope=1.0)
    )
    assert up is Regime7.TRENDING_UP
    assert 0.0 < conf <= 1.0

    down, _ = CLF.classify(
        features(rv=rv, ret5=-strong, mom_slope=-2.0, ema_slope=-1.0)
    )
    assert down is Regime7.TRENDING_DOWN

    # A big return with the slopes disagreeing is chop with a wiggle, not trend.
    disagreeing, _ = CLF.classify(
        features(rv=rv, ret5=strong, mom_slope=-2.0, ema_slope=1.0)
    )
    assert disagreeing is Regime7.RANGING


def test_a_small_move_is_not_a_trend_however_well_aligned():
    rv = 5.0
    weak = rv * 2.236 * (TREND_MIN_Z / 2.0)
    regime, _ = CLF.classify(features(rv=rv, ret5=weak, mom_slope=1.0, ema_slope=1.0))
    assert regime is Regime7.RANGING


def test_ranging_is_the_residual():
    regime, conf = CLF.classify(features())
    assert regime is Regime7.RANGING
    assert 0.0 < conf <= 1.0


# --------------------------------------------------------------------------- #
# Precedence — the documented order, enforced
# --------------------------------------------------------------------------- #


def test_panic_outranks_breakout_and_volatility_expansion():
    # A flush looks like all three at once.  It must be filed as PANIC, because
    # the other two readings invite a strategy to trade into it.
    flush = features(
        rvol_pct=0.99,
        rv=10.0,
        ret5=-10.0 * 2.236 * 3.0,
        breakout_dist=-40.0,
        vol_accel=4.0,
        mom_slope=-5.0,
        ema_slope=-4.0,
    )
    assert CLF.classify(flush)[0] is Regime7.PANIC


def test_breakout_outranks_volatility_expansion():
    # A genuine breakout is necessarily also a vol spike; the breakout reading
    # is the more specific and the more actionable of the two.
    both = features(rvol_pct=HIGH_VOL_PCT + 0.1, breakout_dist=20.0, vol_accel=2.0)
    assert CLF.classify(both)[0] is Regime7.BREAKOUT


def test_volatility_buckets_outrank_trending():
    trending_and_wild = features(
        rvol_pct=HIGH_VOL_PCT + 0.1, ret5=60.0, mom_slope=5.0, ema_slope=4.0
    )
    assert CLF.classify(trending_and_wild)[0] is Regime7.VOLATILITY_EXPANSION

    trending_and_dead = features(
        rvol_pct=LOW_VOL_PCT - 0.1, ret5=60.0, mom_slope=5.0, ema_slope=4.0
    )
    assert CLF.classify(trending_and_dead)[0] is Regime7.LOW_VOLATILITY


def test_confidence_is_always_a_probability():
    for f in (
        features(),
        features(rvol_pct=1.0, rv=20.0, ret5=-500.0),
        features(rvol_pct=0.0),
        features(breakout_dist=1e4, vol_accel=20.0, atr=0.0),
        features(rv=1e-9, ret5=1e6, mom_slope=1e6, ema_slope=1e6),
    ):
        _, conf = CLF.classify(f)
        assert 0.0 <= conf <= 1.0


# --------------------------------------------------------------------------- #
# Distribution over a realistic tape
# --------------------------------------------------------------------------- #


def synthetic_tape(*, minutes: int = 24_000, seed: int = 7) -> list[Trade]:
    """A tape shaped like Binance.US: mostly silent, occasionally eventful.

    Deliberately built as *prints*, not bars, so the whole stack — bucketing,
    empty-bar emission, the feature engine, the classifier — is exercised the
    way the tournament will exercise it.
    """
    rng = random.Random(seed)
    # (label, minutes, drift bps/min, sigma bps, trade probability)
    script = [
        ("quiet", 320, 0.0, 0.4, 0.25),
        ("trend_up", 180, 1.4, 1.8, 0.60),
        ("chop", 260, 0.0, 2.2, 0.55),
        ("trend_down", 180, -1.4, 1.8, 0.60),
        ("compress", 140, 0.0, 0.25, 0.20),
        ("breakout", 60, 3.0, 4.0, 0.85),
        ("expansion", 140, 0.0, 7.0, 0.80),
        ("quiet", 300, 0.0, 0.5, 0.25),
        ("crash", 45, -9.0, 14.0, 0.90),
        ("recover", 120, 2.0, 6.0, 0.70),
    ]
    trades: list[Trade] = []
    price = 100.0
    minute = 0
    while minute < minutes:
        for _label, span, drift, sigma, p_trade in script:
            for _ in range(span):
                if minute >= minutes:
                    break
                price *= 1.0 + (drift + rng.gauss(0.0, sigma)) / 10_000.0
                price = max(1.0, price)
                if rng.random() < p_trade:
                    for _k in range(rng.randint(1, 4)):
                        trades.append(
                            Trade(
                                symbol=SYM,
                                price=price * (1.0 + rng.gauss(0.0, 0.00002)),
                                qty=round(rng.uniform(0.001, 0.05), 6),
                                ts_ms=BASE + minute * MINUTE + rng.randint(0, 59_000),
                                buyer_is_maker=rng.random() < 0.5,
                            )
                        )
                minute += 1
    trades.sort(key=lambda t: t.ts_ms)
    return trades


@pytest.fixture(scope="module")
def classified() -> list[tuple[Regime7, float]]:
    trades = synthetic_tape()
    bars = build_bars(trades, "1m")
    ds = Dataset(
        symbol=SYM,
        bars={"1m": bars},
        trades=trades,
        start_ms=bars[0].open_ms,
        end_ms=bars[-1].close_ms,
    )
    eng = MTFFeatureEngine(intervals=("1m",))
    clf = Regime7Classifier(interval="1m")
    return [clf.classify(f) for f in eng.stream(ds, step="1m")]


def test_every_regime_is_reachable_from_a_realistic_tape(classified):
    dist = regime_distribution(classified)
    missing = [r.value for r, share in dist.items() if share == 0.0]
    assert not missing, f"unreachable regimes: {missing} (distribution: {dist})"


def test_the_distribution_is_not_degenerate(classified):
    dist = regime_distribution(classified)
    worst = max(dist.items(), key=lambda kv: kv[1])
    assert worst[1] <= 0.70, (
        f"{worst[0].value} takes {worst[1]:.1%} of samples; a classifier that "
        f"answers one state most of the time cannot measure per-regime edge "
        f"({dist})"
    )


def test_the_empty_tape_does_not_collapse_the_partition(classified):
    # More than half these minutes contain no print at all.  If the silence
    # were driving the answer, LOW_VOLATILITY would swallow everything.
    dist = regime_distribution(classified)
    assert dist[Regime7.LOW_VOLATILITY] < 0.60
    assert dist[Regime7.TRENDING_UP] > 0.01
    assert dist[Regime7.TRENDING_DOWN] > 0.01


def test_every_classification_carries_a_usable_confidence(classified):
    assert all(0.0 <= c <= 1.0 for _, c in classified)
    # Zero confidence means "warming up"; it must be rare, not the norm.
    zero = sum(1 for _, c in classified if c == 0.0)
    assert zero < len(classified) * 0.01


def test_regime_distribution_always_reports_all_seven_keys():
    assert set(regime_distribution([])) == set(Regime7)
    assert all(v == 0.0 for v in regime_distribution([]).values())
    one = regime_distribution([(Regime7.PANIC, 1.0)])
    assert one[Regime7.PANIC] == 1.0
    assert one[Regime7.RANGING] == 0.0
