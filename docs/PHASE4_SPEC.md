# Phase 4 — predictive entry engine

## The problem this phase exists to fix

Every prior phase tuned exits around a weak entry. Phase 3 settled that this is
a dead end: the exit sweep raised profit factor monotonically with target size
and still topped out at **0.54**, and raising the entry score threshold from 0.0
to 0.9 cut the trade count 24-fold while moving expectancy from −$0.158 to
−$0.145. The conviction score ranks **how loudly a feature fired**, not whether
the trade ends well.

So stop tuning TP/SL. Replace the entry decision with a direct estimate of:

> *Given the current market state, what is the probability this trade reaches a
> profitable NET exit before its stop, within the horizon — and what is the
> expected value after costs?*

Trade only when expected value is positive **and** robust.

**PAPER ONLY. Real orders stay 0.** Nothing in this phase requests, stores or
uses exchange trade credentials, and the paper-only guard still fails the build
on any signing path.

## What is already established — do not re-derive, do not contradict

From `docs/ECONOMICS.md`, `docs/FINAL_REPORT.md`, `docs/V2_REPORT.md`,
`docs/PHASE3_REPORT.md`:

* Binance.US BTC/USD: 1m ATR ≈ 2.2 bps; median |move| 12.9 bps at 25 min,
  19.7 bps at 60 min. 51% of 1-minute buckets and 90% of 5-second buckets
  contain no trade at all.
* Round trip ≈ 3–6 bps. **NET targets below ~10 bps produced literally zero
  trades in 120 days** — they never clear their own cost. Do not test below it.
* Slippage is ~3× fees in every run.
* 2,852 candidates searched across Phase 3; pooled OOS profit factor 0.22.
* Flow/volume families are barely testable: 130 candidates produced zero signals
  in 120 days.
* Maker-first remains the only unfalsified execution structure. Tape collection
  is running and is at ~10 hours of the 24 the study needs.

## Blocking defect — fix before anything is trained or promoted

Phase 3 produced one apparent finalist:

```
volume_impulse|BTCUSD|...|h=3600|x=net_fixed:75|s=vol:3|r=VOLATILITY_EXPANSION
OOS: 79 trades, PF 1.26, expectancy +$0.0697, net +$5.51
```

It reported 7/7 gates but **three of those gates were computed over the wrong
trade set**. The evidence, from its own output:

* gate 4 says the best trade was "$+1.41, **10% of all net P&L**" — implying a
  set whose net is ≈ **$14.10**, against a reported OOS net of **$5.51**;
* the stress rows report "fees +25%" at a net of **$14.32**, which is *higher*
  than the unstressed $5.51 it should be degrading. **Raising fees cannot raise
  net P&L.**

So gates 3 (Monte Carlo), 4 (not dominated by one trade) and 6 (cost stress) ran
over the arm's retained trade list — in-sample and out-of-sample together —
rather than over its 79 out-of-sample trades. A 7/7 that is really a 4/7 is not
a pass. **The finalist must not be promoted until this is fixed and it is
recomputed.**

## Hard rules

* **NET of costs, always.** A +0.25% move is not a winner if fee + fee + spread
  + slippage leaves less than the required NET target. Labels, EV and gates all
  work in net terms.
* **Expected value, not win rate.** `EV = P_win·avg_net_win − P_loss·avg_net_loss
  − timeout/execution penalty`. A high win rate with a bad payoff is a losing
  strategy and the gate must say so.
* **Chronological splits only.** No random shuffling, ever.
* **Purge and embargo are mandatory.** Labels look forward up to the maximum
  horizon, so training samples whose label window overlaps validation or test
  must be dropped. Without this every result is leaked.
* **Entry and exit are selected separately**, both on train/validation, both
  frozen before test. The test window is touched once.
* **Calibration must be measured**, not assumed. A 70% prediction must win ~70%
  of the time.
* **The existing deterministic score becomes one feature**, not an authority.
* **Do not manufacture profitability.** Search aggressively; report honestly.
  "No predictive edge exists" is an acceptable and likely answer.

## Production constraint — inference must be light

Research trains with scikit-learn on a workstation. The worker container must not
need it. Therefore the champion artifact is a **numpy-only inference bundle**:

* linear models export coefficients and the scaler;
* tree ensembles export a flat `TreeEnsembleSpec` (arrays of feature index,
  threshold, left/right child, leaf value) evaluated by a numpy predictor.

A test must assert the exported predictor reproduces sklearn's `predict_proba`
to within 1e-9 on held-out rows. If a model cannot be exported, it cannot be a
champion.

## Package layout — `api/app/predict/`

| Module | Owner | Purpose |
|---|---|---|
| `labels.py` | labels | triple-barrier, cost-aware, multi-horizon labelling |
| `splits.py` | labels | chronological splits with purge + embargo |
| `matrix.py` | features | leakage-safe aligned feature matrix, cached |
| `models.py` | models | the five benchmarked models, training, selection |
| `calibrate.py` | models | Brier, log loss, reliability, Platt/isotonic |
| `ev.py` | ev | expected value, thresholds, EV gate, uncertainty margin |
| `artifact.py` | ev | the numpy-only inference bundle + versioning |
| `engine.py` | integration (me) | the live entry engine that consumes an artifact |

## Frozen cross-module contracts

```python
# app/predict/labels.py
class Outcome(str, enum.Enum):
    TP_BEFORE_SL = "TP_BEFORE_SL"; SL_BEFORE_TP = "SL_BEFORE_TP"
    TIMEOUT = "TIMEOUT"; NO_EDGE = "NO_EDGE"

@dataclass(frozen=True)
class BarrierSpec:
    net_target_pct: float          # NET of every cost, e.g. 0.15 == 0.15%
    stop_kind: str                 # "atr" | "vol" | "structure" | "fixed_net"
    stop_value: float
    horizon_minutes: int
    round_trip_bps: float          # the cost the label must clear
    def key(self) -> str

@dataclass(frozen=True)
class Label:
    symbol: str; ts_ms: int; spec_key: str
    outcome: Outcome
    net_return_bps: float          # realised NET at the exit the label implies
    hit_ms: int                    # when the barrier was touched (0 = timeout)
    entry_price: float; tp_price: float; sl_price: float
    label_end_ms: int              # for purge/embargo

def build_labels(bars, *, spec, symbol, atr_bps_series=None) -> list[Label]
def label_matrix(bars, *, specs, symbol, **kw) -> dict[str, list[Label]]

# app/predict/splits.py
@dataclass(frozen=True)
class Split:
    name: str
    train: tuple[int, int]; validate: tuple[int, int]; test: tuple[int, int]
    purged: int; embargoed: int

def rolling_splits(start_ms, end_ms, *, train_days, validate_days, test_days,
                   step_days, max_horizon_minutes, embargo_minutes=0) -> list[Split]
def apply_purge(labels, split, *, max_horizon_minutes, embargo_minutes) -> tuple[list, list, list]

# app/predict/matrix.py
@dataclass
class FeatureMatrix:
    X: "np.ndarray"; y: "np.ndarray"; ts_ms: "np.ndarray"
    symbols: list[str]; feature_names: list[str]; label_end_ms: "np.ndarray"
    def slice_ms(self, lo: int, hi: int) -> "FeatureMatrix"

FEATURE_GROUPS: dict[str, tuple[str, ...]]   # momentum|volatility|volume|book|regime|cost|time|score
def build_matrix(dataset, labels, *, groups=None, cache_dir=None) -> FeatureMatrix

# app/predict/models.py
MODEL_NAMES = ("logistic", "logistic_l2", "gbm", "random_forest", "hist_gbm")
@dataclass
class TrainedModel:
    name: str; estimator: object; feature_names: list[str]
    scaler_mean: "np.ndarray | None"; scaler_scale: "np.ndarray | None"
    def predict_proba(self, X) -> "np.ndarray"
def train_model(name, Xtr, ytr, *, seed=7, **kw) -> TrainedModel
def benchmark(matrix, split, *, names=MODEL_NAMES, seed=7) -> list[ModelReport]

# app/predict/calibrate.py
@dataclass
class CalibrationReport:
    brier: float; log_loss: float; auc: float; pr_auc: float
    bins: list[tuple[float, float, int]]        # (predicted, actual, n)
    max_abs_error: float; calibrated: bool; method: str
def evaluate_calibration(p, y, *, bins=10) -> CalibrationReport
def fit_calibrator(p_val, y_val, *, method="isotonic") -> Calibrator

# app/predict/ev.py
@dataclass(frozen=True)
class EVInputs:
    p_win: float; p_loss: float; p_timeout: float
    avg_net_win_usd: float; avg_net_loss_usd: float
    cost_usd: float; uncertainty_usd: float
@dataclass(frozen=True)
class EVResult:
    expected_value_usd: float; expected_value_bps: float
    margin_usd: float; eligible: bool; reject: str | None
def expected_value(inputs) -> EVResult
def threshold_search(p, y, net_returns, *, thresholds, cost_bps) -> list[ThresholdReport]

# app/predict/artifact.py
@dataclass(frozen=True)
class PredictiveArtifact:
    version: str; model_name: str; feature_names: tuple[str, ...]
    spec_key: str; probability_threshold: float; ev_threshold_usd: float
    trained_to_ms: int; feature_version: str
    payload: dict          # numpy-only: coefficients or TreeEnsembleSpec arrays
    def predict_proba(self, X) -> "np.ndarray"     # numpy only, no sklearn
def export(model, *, ...) -> PredictiveArtifact
def save(artifact, path) / def load(path) -> PredictiveArtifact
```

## Rejection reasons (item 34)

`LOW_PROBABILITY`, `NEGATIVE_EV`, `COST_TOO_HIGH`, `MODEL_UNCERTAIN`,
`CORRELATED_RISK`, `SPREAD_TOO_WIDE`, `NO_LIQUIDITY`, `STALE_DATA`,
`REGIME_NOT_SUPPORTED`. Every rejection is counted, so the report can show
whether the engine is finding edge or simply refusing everything.

## Promotion gates (item 26) — on top of Phase 3's seven

Out-of-sample: NET P&L > 0; PF ≥ 1.20; EV/trade > 0; reasonable drawdown;
≥ 30 trades; **calibration acceptable** (max absolute reliability error ≤ 0.10);
positive across a majority of test windows; not dominated by one trade, one day
or one coin; survives cost stress. In-sample performance promotes nothing.
