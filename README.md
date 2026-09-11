# Kripto Micro-Profit Trader

Autonomous crypto scalping bot. Scans liquid Binance.US spot pairs, ranks
opportunities by expected **net** edge after fees, spread and slippage, opens
small paper positions, takes small profits, cuts losses fast, releases capital,
and scans again.

**PAPER ONLY.** There is no exchange API key, no request-signing code, no
authenticated endpoint, and no order-submission path anywhere in the tree. A
static guard (`app/safety.py`) scans the source at API startup and refuses to
boot if one appears; `tests/test_paper_only.py` asserts the same thing from four
directions.

- Dashboard: <https://your-domain.example/cryptobot> (Turkish)
- Server: `/srv/netedge` on your-server
- Containers: `cryptobot-{web,api,worker,db,redis}` — own network, own
  database, own volumes. Nothing shared with another project or the archived
  another project stack.

## Read these first

| Document | What it settles |
|---|---|
| [`docs/EXCHANGE_DECISION.md`](docs/EXCHANGE_DECISION.md) | Why Binance.US, with the measured Coinbase/Kraken comparison |
| [`docs/ECONOMICS.md`](docs/ECONOMICS.md) | **The finding that constrains the whole system**: the edge lives in the Tier-0 fee schedule |
| [`docs/INTERFACES.md`](docs/INTERFACES.md) | Module contracts |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | Deploy, operate, roll back, kill |

## The loop

```
live market data → scanner → rank → strategies → signal → expected net edge
→ risk engine → position sizing → paper execution → position monitor
→ take profit / stop / trailing exit → capital released → scan again
```

Three ledgers ($500 / $1,000 / $5,000) see identical decisions and differ only
in sizing, so the comparison is apples to apples. The $500 ledger is the one the
system is tuned for and the dashboard default.

## Layout

```
api/app/
  config.py       every tunable, read from the environment at construction
  types.py        the shared data contracts — read this before anything else
  safety.py       the paper-only guard
  exchange/       Binance.US public REST + WebSocket + local order books
  universe.py     dynamic symbol selection (§6)
  features.py     incremental microstructure + indicator features
  regime.py       TRENDING / RANGING / BREAKOUT / HIGH_VOL / LOW_VOL / ABNORMAL
  strategies/     six independent, separately benchmarked modules
  ranker.py       expected net edge and opportunity ranking
  risk.py         sizing and every circuit breaker
  exits.py        TP / SL / trailing / time stop
  sim/            realistic paper execution + the capital ledger
  backtest/       historical, walk-forward and reporting
  worker/main.py  the autonomous loop
  routes/         FastAPI surface
web/              Next.js dashboard, basePath=/cryptobot, Turkish
```

## Development

```bash
uv venv --python 3.12 .venv
.venv/bin/python -m pip install -r api/requirements-dev.txt
cd api && ../.venv/bin/python -m pytest -q          # offline, deterministic
```

Backtests (needs network, run it off the 2-vCPU box):

```bash
cd api && ../.venv/bin/python scripts/run_backtest.py \
  --symbols BTCUSD,ETHUSD,SOLUSD --periods 7d,30d,90d --out text
cd api && ../.venv/bin/python scripts/run_backtest.py \
  --symbols BTCUSD,ETHUSD,SOLUSD --walkforward --train-days 21 --test-days 7
```

## Deploy

```bash
rsync -az --exclude .venv --exclude __pycache__ --exclude node_modules \
      --exclude .next --exclude .env ./ user@your-server:/srv/netedge/
ssh user@your-server /srv/netedge/scripts/deploy.sh
```

`.env` lives only on the server, `chmod 600`, and is excluded from every rsync.
