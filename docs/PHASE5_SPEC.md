# Phase 5 — aggressive capital utilisation

## The ask

A $500 paper account running 3 positions with most capital idle is not the
intended strategy. Target **8–15 simultaneous positions** (hard ceiling 20),
**60–85% deployed capital**, many small positions ($10–$50), capital rotating
continuously.

**PAPER ONLY. Real orders = 0. No leverage.** Aggressive means more qualified
opportunities, never a lower bar — a negative expected-value trade stays NO
TRADE.

## What is already measured, and what it implies

The position cap is **not** the binding constraint and has not been since V2:
`portfolio.max_open_positions` is already 25 and the risk budget 3%. Phase 3
measured peak concurrent opportunities of **2** with **99.5% of capital idle**
across every allocation mode — few-large, many-small and dynamic produced
byte-identical books because there was nothing to allocate across.

The venue, measured 2026-09-11: **253 TRADING USD/USDT pairs**. At the current
liquidity bar (24h quote volume ≥ $100k, ≥ 200 trades) **21 pass — 4 of them
Tier-0**. The bot scans 3.

So the universe *is* artificially narrow and expanding it is the right lever.
But the expansion has a price, and the price is the whole question:

| | Tier-0 (BTC/ETH/SOL/ADA USD) | Non-Tier-0 (BTCUSDT, ETHUSDT, …) |
|---|---|---|
| Fee | ~0% maker / 0.01% taker | 0.10% / 0.10% |
| Round trip | **~2 bps** | **~20 bps** |
| Liquidity | lower (BTCUSD $1.4M/24h) | higher (BTCUSDT $4.9M/24h) |

A 20 bps round trip is the whole of BTC's typical 60-minute move (19.7 bps). So
the expansion plausibly increases **scanned** opportunities without increasing
**qualified** ones.

**That is a prediction, not a finding.** Phase 5 measures it. The measurement
that settles it is the idle-capital attribution: if idle cash is dominated by
`EXECUTION_COST`, the expansion cannot help and the constraint is the venue. If
it is dominated by `NO_QUALIFIED_SIGNAL`, more markets is exactly the fix.

## Hard rules

* **Aggressive ≠ lower bar.** Do not relax the EV gate, the probability
  threshold or the net-edge floor to manufacture activity. Increase supply
  instead: more liquid pairs, more strategies, more regimes, more horizons,
  maker/taker/hybrid execution, better allocation.
* **Do not force 8 positions if only 4 qualify.** The deployment bands are
  targets, not quotas.
* **Correlated longs are one bet.** Ten crypto longs expressing the same
  broad-market move must be sized as such — but correlation must not become an
  excuse to keep 80% of capital idle either. Report both.
* **Every idle dollar has a reason.** `NO_QUALIFIED_SIGNAL`,
  `CORRELATION_LIMIT`, `RISK_LIMIT`, `LIQUIDITY_LIMIT`, `EXECUTION_COST`,
  `CASH_RESERVE`. If $350 is idle, the dashboard says exactly why.
* **The missed-opportunity audit uses future data for post-hoc analysis only,
  never for a live decision.** Any code path where a label could reach the
  entry decision is a leak and a defect.

## Workstreams and ownership

| Module | Owner | Purpose |
|---|---|---|
| `app/universe.py` (extend) | universe | HOT/WARM/COLD tiers over the whole venue |
| `app/portfolio.py` (extend) | capital | idle-capital attribution, deployment targeting |
| `app/research/missed.py` | audit | MISSED_WINNER vs CORRECT_REJECTION |
| `app/research/profiles.py` | profiles | the three-profile paper comparison |
| `web/` | dashboard | capital utilisation, impossible to miss |
| `app/worker/main.py` | integration (me) | wiring, and the live comparison |

## Frozen contracts

```python
# app/universe.py — additions only, existing behaviour unchanged
class Tier(str, enum.Enum):
    HOT = "hot"; WARM = "warm"; COLD = "cold"

@dataclass(frozen=True)
class TierPolicy:
    hot_max: int = 8          # scanned every cycle
    warm_max: int = 20        # scanned every N cycles
    cold_max: int = 40        # refreshed on the slow loop only
    warm_every: int = 5
    cold_every: int = 60

def tier_for(entry: UniverseEntry, policy: TierPolicy, rank: int) -> Tier

# app/portfolio.py — additions only
class IdleReason(str, enum.Enum):
    NO_QUALIFIED_SIGNAL = "NO_QUALIFIED_SIGNAL"
    CORRELATION_LIMIT   = "CORRELATION_LIMIT"
    RISK_LIMIT          = "RISK_LIMIT"
    LIQUIDITY_LIMIT     = "LIQUIDITY_LIMIT"
    EXECUTION_COST      = "EXECUTION_COST"
    CASH_RESERVE        = "CASH_RESERVE"

@dataclass
class IdleAttribution:
    total_idle_usd: float
    by_reason: dict[str, float]        # sums to total_idle_usd
    deployed_usd: float
    deployed_pct: float
    target_pct: float
    def as_dict(self) -> dict

def attribute_idle(state, *, ranked, rejections, target_pct, equity) -> IdleAttribution

# app/research/missed.py
@dataclass
class MissedAudit:
    windows: int
    rejections_examined: int
    missed_winners: int
    correct_rejections: int
    missed_net_bps: float              # what the rejected moves went on to do
    by_reason: dict[str, tuple[int, int]]   # reason -> (missed, correct)
def audit_rejections(dataset, rejections, *, horizon_minutes, round_trip_bps) -> MissedAudit

# app/research/profiles.py
PROFILES = ("standard", "aggressive", "high_activity")   # 3% / 5% / 7.5% open risk
@dataclass
class ProfileResult:
    name: str
    avg_positions: float; max_positions: int
    avg_deployed_pct: float; avg_idle_pct: float
    idle_by_reason: dict[str, float]
    trades_per_day: float; turnover_per_day: float
    avg_position_usd: float; avg_hold_seconds: float
    gross_pnl: float; fees: float; slippage: float; net_pnl: float
    profit_factor: float; expectancy: float; max_drawdown: float
    largest_loss: float; daily_loss_events: int
def compare_profiles(datasets, *, cfg) -> list[ProfileResult]
```

## Selection rule

Pick the **most aggressive** profile that still has positive out-of-sample
expectancy, positive net P&L after all costs, and an acceptable drawdown on a
$500 account. Not the one with the largest gross P&L — gross is not a result.

If no profile has positive out-of-sample expectancy, say so, and report the
idle-capital attribution anyway: knowing *why* the money is not working is the
deliverable even when the answer is that it should not be.
