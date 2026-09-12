# P&L accounting audit — and the decision it forces

Date: 2026-09-11 · Mode: **PAPER** · Real orders: **0**

---

## 1. The challenge was right to demand the audit; the arithmetic does reconcile

Reported in Phase 5:

```
gross  -$34.41 · fees $8.00 · slippage $25.03 · net -$42.40
-34.41 - 8.00 - 25.03 = -67.44, not -42.40
```

The subtraction is right and the conclusion drawn from it is wrong, because
`gross` in this codebase is **not** P&L before execution costs. From
`Ledger.close`:

```python
gross = (exit_fill_price - entry_fill_price) * qty      # FILL prices
net   = gross - entry_fee - exit_fee
slippage_usd = entry_notional * entry_slippage_bps/1e4  # a MEMO, measured
             + proceeds      * exit_slippage_bps/1e4    # against the mid
```

The fill prices already contain the spread and the slippage, because the
simulator walks the book. So `−34.4053 − 7.9973 = −42.4026` — exact, and
slippage is inside the first term rather than a third deduction. **Nothing is
counted twice.**

But labelling that "gross" without saying so was a genuine reporting defect. A
reader cannot be expected to know that one of the three numbers is already
inside another. It is fixed below and in `scripts/pnl_audit.py`, which now
enforces the decomposition per trade.

## 2. The unambiguous decomposition

```
RAW_MARKET_PNL     P&L at the decision-time mid — perfect execution
                   = reported gross + spread_and_slippage
SPREAD+SLIPPAGE    everything paid away from mid, both legs
ENTRY_FEE / EXIT_FEE
NET_PNL            = RAW − (SPREAD+SLIPPAGE) − ENTRY_FEE − EXIT_FEE
```

Spread and slippage are **not separable after the fact**: the simulator measures
both together against the decision-time mid. That is stated rather than split
into two invented numbers.

`scripts/pnl_audit.py` re-derives `NET` from `RAW` for **every individual
trade** and reports a violation if it disagrees by more than float error. Across
both datasets below: **505 trades, zero violations.**

## 3. Reconciliation — 120-day backtest, 366 trades

| Strategy | Trades | RAW | Spread+slip | Fees | NET | **RAW PF** | NET PF |
|---|---|---|---|---|---|---|---|
| momentum_scalp | 224 | −8.95 | 23.07 | 7.34 | −39.37 | **0.764** | 0.297 |
| breakout_scalp | 122 | −2.28 | 12.47 | 4.01 | −18.76 | **0.876** | 0.327 |
| trend_pullback | 20 | −1.18 | 1.98 | 0.65 | −3.82 | **0.630** | 0.237 |
| **TOTAL** | **366** | **−12.42** | **37.52** | **12.01** | **−61.95** | **0.791** | **0.303** |

```
  RAW MARKET P&L          -12.4186
  - SPREAD+SLIPPAGE        37.5203
  - ENTRY FEES              6.0067
  - EXIT FEES               6.0017
  ================================
  = NET P&L               -61.9473   reported -61.9473   OK
```

RAW expectancy **−$0.0339/trade**. RAW profit factor **0.791**.

## 4. Reconciliation — live paper, 139 trades, ~20 hours

| Ledger | Trades | RAW | Spread+slip | Fees | NET | RAW PF | NET PF |
|---|---|---|---|---|---|---|---|
| base ($500) | 42 | +4.39 | 1.25 | 1.10 | **+2.04** | 4.726 | 2.259 |
| l1000 | 48 | +5.22 | 2.03 | 1.87 | +1.32 | 4.110 | 1.427 |
| l5000 | 49 | +12.13 | 10.36 | 6.42 | **−4.64** | 4.167 | 0.603 |
| **TOTAL** | **139** | **+21.75** | **13.63** | **9.39** | **−1.28** | **4.251** | 0.922 |

RAW expectancy **+$0.1564/trade**. RAW profit factor **4.251**. Reconciles
exactly, every trade.

The ledger split repeats the capacity finding from V2 by a third route: the same
decisions are **net positive at $500 and net negative at $5,000**, because the
larger size walks deeper into a book with $753 of volume a minute behind it.

## 5. The two samples do not disagree — they do not overlap

The first version of this document treated the two samples as a conflict and
sided with the longer one. That was too quick. They barely share a strategy:

| Strategy | Live, 12.66 h | Backtest, 120 d |
|---|---|---|
| momentum_scalp | 67 trades, raw PF 3.675 | 224 trades, raw PF **0.764** |
| breakout_scalp | 3 trades | 122 trades, raw PF 0.876 |
| trend_pullback | — | 20 trades, raw PF 0.630 |
| **flow_impulse** | **48 trades, raw PF 13.09** | **0 trades in 120 days** |
| **book_imbalance** | **21 trades, raw PF 3.41** | **0 trades in 120 days** |

The two strategies carrying the live result recorded **zero trades in 120 days**,
and not by chance. All six strategies were instantiated; a signal census over
three days shows why:

* **`book_imbalance` proposes nothing at all** — 0 signals. It reads the resting
  book, and the harness synthesises the book from a bar, so the feature it
  depends on never moves.
* **`flow_impulse` proposes plenty — 43 signals in three days — and every single
  qualified one is rejected `NOT_SUPPORTED_SHORT`.** Against a synthesised tape
  it only ever goes short, and this is a spot account. Ten qualified signals,
  ten rejections, zero trades.

So the 120-day backtest is not weak evidence about those two strategies. It is
**no evidence**: the harness cannot feed them. The live sample is the only
measurement either has ever had.

## 6. The live sample was long-only in a rally — so measure against that

Every one of the 139 live trades was **LONG**, and the 12.66 hours they span
were a one-way move: ETH **+484 bps**, SOL +246, BTC +114, with maximum adverse
excursions of 40, 122 and 95 bps. A long-only scalper in that tape posts a high
win rate whether or not it predicts anything.

So the benchmark is not zero. It is **holding the same exposure, in the same
symbol, for the same interval**. `scripts/beta_check.py` computes
`ALPHA = RAW − MARKET` per trade:

| Strategy | n | RAW bps | MARKET bps | **ALPHA bps** | 95% CI | hold |
|---|---|---|---|---|---|---|
| momentum_scalp | 67 | 6.33 | 5.48 | **+0.85** | [−2.11, +3.82] | 71 s |
| **flow_impulse** | 48 | 12.10 | **0.18** | **+11.93** | **[+5.43, +18.42]** | 32 s |
| book_imbalance | 21 | 2.66 | 2.11 | +0.55 | [−1.89, +2.99] | 114 s |
| breakout_scalp | 3 | −1.41 | 0.00 | −1.41 | — | 53 s |
| **TOTAL** | 139 | 7.60 | 3.02 | +4.58 | [+1.77, +7.39] | 64 s |

**`momentum_scalp` was beta.** Its raw return is 6.33 bps and the market handed
it 5.48 of them; the remainder is indistinguishable from zero. That is exactly
what the 120-day backtest says about the same strategy, now confirmed from the
live side by a second method. `book_imbalance` is the same story.

**`flow_impulse` is not.** It earned 12.10 bps over intervals in which the
market moved 0.18 — it holds for 32 seconds and captures something that is not
drift. Robustness, because 48 trades is not many:

```
t = 3.60      95% CI [+5.43, +18.42]      Bonferroni x4 [+3.64, +20.21]
median +9.76 bps (not one outlier)        36 of 48 trades positive
drop the best trade  -> +10.47            drop the best three -> +7.35
first half +4.59  second half +19.26      BTC +8.64  ETH +9.21  SOL +18.47
```

It is also the only **net-positive** line in this entire project: +$4.24 after
all costs, net PF 6.649. At a 2.5 bps tier-0 round trip, a 12 bps gross edge
clears the cost wall with room — the first thing measured here that does.

**This is a candidate, not a result.** Forty-eight trades in one 12.66-hour
session in one regime. It has never been tested over a longer period, and it
cannot be, by the harness that produced every other number in this project.

## 7. The answers

**1. Does the strategy have raw alpha?**
**It depends which strategy, and the distinction is the whole finding.**
`momentum_scalp`, `breakout_scalp` and `trend_pullback` — the three the 120-day
backtest can actually evaluate — do not: raw PF 0.791 combined, all three below
1.0 separately, and live `momentum_scalp`'s apparent edge is 5.48 bps of market
drift out of 6.33. **CASE B for everything with a long sample.**
`flow_impulse` does, on the only sample it has: +11.93 bps of alpha over market,
t = 3.60, on 48 trades in 12.66 hours. **CASE A for one strategy, on 12 hours.**

**2. How much of that alpha is destroyed by execution?**
For the bar strategies the question does not apply — there is no alpha to
destroy; execution multiplies an existing loss roughly fivefold (−$12.42 raw to
−$61.95 net over 120 days). For `flow_impulse`, execution takes 12.10 bps gross
to a net that is still positive: costs consume roughly **65%** of it and it
survives, which is what clearing the cost wall looks like.

**3. Does maker recover enough of it?**
For the bar strategies, no — and this corrects a recommendation I had made in
five consecutive reports. Maker execution removes the fees and stops paying the
spread, worth about $49.53 of the backtest's $61.95 of cost, which takes net
from −$61.95 to roughly −$12.42: back to RAW, which is still a loss. **You
cannot remove costs from a strategy that loses at the mid.**
For `flow_impulse` the question is live again, and the measured numbers are in
§8 rather than estimated here.

**4. Does hybrid recover enough of it?**
Same answer, and by construction hybrid recovers less than pure maker on the
cost side because it crosses the spread whenever the quote misses. What hybrid
buys is fill certainty, which matters much more for a 32-second holding period
than for a 71-second one — a missed fill on a 32-second edge is the whole edge.

**5. Is there a strategy worth continuing?**
**Yes — one, and it is not the one this project has spent five phases on.**
`flow_impulse`, on an explicit research footing: it is the only thing here that
has beaten its own benchmark after costs, and also the least-tested thing here.
The three bar-driven strategies should stop being the subject. Phase 4's
classifier (~5 points of hit rate out of sample, purge and embargo enforced)
remains the second-best measured result and the natural entry filter to pair
with it.

## 8. What this changes

**Tape collection goes back to first priority — for a different reason than I
have been giving.** For five reports I argued the tape mattered because maker
execution would fix the cost problem. That premise is gone for the bar
strategies. The tape matters now because it is **the only instrument that can
backtest `flow_impulse` and `book_imbalance` at all.** The bar harness cannot:
one of them proposes nothing, the other proposes only shorts. Every negative
result in Phases 3 through 5 was produced by a harness that is blind to the two
strategies that have shown anything.

**The next build is a tape-driven backtest**, not another parameter sweep. The
recorder, the replay and the book reconstruction already exist; what is missing
is driving the live decision stack from replayed L2 and tape instead of from
synthesised bars. Until that exists, `flow_impulse`'s 12 bps is a 12-hour
anecdote, and no threshold change can turn it into anything else.

**What I will not do is promote it now.** Forty-eight trades, one session, one
direction. The gates exist to refuse exactly this, and a candidate that looks
this good on a sample this small is the case they were written for.
