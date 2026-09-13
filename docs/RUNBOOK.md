# Runbook

Operating notes for a NetEdge deployment. Nothing here is specific to a
particular host: substitute your own server, path and domain.

Conventions used below:

| Placeholder | Meaning |
|---|---|
| `your-server` | the host running the stack |
| `/srv/netedge` | wherever you cloned the repository on that host |
| `https://your-domain.example` | wherever the dashboard is published |
| `${BASE_PATH}` | the path prefix in `.env` (default `/netedge`) |

If the account you deploy with is not in the `docker` group, prefix the Docker
commands below with `sudo`.

## Daily checks

```bash
curl -s https://your-domain.example/netedge/api/health | python3 -m json.tool
docker compose ps
docker stats --no-stream | grep netedge
```

`worker_alive: false`, or a `worker_lag_ms` above ~30 s, means the loop has
stalled even though the container is up. Read the logs before restarting.

```bash
docker compose logs worker --tail 100 | grep -v httpx
```

## Deploy

From a checkout on the server:

```bash
git pull
./scripts/deploy.sh
```

If you deploy by copying files from a workstation instead, **exclude `.env`**:

```bash
rsync -az --delete \
      --exclude .venv --exclude __pycache__ --exclude '*.pyc' \
      --exclude node_modules --exclude .next --exclude .env --exclude .git \
      ./ user@your-server:/srv/netedge/
ssh user@your-server /srv/netedge/scripts/deploy.sh
```

`--exclude .env` is not optional: the server's `.env` holds the only copy of the
database password and the admin bootstrap password. An rsync without it wipes
them.

Partial redeploy after a strategy-only change:

```bash
docker compose build api && docker compose up -d --no-deps api worker
```

## Stop trading immediately

Three levers, in increasing order of severity:

1. **Kill switch** (no restart; open positions are still managed to their exits):
   ```bash
   docker compose exec redis redis-cli set netedge:kill_switch 1
   ```
   Clear it with `set netedge:kill_switch 0`.
2. **Stop the worker** (no new decisions at all; open paper positions freeze):
   ```bash
   docker compose stop worker
   ```
3. **Stop the stack**: `docker compose down`. Never `docker rm -f`, which can
   take volumes with it on some compose versions.

There is no fourth lever for "stop live trading", because there is no live
trading: `TRADING_MODE=PAPER` and the API refuses to boot on anything else.

## Where the data lives

| | |
|---|---|
| Postgres | volume `netedge-pgdata`, container `netedge-db`, db `netedge` |
| Redis | ephemeral by design (`--save ""`), holds only the live dashboard snapshot |
| Backtest cache | volume `netedge-cache` → `/data/backtest-cache` |
| Market tape | volume `netedge-tape` → `/data/tape` |
| Secrets | `.env` next to `docker-compose.yml`, `chmod 600` |

Trades, positions, equity snapshots and backtest runs are **never** deleted by
retention. Market stats age out after 48 h, events after 30 days.

```bash
docker compose exec db psql -U netedge -d netedge \
  -c "select count(*) from trades; select count(*) from equity_snapshots;"
```

## Reverse proxy

`docker/nginx.example.conf` is a template for publishing the dashboard under
`${BASE_PATH}` behind TLS. Two rules matter:

* the `${BASE_PATH}/api/` location must be matched **before** `${BASE_PATH}` —
  the longer prefix has to win;
* if you change `BASE_PATH`, change `basePath` and `assetPrefix` in
  `web/next.config.mjs` to match, and rebuild the web image. Next.js bakes both
  into the build.

Reload rather than restart after a config change:

```bash
docker exec nginx nginx -t && docker exec nginx nginx -s reload
```

## Backtests

Run these off the production host if it is small — the worker needs its cores.

```bash
cd api
BACKTEST_CACHE_DIR=/tmp/bt-cache ../.venv/bin/python scripts/run_backtest.py \
  --symbols BTCUSD,ETHUSD,SOLUSD,ADAUSD --periods 7d,30d,90d,180d,365d \
  --fee-bps 1 --out text                       # Tier-0 reality

# ...and the stress case, which is the number that matters most:
  --fee-bps 10                                 # standard tier

# walk-forward — report the out-of-sample block, never the in-sample one
BACKTEST_CACHE_DIR=/tmp/bt-cache ../.venv/bin/python scripts/run_backtest.py \
  --symbols BTCUSD,ETHUSD,SOLUSD,ADAUSD --walkforward --train-days 21 --test-days 7
```

## Known operational facts

- **Binance.US volume is thin.** BTC/USD turns over roughly $1.6M a day with
  about 3k prints. The universe filters are calibrated to that, not to a
  generically liquid exchange — raising them back to "sensible" values empties
  the universe and the worker logs `universe refresh returned nothing`.
- **`stream.binance.us` may be routed a long way from you.** A round trip of
  150 ms or more from a US East host is expected, not a fault.
- **The whole measured edge was the Tier-0 fee schedule.** See
  `docs/ECONOMICS.md`. If that schedule is withdrawn, stop the system rather
  than widening targets.
- **Sizing.** The default cadence and container limits assume a small two-core
  machine. Measure before lowering `EVAL_INTERVAL_MS`.
