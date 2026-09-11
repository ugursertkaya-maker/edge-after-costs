"""The champion artifact — inference with numpy and nothing else.

This is the production constraint from ``docs/PHASE4_SPEC.md``, and it is not a
preference.  Research trains on the laptop with scikit-learn; the worker
container ships ``numpy`` and does not ship ``scikit-learn``, ``scipy``,
``joblib`` or a pickle of a fitted estimator.  So the champion leaves research
as *data*:

* a **linear model** exports its coefficients, its intercept and the scaler
  that was fitted on the training slice;
* a **tree ensemble** exports a flat :class:`TreeEnsembleSpec` — one array each
  of feature index, threshold, left child, right child and leaf value, with the
  trees concatenated and a root offset per tree — evaluated by
  :func:`predict_tree_ensemble`, which is forty lines of numpy;
* an optional **calibrator** exports two floats (Platt) or two arrays
  (isotonic), applied with ``expit`` or ``np.interp``.

Four properties are enforced here rather than hoped for:

**Nothing in the inference path imports sklearn.**  Not lazily, not inside a
function, not via ``joblib``.  ``tests/test_predict_artifact.py`` loads an
artifact in a *fresh interpreter* and asserts ``"sklearn" not in sys.modules``.
The saved format is a plain ``.npz`` — numpy's own container — with a JSON
metadata blob inside it, so ``load`` needs numpy and the standard library.

**The export is verified, never assumed.**  :func:`export` re-scores the rows it
is given through both sklearn and the exported arrays and raises
:class:`ExportError` if they differ by more than :data:`EXPORT_TOLERANCE`
(1e-9).  A model that cannot be exported cannot be a champion, and the way you
find that out is a loud failure at export time — not a quietly different
probability in production six weeks later.

**A feature-name mismatch is fatal.**  ``feature_names`` is stored in order.
:func:`assert_feature_names` raises :class:`FeatureContractError` naming the
columns that were added, removed or moved.  Silently accepting a matrix whose
columns have shifted is the worst available outcome: the model keeps returning
confident probabilities, computed from the wrong numbers, and nothing in the
system notices.

**Save/load round-trips exactly.**  Floats go through ``.npz`` as binary, not as
text, so a reloaded artifact predicts bit-identically to the one that was saved.

``export`` never silently degrades.  There is no "approximate export", no
fallback to a simpler model, no truncation of an ensemble that turned out to be
large.  It either produces an exact predictor or it raises.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from app.predict.calibrate import expit

__all__ = [
    "ARTIFACT_VERSION",
    "EXPORT_TOLERANCE",
    "ExportError",
    "FeatureContractError",
    "PredictiveArtifact",
    "TreeEnsembleSpec",
    "assert_feature_names",
    "export",
    "load",
    "leaf_sum",
    "predict_tree_ensemble",
    "save",
]

#: Bumped whenever the payload layout changes in a way ``load`` must know about.
ARTIFACT_VERSION = "phase4-1"

#: The spec's number: the exported predictor must match sklearn this closely.
EXPORT_TOLERANCE = 1e-9

#: Marks a leaf in the flat ``feature`` array.
LEAF = -1


class ExportError(RuntimeError):
    """Raised when a model cannot be exported to a numpy-only predictor."""


class FeatureContractError(ValueError):
    """Raised when an artifact is used against a matrix it does not fit."""


# --------------------------------------------------------------------------- #
# Tree ensembles
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TreeEnsembleSpec:
    """A whole forest as six flat arrays.

    Every tree is concatenated into the same arrays and ``root[t]`` is where
    tree ``t`` starts, with child indices already rebased into the flat frame.
    That means traversal is pure integer indexing with no Python-level tree
    objects, no recursion and no per-node allocation — which is what makes a
    numpy-only predictor fast enough to run in the hot path.

    ``combine`` says how the per-tree leaf values become a probability:

    ``"mean_proba"``
        Random forest.  Each leaf holds ``P(class 1)`` at that leaf; the
        ensemble averages them.
    ``"sum_logit"``
        Gradient boosting, both flavours.  Each leaf holds a raw-score
        contribution (already multiplied by the learning rate where sklearn
        would apply it); they are summed onto ``base_score`` and squashed.

    ``missing_go_left`` exists because ``HistGradientBoosting`` routes NaN
    deliberately rather than erroring.  For the other estimators it is all
    ``False`` and NaN never reaches here anyway, because :meth:`PredictiveArtifact.predict_proba`
    rejects non-finite input.

    ``input_dtype`` is the detail that decides whether this reproduces sklearn
    to 1e-9 or to 1e-3.  ``sklearn.tree`` casts its input to **float32** before
    it walks a tree, then compares that float32 against a float64 threshold; a
    row sitting within a float32 ulp of a split therefore goes left in sklearn
    and right in a naive float64 re-implementation, and with three hundred trees
    some row always is.  ``"float32"`` reproduces that rounding exactly.
    ``HistGradientBoosting`` predicts in float64 and wants ``"float64"``.  This
    was not a theoretical concern: it was a 2.4e-3 disagreement on a 300-tree
    forest, three orders of magnitude outside tolerance.
    """

    feature: "np.ndarray"      # int32, LEAF for a leaf node
    threshold: "np.ndarray"    # float64
    left: "np.ndarray"         # int32, flat index
    right: "np.ndarray"        # int32, flat index
    value: "np.ndarray"        # float64, meaningful at leaves
    root: "np.ndarray"         # int32, one per tree
    combine: str = "sum_logit"
    base_score: float = 0.0
    missing_go_left: "np.ndarray | None" = None
    input_dtype: str = "float64"

    @property
    def n_trees(self) -> int:
        return int(self.root.size)

    @property
    def n_nodes(self) -> int:
        return int(self.feature.size)

    def validate(self) -> None:
        n = self.n_nodes
        for name in ("threshold", "left", "right", "value"):
            arr = getattr(self, name)
            if arr.size != n:
                raise ExportError(
                    f"TreeEnsembleSpec.{name} has {arr.size} entries but "
                    f"feature has {n}"
                )
        if self.combine not in ("mean_proba", "sum_logit"):
            raise ExportError(f"unknown combine rule {self.combine!r}")
        if self.root.size == 0:
            raise ExportError("TreeEnsembleSpec has no trees")
        internal = self.feature != LEAF
        if internal.any():
            for child in (self.left[internal], self.right[internal]):
                if child.min() < 0 or child.max() >= n:
                    raise ExportError("tree child index points outside the node arrays")

    def to_payload(self, prefix: str = "tree") -> dict:
        out = {
            f"{prefix}_feature": self.feature.astype(np.int32),
            f"{prefix}_threshold": self.threshold.astype(np.float64),
            f"{prefix}_left": self.left.astype(np.int32),
            f"{prefix}_right": self.right.astype(np.int32),
            f"{prefix}_value": self.value.astype(np.float64),
            f"{prefix}_root": self.root.astype(np.int32),
        }
        if self.missing_go_left is not None:
            out[f"{prefix}_missing_go_left"] = self.missing_go_left.astype(bool)
        return out

    @classmethod
    def from_payload(cls, payload: dict, meta: dict, prefix: str = "tree") -> "TreeEnsembleSpec":
        return cls(
            feature=np.asarray(payload[f"{prefix}_feature"], dtype=np.int32),
            threshold=np.asarray(payload[f"{prefix}_threshold"], dtype=np.float64),
            left=np.asarray(payload[f"{prefix}_left"], dtype=np.int32),
            right=np.asarray(payload[f"{prefix}_right"], dtype=np.int32),
            value=np.asarray(payload[f"{prefix}_value"], dtype=np.float64),
            root=np.asarray(payload[f"{prefix}_root"], dtype=np.int32),
            combine=str(meta.get("combine", "sum_logit")),
            base_score=float(meta.get("base_score", 0.0)),
            input_dtype=str(meta.get("input_dtype", "float64")),
            missing_go_left=(
                np.asarray(payload[f"{prefix}_missing_go_left"], dtype=bool)
                if f"{prefix}_missing_go_left" in payload
                else None
            ),
        )


def leaf_sum(spec: TreeEnsembleSpec, X: "np.ndarray") -> "np.ndarray":
    """Sum of the leaf values every row lands on, across all trees.

    All rows descend every tree simultaneously: ``node`` is a vector of current
    node indices and each iteration advances only the rows that are not yet at a
    leaf.  The loop runs at most ``max_depth`` times, which is why this is
    practical in a live loop even for a 300-tree forest.
    """
    X = np.asarray(X, dtype=float)
    if X.ndim == 1:
        X = X.reshape(1, -1)
    if spec.input_dtype == "float32":
        # Round exactly the way sklearn.tree's check_array does, then compare in
        # float64 exactly the way its C traversal does.
        X = X.astype(np.float32).astype(np.float64)
    n = X.shape[0]
    rows = np.arange(n)
    acc = np.zeros(n, dtype=float)
    mgl = spec.missing_go_left

    for t in range(spec.n_trees):
        node = np.full(n, int(spec.root[t]), dtype=np.int64)
        for _ in range(spec.n_nodes + 1):
            active = spec.feature[node] != LEAF
            if not active.any():
                break
            idx = node[active]
            f = spec.feature[idx].astype(np.int64)
            vals = X[rows[active], f]
            go_left = vals <= spec.threshold[idx]
            if mgl is not None:
                nan = np.isnan(vals)
                if nan.any():
                    go_left = np.where(nan, mgl[idx], go_left)
            node[active] = np.where(go_left, spec.left[idx], spec.right[idx])
        else:  # pragma: no cover - only reachable if the spec has a cycle
            raise ExportError("tree traversal did not terminate; the spec is malformed")
        acc += spec.value[node]
    return acc


def predict_tree_ensemble(spec: TreeEnsembleSpec, X: "np.ndarray") -> "np.ndarray":
    """``P(class 1)`` for every row, using numpy only."""
    acc = leaf_sum(spec, X)
    if spec.combine == "mean_proba":
        return np.clip(acc / spec.n_trees, 0.0, 1.0)
    return expit(acc + spec.base_score)


# --------------------------------------------------------------------------- #
# The artifact
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PredictiveArtifact:
    """A frozen champion: everything needed to score a row in production.

    The metadata is not decoration.  ``spec_key`` says which barrier definition
    the probability refers to (a 0.62 for a 15 bps target over 25 minutes means
    nothing against a 30 bps target), ``feature_version`` says which feature
    code computed the training columns, and ``trained_to_ms`` says how stale it
    is.  An artifact whose ``spec_key`` no longer matches the live exit geometry
    is predicting a trade the engine is not taking.
    """

    version: str
    model_name: str
    feature_names: tuple[str, ...]
    spec_key: str
    probability_threshold: float
    ev_threshold_usd: float
    trained_to_ms: int
    feature_version: str
    payload: dict = field(default_factory=dict)
    #: Calibration measured on held-out rows; ``None`` means never measured.
    calibration: dict | None = None
    #: Out-of-sample performance the promotion decision was made on.
    oos: dict | None = None
    #: Regimes the champion was trained under; ``None`` means "no restriction".
    supported_regimes: tuple[str, ...] | None = None
    net_target_bps: float = 0.0
    stop_bps: float = 0.0
    created_ms: int = 0
    notes: tuple[str, ...] = ()

    # -- calibration read-outs the engine gates on ------------------------- #

    @property
    def calibrated(self) -> bool:
        """True only when calibration was *measured* and passed its tolerance.

        Never measured is reported as not calibrated.  An unmeasured
        probability is a number, not a probability, and the engine refuses to
        bet on one.
        """
        if not self.calibration:
            return False
        return bool(self.calibration.get("calibrated", False))

    @property
    def calibration_max_abs_error(self) -> float:
        if not self.calibration:
            return 1.0
        return float(self.calibration.get("max_abs_error", 1.0))

    @property
    def kind(self) -> str:
        return str(self.payload.get("kind", "unknown"))

    @property
    def n_features(self) -> int:
        return len(self.feature_names)

    # -- inference ---------------------------------------------------------- #

    def _scale(self, X: "np.ndarray") -> "np.ndarray":
        mean = self.payload.get("scaler_mean")
        scale = self.payload.get("scaler_scale")
        if mean is None or scale is None:
            return X
        return (X - np.asarray(mean, dtype=float)) / np.asarray(scale, dtype=float)

    def _calibrate(self, p: "np.ndarray") -> "np.ndarray":
        cal = self.payload.get("calibrator")
        if not cal:
            return p
        method = str(cal.get("method", "identity"))
        if method == "identity":
            return p
        if method == "platt":
            q = np.clip(p, 1e-15, 1.0 - 1e-15)
            return expit(float(cal["a"]) * np.log(q / (1.0 - q)) + float(cal["b"]))
        if method == "isotonic":
            x = np.asarray(cal["x"], dtype=float)
            y = np.asarray(cal["y"], dtype=float)
            return np.clip(np.interp(np.clip(p, x[0], x[-1]), x, y), 0.0, 1.0)
        raise FeatureContractError(f"artifact carries an unknown calibrator {method!r}")

    def predict_p_win(self, X) -> "np.ndarray":
        """``P(TP before SL)`` per row — numpy only, no sklearn anywhere."""
        X = np.asarray(X, dtype=float)
        if X.ndim == 1:
            X = X.reshape(1, -1)
        if X.shape[1] != len(self.feature_names):
            raise FeatureContractError(
                f"artifact {self.version} expects {len(self.feature_names)} features "
                f"({', '.join(self.feature_names[:4])}…) but was given {X.shape[1]}"
            )
        if not np.all(np.isfinite(X)):
            raise FeatureContractError(
                "feature row contains NaN or inf; a model scored on a broken row "
                "is guessing, and the engine must reject the candidate instead"
            )
        kind = self.kind
        Xs = self._scale(X)
        if kind == "linear":
            coef = np.asarray(self.payload["coef"], dtype=float)
            intercept = float(self.payload["intercept"])
            p = expit(Xs @ coef + intercept)
        elif kind == "tree_ensemble":
            spec = TreeEnsembleSpec.from_payload(self.payload, self.payload.get("tree_meta", {}))
            p = predict_tree_ensemble(spec, Xs)
        else:
            raise FeatureContractError(f"artifact payload kind {kind!r} is not supported")
        return self._calibrate(np.asarray(p, dtype=float))

    def predict_proba(self, X) -> "np.ndarray":
        """sklearn's ``(n, 2)`` layout: column 0 is ``P(no)``, column 1 ``P(yes)``.

        The shape matches sklearn deliberately, because the test that matters
        compares the two arrays element for element.
        """
        p = self.predict_p_win(X)
        return np.column_stack([1.0 - p, p])

    # -- housekeeping -------------------------------------------------------- #

    def with_calibration(self, report: Any) -> "PredictiveArtifact":
        """Attach a :class:`~app.predict.calibrate.CalibrationReport`."""
        as_dict = report.as_dict() if hasattr(report, "as_dict") else dict(report)
        return replace(self, calibration=as_dict)

    def describe(self) -> str:
        lines = [
            f"artifact {self.version} — {self.model_name} ({self.kind})",
            f"  spec_key        {self.spec_key}",
            f"  features        {len(self.feature_names)} @ {self.feature_version}",
            f"  trained_to_ms   {self.trained_to_ms}",
            f"  p threshold     {self.probability_threshold:.4f}",
            f"  ev threshold    ${self.ev_threshold_usd:.4f}",
            f"  calibrated      {self.calibrated} "
            f"(max abs error {self.calibration_max_abs_error:.4f})",
        ]
        if self.supported_regimes:
            lines.append(f"  regimes         {', '.join(self.supported_regimes)}")
        for n in self.notes:
            lines.append(f"  note            {n}")
        return "\n".join(lines)


def assert_feature_names(artifact: PredictiveArtifact, feature_names: Sequence[str]) -> None:
    """Fail loudly when the live matrix does not match what the model was trained on.

    Order matters as much as membership: column 3 becoming column 4 keeps every
    name present and feeds every value to the wrong coefficient.  The error says
    which names were added, which went missing and which merely moved, because
    "feature mismatch" on its own does not tell an operator what to fix.
    """
    got = tuple(feature_names)
    want = tuple(artifact.feature_names)
    if got == want:
        return
    missing = [n for n in want if n not in got]
    added = [n for n in got if n not in want]
    moved = [
        n
        for n in want
        if n in got and want.index(n) != got.index(n)
    ]
    detail = []
    if missing:
        detail.append(f"missing {missing}")
    if added:
        detail.append(f"unexpected {added}")
    if moved and not (missing or added):
        detail.append(f"reordered {moved[:8]}")
    raise FeatureContractError(
        f"artifact {artifact.version} ({artifact.model_name}, feature_version="
        f"{artifact.feature_version}) was trained on {len(want)} features and the "
        f"current matrix has {len(got)}: " + "; ".join(detail or ["order differs"]) +
        ". Refusing to score — a model fed misaligned columns still returns a "
        "confident probability, and it is wrong."
    )


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


def _linear_payload(model: Any) -> dict:
    est = model.estimator
    coef = np.asarray(getattr(est, "coef_"), dtype=float)
    intercept = np.asarray(getattr(est, "intercept_"), dtype=float)
    if coef.ndim != 2 or coef.shape[0] != 1:
        raise ExportError(
            f"{model.name}: expected a binary linear model with one coefficient "
            f"row, got shape {coef.shape}. Multi-class is not supported."
        )
    return {
        "kind": "linear",
        "coef": coef[0].astype(np.float64),
        "intercept": float(intercept[0]),
    }


def _flatten_sklearn_trees(trees: Sequence[Any], leaf_value) -> TreeEnsembleSpec:
    """Concatenate ``sklearn.tree._tree.Tree`` objects into flat arrays."""
    feature: list[np.ndarray] = []
    threshold: list[np.ndarray] = []
    left: list[np.ndarray] = []
    right: list[np.ndarray] = []
    value: list[np.ndarray] = []
    roots: list[int] = []
    offset = 0
    for tr in trees:
        n = int(tr.node_count)
        cl = np.asarray(tr.children_left, dtype=np.int64)
        cr = np.asarray(tr.children_right, dtype=np.int64)
        is_leaf = cl == -1
        f = np.asarray(tr.feature, dtype=np.int64).copy()
        f[is_leaf] = LEAF
        # -1 children would rebase to a valid index, so pin leaves at themselves.
        cl = np.where(is_leaf, np.arange(n), cl) + offset
        cr = np.where(is_leaf, np.arange(n), cr) + offset
        feature.append(f)
        threshold.append(np.asarray(tr.threshold, dtype=np.float64))
        left.append(cl)
        right.append(cr)
        value.append(np.asarray(leaf_value(tr), dtype=np.float64))
        roots.append(offset)
        offset += n
    return TreeEnsembleSpec(
        feature=np.concatenate(feature).astype(np.int32),
        threshold=np.concatenate(threshold),
        left=np.concatenate(left).astype(np.int32),
        right=np.concatenate(right).astype(np.int32),
        value=np.concatenate(value),
        root=np.asarray(roots, dtype=np.int32),
    )


def _random_forest_spec(est: Any) -> TreeEnsembleSpec:
    def leaf_value(tr):
        v = np.asarray(tr.value)[:, 0, :]
        total = v.sum(axis=1)
        return np.where(total > 0, v[:, 1] / np.where(total > 0, total, 1.0), 0.0)

    spec = _flatten_sklearn_trees([e.tree_ for e in est.estimators_], leaf_value)
    return replace(spec, combine="mean_proba", base_score=0.0, input_dtype="float32")


def _gbm_spec(est: Any, probe: "np.ndarray") -> TreeEnsembleSpec:
    lr = float(est.learning_rate)

    def leaf_value(tr):
        return np.asarray(tr.value)[:, 0, 0] * lr

    trees = [e.tree_ for e in np.asarray(est.estimators_)[:, 0]]
    spec = replace(_flatten_sklearn_trees(trees, leaf_value), input_dtype="float32")
    # Derive the init/baseline raw score from the public decision_function
    # rather than a private attribute, so this survives a sklearn upgrade.
    raw = np.asarray(est.decision_function(probe), dtype=float).reshape(-1)
    contrib = leaf_sum(spec, probe)
    base = float(np.median(raw - contrib))
    return replace(spec, combine="sum_logit", base_score=base)


def _hist_gbm_spec(est: Any, probe: "np.ndarray") -> TreeEnsembleSpec:
    predictors = getattr(est, "_predictors", None)
    if not predictors:
        raise ExportError(
            "hist_gbm: the fitted model exposes no predictors to export"
        )
    feature: list[np.ndarray] = []
    threshold: list[np.ndarray] = []
    left: list[np.ndarray] = []
    right: list[np.ndarray] = []
    value: list[np.ndarray] = []
    missing: list[np.ndarray] = []
    roots: list[int] = []
    offset = 0
    for per_iter in predictors:
        if len(per_iter) != 1:
            raise ExportError(
                f"hist_gbm: {len(per_iter)} predictors per iteration means this is "
                "not a binary classifier; multi-class cannot be exported"
            )
        nodes = per_iter[0].nodes
        if bool(np.asarray(nodes["is_categorical"]).any()):
            raise ExportError(
                "hist_gbm: categorical splits use a bitset the flat spec cannot "
                "represent. Refit without categorical features, or export a "
                "different model — this one cannot be a champion."
            )
        n = int(nodes.shape[0])
        is_leaf = np.asarray(nodes["is_leaf"]).astype(bool)
        f = np.asarray(nodes["feature_idx"], dtype=np.int64).copy()
        f[is_leaf] = LEAF
        cl = np.asarray(nodes["left"], dtype=np.int64)
        cr = np.asarray(nodes["right"], dtype=np.int64)
        cl = np.where(is_leaf, np.arange(n), cl) + offset
        cr = np.where(is_leaf, np.arange(n), cr) + offset
        feature.append(f)
        threshold.append(np.asarray(nodes["num_threshold"], dtype=np.float64))
        left.append(cl)
        right.append(cr)
        value.append(np.asarray(nodes["value"], dtype=np.float64))
        missing.append(np.asarray(nodes["missing_go_to_left"]).astype(bool))
        roots.append(offset)
        offset += n

    spec = TreeEnsembleSpec(
        feature=np.concatenate(feature).astype(np.int32),
        threshold=np.concatenate(threshold),
        left=np.concatenate(left).astype(np.int32),
        right=np.concatenate(right).astype(np.int32),
        value=np.concatenate(value),
        root=np.asarray(roots, dtype=np.int32),
        combine="sum_logit",
        missing_go_left=np.concatenate(missing),
        input_dtype="float64",
    )
    raw = np.asarray(est.decision_function(probe), dtype=float).reshape(-1)
    base = float(np.median(raw - leaf_sum(spec, probe)))
    return replace(spec, base_score=base)


def export(
    model: Any,
    *,
    spec_key: str,
    probability_threshold: float,
    ev_threshold_usd: float,
    trained_to_ms: int,
    feature_version: str,
    validate_X=None,
    calibrator: Any = None,
    calibration: Any = None,
    oos: dict | None = None,
    supported_regimes: Sequence[str] | None = None,
    net_target_bps: float = 0.0,
    stop_bps: float = 0.0,
    version: str | None = None,
    created_ms: int = 0,
    notes: Sequence[str] = (),
    tolerance: float = EXPORT_TOLERANCE,
) -> PredictiveArtifact:
    """Turn a :class:`~app.predict.models.TrainedModel` into a numpy-only bundle.

    ``validate_X`` should be held-out rows.  When given, the export is *checked*:
    the sklearn estimator and the exported arrays both score those rows and the
    result must agree to ``tolerance``.  If it does not, this raises
    :class:`ExportError` — the export refuses to produce a bundle it knows is
    wrong, which is the difference between "this model cannot be a champion" and
    "this model became a slightly different model on the way to production".

    An unsupported estimator also raises.  There is no degraded path.
    """
    name = getattr(model, "name", "unknown")
    est = getattr(model, "estimator", None)
    if est is None:
        raise ExportError(f"{name}: nothing to export — the model has no estimator")

    cls = type(est).__name__
    if cls in ("LogisticRegression",):
        payload = _linear_payload(model)
    elif cls == "RandomForestClassifier":
        spec = _random_forest_spec(est)
        payload = {"kind": "tree_ensemble", **spec.to_payload()}
        payload["tree_meta"] = {
            "combine": spec.combine,
            "base_score": spec.base_score,
            "input_dtype": spec.input_dtype,
        }
    elif cls in ("GradientBoostingClassifier", "HistGradientBoostingClassifier"):
        if validate_X is None:
            raise ExportError(
                f"{name}: exporting a boosted ensemble needs validate_X to solve "
                "for the baseline raw score and to verify the result"
            )
        probe = np.asarray(validate_X, dtype=float)
        probe = probe if probe.ndim == 2 else probe.reshape(1, -1)
        scaled = model.transform(probe)
        spec = (
            _gbm_spec(est, scaled)
            if cls == "GradientBoostingClassifier"
            else _hist_gbm_spec(est, scaled)
        )
        payload = {"kind": "tree_ensemble", **spec.to_payload()}
        payload["tree_meta"] = {
            "combine": spec.combine,
            "base_score": spec.base_score,
            "input_dtype": spec.input_dtype,
        }
    else:
        raise ExportError(
            f"{name}: {cls} has no numpy-only export. A model that cannot be "
            "exported cannot be a champion — pick one that can, or write its "
            "exporter. There is no approximate fallback."
        )

    if getattr(model, "scaler_mean", None) is not None:
        payload["scaler_mean"] = np.asarray(model.scaler_mean, dtype=np.float64)
        payload["scaler_scale"] = np.asarray(model.scaler_scale, dtype=np.float64)

    if calibrator is not None:
        payload["calibrator"] = calibrator.to_payload()

    cal_dict = None
    if calibration is not None:
        cal_dict = (
            calibration.as_dict() if hasattr(calibration, "as_dict") else dict(calibration)
        )

    artifact = PredictiveArtifact(
        version=version or f"{ARTIFACT_VERSION}:{name}:{spec_key}",
        model_name=name,
        feature_names=tuple(model.feature_names),
        spec_key=str(spec_key),
        probability_threshold=float(probability_threshold),
        ev_threshold_usd=float(ev_threshold_usd),
        trained_to_ms=int(trained_to_ms),
        feature_version=str(feature_version),
        payload=payload,
        calibration=cal_dict,
        oos=dict(oos) if oos else None,
        supported_regimes=tuple(supported_regimes) if supported_regimes else None,
        net_target_bps=float(net_target_bps),
        stop_bps=float(stop_bps),
        created_ms=int(created_ms),
        notes=tuple(notes),
    )

    if validate_X is not None:
        verify_export(artifact, model, validate_X, tolerance=tolerance, calibrator=calibrator)
    return artifact


def verify_export(
    artifact: PredictiveArtifact,
    model: Any,
    X,
    *,
    tolerance: float = EXPORT_TOLERANCE,
    calibrator: Any = None,
) -> float:
    """Re-score ``X`` through sklearn and through the export; return the max gap.

    Raises :class:`ExportError` when the gap exceeds ``tolerance``.  The
    comparison is against the *uncalibrated* probability, because the calibrator
    is applied on top of the model rather than being part of it.
    """
    X = np.asarray(X, dtype=float)
    if X.ndim == 1:
        X = X.reshape(1, -1)
    reference = np.asarray(model.predict_proba(X), dtype=float)
    bare = replace(
        artifact, payload={k: v for k, v in artifact.payload.items() if k != "calibrator"}
    )
    ours = bare.predict_proba(X)
    gap = float(np.max(np.abs(reference - ours)))
    if not np.isfinite(gap) or gap > tolerance:
        raise ExportError(
            f"{artifact.model_name}: the numpy export disagrees with sklearn by "
            f"{gap:.3e} (tolerance {tolerance:.0e}) over {X.shape[0]} rows. "
            "This model cannot be a champion; it would predict something "
            "different in production than it did in research."
        )
    if calibrator is not None:
        cal_ref = np.asarray(calibrator.predict(reference[:, 1]), dtype=float)
        cal_gap = float(np.max(np.abs(cal_ref - artifact.predict_p_win(X))))
        if cal_gap > tolerance:
            raise ExportError(
                f"{artifact.model_name}: the exported calibrator disagrees with the "
                f"fitted one by {cal_gap:.3e} (tolerance {tolerance:.0e})"
            )
    return gap


# --------------------------------------------------------------------------- #
# Persistence — numpy + stdlib only
# --------------------------------------------------------------------------- #

_META_KEY = "__meta__"


def _split_payload(payload: dict) -> tuple[dict, dict]:
    """Arrays go into the npz; scalars and nested dicts go into the JSON blob."""
    arrays: dict[str, np.ndarray] = {}
    scalars: dict[str, Any] = {}
    for key, val in payload.items():
        if isinstance(val, np.ndarray):
            arrays[f"payload__{key}"] = val
        elif isinstance(val, dict):
            nested_arrays = {
                k: v for k, v in val.items() if isinstance(v, np.ndarray)
            }
            for k, v in nested_arrays.items():
                arrays[f"payload__{key}__{k}"] = v
            scalars[key] = {
                k: v for k, v in val.items() if not isinstance(v, np.ndarray)
            }
        else:
            scalars[key] = val
    return arrays, scalars


def save(artifact: PredictiveArtifact, path: str | Path) -> Path:
    """Write the artifact as a ``.npz``. Round-trips exactly — floats stay binary."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    arrays, scalars = _split_payload(artifact.payload)
    meta = {
        "artifact_version": ARTIFACT_VERSION,
        "version": artifact.version,
        "model_name": artifact.model_name,
        "feature_names": list(artifact.feature_names),
        "spec_key": artifact.spec_key,
        "probability_threshold": artifact.probability_threshold,
        "ev_threshold_usd": artifact.ev_threshold_usd,
        "trained_to_ms": artifact.trained_to_ms,
        "feature_version": artifact.feature_version,
        "calibration": artifact.calibration,
        "oos": artifact.oos,
        "supported_regimes": (
            list(artifact.supported_regimes) if artifact.supported_regimes else None
        ),
        "net_target_bps": artifact.net_target_bps,
        "stop_bps": artifact.stop_bps,
        "created_ms": artifact.created_ms,
        "notes": list(artifact.notes),
        "payload_scalars": scalars,
    }
    buf = io.BytesIO()
    np.savez(buf, **arrays, **{_META_KEY: np.frombuffer(
        json.dumps(meta).encode("utf-8"), dtype=np.uint8
    )})
    path.write_bytes(buf.getvalue())
    return path


def load(path: str | Path) -> PredictiveArtifact:
    """Read an artifact back. Imports numpy and the standard library only.

    Deliberately ``allow_pickle=False``: an artifact is data.  If loading one
    ever required unpickling a Python object, the production container would
    need the class that object came from — which is exactly the sklearn
    dependency this whole format exists to avoid — and it would also mean an
    artifact file could execute code.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"no artifact at {path}")
    with np.load(path, allow_pickle=False) as z:
        if _META_KEY not in z.files:
            raise FeatureContractError(
                f"{path} is not a predictive artifact (no metadata block)"
            )
        meta = json.loads(bytes(z[_META_KEY].tobytes()).decode("utf-8"))
        payload: dict[str, Any] = dict(meta.get("payload_scalars") or {})
        for key in z.files:
            if key == _META_KEY or not key.startswith("payload__"):
                continue
            rest = key[len("payload__"):]
            if "__" in rest:
                outer, inner = rest.split("__", 1)
                payload.setdefault(outer, {})
                payload[outer][inner] = np.asarray(z[key])
            else:
                payload[rest] = np.asarray(z[key])

    stored = str(meta.get("artifact_version", ""))
    if stored != ARTIFACT_VERSION:
        raise FeatureContractError(
            f"{path} was written by artifact format {stored!r} but this code "
            f"reads {ARTIFACT_VERSION!r}. Re-export the champion rather than "
            "guessing at the old layout."
        )

    return PredictiveArtifact(
        version=str(meta["version"]),
        model_name=str(meta["model_name"]),
        feature_names=tuple(meta["feature_names"]),
        spec_key=str(meta["spec_key"]),
        probability_threshold=float(meta["probability_threshold"]),
        ev_threshold_usd=float(meta["ev_threshold_usd"]),
        trained_to_ms=int(meta["trained_to_ms"]),
        feature_version=str(meta["feature_version"]),
        payload=payload,
        calibration=meta.get("calibration"),
        oos=meta.get("oos"),
        supported_regimes=(
            tuple(meta["supported_regimes"]) if meta.get("supported_regimes") else None
        ),
        net_target_bps=float(meta.get("net_target_bps", 0.0)),
        stop_bps=float(meta.get("stop_bps", 0.0)),
        created_ms=int(meta.get("created_ms", 0)),
        notes=tuple(meta.get("notes") or ()),
    )
