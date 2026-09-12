# Phase 6 — multi-asset selection, strategy-specific holding, capital recycling

Date: 2026-09-12 · Mode: **PAPER** · Real orders placed: **0**
Live: <https://your-domain.example/cryptobot>

---

## The answer, in one paragraph

**BTC was not dominating, and the universe is not artificially narrow — the
venue is.** BTC is **41.7%** of live trades (58 of 139), so the 70%
concentration flag would not have fired. Of Binance.US's **629 listed pairs**,
416 are eligible by quote asset; **160 are halted by the venue** and **233 trade
less than $100k a day**. That is **94.5% of the venue gone for reasons we do not
control.** The Tier-0 allowlist — the thing the brief suspected — costs **13
pairs**. What was genuinely broken is smaller and more specific: the worker was
calling the wrong universe method, and nothing anywhere measured whether a pair
can absorb the size *we* trade.

---

## 1. Why BTC appeared to dominate — root cause

Two real defects, both upstream of any ranking decision, and neither of them a
preference for BTC.

### Defect 1 — the tiering built in Phase 5 was never connected

`app/worker/main.py` called `universe_selector.refresh()`. Phase 5 built
`refresh_tiers()` / `select_tiers()` — HOT/WARM/COLD over the whole venue,
ranked on `net_score` (cost first) — wrote tests for it, and **never wired it
in**. The live path therefore ranked on the original `score`, which is
liquidity-first, truncated at `max_symbols`, and applied no capacity check.

This is the fourth instance in this project of a component that was correct,
tested, and not reachable from production. Fixed, with the old path retained as
a fallback so that a failed depth probe degrades to "trade the old way" instead
of "have no universe".

### Defect 2 — nothing measured whether a pair can absorb *our* size

The brief's §3 asks for "enough liquidity, not maximum liquidity". Ranking on
24h volume answers a different question, and BTC wins it permanently. The
measure that answers the right one is the order as a share of **one minute** of
the pair's own flow — one minute because a 32-second scalp competes with the
flow arriving in roughly that window, not with the 24h total.

Measured on the venue, 2026-09-12:

| Pair | 24h quote volume | absorbs / min | $25 order | $50 order |
|---|---|---|---|---|
| ETHUSD | $1,778,425 | **$61.75** | 2.0% | 4.0% |
| BTCUSD | $1,190,091 | **$41.32** | 3.0% | 6.0% |
| SOLUSD | $301,108 | **$10.46** | 12.0% | 23.9% |
| ADAUSD | $161,688 | **$5.61** | 22.3% | 44.5% |
| BNBUSD | $56,091 | $1.95 | 64.2% | 128.4% |
| SUIUSD | $24,746 | $0.86 | 145.5% | 291.0% |
| AVAXUSD | $9,493 | $0.33 | 379.2% | 758.5% |

**This explains ADAUSD.** It sat in the HOT tier for the whole of Phase 5 and
never traded once. Its flow absorbs **$5.61 a minute against a $10 minimum
order** — it was never tradable, and nothing in the system could say so.

Capacity **sizes**, it does not delist: the right response to a thin pair is a
$10 order, not the removal of the pair. It rejects only when the pair cannot
absorb even the minimum order — the honest "untradable at any size" condition.
That distinction is deliberate: gating on the largest *permitted* size rather
than the size actually used is the exact mistake Phase 5's risk gate made, and
it misdiagnosed the dominant idle reason when it did.

### What was NOT wrong

Searched for and **not found**: BTC hardcoding in any strategy, model or
allocator; BTC-only training data; BTC-only feature availability; symbol mapping
bugs; WebSocket subscription gaps; stale-data differences by symbol; strategy
availability differences; an allocator that prefers BTC. The USD/USDT dedupe and
the Tier-0 filter ordering were already fixed in Phase 5 (the filter must run
*before* the dedupe, or BTCUSDT wins on volume and the fallback lands on a 10
bps pair).

Raw-volume ranking that permanently favours BTC: **partially true and already
mitigated** — the liquidity term is log-scaled and saturates at
`LIQUIDITY_SPAN`× the minimum, so it does not reward volume without limit. The
capacity measure now covers what the saturation missed.

## 2. The venue, counted

`scripts/funnel_audit.py`, run against live public metadata:

| | Pairs | Share |
|---|---|---|
| Listed on Binance.US | **629** | |
| Eligible by quote asset (USD/USDT) | **416** | 100% |
| — 24h quote volume below the bar | 233 | **56.0%** |
| — halted by the venue (`status BREAK`) | 160 | **38.5%** |
| — the hardcoded Tier-0 allowlist | 13 | **3.1%** |
| — denylisted (stablecoins, leveraged tokens) | 5 | 1.2% |
| **Selected** | **4** | 1.0% |

Turning the Tier-0 restriction off takes the universe from 4 to **7** pairs, at
a median round trip of **20.7 bps** against **2.5**.

## 3. Is the liquidity bar the constraint? No — capacity is

With the capacity gate active, lowering `UNIVERSE_MIN_QUOTE_VOLUME_24H` from
$100,000 to $1,000 changes the tradable universe by **exactly zero pairs**.
Every pair the looser bar admits absorbs less than $10 a minute.

| min 24h volume | min trades | Tier-0 only | tradable pairs | median round trip |
|---|---|---|---|---|
| $100,000 | 200 | yes | **3** | 2.5 bps |
| $50,000 | 100 | yes | **3** | 2.5 bps |
| $10,000 | 20 | yes | **3** | 2.5 bps |
| $1,000 | 5 | yes | **3** | 2.5 bps |
| $100,000 | 200 | no | **7** | 20.7 bps |

## 4. Should we trade the other four? The evidence, at the horizons we hold

The brief says not to restrict to BTC/ETH/SOL "unless evidence proves that is
optimal". Here is the evidence. Expected move scales with √time, so a 60-minute
edge ratio flatters a 32-second strategy; this is each pair's ratio at the
horizon each strategy actually holds for:

| Pair | round trip | 60 min | **flow_impulse (32 s)** | book_imbalance (114 s) | momentum_scalp (71 s) |
|---|---|---|---|---|---|
| ETHUSD | 2.5 bps | 19.52 | **1.84** | 3.47 | 2.74 |
| BTCUSD | 2.5 bps | 10.55 | **0.99** | 1.87 | 1.48 |
| SOLUSD | 4.0 bps | 10.26 | **0.97** | 1.82 | 1.44 |
| ZECUSDT | 28.4 bps | 2.22 | 0.21 | 0.40 | 0.31 |
| XRPUSDT | 20.7 bps | 2.20 | 0.21 | 0.39 | 0.31 |
| DOGEUSDT | 24.7 bps | 1.52 | 0.14 | 0.27 | 0.21 |
| HYPEUSD | 25.1 bps | 1.37 | 0.13 | 0.24 | 0.19 |

At `flow_impulse`'s real horizon a non-Tier-0 pair needs a move roughly **8×
larger** than BTC's to break even on the same signal. Restricting *execution* to
the Tier-0 pairs is therefore justified by measurement rather than by habit.

**But restriction is the wrong mechanism, and it has been removed as one.** The
four WARM pairs are now scanned, scored and ranked on net EV like everything
else, and they appear on the opportunity board with the reason they lost. A
claim that survives only because the alternative was never evaluated is not a
finding — so the alternative is evaluated, every cycle, and the funnel records
the outcome. If XRP ever genuinely out-ranks BTC on net expected value, XRP
trades.

---

## 5. Holding behaviour, measured before it was changed

139 live trades. **No trade in this project has ever lasted longer than 293
seconds.** The 5–10m, 10–25m, 25–60m and >60m buckets are all empty, so the
25-minute checkpoint and 60-minute time stop built in V2 have **never once
fired**.

| Strategy | n | median | mean | max | net $ | exit reasons |
|---|---|---|---|---|---|---|
| momentum_scalp | 67 | **27 s** | 71 s | 293 s | +1.17 | 33 TP · 18 SL · 16 trail |
| flow_impulse | 48 | **24 s** | 32 s | 122 s | **+4.24** | 18 TP · 3 SL · 27 trail |
| book_imbalance | 21 | 89 s | 114 s | 275 s | −5.57 | 5 TP · 10 SL · 6 trail |
| breakout_scalp | 3 | 53 s | 53 s | 53 s | −1.12 | 3 SL |

The brief's worry was that `flow_impulse` might be forced to sit for 25 minutes.
**The opposite was happening.** `momentum_scalp` — a strategy that needs time to
develop — has a **27-second median hold**. That is the churn the brief names,
and it was measured rather than assumed.

### Net P&L by holding bucket, per strategy — never pooled

| Strategy | <30s | 30–60s | 1–2m | 2–5m | 5–10m … >60m |
|---|---|---|---|---|---|
| momentum_scalp | +0.80 (39) | +0.60 (4) | +0.47 (3) | **−0.69 (21)** | — |
| **flow_impulse** | **+1.85 (30)** | **+1.47 (12)** | +0.36 (3) | +0.57 (3) | — |
| book_imbalance | −1.03 (2) | −1.84 (6) | −1.87 (5) | −0.83 (8) | — |
| breakout_scalp | — | −1.12 (3) | — | — | — |

`flow_impulse` is positive in every bucket it occupies. `book_imbalance` is
negative in every one of its. An empty bucket renders empty, never as a zero —
"no trades here" and "these trades netted nothing" are different facts.

### The holding profiles, as implemented

| Strategy | min | typical | max | decay exit | re-eval entry score |
|---|---|---|---|---|---|
| flow_impulse | **0 s** | 30 s | 180 s | yes, 20 s halflife | yes |
| book_imbalance | **0 s** | 120 s | 600 s | yes, 90 s halflife | yes |
| momentum_scalp | 60 s | 600 s | 2400 s | no | **no** |
| breakout_scalp | 60 s | 900 s | 3600 s | no | **no** |
| trend_pullback | 120 s | 1500 s | 3600 s | no | **no** |
| mean_reversion | 60 s | 600 s | 2400 s | no | own stretch targets |

Decay is **measured, not timed**:
`decay = 1 − clamp(trigger_now / trigger_at_entry)`, exiting when
`decay > 0.5^(held / halflife)`. At entry the tolerance is 1.0, so nothing can
decay out immediately; an **intact** impulse survives every tolerance at any
age. A 30-second `flow_impulse` trade is correct when the impulse is spent, and
a 120-second one is correct when it is not. No universal minimum hold exists,
and `min_hold_seconds` is zero for both flow strategies by design.

## 6. TAKER vs MAKER vs HYBRID on flow_impulse's own signals

48 `flow_impulse` signals, replayed against **23.0 hours** of recorded L2 and
tape (4,900 BTC prints, 2,414 ETH, 967 SOL). Wait windows chosen for a signal
whose median hold is 32 seconds — waiting 20 seconds for a maker fill on a
30-second alpha measures the alpha decaying, not the execution.

| maker wait | **maker fill %** | hybrid cross % | TAKER net | MAKER net | MAKER after opp. cost | HYBRID net | markout |
|---|---|---|---|---|---|---|---|
| 50 ms | **0.0%** | 100% | $4.2410 | $0.0000 | −$4.2410 | $4.3525 | — |
| 100 ms | **0.0%** | 100% | $4.2410 | $0.0000 | −$4.2410 | **$4.5227** | — |
| 250 ms | **0.0%** | 100% | $4.2410 | $0.0000 | −$4.2410 | $4.0214 | — |
| 500 ms | **0.0%** | 100% | $4.2410 | $0.0000 | −$4.2410 | $4.2532 | — |
| 1,000 ms | 6.2% | 93.8% | $4.2410 | $0.1038 | −$4.0803 | $3.8838 | +0.54 bps |
| 2,000 ms | 12.5% | 87.5% | $4.2410 | $0.1694 | −$3.6538 | $3.0473 | +0.62 bps |

**Maker execution is not available at this timescale.** A post-only quote at or
one tick behind the touch fills **0% of the time within half a second**. Fill
rate rises with patience — 6.2% at one second, 12.5% at two — and hybrid's net
*falls* as it waits: $4.52 → $3.88 → $3.05. The markout on the fills that do
happen is mildly **positive** (+0.54 to +0.62 bps), so those fills are not
adversely selected; there simply are not enough of them, and the waiting costs
more than the fee saves.

**Hybrid is indistinguishable from taker.** Every arm lands between $3.88 and
$4.52 — a spread of $0.64 — against a taker total of $4.2410 whose own standard
error is **$0.80**. The entire observed spread across every wait window is
**0.8 of one standard error**. There is no measurable difference, and reporting
the 100 ms row as a 6.6% improvement would be reading noise.

**Pure maker is the worst arm by far** once the signals it never filled are
charged to it: $0.17 earned against $3.65 of missed trades, i.e. **−$3.49**.
Skipping the losers is only an achievement if you did not also skip the winners.

This is the second independent reason the maker thesis is finished. The P&L
audit killed it for the bar-driven strategies because they lose at the mid;
this kills it for `flow_impulse` because you cannot rest a quote for long enough
to be filled without outliving the alpha you are trying to capture.
