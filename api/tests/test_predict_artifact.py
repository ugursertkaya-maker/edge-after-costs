"""The artifact tests — proving the worker never needs scikit-learn.

Two of these are the production constraint itself rather than a nicety:

* :func:`test_the_export_reproduces_sklearn_predict_proba_to_1e9` runs for every
  one of the five models and is the spec's own tolerance.  It is the test that
  found the real bug in this module — ``sklearn.tree`` casts its input to
  float32 before walking a tree, and a float64 re-implementation disagreed by
  2.4e-3 on a 300-tree forest, three orders of magnitude outside tolerance.
* :func:`test_loading_an_artifact_in_a_fresh_interpreter_never_imports_sklearn`
  starts a **new interpreter**, loads a saved artifact, scores a row and asserts
  ``"sklearn" not in sys.modules``.  Checking that inside this process would
  prove nothing, because pytest has already imported sklearn by then.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
import pytest

from app.predict.artifact import (
    ARTIFACT_VERSION,
    EXPORT_TOLERANCE,
    ExportError,
    FeatureContractError,
    PredictiveArtifact,
    TreeEnsembleSpec,
    assert_feature_names,
    export,
    load,
    predict_tree_ensemble,
    save,
    verify_export,
)

API_DIR = Path(__file__).resolve().parent.parent

sklearn = pytest.importorskip(
    "sklearn",
    reason="scikit-learn is a research dependency; the artifact itself needs numpy only",
)

from app.predict.calibrate import fit_calibrator  # noqa: E402
from app.predict.models import MODEL_NAMES, split_matrix, train_model  # noqa: E402
from test_predict_models import SMALL, make_split, synthetic_matrix  # noqa: E402


def fitted(name: str, *, n: int = 2400, seed: int = 41):
    """A trained model plus the held-out rows to verify its export against."""
    mx = synthetic_matrix(n=n, seed=seed, edge=1.2)
    split = make_split(mx)
    train, validate, test = split_matrix(mx, split)
    model = train_model(
        name, train.X, train.y, feature_names=train.feature_names, **SMALL.get(name, {})
    )
    return model, validate, test


def exported(name: str, **kw):
    model, validate, test = fitted(name)
    art = export(
        model,
        spec_key="net15|atr:2|h=25",
        probability_threshold=0.62,
        ev_threshold_usd=0.02,
        trained_to_ms=1_700_000_000_000,
        feature_version="fv-test-1",
        validate_X=validate.X,
        **kw,
    )
    return art, model, validate, test


# --------------------------------------------------------------------------- #
# THE 1e-9 TEST
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", MODEL_NAMES)
def test_the_export_reproduces_sklearn_predict_proba_to_1e9(name):
    """The spec's tolerance, for every model type that can be a champion."""
    art, model, _validate, test = exported(name)

    reference = model.predict_proba(test.X)
    ours = art.predict_proba(test.X)

    assert ours.shape == reference.shape == (len(test), 2)
    gap = float(np.max(np.abs(reference - ours)))
    assert gap <= EXPORT_TOLERANCE, (
        f"{name}: the numpy export differs from sklearn by {gap:.3e}"
    )
    assert np.allclose(ours.sum(axis=1), 1.0)


@pytest.mark.parametrize("name", MODEL_NAMES)
def test_the_export_still_matches_after_a_save_and_load(name, tmp_path):
    art, model, _validate, test = exported(name)
    reloaded = load(save(art, tmp_path / f"{name}.npz"))
    assert np.array_equal(reloaded.predict_proba(test.X), art.predict_proba(test.X))
    gap = float(np.max(np.abs(model.predict_proba(test.X) - reloaded.predict_proba(test.X))))
    assert gap <= EXPORT_TOLERANCE


def test_tree_traversal_matches_sklearn_on_rows_sitting_exactly_on_a_split():
    """The float32 regression test.

    sklearn rounds X to float32 before it walks a tree.  A row within a float32
    ulp of a threshold goes left in sklearn and right in a naive float64
    re-implementation; with hundreds of trees, some row always is.  Feeding the
    thresholds back in as feature values makes that collision deliberate.
    """
    art, model, _validate, test = exported("random_forest")
    spec = TreeEnsembleSpec.from_payload(art.payload, art.payload["tree_meta"])
    assert spec.input_dtype == "float32", "sklearn trees must be replayed in float32"

    # Snap every column onto a real split threshold for its own feature, so the
    # comparison lands exactly where the two implementations could diverge.
    boundary = model.transform(test.X)
    for j in range(boundary.shape[1]):
        thresholds = spec.threshold[spec.feature == j]
        if thresholds.size:
            boundary[:, j] = np.resize(thresholds, boundary.shape[0])
    raw = boundary * model.scaler_scale + model.scaler_mean

    gap = float(np.max(np.abs(model.predict_proba(raw) - art.predict_proba(raw))))
    assert gap <= EXPORT_TOLERANCE, f"boundary rows disagree by {gap:.3e}"


# --------------------------------------------------------------------------- #
# NO SKLEARN IN THE INFERENCE PATH
# --------------------------------------------------------------------------- #


def _run_isolated(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=str(API_DIR),
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_loading_an_artifact_in_a_fresh_interpreter_never_imports_sklearn(tmp_path):
    """Load and score in a brand-new process; sklearn must never appear."""
    art, _model, _validate, test = exported("random_forest")
    path = tmp_path / "champion.npz"
    save(art, path)
    row_path = tmp_path / "rows.npy"
    np.save(row_path, test.X[:25])

    result = _run_isolated(
        f"""
        import sys
        assert "sklearn" not in sys.modules, "sklearn was already imported at start-up"
        import numpy as np
        from app.predict.artifact import load
        art = load({str(path)!r})
        rows = np.load({str(row_path)!r})
        proba = art.predict_proba(rows)
        assert proba.shape == (25, 2), proba.shape
        bad = sorted(m for m in sys.modules if m.split(".")[0] in
                     {{"sklearn", "scipy", "joblib", "pandas"}})
        assert not bad, "research libraries reached the inference path: " + repr(bad)
        np.save({str(tmp_path / "proba.npy")!r}, proba)
        print("OK")
        """
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
    assert np.array_equal(np.load(tmp_path / "proba.npy"), art.predict_proba(test.X[:25]))


def test_the_artifact_module_imports_with_sklearn_uninstallable(tmp_path):
    """The production image does not ship sklearn; importing must still work."""
    art, _model, _validate, test = exported("logistic")
    path = tmp_path / "champion.npz"
    save(art, path)

    result = _run_isolated(
        f"""
        import sys
        class Blocker:
            def find_module(self, name, path=None):
                return self.find_spec(name, path)
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in {{"sklearn", "scipy", "joblib"}}:
                    raise ImportError("blocked: production image has no " + name)
                return None
        sys.meta_path.insert(0, Blocker())
        import numpy as np
        from app.predict.artifact import load
        art = load({str(path)!r})
        p = art.predict_proba(np.zeros((3, len(art.feature_names))))
        assert p.shape == (3, 2)
        print("OK")
        """
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_calibrate_needs_no_sklearn_either():
    """calibrate.py is imported by the inference path through ``expit``."""
    result = _run_isolated(
        """
        import sys
        import app.predict.calibrate as c
        import numpy as np
        cal = c.fit_calibrator(np.linspace(0.01, 0.99, 200),
                               (np.arange(200) % 3 == 0).astype(int), method="isotonic")
        assert cal.predict(np.array([0.5])).shape == (1,)
        assert "sklearn" not in sys.modules
        print("OK")
        """
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_artifact_source_does_not_mention_sklearn_in_an_import():
    source = (API_DIR / "app" / "predict" / "artifact.py").read_text()
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "sklearn" not in stripped, f"inference path imports sklearn: {line}"


# --------------------------------------------------------------------------- #
# Export refuses rather than degrading
# --------------------------------------------------------------------------- #


def test_an_unexportable_model_is_refused_in_words():
    class SVM:
        def predict_proba(self, X):  # pragma: no cover - never called
            raise AssertionError

    class Wrapper:
        name = "svm"
        estimator = SVM()
        feature_names = ["a"]
        scaler_mean = None
        scaler_scale = None

    with pytest.raises(ExportError, match="cannot be a champion"):
        export(
            Wrapper(),
            spec_key="k",
            probability_threshold=0.5,
            ev_threshold_usd=0.0,
            trained_to_ms=0,
            feature_version="fv",
        )


def test_export_raises_instead_of_shipping_a_predictor_that_disagrees():
    """Corrupt the exported coefficients; verification must catch it."""
    art, model, _validate, test = exported("logistic")
    broken = PredictiveArtifact(
        **{
            **{f.name: getattr(art, f.name) for f in art.__dataclass_fields__.values()},
            "payload": {**art.payload, "coef": art.payload["coef"] + 0.05},
        }
    )
    with pytest.raises(ExportError, match="cannot be a champion"):
        verify_export(broken, model, test.X)


def test_verify_export_returns_the_measured_gap_when_it_passes():
    art, model, _validate, test = exported("logistic")
    gap = verify_export(art, model, test.X)
    assert 0.0 <= gap <= EXPORT_TOLERANCE


def test_a_boosted_model_cannot_be_exported_without_held_out_rows():
    model, _validate, _test = fitted("gbm")
    with pytest.raises(ExportError, match="validate_X"):
        export(
            model,
            spec_key="k",
            probability_threshold=0.5,
            ev_threshold_usd=0.0,
            trained_to_ms=0,
            feature_version="fv",
        )


# --------------------------------------------------------------------------- #
# Feature-name compatibility — old model vs new matrix
# --------------------------------------------------------------------------- #


def test_a_renamed_feature_fails_loudly():
    art, _model, _validate, _test = exported("logistic")
    current = list(art.feature_names)
    current[1] = "sig_b_v2"
    with pytest.raises(FeatureContractError) as exc:
        assert_feature_names(art, current)
    assert "missing" in str(exc.value) and "sig_b" in str(exc.value)
    assert "unexpected" in str(exc.value)


def test_an_added_feature_fails_loudly():
    art, _model, _validate, _test = exported("logistic")
    with pytest.raises(FeatureContractError, match="unexpected"):
        assert_feature_names(art, list(art.feature_names) + ["book_new_thing"])


def test_a_reordered_matrix_fails_loudly_even_though_every_name_is_present():
    """The nastiest case: nothing is missing, every value feeds the wrong weight."""
    art, _model, _validate, _test = exported("logistic")
    shuffled = list(art.feature_names)
    shuffled[0], shuffled[2] = shuffled[2], shuffled[0]
    with pytest.raises(FeatureContractError, match="reordered"):
        assert_feature_names(art, shuffled)


def test_the_matching_matrix_is_accepted():
    art, _model, _validate, _test = exported("logistic")
    assert_feature_names(art, list(art.feature_names)) is None


def test_predicting_with_the_wrong_number_of_columns_is_refused():
    art, _model, _validate, test = exported("logistic")
    with pytest.raises(FeatureContractError, match="expects"):
        art.predict_proba(test.X[:, :-1])


def test_a_row_containing_nan_is_refused_rather_than_guessed_at():
    art, _model, _validate, test = exported("logistic")
    bad = test.X[:5].copy()
    bad[2, 1] = np.nan
    with pytest.raises(FeatureContractError, match="NaN"):
        art.predict_proba(bad)


# --------------------------------------------------------------------------- #
# Round-trip and metadata
# --------------------------------------------------------------------------- #


def test_save_load_round_trips_every_contract_field(tmp_path):
    model, validate, test = fitted("logistic")
    cal = fit_calibrator(
        model.predict_p_win(validate.X), validate.y, method="isotonic", keys=validate.ts_ms
    )
    art = export(
        model,
        spec_key="net17|atr:2|h=60",
        probability_threshold=0.615,
        ev_threshold_usd=0.0123456789,
        trained_to_ms=1_700_123_456_789,
        feature_version="fv-9",
        validate_X=validate.X,
        calibrator=cal,
        calibration={"max_abs_error": 0.041, "calibrated": True, "method": "isotonic"},
        oos={"trades": 42, "pf": 1.31},
        supported_regimes=["VOLATILITY_EXPANSION"],
        net_target_bps=17.0,
        stop_bps=34.0,
        notes=["synthetic"],
    )
    path = save(art, tmp_path / "deep" / "champion.npz")
    back = load(path)

    for field in (
        "version", "model_name", "feature_names", "spec_key", "probability_threshold",
        "ev_threshold_usd", "trained_to_ms", "feature_version", "calibration", "oos",
        "supported_regimes", "net_target_bps", "stop_bps", "notes",
    ):
        assert getattr(back, field) == getattr(art, field), field
    assert back.probability_threshold == 0.615
    assert back.ev_threshold_usd == 0.0123456789  # exact, not rounded through text
    assert np.array_equal(back.predict_proba(test.X), art.predict_proba(test.X))


def test_the_artifact_carries_the_scaler_that_was_fitted_on_training_rows():
    art, model, _validate, _test = exported("logistic")
    assert np.array_equal(art.payload["scaler_mean"], model.scaler_mean)
    assert np.array_equal(art.payload["scaler_scale"], model.scaler_scale)


def test_calibration_read_outs_default_to_uncalibrated():
    """Never measured must read as not calibrated, not as fine."""
    art, _model, _validate, _test = exported("logistic")
    assert art.calibration is None
    assert art.calibrated is False
    assert art.calibration_max_abs_error == 1.0


def test_a_measured_calibration_sets_the_engine_read_outs():
    art, _model, validate, test = exported("logistic")
    from app.predict.calibrate import calibrate_and_evaluate

    cal = fit_calibrator(
        art.predict_p_win(validate.X), validate.y, method="isotonic", keys=validate.ts_ms
    )
    report = calibrate_and_evaluate(
        cal, art.predict_p_win(test.X), test.y, min_bin_count=20, keys=test.ts_ms
    )
    with_cal = art.with_calibration(report)
    assert with_cal.calibrated == report.calibrated
    assert with_cal.calibration_max_abs_error == pytest.approx(report.max_abs_error)


def test_the_exported_calibrator_reproduces_the_fitted_one():
    model, validate, test = fitted("hist_gbm")
    cal = fit_calibrator(
        model.predict_p_win(validate.X), validate.y, method="platt", keys=validate.ts_ms
    )
    art = export(
        model, spec_key="k", probability_threshold=0.5, ev_threshold_usd=0.0,
        trained_to_ms=0, feature_version="fv", validate_X=validate.X, calibrator=cal,
    )
    expected = cal.predict(model.predict_p_win(test.X))
    assert np.max(np.abs(art.predict_p_win(test.X) - expected)) <= EXPORT_TOLERANCE


def test_an_isotonic_calibrator_also_survives_the_round_trip(tmp_path):
    model, validate, test = fitted("logistic")
    cal = fit_calibrator(
        model.predict_p_win(validate.X), validate.y, method="isotonic", keys=validate.ts_ms
    )
    art = export(
        model, spec_key="k", probability_threshold=0.5, ev_threshold_usd=0.0,
        trained_to_ms=0, feature_version="fv", validate_X=validate.X, calibrator=cal,
    )
    back = load(save(art, tmp_path / "iso.npz"))
    assert np.array_equal(back.predict_p_win(test.X), art.predict_p_win(test.X))
    assert back.payload["calibrator"]["method"] == "isotonic"


def test_describe_reports_the_things_an_operator_needs():
    art, _model, _validate, _test = exported("gbm")
    text = art.describe()
    assert art.spec_key in text
    assert art.feature_version in text
    assert "calibrated" in text


# --------------------------------------------------------------------------- #
# Loading refuses what it should
# --------------------------------------------------------------------------- #


def test_loading_a_missing_file_says_so(tmp_path):
    with pytest.raises(FileNotFoundError):
        load(tmp_path / "nope.npz")


def test_loading_something_that_is_not_an_artifact_is_refused(tmp_path):
    path = tmp_path / "random.npz"
    np.savez(path, junk=np.arange(3))
    with pytest.raises(FeatureContractError, match="not a predictive artifact"):
        load(path)


def test_an_artifact_from_an_older_format_is_refused_not_guessed_at(tmp_path):
    art, _model, _validate, _test = exported("logistic")
    path = tmp_path / "old.npz"
    save(art, path)
    with np.load(path, allow_pickle=False) as z:
        arrays = {k: z[k] for k in z.files}
    meta = json.loads(bytes(arrays["__meta__"].tobytes()).decode("utf-8"))
    meta["artifact_version"] = "phase4-0"
    arrays["__meta__"] = np.frombuffer(json.dumps(meta).encode("utf-8"), dtype=np.uint8)
    np.savez(path, **arrays)
    with pytest.raises(FeatureContractError, match="Re-export the champion"):
        load(path)


def test_the_artifact_format_version_is_recorded(tmp_path):
    art, _model, _validate, _test = exported("logistic")
    path = save(art, tmp_path / "v.npz")
    with np.load(path, allow_pickle=False) as z:
        meta = json.loads(bytes(z["__meta__"].tobytes()).decode("utf-8"))
    assert meta["artifact_version"] == ARTIFACT_VERSION


def test_load_never_unpickles(tmp_path):
    """An artifact is data. Unpickling would mean it could execute code."""
    path = tmp_path / "pickled.npz"
    np.savez(path, __meta__=np.array([{"evil": True}], dtype=object))
    with pytest.raises(ValueError, match="allow_pickle"):
        load(path)


# --------------------------------------------------------------------------- #
# The flat tree spec itself
# --------------------------------------------------------------------------- #


def test_tree_spec_validates_its_own_shape():
    spec = TreeEnsembleSpec(
        feature=np.array([0, -1, -1], dtype=np.int32),
        threshold=np.array([0.5, 0.0, 0.0]),
        left=np.array([1, 1, 2], dtype=np.int32),
        right=np.array([2, 1, 2], dtype=np.int32),
        value=np.array([0.0, 0.2, 0.8]),
        root=np.array([0], dtype=np.int32),
        combine="mean_proba",
    )
    spec.validate()
    assert spec.n_trees == 1
    assert spec.n_nodes == 3
    out = predict_tree_ensemble(spec, np.array([[0.4], [0.6]]))
    assert out.tolist() == [0.2, 0.8]


def test_tree_spec_rejects_a_ragged_payload():
    spec = TreeEnsembleSpec(
        feature=np.array([0, -1], dtype=np.int32),
        threshold=np.array([0.5]),
        left=np.array([1, 1], dtype=np.int32),
        right=np.array([1, 1], dtype=np.int32),
        value=np.array([0.0, 1.0]),
        root=np.array([0], dtype=np.int32),
    )
    with pytest.raises(ExportError, match="threshold"):
        spec.validate()


def test_tree_spec_rejects_a_child_index_outside_the_arrays():
    spec = TreeEnsembleSpec(
        feature=np.array([0, -1], dtype=np.int32),
        threshold=np.array([0.5, 0.0]),
        left=np.array([9, 1], dtype=np.int32),
        right=np.array([1, 1], dtype=np.int32),
        value=np.array([0.0, 1.0]),
        root=np.array([0], dtype=np.int32),
    )
    with pytest.raises(ExportError, match="outside the node arrays"):
        spec.validate()


def test_tree_spec_rejects_an_unknown_combine_rule():
    spec = TreeEnsembleSpec(
        feature=np.array([-1], dtype=np.int32),
        threshold=np.array([0.0]),
        left=np.array([0], dtype=np.int32),
        right=np.array([0], dtype=np.int32),
        value=np.array([0.5]),
        root=np.array([0], dtype=np.int32),
        combine="average_of_vibes",
    )
    with pytest.raises(ExportError, match="combine"):
        spec.validate()


@pytest.mark.parametrize("name", ["gbm", "random_forest", "hist_gbm"])
def test_every_exported_ensemble_is_internally_consistent(name):
    art, _model, _validate, _test = exported(name)
    spec = TreeEnsembleSpec.from_payload(art.payload, art.payload["tree_meta"])
    spec.validate()
    assert spec.n_trees > 1
    assert art.kind == "tree_ensemble"


def test_a_linear_export_carries_coefficients_not_an_estimator():
    art, model, _validate, _test = exported("logistic_l2")
    assert art.kind == "linear"
    assert art.payload["coef"].shape == (len(model.feature_names),)
    assert isinstance(art.payload["intercept"], float)
    assert not any(
        hasattr(v, "predict_proba") for v in art.payload.values()
    ), "a fitted estimator has been smuggled into the payload"
