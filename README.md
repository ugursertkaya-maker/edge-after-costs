# EdgeAfterCosts

**Cost-aware quantitative research and execution simulation.**

A framework for answering one question about a trading strategy: does its
apparent edge survive **after costs**? Fees, the bid/ask spread, slippage from
walking a real order book, latency, finite liquidity, fill probability, a change
of regime, and validation on data the parameters never saw — each of those is
modelled explicitly and each of them is allowed to take the edge away.

The name is the thesis. *Edge before costs* is easy to produce and means
nothing; **edge after costs** is the only quantity a decision can be made on,
and this framework is built to measure it honestly enough that the answer is
usually "no".

EdgeAfterCosts is therefore designed as much to *disprove* strategies as to
find them. Most of what it has measured so far is negative, and those results
are published here rather than hidden — see
[Research findings](#research-findings).

> **This is research software. It is paper-only by construction, it has never
> placed a real order, and it is not investment advice.** See
> [Safety model](#safety-model) and [Limitations](#limitations).

---

## Why this project exists

A backtest is very easy to make look good. The usual way it lies is not fraud
but omission — the simulation quietly assumes away the things that actually
determine whether a strategy makes money:

| Assumed away | What EdgeAfterCosts does instead |
|---|---|
| **Fees** | Every candidate is ranked on *net* edge, with the venue's real fee schedule (including tiered and promotional rates) applied per leg |
| **Spread** | Entry pays the spread; the break-even price is computed and displayed per position |
| **Slippage** | The simulator walks the actual order book rather than filling at one price |
| **Latency** | A configurable latency is applied between decision and fill |
| **Liquidity / capacity** | A per-symbol capacity cap bounds *order size*, not just universe entry — the single largest defect this framework found in itself |
| **Fill probability** | Maker fills are never assumed. They are measured from recorded tape, with queue position consumed first |
| **Regime instability** | Every candidate is partitioned across a seven-regime classifier; an edge that only exists in one regime is reported as such |
| **Overfitting** | Walk-forward validation is mandatory; in-sample columns are rendered dimmed and are the basis of no decision |
| **Look-ahead bias** | Feature construction is causal, labels are purged and embargoed, and window ordering is asserted |
| **Survivorship in the search** | Losing candidates stay in the results table — the easiest way for a search to lie is to not show what it tried |

The claim is not that EdgeAfterCosts models these perfectly. The claim is that
it makes each of them **explicit, configurable and testable**, so that a result
can be attacked on its assumptions instead of taken on trust.

---

## Features

**Market data and universe**
- Public REST + WebSocket ingestion with a locally maintained L2 order book
- Dynamic universe selection with liquidity tiers (HOT / WARM / COLD) and
  configurable, measurable admission thresholds
- Raw market tape capture to disk (hourly, gzipped, with retention) for replay

**Signal generation**
- Six independent strategy modules: momentum, breakout, pullback, mean
  reversion, order-book imbalance and trade-flow impulse
- Incremental microstructure and indicator features
- Seven-regime classifier (`TRENDING_UP`, `TRENDING_DOWN`, `RANGING`,
  `VOLATILITY_EXPANSION`, `LOW_VOLATILITY`, `BREAKOUT`, `PANIC`); each strategy
  declares the regimes it is allowed to act in

**Decision and risk**
- Fee-aware ranking by expected **net** edge after fees, spread and slippage
- Risk engine with position sizing, per-position and portfolio exposure caps,
  correlation control, daily/weekly loss limits, loss-streak cooldown and a
  kill switch
- Exit management: take profit, stop loss, trailing, time stop, signal reversal
- Three simulated account sizes ($500 / $1,000 / $5,000) that see **identical
  decisions** and differ only in sizing, so the comparison is like for like

**Research**
- Event-driven backtest engine with walk-forward validation
- Strategy tournament across families x assets x regimes x horizons, with
  promotion gates that report but never promote
- Robustness suite: cost stress (fees +25%/+50%, slippage x2, latency x2, worse
  spread, random order delay), best-trade-removed expectancy, multi-window
  consistency, minimum sample size
- Predictive entry pipeline (labels → design matrix → splits → calibration →
  expected value), exported as a **NumPy-only inference artifact** so the
  worker container never needs scikit-learn; a test asserts the exported
  predictor matches scikit-learn to 1e-9
- Execution studies: maker fill rate, queue modelling and markout from real tape
- 23 standalone research CLIs in `api/scripts/`

**Operations**
- Quantitative research terminal (Next.js) covering scan activity, the
  opportunity board, positions, trades, portfolio, capital utilisation, idle
  capital attribution, missed-opportunity audit, holding buckets, concentration,
  the tournament, model quality and system health
- Safety invariants enforced by tests, including a static AST scan of the source
  tree

**Scale:** ~38,000 lines of application Python across 79 modules, ~28,800 lines
of tests (65 files, 2,005 passing), ~11,700 lines of TypeScript.

---

## Architecture

```
                    ┌──────────────────────────────────────────┐
   public market    │  exchange/  REST · WebSocket · L2 book    │
   data (no keys)   └───────────────────┬──────────────────────┘
                                        │
                            ┌───────────▼───────────┐
                            │ universe.py           │  liquidity tiers,
                            │ features.py           │  incremental features
                            │ regime.py             │  7-regime classifier
                            └───────────┬───────────┘
                                        │
                        ┌───────────────▼───────────────┐
                        │ strategies/  6 modules        │  → Signal
                        │ predict/     ML entry engine  │  → probability, EV
                        └───────────────┬───────────────┘
                                        │
                              ┌─────────▼─────────┐
                              │ ranker.py         │  expected NET edge
                              └─────────┬─────────┘        after all costs
                                        │
                              ┌─────────▼─────────┐
                              │ risk.py           │  ← the mandatory gate
                              │ portfolio.py      │     sizing, exposure,
                              └─────────┬─────────┘     correlation, limits
                                        │
                              ┌─────────▼─────────┐
                              │ sim/  simulator   │  book-walking fills,
                              │       ledger      │  latency, fees, slippage
                              └─────────┬─────────┘
                                        │
                    ┌───────────────────▼───────────────────┐
                    │ exits.py  TP · SL · trailing · time    │
                    │ holding.py  capital released → rescan  │
                    └───────────────────┬───────────────────┘
                                        │
      ┌─────────────────────────────────▼─────────────────────────────────┐
      │ recorder.py → Postgres          runtime.py → Redis → dashboard    │
      └───────────────────────────────────────────────────────────────────┘

      offline:  backtest/  event-driven engine + walk-forward
                research/  tournament · families · robustness · replay
```

There is no arrow out of this diagram to an exchange order endpoint, because
there is no such code path.

---

## Safety model

This is the part to read carefully if you are evaluating the project.

**1. There is no order execution.** The repository contains no request-signing
helper, no API-key header, no authenticated exchange endpoint and no
order-submission call. Market data is read from public, unauthenticated
endpoints.

**2. No exchange credentials are required or accepted.** The application runs
fully without any exchange key, because there is nothing for a key to
authenticate.

**3. Paper-only is enforced structurally, not promised in prose.**
`app/safety.py` implements two gates:

- a **runtime gate** (`assert_paper_mode`) that every process — API, worker,
  backtester — calls before doing anything else, so a mis-set `TRADING_MODE`
  stops the process rather than quietly changing behaviour;
- a **static AST gate** (`source_tree_is_paper_only`) that parses the shipped
  source and scans for the machinery a live order would need: an API-key header,
  a request-signing helper, an authenticated endpoint path, a stored secret, a
  `signature=` parameter. Comments and docstrings are blanked before the scan,
  so inert prose cannot trip it, while a string literal used in an *expression*
  stays in scope. HMAC is judged in context: signing a session cookie is fine,
  HMAC inside the trading path is a violation.

The API refuses to start if the static scan returns anything.
`tests/test_paper_only.py` asserts the same property from four directions, so
a future commit that adds signing code fails CI **before** that code can run.

**4. The UI states it.** The header rail shows `PAPER` and a live-orders flag
that is expected to read `OFF`; if it ever read `ON`, the dashboard treats that
as a defect and says so.

What this model does *not* claim: it does not make the strategies safe, correct
or profitable, and it is not a substitute for your own review before running
anything against real money. It guarantees one narrow thing — that this code
cannot place an order.

---

## Research findings

The full working is in [`docs/RESEARCH_SUMMARY.md`](docs/RESEARCH_SUMMARY.md)
and the per-phase reports. The headline result is negative and is reported as
such.

**The venue constraint.** Measured over 258,657 one-minute Binance.US bars:
55% of BTC/USD bars contain no trade at all, mean volume is about $753 a minute,
and the 1-minute ATR is 2.2 bps. Coinbase traded 312x and Kraken 126x the volume
on the same day — but at 80-120 bps taker against roughly 1 bps on the Tier-0
schedule. Cheap fees and absent liquidity turned out to be the same phenomenon.

**The out-of-sample search.** 1,400 candidates across 10 families, 4 assets, 7
regimes and 9 horizons, over 120 days plus a complete 863k-trade tape, with
walk-forward enforced:

| Pooled out-of-sample | |
|---|---|
| Trades | **590,306** |
| Net | **−$92,365** |
| Profit factor | **0.22** |

Nothing was promoted. The single "marginal" arm was PF 1.00 over 30 trades,
which is noise. The horizon curve rises monotonically (PF 0.07 at 2 minutes to
0.35 at 60 minutes), so longer really is better — and still far from 1.20.

**The correction that mattered most.** Three of the bar-based strategies lose
money **at the mid price, before any cost is deducted** (366 trades, raw profit
factor 0.791). Costs multiply the loss but are not its cause. The earlier
conclusion that "maker execution will fix this" was wrong and has been retracted
in the report: you cannot subtract cost from a strategy that already loses at
the mid.

**What separating alpha from beta showed.** All 139 live paper trades were long,
during a rally in which ETHUSD moved +484 bps. Benchmarked against simply
holding the same position over the same interval, `momentum_scalp` turned out to
be **beta** (the market supplied 5.48 of its 6.33 bps). `flow_impulse` did not:
+11.93 bps of alpha, 95% CI [+5.43, +18.42]. It is the only net-positive line in
the project — and with 48 trades over one calendar day, it was **not promoted**.

**On maker execution.** Post-only quoting at the touch fills 0% within half a
second at this time scale; patience buys fills and costs more than it buys. A
separate, venue-level study found a real maker opportunity in BTCUSD at the
15-30 second horizon (33% fill, flat markout) — and no strategy in this project
has edge at that horizon. That is a gap named honestly, not a door closed. A
naive maker backtest would have overstated fills by 33%, which is why the study
models the queue rather than the price.

**Conclusion as it stands:** on this venue, taker-based micro-profit scalping
does not retain an edge out of sample under realistic execution assumptions. The
framework is what produced that answer, and would produce the opposite answer if
the data supported one.

---

## Quick start

```bash
git clone <your-fork-url> edge-after-costs
cd edge-after-costs
cp .env.example .env

# Set at minimum POSTGRES_PASSWORD, SECRET_KEY and BOOTSTRAP_ADMIN_PASSWORD.
# openssl rand -hex 32   generates a suitable value for the secrets.

docker compose up --build
```

Then open <http://localhost:3000/edge-after-costs> and sign in with
`BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD`.

No exchange account and no API key are needed: the system reads public market
data only.

For local development without Docker:

```bash
uv venv --python 3.12 .venv     # or: python3.12 -m venv .venv
.venv/bin/python -m pip install -r api/requirements-dev.txt
```

---

## Configuration

Everything is read from the environment; `.env.example` is the annotated
reference. The groups that matter:

| Group | Keys | Notes |
|---|---|---|
| Mode | `TRADING_MODE` | `PAPER` is the only accepted value |
| HTTP | `BASE_PATH`, `PUBLIC_BASE_URL`, `SECRET_KEY`, `COOKIE_SECURE` | changing `BASE_PATH` also requires editing `basePath`/`assetPrefix` in `web/next.config.mjs` |
| Storage | `DATABASE_URL`, `REDIS_URL`, `POSTGRES_*` | Postgres 16 and Redis 7 |
| Exchange | `EXCHANGE_REST_BASE`, `EXCHANGE_WS_BASE` | public endpoints only |
| Fees | `FEE_MODEL`, `FEE_STANDARD_*_BPS` | `standard` is pessimistic; model your own venue honestly |
| Capital and risk | `STARTING_EQUITY`, `RISK_PER_TRADE_PCT`, `MAX_*`, `DAILY_LOSS_LIMIT_PCT`, `LOSS_STREAK_*`, `MIN_NET_EDGE_*` | the risk engine's entire surface |
| Universe | `UNIVERSE_*` | calibrate to your venue; generic thresholds can empty the universe |
| Simulation | `SIM_LATENCY_MS`, `SIM_ADVERSE_BPS`, `SIM_LIMIT_MISS_RATIO`, `SIMULATOR_VERSION` | changing `SIMULATOR_VERSION` marks a boundary on the equity curve, because the two sides are not the same experiment |
| Cadence | `EVAL_INTERVAL_MS`, `POSITION_MONITOR_INTERVAL_MS`, retention | sized for a small two-core host |

---

## Running tests

```bash
# Backend — offline and deterministic, no network, no database
cd api && ../.venv/bin/python -m pytest tests -q

# Frontend
cd web && npm install && npm test && npm run typecheck
```

The backend suite includes the safety invariants
(`tests/test_paper_only.py`, `tests/test_safety.py`). If they fail, the failure
is not something to route around.

Research tests that need scikit-learn skip cleanly when it is absent; install
`api/requirements-dev.txt` to run all of them.

---

## Running research and backtests

These need network access for historical data and are best run off the
deployment host.

```bash
cd api

# Backtest across periods
../.venv/bin/python scripts/run_backtest.py \
  --symbols BTCUSD,ETHUSD,SOLUSD --periods 7d,30d,90d --out text

# Walk-forward — report the out-of-sample block, never the in-sample one
../.venv/bin/python scripts/run_backtest.py \
  --symbols BTCUSD,ETHUSD,SOLUSD --walkforward --train-days 21 --test-days 7

# Tournament (run symbol by symbol; loading them all at once exhausts memory,
# and use --max-params 2 so OFAT sweeps do not consume the budget before
# reaching the exit and stop axes)
../.venv/bin/python scripts/phase3_search.py --symbol BTCUSD --max-params 2

# Execution study over captured tape
../.venv/bin/python scripts/run_execution_study.py \
  --tape-dir /path/to/tape --symbols BTCUSD,ETHUSD,SOLUSD
```

`api/scripts/` holds 23 research CLIs; each one prints what it measured and
writes a JSON artifact. See Appendix A of
[`docs/RESEARCH_SUMMARY.md`](docs/RESEARCH_SUMMARY.md) for what each produces.

---

## Dashboard

A single-page research terminal served under `BASE_PATH`, session-authenticated.
It is designed around one rule: **an unmeasured value is never drawn as zero.**
Panels distinguish "we looked and found nothing" from "nobody looked", because
those license opposite conclusions.

Panels include scan activity and bench, the live opportunity board, open
positions and trade history with full per-trade cost decomposition
(`profit before costs − total cost = true net profit`), portfolio and
concentration, capital utilisation and a nine-reason idle-capital attribution, a
retrospective missed-opportunity audit, holding-time buckets per strategy, the
strategy tournament with in-sample columns visibly dimmed, model quality and
calibration, and system health.

---

## Project structure

```
api/
  app/
    config.py         every tunable, read from the environment
    types.py          the shared data contracts — read this first
    safety.py         the paper-only guard (runtime + static AST)
    exchange/         public REST + WebSocket + local order books
    universe.py       dynamic symbol selection and liquidity tiers
    features.py       incremental microstructure and indicator features
    regime.py         regime classification
    strategies/       six independent, separately benchmarked modules
    ranker.py         expected net edge and opportunity ranking
    risk.py           sizing and every circuit breaker
    portfolio.py      exposure, correlation, idle-capital attribution
    exits.py          TP / SL / trailing / time stop
    sim/              realistic paper execution and the capital ledger
    predict/          ML entry engine + NumPy-only inference artifact
    research/         tournament, families, robustness, regimes, replay
    backtest/         event-driven engine, walk-forward, reporting
    worker/main.py    the autonomous loop
    routes/           FastAPI surface
  scripts/            23 research CLIs
  tests/              65 files
web/                  Next.js research terminal
docker/               reverse-proxy example
docs/                 specs, phase reports, economics, runbook
```

---

## Limitations

Read these as constraints on what any result from this repository can mean.

- **Research software, not a trading product.** There is no live execution and
  no path to it in this codebase.
- **Simulation only.** Every P&L figure anywhere in this project — dashboard,
  reports, database — is simulated. The real order count is zero.
- **Not investment advice, and no guarantee of profitability.** The measured
  result so far is that the strategies tested do **not** retain an edge out of
  sample.
- **Single venue, single asset class.** Crypto spot on one US exchange. The
  universe thresholds are calibrated to that venue's measured liquidity and will
  behave badly if pointed elsewhere without recalibration.
- **Long only.** The short side is not implemented.
- **Small samples in places.** The one positive finding rests on 48 trades over
  one calendar day and is explicitly not promoted.
- **Sub-minute resolution is not honest here.** 89.9% of BTC 5-second buckets on
  this venue are empty; meaningful density starts at 3-5 minutes.
- **The execution simulator is a model.** It walks the book, applies latency and
  charges fees and slippage — it is still a model, and maker fills in particular
  are measured from tape rather than assumed.

---

## Roadmap

Based on what the measurements actually left open:

1. **Maker-first execution research on BTCUSD.** The only structure not ruled
   out. The venue study found 33% fill at 30 seconds with flat markout
   (−0.06 bps) on BTCUSD, against adverse selection on ETHUSD and SOLUSD. The
   tooling reports `INSUFFICIENT DATA` below 24 hours of tape; the gap is a
   strategy with edge at the 15-30 second horizon, which this project does not
   yet have.
2. **Fix the reward/risk realisation defect.** At a nominal 4:1, the average win
   was $0.22 against an average loss of $0.28: targets are not reached while
   stops are. Measuring with that unfixed is measuring with a broken instrument.
3. **Entry-model research.** The predictive classifier adds roughly 5 points of
   out-of-sample hit rate (purge and embargo enforced, chronological
   validation), reaching 67.5% against the 70.0% required. Those 2.5 points are
   a modelling problem on a measured quantity, not a venue problem.
4. **Additional venue adapters.** The ingestion layer is the only
   venue-specific part; the ranking, risk, simulation and research layers are
   not. A second venue would let the liquidity/fee trade-off be tested rather
   than argued.
5. **Flow and volume strategy families.** 130 candidates produced 0 signals over
   120 days — they could not be tested at all, which is a tooling gap, not a
   result.

---

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

One rule is non-negotiable: **nothing may add an order-execution path.** The
safety tests enforce it, and a pull request that trips them will not be merged.

---

## License

[Apache License 2.0](LICENSE).
