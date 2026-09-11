"""Calibration tests — the probability has to mean something.

The headline claim this file defends is the one the EV gate is built on:

> A model predicting 70% across 100 trades should win about 70 of them.

Two tests carry most of the weight.  :func:`test_fitting_and_scoring_the_same_rows_is_refused`
makes the flattering mistake impossible rather than merely discouraged, and
:func:`test_a_model_trained_on_pure_noise_calibrates_to_the_base_rate` shows the
whole pipeline — train, split, calibrate, score — landing on the base rate with
an AUC of a coin flip when there is nothing to find.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.predict.calibrate import (
    CALIBRATION_TOLERANCE,
    CalibrationLeak,
    CalibrationReport,
    IdentityCalibrator,
    IsotonicCalibrator,
    PlattCalibrator,
    average_precision,
    brier_score,
    calibrate_and_evaluate,
    calibrator_from_payload,
    check_not_fit_rows,
    evaluate_calibration,
    expit,
    fit_calibrator,
    format_reliability,
    log_loss,
    precision_recall,
    reliability_failures,
    roc_auc,
)


def _binary(n=2000, seed=0, sharpness=1.0):
    """Probabilities and outcomes drawn *from* those probabilities."""
    rng = np.random.default_rng(seed)
    p = np.clip(expit(rng.normal(scale=sharpness, size=n)), 0.001, 0.999)
    y = (rng.random(n) < p).astype(int)
    return p, y


# --------------------------------------------------------------------------- #
# THE NOISE TEST — the full pipeline, end to end
# --------------------------------------------------------------------------- #


def test_a_model_trained_on_pure_noise_calibrates_to_the_base_rate():
    """Random labels: AUC ≈ 0.5 and a reliability curve sitting on the base rate.

    This is the pipeline-integrity test on the calibration side.  A balanced
    classifier's *raw* score on a 10% base rate averages ~0.50 — that is the
    class weighting doing its job, not a bug — and the calibrator fitted on
    validation must map it back onto ~0.10 on rows it has never seen.  If this
    ever reports a well-separated, well-calibrated model on random labels, the
    split leaks and nothing else in Phase 4 can be believed.
    """
    pytest.importorskip("sklearn", reason="research dependency")
    from test_predict_models import make_split, synthetic_matrix

    from app.predict.models import split_matrix, train_model

    mx = synthetic_matrix(n=9000, seed=4, edge=0.0, base_rate=0.10)
    split = make_split(mx, train=0.55, validate=0.78)
    train, validate, test = split_matrix(mx, split)

    model = train_model("logistic", train.X, train.y, feature_names=train.feature_names)
    p_val = model.predict_p_win(validate.X)
    p_test = model.predict_p_win(test.X)

    raw = evaluate_calibration(p_test, test.y, bins=5, min_bin_count=30)
    assert raw.mean_predicted > 3 * raw.base_rate, (
        "precondition: the balanced raw score should be wildly above the base rate"
    )

    for method in ("isotonic", "platt"):
        cal = fit_calibrator(p_val, validate.y, method=method, keys=validate.ts_ms)
        report = calibrate_and_evaluate(
            cal, p_test, test.y, bins=5, min_bin_count=30, keys=test.ts_ms
        )

        assert 0.42 < report.auc < 0.58, (
            f"{method}: AUC {report.auc:.3f} on labels independent of every "
            "feature — the pipeline is reporting an edge that does not exist"
        )
        assert abs(report.mean_predicted - report.base_rate) < 0.03, (
            f"{method}: mean prediction {report.mean_predicted:.3f} vs base rate "
            f"{report.base_rate:.3f}"
        )
        assert report.max_abs_error <= CALIBRATION_TOLERANCE, format_reliability(report)
        assert report.calibrated is True


# --------------------------------------------------------------------------- #
# THE LEAK GUARD — fitted on validation, never on test
# --------------------------------------------------------------------------- #


def test_fitting_and_scoring_the_same_rows_is_refused():
    """The required test: a calibrator evaluated on its own fit rows must fail."""
    p, y = _binary(500, seed=1)
    cal = fit_calibrator(p, y, method="isotonic")
    with pytest.raises(CalibrationLeak, match="fitted on"):
        calibrate_and_evaluate(cal, p, y)


def test_the_leak_guard_covers_platt_too():
    p, y = _binary(500, seed=2)
    cal = fit_calibrator(p, y, method="platt")
    with pytest.raises(CalibrationLeak):
        calibrate_and_evaluate(cal, p, y)


def test_a_partial_overlap_is_caught_not_just_an_identical_set():
    """A validation window bleeding into test is the realistic version of the leak."""
    p, y = _binary(800, seed=3)
    cal = fit_calibrator(p[:400], y[:400], method="isotonic")
    calibrate_and_evaluate(cal, p[400:], y[400:])  # disjoint: fine
    with pytest.raises(CalibrationLeak, match="fitted on"):
        calibrate_and_evaluate(cal, p[350:700], y[350:700])  # 50 shared rows


def test_row_keys_make_the_leak_check_exact():
    """With ``keys`` a single shared row is enough, and coincidence is not."""
    p, y = _binary(600, seed=30)
    ts = np.arange(600) * 60_000
    cal = fit_calibrator(p[:300], y[:300], method="isotonic", keys=ts[:300])
    calibrate_and_evaluate(cal, p[300:], y[300:], keys=ts[300:])
    with pytest.raises(CalibrationLeak, match="matched by row id"):
        calibrate_and_evaluate(cal, p[299:400], y[299:400], keys=ts[299:400])


def test_row_keys_must_be_unique_per_row():
    p, y = _binary(10, seed=31)
    with pytest.raises(ValueError, match="unique per row"):
        fit_calibrator(p, y, method="platt", keys=np.zeros(10, dtype=int))


def test_repeated_probabilities_alone_do_not_trigger_a_false_leak():
    """A tree ensemble hands hundreds of rows the same leaf probability."""
    rng = np.random.default_rng(32)
    grid = np.array([0.1, 0.3, 0.5, 0.7, 0.9])
    p_fit = rng.choice(grid, 500)
    y_fit = (rng.random(500) < p_fit).astype(int)
    p_eval = rng.choice(grid, 500)
    y_eval = (rng.random(500) < p_eval).astype(int)
    cal = fit_calibrator(p_fit, y_fit, method="isotonic", keys=np.arange(500))
    # Exact keys: disjoint rows, no complaint, even though every value repeats.
    calibrate_and_evaluate(cal, p_eval, y_eval, keys=np.arange(500, 1000))


def test_check_not_fit_rows_is_silent_on_genuinely_held_out_rows():
    p, y = _binary(600, seed=4)
    cal = fit_calibrator(p[:300], y[:300], method="platt")
    check_not_fit_rows(cal, p[300:], y[300:])


def test_strict_false_is_the_only_way_past_the_guard():
    p, y = _binary(300, seed=5)
    cal = fit_calibrator(p, y, method="isotonic")
    report = calibrate_and_evaluate(cal, p, y, strict=False)
    assert report.n == 300  # allowed, but only when asked for explicitly


def test_a_calibrator_that_was_never_fitted_has_nothing_to_leak():
    p, y = _binary(200, seed=6)
    calibrate_and_evaluate(IdentityCalibrator(), p, y)


# --------------------------------------------------------------------------- #
# "70% should win 70 of 100" — and reporting where it does not
# --------------------------------------------------------------------------- #


def test_a_seventy_percent_claim_that_wins_seventy_is_calibrated():
    y = np.zeros(1000, dtype=int)
    y[:700] = 1
    p = np.full(1000, 0.70)
    report = evaluate_calibration(p, y)
    assert report.max_abs_error < 0.01
    assert report.calibrated is True
    assert reliability_failures(report) == []


def test_a_seventy_percent_claim_that_wins_fifty_two_is_reported_as_overconfident():
    y = np.zeros(100, dtype=int)
    y[:52] = 1
    p = np.full(100, 0.70)
    report = evaluate_calibration(p, y)

    assert report.calibrated is False
    failures = reliability_failures(report)
    assert len(failures) == 1
    pred, actual, n, err = failures[0]
    assert pred == pytest.approx(0.70)
    assert actual == pytest.approx(0.52)
    assert n == 100
    assert err == pytest.approx(0.18)

    text = format_reliability(report)
    assert "it said 70% across 100 trades and won 52% of them" in text
    assert "promised more than it delivered" in text
    assert "NOT CALIBRATED" in text


def test_an_underconfident_model_is_reported_in_the_other_direction():
    y = np.zeros(200, dtype=int)
    y[:120] = 1
    report = evaluate_calibration(np.full(200, 0.35), y)
    assert reliability_failures(report)[0][3] < 0
    assert "under-sold itself" in format_reliability(report)


def test_the_gate_tolerance_is_the_number_in_the_spec():
    assert CALIBRATION_TOLERANCE == 0.10
    y = np.zeros(1000, dtype=int)
    y[:620] = 1
    assert evaluate_calibration(np.full(1000, 0.70), y).calibrated is True   # 0.08 off
    y2 = np.zeros(1000, dtype=int)
    y2[:580] = 1
    assert evaluate_calibration(np.full(1000, 0.70), y2).calibrated is False  # 0.12 off


def test_small_bins_are_not_silently_dropped_from_the_worst_case():
    """A three-row bin that is 40 points off is still 40 points off."""
    p = np.concatenate([np.full(500, 0.10), np.full(3, 0.95)])
    y = np.concatenate([(np.arange(500) < 50).astype(int), np.zeros(3, dtype=int)])
    strict = evaluate_calibration(p, y)
    assert strict.max_abs_error > 0.9
    assert strict.calibrated is False
    # ...and the stable-bins-only view is available, but has to be asked for.
    lenient = evaluate_calibration(p, y, min_bin_count=20)
    assert lenient.max_abs_error < 0.05
    assert lenient.calibrated is True


def test_expected_calibration_error_is_count_weighted():
    p = np.concatenate([np.full(990, 0.10), np.full(10, 0.95)])
    y = np.concatenate([(np.arange(990) < 99).astype(int), np.zeros(10, dtype=int)])
    report = evaluate_calibration(p, y)
    assert report.expected_calibration_error < report.max_abs_error


# --------------------------------------------------------------------------- #
# The report contract
# --------------------------------------------------------------------------- #


def test_report_carries_every_field_the_contract_names():
    p, y = _binary(400, seed=7)
    r = evaluate_calibration(p, y)
    assert isinstance(r, CalibrationReport)
    for field in ("brier", "log_loss", "auc", "pr_auc", "bins", "max_abs_error",
                  "calibrated", "method"):
        assert hasattr(r, field)
    assert all(len(b) == 3 for b in r.bins)
    assert sum(b[2] for b in r.bins) == 400


def test_bins_are_ordered_and_non_empty():
    p, y = _binary(1000, seed=8)
    r = evaluate_calibration(p, y, bins=10)
    assert all(b[2] > 0 for b in r.bins)
    assert [b[0] for b in r.bins] == sorted(b[0] for b in r.bins)


def test_worst_bin_is_the_one_furthest_from_the_diagonal():
    p = np.concatenate([np.full(100, 0.15), np.full(100, 0.85)])
    y = np.concatenate([np.zeros(100, dtype=int), np.zeros(100, dtype=int)])
    r = evaluate_calibration(p, y)
    assert r.worst_bin[0] == pytest.approx(0.85)


def test_evaluate_calibration_rejects_bad_input():
    with pytest.raises(ValueError, match="same length"):
        evaluate_calibration([0.1, 0.2], [1])
    with pytest.raises(ValueError, match="binary"):
        evaluate_calibration([0.1, 0.2], [0, 2])
    with pytest.raises(ValueError, match="empty"):
        evaluate_calibration([], [])
    with pytest.raises(ValueError, match="NaN"):
        evaluate_calibration([0.1, np.nan], [0, 1])


def test_report_as_dict_is_json_shaped():
    import json

    p, y = _binary(200, seed=9)
    json.dumps(evaluate_calibration(p, y).as_dict())


# --------------------------------------------------------------------------- #
# Metrics, checked against sklearn
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_the_numpy_metrics_match_sklearn_exactly(seed):
    """numpy-only costs no accuracy — including the tie handling."""
    pytest.importorskip("sklearn", reason="research dependency")
    from sklearn.metrics import (
        average_precision_score,
        brier_score_loss,
        roc_auc_score,
    )
    from sklearn.metrics import log_loss as sk_log_loss

    rng = np.random.default_rng(seed)
    n = 600
    p = np.round(rng.random(n), 2)  # deliberate ties
    y = (rng.random(n) < p).astype(int)

    assert roc_auc(y, p) == pytest.approx(roc_auc_score(y, p), abs=1e-12)
    assert average_precision(y, p) == pytest.approx(average_precision_score(y, p), abs=1e-12)
    assert brier_score(y, p) == pytest.approx(brier_score_loss(y, p), abs=1e-12)
    assert log_loss(y, p) == pytest.approx(sk_log_loss(y, p), abs=1e-12)


def test_auc_of_a_single_class_slice_is_undefined_not_a_coin_flip():
    assert np.isnan(roc_auc(np.zeros(10, dtype=int), np.linspace(0, 1, 10)))
    assert np.isnan(average_precision(np.zeros(10, dtype=int), np.linspace(0, 1, 10)))


def test_a_perfect_ranker_scores_one():
    y = np.array([0] * 50 + [1] * 50)
    p = np.linspace(0.0, 1.0, 100)
    assert roc_auc(y, p) == pytest.approx(1.0)
    assert average_precision(y, p) == pytest.approx(1.0)


def test_precision_is_undefined_when_nothing_fires():
    y = np.array([0, 0, 1, 1])
    precision, recall, n = precision_recall(y, np.array([0.1, 0.1, 0.2, 0.2]), threshold=0.9)
    assert np.isnan(precision)
    assert recall == 0.0
    assert n == 0


def test_expit_is_stable_at_the_extremes():
    z = np.array([-800.0, -1.0, 0.0, 1.0, 800.0])
    out = expit(z)
    assert np.all(np.isfinite(out))
    assert out[0] == pytest.approx(0.0)
    assert out[-1] == pytest.approx(1.0)
    assert out[2] == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# The calibrators themselves
# --------------------------------------------------------------------------- #


def test_isotonic_reproduces_sklearns_isotonic_regression():
    pytest.importorskip("sklearn", reason="research dependency")
    from sklearn.isotonic import IsotonicRegression

    rng = np.random.default_rng(12)
    p = rng.random(500)
    y = (rng.random(500) < p**2).astype(int)

    ours = fit_calibrator(p, y, method="isotonic")
    theirs = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(p, y)
    grid = np.linspace(0.0, 1.0, 201)
    assert np.max(np.abs(ours.predict(grid) - theirs.predict(grid))) < 1e-12


def test_both_calibrators_improve_log_loss_on_held_out_rows():
    """The point of calibration, measured where it counts."""
    rng = np.random.default_rng(13)
    n = 4000
    true_p = rng.random(n) * 0.6 + 0.05
    y = (rng.random(n) < true_p).astype(int)
    # A badly scaled score: monotone in the truth, wrong in level.
    raw = np.clip(true_p * 1.6 + 0.2, 0.001, 0.999)

    fit, test = slice(0, 2000), slice(2000, None)
    before = log_loss(y[test], raw[test])
    for method in ("platt", "isotonic"):
        cal = fit_calibrator(raw[fit], y[fit], method=method)
        after = calibrate_and_evaluate(cal, raw[test], y[test]).log_loss
        assert after < before, f"{method} made the probability worse"


def test_calibrators_are_monotone():
    rng = np.random.default_rng(14)
    p = rng.random(1000)
    y = (rng.random(1000) < p).astype(int)
    grid = np.linspace(0.0, 1.0, 101)
    for method in ("platt", "isotonic"):
        out = fit_calibrator(p, y, method=method).predict(grid)
        assert np.all(np.diff(out) >= -1e-12), f"{method} is not monotone"


def test_calibrated_output_stays_inside_zero_and_one():
    p, y = _binary(500, seed=15)
    for method in ("platt", "isotonic", "identity"):
        out = fit_calibrator(p, y, method=method).predict(np.array([-5.0, 0.0, 0.5, 1.0, 5.0]))
        assert np.all(out >= 0.0) and np.all(out <= 1.0)


def test_isotonic_handles_a_single_distinct_prediction():
    p = np.full(100, 0.4)
    y = (np.arange(100) < 30).astype(int)
    cal = fit_calibrator(p, y, method="isotonic")
    assert cal.predict(np.array([0.4]))[0] == pytest.approx(0.30)


def test_calibrator_payload_round_trips():
    p, y = _binary(400, seed=16)
    for method, cls in (("platt", PlattCalibrator), ("isotonic", IsotonicCalibrator),
                        ("identity", IdentityCalibrator)):
        cal = fit_calibrator(p, y, method=method)
        assert isinstance(cal, cls)
        rebuilt = calibrator_from_payload(cal.to_payload())
        grid = np.linspace(0.01, 0.99, 50)
        assert np.allclose(rebuilt.predict(grid), cal.predict(grid), atol=1e-15)
        assert rebuilt.fit_fingerprint == frozenset(), (
            "the fit fingerprint must not be serialised into production"
        )


def test_an_unknown_calibration_method_is_an_error():
    p, y = _binary(100, seed=17)
    with pytest.raises(ValueError, match="unknown calibration method"):
        fit_calibrator(p, y, method="magic")
    with pytest.raises(ValueError, match="unknown calibrator payload"):
        calibrator_from_payload({"method": "magic"})


def test_a_calibrator_records_how_many_rows_it_saw():
    p, y = _binary(321, seed=18)
    cal = fit_calibrator(p, y, method="platt")
    assert cal.n_fit == 321
    assert cal.method == "platt"


def test_platt_on_already_calibrated_input_is_close_to_the_identity():
    p, y = _binary(6000, seed=19, sharpness=1.5)
    cal = fit_calibrator(p, y, method="platt")
    assert cal.a == pytest.approx(1.0, abs=0.15)
    assert cal.b == pytest.approx(0.0, abs=0.15)
