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

## 5. The two samples disagree, and that is the finding

| | Backtest | Live |
|---|---|---|
| Span | **120 days** | **~20 hours** |
| Trades | 366 | 139 |
| RAW expectancy | **−$0.0339** | **+$0.1564** |
| RAW profit factor | **0.791** | **4.251** |
| Verdict | **CASE B** | **CASE A** |

They are **not the same signal**. The backtest runs the older deterministic path
with `net_fixed` exits; the live engine runs the V2 stack with NET targets,
maker-first entry and a taker re-qualification gate. So neither refutes the
other, and picking the flattering one would be the exact failure this audit
exists to prevent.

On the evidence that spans enough time, the answer is **CASE B**. A raw profit
factor of 4.251 over one 20-hour session is the same shape as the +$1.92 headline
already labelled a lucky stretch, and the same scepticism applies — more so,
because 4.25 is an implausible number for a real edge. Every strategy in the
120-day sample has a raw PF **below 1.0** individually, which is harder to
explain away than a single aggregate.

## 6. The answers

**1. Does the strategy have raw alpha?**
**No**, on the sample that covers enough time. RAW PF 0.791, RAW expectancy
−$0.0339/trade over 366 trades and 120 days, with all three strategies below 1.0
separately. The live 20-hour window says otherwise and is too short to be
believed against it.

**2. How much of that alpha is destroyed by execution?**
The question does not apply in the backtest — there is no alpha to destroy.
Execution turns −$12.42 into −$61.95, so it **multiplies an existing loss by
five**. In the live sample, where RAW is positive, execution consumes **106%**
of it: +$21.75 becomes −$1.28.

**3. Does maker recover enough of it?**
**No, and this corrects a recommendation I have made in five consecutive
reports.** Maker execution removes the fees and stops paying the spread — worth
about $49.53 of the backtest's $61.95 of cost, perhaps more if the spread is
earned rather than merely not paid. That takes net from −$61.95 to roughly
−$12.42, i.e. back to RAW. **It cannot go further, because RAW is where the
strategy already loses money.** You cannot remove costs from a strategy that
loses at the mid.

**4. Does hybrid recover enough of it?**
No, for the same reason, and less than pure maker by construction — hybrid
crosses the spread whenever the maker order misses, so it pays some of the cost
maker avoids.

**5. Is there a strategy worth continuing?**
Not this one. But the two findings that survive are worth more than it was:

* **Phase 4's classifier adds ~5 points of hit rate** out of sample, with purge
  and embargo enforced, on labels that are honest about costs. That is a real
  measurement of real signal, and it is the only thing in this project that has
  demonstrably beaten a coin flip on out-of-sample data.
* **The infrastructure is sound and has caught its own errors repeatedly** — a
  survivorship filter, a cost-stress computed on the wrong trade set, a
  fabricated spread, a cache that never wrote, a guard that took the API down.
  This audit is another instance: the accounting was right and the *label* was
  wrong, and the challenge surfaced it.

## 7. What this changes

**The maker study stops being the priority.** It has been my recommendation
since Phase 1 and the audit removes its premise: execution is not the primary
problem when the signal loses money before execution. The tape collection costs
nothing and continues, and the study is worth running once there is a signal
worth executing — but it is no longer the next step.

**The next step is entry-model research**, which is where Phase 4 already found
the only genuine signal in the project. Specifically: the classifier reached
67.5% against a 70.0% requirement, and closing that 2.5-point gap is a modelling
problem on a measured quantity, not a venue problem.

The one thing I will not do is tune thresholds until a backtest turns green. The
gates exist to refuse that, and this audit is what they are for.
