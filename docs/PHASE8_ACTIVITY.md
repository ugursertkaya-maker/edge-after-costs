# Activity study — which condition is suppressing setups?

Date: 2026-09-13 · Mode: **PAPER** · Real orders: **0**

## 1. Entry funnel — the first binding condition

`momentum_scalp`, 8,054 evaluations, **51 signals (0.633%)**:

| condition | count | share | threshold | median | p75 | p90 | p95 |
|---|---|---|---|---|---|---|---|
| REGIME_NOT_ALLOWED | 3,656 | **45.4%** | — | — | — | — | — |
| **ret_1m** | 3,013 | **37.4%** | 0.150 | **0.000** | −0.000 | −0.000 | 0.000 |
| volume_ratio | 709 | 8.8% | 1.150 | 1.000 | 1.000 | 1.000 | 1.000 |
| ema_slope | 548 | 6.8% | 0.060 | 0.006 | 0.036 | 0.051 | 0.056 |
| ret_5m_agree | 53 | 0.7% | 0.000 | −0.945 | −0.377 | −0.155 | −0.120 |
| trade_flow | 24 | 0.3% | 0.050 | −0.157 | −0.000 | 0.000 | 0.026 |

Other strategies: `breakout_scalp` 0.050% signal rate (regime 83.0%),
`trend_pullback` 0.037% (dist_to_ema 43.6%), `flow_impulse` 0.521%,
`book_imbalance` 0% (imbalance 71.4%), `mean_reversion` 0% (not_stretched 31.5%).

## 2. The regime gate is not a defect

| regime | share | | strategy | market covered |
|---|---|---|---|---|
| TRENDING | 44.1% | | momentum_scalp | 55.5% |
| RANGING | 20.2% | | book_imbalance | 81.7% |
| LOW_VOLATILITY | 17.5% | | flow_impulse | 55.6% |
| HIGH_VOLATILITY | 11.5% | | trend_pullback | 44.1% |
| ABNORMAL | 6.7% | | mean_reversion | 37.7% |
| BREAKOUT | 0.1% | | breakout_scalp | 17.6% |

`REGIME_NOT_ALLOWED` at 45.4% is the exact complement of momentum's 55.5%
coverage. That is the design, not a defect.

## 3. `ret_1m` cannot be relaxed

The threshold is 0.150; the **observed median is 0.000 and p90 is −0.000**. The
distribution is degenerate at zero. Lowering the threshold to 0.01 means
accepting minutes in which the price did not move at all — there is no move to
capture and about 8 bps to pay. This is not a threshold problem; it follows from
**80.4% of ETHUSD bars having high = low**.

## 4. The capacity cap — the single largest defect found

`capacity_max_usd` had existed since Phase 6 but was wired only to the decision
to **enter the universe**; it never bounded order size.

| profile | avg. position | | slippage (bps) | | NET $ | |
|---|---|---|---|---|---|---|
| | before | after | before | after | before | after |
| CURRENT | $1,352 | **$55** | 6.72 | 6.10 | −2,012 | **−43.91** |
| MODERATE | $1,167 | $55 | 6.51 | 6.08 | −2,698 | −65.06 |
| ACTIVE | $1,149 | $55 | 6.50 | 6.08 | −2,760 | −72.39 |
| HIGH_ACTIVITY | $1,144 | $55 | 6.48 | 6.09 | −2,786 | −77.53 |

ETHUSD absorbs roughly $22 a minute; a $1,352 order walks the book, and the
simulator was charging for that honestly. The clamp now lives in
`RiskEngine._apply_clamps` — the one gate both the live allocator and the
backtest pass through.

## 5. Activity / quality table (120 days, 3 symbols, capacity-respecting size)

| Profile | Setups/day | Trades/day | Utilisation | Turnover/day | NET $ | PF | Expectancy | Max DD |
|---|---|---|---|---|---|---|---|---|
| CURRENT | 74.4 | 7.12 | 1.10% | 0.078 | −43.91 | 0.21 | −0.0520 | 43.91 |
| MODERATE | 159.8 | 9.81 | 1.10% | 0.108 | −65.06 | 0.20 | −0.0559 | 65.06 |
| ACTIVE | 199.7 | 11.18 | 1.10% | 0.123 | −72.39 | 0.20 | −0.0546 | 72.39 |
| HIGH_ACTIVITY | 248.8 | 11.75 | 1.10% | 0.130 | −77.53 | 0.19 | −0.0556 | 77.53 |

**No profile is promoted.** None of them has a positive NET expectancy.

## 6. Answers

**1. Which condition is suppressing setups?**
In order: `REGIME_NOT_ALLOWED` (45.4%, by design) and `ret_1m` (37.4%). The
second is the one that binds and cannot be relaxed — its median is zero.

**2. How confidently can it be relaxed?**
Relaxation **works**: supply goes from 74 to 249 setups/day and trades from 7.1
to 11.8/day. And per-trade expectancy **does not degrade** (−0.0520 → −0.0556).
So the current tightness was not buying quality; it was only throttling
activity. But because every profile is negative, none of them is safe —
relaxing simply enlarges the loss.

**3. Which profile gives the highest sustainable activity?**
**None.** The rule requires a positive out-of-sample expectancy and a profit
factor above 1; the best here is PF 0.21.

**4. Realistic trades per day?**
**7–12 trades/day** across 3 symbols. Reaching 20–30+/day in this universe is
only possible by taking the thresholds to zero, and expectancy stays negative.

**5. Can it deploy meaningful capital?**
**No.** At capacity-respecting size, utilisation is **1.10%** — $55 of $5,000.
That is not a policy choice but the depth of the venue: ETHUSD absorbs about
$22 a minute, BTCUSD about $12, SOLUSD about $17. $4,000 of active capital is
not reachable on this exchange; forcing it multiplies slippage 25-fold and takes
the loss from −$43 to −$2,012 — which is exactly what this study measured.
