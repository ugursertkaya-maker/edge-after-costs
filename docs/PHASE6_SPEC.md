# Phase 6 — multi-asset selection, strategy-specific holding, capital recycling

**PAPER ONLY. Real orders = 0.** No live credentials, no trading permission.

## What is actually true before we start

The brief's premise is half right, and the half that is wrong matters.

**BTC is not dominating by share.** Over the 139 live trades: BTCUSD 58
(41.7%), ETHUSD 51 (36.7%), SOLUSD 30 (21.6%). That is not a BTC-only trader
and the `BTC_CONCENTRATION` flag at 70% would not have fired.

**But the universe is three coins out of 253 tradable pairs**, and that is the
real complaint. `UNIVERSE_RESTRICT_TIER0=true` reduces the eligible set to the
four Tier-0 pairs; ADAUSD is the fourth and it has never traded. The
concentration is not in the ranker preferring BTC, it is upstream, in what is
allowed to be a candidate at all.

Phase 6 must prove which of those two it is **with per-symbol funnel counts**,
not assert it.

## Hard rules

* No coin quotas. No "trade at least N coins". Diversification is an *outcome*
  of honest ranking, never an input.
* No universal minimum hold. Holding policy is
  `f(strategy, regime, signal decay, net P&L, volatility, execution state)`.
* `flow_impulse` is **not promoted** in this phase. 48 trades is a candidate.
  Gates report at 100 / 200 / 500 trades.
* Never fake a short. Spot is long-only; short signals are *recorded*, not
  executed.
* Never "price touched = filled" in any maker model.
* Do not suppress BTC. If BTC wins on net EV, BTC trades.

## Ownership — one owner per file, no shared edits

| Files | Owner | Scope |
|---|---|---|
| `app/research/funnel.py`, `scripts/funnel_audit.py` | **integration (me)** | Part A §1 per-symbol funnel |
| `app/universe.py` | **integration (me)** | §2 tiers, §3 liquidity normalisation |
| `app/exits.py`, `app/holding.py` | **agent: holding** | Part B §7–§10 |
| `app/portfolio.py`, `app/queue.py` | **agent: capital** | Part C §11–§15 |
| `web/**` | **agent: dashboard** | §21, §22, §20 panels |
| `app/research/maker_replay.py`, `scripts/flow_execution_study.py` | **integration (me)** | §18, §19 |
| `app/worker/main.py` | **integration (me)** | wiring only — nobody else edits this |

## Frozen contracts

```python
# app/research/funnel.py
STAGES = (
    "discovered", "subscribed", "data_healthy", "features", "strategy_evaluated",
    "model_evaluated", "candidate", "cost_gate", "liquidity_gate", "risk_gate",
    "correlation_gate", "ranked", "selected", "traded",
)

@dataclass
class SymbolFunnel:
    symbol: str
    stages: dict[str, int]              # every key in STAGES, monotone non-increasing
    drop_reasons: dict[str, dict[str, int]]   # stage -> reason -> count
    def as_dict(self) -> dict: ...

@dataclass
class FunnelReport:
    symbols: dict[str, SymbolFunnel]
    def bottleneck(self, symbol: str) -> tuple[str, str]: ...   # (stage, reason)
    def as_dict(self) -> dict: ...

# app/holding.py  -- strategy-specific position management
@dataclass(frozen=True)
class HoldingProfile:
    strategy: str
    min_hold_seconds: float          # 0.0 for flow_impulse; never a global floor
    typical_hold_seconds: float
    max_hold_seconds: float
    decay_exit: bool                 # exit when the originating signal decays
    decay_halflife_seconds: float
    trail_after_net_bps: float
    reeval_entry_score: bool         # False for slow strategies: entry != management

PROFILES: dict[str, HoldingProfile]   # keyed by strategy name
def profile_for(strategy: str) -> HoldingProfile: ...

# app/queue.py -- the live opportunity board
@dataclass
class QueuedOpportunity:
    symbol: str; strategy: str; episode_id: str
    net_ev_bps: float; ranked_at_ms: int; expires_ms: int
def revalidate(q: QueuedOpportunity, *, book, features, fees) -> Opportunity | None

# app/portfolio.py additions
class IdleReason(str, enum.Enum):        # existing members plus:
    COOLDOWN = "COOLDOWN"
    SCANNER_DELAY = "SCANNER_DELAY"
    CAPITAL_STATE_BUG = "CAPITAL_STATE_BUG"
    OTHER = "OTHER"

@dataclass
class RecycleEvent:
    closed_trade_id: str
    closed_ms: int
    capital_released_ms: int
    rescan_triggered_ms: int
    next_entry_ms: int | None
    qualified_candidate_existed: bool
    @property
    def close_to_rescan_ms(self) -> int: ...
    @property
    def close_to_entry_ms(self) -> int | None: ...
```

## Episode identity (§14)

A symbol may be re-entered only on a genuinely new episode:

```
episode_id = sha1(symbol | strategy | regime | signal_bucket | monotone_counter)
```

where `signal_bucket` quantises the triggering feature so that the *same*
impulse does not mint a new id, and the counter advances only when the
strategy's trigger condition goes false and then true again. Re-entering the
same `episode_id` is a defect, not a policy choice.

## Holding profiles — starting values, to be replaced by measurement

| Strategy | min | typical | max | decay exit | re-eval entry score |
|---|---|---|---|---|---|
| flow_impulse | **0 s** | 30 s | 180 s | **yes**, halflife 20 s | yes |
| book_imbalance | 0 s | 120 s | 600 s | yes, halflife 90 s | yes |
| momentum_scalp | 60 s | 600 s | 2400 s | no | **no** |
| breakout_scalp | 60 s | 900 s | 3600 s | no | **no** |
| trend_pullback | 120 s | 1500 s | 3600 s | no | **no** |
| mean_reversion | 60 s | 600 s | 2400 s | no | **no** |

`min_hold_seconds` is an anti-churn floor for the slow strategies only. It is
**zero** for the flow strategies, by design: a 30-second `flow_impulse` trade is
correct when the impulse is spent.

## Reporting gates for flow_impulse (§24)

Report, never auto-promote, at 100 / 200 / 500 trades: net PF, net expectancy,
win rate, median trade, max drawdown, coin concentration, day concentration,
regime concentration, execution-style performance, **alpha vs market drift over
the identical holding interval** (`scripts/beta_check.py`).
