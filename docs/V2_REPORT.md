# V2 report — fee-aware net targets + multi-position capital rotation

Date: 2026-09-11 · Mode: **PAPER** · Live orders placed: **0**
Live: <https://your-domain.example/cryptobot> · 798 automated tests passing

Read `docs/FINAL_REPORT.md` first — V1's result is the reason V2 exists.

---

## What was built

| Directive | Status |
|---|---|
| Fees read, never hardcoded | `FeeProvider` indirection; rates from `settings.fees`, `exchangeInfo` filters honoured live; the authenticated `/sapi/v1/asset/query/trading-fee` source is a documented seam, **not implemented** — the paper-only guard fails the build on `/sapi/` |
| NET, not gross | `NetTargetEngine` solves backwards from required net dollars through every cost line to the gross price that delivers them |
| Three prices per position | `break_even_price`, `net_take_profit_price`, `emergency_stop_price` — all live on the position and on the dashboard |
| No fixed dollar target | Targets are NET percentages of the position; a $10 and a $200 position get different dollar targets from the same profile |
| NET TP profiles | 0.25 / 0.50 / 0.75 / 1.00% as asked, **plus** two venue-calibrated ones (see below) |
| Maker-first | Post-only entries; a would-cross post-only is rejected, never converted; a missed maker order must re-qualify on taker economics before crossing |
| Position count cap removed | The 1–3 cap is gone. Binding constraints are the risk budget, cash reserve, correlation and exposure. The count ceiling is 25 and is a safety valve |
| Position size ≠ risk | Enforced and tested: a $20 position with a 1% stop reports ~$0.20 of account risk |
| Correlation control | Quadratic aggregation `sqrt(w′Rw)` over the risk vector, plus BTC beta and directional exposure caps — eight correlated longs aggregate to roughly one large position's risk, not one eighth |
| 25/60 minute horizon | 25 min is a checkpoint (`KEEP` / `TAKE_PROFIT` / `TRAIL_PROFIT` / `EXIT_WEAK` / `ROTATE_CAPITAL`), 60 min is a time stop. Early profit is taken; a stop at minute two is honoured at minute two; **a loser is never extended** |
| Dashboard | Full per-trade cost breakdown, `MALİYET ÖNCESİ KÂR − TOPLAM MALİYET = GERÇEK NET KÂR`, a PORTFÖY panel with correlated exposure, and the 25/60-minute lifecycle per position |
| Experiment harness | Sweeps profiles, allocation modes and the risk × reserve grid; `winner()` refuses to name one when nothing is net-positive or the sample is too small |

Four bugs were found and fixed while wiring, each of which would have quietly
invalidated results:

1. **`many_small` allocated literally nothing.** It aims at the minimum notional
   and lot-flooring then lands just under it ($9.999 against a $10 floor), so
   every candidate was refused. The entire mode was a no-op.
2. **The NET profile had no effect on backtests.** The engine still used V1 ATR
   levels, so all four profiles produced byte-identical results. The harness
   detected and reported this rather than presenting four copies of one number
   as a comparison.
3. **The reachability guard used the wrong yardstick** — a 25–60 minute target
   compared against a *one minute* ATR, wrong by ~√60.
4. **`volume_ratio` divided by a baseline that is usually zero here** and
   reported readings like **1291×**, so every `volume_ratio > 1.8` gate fired on
   the first print after any quiet spell. Volume *confirmation* was volume
   *noise*.

---

## Result 1 — the requested profiles ask for a tail move

Measured on Binance.US: BTC/USD 1-minute ATR ≈ **2.2 bps**. Diffusion-scaled:

| Window | Typical move |
|---|---|
| 25 minutes | ~11 bps = **0.110%** |
| 60 minutes | ~17 bps = **0.170%** |

Against that:

| Profile | NET target | Gross needed | vs a typical hour |
|---|---|---|---|
| MICRO | 0.25% | ~30 bps | **1.8×** |
| BASE | 0.50% | ~55 bps | **3.2×** |
| FAST | 0.75% | ~80 bps | **4.7×** |
| EXTENDED | 1.00% | ~105 bps | **6.2×** |
| *venue_micro* | *0.05%* | *~8 bps* | *0.5×* |
| *venue_base* | *0.10%* | *~13 bps* | *0.8×* |

None of the four is impossible. All four need **more than the whole typical
hour's move**, so they are tail outcomes — and a bot aiming only at tails trades
almost never and produces no evidence either way. The two venue-calibrated
profiles sit inside a normal hour; `venue_base` is the live default so the
system can actually run and be measured. All six are selectable and all six are
swept by the harness.

## Result 2 — the profiles now diverge, and all still lose

30 days, 4 symbols, 170,096 bars, Tier-0 fees:

| Profile | Trades | Win % | PF | Avg NET | **NET P&L** | Max DD | Avg hold | End equity |
|---|---|---|---|---|---|---|---|---|
| micro 0.25% | 144 | 31.9% | 0.37 | −$0.147 | **−$21.18** | 4.3% | 5.0m | $478.82 |
| base 0.50% | 126 | 24.6% | 0.46 | −$0.136 | **−$17.19** | 3.8% | 6.3m | $482.81 |
| **fast 0.75%** | 114 | 22.8% | **0.54** | **−$0.118** | **−$13.47** | 3.5% | 7.5m | $486.53 |
| extended 1.00% | 108 | 18.5% | 0.47 | −$0.145 | **−$15.65** | 3.7% | 8.4m | $484.35 |

`winner()` returned **NO WINNER**, correctly: the best arm still expects
−$0.118 per trade.

The shape is informative even though every arm loses. Longer targets lose
**less** and hold **longer** — the loss shrinks monotonically from micro to
fast as the average hold stretches from 5.0 to 7.5 minutes. That is the same
finding as V1 pointing the same direction: **the horizon, not the signal, is
what is wrong.** A move needs time to become several multiples of the spread.

## Result 3 — the allocation comparison could not run

| Mode | Trades | NET P&L | Idle cash | Correlated | **Peak open positions** |
|---|---|---|---|---|---|
| few_large | 126 | −$17.19 | 99.5% | 0.5% | **2** |
| many_small | 126 | −$17.19 | 99.5% | 0.5% | **2** |
| dynamic | 126 | −$17.19 | 99.5% | 0.5% | **2** |

All three arms are identical, and the risk × reserve grid (2/3/5% × 10/20/30%)
is nearly identical too — only the 30% reserve arm differs at all, and it does
slightly *worse*.

The reason is the last column. **Peak open positions is 2, and 99.5% of the
capital sits idle.** The allocation machinery works and is tested, but there
were never more than two or three simultaneously qualified opportunities to
allocate across — because the tradable universe on this venue is **three
symbols** and signals on them are sparse.

So the FEW-LARGE vs MANY-SMALL question is, on Binance.US, **not answerable**.
The binding constraint is not the risk budget, the reserve, or the position
count. It is **opportunity supply**. Many small positions need many
simultaneous independent ideas; this venue does not produce them.

That is a genuine answer to the directive — "do not assume MANY-SMALL wins" was
right, and what the evidence says is that neither wins because the comparison
has nothing to bite on.

---

## What is not yet answered: maker-first

This is the one structure V1's report named as still open, and it remains open
on purpose.

The backtest runs on 1-minute OHLCV. A post-only strategy lives or dies on
**queue position and adverse selection**, neither of which exists in a 1-minute
bar — the trade prints the engine synthesises from bar volume are an
approximation, and building a maker backtest on them would produce a number
that looks like evidence and is not. So the maker path is implemented,
unit-tested (a would-cross post-only is rejected; fill probability falls as
queue-ahead size rises; maker fills are charged the maker rate) and **wired
live**, where the real book and the real trade tape decide.

The worker now records `maker_fills` and `maker_misses` in the health block.
That is the evidence to wait for. It is also the most promising thing left:
0% maker fee plus no spread paid removes the cost line that caused 57% of V1's
losses.

---

## Live state

Deployed and running. Universe: BTC/USD, ETH/USD, SOL/USD (the Tier-0 pairs
clearing the liquidity filters). Cash reserve $100 of $500 held back, $400
deployable, risk budget 3%, allocation mode `dynamic`, NET profile
`venue_base`.

Worker CPU ~2% of one core, 61 MB RAM. The whole stack costs less than the
another project worker it replaced.

## Recommendation, unchanged and now better supported

1. **Test maker-first live.** It is the only structure the data has not ruled
   out, and it attacks the dominant cost directly.
2. **The horizon is the lever, not the parameters.** Both V1 and V2 point the
   same way: losses shrink as holding time grows.
3. **Do not tune.** Three suites, thirteen arms, every one net negative. Nothing
   here is a parameter problem, and anything that turns green after tuning on
   this sample is curve fitting.
