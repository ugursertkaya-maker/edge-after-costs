"""The five benchmarked models — simplest first, and complexity must earn its place.

Phase 3 section 17b is the reason this module is written the way it is.  An ML
filter selects a subset of existing signals, so it only helps if a profitable
subset exists; run carelessly on a population with a pooled profit factor of
0.22, it produces a model that fits 2026's noise and looks *better* the more
thoroughly it has overfitted.  Every design choice below is aimed at making
that outcome visible rather than flattering:

**The baseline is a named participant, not a footnote.**  ``logistic`` — an
unregularised linear model on standardised features — is fitted first, and
every other model is scored *against it*.  :attr:`ModelReport.verdict` says
``NOT_WORTH_ITS_COMPLEXITY`` in those words for anything that fails to beat it
out of sample, and :func:`select_champion` will return the baseline rather than
a gradient-boosted machine that merely tied.  A complicated model that does not
beat a simple one is not a discovery, it is a maintenance liability.

**Accuracy is computed and then explicitly ignored.**  At a 90/10 base rate a
model that always predicts "no trade" scores 90% accurate and is worth nothing.
Selection runs on PR-AUC first (the metric that moves when the minority class is
actually found), ROC-AUC second, Brier as a tie-break.  Imbalance is handled by
**class weighting** rather than resampling — ``class_weight="balanced"`` for the
three estimators that accept it and the equivalent ``sample_weight`` for
``gbm``.  Resampling was rejected deliberately: over-sampling duplicates rows
whose label windows already overlap, which corrupts the purge that
``splits.py`` exists to enforce, and under-sampling throws away majority rows
that are the only evidence of what a losing setup looks like.  Weighting keeps
every row, keeps the chronology intact, and only changes the loss.

**Class weighting is honest about what it costs.**  ``class_weight="balanced"``
buys recall on the minority class by telling the loss that a rare positive is
worth ~9 of a common negative — and the direct consequence is that the raw
score is no longer a probability.  At a 10% base rate a balanced model's mean
prediction is ~0.50, not ~0.10, and its reliability curve is 40 percentage
points off the diagonal.  That is not a bug and it is not hidden: the raw score
is used for *ranking* (ROC-AUC, PR-AUC) and ``calibrate.py`` fits the mapping
back to real frequencies on validation data.  An EV gate fed the uncalibrated
score of a balanced model would over-bet every single trade.

**The scaler is fitted on the training slice alone.**  ``matrix.py`` emits
unscaled features on purpose, so that the decision about what the test
distribution looks like is made here, once, and visibly.  Mean and scale ride
on :class:`TrainedModel` and are exported into the artifact, so training and
production see identical numbers.  Trees are scaled too — they do not need it,
but one code path that always standardises is one fewer way for the exported
numpy predictor to disagree with sklearn.

**Ablation and permutation importance are run to find leaks, not to rank
features.**  If removing one feature group collapses AUC from 0.80 to 0.51, or
one column carries 70% of the permutation drop, the first hypothesis is a
leak — a column that has seen the future — not an edge.  :func:`audit_importances`
and :func:`ablate_feature_groups` say so in words.

scikit-learn is a *research* dependency and is imported lazily, so
``import app.predict.models`` works in an image that does not ship it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from app.predict.calibrate import (
    average_precision,
    brier_score,
    log_loss,
    precision_recall,
    roc_auc,
)

__all__ = [
    "BASELINE_NAME",
    "MODEL_NAMES",
    "TREE_MODELS",
    "AblationReport",
    "ImportanceAudit",
    "MatrixSlice",
    "Metrics",
    "ModelReport",
    "ModelTrainingError",
    "PermutationImportance",
    "TrainedModel",
    "ablate_feature_groups",
    "audit_importances",
    "benchmark",
    "drop_features",
    "evaluate_model",
    "feature_groups",
    "format_ablation",
    "format_benchmark",
    "format_importances",
    "permutation_importance",
    "select_champion",
    "split_matrix",
    "train_model",
]

#: The five benchmarked models, in increasing order of how much they can overfit.
MODEL_NAMES = ("logistic", "logistic_l2", "gbm", "random_forest", "hist_gbm")

#: The model every other model has to beat.
BASELINE_NAME = "logistic"

#: The models whose importances come from permuting rather than from coefficients.
TREE_MODELS = frozenset({"gbm", "random_forest", "hist_gbm"})

#: PR-AUC improvement a complicated model must show over the baseline to survive.
DEFAULT_COMPLEXITY_MARGIN = 0.005

#: A single feature carrying more than this share of total importance is a leak
#: suspect, not a finding.
DOMINANCE_SHARE = 0.60

#: The fallback grouping when ``matrix.FEATURE_GROUPS`` is not importable yet.
_FALLBACK_GROUP_NAMES = (
    "momentum",
    "volatility",
    "volume",
    "book",
    "regime",
    "cost",
    "time",
    "score",
)


class ModelTrainingError(RuntimeError):
    """Raised when a model cannot be fitted on the data it was handed."""


# --------------------------------------------------------------------------- #
# Slices
# --------------------------------------------------------------------------- #


@dataclass
class MatrixSlice:
    """A duck-typed subset of a ``FeatureMatrix``.

    ``models.py`` deliberately does not depend on ``matrix.FeatureMatrix``: it
    reads ``X``, ``y``, ``ts_ms``, ``feature_names`` and ``label_end_ms`` off
    whatever it is given and returns this instead.  That keeps the benchmark
    runnable against a synthetic matrix and keeps column-dropping (which
    ablation needs) out of the feature owner's class.
    """

    X: "np.ndarray"
    y: "np.ndarray"
    ts_ms: "np.ndarray"
    feature_names: list[str]
    label_end_ms: "np.ndarray"
    symbols: list[str] = field(default_factory=list)
    name: str = ""

    def __len__(self) -> int:
        return int(self.X.shape[0])

    @property
    def base_rate(self) -> float:
        return float(np.mean(self.y)) if len(self) else float("nan")

    def take(self, mask: "np.ndarray", *, name: str = "") -> "MatrixSlice":
        mask = np.asarray(mask)
        syms: list[str] = []
        if self.symbols and len(self.symbols) == len(mask):
            syms = [s for s, keep in zip(self.symbols, mask.tolist()) if keep]
        return MatrixSlice(
            X=self.X[mask],
            y=self.y[mask],
            ts_ms=self.ts_ms[mask],
            feature_names=list(self.feature_names),
            label_end_ms=self.label_end_ms[mask],
            symbols=syms,
            name=name or self.name,
        )


def _as_slice(matrix: Any, *, name: str = "") -> MatrixSlice:
    if isinstance(matrix, MatrixSlice):
        return matrix
    X = np.asarray(matrix.X, dtype=float)
    n = X.shape[0]
    ts = np.asarray(getattr(matrix, "ts_ms", np.arange(n)), dtype=np.int64)
    end = getattr(matrix, "label_end_ms", None)
    end = np.asarray(end if end is not None else ts, dtype=np.int64)
    return MatrixSlice(
        X=X,
        y=np.asarray(matrix.y).reshape(-1).astype(int),
        ts_ms=ts,
        feature_names=list(matrix.feature_names),
        label_end_ms=end,
        symbols=list(getattr(matrix, "symbols", []) or []),
        name=name,
    )


def split_matrix(
    matrix: Any, split: Any, *, embargo_minutes: int = 0
) -> tuple[MatrixSlice, MatrixSlice, MatrixSlice]:
    """Cut a matrix into train / validate / test with the purge applied.

    A training row is dropped when its **label window** reaches into the next
    window, because its outcome is partly determined by bars the model is about
    to be scored on.  ``splits.py`` does the same thing for label lists; this
    does it on the matrix, using ``label_end_ms``, because that is the column
    that makes it checkable.  The purge is applied forward only — train is
    purged against validate, validate against test — and the embargo pushes the
    cut earlier still.

    Windows are half-open ``[lo, hi)``.
    """
    src = _as_slice(matrix)
    embargo_ms = int(embargo_minutes) * 60_000

    def window(bounds) -> tuple[int, int]:
        lo, hi = bounds
        return int(lo), int(hi)

    tr_lo, tr_hi = window(split.train)
    va_lo, va_hi = window(split.validate)
    te_lo, te_hi = window(split.test)

    in_tr = (src.ts_ms >= tr_lo) & (src.ts_ms < tr_hi)
    in_va = (src.ts_ms >= va_lo) & (src.ts_ms < va_hi)
    in_te = (src.ts_ms >= te_lo) & (src.ts_ms < te_hi)

    # Purge: a row survives only if its label closed before the next window
    # opens (less the embargo).
    in_tr &= src.label_end_ms <= (va_lo - embargo_ms)
    in_va &= src.label_end_ms <= (te_lo - embargo_ms)

    return (
        src.take(in_tr, name="train"),
        src.take(in_va, name="validate"),
        src.take(in_te, name="test"),
    )


def drop_features(sl: MatrixSlice, names: Iterable[str]) -> MatrixSlice:
    """Return the slice without the named columns (ablation's only primitive)."""
    drop = set(names)
    keep = [i for i, n in enumerate(sl.feature_names) if n not in drop]
    if not keep:
        raise ValueError("cannot ablate every feature at once")
    return MatrixSlice(
        X=sl.X[:, keep],
        y=sl.y,
        ts_ms=sl.ts_ms,
        feature_names=[sl.feature_names[i] for i in keep],
        label_end_ms=sl.label_end_ms,
        symbols=list(sl.symbols),
        name=sl.name,
    )


def feature_groups(feature_names: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """``FEATURE_GROUPS`` from ``matrix.py``, restricted to columns we actually have.

    Falls back to prefix matching on the documented group names when
    ``matrix.py`` is not importable — the feature owner is writing it in
    parallel, and the benchmark has to be developable before it lands.  Any
    column matched by nothing is reported under ``"ungrouped"`` rather than
    dropped, because a feature nobody can name is itself worth seeing.
    """
    names = list(feature_names)
    table: Mapping[str, Sequence[str]] | None = None
    try:  # pragma: no cover - depends on a module owned by another agent
        from app.predict.matrix import FEATURE_GROUPS as _FG

        table = _FG
    except Exception:
        table = None

    out: dict[str, tuple[str, ...]] = {}
    claimed: set[str] = set()
    if table:
        for group, members in table.items():
            present = tuple(n for n in names if n in set(members))
            if present:
                out[group] = present
                claimed.update(present)
    else:
        for group in _FALLBACK_GROUP_NAMES:
            present = tuple(n for n in names if n == group or n.startswith(group + "_"))
            if present:
                out[group] = present
                claimed.update(present)

    leftover = tuple(n for n in names if n not in claimed)
    if leftover:
        out["ungrouped"] = leftover
    return out


# --------------------------------------------------------------------------- #
# Training
# --------------------------------------------------------------------------- #


def _fit_scaler(X: "np.ndarray") -> tuple["np.ndarray", "np.ndarray"]:
    """Mean/scale from the **training rows only**.

    A zero-variance column gets ``scale=1`` rather than ``scale=0``: dividing by
    its (zero) spread would produce NaNs, and a constant column carries no
    information anyway, so it is left as a constant offset the model can absorb.
    """
    mean = X.mean(axis=0)
    scale = X.std(axis=0)
    scale = np.where(scale < 1e-12, 1.0, scale)
    return mean.astype(float), scale.astype(float)


@dataclass
class TrainedModel:
    """A fitted estimator plus the scaler that was fitted with it.

    The scaler travels with the model because the alternative — refitting it
    wherever the model is used — is how the test distribution leaks into
    training. ``predict_proba`` returns sklearn's ``(n, 2)`` layout so that the
    exported artifact can be compared against it column for column.
    """

    name: str
    estimator: object
    feature_names: list[str]
    scaler_mean: "np.ndarray | None"
    scaler_scale: "np.ndarray | None"
    class_weight: str = "balanced"
    imbalance_method: str = "class_weight"
    seed: int = 7
    n_train: int = 0
    train_base_rate: float = float("nan")
    params: dict = field(default_factory=dict)

    def transform(self, X) -> "np.ndarray":
        X = np.asarray(X, dtype=float)
        if X.ndim == 1:
            X = X.reshape(1, -1)
        if X.shape[1] != len(self.feature_names):
            raise ValueError(
                f"{self.name}: expected {len(self.feature_names)} features, got {X.shape[1]}"
            )
        if self.scaler_mean is None or self.scaler_scale is None:
            return X
        return (X - self.scaler_mean) / self.scaler_scale

    def predict_proba(self, X) -> "np.ndarray":
        return np.asarray(self.estimator.predict_proba(self.transform(X)), dtype=float)

    def predict_p_win(self, X) -> "np.ndarray":
        """Column 1 — the probability of the positive (TP-before-SL) class."""
        return self.predict_proba(X)[:, 1]


def _balanced_sample_weight(y: "np.ndarray") -> "np.ndarray":
    """sklearn's ``class_weight="balanced"``, as a per-row weight.

    ``n_samples / (n_classes · bincount(y))`` — which is what lets ``gbm``,
    the one estimator without a ``class_weight`` argument, be trained under
    exactly the same imbalance policy as the other four.
    """
    y = np.asarray(y).reshape(-1).astype(int)
    classes, counts = np.unique(y, return_counts=True)
    w = {int(c): y.size / (classes.size * cnt) for c, cnt in zip(classes, counts)}
    return np.array([w[int(v)] for v in y], dtype=float)


def train_model(name: str, Xtr, ytr, *, seed: int = 7, **kw) -> TrainedModel:
    """Fit one of :data:`MODEL_NAMES` on the training slice.

    Extra keyword arguments are forwarded to the estimator, so a caller can
    sweep depth or regularisation without this function growing a parameter per
    hyper-parameter.
    """
    if name not in MODEL_NAMES:
        raise ValueError(f"unknown model {name!r}; expected one of {MODEL_NAMES}")

    X = np.asarray(Xtr, dtype=float)
    y = np.asarray(ytr).reshape(-1).astype(int)
    if X.ndim != 2:
        raise ValueError("Xtr must be 2-dimensional")
    if X.shape[0] != y.size:
        raise ValueError(f"X has {X.shape[0]} rows but y has {y.size}")
    if X.shape[0] == 0:
        raise ModelTrainingError(f"{name}: cannot train on an empty slice")
    classes = np.unique(y)
    if classes.size < 2:
        raise ModelTrainingError(
            f"{name}: training slice has a single class ({classes.tolist()}); "
            "there is nothing to separate"
        )
    if not np.all(np.isfinite(X)):
        raise ModelTrainingError(f"{name}: training features contain NaN or inf")

    feature_names = list(kw.pop("feature_names", [f"f{i}" for i in range(X.shape[1])]))
    mean, scale = _fit_scaler(X)
    Xs = (X - mean) / scale

    try:  # pragma: no cover - exercised whenever sklearn is installed
        from sklearn.ensemble import (
            GradientBoostingClassifier,
            HistGradientBoostingClassifier,
            RandomForestClassifier,
        )
        from sklearn.linear_model import LogisticRegression
    except ImportError as exc:  # pragma: no cover - production image has no sklearn
        raise ModelTrainingError(
            "scikit-learn is a research dependency and is not installed. "
            "Training happens on the laptop; production runs the exported "
            "numpy-only artifact instead."
        ) from exc

    sample_weight = None
    imbalance = "class_weight=balanced"

    if name == "logistic":
        params = {"penalty": None, "max_iter": 2000, "class_weight": "balanced"}
        params.update(kw)
        est = LogisticRegression(**params)
    elif name == "logistic_l2":
        params = {
            "penalty": "l2",
            "C": 1.0,
            "max_iter": 2000,
            "class_weight": "balanced",
            "solver": "lbfgs",
        }
        params.update(kw)
        est = LogisticRegression(**params)
    elif name == "gbm":
        params = {
            "n_estimators": 200,
            "max_depth": 3,
            "learning_rate": 0.05,
            "subsample": 1.0,
            "random_state": seed,
        }
        params.update(kw)
        est = GradientBoostingClassifier(**params)
        sample_weight = _balanced_sample_weight(y)
        imbalance = "sample_weight=balanced (GradientBoosting has no class_weight)"
    elif name == "random_forest":
        params = {
            "n_estimators": 300,
            "max_depth": 6,
            "min_samples_leaf": 20,
            "class_weight": "balanced_subsample",
            "random_state": seed,
            "n_jobs": 1,
        }
        params.update(kw)
        est = RandomForestClassifier(**params)
        imbalance = "class_weight=balanced_subsample"
    else:  # hist_gbm
        params = {
            "max_iter": 200,
            "max_depth": 4,
            "learning_rate": 0.05,
            "min_samples_leaf": 20,
            "early_stopping": False,
            "class_weight": "balanced",
            "random_state": seed,
        }
        params.update(kw)
        est = HistGradientBoostingClassifier(**params)

    if sample_weight is not None:
        est.fit(Xs, y, sample_weight=sample_weight)
    else:
        est.fit(Xs, y)

    return TrainedModel(
        name=name,
        estimator=est,
        feature_names=feature_names,
        scaler_mean=mean,
        scaler_scale=scale,
        class_weight="balanced",
        imbalance_method=imbalance,
        seed=int(seed),
        n_train=int(X.shape[0]),
        train_base_rate=float(y.mean()),
        params={k: v for k, v in params.items()},
    )


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Metrics:
    """Out-of-sample scores for one model on one slice.

    ``accuracy`` is present so the report can show *why* it is not being used:
    at a 10% base rate the always-no model reads 0.90 here and ``nan``
    precision, which is the whole argument in two numbers.
    """

    n: int
    base_rate: float
    precision: float
    recall: float
    roc_auc: float
    pr_auc: float
    brier: float
    log_loss: float
    accuracy: float
    positive_rate: float
    threshold: float = 0.5

    def as_dict(self) -> dict:
        return {
            "n": self.n,
            "base_rate": self.base_rate,
            "precision": self.precision,
            "recall": self.recall,
            "roc_auc": self.roc_auc,
            "pr_auc": self.pr_auc,
            "brier": self.brier,
            "log_loss": self.log_loss,
            "accuracy": self.accuracy,
            "positive_rate": self.positive_rate,
        }


def evaluate_model(model: TrainedModel, X, y, *, threshold: float = 0.5) -> Metrics:
    """Score a fitted model on rows it was not trained on."""
    y = np.asarray(y).reshape(-1).astype(int)
    p = model.predict_p_win(X)
    return score_probabilities(p, y, threshold=threshold)


def score_probabilities(p, y, *, threshold: float = 0.5) -> Metrics:
    """The metric block, computed from probabilities alone."""
    p = np.asarray(p, dtype=float).reshape(-1)
    y = np.asarray(y).reshape(-1).astype(int)
    precision, recall, n_fired = precision_recall(y, p, threshold=threshold)
    pred = (p >= threshold).astype(int)
    return Metrics(
        n=int(y.size),
        base_rate=float(y.mean()) if y.size else float("nan"),
        precision=float(precision),
        recall=float(recall),
        roc_auc=float(roc_auc(y, p)),
        pr_auc=float(average_precision(y, p)),
        brier=float(brier_score(y, p)),
        log_loss=float(log_loss(y, p)),
        accuracy=float(np.mean(pred == y)) if y.size else float("nan"),
        positive_rate=float(n_fired / y.size) if y.size else float("nan"),
        threshold=float(threshold),
    )


# --------------------------------------------------------------------------- #
# The benchmark
# --------------------------------------------------------------------------- #


@dataclass
class ModelReport:
    """One model, scored out of sample and judged against the baseline."""

    name: str
    model: TrainedModel | None
    train: Metrics | None
    oos: Metrics | None
    eval_on: str
    is_baseline: bool
    beats_baseline: bool
    verdict: str
    delta_pr_auc: float
    delta_roc_auc: float
    delta_brier: float
    notes: tuple[str, ...] = ()
    error: str | None = None
    imbalance_method: str = ""

    @property
    def survives(self) -> bool:
        """True when this model has earned the right to be considered.

        The baseline always survives — it is the thing complexity is measured
        against.  Everything else has to beat it.
        """
        return self.error is None and (self.is_baseline or self.beats_baseline)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "verdict": self.verdict,
            "beats_baseline": self.beats_baseline,
            "eval_on": self.eval_on,
            "delta_pr_auc": self.delta_pr_auc,
            "delta_roc_auc": self.delta_roc_auc,
            "delta_brier": self.delta_brier,
            "oos": self.oos.as_dict() if self.oos else None,
            "train": self.train.as_dict() if self.train else None,
            "imbalance_method": self.imbalance_method,
            "notes": list(self.notes),
            "error": self.error,
        }


def _judge(
    name: str,
    oos: Metrics | None,
    baseline: Metrics | None,
    *,
    margin: float,
) -> tuple[bool, str, tuple[str, ...]]:
    """The complexity rule, applied arithmetically instead of editorially.

    Primary metric is PR-AUC, because under a 90/10 base rate that is the one
    that moves when the minority class is genuinely being found.  ROC-AUC acts
    as a veto: a model that buys PR-AUC by destroying ranking everywhere else
    has not improved anything.
    """
    notes: list[str] = []
    if oos is None:
        return False, "FAILED", ("model could not be trained or scored",)
    if name == BASELINE_NAME:
        if not np.isfinite(oos.roc_auc):
            return True, "BASELINE_DEGENERATE", ("single-class evaluation slice",)
        return True, "BASELINE", ()
    if baseline is None or not np.isfinite(baseline.pr_auc):
        return False, "NO_BASELINE", ("baseline did not train; nothing to beat",)

    d_pr = oos.pr_auc - baseline.pr_auc
    d_roc = oos.roc_auc - baseline.roc_auc
    if not np.isfinite(d_pr) or not np.isfinite(d_roc):
        return False, "FAILED", ("metrics are undefined on this slice",)

    if oos.positive_rate in (0.0, 1.0):
        notes.append(
            "predicts a single class at the 0.5 threshold — degenerate, "
            "not a filter"
        )
        return False, "DEGENERATE", tuple(notes)

    if d_pr < margin:
        notes.append(
            f"PR-AUC {oos.pr_auc:.4f} vs baseline {baseline.pr_auc:.4f} "
            f"({d_pr:+.4f}, needs >= {margin:+.4f})"
        )
        return False, "NOT_WORTH_ITS_COMPLEXITY", tuple(notes)
    if d_roc < 0.0:
        notes.append(
            f"PR-AUC improved ({d_pr:+.4f}) but ROC-AUC fell ({d_roc:+.4f}); "
            "ranking got worse overall"
        )
        return False, "NOT_WORTH_ITS_COMPLEXITY", tuple(notes)

    notes.append(f"PR-AUC {d_pr:+.4f} and ROC-AUC {d_roc:+.4f} over the baseline")
    return True, "BEATS_BASELINE", tuple(notes)


def benchmark(
    matrix: Any,
    split: Any,
    *,
    names: Sequence[str] = MODEL_NAMES,
    seed: int = 7,
    eval_on: str = "validate",
    embargo_minutes: int = 0,
    margin: float = DEFAULT_COMPLEXITY_MARGIN,
    threshold: float = 0.5,
    model_kwargs: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[ModelReport]:
    """Train every named model on ``split.train`` and score it out of sample.

    ``eval_on`` defaults to ``"validate"`` and should stay there.  Model
    selection, calibration and thresholding all happen on train/validate; the
    test window is touched **once**, after everything is frozen.  A benchmark
    that reads the test slice on every iteration has turned it into a
    validation set, and the number it prints at the end is no longer out of
    sample.

    The baseline is always trained, even when it was not asked for, because
    every verdict in the returned list is relative to it.

    ``model_kwargs`` is keyed by model name — ``{"gbm": {"n_estimators": 50}}`` —
    because the five estimators do not share a hyper-parameter vocabulary and a
    single ``**kw`` would hand ``n_estimators`` to a model that calls it
    ``max_iter``.
    """
    per_model = {k: dict(v) for k, v in (model_kwargs or {}).items()}
    if eval_on not in ("validate", "test"):
        raise ValueError("eval_on must be 'validate' or 'test'")

    train, validate, test = split_matrix(matrix, split, embargo_minutes=embargo_minutes)
    held = validate if eval_on == "validate" else test
    if len(train) == 0:
        raise ModelTrainingError(
            "training slice is empty after purging — check the split bounds "
            "and the label horizon"
        )
    if len(held) == 0:
        raise ModelTrainingError(f"{eval_on} slice is empty after purging")

    requested = list(dict.fromkeys(names))
    order = [BASELINE_NAME] + [n for n in requested if n != BASELINE_NAME]

    fitted: dict[str, tuple[TrainedModel | None, Metrics | None, Metrics | None, str | None]] = {}
    for name in order:
        try:
            model = train_model(
                name,
                train.X,
                train.y,
                seed=seed,
                feature_names=train.feature_names,
                **per_model.get(name, {}),
            )
            fitted[name] = (
                model,
                evaluate_model(model, train.X, train.y, threshold=threshold),
                evaluate_model(model, held.X, held.y, threshold=threshold),
                None,
            )
        except Exception as exc:  # noqa: BLE001 - a failed model is a result
            fitted[name] = (None, None, None, f"{type(exc).__name__}: {exc}")

    baseline_oos = fitted[BASELINE_NAME][2]
    reports: list[ModelReport] = []
    for name in order:
        # The baseline is reported even when it was not requested: a benchmark
        # that hides its own yardstick is not a benchmark.
        model, train_m, oos, err = fitted[name]
        beats, verdict, notes = _judge(name, oos, baseline_oos, margin=margin)
        if err:
            beats, verdict, notes = False, "FAILED", (err,)
        d_pr = d_roc = d_brier = float("nan")
        if oos is not None and baseline_oos is not None:
            d_pr = oos.pr_auc - baseline_oos.pr_auc
            d_roc = oos.roc_auc - baseline_oos.roc_auc
            d_brier = oos.brier - baseline_oos.brier
        extra = list(notes)
        if train_m is not None and oos is not None and np.isfinite(oos.roc_auc):
            gap = train_m.roc_auc - oos.roc_auc
            if gap > 0.15:
                extra.append(
                    f"in-sample ROC-AUC {train_m.roc_auc:.3f} vs out-of-sample "
                    f"{oos.roc_auc:.3f} ({gap:+.3f}) — memorising, not learning"
                )
        reports.append(
            ModelReport(
                name=name,
                model=model,
                train=train_m,
                oos=oos,
                eval_on=eval_on,
                is_baseline=(name == BASELINE_NAME),
                beats_baseline=beats and name != BASELINE_NAME,
                verdict=verdict,
                delta_pr_auc=float(d_pr),
                delta_roc_auc=float(d_roc),
                delta_brier=float(d_brier),
                notes=tuple(extra),
                error=err,
                imbalance_method=model.imbalance_method if model else "",
            )
        )
    return reports


def select_champion(
    reports: Sequence[ModelReport],
) -> tuple[ModelReport | None, str]:
    """Pick the model to export, and say why in one sentence.

    Ties go to the baseline.  When nothing beats it, the baseline *is* the
    champion — the answer "the simple model was as good as the complicated
    ones" is a result, not a failure to find one.
    """
    ok = [r for r in reports if r.error is None and r.oos is not None]
    if not ok:
        return None, "no model trained successfully; there is nothing to promote"
    survivors = [r for r in ok if r.beats_baseline]
    baseline = next((r for r in ok if r.is_baseline), None)
    if not survivors:
        if baseline is None:
            return None, "the baseline failed to train, so no comparison is possible"
        losers = [r.name for r in ok if not r.is_baseline]
        return baseline, (
            f"no model beat the {BASELINE_NAME} baseline out of sample "
            f"({', '.join(losers) or 'none tested'}); "
            "the baseline is the champion because the extra complexity bought nothing"
        )
    best = max(survivors, key=lambda r: (r.oos.pr_auc, r.oos.roc_auc, -r.oos.brier))
    return best, (
        f"{best.name} beat the baseline on PR-AUC by {best.delta_pr_auc:+.4f} "
        f"and on ROC-AUC by {best.delta_roc_auc:+.4f} out of sample"
    )


def format_benchmark(reports: Sequence[ModelReport]) -> str:
    """The comparison table, with the complexity verdict spelled out per row."""
    if not reports:
        return "no models benchmarked"
    eval_on = reports[0].eval_on
    lines = [
        f"model benchmark — scored on the {eval_on} slice "
        f"(baseline = {BASELINE_NAME}; accuracy shown but NOT used for selection)",
        f"{'model':<15}{'n':>7}{'prec':>8}{'recall':>8}{'roc':>8}{'pr-auc':>9}"
        f"{'brier':>9}{'acc':>8}{'dPR':>9}  verdict",
    ]
    for r in reports:
        if r.oos is None:
            lines.append(f"{r.name:<15}{'—':>7}{'':>49}  FAILED: {r.error}")
            continue
        m = r.oos
        lines.append(
            f"{r.name:<15}{m.n:>7}{m.precision:>8.3f}{m.recall:>8.3f}"
            f"{m.roc_auc:>8.4f}{m.pr_auc:>9.4f}{m.brier:>9.5f}{m.accuracy:>8.3f}"
            f"{r.delta_pr_auc:>+9.4f}  {r.verdict}"
        )
        for note in r.notes:
            lines.append(f"{'':<15}  · {note}")
    champ, why = select_champion(reports)
    lines.append(f"champion: {champ.name if champ else 'none'} — {why}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Ablation (item 23)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AblationReport:
    """What happens out of sample when one feature group is removed."""

    group: str
    n_features_removed: int
    features: tuple[str, ...]
    roc_auc: float
    pr_auc: float
    brier: float
    delta_roc_auc: float
    delta_pr_auc: float
    verdict: str
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "group": self.group,
            "n_features_removed": self.n_features_removed,
            "roc_auc": self.roc_auc,
            "pr_auc": self.pr_auc,
            "delta_roc_auc": self.delta_roc_auc,
            "delta_pr_auc": self.delta_pr_auc,
            "verdict": self.verdict,
            "note": self.note,
        }


def ablate_feature_groups(
    matrix: Any,
    split: Any,
    *,
    name: str = BASELINE_NAME,
    seed: int = 7,
    eval_on: str = "validate",
    embargo_minutes: int = 0,
    groups: Mapping[str, Sequence[str]] | None = None,
    useless_tol: float = 0.002,
    dominance_drop: float = 0.15,
    **kw,
) -> list[AblationReport]:
    """Remove one ``FEATURE_GROUPS`` group at a time, refit, and report the change.

    Read the signs carefully: ``delta`` is *ablated minus full*, so a **negative**
    delta means removing the group made things worse, i.e. the group was doing
    work.  A positive delta means the model is better without it.

    Three verdicts come out of this:

    ``USEFUL``
        Removing it costs real out-of-sample performance.
    ``USELESS``
        Removing it changes nothing beyond ``useless_tol``.  Carrying a feature
        that does nothing is a cost with no benefit — it still has to be
        computed live, and it still has a way to go wrong.
    ``HARMFUL``
        The model is *better* without it.
    ``SUSPICIOUS_DOMINANCE``
        Removing this one group collapses the model.  A single group that holds
        up the entire result is the signature of leakage far more often than
        the signature of an edge, and it should be audited before it is trusted.
    """
    src = _as_slice(matrix)
    table = dict(groups) if groups else feature_groups(src.feature_names)
    if not table:
        return []

    train, validate, test = split_matrix(src, split, embargo_minutes=embargo_minutes)
    held = validate if eval_on == "validate" else test

    full = train_model(
        name, train.X, train.y, seed=seed, feature_names=train.feature_names, **kw
    )
    base = evaluate_model(full, held.X, held.y)

    out: list[AblationReport] = []
    for group, members in sorted(table.items()):
        members = tuple(members)
        if len(members) >= len(src.feature_names):
            out.append(
                AblationReport(
                    group=group,
                    n_features_removed=len(members),
                    features=members,
                    roc_auc=float("nan"),
                    pr_auc=float("nan"),
                    brier=float("nan"),
                    delta_roc_auc=float("nan"),
                    delta_pr_auc=float("nan"),
                    verdict="NOT_ABLATABLE",
                    note="this group is every feature there is; removing it "
                    "leaves no model to score",
                )
            )
            continue
        tr = drop_features(train, members)
        ho = drop_features(held, members)
        try:
            m = train_model(
                name, tr.X, tr.y, seed=seed, feature_names=tr.feature_names, **kw
            )
            metrics = evaluate_model(m, ho.X, ho.y)
        except Exception as exc:  # noqa: BLE001
            out.append(
                AblationReport(
                    group=group,
                    n_features_removed=len(members),
                    features=members,
                    roc_auc=float("nan"),
                    pr_auc=float("nan"),
                    brier=float("nan"),
                    delta_roc_auc=float("nan"),
                    delta_pr_auc=float("nan"),
                    verdict="FAILED",
                    note=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        d_roc = metrics.roc_auc - base.roc_auc
        d_pr = metrics.pr_auc - base.pr_auc
        note = ""
        if not np.isfinite(d_roc):
            verdict = "UNDEFINED"
        elif d_roc <= -dominance_drop:
            verdict = "SUSPICIOUS_DOMINANCE"
            note = (
                f"removing {group} costs {abs(d_roc):.3f} ROC-AUC on its own — one "
                "group holding up the whole model is more likely a leak than an "
                "edge; check that nothing in it can see past the entry bar"
            )
        elif d_roc < -useless_tol:
            verdict = "USEFUL"
            note = f"carries {abs(d_roc):.4f} ROC-AUC that nothing else replaces"
        elif d_roc > useless_tol:
            verdict = "HARMFUL"
            note = (
                f"the model is {d_roc:.4f} ROC-AUC better without it — it is "
                "adding noise, not information"
            )
        else:
            verdict = "USELESS"
            note = (
                f"removing it moves ROC-AUC by {d_roc:+.4f}; it is being computed "
                "live for nothing"
            )
        out.append(
            AblationReport(
                group=group,
                n_features_removed=len(members),
                features=members,
                roc_auc=metrics.roc_auc,
                pr_auc=metrics.pr_auc,
                brier=metrics.brier,
                delta_roc_auc=float(d_roc),
                delta_pr_auc=float(d_pr),
                verdict=verdict,
                note=note,
            )
        )
    return out


def format_ablation(reports: Sequence[AblationReport], *, model: str = "") -> str:
    if not reports:
        return "no feature groups to ablate"
    lines = [
        f"feature-group ablation{f' [{model}]' if model else ''} — "
        "delta is (ablated − full), so negative means the group was doing work",
        f"{'group':<14}{'n':>4}{'roc':>9}{'dROC':>9}{'dPR':>9}  verdict",
    ]
    for r in reports:
        lines.append(
            f"{r.group:<14}{r.n_features_removed:>4}{r.roc_auc:>9.4f}"
            f"{r.delta_roc_auc:>+9.4f}{r.delta_pr_auc:>+9.4f}  {r.verdict}"
        )
        if r.note:
            lines.append(f"{'':<14}  · {r.note}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Permutation importance (item 24)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PermutationImportance:
    """How much out-of-sample score is lost when one column is shuffled."""

    feature: str
    mean_drop: float
    std_drop: float
    rank: int
    metric: str = "roc_auc"

    def as_dict(self) -> dict:
        return {
            "feature": self.feature,
            "mean_drop": self.mean_drop,
            "std_drop": self.std_drop,
            "rank": self.rank,
            "metric": self.metric,
        }


def permutation_importance(
    model: TrainedModel,
    X,
    y,
    *,
    n_repeats: int = 5,
    seed: int = 7,
    metric: str = "roc_auc",
    feature_names: Sequence[str] | None = None,
) -> list[PermutationImportance]:
    """Shuffle each column in turn and measure what the model loses.

    Run on **held-out** rows.  Permutation importance on training data measures
    what the model memorised, which is a different and much less interesting
    question than what it depends on.
    """
    scorers = {
        "roc_auc": roc_auc,
        "pr_auc": average_precision,
        "brier": lambda yy, pp: -brier_score(yy, pp),  # negated: higher is better
    }
    if metric not in scorers:
        raise ValueError(f"unknown metric {metric!r}; use {sorted(scorers)}")
    score = scorers[metric]

    X = np.asarray(X, dtype=float)
    y = np.asarray(y).reshape(-1).astype(int)
    names = list(feature_names or model.feature_names)
    if len(names) != X.shape[1]:
        names = [f"f{i}" for i in range(X.shape[1])]

    base = score(y, model.predict_p_win(X))
    rng = np.random.default_rng(seed)
    rows: list[tuple[str, float, float]] = []
    for j in range(X.shape[1]):
        drops = []
        col = X[:, j].copy()
        for _ in range(n_repeats):
            Xp = X.copy()
            Xp[:, j] = rng.permutation(col)
            drops.append(base - score(y, model.predict_p_win(Xp)))
        rows.append((names[j], float(np.mean(drops)), float(np.std(drops))))

    rows.sort(key=lambda r: -r[1])
    return [
        PermutationImportance(
            feature=n, mean_drop=m, std_drop=s, rank=i + 1, metric=metric
        )
        for i, (n, m, s) in enumerate(rows)
    ]


@dataclass(frozen=True)
class ImportanceAudit:
    """The three things importances are actually being run to find."""

    top_feature: str
    top_share: float
    dominated: bool
    useless: tuple[str, ...]
    unstable: tuple[str, ...]
    warnings: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "top_feature": self.top_feature,
            "top_share": self.top_share,
            "dominated": self.dominated,
            "useless": list(self.useless),
            "unstable": list(self.unstable),
            "warnings": list(self.warnings),
        }


def audit_importances(
    items: Sequence[PermutationImportance],
    *,
    dominance_share: float = DOMINANCE_SHARE,
    useless_tol: float = 1e-4,
    instability_ratio: float = 1.0,
) -> ImportanceAudit:
    """Turn a ranked importance list into the warnings it exists to produce.

    ``dominated``
        One feature accounts for more than ``dominance_share`` of all the
        importance there is.  **This is a leakage alarm, not a discovery.**  A
        market feature that alone explains a trade's outcome would be an edge
        nobody else had found; a column that accidentally contains information
        from after the entry bar explains it too, and is overwhelmingly the more
        likely of the two.  It gets audited before it gets believed.
    ``useless``
        Shuffling the column costs nothing.  It is being computed live for no
        return.
    ``unstable``
        The spread across repeats is as large as the effect itself, so the
        "importance" is a sampling artefact.
    """
    if not items:
        return ImportanceAudit("", 0.0, False, (), (), ("no importances computed",))

    positives = [max(0.0, i.mean_drop) for i in items]
    total = float(sum(positives))
    top = items[0]
    share = (max(0.0, top.mean_drop) / total) if total > 0 else 0.0

    useless = tuple(i.feature for i in items if abs(i.mean_drop) <= useless_tol)
    unstable = tuple(
        i.feature
        for i in items
        if i.mean_drop > useless_tol and i.std_drop >= instability_ratio * abs(i.mean_drop)
    )

    warnings: list[str] = []
    dominated = bool(total > 0 and share > dominance_share)
    if dominated:
        warnings.append(
            f"{top.feature} carries {share:.0%} of all permutation importance. "
            "A single feature holding up the whole model is far more likely to be "
            "a leak than an edge — verify it cannot see any bar at or after entry "
            "before this model is promoted."
        )
    if useless:
        warnings.append(
            f"{len(useless)} feature(s) cost nothing when shuffled: "
            f"{', '.join(useless[:8])}{' …' if len(useless) > 8 else ''}"
        )
    if unstable:
        warnings.append(
            f"{len(unstable)} feature(s) have an importance smaller than its own "
            f"spread across repeats: {', '.join(unstable[:8])}"
        )
    if total <= 0:
        warnings.append(
            "no feature loses anything when shuffled — the model is not using its "
            "inputs at all"
        )
    return ImportanceAudit(
        top_feature=top.feature,
        top_share=float(share),
        dominated=dominated,
        useless=useless,
        unstable=unstable,
        warnings=tuple(warnings),
    )


def format_importances(
    items: Sequence[PermutationImportance], *, model: str = "", top: int = 15
) -> str:
    if not items:
        return "no permutation importances computed"
    audit = audit_importances(items)
    lines = [
        f"permutation importance{f' [{model}]' if model else ''} "
        f"(metric={items[0].metric}, held-out rows)",
        f"{'rank':>5}  {'feature':<28}{'mean drop':>11}{'std':>10}",
    ]
    for i in items[:top]:
        lines.append(f"{i.rank:>5}  {i.feature:<28}{i.mean_drop:>+11.5f}{i.std_drop:>10.5f}")
    if len(items) > top:
        lines.append(f"        … {len(items) - top} more")
    for w in audit.warnings:
        lines.append(f"  !! {w}")
    return "\n".join(lines)
