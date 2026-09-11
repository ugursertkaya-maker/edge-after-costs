"""The model benchmark, and the tests that stop it flattering itself.

The single most important test in this file is
:func:`test_a_pipeline_that_finds_an_edge_in_pure_noise_is_broken`.  Phase 3
found a pooled out-of-sample profit factor of 0.22 across 590,306 trades, so the
prior on "the classifier found something" is low and the prior on "the harness
is leaking" is not.  A pipeline that reports an edge on random labels would
report one on market data too, and there would be no way to tell the two results
apart.  That test is the thing that says which one we are looking at.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest

sklearn = pytest.importorskip(
    "sklearn",
    reason="scikit-learn is a research dependency (see api/requirements-dev.txt); "
    "production runs the numpy-only artifact instead",
)

from app.predict.models import (  # noqa: E402
    BASELINE_NAME,
    MODEL_NAMES,
    MatrixSlice,
    Metrics,
    ModelReport,
    ModelTrainingError,
    ablate_feature_groups,
    audit_importances,
    benchmark,
    drop_features,
    evaluate_model,
    feature_groups,
    format_ablation,
    format_benchmark,
    format_importances,
    permutation_importance,
    select_champion,
    split_matrix,
    train_model,
)

MINUTE = 60_000
T0 = 1_700_000_000_000

#: Small estimators everywhere: these tests check behaviour, not convergence,
#: and the whole suite has to stay fast.
SMALL = {
    "gbm": {"n_estimators": 40, "max_depth": 2},
    "random_forest": {"n_estimators": 40, "max_depth": 4},
    "hist_gbm": {"max_iter": 40, "max_depth": 3},
}


@dataclass(frozen=True)
class FakeSplit:
    """The frozen ``splits.Split`` shape, without importing another agent's module."""

    name: str
    train: tuple[int, int]
    validate: tuple[int, int]
    test: tuple[int, int]
    purged: int = 0
    embargoed: int = 0


def synthetic_matrix(
    n: int = 2400,
    *,
    seed: int = 11,
    edge: float = 1.0,
    base_rate: float = 0.12,
    horizon_minutes: int = 25,
    n_noise: int = 4,
) -> MatrixSlice:
    """A labelled matrix with a known, adjustable amount of real signal.

    ``edge=0`` produces labels that are independent of every column — the noise
    case.  Three named columns carry the signal when ``edge > 0``; the rest are
    drawn independently and carry nothing, which is what makes ablation and
    permutation importance checkable against a known answer.
    """
    rng = np.random.default_rng(seed)
    signal_names = ["sig_a", "sig_b", "sig_c"]
    noise_names = [f"noise_{i}" for i in range(n_noise)]
    names = signal_names + noise_names
    X = rng.normal(size=(n, len(names)))
    z = edge * (0.9 * X[:, 0] + 0.6 * X[:, 1] - 0.5 * X[:, 2]) + rng.normal(size=n)
    if edge == 0.0:
        # Labels drawn from their own stream so they cannot correlate with X
        # even by construction.
        y = (rng.random(n) < base_rate).astype(int)
    else:
        y = (z > np.quantile(z, 1.0 - base_rate)).astype(int)
    ts = T0 + np.arange(n, dtype=np.int64) * MINUTE
    return MatrixSlice(
        X=X,
        y=y,
        ts_ms=ts,
        feature_names=names,
        label_end_ms=ts + horizon_minutes * MINUTE,
        symbols=["BTCUSD"] * n,
    )


def make_split(mx: MatrixSlice, *, train: float = 0.6, validate: float = 0.8) -> FakeSplit:
    ts = mx.ts_ms
    lo = int(ts[0])
    hi = int(ts[-1]) + 1
    a = int(ts[int(len(ts) * train)])
    b = int(ts[int(len(ts) * validate)])
    return FakeSplit("s0", (lo, a), (a, b), (b, hi))


# --------------------------------------------------------------------------- #
# THE NOISE TEST
# --------------------------------------------------------------------------- #


def test_a_pipeline_that_finds_an_edge_in_pure_noise_is_broken():
    """Random labels must produce AUC ≈ 0.5 from every one of the five models.

    This is the load-bearing test of the whole benchmark.  Purging, the
    train-only scaler, the chronological split and the out-of-sample evaluation
    all exist to make this come out at 0.5; if any of them regresses, some
    model starts reporting an edge here, and the failure shows up on synthetic
    data with a known answer instead of on market data where nobody can tell.
    """
    mx = synthetic_matrix(n=9000, seed=4, edge=0.0, base_rate=0.10)
    reports = benchmark(mx, make_split(mx, train=0.6, validate=0.75), model_kwargs=SMALL)

    assert {r.name for r in reports} == set(MODEL_NAMES)
    for r in reports:
        assert r.error is None, f"{r.name} failed to train: {r.error}"
        assert 0.42 < r.oos.roc_auc < 0.58, (
            f"{r.name} reports ROC-AUC {r.oos.roc_auc:.3f} on labels that are "
            "independent of every feature. Either the split leaks or the "
            "evaluation is being run on training rows."
        )
        assert 0.40 < r.oos.pr_auc / max(r.oos.base_rate, 1e-9) < 2.0 or (
            r.oos.pr_auc < 3 * r.oos.base_rate
        ), f"{r.name} PR-AUC {r.oos.pr_auc:.3f} far above the base rate on noise"


def test_noise_never_promotes_a_complicated_model_on_a_real_margin():
    """On noise the winner is the baseline or a hair's-breadth fluke, never a landslide."""
    mx = synthetic_matrix(n=9000, seed=4, edge=0.0, base_rate=0.10)
    reports = benchmark(mx, make_split(mx, train=0.6, validate=0.75), model_kwargs=SMALL)
    for r in reports:
        if r.is_baseline:
            continue
        assert r.delta_roc_auc < 0.10, (
            f"{r.name} beats the baseline by {r.delta_roc_auc:+.3f} ROC-AUC on "
            "random labels — that is a leak, not a model"
        )


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #


def test_model_names_match_the_frozen_contract():
    assert MODEL_NAMES == ("logistic", "logistic_l2", "gbm", "random_forest", "hist_gbm")
    assert BASELINE_NAME == "logistic"
    assert BASELINE_NAME in MODEL_NAMES


def test_train_model_rejects_an_unknown_name():
    mx = synthetic_matrix(n=300)
    with pytest.raises(ValueError, match="unknown model"):
        train_model("xgboost", mx.X, mx.y)


def test_trained_model_predict_proba_is_sklearns_two_column_layout():
    mx = synthetic_matrix(n=600)
    m = train_model("logistic", mx.X, mx.y, feature_names=mx.feature_names)
    p = m.predict_proba(mx.X)
    assert p.shape == (600, 2)
    assert np.allclose(p.sum(axis=1), 1.0)
    assert np.allclose(p[:, 1], m.predict_p_win(mx.X))


# --------------------------------------------------------------------------- #
# The scaler: training slice only
# --------------------------------------------------------------------------- #


def test_scaler_is_fitted_on_the_training_slice_only():
    """matrix.py emits unscaled features so that this decision is made once, here."""
    mx = synthetic_matrix(n=2000, seed=3)
    # Shift the later half hard, so a scaler fitted on everything would differ
    # from one fitted on the training rows by an amount no rounding explains.
    mx.X[1000:, 0] += 50.0
    split = make_split(mx)
    train, _validate, _test = split_matrix(mx, split)

    m = train_model("logistic", train.X, train.y, feature_names=train.feature_names)

    assert np.allclose(m.scaler_mean, train.X.mean(axis=0))
    assert np.allclose(m.scaler_scale, train.X.std(axis=0))
    assert not np.allclose(m.scaler_mean, mx.X.mean(axis=0)), (
        "the scaler saw rows outside the training slice — the test distribution "
        "has leaked into training"
    )
    assert np.allclose(m.transform(train.X).mean(axis=0), 0.0, atol=1e-9)


def test_scaler_survives_a_constant_column_without_producing_nan():
    mx = synthetic_matrix(n=500, seed=8)
    mx.X[:, 3] = 4.0
    m = train_model("logistic", mx.X, mx.y, feature_names=mx.feature_names)
    assert m.scaler_scale[3] == 1.0
    assert np.all(np.isfinite(m.transform(mx.X)))


def test_the_scaler_travels_with_the_model():
    mx = synthetic_matrix(n=800, seed=9)
    m = train_model("logistic", mx.X, mx.y, feature_names=mx.feature_names)
    assert m.scaler_mean is not None and m.scaler_scale is not None
    assert m.scaler_mean.shape == (mx.X.shape[1],)
    manual = (mx.X - m.scaler_mean) / m.scaler_scale
    assert np.allclose(m.transform(mx.X), manual)


# --------------------------------------------------------------------------- #
# Purge and embargo
# --------------------------------------------------------------------------- #


def test_purge_drops_training_rows_whose_label_window_crosses_the_boundary():
    mx = synthetic_matrix(n=1200, seed=5, horizon_minutes=30)
    split = make_split(mx)
    train, validate, test = split_matrix(mx, split)

    assert len(train) > 0
    assert int(train.label_end_ms.max()) <= split.validate[0], (
        "a training row's label is still resolving inside the validation window"
    )
    assert int(validate.label_end_ms.max()) <= split.test[0]
    # The purge actually removed something, i.e. this is not vacuous.
    unpurged = int(((mx.ts_ms >= split.train[0]) & (mx.ts_ms < split.train[1])).sum())
    assert len(train) < unpurged
    assert len(test) > 0


def test_embargo_pushes_the_cut_further_back():
    mx = synthetic_matrix(n=1200, seed=5, horizon_minutes=30)
    split = make_split(mx)
    plain, _, _ = split_matrix(mx, split, embargo_minutes=0)
    embargoed, _, _ = split_matrix(mx, split, embargo_minutes=60)
    assert len(embargoed) < len(plain)
    assert int(embargoed.label_end_ms.max()) <= split.validate[0] - 60 * MINUTE


def test_the_three_slices_never_share_a_row():
    mx = synthetic_matrix(n=1500, seed=6)
    split = make_split(mx)
    train, validate, test = split_matrix(mx, split)
    ids = [set(s.ts_ms.tolist()) for s in (train, validate, test)]
    assert ids[0] & ids[1] == set()
    assert ids[1] & ids[2] == set()
    assert ids[0] & ids[2] == set()


# --------------------------------------------------------------------------- #
# Imbalance handled honestly (item 10)
# --------------------------------------------------------------------------- #


def test_accuracy_rewards_the_useless_majority_model_which_is_why_it_is_not_used():
    """The argument for ignoring accuracy, as two numbers rather than a claim."""
    y = np.zeros(1000, dtype=int)
    y[:80] = 1  # 8% base rate
    always_no = np.full(1000, 0.02)
    from app.predict.models import score_probabilities

    m = score_probabilities(always_no, y)
    assert m.accuracy == pytest.approx(0.92)
    assert np.isnan(m.precision), "a model that never fires has no precision"
    assert m.recall == 0.0
    assert m.positive_rate == 0.0


def test_class_weighting_stops_the_model_predicting_the_majority_forever():
    mx = synthetic_matrix(n=3000, seed=12, base_rate=0.08, edge=1.2)
    split = make_split(mx)
    reports = benchmark(mx, split, model_kwargs=SMALL)
    for r in reports:
        assert r.error is None
        assert r.oos.positive_rate > 0.02, (
            f"{r.name} fires on {r.oos.positive_rate:.3%} of rows — it has "
            "collapsed onto the majority class"
        )
        assert r.oos.recall > 0.2, f"{r.name} recall {r.oos.recall:.3f} is majority-collapse"


def test_every_model_states_which_imbalance_treatment_it_used():
    mx = synthetic_matrix(n=1200, seed=13, base_rate=0.10)
    for name in MODEL_NAMES:
        m = train_model(
            name, mx.X, mx.y, feature_names=mx.feature_names, **SMALL.get(name, {})
        )
        assert m.imbalance_method, f"{name} does not say how it handled imbalance"
        if name == "gbm":
            assert "sample_weight" in m.imbalance_method
        else:
            assert "class_weight" in m.imbalance_method


def test_balanced_sample_weight_matches_sklearns_formula():
    """``gbm`` has no ``class_weight``; the sample weights must be the same policy."""
    from sklearn.utils.class_weight import compute_sample_weight

    from app.predict.models import _balanced_sample_weight

    y = np.array([0] * 90 + [1] * 10)
    assert np.allclose(_balanced_sample_weight(y), compute_sample_weight("balanced", y))


def test_benchmark_reports_precision_recall_roc_pr_and_brier():
    mx = synthetic_matrix(n=2000, seed=14)
    reports = benchmark(mx, make_split(mx), model_kwargs=SMALL)
    for r in reports:
        for field in ("precision", "recall", "roc_auc", "pr_auc", "brier"):
            assert hasattr(r.oos, field)
            assert np.isfinite(getattr(r.oos, field)), f"{r.name}.{field} is not finite"


def test_balanced_weights_leave_the_raw_score_uncalibrated_by_design():
    """Documented consequence: the raw score of a balanced model is not a probability."""
    mx = synthetic_matrix(n=4000, seed=15, base_rate=0.08, edge=1.0)
    split = make_split(mx)
    train, _, test = split_matrix(mx, split)
    m = train_model("logistic", train.X, train.y, feature_names=train.feature_names)
    mean_p = float(m.predict_p_win(test.X).mean())
    assert mean_p > 3 * float(test.y.mean()), (
        "balanced class weights are expected to inflate the raw score far above "
        "the base rate; calibrate.py is what maps it back"
    )


# --------------------------------------------------------------------------- #
# Complexity must beat the baseline
# --------------------------------------------------------------------------- #


def test_the_baseline_is_trained_even_when_it_was_not_requested():
    mx = synthetic_matrix(n=1500, seed=16)
    reports = benchmark(mx, make_split(mx), names=("hist_gbm",), model_kwargs=SMALL)
    assert [r.name for r in reports] == [BASELINE_NAME, "hist_gbm"]
    assert reports[0].verdict == "BASELINE"


def test_a_model_that_fails_to_beat_the_baseline_is_named_as_such():
    """A deliberately crippled gbm must be reported as not worth its complexity."""
    mx = synthetic_matrix(n=2000, seed=17)
    reports = benchmark(
        mx,
        make_split(mx),
        names=("gbm",),
        model_kwargs={"gbm": {"n_estimators": 1, "max_depth": 1, "learning_rate": 0.001}},
    )
    gbm = next(r for r in reports if r.name == "gbm")
    assert gbm.verdict in ("NOT_WORTH_ITS_COMPLEXITY", "DEGENERATE")
    assert gbm.beats_baseline is False
    assert gbm.survives is False
    assert gbm.notes, "a rejected model must say why"


def test_select_champion_keeps_the_baseline_when_nothing_beats_it():
    base = ModelReport(
        name=BASELINE_NAME, model=None, train=None,
        oos=Metrics(100, 0.1, 0.3, 0.5, 0.70, 0.30, 0.10, 0.4, 0.9, 0.2),
        eval_on="validate", is_baseline=True, beats_baseline=False, verdict="BASELINE",
        delta_pr_auc=0.0, delta_roc_auc=0.0, delta_brier=0.0,
    )
    worse = ModelReport(
        name="gbm", model=None, train=None,
        oos=Metrics(100, 0.1, 0.3, 0.5, 0.69, 0.29, 0.11, 0.4, 0.9, 0.2),
        eval_on="validate", is_baseline=False, beats_baseline=False,
        verdict="NOT_WORTH_ITS_COMPLEXITY",
        delta_pr_auc=-0.01, delta_roc_auc=-0.01, delta_brier=0.01,
    )
    champ, why = select_champion([base, worse])
    assert champ is base
    assert "complexity bought nothing" in why


def test_select_champion_promotes_a_model_that_really_does_beat_the_baseline():
    base = ModelReport(
        name=BASELINE_NAME, model=None, train=None,
        oos=Metrics(100, 0.1, 0.3, 0.5, 0.70, 0.30, 0.10, 0.4, 0.9, 0.2),
        eval_on="validate", is_baseline=True, beats_baseline=False, verdict="BASELINE",
        delta_pr_auc=0.0, delta_roc_auc=0.0, delta_brier=0.0,
    )
    better = ModelReport(
        name="hist_gbm", model=None, train=None,
        oos=Metrics(100, 0.1, 0.4, 0.6, 0.78, 0.45, 0.08, 0.5, 0.9, 0.3),
        eval_on="validate", is_baseline=False, beats_baseline=True,
        verdict="BEATS_BASELINE",
        delta_pr_auc=0.15, delta_roc_auc=0.08, delta_brier=-0.02,
    )
    champ, why = select_champion([base, better])
    assert champ is better
    assert "beat the baseline" in why


def test_pr_auc_gain_bought_with_a_roc_auc_loss_is_rejected():
    mx = synthetic_matrix(n=800, seed=18)
    reports = benchmark(mx, make_split(mx), names=("logistic_l2",), model_kwargs=SMALL)
    # Whatever the verdict, the rule is arithmetic, not editorial.
    l2 = next(r for r in reports if r.name == "logistic_l2")
    if l2.delta_pr_auc >= 0.005 and l2.delta_roc_auc < 0:
        assert l2.verdict == "NOT_WORTH_ITS_COMPLEXITY"


def test_format_benchmark_names_the_champion_and_the_rejects():
    mx = synthetic_matrix(n=1500, seed=19)
    text = format_benchmark(benchmark(mx, make_split(mx), model_kwargs=SMALL))
    assert "champion:" in text
    assert "baseline = logistic" in text
    assert "NOT used for selection" in text


# --------------------------------------------------------------------------- #
# The test window is touched once
# --------------------------------------------------------------------------- #


def test_benchmark_scores_on_validation_not_test_by_default():
    mx = synthetic_matrix(n=2000, seed=20)
    split = make_split(mx)
    _, validate, test = split_matrix(mx, split)
    reports = benchmark(mx, split, names=(BASELINE_NAME,), model_kwargs=SMALL)
    assert reports[0].eval_on == "validate"
    assert reports[0].oos.n == len(validate)
    assert reports[0].oos.n != len(test)


def test_benchmark_can_be_asked_for_the_test_slice_explicitly():
    mx = synthetic_matrix(n=2000, seed=21)
    split = make_split(mx)
    _, _, test = split_matrix(mx, split)
    reports = benchmark(mx, split, names=(BASELINE_NAME,), eval_on="test", model_kwargs=SMALL)
    assert reports[0].oos.n == len(test)


def test_benchmark_rejects_an_unknown_evaluation_slice():
    mx = synthetic_matrix(n=400, seed=22)
    with pytest.raises(ValueError, match="validate"):
        benchmark(mx, make_split(mx), eval_on="train")


def test_benchmark_is_deterministic_under_the_same_seed():
    mx = synthetic_matrix(n=1500, seed=23)
    split = make_split(mx)
    a = benchmark(mx, split, seed=7, model_kwargs=SMALL)
    b = benchmark(mx, split, seed=7, model_kwargs=SMALL)
    for ra, rb in zip(a, b):
        assert ra.oos.roc_auc == rb.oos.roc_auc
        assert ra.oos.brier == rb.oos.brier


def test_a_single_class_training_slice_is_an_error_not_a_model():
    X = np.random.default_rng(0).normal(size=(200, 4))
    y = np.zeros(200, dtype=int)
    with pytest.raises(ModelTrainingError, match="single class"):
        train_model("logistic", X, y)


def test_a_model_that_fails_to_train_is_reported_not_swallowed():
    mx = synthetic_matrix(n=1000, seed=24)
    reports = benchmark(
        mx, make_split(mx), names=("random_forest",),
        model_kwargs={"random_forest": {"n_estimators": -5}},
    )
    rf = next(r for r in reports if r.name == "random_forest")
    assert rf.error is not None
    assert rf.verdict == "FAILED"
    assert rf.survives is False


# --------------------------------------------------------------------------- #
# Feature ablation (item 23)
# --------------------------------------------------------------------------- #


GROUPS = {"signal": ("sig_a", "sig_b", "sig_c"), "junk": ("noise_0", "noise_1")}


def test_ablation_flags_the_group_that_carries_the_model():
    mx = synthetic_matrix(n=4000, seed=25, edge=1.4)
    reports = ablate_feature_groups(mx, make_split(mx), groups=GROUPS)
    signal = next(r for r in reports if r.group == "signal")
    assert signal.delta_roc_auc < 0, "removing the only real signal must hurt"
    assert signal.verdict in ("USEFUL", "SUSPICIOUS_DOMINANCE")


def test_ablation_calls_total_dependence_on_one_group_a_leak_suspect():
    mx = synthetic_matrix(n=4000, seed=26, edge=1.6)
    reports = ablate_feature_groups(mx, make_split(mx), groups=GROUPS)
    signal = next(r for r in reports if r.group == "signal")
    assert signal.verdict == "SUSPICIOUS_DOMINANCE"
    assert "leak" in signal.note, "the warning has to say the word"


def test_ablation_flags_a_group_that_does_nothing():
    mx = synthetic_matrix(n=4000, seed=27, edge=1.2)
    reports = ablate_feature_groups(mx, make_split(mx), groups=GROUPS)
    junk = next(r for r in reports if r.group == "junk")
    assert junk.verdict in ("USELESS", "HARMFUL")
    assert abs(junk.delta_roc_auc) < 0.05


def test_ablation_refuses_to_remove_every_feature():
    mx = synthetic_matrix(n=800, seed=28)
    groups = {"everything": tuple(mx.feature_names)}
    reports = ablate_feature_groups(mx, make_split(mx), groups=groups)
    assert reports[0].verdict == "NOT_ABLATABLE"


def test_ablation_sign_convention_is_ablated_minus_full():
    mx = synthetic_matrix(n=3000, seed=29, edge=1.3)
    reports = ablate_feature_groups(mx, make_split(mx), groups=GROUPS)
    text = format_ablation(reports)
    assert "ablated − full" in text
    assert "negative means the group was doing work" in text


def test_drop_features_removes_exactly_the_named_columns():
    mx = synthetic_matrix(n=200, seed=30)
    smaller = drop_features(mx, ["sig_a", "noise_0"])
    assert smaller.feature_names == ["sig_b", "sig_c", "noise_1", "noise_2", "noise_3"]
    assert smaller.X.shape == (200, 5)
    assert np.array_equal(smaller.X[:, 0], mx.X[:, 1])


def test_feature_groups_accounts_for_every_column():
    mx = synthetic_matrix(n=100, seed=31)
    table = feature_groups(mx.feature_names)
    seen = {n for members in table.values() for n in members}
    assert seen == set(mx.feature_names), "a column that belongs to no group was dropped"


# --------------------------------------------------------------------------- #
# Permutation importance (item 24)
# --------------------------------------------------------------------------- #


def test_permutation_importance_finds_the_real_driver():
    mx = synthetic_matrix(n=4000, seed=32, edge=1.4)
    split = make_split(mx)
    train, validate, _ = split_matrix(mx, split)
    m = train_model(
        "random_forest", train.X, train.y, feature_names=train.feature_names, **SMALL["random_forest"]
    )
    imps = permutation_importance(m, validate.X, validate.y, n_repeats=4, seed=3)
    assert imps[0].feature == "sig_a"
    assert imps[0].rank == 1
    assert imps[0].mean_drop > 0.02


def test_permutation_importance_is_about_zero_for_an_unused_column():
    mx = synthetic_matrix(n=4000, seed=33, edge=1.4)
    split = make_split(mx)
    train, validate, _ = split_matrix(mx, split)
    m = train_model(
        "random_forest", train.X, train.y, feature_names=train.feature_names, **SMALL["random_forest"]
    )
    imps = {i.feature: i for i in permutation_importance(m, validate.X, validate.y, n_repeats=4, seed=3)}
    assert abs(imps["noise_3"].mean_drop) < 0.02


def test_permutation_importance_rejects_an_unknown_metric():
    mx = synthetic_matrix(n=300, seed=34)
    m = train_model("logistic", mx.X, mx.y, feature_names=mx.feature_names)
    with pytest.raises(ValueError, match="unknown metric"):
        permutation_importance(m, mx.X, mx.y, metric="sharpe")


def test_audit_calls_one_feature_domination_a_leak_before_it_calls_it_an_edge():
    from app.predict.models import PermutationImportance

    items = [
        PermutationImportance("suspicious_col", 0.30, 0.01, 1),
        PermutationImportance("b", 0.02, 0.01, 2),
        PermutationImportance("c", 0.01, 0.01, 3),
    ]
    audit = audit_importances(items)
    assert audit.dominated is True
    assert audit.top_feature == "suspicious_col"
    assert audit.top_share > 0.6
    joined = " ".join(audit.warnings)
    assert "leak" in joined and "edge" in joined


def test_audit_names_useless_and_unstable_features():
    from app.predict.models import PermutationImportance

    items = [
        PermutationImportance("real", 0.10, 0.005, 1),
        PermutationImportance("real2", 0.09, 0.004, 2),
        PermutationImportance("jittery", 0.01, 0.05, 3),
        PermutationImportance("inert", 0.0, 0.0, 4),
    ]
    audit = audit_importances(items)
    assert "inert" in audit.useless
    assert "jittery" in audit.unstable
    assert audit.dominated is False


def test_audit_says_so_when_the_model_uses_none_of_its_inputs():
    from app.predict.models import PermutationImportance

    audit = audit_importances([PermutationImportance("a", 0.0, 0.0, 1)])
    assert any("not using its inputs" in w for w in audit.warnings)


def test_format_importances_surfaces_the_warnings():
    from app.predict.models import PermutationImportance

    text = format_importances(
        [
            PermutationImportance("dominant", 0.4, 0.01, 1),
            PermutationImportance("minor", 0.01, 0.005, 2),
        ],
        model="random_forest",
    )
    assert "!!" in text
    assert "dominant" in text


def test_evaluate_model_scores_held_out_rows():
    mx = synthetic_matrix(n=2000, seed=35, edge=1.4)
    split = make_split(mx)
    train, validate, _ = split_matrix(mx, split)
    m = train_model("logistic", train.X, train.y, feature_names=train.feature_names)
    oos = evaluate_model(m, validate.X, validate.y)
    assert isinstance(oos, Metrics)
    assert oos.n == len(validate)
    assert 0.0 <= oos.roc_auc <= 1.0
