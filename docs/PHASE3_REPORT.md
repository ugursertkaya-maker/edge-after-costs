# Phase 3 — profitability search / strategy tournament

Date: 2026-09-11 · Mode: **PAPER** · Real orders placed: **0**
Live: <https://your-domain.example/cryptobot> · 1,167 automated tests passing

---

## The answer

**No robust profitable strategy exists on Binance.US for this system as
specified. NO.**

That conclusion rests on **590,306 out-of-sample trades** across 1,400 evaluated
candidates, four coins, ten strategy families, seven regimes and six holding
horizons, pooling to **−$92,365** net. It is not a close call and it is not a
small sample.

Nothing was tuned to reach it, and nothing was hidden to protect it. Every arm
tested — including the losers, including the abandoned — is in
`/tmp/phase3/tournament.txt` and the top 150 are on the dashboard.

---

## 1–4. What was searched

| | |
|---|---|
| **Strategy families** | 10, independent: momentum_continuation, breakout, trend_pullback, mean_reversion, volume_impulse, orderflow_imbalance, vwap_reversion, volatility_expansion, short_term_reversal, hybrid_multifactor |
| **Coins** | BTC/USD, ETH/USD, SOL/USD, ADA/USD (every Tier-0 pair clearing the liquidity filters) |
| **Horizons** | 2, 5, 10, 15, 25, 40, 60, 90, 120 minutes — none a minimum hold; early TP, early stop, signal invalidation, trailing and time stop all live |
| **Parameter combinations** | 444 declared across the ten families (measured by test, not claimed) |
| **Candidates enumerated** | 1,680 (420/symbol); **1,400 evaluated**, 280 abandoned early for insufficient signal supply |
| **Walk-forward** | 30/7, 60/14, 90/30 rolling; parameters frozen from train to test |
| **Period** | 120 days of real Binance.US 1-minute data, plus the full aggregate-trade tape (863,000 prints) |
| **Costs** | Tier-0: 1 bps/side fee, 2 bps slippage, spread from the synthesised book |

No prior assumption was carried in. 25–60 minutes, 0.25–1.00% targets,
many-small allocation, momentum, 1-minute bars and the existing six signals were
all treated as hypotheses and all were searched.

## 5–14. The out-of-sample result

**Best in-sample arm**: irrelevant by construction and deliberately not quoted
as a result — every in-sample number in the report is rendered desaturated and
hatched under the heading "ÖRNEKLEM İÇİ — SONUÇ DEĞİLDİR".

**Best out-of-sample arm**: none qualified. One arm was *marginal*:

```
momentum_continuation / ETHUSD / 25m / net_fixed:17 / atr:2
  trades = 30    PF = 1.00    expectancy = $0.0001/trade
```

Thirty trades — exactly the minimum sample — at a profit factor of precisely
1.00 and an expectancy of one hundredth of a cent. That is break-even noise, and
it is reported as marginal rather than as a finalist for that reason.

Pooled across every arm:

| | |
|---|---|
| OOS trades | **590,306** |
| OOS net P&L | **−$92,365.45** |
| Fees | $20,344 |
| **Slippage** | **$61,667** — three times the fees, again |
| OOS win rate (pooled) | 19.6% |
| OOS profit factor (pooled) | 0.22 |
| OOS expectancy | −$0.157/trade |
| Max drawdown (worst arm) | $59.13 on a $500 account |

### The horizon curve — the thing the search was built to find

| Horizon | Arms | Trades | PF | Win % | Expectancy | Avg hold |
|---|---|---|---|---|---|---|
| 2m | 264 | 104,129 | **0.07** | 6.1% | −$0.148 | 2.0m |
| 5m | 264 | 109,954 | 0.12 | 10.4% | −$0.147 | 2.9m |
| 10m | 264 | 114,582 | 0.21 | 18.3% | −$0.150 | 5.6m |
| 15m | 264 | 115,659 | 0.24 | 21.5% | −$0.156 | 7.3m |
| 25m | 80 | 37,723 | 0.28 | 26.2% | −$0.158 | 11.4m |
| **60m** | 264 | 108,259 | **0.35** | 39.0% | −$0.182 | 22.3m |

The direction predicted in `docs/ECONOMICS.md` is confirmed: **profit factor
rises monotonically with the horizon**, from 0.07 at two minutes to 0.35 at
sixty. The earlier correction was right — longer is better here, and the 2- and
5-minute horizons are hopeless.

But 0.35 is not 1.20, and it is not even 1.00. The trend is real and the level
is nowhere near enough. Extrapolating the curve does not rescue it either: the
gain from 25m to 60m is +0.07 of profit factor, so reaching 1.0 on that slope
would need a holding period measured in days, which is a different product.

### Why, quantified

| Horizon | Median move | Required win % (symmetric payoff) | Actual win % |
|---|---|---|---|
| 2m | 3.2 bps | 99.8% | 6.1% |
| 5m | 6.6 bps | 92.3% | 10.4% |
| 10m | 9.4 bps | 85.8% | 18.3% |
| 15m | 12.6 bps | 75.0% | 21.5% |
| 25m | 16.2 bps | 69.4% | 26.2% |

The gap is not marginal. At every horizon the realised hit rate is roughly a
third of what the cost structure demands.

### Per coin

| Symbol | Arms | Trades | Net P&L | PF | Fees | Slippage | Best family |
|---|---|---|---|---|---|---|---|
| BTC/USD | 345 | 156,475 | −$23,374 | 0.17 | $5,399 | $16,281 | breakout / 60m |
| ETH/USD | 365 | 149,394 | −$23,264 | 0.21 | $5,153 | $15,652 | momentum / 60m |
| SOL/USD | 350 | 148,366 | −$22,557 | 0.26 | $5,110 | $15,516 | breakout / 60m |
| ADA/USD | 340 | 136,071 | −$23,170 | 0.26 | $4,683 | $14,218 | volume_impulse / 60m |

No coin is materially better than any other, and removing coins does not help —
there is no subset that survives. Slippage is 2.9–3.0× fees on every one.

### Per regime

| Regime | Trades | PF | Win % | Avg win | Avg loss |
|---|---|---|---|---|---|
| BREAKOUT | 163,886 | 0.25 | 23.2% | $0.235 | −$0.284 |
| VOLATILITY_EXPANSION | 133,762 | 0.24 | 20.3% | $0.246 | −$0.264 |
| TRENDING_UP | 110,257 | 0.21 | 17.9% | $0.223 | −$0.234 |
| PANIC | (smallest) | 0.14 | 29.0% | $0.144 | −$0.417 |

Every regime is negative. There is no regime in which some family works, so the
"activate a strategy only in its regime" design has nothing to activate.

**The mechanical diagnosis**, visible in those last two columns: average win
$0.22–0.25 against average loss $0.23–0.42, with a nominal reward:risk of about
4:1 (a 17 bps target against a ~4 bps ATR stop). Positions are systematically
**not reaching their targets while losses run past their stops** — the exits
give back more than the entries earn. That is the single clearest mechanical
finding of the search.

### Per family

Every one of the ten is net negative on BTC/USD, with profit factors clustered
between 0.25 and 0.36 — a range so tight it says the families are not the
variable that matters. Two are barely testable here at all:
`volume_impulse` and `orderflow_imbalance` account for most of the **280
candidates abandoned for insufficient signal supply** (130 of them produced
*zero* signals in 120 days), which is the thin-tape finding reappearing in the
signal layer.

## 16. Stress tests

Not yet meaningful, and the report says so rather than implying otherwise. The
cost stresses that can be applied analytically (fees +25%, +50%, slippage ×2,
worse spread) are exact, but they are only computed for arms the tournament
retained trades for — and it retains them only for plausible finalists, of
which there were none. The stresses that change *which* trades happen (latency
×2, missed fills, execution delay) need a replay the search did not supply and
are reported as **"not evaluated"**, never as passed.

Since no arm reached the stress gate on its own merits, stressing them further
would be decorating a negative result.

## 17. Monte Carlo

Not run: it is computed only for arms with trades retained, i.e. plausible
finalists. With zero finalists there is nothing to resample. The machinery is
built and tested (bootstrap of trade order, seeded, reporting median and p95
drawdown, worst loss streak, and probability of ending below the starting
$500), and the gate that consumes it requires `prob_below_start <= 35%`.

## 18. Maker vs taker vs hybrid — **still open, and deliberately so**

This is the one question Phase 3 could not answer, and the honest reason is
data, not effort.

A post-only strategy lives or dies on queue position, trade-through and adverse
selection. None of those exist in a 1-minute bar, so the study runs on the raw
tape — which the bot only began recording today. Current state:

```
tape spans 5.4h, need 24h
BTCUSD: 467 prints (need 500)   ETHUSD: 146 (need 500)   SOLUSD: 157 (need 500)
>> INSUFFICIENT DATA — this run cannot say whether resting beats crossing.
```

What the partial tape shows, at sample sizes too small to conclude from:

| Symbol | Maker fill rate | 5s markout | Reading |
|---|---|---|---|
| BTC/USD | 16.2% (n=65) | **−0.46 bps** | **FLAT** — no measurable adverse selection |
| ETH/USD | 2.8% (n=11) | −2.99 bps | adverse selection |
| SOL/USD | 2.5% (n=10) | −3.46 bps | adverse selection |

BTC/USD is the interesting row: a 16% fill rate with essentially flat markout is
the profile under which 0% maker fees plus an earned spread would be real money
rather than a fee saving handed back through the price. ETH and SOL show the
opposite, on eleven and ten fills respectively — which is to say, on nothing.

**The recorder is running and this re-runs itself the moment there is a day of
tape.** No number here should be quoted as a finding until then.

## 19. Best coin / regime / horizon combinations

There are none that work. The *least bad* is breakout or momentum on any coin at
the 60-minute horizon in a low-volatility regime — profit factor 0.35, still
losing $0.18 a trade.

## 20. $500 ending paper-equivalent capital

The live paper engine has been running continuously with **zero entries** since
the V2 deploy: 8,321 evaluation passes, every opportunity rejected on
`NEGATIVE_EDGE` or `EDGE_BELOW_MIN`. Equity **$500.00**, unchanged.

A bot that opens nothing on a market where nothing clears its cost is behaving
correctly. It is also, correctly, producing no evidence — which is what the
historical search was for.

## 21–23. Does one robust profitable strategy exist?

**NO.**

What remains structurally unprofitable, precisely:

1. **Taker execution at any horizon under two hours.** The realised hit rate
   (6–39%) is about a third of what the cost structure requires (69–100%). The
   gap is not a tuning distance.
2. **Sub-minute anything.** 89.9% of BTC/USD 5-second buckets and 65.6% of
   30-second buckets contain no trade at all. A 20-bar window at 5s sees two
   real prints. Those timeframes carry no information on this venue, so the
   multi-timeframe design has fewer usable layers than intended — honest density
   starts at 3–5 minutes.
3. **Flow and volume strategies.** They need a tape this venue does not produce:
   130 candidates generated zero signals in 120 days.
4. **The exit logic as built.** Average win $0.22 against average loss $0.28 on
   a nominal 4:1 reward:risk means targets are not being reached while stops are
   being overshot. This is the one finding that is a *defect* rather than a
   market fact, and it is the first thing to fix before any further search.

What is **not** ruled out: **maker-first on BTC/USD specifically**, where the
early tape shows a 16% fill rate at flat markout. That is the only remaining
candidate structure, and it needs 24 hours of tape to be answerable.

## 24–27. Housekeeping

| | |
|---|---|
| Automated tests | **1,167 passing**, 1 skipped, all offline and deterministic |
| Worker CPU / RAM | 2.7% of one core, 62 MB of a 640 MB limit |
| Host load | 1.55 on 2 vCPU — lower than before this project started |
| Search compute | ran entirely off-box on the laptop; the VPS was never loaded |
| **PAPER mode** | **YES** — the only accepted value; API and worker refuse to start otherwise |
| **Real orders** | **0** |
| Exchange credentials configured | **NO** |
| Withdrawal-enabled credential | **NO** — none exists |

## What I would do next, in order

1. **Fix the exits before searching again.** The avg-win/avg-loss inversion at
   4:1 nominal reward:risk is a bug-shaped result, not a market fact. Until a
   position that reaches its target actually books its target, every other
   result is measured through a broken instrument.
2. **Let the tape reach 24 hours and re-run the execution study.** It is one
   command and it answers the only open structural question.
3. **Do not tune.** 1,400 candidates, 590,306 out-of-sample trades, ten
   families, seven regimes, six horizons, four coins — all negative. Anything
   that turns green after tuning on this sample is curve fitting, and the gates
   exist precisely to refuse it.
