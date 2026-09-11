# Phase 3 — profitability search / strategy tournament

Date: 2026-09-11 · Mode: **PAPER** · Real orders placed: **0**
Live: <https://your-domain.example/cryptobot> · 1,167 automated tests passing

---

## The answer

**No robust profitable strategy was found. NO — with one axis still being
measured, named below rather than glossed over.**

The search covered **590,306 out-of-sample trades** across 1,400 evaluated
candidates, four coins, ten strategy families, seven regimes and six holding
horizons, pooling to **−$92,365** net. On the family, parameter, horizon,
regime and coin axes the answer is unambiguous and the sample is large.

**That run did not vary the exit and stop geometry.** All 1,400 arms ran the
baseline pair — a 17 bps net target against an `atr:2` stop. That stop is
horizon-scaled (`mult × 1m-ATR × √horizon`), so at the 60-minute baseline it is
≈ **34 bps**: a reward:risk of **0.5 : 1** needing a **~67% hit rate** to break
even, which nothing reached.

A second sweep (below) varied exactly that axis across 17 exit profiles and 11
stop profiles — 1,452 more arms. **One arm came back clearing all seven gates.
It is not being promoted, and section 15b explains why in detail:** three of its
seven gates were computed against a different trade set than the one they
report on, and one hit out of ~2,850 candidates is what noise produces.

Nothing was tuned to reach this, and nothing was hidden to protect it. Every arm
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

**What those last two columns actually say.** Average win $0.22–0.25 against
average loss $0.23–0.42 is a ratio of roughly **0.79**. Set against the
baseline's nominal reward:risk of **0.5 : 1** (17 bps target, ≈34 bps
horizon-scaled stop), that is *better* than the geometry implies — the trailing
and time exits are cutting some losses short rather than letting them run.

So there is no exit defect here. The arms lose because a 0.5 : 1 payoff needs a
~67% hit rate and the best family managed 39%. That is a statement about the
signal and the geometry together, not about the exit machinery, and it is the
reason the second sweep varies the geometry rather than the signal.

### Per family

Every one of the ten is net negative on BTC/USD, with profit factors clustered
between 0.25 and 0.36 — a range so tight it says the families are not the
variable that matters. Two are barely testable here at all:
`volume_impulse` and `orderflow_imbalance` account for most of the **280
candidates abandoned for insufficient signal supply** (130 of them produced
*zero* signals in 120 days), which is the thin-tape finding reappearing in the
signal layer.

## 15b. The exit/stop sweep — one apparent finalist, and why it was not promoted

The main tournament held the exit and stop fixed. A second sweep varied them:
**1,452 arms** on BTC/USD and SOL/USD, 120 days, all 17 exit profiles × 11 stop
profiles × 9 horizons × 6 entry-score thresholds × 8 regime gates, four families
(breakout, momentum_continuation, volume_impulse, mean_reversion).

One arm cleared every gate:

```
volume_impulse / BTCUSD / 60m / net_fixed:75 / vol:3 / VOLATILITY_EXPANSION / q0.35
  OUT OF SAMPLE   79 trades   win 54.4%   PF 1.26   expectancy +$0.0697   net +$5.51
  IN SAMPLE      357 trades   win 46.2%   PF 1.11   expectancy +$0.0314   net +$11.21
  max drawdown $4.29   Monte Carlo p95 drawdown $4.28   P(end below $500) 0.2%
  gates: 7/7 PASS
```

A 75 bps target against a ~51 bps stop (`vol:3` ≈ 3 × horizon-scaled vol) is a
**1.47 : 1** payoff — the opposite geometry to the baseline, and exactly the
direction the analysis predicted would be needed. The mechanism is coherent:
take a volume impulse only during a volatility expansion, aim well past the
typical hourly move, and accept a low hit rate for a favourable payoff.

**It is still not being promoted. Three reasons, in order of weight.**

**1. Three of the seven gates were computed against a different trade set than
they report on.** Gate 4 states the best trade was "$+1.41, 10% of all net P&L",
which implies a set whose net is ≈ **$14.10** — but the reported out-of-sample
net is **$5.51**. The fee-stress rows tell the same story: "fees +25%" reports a
net of **$14.32**, which is *higher* than the unstressed $5.51 it is supposed to
be degrading. Raising fees cannot raise net P&L. So gates 3, 4 and 6 — the
not-dominated-by-one-trade test, the Monte Carlo, and the cost stress — ran over
the arm's retained trade list (in-sample and out-of-sample together), not over
the 79 out-of-sample trades whose numbers are in the headline. **Those three
gates therefore do not verify the out-of-sample result**, and a 7/7 that is
really a 4/7 is not a pass. This is a defect in the gate plumbing, not in the
strategy, and it is the first thing to fix.

**2. One hit in ~2,850 candidates is what noise produces.** Across both runs
2,852 candidates were evaluated. Searching that many arms and finding a single
one above a PF-1.20 bar is not evidence of an edge; it is the multiple
comparisons problem stated out loud. The gates were designed to be hard, but
they were applied once per arm, and nothing in them corrects for having asked
the question 2,852 times.

**3. Its out-of-sample result beats its in-sample result** (PF 1.26 vs 1.11,
expectancy $0.070 vs $0.031). A genuine edge normally degrades from train to
test. An edge that *improves* is more consistent with having drawn a favourable
sample than with having found structure.

Two further caveats worth stating plainly: the three stresses that were **not**
evaluated — latency ×2, missed fills, random execution delay — are precisely the
ones that matter most for a family whose thesis is reacting quickly to a volume
spike. And the economics are modest even if real: **+$5.51 on $500 over 120
days**, about 1.1%, at $0.07 a trade.

### The three curves the sweep was run for (brief items 9, 10, 11)

**Exit profile — bigger targets are uniformly better, and the cost wall is
visible.**

| Exit profile | Arms | Trades | PF | Win % | Expectancy | Avg hold |
|---|---|---|---|---|---|---|
| net_fixed:3 / :5 / :8 | 133 | **0** | — | — | — | — |
| net_fixed:12 | 35 | 10,331 | 0.28 | 32.6% | −$0.158 | 17.3m |
| net_fixed:17 *(baseline)* | 403 | 146,555 | 0.32 | 33.1% | −$0.161 | 18.9m |
| net_fixed:25 | 32 | 8,178 | 0.41 | 36.1% | −$0.153 | 22.8m |
| net_fixed:50 | 47 | 11,572 | 0.41 | 27.6% | −$0.142 | 20.8m |
| net_fixed:75 | 46 | 12,063 | 0.50 | 30.7% | −$0.137 | 29.1m |
| **net_fixed:110** | 45 | 6,857 | **0.54** | 31.4% | −$0.133 | 33.3m |
| atr:0.75 / 1.5 / 3 | 136 | 28,314 | 0.42 / 0.47 / 0.50 | | | |
| trail:0.25 / 0.5 | 77 | 17,952 | 0.44 / 0.45 | | | |
| structure | 33 | 7,364 | 0.43 | 38.4% | −$0.162 | 26.6m |
| decay | 42 | 8,964 | 0.50 | 31.1% | −$0.145 | 27.5m |

Two things read straight off this. **Targets of 3, 5 and 8 bps produced zero
trades in 120 days** — they never clear their own round trip, so no entry ever
qualifies. That is the cost wall made visible, and it is why the ladder was
deliberately started one rung inside it. And **profit factor climbs
monotonically with target size**, 0.28 at 12 bps to 0.54 at 110 bps. The
baseline the main tournament used (17 bps) was close to the worst rung on the
ladder.

**Stop profile — the noise floor is measurable.**

| Stop profile | Arms | Trades | PF | Win % | Avg hold |
|---|---|---|---|---|---|
| fixed:10 | 63 | 3,166 | 0.44 | 24.6% | 10.6m |
| fixed:25 | 48 | **227** | 0.48 | 36.1% | 16.5m |
| fixed:50 | 58 | **411** | 0.48 | 39.9% | 17.6m |
| atr:1 / :2 / :3 | 624 | 209,718 | 0.35 / 0.39 / 0.36 | | |
| vol:1.5 / :3 | 114 | 28,036 | 0.41 / 0.41 | | |
| structure | 60 | 4,007 | 0.33 | 22.6% | 16.1m |
| time | 49 | 7,932 | 0.36 | 39.3% | 24.4m |
| reversal | 55 | 15,208 | **0.30** | 25.0% | 13.5m |

The report flagged the failure mode the brief asked for by name:
`fixed:10` — **"62% of exits are stops at a 25% win rate — this stop is inside
the noise."** A 10 bps stop on an asset whose 1-minute ATR is 2.2 bps is stopped
out by ordinary tick noise before the thesis has a chance. At the other end,
`fixed:25` and `fixed:50` show the best profit factors (0.48) but on 227 and 411
trades — too few to lean on. `reversal` is the worst stop tested.

**Entry quality — stricter thresholds do not help.**

| Min score | Arms | Trades | PF | Expectancy |
|---|---|---|---|---|
| 0.00 | 117 | 22,131 | 0.37 | −$0.158 |
| 0.35 | 103 | 22,123 | 0.37 | −$0.149 |
| 0.50 | 598 | 211,780 | 0.39 | −$0.154 |
| 0.65 | 101 | 9,090 | 0.34 | −$0.160 |
| 0.80 | 82 | 2,663 | 0.34 | −$0.156 |
| 0.90 | 70 | 918 | 0.38 | −$0.145 |

This is a clean refutation of a reasonable hypothesis. Raising the bar from 0.0
to 0.9 cuts the trade count 24-fold — from 22,131 to 918 — and moves expectancy
from −$0.158 to −$0.145. **"Ten excellent trades beat a hundred and fifty
mediocre ones" is false here**, because the excellent ones are not better per
trade. The conviction score is not ranking trades by outcome; it is ranking them
by how loudly the feature fired.

### What would make it credible

1. **Fix the gate plumbing** so gates 3, 4 and 6 run over the out-of-sample
   trades only, then re-evaluate. That alone may fail it.
2. **Supply the replay** so latency, missed-fill and delay stress can actually
   be evaluated rather than reported as unevaluated.
3. **Forward-test it in paper**, on data that did not exist when the search ran.
   That is the only test the multiple-comparisons problem cannot corrupt, it
   costs nothing but time, and the champion mechanism exists to run exactly it.

Step 3 is the reason the arm is recorded here in full rather than discarded. It
is a hypothesis with a coherent mechanism and a plausible geometry — it is just
not yet a result, and promoting it today would be presenting a lottery ticket as
a finding.

## 16. Stress tests

Four of the seven stresses are analytic — fees +25%, fees +50%, slippage ×2 and
worse spread are re-priced exactly from the itemised costs on each closed trade.
The other three change *which* trades happen rather than what they cost (latency
×2, missed fills, random execution delay), so they need a replay the search did
not supply and are reported as **"not evaluated"** — never as passed.

For the 1,400 main-run arms this was moot: none reached the stress gate on its
own merits. For the one arm from the exit sweep that did, the four analytic
stresses all reported "survived" — but **on the wrong trade set** (see 15b: the
fee-stress base exceeds the unstressed out-of-sample net, which is
arithmetically impossible), so that pass is not usable. And the three
unevaluated stresses are the ones that matter most for a volume-impulse family.

## 17. Monte Carlo

Run once, for the exit sweep's single apparent finalist: 2,000 bootstrap
resamples of trade order reported a median drawdown of $2.16, a 95th-percentile
drawdown of $4.28 (0.9% of the account), a worst loss streak of 11, and a 0.2%
probability of ending below the starting $500 — against the gate's 35% limit.

Those numbers are reassuring in isolation and should be read with the same
caveat as the stresses: they were computed over the retained trade list rather
than the 79 out-of-sample trades. The method's own limitation also applies and
is documented in the module — bootstrapping trade order destroys serial
correlation, which flatters any strategy whose losses cluster.

## 17b. Machine learning — not run, and why

The brief allows an ML filter *after* the deterministic strategies are
benchmarked. They now are, so this is answerable rather than deferred.

**It was not run, deliberately.** An ML filter selects a subset of existing
signals — it predicts which of them reach the target before the stop. That only
helps if the underlying signal population contains a profitable subset to find.
Across 590,306 out-of-sample trades the pooled profit factor is **0.22**, every
one of ten families sits between 0.25 and 0.36, every regime is negative, and
every coin is negative. There is no corner of that distribution with positive
expectancy for a classifier to isolate.

Training one anyway would produce a model that fits the noise in the 2026
sample, and its out-of-sample result would be the same −$0.15 a trade with more
machinery in front of it. Worse, a model that *did* look good on this data would
be the most dangerous possible outcome, because it would be indistinguishable
from overfitting at this signal-to-noise ratio.

The point at which ML becomes worth running is when a deterministic arm clears
the gates on its own and the question becomes "can a filter make a working edge
larger". We are not at that point, and the honest answer to "did you try ML" is
"it would have been a way of hiding this result, not testing it".

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

**NO.** One arm out of 2,852 cleared the mechanical gates, and section 15b sets
out why that is not a yes: three of its gates were measured against the wrong
trade set, and a single hit at that search width is indistinguishable from noise.

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
What is **not** ruled out, and is being measured:

* **The exit and stop geometry.** The 1,400-arm run held a 0.5 : 1 payoff fixed.
  A target larger than its stop needs a far lower hit rate to break even, and
  that axis has not been searched yet. This is the live question.
* **Maker-first on BTC/USD specifically**, where the early tape shows a 16% fill
  rate at flat markout. It needs 24 hours of tape to be answerable.

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

1. **Finish the exit/stop sweep.** It is the one searched axis the main run held
   fixed, and it held it at an unfavourable 0.5 : 1 payoff. Running.
2. **Let the tape reach 24 hours and re-run the execution study.** One command,
   and it answers the remaining structural question.
3. **Do not tune.** 1,400 candidates, 590,306 out-of-sample trades, ten
   families, seven regimes, six horizons, four coins — all negative on those
   axes. Anything that turns green after tuning on this sample is curve fitting,
   and the gates exist precisely to refuse it. Widening the search to an axis
   that was never searched is not the same thing as tuning one that was.
