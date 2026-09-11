# Phase report — Crypto Micro-Profit another project

Date: 2026-09-11 · Mode: **PAPER** · Live orders placed: **0**

---

## 1. Production safety

| Item | Result |
|---|---|
| another project worker stopped | **YES** — `other-project-worker` exited 0, `restart=no` |
| Auto-restart disabled | **YES** — parked behind a compose profile in `docker-compose.override.yml`; `docker compose up -d` no longer starts it |
| another project source retained | **YES** — `/srv/other-project` untouched |
| another project data retained | **YES** — 21 tables, **6,596 MB**, 2,445,224 orderbook snapshots, 90,124 sim trades, volumes `other-project-pgdata` / `other-project-redisdata` intact |
| another project logs retained | **YES** |
| `/other-project` still reachable | **YES**, with an "ARŞİV / DURDURULDU" banner injected by nginx `sub_filter` — no rebuild of the archived app was needed, and removing four lines reverts it |
| Rollback possible | **YES** — `docker compose --profile archived up -d worker` |
| CPU freed | `other-project-worker` **64.6% → 0**, `other-project-db` **17.6% → ~1%** |
| **another project regression** | **PASS** — all 5 containers healthy, `/api/health` `{"status":"ok","api":true,"database":true,"redis":true}`, `/` 307, `/login` 200. Not modified in any way. |

---

## 2. The system

- **URL:** <https://your-domain.example/cryptobot> (Turkish dashboard, login-gated)
- **Server:** `/srv/netedge`, containers `cryptobot-{web,api,worker,db,redis}`
- Own network, own Postgres, own Redis, own volumes. Nothing shared with
  another project or the archived another project stack.
- **10,332 lines** of application Python across 52 modules, **24 test files**,
  **599 automated tests passing** (1 skipped), all offline and deterministic.

### Exchange selected: **Binance.US**

Chosen on cost, which is the only variable that matters for a micro-profit
strategy. Full comparison in `docs/EXCHANGE_DECISION.md`; measured from the VPS
(which is physically in US East — a US IP, so no bypass of any kind was
needed or used).

| | **Binance.US** | Coinbase Advanced | Kraken Pro |
|---|---|---|---|
| Public REST | ✅ verified `200` | ✅ verified `200` | ✅ verified `200` |
| Public WebSocket | ✅ verified, 12 msg/s live | ✅ | ✅ |
| Base maker/taker | **0.10% / 0.10%**, Tier-0 list at **0% / 0.01%** | 0.40% / 0.60% | 0.40% / 0.80% |
| Round trip on $100 | **$0.02 – $0.20** | $1.20 | $0.80 |
| Min notional | **$1.00** | ~$1 | pair-dependent |
| Sandbox | none | limited | limited |
| REST latency from VPS | 480 ms | 95 ms | 140 ms |
| WS TCP RTT from VPS | 165 ms (Tokyo-routed) | 2.3 ms | 1.9 ms |

**Why it won:** targeting +$0.25 on a $100 position (a +0.25% move), Coinbase
needs +1.45% to break even and Kraken +1.05%. Both are structurally impossible
for this strategy. Binance.US needs +0.27%. The 165 ms latency handicap costs a
fraction of a basis point at our holding times; the fee difference is 100+ basis
points per round trip. Not close.

### Verification performed

```
GET  https://api.binance.us/api/v3/ping                → 200
GET  .../api/v3/exchangeInfo                           → 629 symbols, 265 TRADING
GET  .../api/v3/ticker/bookTicker?symbol=BTCUSDT       → live bid/ask
GET  .../api/v3/depth, /klines, /ticker/24hr           → all 200
WSS  wss://stream.binance.us:9443/stream?streams=...   → connected, 12 updates/s,
                                                          0 reconnects, 0 dropped
```

### Coins monitored

Universe is dynamic. It currently resolves to **BTC/USD, ETH/USD, SOL/USD** —
the Tier-0 pairs that clear the liquidity filters. ADA/USD, SUI/USD, AVAX/USD,
DOT/USD and the rest of the Tier-0 list are rejected on 24h volume.

### Strategies implemented (6, separately benchmarked)

`momentum_scalp`, `breakout_scalp`, `trend_pullback`, `mean_reversion`,
`flow_impulse`, `book_imbalance` — each regime-gated, each with ATR-derived
targets and stops, none aware of fees, capital or positions.

---

## 3. THE FINDING

**Binance.US has the fees a micro-profit bot needs and none of the liquidity.
Coinbase and Kraken have the liquidity and fees 80–120× too high. There is no
US-regulated spot venue where this strategy works as a taker.**

Measured over 258,657 one-minute bars per pair (≈180 days), pulled from
Binance.US itself:

| Pair | Bars with **zero trades** | Avg $ volume / minute | Avg trades / minute | 1m ATR |
|---|---|---|---|---|
| BTC/USD | **55.5%** | $753 | 2.19 | **2.2 bps** |
| ETH/USD | **69.2%** | $595 | 1.21 | **2.0 bps** |
| SOL/USD | **75.0%** | $200 | 0.72 | **3.7 bps** |
| ADA/USD | **87.3%** | $88 | 0.26 | — |

Against the same asset on the same day:

| Venue | BTC/USD 24h volume | Trades / 24h | Round-trip cost |
|---|---|---|---|
| **Binance.US** | **$1.6M** | ~3,000 | **~1 bps** |
| Kraken | $198M (**126×**) | 109,419 (**36×**) | ~80 bps |
| Coinbase | $490M (**312×**) | — | ~120 bps |

The cheap fee and the missing flow are **the same fact**: Binance.US zeroed its
fees because its volume collapsed to ~0.2% US market share. You cannot have one
without the other.

Full analysis: `docs/ECONOMICS.md`.

---

## 4. Backtest results — all negative

Real Binance.US 1-minute history. Fees **1 bps/side (Tier-0)**, spread modelled
from the bar range with a 2 bps floor, slippage from a synthesised book, entries
filled one bar **after** the signal, and when a bar's range contains both the
stop and the target the **stop is always assumed to hit first**.

| Period | Bars | Trades | Win rate | Fees | Slippage | **NET P&L** | End equity |
|---|---|---|---|---|---|---|---|
| 7d | 40,248 | 8 | 12.5% | $0.28 | $0.86 | **−$4.56** | $495.44 (−0.91%) |
| 30d | 170,580 | 185 | 36.8% | $6.18 | $19.22 | **−$39.94** | $460.06 (−7.99%) |
| 90d | 516,176 | 424 | 40.8% | $13.58 | $42.60 | **−$80.32** | $419.68 (−16.06%) |

90-day per-strategy leaderboard:

| Strategy | Trades | Win % | Fees | Slippage | **Net** | PF | Verdict |
|---|---|---|---|---|---|---|---|
| trend_pullback | 22 | 27.3% | $0.71 | $2.14 | **−$4.47** | 0.16 | LOSS (best) |
| breakout_scalp | 164 | 42.1% | $5.27 | $16.39 | **−$30.40** | 0.21 | LOSS |
| momentum_scalp | 238 | 41.2% | $7.60 | $24.07 | **−$45.46** | 0.22 | LOSS (worst) |
| mean_reversion / flow_impulse / book_imbalance | 0 | — | — | — | — | — | never qualified |

**Slippage, not fees, is the dominant cost**: $42.60 of slippage against $13.58
of fees over 90 days. On a venue with $753 of volume per minute, crossing the
spread is expensive no matter how cheap the commission is.

### Walk-forward — the number that counts

21 days train / 7 days test, 9 non-overlapping windows over 90 days. The only
thing "optimised" is which strategies were net-profitable in-sample.

| | IN-SAMPLE | **OUT-OF-SAMPLE** |
|---|---|---|
| Trades | 883 | **6** |
| Win rate | 42.0% | 16.7% |
| Expectancy / trade | −$0.19 | **−$0.26** |
| Profit factor | 0.23 | **0.06** |
| **NET P&L** | −$168.58 | **−$1.58** |
| Verdict | LOSS | **LOSS** |

In **6 of 9 windows no strategy was net-profitable in-sample, so out-of-sample
traded nothing at all** — which is the system behaving correctly, not failing.

### Fee stress — standard tier

Re-running 30d at the standard 0.10%/0.10% tier: **0 trades**, 4,064 of 4,066
signals disqualified by the ranker on net edge. Exactly as `docs/ECONOMICS.md`
predicts — off the Tier-0 list the strategy is not unprofitable, it is
arithmetically impossible.

---

## 5. Live paper run

Started 2026-09-11 03:0x UTC. Starting capital **$500** (comparison ledgers
$1,000 / $5,000 run the same decisions at different size).

The full loop has now run end to end on live data. First completed round trip:

| | |
|---|---|
| BTC/USD · `book_imbalance` · TRAILING_PROFIT · held 1m 29s | |
| Entry | 76,776.84 |
| Exit | 76,802.55 |
| Gross P&L | **+$0.0584** |
| Fees | −$0.0349 |
| Slippage | −$0.0449 |
| **NET P&L** | **+$0.0235** |

That is precisely the micro-profit shape the brief describes — two and a half
cents, banked only because it survived every cost line. Base ledger equity
$500.02, 1 trade, 0 open.

### The same trade, three account sizes — the capacity limit, measured

All three ledgers took the identical decision at the same instant. Only the
size differed:

| Ledger | Net P&L on that trade |
|---|---|
| $500 | **+$0.0235** |
| $1,000 | **+$0.0233** |
| $5,000 | **−$0.0431** |

**The same trade is profitable at $500 and loss-making at $5,000**, because the
larger order walks deeper into a book that has $753 of volume a minute behind
it. This is the liquidity finding restated as a capacity ceiling: whatever edge
exists here does not scale past roughly four figures, and the extra capital is
paid straight back out as slippage.

Live rejection ledger over 181 evaluation passes (3 entries, 3 exits):

| Reason | Count |
|---|---|
| `NEGATIVE_EDGE` | 137 |
| `EDGE_BELOW_MIN` | 36 |
| `DUPLICATE_SYMBOL` | 21 |

A representative live decision, fully costed:

```
SOLUSD  book_imbalance   gross 2.49 bps
                       − fee   1.00
                       − spread 0.50
                       − slip   1.45
                       = net   −0.47 bps   → rejected
```

That is the whole thesis in five lines: the gross move and the cost of
capturing it are the same size.

One profitable trade is not evidence of anything — it is a single sample, and
the backtests over 424 trades say the distribution is negative. What it does
prove is that the machinery works: scan, rank, cost, size, fill, monitor,
trail, exit, release capital, record. **A bot that opens no positions on a
market with no edge is also working correctly.** The 7-day observation window
is running and will accumulate evidence either way.

---

## 6. Operational numbers

| | |
|---|---|
| Worker CPU | **2.0%** of one core |
| Worker RAM | **61 MB** / 640 MB limit |
| Whole stack CPU | ~3.3% (worker 2.0, api 0.5, redis 0.7, db 0.1, web 0.0) |
| Whole stack RAM | ~220 MB |
| Host load average | 1.08 on 2 vCPU — **lower than before this project**, because stopping another project freed more than we consumed |
| DB growth | 9.9 MB in the first ~40 min; `market_stats` ages out at 48 h, events at 30 days; trades/positions/equity/backtests never deleted |
| WS health | connected, 12 updates/s, 0 reconnects, 0 dropped, book age 150–700 ms |
| Automated tests | **599 passing**, 1 skipped, fully offline |

---

## 7. Safety posture

Live-verified at `GET /cryptobot/api/system/safety`:

```json
{ "mode": "PAPER", "live_orders_enabled": false,
  "exchange_credentials_configured": false, "withdrawal_permission": false,
  "source_scan_violations": [], "clean": true }
```

| Item | Status |
|---|---|
| PAPER mode | **YES** — the only accepted value; API and worker refuse to start otherwise |
| Live orders placed | **0** |
| Exchange trade credentials configured | **NO** |
| Withdrawal-enabled credential | **NO** — none exists |
| Signing code in the tree | **none** — a static guard scans every `.py` at API startup for `X-MBX-APIKEY`, HMAC in a trading path, `/api/v3/order`, `/sapi/`, `api_secret`, `signature=`, and refuses to boot on a hit |
| Proof under test | `tests/test_paper_only.py` — 7 tests attacking the claim from four directions, including one that plants a violation to prove the guard catches it |

---

## 8. What the user needs to do next

**Nothing is required.** Do not create a Binance.US account yet.

The KYC/API-key step exists to enable live trading, and **the evidence says do
not trade this live**: out-of-sample net expectancy is −$0.26 per trade and
every strategy is net negative over every period tested. Creating an account now
would spend effort on a path the data has closed.

If and when a version does clear validation, the flow is: create the account →
Basic Verification → enable 2FA → create an Exchange API key with **Reading
only** (not Spot Trading) → restrict it to IP `your-server` → leave
**withdrawals OFF** → run `sudo /srv/netedge/scripts/configure-exchange.sh`
on the server, which stores the secret `chmod 600`, root-owned, outside `.env`
and outside git. **Never paste an API secret into a chat window.** That script
does not enable live trading; nothing in this repository does.

### Recommendation

Three options, in the order I would take them:

1. **Change the structure, not the parameters.** Binance.US charges **0%
   maker**. A passive, post-only strategy pays no fee *and* stops paying the
   slippage that caused 57% of the losses. Tight quotes with thin flow is the
   textbook case for earning the spread rather than crossing it. The simulator
   already models resting limit orders, queue misses and maker fees, so the
   machinery to test this exists. This is the one structure the data has not
   ruled out.
2. **Change the horizon.** One-minute moves on Binance.US majors (2 bps) are the
   same size as the cost of trading them. A 15–60 minute horizon gives a move
   several multiples of the spread. That is no longer a micro-profit scalper —
   it is a different bet, and it would need its own validation.
3. **Stop.** The honest reading of the walk-forward is that this specific idea
   does not have an edge on the venues legally available to a US trader. That is
   a real result and it cost no capital to find out, which is exactly what the
   paper phase was for.

What I would **not** do is tune the parameters until a backtest turns green.
With 9 walk-forward windows and 6 out-of-sample trades, anything that looks
profitable after tuning is curve fitting, and §24 of the brief exists precisely
to prevent it.
