# Phase 5 — aggressive capital utilisation

Date: 2026-09-11 · Mode: **PAPER** · Real orders placed: **0**
Mode: **PAPER** · **1,768 automated tests passing**

---

## The answer, in one paragraph

**The bot is not being conservative. It is being priced out.**

Of a $500 account, **~$318 of the idle balance is attributed to
`EXECUTION_COST`** — candidates existed, were evaluated, and did not survive
their own round trip. `CORRELATION_LIMIT` is **$0.17**. `RISK_LIMIT` is **$5**.
The three knobs this phase was asked to turn — position count, risk budget,
deployment band — were turned across **12 arms and produced 2 distinct books**.
Peak concurrent positions: **3**, against a ceiling of 20 and a target of 8–15.

Raising the aggression does not raise the activity, because aggression was never
the constraint.

---

## 1–5. Positions, capital, and exactly why it is idle

| | Measured |
|---|---|
| Average simultaneous positions | **0.01** |
| Maximum simultaneous positions | **3** (ceiling 20, target 8–15) |
| Average deployed capital | **0.17%** (target 60–85%) |
| Average idle capital | **99.83%** |

**Idle capital by reason**, time-weighted over $500, summing exactly to the idle
balance — no residual bucket:

| Reason | at 60% band | at 85% band | What it means |
|---|---|---|---|
| **EXECUTION_COST** | **$258.54** | **$366.40** | candidates existed; the edge did not survive the round trip |
| CASH_RESERVE | $200.00 | $75.00 | deliberately held back — policy, not failure |
| NO_QUALIFIED_SIGNAL | $36.54 | $51.81 | nothing ranked at all |
| RISK_LIMIT | $4.08 | $5.78 | the open-risk budget |
| CORRELATION_LIMIT | $0.00 | **$0.17** | correlated exposure |
| LIQUIDITY_LIMIT | $0.00 | $0.00 | depth |

This is the measurement `PHASE5_SPEC.md` said would settle the universe
question, and it settles it **against expansion**. `EXECUTION_COST` dominating
means the venue is the constraint: more markets at a 20 bps round trip buys more
*scanned* opportunities and no more *qualified* ones.

It also clears correlation of the charge. The spec warned that correlation must
not become an excuse for idle cash. It is not the excuse — it accounts for
seventeen cents.

## The universe expansion, measured

`restrict_to_fee_tier0=false` widens the eligible set to 12 pairs. **All four
HOT pairs are Tier-0. Not one standard-fee pair qualifies for HOT.**

| Tier | Example | Round trip | 60m move | **Break-even hit rate** |
|---|---|---|---|---|
| HOT | ETHUSD / BTCUSD | 2.5–2.6 bps | 26–49 bps | **52.6–54.8%** |
| HOT | SOLUSD / ADAUSD | 4.0–6.4 bps | 39–41 bps | 54.9–58.1% |
| WARM | ZECUSDT / XRPUSDT | 20.7–28.6 bps | 45–76 bps | **68.9–72.8%** |
| COLD | HYPEUSD / BNBUSDT | 22–26 bps | 25–37 bps | **86–94%** |

Dropping the liquidity bar tenfold (to $10k / 20 trades) widens the venue set to
31 pairs — and adds exactly **two more Tier-0 pairs** to HOT. Everything else it
finds costs 20–100 bps to trade.

**The binding constraint is the fee table, not the liquidity bar.** Scanning
more markets was the right instinct and it has been implemented — HOT/WARM/COLD
tiers now cover the whole venue, and COLD is a watchlist rather than a websocket
subscription — but it cannot produce qualified opportunities that the fee
schedule forbids.

## 6–17. The three-profile comparison

STANDARD 3% / AGGRESSIVE 5% / HIGH-ACTIVITY 7.5% open risk, each crossed with
60/70/80/85% deployment bands. 120 days, four coins, 1 bps/side.

| Arm | Risk % | Max pos | Deployed % | Trades | Trades/day | **Net $** | PF | Expectancy | Max DD |
|---|---|---|---|---|---|---|---|---|---|
| standard @60% | 3.0 | 2 | 0.17 | 320 | 2.69 | **−$42.40** | 0.30 | −$0.1325 | 8.51% |
| standard @70% | 3.0 | 3 | 0.17 | 322 | 2.70 | −$42.67 | 0.30 | −$0.1325 | 8.57% |
| aggressive @60–85% | 5.0 | — | — | — | — | *identical, row for row* | | | |
| high_activity @60–85% | 7.5 | — | — | — | — | *identical, row for row* | | | |

**Twelve arms produced two distinct books**, and the only pair that differs
differs by two trades. This is Phase 3's allocation-mode finding reproduced by a
different route: when there is nothing to allocate across, every allocation
policy is the same policy.

Cost breakdown on the representative arm — **corrected labelling**, see
`docs/PNL_AUDIT.md`: what was reported as "gross" (−$34.41) is P&L at the **fill
prices**, so the spread and slippage are already inside it; net is
`gross − fees` = −$42.40, and slippage is a memo, not a third deduction.
Nothing was counted twice, but calling it "gross" was a reporting defect.

The honest decomposition of the same arm: **RAW market P&L (at the mid)
−$9.38**, spread+slippage $25.03, fees $8.00, **net −$42.40**. Slippage is
**3.1× fees** — the same ratio every phase has measured.

**The raw number is negative**, which is the finding that matters and is
developed in `docs/PNL_AUDIT.md`: the signal loses money before any execution
cost is charged.

Out-of-sample (30% holdout by entry time, 122 trades): expectancy
**−$0.1477/trade**.

**Selection: NO PROFILE QUALIFIES.** Every arm fails on net P&L, so the rule
"pick the most aggressive profile that still has positive out-of-sample
expectancy" has nothing to pick.

### Daily loss circuit breakers

**−3%, −5%, −7.5%: zero trips. All twelve arms. 120 days.** The worst single-day
loss never reached −3%. The 8.51% maximum drawdown accumulated over four months
rather than in any one session.

The breakers are not what is holding the account back. Nothing is bleeding fast;
it is bleeding slowly, one round trip at a time.

## 18. Missed-opportunity audit — was the gate right to refuse?

1,739 high-idle windows, **7,735 recorded rejections**, all examined.

| Rejection reason | MISSED | CORRECT | Missed % |
|---|---|---|---|
| NEGATIVE_EDGE | 2,127 | 3,811 | 35.8% |
| NOT_SUPPORTED_SHORT | 362 | 586 | 38.2% |
| EDGE_BELOW_MIN | 245 | 483 | 33.7% |
| BAD_REWARD_RISK | 32 | 49 | 39.5% |
| DUPLICATE_SYMBOL | 13 | 24 | 35.1% |
| **Total** | **2,780** | **4,955** | **35.9%** |

`missed_net_bps` = **−3.28 bps per refused candidate**. The winners it turned
away would have paid +20,196 bps; the losers it avoided would have cost −45,587.

**The gate is refusing correctly** — on balance the refusals saved more than
twice what they cost.

But the second number is the more interesting one: the miss rate is **34–39%
under every single reason**. A well-tuned filter should refuse a *lower*
fraction of winners under its strongest reason than under its weakest. This one
refuses about the same share whatever justification it gives, which says it is
not being selective — it is being **uniformly costly**. That is the signature of
a cost wall, not of a mis-calibrated filter, and it is consistent with
everything else in this report.

## 19–20. Best profile

**None.** The most aggressive profile with positive out-of-sample expectancy
does not exist in this set, because no profile has positive out-of-sample
expectancy.

The honest ranking is that all twelve are the same strategy paying the same
costs, and the differences between them are noise.

## 21–22. Housekeeping

| | |
|---|---|
| Automated tests | **1,768 passing**, 1 skipped |
| **PAPER mode** | **YES** |
| **Real orders** | **0** |
| Leverage | none — and none was added to manufacture P&L |
| Exchange credentials | **NO** |

## What this means for the ask

The brief's closing line was: *"I do NOT want a bot that protects $500 by barely
using it."* That is fair, and the measurement says it is not what is happening.
The account is not being protected. It is being **priced out** — $318 of it sits
idle because the trades available cost more to execute than they are expected to
return, and a gate that let them through would not be aggressive, it would be
losing money faster.

Everything the phase asked for has been built and is live: the universe now
tiers the whole venue, the allocator targets a deployment band and sizes for
8–15 small positions, the dashboard shows every idle dollar with its reason, and
the missed-opportunity audit grades the gate's judgement. What the phase could
not do is make those mechanisms bite, because the constraint they were built to
relieve was never the one holding the account back.

**This section previously recommended maker execution as the first lever. The
P&L audit removed its premise — see `docs/PNL_AUDIT.md`.** Over 120 days and 366
trades the raw profit factor is **0.791**, with every strategy below 1.0
separately: the signal loses at the mid, before any cost. Maker execution would
recover roughly $49.53 of the $61.95 of cost and land back at the raw number,
which is still a loss. You cannot remove costs from a strategy that does not
make money at the mid.

**The revised order:**

1. **Entry-model research.** Phase 4's classifier adds ~5 points of hit rate out
   of sample with leakage controlled — the only thing in this project that has
   demonstrably beaten a coin flip on unseen data. It reached 67.5% against a
   70.0% requirement, and that 2.5-point gap is a modelling problem on a
   measured quantity.
2. **Maker execution, later.** Still the right move *once there is a signal
   worth executing*. Tape collection costs nothing and continues.
3. **Accepting that this account works slowly or not at all.** A $500 account on
   a venue where a typical trade must clear 1.5 one-minute ATRs to break even is
   not a high-turnover account. Wanting it to be does not make the arithmetic
   different.
