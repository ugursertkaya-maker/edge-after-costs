"""Calibration — making the probability mean what it says.

A classifier that ranks well is not the same thing as a classifier you can bet
on.  Expected value is ``p·win − (1−p)·loss − cost``; if ``p`` is 0.70 when the
true frequency is 0.52, every EV number downstream is fiction and the engine
will size into trades it should have refused.  So this module measures the one
property the EV gate depends on:

> **A model predicting 70% across 100 trades should win about 70 of them.**

and :func:`format_reliability` prints, in English, every bin where it does not.

Three rules are enforced by the code rather than left to the reader:

1.  **A calibrator is fitted on validation data and never on test.**  Every
    :class:`Calibrator` fingerprints the rows it was fitted on, and
    :func:`calibrate_and_evaluate` raises :class:`CalibrationLeak` if it is
    then asked to score those same rows.  Reporting a calibrator's fit error as
    if it were its test error is the most flattering mistake available here, so
    it is made impossible rather than discouraged.
2.  **Nothing imports scikit-learn.**  Both calibrators — Platt and isotonic —
    are fitted in plain numpy (IRLS for Platt, pool-adjacent-violators for
    isotonic), which is what lets the production artifact apply a calibrated
    probability with numpy alone.  The metrics below are numpy too, and
    ``tests/test_predict_calibrate.py`` checks them against scikit-learn to
    1e-12 so "numpy-only" costs no accuracy.
3.  **Small bins are not quietly dropped.**  ``max_abs_error`` defaults to
    looking at every non-empty bin.  A bin with three rows in it genuinely can
    be 33 percentage points off, and hiding that behind a minimum count is how
    a calibration gate gets passed by a model that has not earned it.  Pass
    ``min_bin_count`` explicitly when you want the stable-bins-only view, and
    read :attr:`CalibrationReport.expected_calibration_error` — the count
    weighted average — alongside it.

The promotion gate in ``docs/PHASE4_SPEC.md`` is *max absolute reliability
error ≤ 0.10*; :data:`CALIBRATION_TOLERANCE` is that number and
:attr:`CalibrationReport.calibrated` is that comparison.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

__all__ = [
    "CALIBRATION_TOLERANCE",
    "CalibrationLeak",
    "CalibrationReport",
    "Calibrator",
    "DEFAULT_LEAK_FRACTION",
    "IdentityCalibrator",
    "IsotonicCalibrator",
    "PlattCalibrator",
    "average_precision",
    "brier_score",
    "calibrate_and_evaluate",
    "calibrator_from_payload",
    "check_not_fit_rows",
    "evaluate_calibration",
    "expit",
    "fit_calibrator",
    "format_reliability",
    "log_loss",
    "precision_recall",
    "reliability_failures",
    "roc_auc",
]

#: The Phase 4 promotion gate: max absolute reliability error must not exceed this.
CALIBRATION_TOLERANCE = 0.10

#: Probabilities are clipped to this interval before any log is taken.
_EPS = 1e-15


class CalibrationLeak(RuntimeError):
    """Raised when a calibrator is evaluated on the rows it was fitted on."""


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #


def expit(z: "np.ndarray | float") -> "np.ndarray":
    """Numerically stable logistic sigmoid, numpy only."""
    z = np.asarray(z, dtype=float)
    out = np.empty_like(z)
    pos = z >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-z[pos]))
    ez = np.exp(z[~pos])
    out[~pos] = ez / (1.0 + ez)
    return out


def _logit(p: "np.ndarray") -> "np.ndarray":
    p = np.clip(np.asarray(p, dtype=float), _EPS, 1.0 - _EPS)
    return np.log(p / (1.0 - p))


def _as_pair(p: Sequence[float] | "np.ndarray", y: Sequence[int] | "np.ndarray"):
    p = np.asarray(p, dtype=float).reshape(-1)
    y = np.asarray(y).reshape(-1).astype(float)
    if p.shape != y.shape:
        raise ValueError(f"p and y must have the same length, got {p.shape} and {y.shape}")
    if p.size == 0:
        raise ValueError("cannot score an empty prediction set")
    if not np.all(np.isfinite(p)):
        raise ValueError("predicted probabilities contain NaN or inf")
    uniq = set(np.unique(y).tolist())
    if not uniq <= {0.0, 1.0}:
        raise ValueError(f"y must be binary 0/1, saw {sorted(uniq)}")
    return p, y


def roc_auc(y: "np.ndarray", p: "np.ndarray") -> float:
    """Rank-based ROC-AUC with tie correction (identical to sklearn's).

    ``nan`` when only one class is present — an AUC over a single class is
    undefined, and returning 0.5 there would silently look like a coin flip
    rather than like missing information.
    """
    p, y = _as_pair(p, y)
    n_pos = float(y.sum())
    n_neg = float(y.size - n_pos)
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    order = np.argsort(p, kind="mergesort")
    ranks = np.empty(p.size, dtype=float)
    sp = p[order]
    i = 0
    while i < sp.size:
        j = i
        while j + 1 < sp.size and sp[j + 1] == sp[i]:
            j += 1
        ranks[i : j + 1] = 0.5 * (i + j) + 1.0  # 1-based mid-rank
        i = j + 1
    rank_of = np.empty(p.size, dtype=float)
    rank_of[order] = ranks
    return float((rank_of[y == 1].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def average_precision(y: "np.ndarray", p: "np.ndarray") -> float:
    """PR-AUC as a step-wise average precision (sklearn's definition).

    Under class imbalance this is the metric that moves when a model actually
    finds the minority class; ROC-AUC can stay respectable while precision is
    useless, which is exactly the failure mode Phase 4 has to avoid.
    """
    p, y = _as_pair(p, y)
    n_pos = float(y.sum())
    if n_pos == 0:
        return float("nan")
    order = np.argsort(-p, kind="mergesort")
    ys = y[order]
    ps = p[order]
    distinct = np.where(np.diff(ps))[0]
    idx = np.r_[distinct, ps.size - 1]
    tps = np.cumsum(ys)[idx]
    fps = (idx + 1) - tps
    precision = tps / np.maximum(tps + fps, 1e-300)
    recall = tps / n_pos
    prev_recall = np.r_[0.0, recall[:-1]]
    return float(np.sum((recall - prev_recall) * precision))


def brier_score(y: "np.ndarray", p: "np.ndarray") -> float:
    """Mean squared error of the probability — the sharpness+calibration score."""
    p, y = _as_pair(p, y)
    return float(np.mean((p - y) ** 2))


def log_loss(y: "np.ndarray", p: "np.ndarray") -> float:
    """Binary cross-entropy, clipped exactly the way sklearn clips it."""
    p, y = _as_pair(p, y)
    q = np.clip(p, _EPS, 1.0 - _EPS)
    return float(-np.mean(y * np.log(q) + (1.0 - y) * np.log(1.0 - q)))


def precision_recall(y: "np.ndarray", p: "np.ndarray", *, threshold: float = 0.5):
    """``(precision, recall, n_predicted_positive)`` at a decision threshold.

    Precision is ``nan``, not 1.0, when nothing is predicted positive.  A model
    that never fires has no precision; calling it perfect is how a degenerate
    majority-class classifier gets promoted.
    """
    p, y = _as_pair(p, y)
    fired = p >= threshold
    n_fired = int(fired.sum())
    tp = float(y[fired].sum()) if n_fired else 0.0
    n_pos = float(y.sum())
    precision = tp / n_fired if n_fired else float("nan")
    recall = tp / n_pos if n_pos else float("nan")
    return precision, recall, n_fired


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CalibrationReport:
    """Everything needed to say whether a probability can be bet on."""

    brier: float
    log_loss: float
    auc: float
    pr_auc: float
    #: ``(mean predicted, observed frequency, n)`` per non-empty bin, low to high.
    bins: list[tuple[float, float, int]]
    max_abs_error: float
    calibrated: bool
    method: str
    n: int = 0
    base_rate: float = 0.0
    mean_predicted: float = 0.0
    expected_calibration_error: float = 0.0
    tolerance: float = CALIBRATION_TOLERANCE
    min_bin_count: int = 1

    @property
    def worst_bin(self) -> tuple[float, float, int] | None:
        """The bin furthest from the diagonal, or ``None`` when there are none."""
        eligible = [b for b in self.bins if b[2] >= self.min_bin_count]
        if not eligible:
            return None
        return max(eligible, key=lambda b: abs(b[0] - b[1]))

    def as_dict(self) -> dict:
        return {
            "method": self.method,
            "n": self.n,
            "base_rate": self.base_rate,
            "mean_predicted": self.mean_predicted,
            "brier": self.brier,
            "log_loss": self.log_loss,
            "auc": self.auc,
            "pr_auc": self.pr_auc,
            "max_abs_error": self.max_abs_error,
            "expected_calibration_error": self.expected_calibration_error,
            "calibrated": self.calibrated,
            "tolerance": self.tolerance,
            "bins": [list(b) for b in self.bins],
        }


def evaluate_calibration(
    p,
    y,
    *,
    bins: int = 10,
    method: str = "raw",
    tolerance: float = CALIBRATION_TOLERANCE,
    min_bin_count: int = 1,
) -> CalibrationReport:
    """Score a probability against the outcomes it claimed to predict.

    ``bins`` are equal-width over ``[0, 1]`` rather than equal-count.  Equal
    count quantile bins look better for a model whose predictions all pile up
    in one place, because they spread that pile across the whole diagonal; a
    fixed grid shows the pile for what it is.
    """
    p, y = _as_pair(p, y)
    if bins < 1:
        raise ValueError("bins must be >= 1")

    edges = np.linspace(0.0, 1.0, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1], right=False), 0, bins - 1)
    rows: list[tuple[float, float, int]] = []
    for b in range(bins):
        sel = idx == b
        n = int(sel.sum())
        if n == 0:
            continue
        rows.append((float(p[sel].mean()), float(y[sel].mean()), n))

    eligible = [r for r in rows if r[2] >= min_bin_count]
    max_err = max((abs(a - o) for a, o, _ in eligible), default=0.0)
    total = sum(r[2] for r in rows) or 1
    ece = sum(abs(a - o) * n for a, o, n in rows) / total

    return CalibrationReport(
        brier=brier_score(y, p),
        log_loss=log_loss(y, p),
        auc=roc_auc(y, p),
        pr_auc=average_precision(y, p),
        bins=rows,
        max_abs_error=float(max_err),
        calibrated=bool(max_err <= tolerance),
        method=method,
        n=int(p.size),
        base_rate=float(y.mean()),
        mean_predicted=float(p.mean()),
        expected_calibration_error=float(ece),
        tolerance=float(tolerance),
        min_bin_count=int(min_bin_count),
    )


def reliability_failures(
    report: CalibrationReport, *, tolerance: float | None = None
) -> list[tuple[float, float, int, float]]:
    """Bins where the claim and the outcome disagree by more than ``tolerance``.

    Returns ``(predicted, actual, n, signed_error)`` with a **positive** error
    meaning overconfident — the model promised more than it delivered, which is
    the direction that loses money.
    """
    tol = report.tolerance if tolerance is None else tolerance
    out = []
    for pred, actual, n in report.bins:
        if n < report.min_bin_count:
            continue
        err = pred - actual
        if abs(err) > tol:
            out.append((pred, actual, n, err))
    return out


def format_reliability(report: CalibrationReport) -> str:
    """The reliability table in English, including the verdict line."""
    lines = [
        f"calibration [{report.method}] n={report.n} "
        f"base rate {report.base_rate:.3f} mean predicted {report.mean_predicted:.3f}",
        f"  brier {report.brier:.5f}  log loss {report.log_loss:.5f}  "
        f"auc {report.auc:.4f}  pr-auc {report.pr_auc:.4f}",
        "  predicted   actual      n   error",
    ]
    for pred, actual, n in report.bins:
        err = pred - actual
        flag = ""
        if n >= report.min_bin_count and abs(err) > report.tolerance:
            flag = "  <-- OVERCONFIDENT" if err > 0 else "  <-- UNDERCONFIDENT"
        lines.append(
            f"     {pred:6.3f}   {actual:6.3f}  {n:5d}  {err:+6.3f}{flag}"
        )
    lines.append(
        f"  max abs error {report.max_abs_error:.4f} (gate <= {report.tolerance:.2f})  "
        f"ECE {report.expected_calibration_error:.4f}  "
        f"=> {'CALIBRATED' if report.calibrated else 'NOT CALIBRATED'}"
    )
    bad = reliability_failures(report)
    for pred, actual, n, err in bad:
        verdict = "promised more than it delivered" if err > 0 else "under-sold itself"
        lines.append(
            f"  it said {pred * 100:.0f}% across {n} trades and won "
            f"{actual * 100:.0f}% of them ({err * 100:+.0f}pp) — {verdict}"
        )
    if not bad:
        lines.append("  every bin is within tolerance")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Calibrators
# --------------------------------------------------------------------------- #


def _fingerprint(p: "np.ndarray", y: "np.ndarray") -> frozenset[str]:
    """Per-row digests of the fit set, so an overlap can be detected later.

    Row-wise rather than whole-set, because the leak we care about is "these
    test rows were in the fit set", not "the sets are identical".

    ``(p, y)`` is a *value* fingerprint and two genuinely different rows can
    collide — a tree ensemble hands the same leaf probability to hundreds of
    rows.  That is why :func:`fit_calibrator` also accepts ``keys`` (row
    identities such as ``ts_ms``): with keys the check is exact and any overlap
    at all is a leak, and without them it falls back to this and needs a
    fraction of the evaluation set to overlap before it will call it one.
    """
    out = set()
    for pi, yi in zip(np.asarray(p, dtype=float).reshape(-1), np.asarray(y).reshape(-1)):
        out.add(hashlib.blake2b(f"{pi!r}|{int(yi)}".encode(), digest_size=8).hexdigest())
    return frozenset(out)


def _as_keys(keys) -> frozenset:
    if keys is None:
        return frozenset()
    return frozenset(np.asarray(keys).reshape(-1).tolist())


#: Without row keys, this fraction of the evaluation set must look like fit rows
#: before a leak is declared.  Full overlap — the mistake this exists to stop —
#: is 1.0 and is always caught.
DEFAULT_LEAK_FRACTION = 0.10


@dataclass
class Calibrator:
    """Base class: a monotone map from raw probability to honest probability."""

    method: str = "identity"
    #: Digests of the rows this calibrator was fitted on (never serialised).
    fit_fingerprint: frozenset[str] = field(default_factory=frozenset, repr=False)
    #: Exact row identities (e.g. ``ts_ms``) when the caller supplied them.
    fit_keys: frozenset = field(default_factory=frozenset, repr=False)
    n_fit: int = 0

    def predict(self, p) -> "np.ndarray":  # pragma: no cover - abstract
        raise NotImplementedError

    def __call__(self, p) -> "np.ndarray":
        return self.predict(p)

    def to_payload(self) -> dict:  # pragma: no cover - abstract
        raise NotImplementedError


@dataclass
class IdentityCalibrator(Calibrator):
    """No-op: the model's probability is already taken at face value."""

    method: str = "identity"

    def predict(self, p) -> "np.ndarray":
        return np.clip(np.asarray(p, dtype=float).reshape(-1), 0.0, 1.0)

    def to_payload(self) -> dict:
        return {"method": "identity"}


@dataclass
class PlattCalibrator(Calibrator):
    """Platt scaling: ``sigmoid(a · logit(p) + b)``, fitted by Newton/IRLS.

    Fitted on the *logit* rather than on the raw probability so that a model
    which is already calibrated maps to roughly ``a=1, b=0`` instead of to a
    squashed approximation of itself.  Two parameters only, which is the point:
    it cannot memorise a small validation set the way isotonic can.
    """

    method: str = "platt"
    a: float = 1.0
    b: float = 0.0

    def predict(self, p) -> "np.ndarray":
        return expit(self.a * _logit(np.asarray(p, dtype=float).reshape(-1)) + self.b)

    def to_payload(self) -> dict:
        return {"method": "platt", "a": float(self.a), "b": float(self.b)}


@dataclass
class IsotonicCalibrator(Calibrator):
    """Isotonic regression: a monotone step function fitted by PAVA.

    Strictly more flexible than Platt and strictly more able to overfit, which
    is why it is fitted on validation data and evaluated somewhere else.
    Exported as ``(x, y)`` knots and applied with ``np.interp``, clipped at
    both ends — the same thing sklearn's ``out_of_bounds="clip"`` does.
    """

    method: str = "isotonic"
    x: "np.ndarray" = field(default_factory=lambda: np.array([0.0, 1.0]))
    y: "np.ndarray" = field(default_factory=lambda: np.array([0.0, 1.0]))

    def predict(self, p) -> "np.ndarray":
        q = np.clip(np.asarray(p, dtype=float).reshape(-1), self.x[0], self.x[-1])
        return np.clip(np.interp(q, self.x, self.y), 0.0, 1.0)

    def to_payload(self) -> dict:
        return {
            "method": "isotonic",
            "x": np.asarray(self.x, dtype=float),
            "y": np.asarray(self.y, dtype=float),
        }


def _pava(x: "np.ndarray", y: "np.ndarray", w: "np.ndarray"):
    """Pool adjacent violators — the whole of isotonic regression, in numpy."""
    order = np.argsort(x, kind="mergesort")
    x, y, w = x[order], y[order], w[order]
    # Merge exact ties in x first; a step function cannot have two values at one x.
    xs: list[float] = []
    ys: list[float] = []
    ws: list[float] = []
    i = 0
    while i < x.size:
        j = i
        while j + 1 < x.size and x[j + 1] == x[i]:
            j += 1
        wt = float(w[i : j + 1].sum())
        xs.append(float(x[i]))
        ys.append(float((y[i : j + 1] * w[i : j + 1]).sum() / wt) if wt else 0.0)
        ws.append(wt)
        i = j + 1

    # Stack-based PAVA over the deduplicated blocks.  Each block carries its
    # total weight, its weighted mean and how many x-positions it spans, which
    # is all that is needed to expand it back out afterwards.
    blocks: list[list[float]] = []
    for v, wt in zip(ys, ws):
        blocks.append([wt, v, 1.0])
        while len(blocks) > 1 and blocks[-2][1] > blocks[-1][1]:
            w2, v2, n2 = blocks.pop()
            w1, v1, n1 = blocks.pop()
            total = w1 + w2
            blocks.append(
                [total, (v1 * w1 + v2 * w2) / total if total else 0.0, n1 + n2]
            )
    fitted = np.empty(len(ys), dtype=float)
    pos = 0
    for _wt, v, n in blocks:
        end = pos + int(n)
        fitted[pos:end] = v
        pos = end
    return np.asarray(xs, dtype=float), fitted


def fit_calibrator(
    p_val,
    y_val,
    *,
    method: str = "isotonic",
    smoothing: bool = True,
    max_iter: int = 100,
    keys=None,
) -> Calibrator:
    """Fit a calibrator **on validation data**.

    This function is never given test rows.  The calibrator it returns carries
    a fingerprint of the rows it saw, and :func:`calibrate_and_evaluate` refuses
    to score those rows — so the "fitted and evaluated on the same data" result,
    which always looks excellent and always means nothing, cannot be produced
    by accident.

    Pass ``keys`` — the ``ts_ms`` of the validation rows, or any other row
    identity — whenever you have them.  With keys the leak check is exact.
    Without them it compares ``(probability, label)`` values, which cannot
    distinguish a genuinely repeated prediction from a repeated *row*, so it
    needs :data:`DEFAULT_LEAK_FRACTION` of the evaluation set to match before it
    will refuse.
    """
    p, y = _as_pair(p_val, y_val)
    fp = _fingerprint(p, y)
    kf = _as_keys(keys)
    if kf and len(kf) != p.size:
        raise ValueError(
            f"keys must be unique per row: got {len(kf)} distinct for {p.size} rows"
        )
    n = int(p.size)

    if method == "identity":
        return IdentityCalibrator(
            method="identity", fit_fingerprint=fp, fit_keys=kf, n_fit=n
        )

    if method == "platt":
        n_pos = float(y.sum())
        n_neg = float(n - n_pos)
        if smoothing and n_pos > 0 and n_neg > 0:
            hi = (n_pos + 1.0) / (n_pos + 2.0)
            lo = 1.0 / (n_neg + 2.0)
            t = np.where(y > 0, hi, lo)
        else:
            t = y.astype(float)
        f = _logit(p)

        def objective(aa: float, bb: float) -> float:
            z = aa * f + bb
            # log(1+exp(-z)) evaluated without overflowing on either branch.
            pos = np.where(z >= 0, np.log1p(np.exp(-np.abs(z))), z + np.log1p(np.exp(-np.abs(z))))
            return float(np.sum((t - 1.0) * z - pos))

        a, b = 1.0, 0.0
        obj = objective(a, b)
        for _ in range(max_iter):
            z = a * f + b
            q = expit(z)
            g_a = float(np.sum((q - t) * f))
            g_b = float(np.sum(q - t))
            wgt = q * (1.0 - q) + 1e-12
            h_aa = float(np.sum(wgt * f * f)) + 1e-9
            h_ab = float(np.sum(wgt * f))
            h_bb = float(np.sum(wgt)) + 1e-9
            det = h_aa * h_bb - h_ab * h_ab
            if abs(det) < 1e-18 or max(abs(g_a), abs(g_b)) < 1e-9:
                break
            da = (h_bb * g_a - h_ab * g_b) / det
            db = (h_aa * g_b - h_ab * g_a) / det
            # Backtracking line search (Lin, Weng & Lin 2007).  Without it the
            # Newton step diverges on a near-separable validation set — a score
            # that happens to order the labels perfectly sends the slope to
            # 1e11 and the "calibrated" probability becomes a step function.
            step = 1.0
            improved = False
            for _ in range(32):
                cand_a, cand_b = a - step * da, b - step * db
                cand = objective(cand_a, cand_b)
                if cand < obj:
                    a, b, obj = cand_a, cand_b, cand
                    improved = True
                    break
                step *= 0.5
            if not improved or max(abs(step * da), abs(step * db)) < 1e-12:
                break
        if not (math.isfinite(a) and math.isfinite(b)) or abs(a) > 1e6:
            a, b = 1.0, 0.0
        return PlattCalibrator(
            method="platt", fit_fingerprint=fp, fit_keys=kf, n_fit=n,
            a=float(a), b=float(b),
        )

    if method == "isotonic":
        xs, fitted = _pava(p, y, np.ones_like(p))
        if xs.size == 1:  # a single distinct prediction: a constant map
            xs = np.array([0.0, 1.0])
            fitted = np.array([fitted[0], fitted[0]])
        return IsotonicCalibrator(
            method="isotonic",
            fit_fingerprint=fp,
            fit_keys=kf,
            n_fit=n,
            x=np.clip(xs, 0.0, 1.0),
            y=np.clip(fitted, 0.0, 1.0),
        )

    raise ValueError(f"unknown calibration method {method!r}; use platt/isotonic/identity")


def calibrator_from_payload(payload: dict) -> Calibrator:
    """Rebuild a calibrator from its exported payload (numpy only)."""
    method = str(payload.get("method", "identity"))
    if method == "identity":
        return IdentityCalibrator()
    if method == "platt":
        return PlattCalibrator(a=float(payload["a"]), b=float(payload["b"]))
    if method == "isotonic":
        return IsotonicCalibrator(
            x=np.asarray(payload["x"], dtype=float),
            y=np.asarray(payload["y"], dtype=float),
        )
    raise ValueError(f"unknown calibrator payload method {method!r}")


# --------------------------------------------------------------------------- #
# The leak guard
# --------------------------------------------------------------------------- #


def check_not_fit_rows(
    calibrator: Calibrator,
    p,
    y,
    *,
    keys=None,
    leak_fraction: float = DEFAULT_LEAK_FRACTION,
) -> None:
    """Raise :class:`CalibrationLeak` if these rows were used to fit.

    The check is row-wise, so a partial overlap — a validation window that
    bleeds into the test window because somebody forgot the embargo — is caught
    just as loudly as scoring the exact fit set.

    With ``keys`` on both sides the comparison is exact and **one** shared row
    is enough to refuse.  Falling back to ``(probability, label)`` values, it
    takes ``leak_fraction`` of the evaluation set, because identical
    probabilities are ordinary — every leaf of a tree emits one — and a guard
    that cried leak on a coincidence would simply be turned off.
    """
    eval_keys = _as_keys(keys)
    if calibrator.fit_keys and eval_keys:
        shared = calibrator.fit_keys & eval_keys
        if shared:
            raise CalibrationLeak(
                f"{calibrator.method} calibrator is being evaluated on "
                f"{len(shared)} row(s) it was fitted on, matched by row id "
                f"(fit n={calibrator.n_fit}, eval n={len(eval_keys)}). "
                "Fit on validation, evaluate on test."
            )
        return

    if not calibrator.fit_fingerprint:
        return
    p_arr, y_arr = _as_pair(p, y)
    eval_fp = _fingerprint(p_arr, y_arr)
    overlap = eval_fp & calibrator.fit_fingerprint
    if not overlap:
        return
    fraction = len(overlap) / max(len(eval_fp), 1)
    if fraction >= leak_fraction:
        raise CalibrationLeak(
            f"{calibrator.method} calibrator is being evaluated on rows it was "
            f"fitted on: {fraction:.0%} of the evaluation set matches the fit set "
            f"by (probability, label) (fit n={calibrator.n_fit}, "
            f"eval n={p_arr.size}). Fit on validation, evaluate on test. "
            "Pass keys= on both sides for an exact check."
        )


def calibrate_and_evaluate(
    calibrator: Calibrator,
    p,
    y,
    *,
    bins: int = 10,
    tolerance: float = CALIBRATION_TOLERANCE,
    min_bin_count: int = 1,
    strict: bool = True,
    keys=None,
) -> CalibrationReport:
    """Apply a calibrator to held-out rows and score the result.

    ``strict=True`` — the default, and the only setting any report should use —
    refuses to run on rows the calibrator was fitted on.
    """
    if strict:
        check_not_fit_rows(calibrator, p, y, keys=keys)
    return evaluate_calibration(
        calibrator.predict(p),
        y,
        bins=bins,
        method=calibrator.method,
        tolerance=tolerance,
        min_bin_count=min_bin_count,
    )
