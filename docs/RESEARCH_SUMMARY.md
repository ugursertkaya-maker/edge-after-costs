# Research summary

**Mode:** PAPER · **Real orders placed:** 0 · **Exchange credentials:** none ·
**Leverage:** none

This file consolidates what the framework measured across Phases 1-8. Every
number here is a measurement produced by a named tool in `api/scripts/`, not a
recollection or an estimate. The per-phase documents listed at the end carry the
full working.

The headline is a negative result, and it is kept deliberately: the point of
this framework is to find out whether an idea survives realistic costs, and
five families of ideas did not.

---

## 0. The result in one paragraph

The three bar-based strategies in this book (`momentum_scalp`, `breakout_scalp`,
`trend_pullback`) **lose money at the mid price, before a single cost is
deducted** — 366 trades over 120 days, raw profit factor **0.791**, all three
individually below 1.0. Costs multiply that loss roughly fivefold, but costs are
**not the cause**. Across five earlier reports the conclusion "maker execution
will fix this" was asserted and was wrong; it has been corrected: **you cannot
subtract cost from a strategy that already loses at the mid.**

Exactly one thing in the project beats its own benchmark: `flow_impulse`. It
earns **12.10 bps** against the **0.18 bps** the market gave over the same
holding interval — real edge rather than drift. But the sample is **48 trades,
one calendar day, 93.8% hit rate**. That is a candidate, not proof. **It was not
promoted.**

---

## 1. The venue constraint (`docs/ECONOMICS.md`)

Measured over 258,657 one-minute bars on Binance.US:

| Measurement | Value |
|---|---|
| BTC/USD bars with **no trade at all** | 55% |
| Mean volume per minute | ~$753 |
| 1-minute ATR | 2.2 bps |
| Coinbase volume, same day | **312x** |
| Kraken volume, same day | **126x** |

Cheap fees and absent liquidity are the **same phenomenon**: Binance.US zeroed
its fee schedule because its volume collapsed. Coinbase and Kraken have the
liquidity but charge 80-120 bps taker, against roughly 1 bps on the Tier-0
schedule. The conclusion the data forces: **no US spot venue supports
taker-based micro-profit scalping.**

A 30-day backtest returned **-7.99% net**, with **57% of the gross loss
attributable to slippage alone**. The system was working correctly; the
opportunity was not there.

---

## 2. Alpha versus being long in a rising market

All 139 trades in the live paper sample were LONG, and the 12.66 hours they
covered were a one-directional rally (ETHUSD **+484.4 bps** over the window). In
a band like that a long-only scalper posts a high hit rate whether or not it
predicts anything. The honest benchmark is **holding the same position, in the
same symbol, over the same interval.**

`api/scripts/beta_check.py`, ALPHA = RAW − MARKET, per trade:

| Strategy | n | Raw bps | Market bps | **Alpha bps** | 95% CI | Hold |
|---|---|---|---|---|---|---|
| **flow_impulse** | 48 | 12.10 | **0.18** | **+11.93** | **[+5.43, +18.42]** | 32s |
| momentum_scalp | 67 | 6.33 | 5.48 | +0.85 | [−2.11, +3.82] | 71s |
| book_imbalance | 21 | 2.66 | 2.11 | +0.55 | [−1.89, +2.99] | 114s |
| breakout_scalp | 3 | −1.41 | 0.00 | −1.41 | — | 53s |
| **TOTAL** | 139 | 7.60 | 3.02 | +4.58 | [+1.77, +7.39] | 64s |

**`momentum_scalp` was beta**: the market supplied 5.48 of its 6.33 bps and the
remainder is indistinguishable from zero. The 120-day backtest and the live
sample reached the same verdict by two independent routes. `book_imbalance` is
the same story.

`flow_impulse` is not. Robustness checks on its 48 trades: t = 3.60; Bonferroni
x4 CI [+3.64, +20.21]; median +9.76 bps (so not one outlier); 36 of 48 trades
positive; +10.47 bps with the best trade removed and +7.35 bps with the best
three removed; +4.59 / +19.26 bps across the two halves; positive in all three
symbols. It is the only net-positive line in the project.

---

## 3. The out-of-sample search (`docs/PHASE3_REPORT.md`)

1,400 candidates, 10 families, 4 assets, 7 regimes, 9 horizons, 120 days of
history plus a complete 863k-trade tape, with walk-forward validation enforced.

| Pooled out-of-sample | Value |
|---|---|
| Trades | **590,306** |
| Net | **−$92,365** |
| Profit factor | **0.22** |

**Nothing was promoted.** The single "marginal" arm was PF 1.00 over 30 trades,
which is noise.

The horizon curve rises monotonically (PF 0.07 at 2 minutes to 0.35 at 60), so
longer genuinely is better — and still nowhere near 1.20. The hit rate required
was 69-100%; the hit rate realised was 6-39%.

Two findings must be kept apart:

* **the market fact** — the hit rate is not there;
* **the defect** — at a nominal 4:1 reward/risk, the average win was $0.22
  against an average loss of $0.28. Targets were not being reached while stops
  were. The defect has to be fixed first, or you are measuring with a broken
  instrument.

Slippage ran roughly 3x commission everywhere. Flow and volume families could
not be tested at all (130 candidates produced 0 signals over 120 days).
Sub-minute measurement is not possible here: 89.9% of BTC 5-second buckets are
empty, 51% of 1-minute buckets are, and honest density starts at 3-5 minutes.

---

## 4. Can maker execution rescue it? (two separate questions)

**Question A — can `flow_impulse` use maker fills?** The 48 live signals were
replayed against recorded L2 and tape:

| Wait | Maker fill | TAKER net | MAKER net | HYBRID net | Markout |
|---|---|---|---|---|---|
| 50 ms | **0.0%** | $4.2410 | $0.0000 | $4.3525 | — |
| 250 ms | **0.0%** | $4.2410 | $0.0000 | $4.0214 | — |
| 1,000 ms | 6.2% | $4.2410 | $0.1038 | $3.8838 | +0.54 bps |
| 2,000 ms | 12.5% | $4.2410 | $0.1694 | $3.0473 | +0.62 bps |

**Maker execution does not exist at this time scale.** A post-only quote at the
touch or one tick behind fills **0%** within half a second. Patience buys fills
and costs more than it buys. Hybrid is **indistinguishable from taker**: the
entire spread across all wait windows is 0.8 of one standard error of the taker
total, so reporting the 100 ms row as a "6.6% improvement" would be reading
noise. Pure maker is the worst arm: $0.17 captured against $3.65 of signals
never filled.

**Question B — does the venue reward resting?** A different question, and the
answer varies by symbol. Over 33.94 hours of tape:

| Symbol | Fill 15s | Fill 30s | Markout 5s | Verdict |
|---|---|---|---|---|
| **BTCUSD** | **22.0%** | **33.0%** | **−0.06 bps** | **FLAT** |
| ETHUSD | 3.0% | 5.0% | −2.07 bps | **ADVERSE SELECTION** |
| SOLUSD | 2.0% | 4.0% | −1.75 bps | **ADVERSE SELECTION** |

**Read markout before fill rate.** High fill with negative markout is worse than
low fill with flat markout: it means the 0% maker fee is being repaid in price.

A naive maker backtest would have overstated fills by 33%: of 220 quote cells
that were touched, 54 were touched without filling. That is why the study models
the **queue** rather than the price.

**Synthesis:** a narrower conclusion than "maker does not work here" — there is
a real maker opportunity in BTCUSD at the 15-30 second horizon, and this project
has no strategy with edge at that horizon. `flow_impulse` is too fast to wait;
`momentum_scalp` is slow enough and is beta. That is a gap to name, not a door
to close. It does not rescue the current book.

---

## 5. Capacity (`docs/PHASE8_ACTIVITY.md`)

The largest single defect found: the capacity cap bounded *universe entry* but
never *order size*. With the clamp moved into `RiskEngine._apply_clamps` — the
one gate both the live allocator and the backtest pass through — the measured
loss on the same data fell from **−$2,012 to −$43.91**, purely by not walking
the book.

At capacity-respecting size, utilisation is **1.10%** ($55 of $5,000). That is
the depth of the venue, not a policy choice: ETHUSD absorbs about $22 a minute.

Relaxing entry conditions raises supply from 74 to 249 setups/day without
degrading per-trade expectancy (−0.0520 to −0.0556) — so the tightness was
throttling activity rather than buying quality. Every profile is still negative,
so relaxation only enlarges the loss.

---

## 6. What is deliberately not claimed

* **`flow_impulse` is not proven.** One day, one regime, 48 trades, 93.8% hit
  rate. It is the best thing measured in this project and it is still a sample.
* **The 100 ms hybrid row is not a 6.6% improvement.** It is inside the noise.
* **Lifting the Tier-0 restriction is not expected to help.** The four WARM
  pairs are 5-9x behind on edge ratio at our horizons. They are scanned so the
  claim stays falsifiable, not because it is doubted.
* **Nothing here says this account will make money.** Phases 3-5 said it will
  not.
* **No threshold was moved to improve a result.** The gates exist precisely to
  refuse that.

---

## Appendix A — the tools that produced these numbers

| Tool | What it produces |
|---|---|
| `api/scripts/pnl_audit.py` | RAW/NET decomposition, per-trade violation check |
| `api/scripts/beta_check.py` | ALPHA = RAW − MARKET, per trade, with confidence intervals |
| `api/scripts/funnel_audit.py` | Per-symbol funnel counts across the whole venue |
| `api/scripts/universe_sensitivity.py` | Liquidity-bar and fee-tier sensitivity |
| `api/scripts/holding_buckets.py` | Per-strategy holding-bucket performance |
| `api/scripts/flow_execution_study.py` | TAKER/MAKER/HYBRID at millisecond scale |
| `api/scripts/run_execution_study.py` | Venue-wide quote grid, markout, queue model |
| `api/scripts/flow_gates.py` | Promotion gates — reports, never promotes |
| `api/scripts/liveconfig_backtest.py` | 120-day replay with the deployed configuration |
| `api/scripts/phase3_search.py` | Tournament and gates, symbol by symbol |
| `api/scripts/horizon_ceiling.py` | Move distribution per horizon |

## Appendix B — phase documents

`EXCHANGE_DECISION.md` · `ECONOMICS.md` · `INTERFACES.md` · `RUNBOOK.md` ·
`FINAL_REPORT.md` · `V2_SPEC.md` · `V2_REPORT.md` · `PHASE3_SPEC.md` ·
`PHASE3_REPORT.md` · `PHASE4_SPEC.md` · `PHASE4_REPORT.md` · `PHASE5_SPEC.md` ·
`PHASE5_REPORT.md` · `PNL_AUDIT.md` · `PHASE6_SPEC.md` · `PHASE6_REPORT.md` ·
`PHASE8_ACTIVITY.md`
