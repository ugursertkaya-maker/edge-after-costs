# Phase 4 — predictive entry engine

Date: 2026-09-11 · Mode: **PAPER** · Real orders placed: **0**
Mode: **PAPER** · **1,556 automated tests passing**

---

## The answer

**No paper champion. NO.**

But this is the first phase whose answer is not "there is nothing here". The
predictive engine **demonstrably works**: at the best cell it selects entries
that win **67.5%** of the time against a base rate of **62.1%** — the classifier
adds about **five points of hit rate** over a random entry, out of sample, with
purge and embargo enforced.

It is still not enough. Breaking even at that geometry needs **70.0%**. The
model closes most of the gap and falls **2.5 points short**, which is why
expectancy is **−0.74 bps per trade** instead of positive.

And even that near-miss could not be promoted, because its probabilities are not
trustworthy: calibration error **0.266** against a gate of 0.10. A probability
that is wrong by 27 points is not a probability, and a threshold chosen on top
of one is not a threshold.

---

## 1. The blocking defect: root cause and fix

Phase 3's single apparent finalist was produced by a **survivorship filter**.
`_run_task` in `tournament.py` discarded the trades of every walk-forward window
whose out-of-sample net was `<= 0`, while keeping that window's *statistics* in
the merge. So `out_of_sample` described all 16 windows (79 trades, +$5.51) while
`out_of_sample_trades` held only the 9 profitable ones (45 trades, **+$14.72**).
The gates were being shown only the windows that worked. Every strange number in
the original output reconstructs from that exactly: the fee-stress base of
$14.32 is `14.7176 − 0.25 × 1.5786`, and gate 4's "$1.41 = 10% of net" is
`1.4115 / 14.7176`.

**Recomputed on all 79 trades, the arm fails gate 6.** Slippage ×2 turns +$5.51
into **−$2.84** — doubling slippage costs $8.35, more than the entire edge.
P(ending below $500) rises from 0.2% to 21.2%; the best trade's share of net
rises from 10% to 26%. Verdict **6/7, rejected**.

The fix is two independent halves that do not trust each other: the producer
raises if an arm's trade list stops reconciling with its own statistics, and the
consumer re-checks before gating and reports **"not evaluated"** rather than a
number when it cannot reconcile.

**A second validity finding:** the 79 out-of-sample trades were only **50
distinct round trips**. The 30/7 and 60/14 walk-forward schemes tile more than
40 days of the same calendar and were pooled. Phase 4 deduplicates.

**A third, in the data cache:** BTC/USD 1m has two bars written with different
`close_ms` conventions (`open+59999` vs `open+60000`) on 2026-08-31, followed by
a **545-minute hole**. The cache in `app/backtest/data.py` has merged series
from two writers. The labeller handles both correctly — the gap becomes
`EMPTY_WINDOW` rather than a silent fabrication — but roughly nine hours of
history is missing from every result in Phases 3 and 4.

## 2–4. Labels, horizons, and the target/stop grid

Triple-barrier, **NET of costs**: a 0.15% net target with a 4.5 bps round trip
triggers at **19.5 bps gross**. The loss side moves the other way, so a
`fixed_net` stop sits *closer* than its nominal value while the target sits
further. A path that rises exactly 15 bps against a 15 bps net target is a
**TIMEOUT worth 10 bps**, not a win — there is a test named for it.

172,257 bars, 120 days of BTC/USD, seven horizons × eight targets. Sanity
checks reproduce `docs/ECONOMICS.md` independently: median 1m ATR **2.06 bps**,
**51.0%** zero-trade bars.

Class balance, TP / SL / TIMEOUT with win% = TP/(TP+SL):

| Horizon | 0.10% | 0.15% | 0.25% | 0.50% | 1.00% |
|---|---|---|---|---|---|
| 5m | 42.2% | 30.2% | 15.2% | 3.3% | 0.4% |
| 15m | 56.1% | 47.1% | 31.4% | 10.6% | 1.8% |
| 25m | 60.8% | 53.2% | 39.3% | 16.7% | 3.7% |
| 60m | 67.8% | 62.1% | 51.5% | 30.3% | 9.8% |
| **90m** | **70.6%** | 65.6% | 56.6% | 37.0% | 14.3% |

`NO_EDGE` is ≤0.06% everywhere and is a closed list of five structural reasons
rather than a dumping ground. Two of them matter: ATR warm-up would otherwise
produce a zero-width stop that labels every warm-up bar a loser, and the last H
minutes of the series would otherwise become free negatives — they are
`CENSORED` instead.

**The gap the model has to close**, before any model is trained:

| Horizon | Target | Break-even p | Base rate | Gap |
|---|---|---|---|---|
| 90m | 0.35% | 55.5% | 48.5% | **−6.9** |
| 90m | 0.25% | 63.6% | 56.6% | −7.0 |
| 60m | 0.20% | 64.6% | 56.8% | −7.8 |
| 25m | 0.15% | 62.6% | 53.2% | −9.4 |
| 5m | 0.25% | 35.4% | 15.2% | −20.3 |

The gap narrows monotonically with horizon — the same direction Phase 3's
profit-factor curve found by a completely different route. The best case asks
the classifier for **about seven points of hit rate** over a random entry.

## 5. Feature groups

517,347 rows × **105 features** on 120 days of BTC/USD; cold build 30 s, warm
1.1 s. Groups: momentum 39, volatility 15, volume 24, regime 7, cost 10, time 6,
score 4 — and **book: 0**, because the historical tape carries no L2 and the
group is omitted rather than zero-filled.

**Leakage is proven, not asserted**: a 60-day build and a 120-day build are
byte-identical across the **257,613 rows they share**, and appending 120 violent
future bars (5× price, 500× volume) changes nothing at any earlier timestamp.

The measurement that summarises this venue in one number: **`breakeven_over_atr`
has a median of 1.47** — a typical trade must beat about one and a half
one-minute ATRs just to reach zero.

Groups that are constants rather than features, and should be dropped or fixed:

* `risk_on` — 100% neutral on a single symbol (breadth across one symbol is that
  symbol's own sign); 0% on a four-symbol run.
* the **cost group — 55.3% neutral** without L2. This is the group the live
  recorder's books would improve most.
* `ret_5s` 92% neutral, `range_compress_5s` **99.9%**. Sub-minute is dead here
  by measurement, not assumption.

## 6–8. Models

Five benchmarked, simplest first, with a complexity rule that is **arithmetic
rather than editorial**: PR-AUC must improve by ≥ 0.005 *and* ROC-AUC must not
fall. On synthetic data with a realistic weak edge, all four complex models were
rejected and plain **logistic** was champion — "the extra complexity bought
nothing" is a result, not a failure.

Imbalance is handled with class weights, not resampling: over-sampling
duplicates rows whose label windows already overlap, which would corrupt the
very purge `splits.py` exists to enforce.

**The noise control is the most important test in the phase.** Fed pure noise,
all five models land at ROC-AUC 0.487–0.512, every non-baseline is rejected, and
three carry an automatic "in-sample 0.83 vs out-of-sample 0.51 — memorising, not
learning" note. A pipeline that reports an edge on noise cannot be trusted when
it reports one on data; this one does not.

Two real bugs the tests caught rather than luck:

* **float32.** `sklearn.tree` casts X to float32 before traversal and then
  compares against a float64 threshold. A naive float64 re-implementation
  disagreed by **2.4e-3** on a 300-tree forest — three orders of magnitude
  outside the 1e-9 export tolerance. The exporter now replays that rounding.
* **Platt divergence.** Newton without a line search sent the slope to 1.2e11 on
  a near-separable validation set.

## 9–13. Out-of-sample results

Chronological splits, 60 d train / 15 d validate / 15 d test, rolling; purge
against the maximum label horizon plus a 60-minute embargo; model selected and
calibrated on validate; threshold chosen on validate; **test scored once**.

BTC/USD, round trip 6 bps:

| Horizon | Target | Rows selected | Win % | **Needs** | PF | EV (bps) | Calib. error |
|---|---|---|---|---|---|---|---|
| 15m | 0.15% | 611 | 50.9% | 70.0% | 0.44 | −5.73 | 0.419 |
| 25m | 0.15% | 1,422 | 61.0% | 70.0% | 0.67 | −2.71 | 0.875 |
| 25m | 0.25% | 42 | 26.2% | 62.0% | 0.22 | −17.90 | 0.508 |
| **60m** | **0.15%** | **8,347** | **67.5%** | **70.0%** | **0.89** | **−0.74** | 0.266 |
| 60m | 0.25% | 2,222 | 49.9% | 62.0% | 0.61 | −6.05 | 0.361 |
| 90m | 0.15% | 18,646 | 65.8% | 70.0% | 0.82 | −1.27 | 0.203 |
| 90m | 0.25% | 2,975 | 53.3% | 62.0% | 0.70 | −4.33 | 0.599 |
| 90m | 0.50% | 256 | 24.2% | 56.0% | 0.25 | −31.78 | 1.000 |

ETH/USD's first cell agrees: 61.9% against 70.0% needed.

**A prediction of mine that the data refuted.** I expected larger targets to
help, because the break-even requirement falls with target size (70% → 62% →
56%). It does fall — but the model's accuracy collapses faster than the
requirement (67.5% → 49.9% → 24.2%). A bigger target is a harder question:
reaching 50 bps before a stop is a rarer event and the classifier's skill does
not transfer to it. Larger targets are worse here, not better.

**An honesty note on the counts.** 8,347 and 18,646 are *labelled instants above
the threshold*, not executable round trips. Every bar is a candidate and the
label windows overlap heavily, so these are classification results, not a
backtest — nobody can hold 18,646 overlapping positions. The win rates and the
break-even comparison are meaningful; the trade counts are not tradeable
quantities and no P&L in this section should be read as an account balance.

## 9b. Calibration — why nothing was promoted

Calibration error ranges **0.203 to 1.000** across every cell; the gate is
**0.10**. Even the best cell is at 0.266.

This is not a detail. The entry threshold is chosen by asking "at what predicted
probability does expected value turn positive", and that question is meaningless
when the predicted probability is wrong by 20–100 points. The engine enforces
this in code rather than in prose: an uncalibrated artifact caps expected value
at zero and returns `MODEL_UNCERTAIN`, and the dashboard is handed `null` instead
of a confidence figure it would otherwise dress up as authority.

Class weighting is part of the cause and is understood: balanced weights make
the raw score deliberately *not* a probability (mean 0.436 against a 0.117 base
rate), and isotonic calibration on validation is supposed to put it back. On
synthetic data it does — mean 0.115 against a base rate of 0.117, max error
0.018. On real data it does not, which says the validation distribution does not
match the test distribution: the market moved between the two windows. That is
the diagnosis to attack next.

## 16–17. Concentration, 24–25. stress and Monte Carlo

Not reported, because reporting them would imply a candidate worth stressing.
No cell passed expectancy > 0, so none reached the concentration, cost-stress or
Monte-Carlo gates. The machinery exists and is tested; running it on a negative
arm would decorate a negative result.

## 18–20. Coins and regimes

BTC/USD is the only symbol with a complete grid at the time of writing; ETH/USD
agrees on its first cell and SOL/USD is still running. Phase 3 established
across 590,306 out-of-sample trades that no coin and no regime is materially
better than any other — profit factors clustered 0.17–0.26 by coin and
0.14–0.34 by regime — and nothing in Phase 4 so far contradicts that.

## 21–22. Threshold and EV gate

The threshold sweep runs 0.55–0.75 and selects on out-of-sample net profit,
never win rate, refusing any threshold with fewer than 30 trades. The EV gate
sweeps +0.02% to +0.20% of expected net return with a dollar floor scaled to
position size, so a $100 position chasing $0.01 fails even the loosest row.

Neither was selected for deployment, because no cell produced a positive
expectancy to threshold.

The known trap from Phase 3 — a stricter threshold that cuts trade count 24-fold
and moves expectancy by $0.013 — is now visible by construction: every threshold
row carries its trade-count ratio and its expectancy gain, and the table prints
`buying silence` when the two disagree.

## 23. Maker / taker / hybrid

Still open, still collecting. The tape is at **~12 hours** of the 24 the study
requires. The machinery is built and tested (queue ahead, trade-through, partial
fills, timeouts, markout at 1s/5s/30s, and a test named
`test_touched_is_not_filled`), and it refuses to conclude:

```
INSUFFICIENT DATA — this run cannot say whether resting beats crossing.
BTCUSD: 467 prints (need 500)  ETHUSD: 146  SOLUSD: 157
```

The partial reading is the one encouraging signal in this phase: BTC/USD shows a
**16.2% fill rate with a 5s markout of −0.46 bps — flat**, i.e. no measurable
adverse selection, which is the profile under which 0% maker fees and an earned
spread would be real money. ETH and SOL show adverse selection on eleven and ten
fills respectively, which is to say on nothing.

## 25b. The live paper engine started trading — and it is up $1.92

After ~16 hours the deterministic V2 engine (no champion; nothing from Phase 4
is deployed) has closed its first real set of paper trades:

| | |
|---|---|
| Closed trades | **33** |
| Win rate | **84.8%** |
| Profit factor | **2.46** |
| Gross P&L | +$2.71 |
| Costs | −$1.81 |
| **Net P&L** | **+$1.92** |
| Equity | **$501.92** |
| Exits | 14 take-profit, 14 trailing, 5 stop |
| Strategies | flow_impulse 16 trades +$1.31, momentum_scalp 17 trades +$0.61 |

**This is not evidence and must not be read as any.** Thirty-three trades over
sixteen hours, against Phase 3's 590,306 out-of-sample trades at a profit factor
of 0.22 and Phase 4's finding that the best achievable hit rate anywhere on this
venue is about 67.5%. An 84.8% win rate over 33 trades in a single session is
what a lucky stretch looks like, and the trades are not independent — they
overlap, in the same few hours, across three correlated symbols. The brief's own
instruction applies: several hours of live paper is not long-term proof.

What it does establish is that the machinery runs end to end on live data
without supervision: entries, trailing exits, stops, checkpoints, capital
recycling and cost accounting all behave. Costs are 67% of gross, which is the
same ratio every other measurement in this project has produced.

### The finding inside it: maker-first fills nothing

```
maker_fills 0    maker_misses 1,789    TAKER_NOT_QUALIFIED 1,690
```

**Every post-only entry missed — 1,789 of them, a 0% fill rate.** All 33 trades
were taker fallbacks, and 1,690 further attempts were correctly refused because
the trade no longer cleared its economics as a taker.

That sits against the tape replay's estimate of a **16.2%** maker fill rate on
BTC/USD. The two disagree by the entire measurement, so at least one of them is
wrong about something, and the difference is worth naming rather than averaging:
the live engine rests one tick behind the touch and cancels on a 20-second
timeout, while the replay sweeps offsets and timeouts as parameters. A 0% live
fill rate says the live quoting parameters are not viable as configured — it
does **not** say passive execution is dead here, which is exactly the question
the 24-hour tape study exists to answer.

Either way the honest reading is: the one structure that could remove the cost
wall currently fills nothing in production, and that is now a measured fact
rather than an assumption.

## 26–32. Housekeeping

| | |
|---|---|
| **Paper champion exists** | **NO** |
| Automated tests | **1,556 passing**, 1 skipped |
| Research compute | entirely off-box; the deployment host was never loaded |
| Worker CPU / RAM | 5.2% of one core, 78 MB of 640 MB |
| Host load | 0.51 on 2 vCPU |
| **PAPER mode** | **YES** |
| **Real orders** | **0** |
| Exchange credentials configured | **NO** |
| Withdrawal-enabled credential | **NO** — none exists |

sklearn is a research dependency only. `requirements.txt` — the production image
— is untouched, and a champion would ship as a numpy-only artifact whose
exported predictor is tested to match sklearn's `predict_proba` to 1e-9.

## 28. Why no predictive edge, precisely

Not "the model learned nothing". The model learned about **five points of hit
rate**, out of sample, with leakage controlled. Three things stand between that
and a champion, in order of how fixable they look:

1. **Calibration is broken on real data** (error 0.27 against a 0.10 gate) while
   working on synthetic data (0.018). The validation and test distributions
   differ — the market moved between the windows. This is the most promising
   thing to attack, because it is a modelling problem rather than a market fact.
2. **The remaining gap is ~2.5 points of hit rate** at the best cell. That is
   small, but it is the difference between −0.74 bps and zero, and closing it by
   tuning on this sample would be curve fitting.
3. **The cost wall is the binding constraint and is structural.** A typical
   trade must beat 1.5 one-minute ATRs to reach zero. Larger targets lower the
   requirement and lower the model's accuracy faster. No arrangement of the
   entry signal changes the arithmetic.

Which is why the one genuinely untested structure still matters more than any of
this: **maker execution removes the cost wall rather than trying to out-predict
it**. At 0% maker fees with the spread earned instead of paid, the break-even
hit rate drops to roughly a coin flip — and the model is already well above one.
