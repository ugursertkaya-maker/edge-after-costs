"""The live predictive entry engine.

Its job is to be the last thing between a model and the book, so the tests are
mostly about refusing: no model, a stale book, a missing feature, an
uncalibrated probability, a positive probability with negative expected value.
Every one of those must be a clean NO TRADE with a countable reason.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.predict.engine import REJECT_REASONS, PredictiveEntryEngine
from app.types import Features, Regime, RegimeState


class FakeArtifact:
    """Minimal stand-in for a PredictiveArtifact — numpy in, probability out."""

    def __init__(self, p=0.7, *, calibrated=True, max_err=0.03, names=("atr_bps",),
                 threshold=0.6, ev_threshold=0.0, target_bps=40.0, stop_bps=40.0,
                 supported=None):
        self.version = "PredictiveEngine-test"
        self.model_name = "logistic"
        self.feature_names = tuple(names)
        self.probability_threshold = threshold
        self.ev_threshold_usd = ev_threshold
        self.calibrated = calibrated
        self.calibration_max_abs_error = max_err
        self.net_target_bps = target_bps
        self.stop_bps = stop_bps
        self.supported_regimes = supported
        self._p = p

    def predict_proba(self, X):
        return np.asarray([[1.0 - self._p, self._p]] * len(X), dtype=float)


def features(**over) -> Features:
    base = dict(
        symbol="BTCUSD", ts_ms=1_700_000_000_000, price=60_000.0, mid=60_000.0,
        microprice=60_000.0, spread_bps=1.0, book_age_ms=100,
        depth_bid_usd=50_000.0, depth_ask_usd=50_000.0, imbalance=0.0,
        atr_bps=2.2, realized_vol_bps=5.0, vol_percentile=0.5,
        ret_1m_bps=1.0, ret_5m_bps=2.0, ret_15m_bps=3.0,
        ema_fast=60_000.0, ema_slow=59_990.0, ema_slope_bps=0.5,
        trend_strength=0.6, range_pos=0.5, bb_width_bps=20.0, compression=0.3,
        volume_ratio=1.2, trade_flow_imbalance=0.1, trade_flow_accel=0.05,
        vwap=60_000.0, vwap_dev_bps=0.0, quote_volume_24h=1.6e6,
        trades_24h=3_000, bars_available=240,
    )
    base.update(over)
    return Features(**base)


def regime(kind=Regime.TRENDING) -> RegimeState:
    return RegimeState(regime=kind, confidence=0.8)


def engine(artifact=None, **kw) -> PredictiveEntryEngine:
    return PredictiveEntryEngine(artifact=artifact, **kw)


def score(eng, **kw):
    params = dict(notional_usd=150.0, round_trip_bps=4.0)
    params.update(kw)
    return eng.score(features(), regime(), **params)


# --------------------------------------------------------------------------- #
# Failing closed
# --------------------------------------------------------------------------- #


def test_no_model_is_a_clean_no_trade():
    eng = engine(None)
    p = score(eng)
    assert p.decision == "NO_TRADE"
    assert p.reject_reason == "NO_MODEL"
    assert eng.stats.rejections["NO_MODEL"] == 1
    assert not eng.ready


def test_a_broken_artifact_path_does_not_raise(tmp_path):
    eng = PredictiveEntryEngine(artifact_path=tmp_path / "missing.json")
    assert not eng.ready
    assert eng.load_error
    assert score(eng).reject_reason == "NO_MODEL"


def test_a_stale_book_is_refused_before_the_model_is_consulted():
    eng = engine(FakeArtifact())
    p = eng.score(features(book_age_ms=999_999), regime(),
                  notional_usd=150.0, round_trip_bps=4.0)
    assert p.reject_reason == "STALE_DATA"
    assert p.p_tp == 0.0, "the model must not have been consulted"


def test_a_wide_spread_is_refused():
    eng = engine(FakeArtifact())
    p = eng.score(features(spread_bps=500.0), regime(),
                  notional_usd=150.0, round_trip_bps=4.0)
    assert p.reject_reason == "SPREAD_TOO_WIDE"


def test_thin_depth_is_refused():
    eng = engine(FakeArtifact())
    p = eng.score(features(depth_bid_usd=1.0, depth_ask_usd=1.0), regime(),
                  notional_usd=150.0, round_trip_bps=4.0)
    assert p.reject_reason == "NO_LIQUIDITY"


def test_abnormal_regime_is_refused():
    eng = engine(FakeArtifact())
    p = eng.score(features(), regime(Regime.ABNORMAL),
                  notional_usd=150.0, round_trip_bps=4.0)
    assert p.reject_reason == "REGIME_NOT_SUPPORTED"


def test_a_regime_the_model_was_not_trained_on_is_refused():
    eng = engine(FakeArtifact(supported=("RANGING",)))
    assert score(eng).reject_reason == "REGIME_NOT_SUPPORTED"


def test_a_missing_feature_fails_rather_than_zero_filling():
    """A model scored on a silently zeroed column is predicting about a market
    it was never shown."""
    eng = engine(FakeArtifact(names=("a_feature_that_does_not_exist",)))
    p = score(eng)
    assert p.reject_reason == "FEATURE_MISMATCH"


def test_a_nan_feature_fails_rather_than_propagating():
    eng = engine(FakeArtifact(names=("atr_bps",)))
    p = eng.score(features(atr_bps=float("nan")), regime(),
                  notional_usd=150.0, round_trip_bps=4.0)
    assert p.reject_reason == "FEATURE_MISMATCH"


# --------------------------------------------------------------------------- #
# Calibration gates the whole thing
# --------------------------------------------------------------------------- #


def test_an_uncalibrated_model_cannot_authorise_a_trade():
    """An uncalibrated probability is a number, not a probability."""
    eng = engine(FakeArtifact(p=0.95, calibrated=False))
    p = score(eng)
    assert p.decision == "NO_TRADE"
    assert p.reject_reason == "MODEL_UNCERTAIN"
    assert p.confidence_known is False
    assert p.as_dict()["confidence"] is None, "no fake confidence on the dashboard"


def test_a_badly_calibrated_model_cannot_authorise_a_trade():
    eng = engine(FakeArtifact(p=0.95, calibrated=True, max_err=0.4))
    p = score(eng)
    assert p.reject_reason == "MODEL_UNCERTAIN"
    assert p.confidence_known is False


def test_a_well_calibrated_model_reports_confidence():
    eng = engine(FakeArtifact(p=0.8, max_err=0.02))
    p = score(eng)
    assert p.confidence_known is True
    assert p.as_dict()["confidence"] == pytest.approx(0.98)


# --------------------------------------------------------------------------- #
# Expected value, not probability
# --------------------------------------------------------------------------- #


def test_a_high_probability_with_a_bad_payoff_is_refused():
    """70% of winning $0.10 against 30% of losing $0.40 is a losing trade."""
    eng = engine(FakeArtifact(p=0.7, threshold=0.6, target_bps=10.0, stop_bps=120.0))
    p = score(eng)
    assert p.p_tp == pytest.approx(0.7)
    assert p.expected_value < 0
    assert p.decision == "NO_TRADE"
    assert p.reject_reason == "NEGATIVE_EV"


def test_a_good_payoff_at_a_sufficient_probability_is_a_buy():
    eng = engine(FakeArtifact(p=0.75, threshold=0.6, target_bps=80.0, stop_bps=40.0))
    p = score(eng)
    assert p.decision == "BUY"
    assert p.expected_value > 0
    assert p.reject_reason is None
    assert eng.stats.buys == 1


def test_expected_value_arithmetic_is_exact():
    eng = engine(FakeArtifact(p=0.6, threshold=0.5, target_bps=100.0, stop_bps=50.0))
    p = eng.score(features(), regime(), notional_usd=1_000.0, round_trip_bps=10.0)
    # win $10, loss $5, cost $1  ->  0.6*10 - 0.4*5 - 1 = 3.0
    assert p.expected_net_win == pytest.approx(10.0)
    assert p.expected_net_loss == pytest.approx(5.0)
    assert p.execution_cost == pytest.approx(1.0)
    assert p.expected_value == pytest.approx(3.0)


def test_costs_larger_than_the_gross_are_called_out_as_such():
    eng = engine(FakeArtifact(p=0.65, threshold=0.5, target_bps=2.0, stop_bps=2.0))
    p = eng.score(features(), regime(), notional_usd=100.0, round_trip_bps=60.0)
    assert p.reject_reason == "COST_TOO_HIGH"


def test_below_threshold_but_close_is_a_watch_not_a_silent_skip():
    eng = engine(FakeArtifact(p=0.58, threshold=0.6, target_bps=80.0, stop_bps=40.0))
    p = score(eng)
    assert p.decision == "WATCH"
    assert eng.stats.watches == 1


def test_far_below_threshold_is_a_flat_no_trade():
    eng = engine(FakeArtifact(p=0.2, threshold=0.6, target_bps=80.0, stop_bps=40.0))
    p = score(eng)
    assert p.decision == "NO_TRADE"
    assert p.reject_reason == "LOW_PROBABILITY"


def test_an_ev_threshold_above_the_edge_refuses():
    eng = engine(FakeArtifact(p=0.75, threshold=0.6, target_bps=80.0,
                              stop_bps=40.0, ev_threshold=1_000.0))
    assert score(eng).decision == "NO_TRADE"


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def test_every_rejection_reason_is_in_the_declared_set():
    eng = engine(FakeArtifact(p=0.2, threshold=0.9))
    for _ in range(3):
        score(eng)
    assert set(eng.stats.rejections) <= set(REJECT_REASONS)


def test_rejections_are_counted_so_refusal_is_distinguishable_from_no_edge():
    eng = engine(FakeArtifact(p=0.2, threshold=0.9, target_bps=80.0, stop_bps=40.0))
    for _ in range(5):
        score(eng)
    assert eng.stats.rejections["LOW_PROBABILITY"] == 5
    assert eng.stats.scored == 5


def test_the_explanation_is_stored_values_not_prose():
    eng = engine(FakeArtifact(p=0.75, threshold=0.6, target_bps=80.0, stop_bps=40.0))
    p = score(eng)
    ex = p.explanation()
    for key in ("p_win", "expected_net_win", "expected_net_loss",
                "expected_value", "regime", "model_version"):
        assert key in ex
    assert all(not isinstance(v, str) or key in ("regime", "model_version", "symbol",
                                                 "decision")
               for key, v in ex.items())


def test_payload_without_a_model_says_so_rather_than_faking_a_version():
    eng = engine(None)
    p = score(eng)
    payload = eng.payload([p])
    assert payload["ready"] is False
    assert payload["model_version"] is None
    assert payload["rejections"]["NO_MODEL"] == 1
    assert payload["explanations"] == []


def test_payload_only_explains_trades_it_actually_took():
    eng = engine(FakeArtifact(p=0.75, threshold=0.6, target_bps=80.0, stop_bps=40.0))
    taken = score(eng)
    eng2 = engine(FakeArtifact(p=0.1, threshold=0.6))
    skipped = score(eng2)
    payload = eng.payload([taken, skipped])
    assert len(payload["explanations"]) == 1
    assert payload["explanations"][0]["decision"] == "BUY"
