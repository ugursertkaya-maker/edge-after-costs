"""Shared data contracts.

Every module in the bot talks to every other module through the structures in
this file.  Nothing here touches the database or the network, so it is cheap to
import from tests.

Conventions used throughout the codebase:

* All prices and sizes are ``float``; all money is USD.
* ``bps`` means basis points (1 bps = 0.01%).
* All timestamps are epoch **milliseconds**, UTC, named ``*_ms``.
* ``Side.LONG`` is the only side V1 can actually trade (spot).  ``Side.SHORT``
  exists so the risk/sim/strategy plumbing is already symmetric.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Any


def now_ms() -> int:
    return int(time.time() * 1000)


class Side(str, enum.Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class Decision(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"
    WATCH = "WATCH"
    NO_TRADE = "NO_TRADE"


class Regime(str, enum.Enum):
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    BREAKOUT = "BREAKOUT"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    ABNORMAL = "ABNORMAL"
    UNKNOWN = "UNKNOWN"


class ExitReason(str, enum.Enum):
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_PROFIT = "TRAILING_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    TIME_STOP = "TIME_STOP"
    SIGNAL_REVERSAL = "SIGNAL_REVERSAL"
    RISK_EXIT = "RISK_EXIT"


class OrderType(str, enum.Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class RejectReason(str, enum.Enum):
    STALE_BOOK = "STALE_BOOK"
    WIDE_SPREAD = "WIDE_SPREAD"
    THIN_DEPTH = "THIN_DEPTH"
    NEGATIVE_EDGE = "NEGATIVE_EDGE"
    EDGE_BELOW_MIN = "EDGE_BELOW_MIN"
    RISK_BUDGET = "RISK_BUDGET"
    MAX_POSITIONS = "MAX_POSITIONS"
    DUPLICATE_SYMBOL = "DUPLICATE_SYMBOL"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    WEEKLY_LOSS_LIMIT = "WEEKLY_LOSS_LIMIT"
    LOSS_STREAK_COOLDOWN = "LOSS_STREAK_COOLDOWN"
    ABNORMAL_VOLATILITY = "ABNORMAL_VOLATILITY"
    DATA_FAILURE = "DATA_FAILURE"
    KILL_SWITCH = "KILL_SWITCH"
    BELOW_MIN_NOTIONAL = "BELOW_MIN_NOTIONAL"
    BAD_REWARD_RISK = "BAD_REWARD_RISK"
    SLIPPAGE_TOO_HIGH = "SLIPPAGE_TOO_HIGH"
    NOT_SUPPORTED_SHORT = "NOT_SUPPORTED_SHORT"


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SymbolInfo:
    """Static exchange metadata for one tradable pair."""

    symbol: str
    base_asset: str
    quote_asset: str
    status: str
    tick_size: float
    step_size: float
    min_qty: float
    min_notional: float

    def round_price(self, price: float) -> float:
        if self.tick_size <= 0:
            return price
        return round(round(price / self.tick_size) * self.tick_size, 10)

    def floor_qty(self, qty: float) -> float:
        if self.step_size <= 0:
            return qty
        import math

        return round(math.floor(qty / self.step_size) * self.step_size, 10)


@dataclass
class BookLevel:
    price: float
    qty: float


@dataclass
class OrderBook:
    """A local order book for one symbol.

    ``bids`` are sorted descending, ``asks`` ascending.  ``last_update_ms`` is
    when we last applied an update, which is what staleness is measured from.
    """

    symbol: str
    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)
    last_update_id: int = 0
    last_update_ms: int = 0
    # Set when a gap/sequence error was detected and a resync is pending.
    desynced: bool = False

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 0.0

    @property
    def mid(self) -> float:
        if not self.bids or not self.asks:
            return 0.0
        return (self.bids[0].price + self.asks[0].price) / 2.0

    @property
    def microprice(self) -> float:
        """Size-weighted mid — leans toward the side with less resting size."""
        if not self.bids or not self.asks:
            return 0.0
        bq, aq = self.bids[0].qty, self.asks[0].qty
        if bq + aq <= 0:
            return self.mid
        return (self.bids[0].price * aq + self.asks[0].price * bq) / (bq + aq)

    @property
    def spread_bps(self) -> float:
        m = self.mid
        if m <= 0:
            return float("inf")
        return (self.asks[0].price - self.bids[0].price) / m * 10_000.0

    def age_ms(self, ref_ms: int | None = None) -> int:
        return (now_ms() if ref_ms is None else ref_ms) - self.last_update_ms

    def depth_usd(self, side: str, bps: float = 10.0) -> float:
        """Notional resting within ``bps`` of mid on ``side`` ('bid'/'ask')."""
        m = self.mid
        if m <= 0:
            return 0.0
        limit = m * bps / 10_000.0
        levels = self.bids if side == "bid" else self.asks
        total = 0.0
        for lv in levels:
            if abs(lv.price - m) > limit:
                break
            total += lv.price * lv.qty
        return total

    def imbalance(self, bps: float = 10.0) -> float:
        """(bid - ask) / (bid + ask) notional within ``bps``.  Range [-1, 1]."""
        b = self.depth_usd("bid", bps)
        a = self.depth_usd("ask", bps)
        if b + a <= 0:
            return 0.0
        return (b - a) / (b + a)

    def is_valid(self) -> bool:
        return (
            bool(self.bids)
            and bool(self.asks)
            and not self.desynced
            and self.asks[0].price > self.bids[0].price
        )


@dataclass(frozen=True)
class Trade:
    """One aggregated public trade print."""

    symbol: str
    price: float
    qty: float
    ts_ms: int
    buyer_is_maker: bool  # True => the aggressor was a SELLER

    @property
    def notional(self) -> float:
        return self.price * self.qty

    @property
    def aggressor(self) -> Side:
        return Side.SHORT if self.buyer_is_maker else Side.LONG


@dataclass(frozen=True)
class Bar:
    """OHLCV bar.  Used identically by the live feature engine and backtester."""

    symbol: str
    open_ms: int
    close_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float           # base asset
    quote_volume: float     # USD
    trades: int
    taker_buy_quote: float = 0.0

    @property
    def range_bps(self) -> float:
        if self.close <= 0:
            return 0.0
        return (self.high - self.low) / self.close * 10_000.0


@dataclass
class MarketSnapshot:
    """Everything a decision is allowed to see for one symbol, at one instant."""

    symbol: str
    ts_ms: int
    book: OrderBook
    bars_1m: list[Bar]
    recent_trades: list[Trade]
    info: SymbolInfo
    quote_volume_24h: float = 0.0
    trades_24h: int = 0

    @property
    def price(self) -> float:
        return self.book.mid or (self.bars_1m[-1].close if self.bars_1m else 0.0)


# --------------------------------------------------------------------------- #
# Features / regime / signals
# --------------------------------------------------------------------------- #


@dataclass
class Features:
    """Derived, strategy-agnostic view of a market.

    Every field is documented because the dashboard renders them and the
    strategy modules consume them by name.
    """

    symbol: str
    ts_ms: int
    price: float
    mid: float
    microprice: float
    spread_bps: float
    book_age_ms: int
    depth_bid_usd: float
    depth_ask_usd: float
    imbalance: float                 # [-1, 1]
    atr_bps: float                   # 14-bar ATR on 1m, in bps of price
    realized_vol_bps: float          # stdev of 1m returns, bps
    vol_percentile: float            # 0..1 vs trailing window
    ret_1m_bps: float
    ret_5m_bps: float
    ret_15m_bps: float
    ema_fast: float
    ema_slow: float
    ema_slope_bps: float             # per bar
    trend_strength: float            # 0..1
    range_pos: float                 # 0..1, where price sits in recent range
    bb_width_bps: float
    compression: float               # 0..1, 1 = maximally compressed
    volume_ratio: float              # last 1m quote vol / trailing mean
    trade_flow_imbalance: float      # [-1, 1] aggressor notional imbalance
    trade_flow_accel: float          # short flow vs long flow
    vwap: float
    vwap_dev_bps: float
    quote_volume_24h: float
    trades_24h: int
    bars_available: int
    extras: dict[str, float] = field(default_factory=dict)


@dataclass
class RegimeState:
    regime: Regime
    confidence: float                # 0..1
    reasons: list[str] = field(default_factory=list)


@dataclass
class Signal:
    """A strategy's opinion about one symbol at one instant.

    ``score`` is in [0, 1] and expresses conviction, *not* expected profit.
    ``target_bps``/``stop_bps`` are the strategy's own view of where the move
    goes and where it is wrong; the exit engine may tighten but never loosen
    them beyond configured bounds.
    """

    strategy: str
    symbol: str
    side: Side
    score: float
    target_bps: float
    stop_bps: float
    ts_ms: int
    horizon_seconds: int = 900
    reasons: list[str] = field(default_factory=list)
    features_used: dict[str, float] = field(default_factory=dict)

    @property
    def reward_risk(self) -> float:
        return self.target_bps / self.stop_bps if self.stop_bps > 0 else 0.0


@dataclass
class Opportunity:
    """A ranked, costed candidate — the thing the risk engine decides on."""

    signal: Signal
    features: Features
    regime: RegimeState
    gross_edge_bps: float
    fee_bps: float
    spread_cost_bps: float
    slippage_bps: float
    net_edge_bps: float
    rank_score: float
    expected_hold_seconds: int
    historical_expectancy_bps: float = 0.0
    sample_size: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def symbol(self) -> str:
        return self.signal.symbol


# --------------------------------------------------------------------------- #
# Execution / positions
# --------------------------------------------------------------------------- #


@dataclass
class OrderRequest:
    symbol: str
    side: Side
    order_type: OrderType
    qty: float
    limit_price: float | None = None
    client_id: str = ""
    reduce_only: bool = False
    ts_ms: int = 0


@dataclass
class Fill:
    price: float
    qty: float
    fee_usd: float
    is_maker: bool
    ts_ms: int


@dataclass
class OrderResult:
    """Outcome of a *simulated* order.  V1 never produces a live one."""

    accepted: bool
    order_type: OrderType
    requested_qty: float
    filled_qty: float
    avg_price: float
    fees_usd: float
    slippage_bps: float
    fills: list[Fill] = field(default_factory=list)
    reject_reason: str | None = None
    latency_ms: int = 0

    @property
    def fully_filled(self) -> bool:
        return self.requested_qty > 0 and self.filled_qty >= self.requested_qty * 0.999

    @property
    def partial(self) -> bool:
        return 0 < self.filled_qty < self.requested_qty * 0.999

    @property
    def notional(self) -> float:
        return self.avg_price * self.filled_qty


@dataclass
class Position:
    """An open paper position."""

    id: str
    symbol: str
    side: Side
    strategy: str
    qty: float
    entry_price: float
    entry_ms: int
    notional_usd: float
    risk_usd: float
    take_profit: float
    stop_loss: float
    entry_fees_usd: float
    entry_slippage_bps: float
    ledger: str = "base"
    regime: str = Regime.UNKNOWN.value
    trailing_armed: bool = False
    best_price: float = 0.0
    target_bps: float = 0.0
    stop_bps: float = 0.0
    expected_net_edge_bps: float = 0.0
    time_stop_ms: int = 0
    last_price: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def unrealized_usd(self, price: float) -> float:
        if self.side is Side.LONG:
            return (price - self.entry_price) * self.qty
        return (self.entry_price - price) * self.qty

    def unrealized_bps(self, price: float) -> float:
        if self.entry_price <= 0:
            return 0.0
        raw = (price - self.entry_price) / self.entry_price * 10_000.0
        return raw if self.side is Side.LONG else -raw

    def holding_seconds(self, ref_ms: int | None = None) -> float:
        return ((now_ms() if ref_ms is None else ref_ms) - self.entry_ms) / 1000.0


@dataclass
class ClosedTrade:
    """A completed round trip, with every cost itemised (§20, §30)."""

    id: str
    symbol: str
    side: Side
    strategy: str
    regime: str
    ledger: str
    qty: float
    entry_price: float
    exit_price: float
    entry_ms: int
    exit_ms: int
    notional_usd: float
    gross_pnl_usd: float
    entry_fee_usd: float
    exit_fee_usd: float
    slippage_usd: float
    net_pnl_usd: float
    exit_reason: ExitReason
    risk_usd: float
    expected_net_edge_bps: float
    simulator_version: str

    @property
    def fees_usd(self) -> float:
        return self.entry_fee_usd + self.exit_fee_usd

    @property
    def holding_seconds(self) -> float:
        return (self.exit_ms - self.entry_ms) / 1000.0

    @property
    def r_multiple(self) -> float:
        return self.net_pnl_usd / self.risk_usd if self.risk_usd > 0 else 0.0

    @property
    def is_win(self) -> bool:
        return self.net_pnl_usd > 0


@dataclass
class RiskVerdict:
    """The risk engine's answer about one opportunity."""

    approved: bool
    qty: float = 0.0
    notional_usd: float = 0.0
    risk_usd: float = 0.0
    take_profit: float = 0.0
    stop_loss: float = 0.0
    reject_reason: RejectReason | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class PerfStats:
    """Aggregate performance of a strategy / ledger / the whole book."""

    trades: int = 0
    wins: int = 0
    losses: int = 0
    gross_pnl: float = 0.0
    fees: float = 0.0
    slippage: float = 0.0
    net_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    max_drawdown: float = 0.0
    avg_holding_seconds: float = 0.0
    capital_turnover: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0
