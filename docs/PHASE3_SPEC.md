# Phase 3 — profitability search / strategy tournament

## The mission, stated honestly

Search aggressively for a strategy that is **positive after costs and
out-of-sample**. If none exists, say so. Do not manufacture a positive backtest.

Every prior assumption is a hypothesis now: 25–60 minute holds, 0.25–1.00%
targets, many-small allocation, momentum, 1-minute bars, the existing six
signals. None of them is privileged.

**PAPER ONLY throughout. Real orders stay 0. Nothing here enables live
trading, and the paper-only guard still fails the build on any signing path.**

## What we already know (do not re-derive, do not ignore)

From `docs/FINAL_REPORT.md`, `docs/ECONOMICS.md`, `docs/V2_REPORT.md`:

* Binance.US BTC/USD: **55% of 1-minute bars contain zero trades**, ~$753 of
  volume per minute, 1m ATR ≈ **2.2 bps**, typical 60-minute move ≈ **17 bps**.
* Fees are near zero (Tier-0: 0% maker / 0.01% taker); **slippage, not fees,
  dominated every loss** — $52 of slippage against $16 of fees over 365 days.
* Every backtest and the walk-forward out-of-sample are net negative. Losses
  shrink monotonically as the holding horizon lengthens.
* The allocation comparison could not run: peak concurrent opportunities was
  **2**, and 99.5% of capital sat idle. The binding constraint is **opportunity
  supply**, not risk budget.
* Maker-first is the one structure never properly tested, because a 1-minute
  bar contains no queue position.

Two consequences shape Phase 3:

1. **The tape is now being recorded live** (`app/recorder.py`, `/data/tape` on
   the server, hourly gzip NDJSON, 14-day retention). Maker evaluation runs on
   that, not on bars.
2. Binance.US's thinness is, for once, an advantage for research: at ~3k trades
   a day, **the entire historical tape is small enough to download**. Full
   `aggTrades` history gives real sub-minute data instead of synthesised ticks.

## Hard rules

* **Optimise NET only.** gross − entry fee − exit fee − spread − slippage −
  missed-fill opportunity cost. Never rank on gross.
* **Walk-forward is mandatory.** Parameters chosen on train are frozen for test.
  Report aggregate out-of-sample, never the best window.
* **No leakage.** No future bar, no full-sample normalisation, no fitting a
  scaler on test data. Time-ordered splits only.
* **Report losers.** Every arm tested appears in the output, including the ones
  that failed. A suite where nothing works is a result.
* **No overfitting.** Prefer fewer parameters, wider plateaus, and results that
  survive cost stress. A parameter set that is only good at one exact value is
  noise.
* **Determinism.** Seeded RNG, no wall clock, no network in tests.
* **The host is small.** Research runs on a workstation against cached data.
  Do not add load to the deployment; the live paper engine keeps running.

## Promotion gates — a strategy is a FINALIST only if, OUT-OF-SAMPLE

1. NET expectancy per trade **> 0**
2. Profit factor **≥ 1.20** (preferred; a strategy between 1.0 and 1.2 may be
   reported as *marginal*, never as a finalist)
3. Max drawdown reasonable against a **$500** account
4. **Not dominated by one trade** — removing the single best trade must leave
   expectancy positive
5. **Multiple test windows positive** — a majority of walk-forward windows, not
   one lucky one
6. **Survives cost stress**: fees +25% and +50%, slippage ×2, latency ×2, worse
   spread, random execution delay
7. Minimum sample: **≥ 30 out-of-sample trades**

Do not lower these to find a winner.

## Package layout — `api/app/research/`

Each module is owned by exactly one workstream. Do not edit another's files.

| Module | Owner | Purpose |
|---|---|---|
| `dataset.py` | data | aggTrades → multi-timeframe bars + tape datasets, cached |
| `features_mtf.py` | features | the multi-timeframe feature block |
| `regimes.py` | features | the 7-regime classifier |
| `families.py` | strategies | ten independent, parameterised strategy families |
| `tournament.py` | search | the grid/walk-forward search and per-coin/regime tables |
| `robustness.py` | search | cost stress, Monte Carlo resampling, the gates |
| `maker_replay.py` | maker | queue-aware maker/taker/hybrid replay on the tape |
| `champion.py` | integration (me) | the selected strategy, versioned, for live paper |

Shared contracts live in `app/types.py` and `app/config.py` and are **frozen**.
Research modules may define their own local dataclasses freely, but anything
crossing a module boundary must be in the table below.

## Frozen cross-module contracts

```python
# app/research/dataset.py
@dataclass(frozen=True)
class Dataset:
    symbol: str
    bars: dict[str, list[Bar]]        # keyed by interval: "5s","15s","30s","1m","3m","5m","15m"
    trades: list[Trade]               # full tape, time-ordered
    start_ms: int
    end_ms: int

def load_dataset(symbol, start_ms, end_ms, *, intervals=(...), cache_dir=None) -> Dataset
def build_bars(trades: list[Trade], interval: str) -> list[Bar]      # pure
async def download_agg_trades(rest, symbol, start_ms, end_ms, *, cache_dir=None) -> list[Trade]

# app/research/features_mtf.py
@dataclass
class MTFFeatures:
    symbol: str
    ts_ms: int
    price: float
    values: dict[str, float]          # every named feature, already normalised
    def get(self, name: str, default: float = 0.0) -> float

FEATURE_NAMES: tuple[str, ...]        # every key values may contain
class MTFFeatureEngine:
    def __init__(self, *, settings=None, intervals=(...)) -> None
    def update(self, dataset: Dataset, index_ms: int) -> MTFFeatures
    def stream(self, dataset: Dataset, *, step: str = "1m") -> Iterator[MTFFeatures]

# app/research/regimes.py
class Regime7(str, enum.Enum):
    TRENDING_UP; TRENDING_DOWN; RANGING; VOLATILITY_EXPANSION
    LOW_VOLATILITY; BREAKOUT; PANIC
class Regime7Classifier:
    def classify(self, f: MTFFeatures) -> tuple[Regime7, float]   # (regime, confidence)

# app/research/families.py
@dataclass(frozen=True)
class ParamSpace:
    name: str
    grid: dict[str, tuple]            # param -> candidate values
    def combinations(self) -> Iterator[dict]

@dataclass
class ResearchSignal:
    family: str; symbol: str; side: Side; score: float; ts_ms: int
    reasons: list[str] = field(default_factory=list)

class StrategyFamily:
    name: str
    param_space: ParamSpace
    allowed_regimes: frozenset[Regime7] | None      # None = all, decided by data
    def signal(self, f: MTFFeatures, params: dict) -> ResearchSignal | None

FAMILIES: tuple[StrategyFamily, ...]  # exactly the ten in section 4

# app/research/tournament.py
@dataclass(frozen=True)
class Candidate:
    family: str; symbol: str; params: dict; horizon_seconds: int
    exit_profile: str; stop_profile: str; regime: Regime7 | None
    def key(self) -> str

@dataclass
class ArmResult:
    candidate: Candidate
    trades: list[ClosedTrade]
    stats: PerfStats
    fees_usd: float; slippage_usd: float
    windows: list[PerfStats]          # per walk-forward window, out-of-sample
    in_sample: PerfStats
    out_of_sample: PerfStats

def run_tournament(datasets, *, config, on_progress=None) -> list[ArmResult]
def per_coin_table(results) -> str
def per_regime_table(results) -> str
def per_horizon_table(results) -> str

# app/research/robustness.py
@dataclass
class StressResult:
    label: str; stats: PerfStats; survived: bool
@dataclass
class MonteCarloResult:
    median_drawdown: float; p95_drawdown: float
    worst_loss_streak: int; prob_below_start: float; samples: int
@dataclass
class GateReport:
    candidate_key: str
    passed: bool
    reasons: list[str]                # one line per gate, pass or fail
    stress: list[StressResult]
    monte_carlo: MonteCarloResult | None

def stress_suite(arm: ArmResult, *, replay) -> list[StressResult]
def monte_carlo(trades: list[ClosedTrade], *, equity: float, samples: int = 2000,
                seed: int = 7) -> MonteCarloResult
def evaluate_gates(arm: ArmResult, *, equity: float = 500.0, replay=None) -> GateReport
def finalists(arms: list[ArmResult], **kw) -> list[GateReport]

# app/research/maker_replay.py
@dataclass
class MakerOutcome:
    filled: bool; fill_price: float; fill_qty: float
    wait_ms: int; queue_ahead_usd: float
    missed_reason: str | None
    adverse_selection_bps: float      # markout against us after the fill
@dataclass
class ExecutionComparison:
    taker: PerfStats; maker: PerfStats; hybrid: PerfStats
    maker_fill_rate: float; hybrid_cross_rate: float
    markout_bps: dict[str, float]     # horizon label -> mean markout

class MakerReplay:
    def __init__(self, *, settings=None, tape_dir=None) -> None
    def simulate_quote(self, *, symbol, side, price, qty, from_ms, timeout_ms,
                       book_state, tape) -> MakerOutcome
    def compare(self, arm, dataset, *, tape) -> ExecutionComparison
```

## Objective function

Rank candidates by out-of-sample **NET expectancy per trade**, with profit
factor and drawdown as gates rather than as terms. Tie-break toward fewer
parameters and a wider plateau — a peak that exists only at one exact parameter
value is noise, and saying so in the report is part of the job.
