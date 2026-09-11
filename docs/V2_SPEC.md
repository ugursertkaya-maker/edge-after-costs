# V2 — fee-aware net targets + multi-position capital rotation

Two directives from the user, taken together. V1 stays exactly as it is
(PAPER-only, Binance.US public data, six strategies, the whole risk and
simulation stack). V2 changes **what counts as a target** and **how capital is
spread**.

Read `docs/FINAL_REPORT.md` and `docs/ECONOMICS.md` first — V1's measured result
is that 1-minute taker scalping on Binance.US is net negative, dominated by
slippage. V2's two changes attack exactly that: *maker-first execution* removes
the slippage and the fee, and a *25–60 minute horizon* gives the move room to
be several multiples of the spread. This is a structural change, not a
parameter tune.

---

## A. Exchange and fees — no hardcoding

Binance.US Advanced Spot stays the venue. Do not redesign around another
exchange unless its official API becomes unavailable or technically unsuitable.

Fees must be **read, not assumed**:

* Today: maker 0% across pairs, taker 0.02% or lower, Tier-0 pairs 0.01% taker.
  These are current observations, **not constants**.
* In a future authenticated phase the account's real rates come from
  `GET /sapi/v1/asset/query/trading-fee` → `makerCommission` / `takerCommission`
  per symbol, refreshed periodically. **V1/V2 are PAPER-only and must not
  contain that call** — the paper-only guard in `app/safety.py` fails the build
  on `/sapi/`. Design the fee provider so the authenticated source can be
  dropped in later behind the same interface, and leave a TODO naming the
  endpoint; do not implement it.
* Tick size, step size, min qty, **min notional** and trading status always come
  from `GET /api/v3/exchangeInfo`. The current Advanced-Trading minimum order
  value is around $10 — never hardcode it, always honour the filter.

## B. The governing economic rule

> Never trade because "we can make 10 cents". Decide: *after actual Binance.US
> fees, spread, slippage and execution uncertainty, does this trade offer enough
> NET return relative to the capital and risk required?*

A fixed dollar target is incoherent — $0.50 is 5% of a $10 position and 0.25%
of a $200 one. Targets are **NET percentages of the position**.

For every prospective trade:

```
expected price move
→ expected gross profit
− entry fee
− expected exit fee
− bid/ask spread cost
− estimated entry slippage
− estimated exit slippage
− latency safety buffer
= EXPECTED NET PROFIT
```

Only `EXPECTED NET PROFIT` matters. **A gross gain must never be displayed as
profit.** Enter only if `expected_net_profit > 0` **and** it meets the active
profile's threshold **and** it clears the uncertainty margin.

Every position carries three prices: `break_even_price` (entry fee + expected
exit fee + spread + slippage), `net_take_profit_price`, `emergency_stop_price`.

### NET take-profit profiles (research, all PAPER)

| profile | NET target |
|---|---|
| MICRO | 0.25% |
| BASE | 0.50% |
| FAST | 0.75% |
| EXTENDED | 1.00% |

Do not assume one is best. Evaluate per strategy × coin × regime. The gross
move required is always **larger** than the net target; the engine solves for it.

## C. Maker-first execution

Binance.US charges 0% maker. Prefer **post-only maker limit** orders where
timing allows. But:

* do not assume they fill — model queue position and depth, simulate fill
  probability;
* cancel stale orders; never leave a resting entry after the signal is gone;
* if a maker order misses and the signal is still valid, **recompute the trade
  as a taker** and only cross if it *still* clears the NET gate. Never
  auto-convert a missed maker into a market order.

## D. Portfolio — many small positions, bounded by risk

The 1–3 concurrent-position cap is **removed**. A high count ceiling remains as
a safety valve only (`portfolio.max_open_positions`, default 25); it must not be
the normal constraint. Whether another position may open is decided by:
available cash, total exposure, risk per trade, total open risk, correlation,
liquidity, signal quality, expected net edge, the exchange minimum, current
spread/slippage, and the cash reserve.

Valid notionals are whatever risk produces — $10, $15, $25, $50, $100 are
*examples*, not tiers.

**Position size is not risk.** A $20 position with a 1% stop risks ~$0.20 plus
costs, not $20. This distinction must hold everywhere.

* **Total open risk budget**: research 2% / 3% / 5% of equity. Default 3% → a
  $500 account may carry ~$15 of simultaneous modelled loss, which supports many
  small positions when each stop is tight and justified.
* **Cash reserve**: research 10% / 20% / 30%. Never deploy 100% of cash just
  because signals exist.
* **Correlation**: eight simultaneous crypto longs are one large crypto long.
  Aggregate correlated risk, measure BTC sensitivity and directional portfolio
  exposure, and cap them. "Many small positions" must not secretly become one
  enormous correlated bet.
* **Allocation**: rank all qualified opportunities, then allocate — better
  setups may get more capital, but only via expected net edge, risk, stop
  distance, liquidity, volatility, correlation and available capital. Never
  arbitrary labels.
* **Do not overtrade**: many *allowed* is not many *taken*. Each trade passes
  every gate independently. If 20 coins qualify, open many; if zero qualify,
  open zero.

## E. Horizon — 25 to 60 minutes

Target development window ~25 min; maximum normal hold 60 min.
**25 minutes is a checkpoint, never a lock.**

* **Early profit**: NET TP reached at minute 8 with momentum fading → take it.
  Do not wait for minute 25 and give it back.
* **Early loss**: stop hit → exit immediately. Also exit early on signal
  invalidation, momentum collapse, order-flow reversal, abnormal spread
  expansion, vanishing liquidity, a data-quality problem, or a portfolio risk
  event.
* **25-minute checkpoint**: reassess NET P&L, whether the original signal still
  holds, momentum, volume, order flow, trend, spread, volatility, opportunity
  cost, and whether a materially stronger opportunity exists. Verdicts: `KEEP`,
  `TAKE_PROFIT`, `TRAIL_PROFIT`, `EXIT_WEAK`, `ROTATE_CAPITAL`.
* **60-minute time stop**: default close and release the capital. A scalp must
  not silently become a multi-hour investment. Extension only if profit is
  already protected, a trailing stop is active, the signal is exceptionally
  strong, **and** paper evidence shows extension improves NET expectancy — and
  it is then classified `EXTENDED_WINNER` and measured separately. **Never
  extend a loser to avoid realising it.**

## F. Objective

Capital turnover with positive NET expectancy — not maximum holding time and
not maximum trade count. Ten trades at +$0.30 NET beat fifty that fees and
slippage turn negative.

## G. Dashboard additions

Per open and completed trade: Position Size, Entry, Current, **Gross P&L**,
Entry Fee, Expected/Actual Exit Fee, Spread Cost, Slippage, **Total Trading
Cost**, **NET P&L**, Maker/Taker, Break-even Price, NET TP Price, NET TP %,
Stop, Account Risk $. Plus `PROFIT BEFORE COSTS`, `TOTAL COSTS`, `REAL NET
PROFIT` as three separate values.

Portfolio panel: Open Positions, Capital Deployed, Available Cash, Cash
Reserve, Total Open Risk $ and %, Correlated Exposure, Average and Median
Position Size, Average Holding Time, Positions <25 min / 25–60 min / >60 min,
Capital Turnover per Day, NET Profit per $1 Deployed.

Per open position: Coin, Position $, Account Risk $, Entry Time, Age, Expected
Hold Window, 25-Min Check Status, 60-Min Time Stop, NET P&L, NET TP, Stop,
Signal Strength, Strategy.

## H. Paper experiments to run

1. **NET target profiles**: 0.25 / 0.50 / 0.75 / 1.00%. Report trades, wins,
   losses, win rate, avg gross, avg fees, avg spread+slippage, avg NET, profit
   factor, expectancy/trade, max drawdown, avg holding time, trades/day,
   turnover/day, ending equity. **Do not pick on win rate** — pick on the
   highest repeatable NET expectancy with acceptable drawdown.
2. **Allocation modes**: (A) FEW-LARGE, (B) MANY-SMALL, (C) DYNAMIC. Compare NET
   P&L, fees, slippage, trade count, turnover, max drawdown, profit factor,
   expectancy, idle cash, correlated exposure, average holding time. **Do not
   assume MANY-SMALL wins.**
3. **Risk budgets** 2/3/5% and **reserves** 10/20/30%.

---

## Frozen contracts (already landed — build against these)

`app/config.py`: `ProfitConfig`, `ExecutionStyleConfig`, `PortfolioConfig`,
`HorizonConfig`, reachable as `settings.profit`, `settings.execution_style`,
`settings.portfolio`, `settings.horizon`.

`app/types.py`: `CostBreakdown`, `NetTargets`, `PortfolioState`, `Allocation`;
new `ExitReason.EXIT_WEAK` / `ROTATE_CAPITAL`; new `RejectReason`
`NET_TARGET_UNREACHABLE`, `CASH_RESERVE`, `PORTFOLIO_RISK_BUDGET`,
`CORRELATED_EXPOSURE`, `DIRECTIONAL_EXPOSURE`; `Position` gained `targets`,
`break_even_price`, `checkpoint_ms`, `checkpoint_done`, `entry_is_maker`,
`extended`.

### Dashboard payload additions (worker → API → web)

```
positions[]: + account_risk_usd, age_seconds, expected_hold_window,
             checkpoint_status ("PENDING"|"KEEP"|"TRAIL"|"DUE"),
             time_stop_ms, net_tp_price, net_tp_pct, break_even_price,
             gross_pnl_usd, entry_fee_usd, expected_exit_fee_usd,
             spread_cost_usd, slippage_usd, total_cost_usd, net_pnl_usd,
             maker_or_taker, signal_strength
trades[]:    + the same cost breakdown, plus maker_or_taker and
             net_tp_pct / break_even_price actually used
portfolio:   { open_positions, capital_deployed, available_cash, cash_reserve,
               open_risk_usd, open_risk_pct, correlated_exposure_pct,
               avg_position_usd, median_position_usd, avg_holding_seconds,
               positions_under_25m, positions_25_60m, positions_over_60m,
               capital_turnover_per_day, net_profit_per_dollar_deployed,
               allocation_mode, net_tp_profile }
metrics:     + profit_before_costs, total_costs, real_net_profit
```
