# NETEDGE — build brief (read this first)

## What we are building

An autonomous **PAPER-ONLY** crypto scalping bot. It continuously scans liquid
Binance.US spot pairs, ranks opportunities by **expected NET edge after fees,
spread and slippage**, opens small paper positions, takes small profits, cuts
losses fast, releases capital, and scans again.

Starting simulated capital **$500**. Comparison ledgers $1,000 and $5,000.

**V1 IS PAPER ONLY. There is no exchange API key, no signing code, no
authenticated endpoint, no order-submission path of any kind. Public market
data only.** Anything that could place a real order is a defect.

## Exchange (already decided — do not re-litigate)

**Binance.US**, public REST + WebSocket only.

- REST base `https://api.binance.us` (e.g. `/api/v3/exchangeInfo`,
  `/api/v3/klines`, `/api/v3/depth`, `/api/v3/ticker/24hr`, `/api/v3/aggTrades`)
- WS base `wss://stream.binance.us:9443` (combined streams at
  `/stream?streams=btcusd@bookTicker/btcusd@aggTrade/...`; symbols lowercase)
- Min notional on majors is **$1.00**; tick/step come from `exchangeInfo`.
- Fees: standard tier 0.10% maker / 0.10% taker; a published "Tier 0" pair list
  trades at 0% maker / 0.01% taker. The simulator defaults to the **per-pair
  Tier-0 table** (what we would really pay) and the scanner is restricted to
  those pairs, because at the standard tier an ATR-sized target is structurally
  unprofitable — see docs/ECONOMICS.md. Every result is also reported under
  `FEE_MODEL=standard` as a stress case.
- Measured from our VPS: REST ~480 ms, WS TCP RTT ~165 ms (stream.binance.us is
  routed to ap-northeast-1). Treat 250 ms as the default execution latency.

## Host constraints — these are hard

Production VPS has **2 vCPU / 8 GB shared with ~20 other containers**. The
worker container gets `cpus: 1.0`, `mem_limit: 768m`.

- Bounded concurrency, incremental feature updates, batched DB writes.
- Hot tier (5 symbols) keeps full diff-maintained order books; warm tier
  (the rest, ~7) uses bookTicker + periodic partial-depth snapshots.
- Never busy-spin. Never recompute a full indicator history per tick.

## Shared contracts

`api/app/types.py` — **the single source of truth for all data structures.**
Read it completely before writing anything. Do not change its public names or
field meanings; if you genuinely need a new field, add it with a default and
say so in your report.

`api/app/config.py` — all tunables. Access via `get_settings()`. Never read
`os.environ` directly outside `config.py`.

Conventions:
- prices/sizes are `float`, money is USD, `bps` = basis points (1 bps = 0.01%)
- **all timestamps are epoch milliseconds**, field/param names end in `_ms`
- `Side.LONG` is the only side V1 can trade (spot). `Side.SHORT` must be
  *structurally supported* everywhere but is gated off at the risk layer.

## Layout

```
api/app/
  config.py      types.py      safety.py     db.py
  models/        exchange/     strategies/   sim/     backtest/    routes/
  features.py    regime.py     ranker.py     risk.py  exits.py     universe.py
  worker/main.py
api/tests/
web/             (Next.js 15 dashboard, basePath=/cryptobot, Turkish UI)
```

## Non-negotiables

1. **No live orders.** No `requests`/`httpx` call that signs anything, no
   `X-MBX-APIKEY` header, no HMAC, no `/api/v3/order`.
2. **Never trade on stale data.** Every decision path checks book age, spread,
   depth, and desync flags first.
3. **Net expectancy only.** A gross-positive, net-negative setup is a losing
   setup and must be rejected.
4. **No martingale**, no averaging down, no doubling after losses.
5. Every trade has a defined stop **before** entry.
6. Deterministic tests: no network, no wall-clock dependence, no randomness
   without a seeded generator.

## Style

Match the tone of `types.py`: clear names, docstrings that explain *why*, no
noisy comments restating code. Type hints everywhere. Python 3.12,
`from __future__ import annotations`. Standard library + the pinned deps only.

Pinned deps available: `fastapi`, `uvicorn`, `sqlalchemy`, `psycopg[binary]`,
`alembic`, `redis`, `httpx`, `websockets`, `pydantic`, `passlib[bcrypt]`,
`python-jose`, `numpy`, `pytest`, `pytest-asyncio`, `freezegun`.
Prefer plain Python/`numpy` over pandas (not installed).

## Testing

`pytest` from `api/`. Write tests next to the feature in `api/tests/`.
Name them `test_<area>_<thing>.py`. Fixtures go in `api/tests/fixtures/`.
Tests must pass with **no network access**.
