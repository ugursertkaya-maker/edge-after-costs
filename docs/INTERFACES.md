# Module interfaces — frozen contract

Six people are building this at once. **These signatures are frozen.** Build to
them exactly; if you think one is wrong, still implement it as written and flag
the problem in your report. All names below are importable from the stated
module. Everything takes/returns the structures in `api/app/types.py`.

`S = get_settings()` from `app.config`. Every class takes an optional
`settings: Settings | None = None` first keyword so tests can inject config.

---

## app/safety.py

```python
class LiveTradingDisabled(RuntimeError): ...

def assert_paper_mode(settings: Settings | None = None) -> None
    """Raise LiveTradingDisabled unless settings.mode == 'PAPER'."""

def source_tree_is_paper_only(root: Path) -> list[str]
    """Static scan of the api/app tree. Returns a list of human-readable
    violations: signed-request helpers, 'X-MBX-APIKEY', HMAC over a secret,
    '/api/v3/order', '/sapi/', 'api_secret'. Empty list == clean."""
```

## app/exchange/rest.py

```python
class BinanceUSRest:
    def __init__(self, *, base_url: str | None = None,
                 client: "httpx.AsyncClient | None" = None,
                 settings: Settings | None = None) -> None
    async def ping(self) -> bool
    async def server_time_ms(self) -> int
    async def exchange_info(self) -> dict[str, SymbolInfo]      # keyed by symbol
    async def ticker_24h(self) -> list[dict]                    # raw dicts
    async def depth(self, symbol: str, limit: int = 100) -> OrderBook
    async def klines(self, symbol: str, interval: str = "1m", *,
                     start_ms: int | None = None, end_ms: int | None = None,
                     limit: int = 1000) -> list[Bar]
    async def agg_trades(self, symbol: str, *, start_ms: int | None = None,
                         end_ms: int | None = None, limit: int = 1000) -> list[Trade]
    async def close(self) -> None
```

Retries with backoff on 429/418/5xx, respects `Retry-After`, caps concurrency.

## app/exchange/book.py

```python
class BookManager:
    def __init__(self, *, max_levels: int = 50, settings=None) -> None
    def apply_snapshot(self, symbol: str, last_update_id: int,
                       bids: list[tuple[float, float]],
                       asks: list[tuple[float, float]], ts_ms: int) -> None
    def apply_diff(self, symbol: str, first_id: int, final_id: int,
                   bids: list[tuple[float, float]],
                   asks: list[tuple[float, float]], ts_ms: int) -> bool
        """Binance diff-depth rules. Returns False and marks the book desynced
        on a sequence gap; buffers updates that arrive before the snapshot."""
    def apply_partial(self, symbol: str, last_update_id: int,
                      bids, asks, ts_ms: int) -> None      # depth20 snapshot stream
    def apply_book_ticker(self, symbol: str, bid: float, bid_qty: float,
                          ask: float, ask_qty: float, ts_ms: int) -> None
    def get(self, symbol: str) -> OrderBook | None
    def mark_desynced(self, symbol: str, why: str) -> None
    def needs_resync(self) -> list[str]
    def stats(self) -> dict[str, object]
```

## app/exchange/ws.py

```python
@dataclass
class DataHealth:
    connected: bool; streams: int; updates_per_sec: float
    reconnects: int; dropped: int; last_message_ms: int
    stale_symbols: list[str]; desynced_symbols: list[str]

class MarketDataFeed:
    def __init__(self, *, books: BookManager, settings=None) -> None
    def set_universe(self, hot: list[str], warm: list[str]) -> None
    async def run(self, stop: "asyncio.Event | None" = None) -> None
    def recent_trades(self, symbol: str, limit: int = 200) -> list[Trade]
    def bars(self, symbol: str, limit: int = 240) -> list[Bar]     # 1m, closed-first-to-last
    def ingest_message(self, msg: dict, ts_ms: int) -> None        # pure, unit-testable
    def health(self) -> DataHealth
```

`ingest_message` handles combined-stream envelopes `{"stream":..., "data":...}`
and the raw payloads for `bookTicker`, `aggTrade`, `kline`, `depthUpdate`,
`depth20`. It must never raise on malformed input — count it as `dropped`.

## app/universe.py

```python
@dataclass(frozen=True)
class UniverseEntry:
    symbol: str; info: SymbolInfo; quote_volume_24h: float; trades_24h: int
    spread_bps: float; volatility_bps: float; depth_usd: float
    score: float; tier: str                      # "hot" | "warm"

class UniverseSelector:
    def __init__(self, *, settings=None) -> None
    def select(self, tickers: list[dict], infos: dict[str, SymbolInfo],
               *, books: dict[str, OrderBook] | None = None) -> list[UniverseEntry]
        """Pure and deterministic. Applies every §6 filter, scores, sorts,
        truncates to max_symbols, tags the top hot_symbols as 'hot'."""
    async def refresh(self, rest: "BinanceUSRest") -> list[UniverseEntry]
```

## app/features.py

```python
class FeatureEngine:
    def __init__(self, *, settings=None) -> None
    def compute(self, snap: MarketSnapshot) -> Features
        """Incremental per symbol: O(1)-ish per call, not O(history)."""
    def reset(self, symbol: str | None = None) -> None
```

## app/regime.py

```python
class RegimeClassifier:
    def __init__(self, *, settings=None) -> None
    def classify(self, f: Features) -> RegimeState
```

## app/strategies/

```python
# base.py
class Strategy:
    name: str
    allowed_regimes: frozenset[Regime]
    def evaluate(self, f: Features, regime: RegimeState) -> Signal | None

# __init__.py
def get_strategies(names: Iterable[str] | None = None) -> list[Strategy]
STRATEGY_NAMES: tuple[str, ...]   # ("momentum_scalp","breakout_scalp",
                                  #  "trend_pullback","mean_reversion",
                                  #  "flow_impulse","book_imbalance")
```

Each strategy returns `None` when its regime gate fails or conviction is low.
A strategy **never** looks at positions, capital, fees or risk.

## app/ranker.py

```python
ExpectancyProvider = Callable[[str, str, str], tuple[float, int]]
    # (strategy, symbol, regime) -> (historical_expectancy_bps, sample_size)

class OpportunityRanker:
    def __init__(self, *, settings=None,
                 expectancy: ExpectancyProvider | None = None) -> None
    def build(self, signal: Signal, f: Features, regime: RegimeState,
              *, taker_bps: float, maker_bps: float,
              slippage_bps: float) -> Opportunity
    def rank(self, opps: list[Opportunity]) -> list[Opportunity]   # best first
    def qualifies(self, opp: Opportunity) -> bool
```

`net_edge_bps = gross_edge_bps - fee_bps - spread_cost_bps - slippage_bps`,
where `gross_edge_bps = signal.target_bps * P(hit)` and `P(hit)` is derived from
`signal.score` and regime confidence. Round-trip fees count both legs.

## app/risk.py

```python
@dataclass
class RiskState:
    equity: float
    open_positions: list[Position]
    realized_pnl_today: float
    realized_pnl_week: float
    loss_streak: int
    cooldown_until_ms: int
    kill_switch: bool
    data_healthy: bool
    ts_ms: int

class RiskEngine:
    def __init__(self, *, settings=None) -> None
    def evaluate(self, opp: Opportunity, state: RiskState,
                 *, info: SymbolInfo, book: OrderBook) -> RiskVerdict
    def blocked_reason(self, state: RiskState) -> RejectReason | None
```

Sizing: `risk_usd = equity * risk_per_trade_pct/100`;
`qty = risk_usd / (stop_distance_price + slippage_allowance)`, then clamped by
max position %, exposure %, concurrent-risk %, free cash, min notional, and the
symbol's lot/tick filters. `Side.SHORT` is rejected with `NOT_SUPPORTED_SHORT`.

## app/exits.py

```python
class ExitEngine:
    def __init__(self, *, settings=None) -> None
    def initial_levels(self, side: Side, entry_price: float, f: Features,
                       signal: Signal, *, round_trip_cost_bps: float,
                       info: SymbolInfo) -> tuple[float, float]     # (tp, sl)
    def check(self, pos: Position, book: OrderBook,
              f: Features | None, ts_ms: int,
              *, reversal: bool = False) -> ExitReason | None
    def update_trailing(self, pos: Position, price: float) -> None
```

TP must clear `round_trip_cost_bps + tp_min_bps_over_cost`. Reward:risk below
`min_reward_risk` must be surfaced so the risk engine can veto.

## app/sim/simulator.py

```python
class PaperSimulator:
    def __init__(self, *, settings=None, rng: "random.Random | None" = None) -> None
    def execute(self, req: OrderRequest, book: OrderBook, info: SymbolInfo, *,
                trades: list[Trade] | None = None,
                ts_ms: int | None = None,
                latency_ms: int | None = None) -> OrderResult
    def estimate_slippage_bps(self, side: Side, notional_usd: float,
                              book: OrderBook) -> float
```

Market orders walk the book from the far side, level by level, applying
`adverse_bps` and the configured latency; unfillable remainder is a partial
fill. Limit orders fill only if `trades` prove price traded through the limit,
and are skipped with probability `limit_miss_ratio`. Fees come from
`settings.fees`. `OrderResult.slippage_bps` is measured against the
decision-time mid. **Never fill at mid. Never assume full fill.**

## app/sim/ledger.py

```python
class Ledger:
    def __init__(self, name: str, starting_equity: float,
                 simulator_version: str) -> None
    name: str; starting_equity: float; cash: float
    open_positions: dict[str, Position]; closed_trades: list[ClosedTrade]
    def can_afford(self, notional_usd: float) -> bool
    def open(self, pos: Position, entry: OrderResult) -> None
    def close(self, pos: Position, exit_: OrderResult,
              reason: ExitReason, ts_ms: int) -> ClosedTrade
    def mark(self, prices: dict[str, float], ts_ms: int) -> None
    @property
    def equity(self) -> float
    @property
    def exposure_usd(self) -> float
    @property
    def unrealized_pnl(self) -> float
    @property
    def realized_pnl(self) -> float
    def realized_since(self, since_ms: int) -> float
    def loss_streak(self) -> int
    def stats(self, *, strategy: str | None = None,
              since_ms: int | None = None) -> PerfStats
    def equity_points(self) -> list[tuple[int, float]]
```

Capital recycling (§19) lives here: closing a position returns its notional plus
net P&L to `cash` immediately.

## app/backtest/

```python
# data.py
async def fetch_klines(rest, symbol, interval, start_ms, end_ms) -> list[Bar]
def load_cached(symbol: str, interval: str) -> list[Bar]
def save_cache(symbol: str, interval: str, bars: list[Bar]) -> Path

# engine.py
@dataclass
class BacktestConfig:
    starting_equity: float = 500.0
    fee_bps_per_side: float = 10.0
    spread_bps: float | None = None      # None => estimate from bar range
    slippage_bps: float = 2.0
    latency_bars: int = 1
    risk_per_trade_pct: float = 0.5
    max_concurrent: int = 3
    strategies: tuple[str, ...] | None = None
    seed: int = 7

@dataclass
class BacktestResult:
    label: str; start_ms: int; end_ms: int
    trades: list[ClosedTrade]; stats: PerfStats
    per_strategy: dict[str, PerfStats]
    equity_curve: list[tuple[int, float]]
    starting_equity: float; ending_equity: float

def run_backtest(bars_by_symbol: dict[str, list[Bar]], cfg: BacktestConfig,
                 *, label: str = "backtest") -> BacktestResult

# walkforward.py
@dataclass
class WalkForwardResult:
    windows: list[tuple[BacktestResult, BacktestResult]]   # (in-sample, out-of-sample)
    in_sample: PerfStats; out_of_sample: PerfStats
def run_walk_forward(bars_by_symbol, cfg, *, train_days: int, test_days: int) -> WalkForwardResult
```

The backtester drives the **real** `FeatureEngine`, `RegimeClassifier`,
strategies, `OpportunityRanker`, `RiskEngine` and `ExitEngine` — it must not
reimplement any of them. It synthesises a book from each bar (mid = close,
spread from `cfg.spread_bps` or the bar range) so `PaperSimulator` can be used
unchanged. Entries fill on the bar `latency_bars` after the signal.

## app/models/ (SQLAlchemy 2.0)

Tables (§26): `markets`, `market_stats`, `signals`, `signal_features`,
`strategy_decisions`, `paper_orders`, `paper_fills`, `positions`, `trades`,
`equity_snapshots`, `strategy_performance`, `risk_events`, `system_events`,
`data_quality_events`, `backtest_runs`, `users`.

```python
# app/db.py
Base; engine; SessionLocal; def get_session(); def init_db(); def healthcheck() -> bool
# app/models/__init__.py re-exports every model class
def apply_retention(session, *, now_ms: int, settings=None) -> dict[str, int]
```
