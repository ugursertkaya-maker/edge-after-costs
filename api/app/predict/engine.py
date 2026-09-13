"""The live predictive entry engine.

This is what replaces "momentum score = 83, therefore BUY".  It loads a frozen
:class:`PredictiveArtifact`, scores the current market state, turns the
probability into an expected value **after costs**, and returns a decision with
the numbers that produced it attached — so every trade and every rejection can
be explained from stored values rather than from a story told afterwards.

Three properties matter more than anything else here:

* **It fails closed.**  No artifact, a feature mismatch, a stale book, an
  uncalibrated model — every one of those returns NO TRADE with a reason, never
  a guess.  The engine is the last thing between a model and the book, and a
  model that cannot be trusted must not be allowed to act.
* **It needs numpy only.**  The artifact carries either linear coefficients or a
  flattened tree ensemble; scikit-learn trains it on a laptop and never ships.
* **It decides on expected value, not probability.**  A 70% chance of winning
  $0.10 against a 30% chance of losing $0.40 is a losing trade, and the gate
  says so.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.types import Features, Regime, RegimeState, Side, now_ms

log = logging.getLogger("edge_after_costs.predict")

#: Every way the engine can decline.  Counted, so the report can tell "found no
#: edge" apart from "refuses everything" — identical in the P&L, completely
#: different diagnoses.
REJECT_REASONS = (
    "LOW_PROBABILITY",
    "NEGATIVE_EV",
    "COST_TOO_HIGH",
    "MODEL_UNCERTAIN",
    "CORRELATED_RISK",
    "SPREAD_TOO_WIDE",
    "NO_LIQUIDITY",
    "STALE_DATA",
    "REGIME_NOT_SUPPORTED",
    "NO_MODEL",
    "FEATURE_MISMATCH",
)


@dataclass
class Prediction:
    """One scored candidate, with the evidence behind the decision."""

    symbol: str
    ts_ms: int
    regime: str
    model: str
    p_tp: float
    p_sl: float
    p_timeout: float
    expected_net_win: float
    expected_net_loss: float
    expected_value: float
    execution_cost: float
    confidence: float
    confidence_known: bool
    decision: str                 # "BUY" | "WATCH" | "NO_TRADE"
    reject_reason: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "regime": self.regime,
            "model": self.model,
            "p_tp": round(self.p_tp, 4),
            "p_sl": round(self.p_sl, 4),
            "p_timeout": round(self.p_timeout, 4),
            "expected_net_win": round(self.expected_net_win, 4),
            "expected_net_loss": round(self.expected_net_loss, 4),
            "expected_value": round(self.expected_value, 4),
            "execution_cost": round(self.execution_cost, 4),
            "confidence": round(self.confidence, 4) if self.confidence_known else None,
            "confidence_known": self.confidence_known,
            "decision": self.decision,
            "reject_reason": self.reject_reason,
        }

    def explanation(self) -> dict[str, Any]:
        """The stored values a "why did it trade?" line is rendered from.

        Deliberately a dict of numbers, not a sentence: the explanation must be
        derived from what the model and the risk engine actually computed, never
        composed after the fact.
        """
        return {
            "symbol": self.symbol,
            "decision": self.decision,
            "p_win": round(self.p_tp, 4),
            "expected_net_win": round(self.expected_net_win, 4),
            "expected_net_loss": round(self.expected_net_loss, 4),
            "expected_value": round(self.expected_value, 4),
            "regime": self.regime,
            "model_version": self.model,
            "ts_ms": self.ts_ms,
        }


@dataclass
class EngineStats:
    scored: int = 0
    buys: int = 0
    watches: int = 0
    rejections: dict[str, int] = field(default_factory=dict)

    def reject(self, reason: str) -> None:
        self.rejections[reason] = self.rejections.get(reason, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "scored": self.scored,
            "buys": self.buys,
            "watches": self.watches,
            "rejections": dict(self.rejections),
        }


class PredictiveEntryEngine:
    """Scores candidates against a frozen artifact and gates them on EV."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        artifact: Any = None,
        artifact_path: str | Path | None = None,
    ) -> None:
        self.s = settings or get_settings()
        self.stats = EngineStats()
        self.artifact = artifact
        self.load_error: str | None = None
        if self.artifact is None and artifact_path is not None:
            self._load(artifact_path)

    # ------------------------------------------------------------------ #

    def _load(self, path: str | Path) -> None:
        try:
            from app.predict.artifact import load as load_artifact

            self.artifact = load_artifact(Path(path))
            log.info(
                "predictive artifact loaded: %s (%s, %d features)",
                getattr(self.artifact, "version", "?"),
                getattr(self.artifact, "model_name", "?"),
                len(getattr(self.artifact, "feature_names", ()) or ()),
            )
        except Exception as exc:
            # Failing closed is the point: no model is a clean NO TRADE, and a
            # half-loaded one must never be allowed to score anything.
            self.artifact = None
            self.load_error = f"{type(exc).__name__}: {exc}"
            log.warning("predictive artifact not loaded: %s", self.load_error)

    @property
    def ready(self) -> bool:
        return self.artifact is not None

    @property
    def version(self) -> str:
        return str(getattr(self.artifact, "version", "none")) if self.ready else "none"

    # ------------------------------------------------------------------ #

    def score(
        self,
        feats: Features,
        regime: RegimeState,
        *,
        notional_usd: float,
        round_trip_bps: float,
        book_age_ms: int | None = None,
        spread_bps: float | None = None,
        depth_usd: float | None = None,
    ) -> Prediction:
        """Score one candidate.  Always returns a Prediction, never raises."""
        self.stats.scored += 1
        ts = int(getattr(feats, "ts_ms", 0)) or now_ms()
        regime_name = regime.regime.value if regime else Regime.UNKNOWN.value

        def refuse(reason: str, **over) -> Prediction:
            self.stats.reject(reason)
            base = dict(
                symbol=feats.symbol, ts_ms=ts, regime=regime_name,
                model=self.version, p_tp=0.0, p_sl=0.0, p_timeout=0.0,
                expected_net_win=0.0, expected_net_loss=0.0, expected_value=0.0,
                execution_cost=0.0, confidence=0.0, confidence_known=False,
                decision="NO_TRADE", reject_reason=reason,
            )
            base.update(over)
            return Prediction(**base)

        if not self.ready:
            return refuse("NO_MODEL")

        # Data-quality gates first. A model scoring a stale book produces a
        # confident number about a market that no longer exists.
        age = book_age_ms if book_age_ms is not None else getattr(feats, "book_age_ms", 0)
        if age and age > self.s.risk.max_book_age_ms:
            return refuse("STALE_DATA")
        spread = spread_bps if spread_bps is not None else getattr(feats, "spread_bps", 0.0)
        if spread and spread > self.s.risk.max_entry_spread_bps:
            return refuse("SPREAD_TOO_WIDE")
        depth = depth_usd
        if depth is None:
            depth = getattr(feats, "depth_bid_usd", 0.0) + getattr(feats, "depth_ask_usd", 0.0)
        if depth and depth < self.s.risk.min_book_depth_usd:
            return refuse("NO_LIQUIDITY")
        if regime and regime.regime is Regime.ABNORMAL:
            return refuse("REGIME_NOT_SUPPORTED")

        supported = getattr(self.artifact, "supported_regimes", None)
        if supported and regime_name not in supported:
            return refuse("REGIME_NOT_SUPPORTED")

        try:
            probs = self._predict(feats)
        except Exception as exc:
            log.debug("scoring failed for %s: %s", feats.symbol, exc)
            return refuse("FEATURE_MISMATCH")
        if probs is None:
            return refuse("FEATURE_MISMATCH")
        p_tp, p_sl, p_timeout = probs

        calibrated = bool(getattr(self.artifact, "calibrated", False))
        max_err = float(getattr(self.artifact, "calibration_max_abs_error", 1.0) or 1.0)
        # An uncalibrated probability is a number, not a probability. It is not
        # allowed to authorise a trade, and the dashboard is told not to dress
        # it up as confidence either.
        confidence_known = calibrated and max_err <= 0.10
        confidence = max(0.0, 1.0 - max_err) if confidence_known else 0.0

        ev = self._expected_value(
            p_tp=p_tp, p_sl=p_sl, p_timeout=p_timeout,
            notional_usd=notional_usd, round_trip_bps=round_trip_bps,
            confidence_known=confidence_known,
        )
        pred = Prediction(
            symbol=feats.symbol, ts_ms=ts, regime=regime_name, model=self.version,
            p_tp=p_tp, p_sl=p_sl, p_timeout=p_timeout,
            expected_net_win=ev["win"], expected_net_loss=ev["loss"],
            expected_value=ev["ev"], execution_cost=ev["cost"],
            confidence=confidence, confidence_known=confidence_known,
            decision="NO_TRADE",
        )

        threshold = float(getattr(self.artifact, "probability_threshold", 0.5))
        if not confidence_known:
            pred.reject_reason = "MODEL_UNCERTAIN"
        elif p_tp < threshold:
            pred.reject_reason = "LOW_PROBABILITY"
        elif ev["cost"] >= ev["gross"]:
            pred.reject_reason = "COST_TOO_HIGH"
        elif ev["ev"] <= 0:
            pred.reject_reason = "NEGATIVE_EV"
        elif ev["ev"] < float(getattr(self.artifact, "ev_threshold_usd", 0.0)):
            pred.reject_reason = "NEGATIVE_EV"
        else:
            pred.decision = "BUY"

        if pred.decision == "BUY":
            self.stats.buys += 1
        else:
            # A near miss is worth showing as WATCH rather than hiding: it is
            # how an operator sees the engine working rather than sleeping.
            if pred.reject_reason == "LOW_PROBABILITY" and p_tp >= threshold * 0.9:
                pred.decision = "WATCH"
                self.stats.watches += 1
            self.stats.reject(pred.reject_reason or "NEGATIVE_EV")
        return pred

    # ------------------------------------------------------------------ #

    def _predict(self, feats: Features) -> tuple[float, float, float] | None:
        import numpy as np

        names = list(getattr(self.artifact, "feature_names", ()) or ())
        if not names:
            return None
        row = self._feature_row(feats, names)
        if row is None:
            return None
        proba = self.artifact.predict_proba(np.asarray([row], dtype=float))
        arr = np.asarray(proba, dtype=float).reshape(-1)
        if arr.size == 1:                      # binary: P(TP)
            p_tp = float(arr[0])
            return p_tp, 1.0 - p_tp, 0.0
        if arr.size == 2:
            return float(arr[1]), float(arr[0]), 0.0
        total = float(arr.sum()) or 1.0
        return float(arr[0] / total), float(arr[1] / total), float(arr[2] / total)

    @staticmethod
    def _feature_row(feats: Features, names: list[str]) -> list[float] | None:
        """Pull the artifact's exact feature list out of a live Features block.

        Missing a feature is a hard failure, not a zero-fill: a model scored on
        a silently zeroed column is predicting about a market it was never shown.
        """
        extras = getattr(feats, "extras", {}) or {}
        row: list[float] = []
        for name in names:
            if hasattr(feats, name):
                value = getattr(feats, name)
            elif name in extras:
                value = extras[name]
            else:
                return None
            try:
                v = float(value)
            except (TypeError, ValueError):
                return None
            if v != v or v in (float("inf"), float("-inf")):
                return None
            row.append(v)
        return row

    def _expected_value(
        self, *, p_tp: float, p_sl: float, p_timeout: float,
        notional_usd: float, round_trip_bps: float, confidence_known: bool,
    ) -> dict[str, float]:
        """EV in dollars, after every cost line, with an uncertainty haircut."""
        target_bps = float(getattr(self.artifact, "net_target_bps", 0.0))
        stop_bps = float(getattr(self.artifact, "stop_bps", target_bps)) or target_bps
        cost = notional_usd * round_trip_bps / 10_000.0
        win = notional_usd * target_bps / 10_000.0
        loss = notional_usd * stop_bps / 10_000.0
        gross = p_tp * win
        ev = p_tp * win - p_sl * loss - cost
        if not confidence_known:
            ev = min(ev, 0.0)
        return {"ev": ev, "win": win, "loss": loss, "cost": cost, "gross": gross}

    # ------------------------------------------------------------------ #

    def payload(self, predictions: list[Prediction]) -> dict[str, Any]:
        """The dashboard block for the predictive engine."""
        a = self.artifact
        if a is None:
            return {
                "model_version": None,
                "ready": False,
                "load_error": self.load_error,
                "candidates": [p.as_dict() for p in predictions],
                "rejections": dict(self.stats.rejections),
                "explanations": [],
            }
        return {
            "model_version": getattr(a, "version", None),
            "model_name": getattr(a, "model_name", None),
            "feature_version": getattr(a, "feature_version", None),
            "spec_key": getattr(a, "spec_key", None),
            "trained_to_ms": getattr(a, "trained_to_ms", None),
            "probability_threshold": getattr(a, "probability_threshold", None),
            "ev_threshold_usd": getattr(a, "ev_threshold_usd", None),
            "calibration": getattr(a, "calibration", None),
            "oos": getattr(a, "oos", None),
            "ready": True,
            "candidates": [p.as_dict() for p in predictions],
            "rejections": dict(self.stats.rejections),
            "explanations": [
                p.explanation() for p in predictions if p.decision == "BUY"
            ],
        }
