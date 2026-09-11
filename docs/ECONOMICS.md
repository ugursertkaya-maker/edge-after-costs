# The economics of a micro-profit bot — the finding that shapes everything

This came out of building the signal layer, and it constrains the whole system,
so it is written down rather than buried in a config default.

## Part 1 — the fee wall

A 1-minute ATR on a *typical* liquid crypto venue sits around 15–25 bps, and an
honest short-horizon target is scaled from that ATR. Price the round trip
against a 20 bps target:

| Fee tier | Entry | Exit | Round trip | Plus spread (~2 bps) | Net of a 20 bps target |
|---|---|---|---|---|---|
| Binance.US **standard** (0.10%) | 10 bps | 10 bps | **20 bps** | 22 bps | **−2 bps** |
| Binance.US **Tier 0** (0% / 0.01%) | 0 bps | 1 bps | **1 bps** | 3 bps | **+17 bps** |
| Kraken base (0.40% / 0.80%) | 40 bps | 80 bps | **120 bps** | 122 bps | **−102 bps** |
| Coinbase base (0.40% / 0.60%) | 40 bps | 60 bps | **100 bps** | 102 bps | **−82 bps** |

At the standard tier the strategy is not merely unprofitable, it is
**structurally impossible**: the cost of transacting exceeds the entire
distribution of moves it is trying to capture. No amount of signal quality
fixes a negative constant.

The test `test_build_subtracts_round_trip_fees_spread_and_slippage` asserts
this arithmetic explicitly so nobody can quietly optimise it away.

## The consequence, which is now enforced in code

1. `FEE_MODEL` defaults to **`tier0`** — the per-pair table that reflects what
   we would actually pay, not a flat average.
2. `UNIVERSE_RESTRICT_TIER0` defaults to **true**: the scanner will not even
   look at a pair whose fee tier cannot fund an ATR-sized target. It falls back
   to the unrestricted set only if the Tier-0 filter would leave the universe
   empty, so a stale fee table degrades to "trade badly" rather than "go blind".
3. Every reported result is produced **twice**: once at Tier 0, once at the
   standard tier as a stress case. If the standard-tier run is negative, that
   is not a failure of the run — it is the finding.

## Part 2 — the harder problem, measured on the real venue

The table above was built on a generic 15–25 bps ATR. **Binance.US is not a
generic venue.** Measured on 240 live 1-minute bars, 2026-09-11 03:0x UTC:

| Pair | 1m ATR | Realized vol (30×1m) | Spread | Depth ±10 bps |
|---|---|---|---|---|
| BTC/USD | **2.2 bps** | 5.0 bps | 0.50 bps | $318k |
| ETH/USD | **2.0 bps** | 3.6 bps | 0.53 bps | $89k |
| SOL/USD | **3.7 bps** | 4.7 bps | 2.02 bps | $23k |

An ATR-scaled target on BTC/USD is therefore about **2 bps gross, not 20**.
Now the Tier-0 round trip — 1 bps of fee plus ~0.5–2 bps of spread plus
slippage — is no longer negligible against the target. It is most of it.

This is the real constraint, and it is worse than the fee problem because no
fee schedule fixes it: **on Binance.US, one-minute moves on the majors are
roughly the same size as the cost of trading them.** The fee wall rules out
three of the four exchanges; this rules out most of the *horizon*.

The system's response is to refuse, which is correct — `min_net_edge_bps`
rejects a setup whose expected net edge is below the gate, so a thin market
simply produces few or no trades rather than a stream of negative-expectancy
ones. A bot that does nothing on a day with no edge is behaving properly. But
it does mean headline trade counts will be low, and it points at the horizon,
not the signal, as the thing to change: if there is an edge here it lives at a
5–30 minute holding time where the move is several multiples of the spread, not
at 60 seconds.

## Part 3 — the decisive measurement: there is almost no flow

Measured over **258,657 one-minute bars per pair** (≈180 days) pulled from
Binance.US itself:

| Pair | 1m bars | Bars with **zero trades** | Avg $ volume per minute | Avg trades per minute |
|---|---|---|---|---|
| BTC/USD | 258,657 | **143,504 (55.5%)** | $753 | 2.19 |
| ETH/USD | 258,657 | **179,104 (69.2%)** | $595 | 1.21 |
| SOL/USD | 258,657 | **193,878 (75.0%)** | $200 | 0.72 |
| ADA/USD | 258,657 | **225,799 (87.3%)** | $88 | 0.26 |

More than half of BTC/USD's minutes contain **no trade at all**. A momentum or
flow strategy has, most of the time, literally nothing to measure.

Against the other two venues, the same asset on the same day:

| Venue | BTC/USD 24h volume | Trades / 24h | Taker fee | Round-trip cost |
|---|---|---|---|---|
| **Binance.US** | **$1.6M** | ~3,000 | 0.01% | **~1 bps** |
| Kraken | $198M (**126×**) | 109,419 (**36×**) | 0.40% base | ~80 bps |
| Coinbase | $490M (**312×**) | — | 0.60% base | ~120 bps |

That is the whole problem in one table, and it is a genuine fork with no good
branch:

* **Binance.US** has the fees a micro-profit bot needs and none of the flow.
* **Coinbase and Kraken** have the flow and fees that are 80–120× too high.

Binance.US's fees are low *because* its volume collapsed — the near-zero
schedule is an acquisition subsidy for a venue at ~0.2% US market share. The
cheap fee and the missing liquidity are the same fact, not two independent ones,
so there is no venue that offers both.

**Conclusion: no US-regulated spot venue currently supports a taker-driven
micro-profit scalper.** That is a finding about the market, not a defect in the
bot, and the backtests confirm it — see the final report.

### The one structure that is not ruled out

Binance.US charges **0% maker**. A passive, post-only strategy there would pay
no fee *and* avoid the slippage that dominates the backtest losses (57% of the
gross loss in the 30-day run was slippage paid crossing the spread). Tight
quotes with thin flow is the textbook setting for earning the spread rather
than paying it.

That is a different system — quoting, queue position, inventory and adverse
selection instead of scanning and crossing — and it is the honest V2 direction.
The simulator already models resting limit orders, queue misses and maker fees,
so the machinery to test it exists.

## Part 4 — a correction: the horizon, measured properly

Parts 2 and 3 scaled a 1-minute ATR up with `sqrt(t)` to estimate what price
does over a holding period. That estimate was too pessimistic, and measuring it
directly on 242 days of real data corrects it. Median **absolute** move, in bps
(`api/scripts/horizon_ceiling.py`):

| Horizon | BTC/USD | ETH/USD | SOL/USD | BTC: % of windows > 3 bps | > 6 bps |
|---|---|---|---|---|---|
| 2m | **0.9** | 0.0 | 0.0 | 38% | 26% |
| 5m | 4.5 | 3.6 | 3.8 | 58% | 43% |
| 15m | 9.9 | 12.6 | 15.3 | 78% | 65% |
| **25m** | **12.9** | **17.4** | **21.2** | 84% | 72% |
| **60m** | **19.7** | **27.3** | **33.3** | 91% | 82% |
| 120m | 28.0 | 37.8 | 45.8 | 93% | 87% |

The tails are much fatter than diffusion implies — BTC's 90th percentile at 60
minutes is 68 bps against a 20 bps median.

**This changes the conclusion, and the change is worth stating plainly.** The
claim in Part 2 — that the move and the cost of capturing it are the same size —
holds at **one minute** and is wrong at **25–60 minutes**, where the median move
is four to ten times the ~3 bps taker cost floor. V1 and V2 lost money at an
average hold of 3–8 minutes. That now looks like a consequence of exiting too
early, not proof that the venue cannot be traded.

What the venue actually demands is a **hit rate**. A symmetric-payoff strategy
captures `(2p − 1) × E|move| − costs`, so break-even needs:

```
p > 0.5 + costs / (2 × E|move|)
```

| Horizon | E&#124;move&#124; (BTC) | Taker break-even p | Maker break-even p |
|---|---|---|---|
| 2m | 0.9 bps | **>90%** — hopeless | ~50% |
| 25m | 12.9 bps | **~62%** | ~50% |
| 60m | 19.7 bps | **~58%** | ~50% |

So: as a **taker**, the system needs to call direction right about 58–62% of the
time at a 25–60 minute horizon. That is demanding but not absurd for a
selective, low-frequency signal. As a **maker**, with 0% fees and the spread
earned rather than paid, break-even sits at roughly a coin flip — which is why
the maker study is the priority and why the tournament weights longer horizons.

SOL/USD moves the most (33 bps median at 60 minutes against BTC's 20) despite
thinner depth, so it should not be dropped on liquidity alone.

## What this means for the user

**The viability of this bot is a direct function of the Binance.US Tier-0
promotional fee schedule.** That schedule is the edge. It is not permanent and
Binance.US has changed it before.

Before any live phase, re-verify the Tier-0 list and rates. If Tier 0 is
withdrawn or the traded pair leaves the list, the honest response is to stop the
bot, not to widen the targets — widening the target to cover a 20 bps fee turns
a micro-profit scalper into a directional swing bot, which is a different system
with a different (and unvalidated) edge.

A monitor for this belongs in a future phase: compare the realised fee on the
authenticated account against the modelled fee, and trip the kill switch when
they diverge.
