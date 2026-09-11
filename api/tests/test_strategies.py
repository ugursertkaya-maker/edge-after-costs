"""Strategies: each one fires on the setup it was written for, stays silent in
the wrong regime and on featureless data, and never emits a non-positive stop.
"""

from __future__ import annotations

import random

import pytest

from app.strategies import STRATEGY_NAMES, get_strategies
from app.strategies.base import MIN_STOP_BPS, MIN_TARGET_BPS, Strategy
from app.types import Features, Regime, RegimeState, Side

from fixtures.market import (
    flow_trades,
    make_bars,
    make_book,
    make_features,
    make_regime,
    make_snapshot,
    zigzag_closes,
)

ATR = 20.0


# --------------------------------------------------------------------------- #
# The setups.  Each returns (features, regime) that the named strategy should
# take, plus the side it should take it on.
# --------------------------------------------------------------------------- #


def momentum_setup(direction: int = 1) -> tuple[Features, RegimeState]:
    f = make_features(
        atr_bps=ATR,
        ema_fast=100.0 + direction * 1.0,
        ema_slow=100.0,
        ema_slope_bps=direction * 3.0,
        ret_1m_bps=direction * 10.0,
        ret_5m_bps=direction * 30.0,
        volume_ratio=1.8,
        trade_flow_imbalance=direction * 0.4,
        trade_flow_accel=direction * 0.1,
        trend_strength=0.7,
        spread_bps=2.0,
    )
    return f, make_regime(Regime.TRENDING, 0.7)


def breakout_setup(direction: int = 1) -> tuple[Features, RegimeState]:
    f = make_features(
        atr_bps=ATR,
        compression=0.4,
        volume_ratio=2.5,
        range_pos=0.95 if direction > 0 else 0.05,
        trade_flow_imbalance=direction * 0.3,
        ret_1m_bps=direction * 15.0,
        spread_bps=2.0,
        extras={"compression_prev": 0.85},
    )
    return f, make_regime(Regime.BREAKOUT, 0.7)


def pullback_setup(direction: int = 1) -> tuple[Features, RegimeState]:
    f = make_features(
        atr_bps=ATR,
        price=100.0 + direction * 0.1,
        mid=100.0 + direction * 0.1,
        ema_fast=100.0 + direction * 0.5,
        ema_slow=100.0,
        ema_slope_bps=direction * 1.0,
        trend_strength=0.7,
        ret_1m_bps=direction * 4.0,
        ret_5m_bps=-direction * 15.0,
        range_pos=0.5,
        vwap=100.15,
        vwap_dev_bps=-direction * 5.0,
        spread_bps=2.0,
    )
    return f, make_regime(Regime.TRENDING, 0.7)


def meanrev_setup(direction: int = 1) -> tuple[Features, RegimeState]:
    f = make_features(
        atr_bps=ATR,
        bb_width_bps=40.0,
        vwap_dev_bps=-direction * 40.0,
        range_pos=0.10 if direction > 0 else 0.90,
        trade_flow_imbalance=-direction * 0.3,
        trade_flow_accel=direction * 0.25,
        mid=100.0,
        microprice=100.0 + direction * 0.005,
        trend_strength=0.1,
        spread_bps=2.0,
    )
    return f, make_regime(Regime.RANGING, 0.6)


def flow_setup(direction: int = 1) -> tuple[Features, RegimeState]:
    f = make_features(
        atr_bps=ATR,
        trade_flow_imbalance=direction * 0.7,
        trade_flow_accel=direction * 0.35,
        ret_1m_bps=direction * 20.0,
        volume_ratio=2.5,
        spread_bps=2.0,
    )
    return f, make_regime(Regime.HIGH_VOLATILITY, 0.7)


def imbalance_setup(direction: int = 1) -> tuple[Features, RegimeState]:
    f = make_features(
        atr_bps=ATR,
        imbalance=direction * 0.6,
        mid=100.0,
        microprice=100.0 + direction * 0.008,
        depth_bid_usd=30_000.0 if direction > 0 else 10_000.0,
        depth_ask_usd=10_000.0 if direction > 0 else 30_000.0,
        trade_flow_imbalance=direction * 0.1,
        spread_bps=2.0,
    )
    return f, make_regime(Regime.RANGING, 0.6)


SETUPS = {
    "momentum_scalp": momentum_setup,
    "breakout_scalp": breakout_setup,
    "trend_pullback": pullback_setup,
    "mean_reversion": meanrev_setup,
    "flow_impulse": flow_setup,
    "book_imbalance": imbalance_setup,
}

# A regime every strategy refuses (none of them allow it).
WRONG_REGIME = {
    "momentum_scalp": Regime.RANGING,
    "breakout_scalp": Regime.HIGH_VOLATILITY,
    "trend_pullback": Regime.RANGING,
    "mean_reversion": Regime.TRENDING,
    "flow_impulse": Regime.RANGING,
    "book_imbalance": Regime.BREAKOUT,
}


def strategy(name: str) -> Strategy:
    return get_strategies([name])[0]


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_registry_exposes_exactly_six_strategies_in_order():
    assert STRATEGY_NAMES == (
        "momentum_scalp",
        "breakout_scalp",
        "trend_pullback",
        "mean_reversion",
        "flow_impulse",
        "book_imbalance",
    )
    assert [s.name for s in get_strategies()] == list(STRATEGY_NAMES)


def test_get_strategies_subset_keeps_registry_order():
    got = get_strategies(["book_imbalance", "momentum_scalp"])
    assert [s.name for s in got] == ["momentum_scalp", "book_imbalance"]


def test_get_strategies_rejects_an_unknown_name():
    with pytest.raises(KeyError):
        get_strategies(["nope"])


def test_no_strategy_opts_into_abnormal():
    for s in get_strategies():
        assert Regime.ABNORMAL not in s.allowed_regimes
        assert s.allowed_regimes


# --------------------------------------------------------------------------- #
# Firing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", STRATEGY_NAMES)
def test_strategy_fires_long_on_its_own_setup(name):
    f, regime = SETUPS[name](1)
    sig = strategy(name).evaluate(f, regime)
    assert sig is not None, f"{name} did not fire on its own setup"
    assert sig.strategy == name
    assert sig.symbol == f.symbol
    assert sig.side is Side.LONG
    assert 0.0 <= sig.score <= 1.0
    assert sig.reasons
    assert sig.target_bps >= MIN_TARGET_BPS
    assert sig.stop_bps >= MIN_STOP_BPS
    assert sig.horizon_seconds > 0


@pytest.mark.parametrize("name", STRATEGY_NAMES)
def test_strategy_is_symmetric_and_may_emit_short(name):
    """V1 is spot-long-only, but the risk layer — not the strategy — is what
    rejects a short.  The logic here stays symmetric on purpose."""
    f, regime = SETUPS[name](-1)
    sig = strategy(name).evaluate(f, regime)
    assert sig is not None, f"{name} is not symmetric"
    assert sig.side is Side.SHORT
    assert sig.stop_bps > 0


# bps-denominated features that describe *the same setup* on a pair that moves
# twice as much; scaling ATR alone would describe a different, weaker setup.
_ATR_RELATIVE_FIELDS = (
    "atr_bps",
    "ret_1m_bps",
    "ret_5m_bps",
    "ret_15m_bps",
    "ema_slope_bps",
    "vwap_dev_bps",
    "bb_width_bps",
)


def scaled_setup(name: str, direction: int, mult: float):
    f, regime = SETUPS[name](direction)
    for field_name in _ATR_RELATIVE_FIELDS:
        setattr(f, field_name, getattr(f, field_name) * mult)
    return f, regime


@pytest.mark.parametrize("name", STRATEGY_NAMES)
def test_targets_and_stops_scale_with_atr(name):
    """Not constants: the same setup on a twice-as-volatile pair asks for
    roughly twice the target and twice the stop."""
    f, regime = scaled_setup(name, 1, 1.0)
    small = strategy(name).evaluate(f, regime)

    f2, regime2 = scaled_setup(name, 1, 2.0)
    big = strategy(name).evaluate(f2, regime2)

    assert small is not None and big is not None
    assert big.stop_bps == pytest.approx(small.stop_bps * 2.0, rel=1e-9)
    assert big.target_bps == pytest.approx(small.target_bps * 2.0, rel=1e-9)


# --------------------------------------------------------------------------- #
# Silence
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", STRATEGY_NAMES)
def test_strategy_is_silent_in_the_wrong_regime(name):
    f, _ = SETUPS[name](1)
    wrong = make_regime(WRONG_REGIME[name], 0.9)
    assert wrong.regime not in strategy(name).allowed_regimes
    assert strategy(name).evaluate(f, wrong) is None


@pytest.mark.parametrize("name", STRATEGY_NAMES)
def test_strategy_is_silent_in_an_abnormal_regime(name):
    f, _ = SETUPS[name](1)
    assert strategy(name).evaluate(f, make_regime(Regime.ABNORMAL, 1.0)) is None


@pytest.mark.parametrize("name", STRATEGY_NAMES)
def test_abnormal_is_refused_even_if_a_strategy_declares_it(name):
    s = strategy(name)
    s.allowed_regimes = frozenset(s.allowed_regimes | {Regime.ABNORMAL})
    f, _ = SETUPS[name](1)
    assert s.evaluate(f, make_regime(Regime.ABNORMAL, 1.0)) is None


@pytest.mark.parametrize("name", STRATEGY_NAMES)
@pytest.mark.parametrize("regime", list(Regime))
def test_strategy_is_silent_on_flat_data(name, regime):
    """Neutral features: no trend, no flow, no imbalance, nothing to trade."""
    f = make_features()
    assert strategy(name).evaluate(f, make_regime(regime, 0.8)) is None


@pytest.mark.parametrize("name", STRATEGY_NAMES)
def test_strategy_is_silent_during_cold_start(name):
    f, regime = SETUPS[name](1)
    f.bars_available = 10
    assert strategy(name).evaluate(f, regime) is None


@pytest.mark.parametrize("name", STRATEGY_NAMES)
def test_strategy_is_silent_without_an_atr(name):
    f, regime = SETUPS[name](1)
    f.atr_bps = 0.0
    assert strategy(name).evaluate(f, regime) is None


@pytest.mark.parametrize("name", STRATEGY_NAMES)
def test_strategy_refuses_when_the_spread_eats_the_target(name):
    f, regime = SETUPS[name](1)
    f.spread_bps = 500.0
    assert strategy(name).evaluate(f, regime) is None


@pytest.mark.parametrize("name", STRATEGY_NAMES)
def test_stop_is_always_positive_across_a_sweep(name):
    """Fuzz the setup deterministically; any signal produced must be sane."""
    s = strategy(name)
    produced = 0
    for atr in (0.5, 2.0, 8.0, 20.0, 75.0, 300.0):
        for flip in (1, -1):
            for scale_ in (0.25, 0.5, 1.0, 2.0):
                f, regime = SETUPS[name](flip)
                f.atr_bps = atr
                f.ret_1m_bps *= scale_
                f.ret_5m_bps *= scale_
                f.trade_flow_imbalance = max(
                    -1.0, min(1.0, f.trade_flow_imbalance * scale_)
                )
                f.volume_ratio *= scale_
                sig = s.evaluate(f, regime)
                if sig is None:
                    continue
                produced += 1
                assert sig.stop_bps > 0.0
                assert sig.target_bps > 0.0
                assert 0.0 <= sig.score <= 1.0
                assert sig.reward_risk > 0.0
    assert produced > 0, f"{name} never fired during the sweep"


# --------------------------------------------------------------------------- #
# Isolation from the rest of the bot
# --------------------------------------------------------------------------- #


def test_strategy_modules_do_not_reference_fees_capital_or_risk():
    """A strategy that peeked at costs would double-count the ranker."""
    import inspect

    import app.strategies as pkg

    banned = ("fee", "capital", "equity", "position", "settings.risk", "notional")
    for s in get_strategies():
        src = inspect.getsource(type(s))
        lowered = src.lower()
        for word in banned:
            assert word not in lowered, f"{s.name} references {word!r}"
    assert pkg.STRATEGY_NAMES


# --------------------------------------------------------------------------- #
# End-to-end: signals from the *real* FeatureEngine, not hand-built Features.
#
# Hand-built features prove the entry logic; these prove the features a live
# feed would actually produce can reach that logic at all.
# --------------------------------------------------------------------------- #


def _run(bars, *, book_for, trades_for):
    """Feed a bar series through FeatureEngine -> RegimeClassifier -> strategies.

    Returns (regimes seen, strategies that fired at least once)."""
    from app.features import FeatureEngine
    from app.regime import RegimeClassifier

    eng = FeatureEngine()
    clf = RegimeClassifier()
    strategies = get_strategies()
    regimes: set[Regime] = set()
    fired: set[str] = set()
    last = None
    for i, bar in enumerate(bars):
        ts = bar.close_ms + 1
        f = eng.compute(
            make_snapshot(
                bars=bars[: i + 1],
                book=book_for(i, bar, ts),
                trades=trades_for(i, bar, ts),
                ts_ms=ts,
            )
        )
        state = clf.classify(f)
        regimes.add(state.regime)
        for s in strategies:
            sig = s.evaluate(f, state)
            if sig is not None:
                fired.add(s.name)
                assert sig.stop_bps > 0.0
                assert sig.target_bps > 0.0
                assert 0.0 <= sig.score <= 1.0
        last = (f, state)
    return regimes, fired, last


def _squeeze_then_break():
    wide = zigzag_closes(140, amp_bps=40.0)
    tight = [wide[-1] * (1.0 + (0.00005 if i % 2 else -0.00005)) for i in range(40)]
    closes = wide + tight + [tight[-1] * 1.004, tight[-1] * 1.008]
    volumes = [100_000.0] * (len(closes) - 2) + [400_000.0, 400_000.0]
    return make_bars(closes, quote_volumes=volumes)


def test_a_real_squeeze_and_break_reaches_the_breakout_strategies():
    bars = _squeeze_then_break()
    regimes, fired, (f, state) = _run(
        bars,
        book_for=lambda i, bar, ts: make_book(mid=bar.close, spread_bps=2.0,
                                              ts_ms=ts - 100),
        trades_for=lambda i, bar, ts: flow_trades(ts_ms=ts, imb_60=0.5, imb_15=0.7,
                                                  price=bar.close),
    )
    assert state.regime is Regime.BREAKOUT
    assert Regime.ABNORMAL not in regimes
    assert {"breakout_scalp", "flow_impulse"} <= fired


def test_a_real_lopsided_book_in_a_range_reaches_book_imbalance():
    bars = make_bars(zigzag_closes(200, amp_bps=12.0))
    regimes, fired, (f, state) = _run(
        bars,
        book_for=lambda i, bar, ts: make_book(mid=bar.close, spread_bps=1.5,
                                              bid_qty=200.0, ask_qty=20.0,
                                              ts_ms=ts - 100),
        trades_for=lambda i, bar, ts: flow_trades(ts_ms=ts, imb_60=0.2,
                                                  price=bar.close),
    )
    assert state.regime is Regime.RANGING
    assert f.imbalance > 0.5
    assert "book_imbalance" in fired


def test_a_real_trend_reaches_the_trend_strategies():
    rng = random.Random(11)
    closes = [100.0]
    for _ in range(300):
        closes.append(closes[-1] * (1.0 + 0.0006 + rng.gauss(0.0, 0.0012)))
    bars = make_bars(closes, quote_volumes=[100_000.0 * (1 + rng.random() * 2)
                                            for _ in closes])
    regimes, fired, _ = _run(
        bars,
        book_for=lambda i, bar, ts: make_book(mid=bar.close, spread_bps=2.0,
                                              ts_ms=ts - 100),
        trades_for=lambda i, bar, ts: flow_trades(
            ts_ms=ts, imb_60=0.45, imb_15=0.75, price=bar.close
        ),
    )
    assert Regime.TRENDING in regimes
    assert {"momentum_scalp", "trend_pullback"} & fired


def test_a_real_quiet_range_reaches_mean_reversion():
    rng = random.Random(5)
    closes = [100.0]
    for i in range(400):
        pull = (100.0 - closes[-1]) * 0.05
        closes.append(closes[-1] + pull + rng.gauss(0.0, 0.09))
    bars = make_bars(closes, quote_volumes=[100_000.0] * len(closes))

    def trades_for(i, bar, ts):
        # aggressive flow pushing price away from value, already decelerating
        away = -1.0 if bar.close < 100.0 else 1.0
        return flow_trades(ts_ms=ts, imb_60=0.4 * away, imb_15=-0.6 * away,
                           price=bar.close)

    regimes, fired, _ = _run(
        bars,
        book_for=lambda i, bar, ts: make_book(
            mid=bar.close,
            spread_bps=1.5,
            bid_qty=60.0 if bar.close < 100.0 else 40.0,
            ask_qty=40.0 if bar.close < 100.0 else 60.0,
            ts_ms=ts - 100,
        ),
        trades_for=trades_for,
    )
    assert "mean_reversion" in fired
