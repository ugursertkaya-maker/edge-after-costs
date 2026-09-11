# Runbook

Host: `your-server` · `ssh user@your-server` · project at `/srv/netedge`
Dashboard: <https://your-domain.example/cryptobot>

Everything below uses `sudo -n docker` because the `deploy` user is not in the
`docker` group on this host.

## Daily checks

```bash
curl -s https://your-domain.example/cryptobot/api/health | python3 -m json.tool
ssh user@your-server 'cd /srv/netedge && sudo -n docker compose ps'
ssh user@your-server 'sudo -n docker stats --no-stream | grep cryptobot'
```

`worker_alive: false` or a `worker_lag_ms` above ~30 s means the loop has
stalled even though the container is up. Check the logs before restarting.

```bash
ssh user@your-server 'sudo -n docker logs cryptobot-worker --tail 100 | grep -v httpx'
```

## Deploy

```bash
# from the laptop
cd /home/user/crypto-trader
rsync -az --delete --exclude .venv --exclude __pycache__ --exclude '*.pyc' \
      --exclude node_modules --exclude .next --exclude .env --exclude .git \
      ./ user@your-server:/srv/netedge/
ssh user@your-server /srv/netedge/scripts/deploy.sh
```

`--exclude .env` is not optional: the server `.env` holds the only copy of the
database password and the admin bootstrap password. An rsync without it wipes
them.

Partial redeploy after a strategy-only change:

```bash
ssh user@your-server 'cd /srv/netedge && sudo -n docker compose build api \
  && sudo -n docker compose up -d --no-deps api worker'
```

## Stop trading immediately

Three levers, in increasing order of severity:

1. **Kill switch** (no restart, positions still managed to their exits):
   ```bash
   ssh user@your-server 'sudo -n docker exec cryptobot-redis redis-cli set cryptobot:kill_switch 1'
   ```
   Clear it with `set cryptobot:kill_switch 0`.
2. **Stop the worker** (no new decisions at all; open paper positions freeze):
   ```bash
   ssh user@your-server 'cd /srv/netedge && sudo -n docker compose stop worker'
   ```
3. **Stop the stack**: `docker compose down` — never `docker rm -f`, which would
   take volumes with it on this host's compose version.

There is no fourth lever for "stop live trading", because there is no live
trading: `TRADING_MODE=PAPER` and the API refuses to boot on anything else.

## Where the data lives

| | |
|---|---|
| Postgres | volume `cryptobot-pgdata`, container `cryptobot-db`, db `cryptobot` |
| Redis | ephemeral by design (`--save ""`), holds only the live dashboard snapshot |
| Backtest cache | volume `cryptobot-cache` → `/data/backtest-cache` |
| Secrets | `/srv/netedge/.env`, `chmod 600` |

Trades, positions, equity snapshots and backtest runs are **never** deleted by
retention. Market stats age out after 48 h, events after 30 days.

```bash
ssh user@your-server 'sudo -n docker exec cryptobot-db psql -U cryptobot -d cryptobot \
  -c "select count(*) from trades; select count(*) from equity_snapshots;"'
```

## Nginx

The `/cryptobot` locations live in the shared vhost
`/etc/nginx/conf.d/site.conf`, added by `scripts/patch-nginx.py`,
which leaves a `.bak` beside the file. Revert by restoring it.

```bash
ssh user@your-server 'sudo -n docker exec nginx nginx -t && sudo -n docker exec nginx nginx -s reload'
```

Nginx runs from a read-only bind mount of `conf.d`, so a config change needs a
reload, not a restart — and never a `--force-recreate` of the shared nginx while
other sites are live.

## The archived another project stack

`cryptobot` replaced it as the active research project. The worker is stopped
and parked behind a compose profile in
`/srv/other-project/docker-compose.override.yml`; `docker compose up -d`
will not bring it back. Source, database, volumes and logs are all retained.

To resume it:

```bash
ssh user@your-server 'cd /srv/other-project \
  && sudo -n docker compose --profile archived up -d worker \
  && sudo -n docker update --restart=unless-stopped other-project-worker'
```

## Backtests

Run these off the VPS — the host only has 2 vCPU and the worker needs them.

```bash
cd /home/user/crypto-trader/api
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

- **Binance.US volume is thin.** BTC/USD turns over ~$1.6M a day with ~3k
  prints. The universe filters are calibrated to that, not to a generic liquid
  exchange — raising them back to "sensible" values empties the universe and the
  worker logs `universe refresh returned nothing`.
- **`stream.binance.us` is routed to Tokyo** (~165 ms RTT from this host). That
  is expected, not a fault.
- **The whole edge is the Tier-0 fee schedule.** See `docs/ECONOMICS.md`. If
  Binance.US withdraws it, stop the bot rather than widening targets.
