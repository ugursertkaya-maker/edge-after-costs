# Exchange selection — Binance.US

Decided 2026-09-10. Every number below was measured from a **US-hosted**
server (a US IP, which is why all three candidates are reachable without any
bypass) or read from the exchange's own documentation. No VPN, proxy or
offshore venue was considered.

## The decision in one line

For a bot whose entire thesis is *many small profits*, **round-trip cost is the
only variable that matters**, and Binance.US is 6–60× cheaper than the
alternatives at our account size. Nothing else came close enough to matter.

## Side-by-side

| | **Binance.US** | Coinbase Advanced | Kraken Pro |
|---|---|---|---|
| US availability | Yes | Yes | Yes |
| Spot trading API | Yes | Yes | Yes |
| Public REST market data | Yes, unauthenticated | Yes | Yes |
| Public WebSocket | Yes, `wss://stream.binance.us:9443` | Yes | Yes, v2 |
| Order-book depth | Full diff stream + REST snapshot | L2/L3 | L2/L3 + microsecond stamps |
| Trade stream | `@aggTrade` | `matches` | `trade` |
| **Base-tier maker/taker** | **0.10% / 0.10%**, and a published **Tier 0 list at 0% / 0.01%** covering BTC, ETH, SOL, ADA, AVAX, DOT, SUI… | **0.40% / 0.60%** (0.60%/1.20% under $1k 30-day volume) | **0.40% / 0.80%** base |
| **Round-trip cost on a $100 position** | **$0.02 (Tier 0) – $0.20** | **$1.20** | **$0.80** |
| Minimum notional | **$1.00** on majors | ~$1 | pair-dependent, generally higher |
| Fractional / notional orders | Yes | Yes | Yes |
| Tradable pairs (TRADING status) | 265 (202 USDT, 54 USD) | — | — |
| REST rate limit | 6,000 weight/min; 61,000 raw req/5 min | tighter | tiered |
| Sandbox / testnet | **No** | Limited | Limited |
| Measured REST latency from the test host | 480 ms (`api.binance.us` → AWS us-east) | **95 ms** | 140 ms |
| Measured WS TCP RTT from the test host | **165 ms** (`stream.binance.us` resolved into AWS **ap-northeast-1/Tokyo**) | **2.3 ms** (Cloudflare) | **1.9 ms** (Cloudflare) |
| API key permission isolation | Read / Spot / Withdraw separable, IP whitelist | Yes | Yes |
| Withdrawal isolation | Yes — withdrawal is a separate toggle, left OFF | Yes | Yes |

## Why Binance.US wins despite being the slowest feed

Work the arithmetic for the actual objective. Target a **+$0.25 profit on a
$100 position** — that is a +0.25% move.

- Coinbase base tier: 0.60% taker × 2 legs = **$1.20 of cost**. The trade needs
  a **+1.45%** move to net $0.25. That is not a micro-profit strategy any more;
  it is a swing-trading strategy with a scalper's target. Structurally dead.
- Kraken base tier: 0.40% × 2 = **$0.80**. Needs +1.05%. Same problem.
- Binance.US standard tier: 0.10% × 2 = **$0.20**. Needs +0.45%. Viable.
- Binance.US Tier 0 (BTC/ETH/SOL/ADA/AVAX/DOT/SUI vs USD): **~$0.02**. Needs
  +0.27%. Comfortably viable.

The latency handicap is real but is charged against a different budget. Our
holding times are minutes, not microseconds; 165 ms of feed delay costs perhaps
a fraction of a basis point of adverse selection on a liquid major. The fee
difference costs **100–118 basis points per round trip**. Two orders of
magnitude apart — the trade-off is not close.

We keep the latency honest rather than ignoring it: the paper simulator's
default execution latency is **250 ms**, deliberately worse than the measured
165 ms RTT, and the backtest suite is also run at 500 ms and 1 s.

## What we do *not* rely on

- **No sandbox exists on Binance.US.** That is fine for V1 precisely because V1
  never authenticates. It does mean that a future live phase has no dry-run
  venue, which is an argument for a very small first live size — flagged for
  the decision that comes after this project.
- **The 0% / 0.01% Tier-0 rate is promotional** and Binance.US has changed it
  before. So the simulator **defaults to the pessimistic 0.10% / 0.10%
  standard tier** (`FEE_MODEL=standard`). Every headline result in the final
  report is produced under that assumption; the Tier-0 table exists behind
  `FEE_MODEL=tier0` for comparison only.

## Verification performed

```
curl https://api.binance.us/api/v3/ping                       -> 200
curl https://api.binance.us/api/v3/ticker/bookTicker?symbol=BTCUSDT
                                                              -> live bid/ask
curl https://api.binance.us/api/v3/exchangeInfo               -> 629 symbols, 265 TRADING
curl https://api.exchange.coinbase.com/products/BTC-USD/ticker -> 200
curl https://api.kraken.com/0/public/Ticker?pair=XBTUSD        -> 200
tcp connect stream.binance.us:9443                             -> 165 ms
```

`MIN_NOTIONAL` on BTCUSD / ETHUSD / SOLUSD / BTCUSDT is **$1.00**, tick size
0.01 — a $500 account can hold three $50–150 positions without ever brushing an
exchange minimum.
