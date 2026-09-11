"""Test doubles for the backtest harness.

The backtester must drive the *real* production objects.  While those modules
are still being written, these minimal-but-honest stand-ins let the harness be
developed and regression-tested offline.  They implement exactly the
``INTERFACES.md`` signatures, so anything the harness does to a double it can
also do to the real thing.

They are deliberately dumb: no edge, no cleverness, just enough behaviour to
prove the *harness* is correct (no same-bar fills, stop-before-target, fees and
slippage actually charged, capital recycled).  They are never imported by
``app`` code.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from random import Random
from typing import Any, Sequence

from app.backtest.engine import BacktestConfig, Components, backtest_settings
from app.config import Settings
from app.types import (
    Bar,
    ClosedTrade,
    ExitReason,
    Features,
    Fill,
    MarketSnapshot,
    OrderBook,
    OrderRequest,
    OrderResult,
    PerfStats,
    Position,
    Regime,
    RegimeState,
    RejectReason,
    RiskVerdict,
    Side,
    Signal,
    SymbolInfo,
    Trade,
)

__all__ = [
    "DoubleExitEngine",
    "DoubleFeatureEngine",
    "DoubleLedger",
    "DoubleRanker",
    "DoubleRegimeClassifier",
    "DoubleRiskEngine",
    "DoubleSimulator",
    "DoubleRiskState",
    "AlwaysLongStrategy",
    "NeverStrategy",
    "ScriptedStrategy",
    "make_components",
    "make_bars",
    "breakout_bars",
    "range_bars",
    "trend_bars",
    "single_bar_series",
]


# --------------------------------------------------------------------------- #
# Synthetic bar series
# --------------------------------------------------------------------------- #

MINUTE = 60_000


def make_bars(
    symbol: str,
    count: int,
    *,
    start_ms: int = 1_700_000_000_000,
    start_price: float = 100.0,
    drift_bps: float = 0.0,
    noise_bps: float = 5.0,
    range_bps: float = 20.0,
    quote_volume: float = 250_000.0,
    seed: int = 11,
    taker_buy_frac: float = 0.55,
    step_ms: int = MINUTE,
) -> list[Bar]:
    """A deterministic 1m series with a controllable drift and bar range."""
    rng = Random(seed)
    bars: list[Bar] = []
    price = start_price
    for i in range(count):
        open_ms = start_ms + i * step_ms
        o = price
        step = o * (drift_bps + rng.uniform(-noise_bps, noise_bps)) / 10_000.0
        c = max(o + step, 1e-8)
        half_range = max(abs(c - o), o * range_bps / 10_000.0) / 2.0
        hi = max(o, c) + half_range
        lo = max(min(o, c) - half_range, 1e-9)
        # Volume and taker flow have to vary or features like ``volume_ratio``
        # and ``imbalance`` are constants and whole strategies never fire.
        qv = quote_volume * (1.0 + rng.uniform(-0.45, 0.85))
        flow = min(max(taker_buy_frac + rng.uniform(-0.18, 0.18), 0.05), 0.95)
        bars.append(
            Bar(
                symbol=symbol,
                open_ms=open_ms,
                close_ms=open_ms + step_ms - 1,
                open=o,
                high=hi,
                low=lo,
                close=c,
                volume=qv / c,
                quote_volume=qv,
                trades=int(400 * qv / quote_volume),
                taker_buy_quote=qv * flow,
            )
        )
        price = c
    return bars


def trend_bars(symbol: str = "BTCUSD", count: int = 120, **kw: Any) -> list[Bar]:
    kw.setdefault("drift_bps", 12.0)
    kw.setdefault("noise_bps", 3.0)
    return make_bars(symbol, count, **kw)


def range_bars(symbol: str = "ETHUSD", count: int = 120, **kw: Any) -> list[Bar]:
    kw.setdefault("drift_bps", 0.0)
    kw.setdefault("noise_bps", 6.0)
    return make_bars(symbol, count, **kw)


def breakout_bars(
    symbol: str = "SOLUSD", quiet: int = 60, run: int = 60, **kw: Any
) -> list[Bar]:
    """Compression, then a one-way expansion."""
    kw.pop("drift_bps", None)
    kw.pop("noise_bps", None)
    kw.pop("range_bps", None)
    head = make_bars(symbol, quiet, drift_bps=0.0, noise_bps=1.5, range_bps=6.0, **kw)
    step = head[1].open_ms - head[0].open_ms
    tail_kw = {k: v for k, v in kw.items() if k not in ("start_ms", "start_price")}
    tail = make_bars(
        symbol,
        run,
        start_ms=head[-1].open_ms + step,
        start_price=head[-1].close,
        drift_bps=25.0,
        noise_bps=4.0,
        range_bps=40.0,
        **tail_kw,
    )
    return head + tail


def single_bar_series(
    symbol: str,
    specs: Sequence[tuple[float, float, float, float]],
    *,
    start_ms: int = 1_700_000_000_000,
    quote_volume: float = 250_000.0,
) -> list[Bar]:
    """Hand-written OHLC bars, for tests that need an exact bar shape."""
    bars: list[Bar] = []
    for i, (o, h, lo, c) in enumerate(specs):
        open_ms = start_ms + i * MINUTE
        bars.append(
            Bar(
                symbol=symbol,
                open_ms=open_ms,
                close_ms=open_ms + MINUTE - 1,
                open=o,
                high=h,
                low=lo,
                close=c,
                volume=quote_volume / c,
                quote_volume=quote_volume,
                trades=400,
                taker_buy_quote=quote_volume * 0.5,
            )
        )
    return bars


# --------------------------------------------------------------------------- #
# app/risk.py :: RiskState
# --------------------------------------------------------------------------- #


@dataclass
class DoubleRiskState:
    equity: float
    open_positions: list[Position]
    realized_pnl_today: float
    realized_pnl_week: float
    loss_streak: int
    cooldown_until_ms: int
    kill_switch: bool
    data_healthy: bool
    ts_ms: int


# --------------------------------------------------------------------------- #
# app/features.py
# --------------------------------------------------------------------------- #


class DoubleFeatureEngine:
    def __init__(self, *, settings: Settings | None = None) -> None:
        self.settings = settings
        self._state: dict[str, dict[str, float]] = {}

    def reset(self, symbol: str | None = None) -> None:
        if symbol is None:
            self._state.clear()
        else:
            self._state.pop(symbol, None)

    def compute(self, snap: MarketSnapshot) -> Features:
        bars = snap.bars_1m
        closes = [b.close for b in bars] or [snap.price]
        price = snap.book.mid or closes[-1]

        def ret_bps(n: int) -> float:
            if len(closes) <= n or closes[-1 - n] <= 0:
                return 0.0
            return (closes[-1] - closes[-1 - n]) / closes[-1 - n] * 10_000.0

        window = bars[-14:]
        atr_bps = (
            sum(b.range_bps for b in window) / len(window) if window else 0.0
        )
        rets = [
            (closes[i] - closes[i - 1]) / closes[i - 1] * 10_000.0
            for i in range(1, len(closes))
            if closes[i - 1] > 0
        ][-30:]
        mean = sum(rets) / len(rets) if rets else 0.0
        var = sum((r - mean) ** 2 for r in rets) / len(rets) if rets else 0.0
        rvol = math.sqrt(var)

        ema_fast = self._ema(snap.symbol, "fast", price, 8)
        ema_slow = self._ema(snap.symbol, "slow", price, 21)
        slope_bps = (ema_fast - ema_slow) / price * 10_000.0 if price else 0.0
        hi = max((b.high for b in bars[-60:]), default=price)
        lo = min((b.low for b in bars[-60:]), default=price)
        range_pos = (price - lo) / (hi - lo) if hi > lo else 0.5

        flow_num = sum(
            (t.notional if t.aggressor is Side.LONG else -t.notional)
            for t in snap.recent_trades
        )
        flow_den = sum(t.notional for t in snap.recent_trades) or 1.0

        return Features(
            symbol=snap.symbol,
            ts_ms=snap.ts_ms,
            price=price,
            mid=snap.book.mid,
            microprice=snap.book.microprice,
            spread_bps=snap.book.spread_bps,
            book_age_ms=snap.book.age_ms(snap.ts_ms),
            depth_bid_usd=snap.book.depth_usd("bid"),
            depth_ask_usd=snap.book.depth_usd("ask"),
            imbalance=snap.book.imbalance(),
            atr_bps=atr_bps,
            realized_vol_bps=rvol,
            vol_percentile=0.5,
            ret_1m_bps=ret_bps(1),
            ret_5m_bps=ret_bps(5),
            ret_15m_bps=ret_bps(15),
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            ema_slope_bps=slope_bps,
            trend_strength=min(abs(slope_bps) / 20.0, 1.0),
            range_pos=range_pos,
            bb_width_bps=rvol * 4.0,
            compression=max(0.0, 1.0 - rvol / 25.0),
            volume_ratio=1.0,
            trade_flow_imbalance=flow_num / flow_den,
            trade_flow_accel=0.0,
            vwap=sum(closes[-20:]) / len(closes[-20:]),
            vwap_dev_bps=0.0,
            quote_volume_24h=snap.quote_volume_24h,
            trades_24h=snap.trades_24h,
            bars_available=len(bars),
        )

    def _ema(self, symbol: str, key: str, price: float, span: int) -> float:
        st = self._state.setdefault(symbol, {})
        prev = st.get(key)
        alpha = 2.0 / (span + 1.0)
        value = price if prev is None else prev + alpha * (price - prev)
        st[key] = value
        return value


# --------------------------------------------------------------------------- #
# app/regime.py
# --------------------------------------------------------------------------- #


class DoubleRegimeClassifier:
    def __init__(self, *, settings: Settings | None = None) -> None:
        self.settings = settings

    def classify(self, f: Features) -> RegimeState:
        if f.bars_available < 5:
            return RegimeState(Regime.UNKNOWN, 0.0, ["warming up"])
        if f.trend_strength > 0.35:
            return RegimeState(Regime.TRENDING, min(f.trend_strength, 1.0), ["ema spread"])
        if f.compression > 0.7:
            return RegimeState(Regime.LOW_VOLATILITY, 0.6, ["compressed"])
        return RegimeState(Regime.RANGING, 0.5, ["no trend"])


# --------------------------------------------------------------------------- #
# app/strategies/
# --------------------------------------------------------------------------- #


class ScriptedStrategy:
    """Fires on an explicit list of bar timestamps.  Used for exact-shape tests."""

    allowed_regimes = frozenset(Regime)

    def __init__(
        self,
        name: str = "scripted",
        *,
        fire_at_ms: Iterable[int] = (),
        target_bps: float = 120.0,
        stop_bps: float = 60.0,
        score: float = 0.9,
        side: Side = Side.LONG,
    ) -> None:
        self.name = name
        self.fire_at_ms = set(fire_at_ms)
        self.target_bps = target_bps
        self.stop_bps = stop_bps
        self.score = score
        self.side = side
        self.calls = 0

    def evaluate(self, f: Features, regime: RegimeState) -> Signal | None:
        self.calls += 1
        if f.ts_ms not in self.fire_at_ms:
            return None
        return Signal(
            strategy=self.name,
            symbol=f.symbol,
            side=self.side,
            score=self.score,
            target_bps=self.target_bps,
            stop_bps=self.stop_bps,
            ts_ms=f.ts_ms,
            reasons=["scripted"],
        )


class AlwaysLongStrategy:
    """Signals long whenever the short EMA is above the long one."""

    allowed_regimes = frozenset({Regime.TRENDING, Regime.BREAKOUT, Regime.RANGING})

    def __init__(
        self,
        name: str = "always_long",
        *,
        target_bps: float = 140.0,
        stop_bps: float = 70.0,
        min_bars: int = 25,
    ) -> None:
        self.name = name
        self.target_bps = target_bps
        self.stop_bps = stop_bps
        self.min_bars = min_bars

    def evaluate(self, f: Features, regime: RegimeState) -> Signal | None:
        if f.bars_available < self.min_bars or regime.regime not in self.allowed_regimes:
            return None
        if f.ema_slope_bps <= 0:
            return None
        return Signal(
            strategy=self.name,
            symbol=f.symbol,
            side=Side.LONG,
            score=min(0.5 + f.trend_strength / 2.0, 0.99),
            target_bps=self.target_bps,
            stop_bps=self.stop_bps,
            ts_ms=f.ts_ms,
            reasons=["ema_slope>0"],
        )


class NeverStrategy:
    allowed_regimes = frozenset(Regime)

    def __init__(self, name: str = "never") -> None:
        self.name = name
        self.calls = 0

    def evaluate(self, f: Features, regime: RegimeState) -> Signal | None:
        self.calls += 1
        return None


# --------------------------------------------------------------------------- #
# app/ranker.py
# --------------------------------------------------------------------------- #


class DoubleRanker:
    def __init__(self, *, settings: Settings | None = None, expectancy=None) -> None:
        self.settings = settings
        self.expectancy = expectancy

    def build(
        self,
        signal: Signal,
        f: Features,
        regime: RegimeState,
        *,
        taker_bps: float,
        maker_bps: float,
        slippage_bps: float,
    ):
        from app.types import Opportunity

        p_hit = min(max(0.30 + 0.45 * signal.score * (0.5 + regime.confidence / 2.0), 0.0), 0.95)
        gross = signal.target_bps * p_hit
        fee = 2.0 * taker_bps
        spread_cost = f.spread_bps
        net = gross - fee - spread_cost - slippage_bps
        return Opportunity(
            signal=signal,
            features=f,
            regime=regime,
            gross_edge_bps=gross,
            fee_bps=fee,
            spread_cost_bps=spread_cost,
            slippage_bps=slippage_bps,
            net_edge_bps=net,
            rank_score=net * signal.score,
            expected_hold_seconds=signal.horizon_seconds,
        )

    def rank(self, opps: list) -> list:
        return sorted(opps, key=lambda o: o.rank_score, reverse=True)

    def qualifies(self, opp) -> bool:
        floor = self.settings.risk.min_net_edge_bps if self.settings else 4.0
        return opp.net_edge_bps >= floor


# --------------------------------------------------------------------------- #
# app/risk.py
# --------------------------------------------------------------------------- #


class DoubleRiskEngine:
    def __init__(self, *, settings: Settings | None = None) -> None:
        self.settings = settings or backtest_settings(BacktestConfig())

    def blocked_reason(self, state) -> RejectReason | None:
        r = self.settings.risk
        if state.kill_switch:
            return RejectReason.KILL_SWITCH
        if not state.data_healthy:
            return RejectReason.DATA_FAILURE
        if state.ts_ms < state.cooldown_until_ms:
            return RejectReason.LOSS_STREAK_COOLDOWN
        if state.equity > 0 and state.realized_pnl_today < 0:
            if abs(state.realized_pnl_today) / state.equity * 100.0 >= r.daily_loss_limit_pct:
                return RejectReason.DAILY_LOSS_LIMIT
        if len(state.open_positions) >= r.max_concurrent_positions:
            return RejectReason.MAX_POSITIONS
        return None

    def evaluate(self, opp, state, *, info: SymbolInfo, book: OrderBook) -> RiskVerdict:
        r = self.settings.risk
        sig = opp.signal
        if sig.side is Side.SHORT:
            return RiskVerdict(approved=False, reject_reason=RejectReason.NOT_SUPPORTED_SHORT)
        blocked = self.blocked_reason(state)
        if blocked is not None:
            return RiskVerdict(approved=False, reject_reason=blocked)
        if any(p.symbol == sig.symbol for p in state.open_positions):
            return RiskVerdict(approved=False, reject_reason=RejectReason.DUPLICATE_SYMBOL)
        if not book.is_valid():
            return RiskVerdict(approved=False, reject_reason=RejectReason.STALE_BOOK)
        if book.spread_bps > r.max_entry_spread_bps:
            return RiskVerdict(approved=False, reject_reason=RejectReason.WIDE_SPREAD)
        if opp.net_edge_bps < r.min_net_edge_bps:
            return RiskVerdict(approved=False, reject_reason=RejectReason.EDGE_BELOW_MIN)

        price = book.mid
        stop_dist = price * sig.stop_bps / 10_000.0
        allowance = price * opp.slippage_bps / 10_000.0
        if stop_dist + allowance <= 0:
            return RiskVerdict(approved=False, reject_reason=RejectReason.BAD_REWARD_RISK)

        risk_usd = state.equity * r.risk_per_trade_pct / 100.0
        qty = risk_usd / (stop_dist + allowance)
        max_notional = min(
            state.equity * r.max_position_pct_equity / 100.0,
            max(state.equity * r.max_total_exposure_pct / 100.0
                - sum(p.notional_usd for p in state.open_positions), 0.0),
        )
        qty = min(qty, max_notional / price if price > 0 else 0.0)
        qty = info.floor_qty(qty)
        notional = qty * price
        if qty <= 0 or notional < max(r.min_notional_usd, info.min_notional):
            return RiskVerdict(approved=False, reject_reason=RejectReason.BELOW_MIN_NOTIONAL)

        return RiskVerdict(
            approved=True,
            qty=qty,
            notional_usd=notional,
            risk_usd=qty * stop_dist,
            take_profit=price * (1.0 + sig.target_bps / 10_000.0),
            stop_loss=price * (1.0 - sig.stop_bps / 10_000.0),
        )


# --------------------------------------------------------------------------- #
# app/exits.py
# --------------------------------------------------------------------------- #


class DoubleExitEngine:
    def __init__(self, *, settings: Settings | None = None) -> None:
        self.settings = settings or backtest_settings(BacktestConfig())

    def initial_levels(
        self,
        side: Side,
        entry_price: float,
        f: Features | None,
        signal: Signal,
        *,
        round_trip_cost_bps: float,
        info: SymbolInfo,
    ) -> tuple[float, float]:
        e = self.settings.exits
        tp_bps = max(signal.target_bps, round_trip_cost_bps + e.tp_min_bps_over_cost)
        sl_bps = max(signal.stop_bps, 1.0)
        if side is Side.LONG:
            tp = entry_price * (1.0 + tp_bps / 10_000.0)
            sl = entry_price * (1.0 - sl_bps / 10_000.0)
        else:
            tp = entry_price * (1.0 - tp_bps / 10_000.0)
            sl = entry_price * (1.0 + sl_bps / 10_000.0)
        return info.round_price(tp), info.round_price(sl)

    def check(
        self,
        pos: Position,
        book: OrderBook,
        f: Features | None,
        ts_ms: int,
        *,
        reversal: bool = False,
    ) -> ExitReason | None:
        price = book.mid
        if price <= 0:
            return None
        long = pos.side is Side.LONG
        # Stop is always evaluated first — the pessimistic tie-break.
        if long and price <= pos.stop_loss:
            return ExitReason.STOP_LOSS
        if not long and price >= pos.stop_loss:
            return ExitReason.STOP_LOSS
        if long and price >= pos.take_profit:
            return ExitReason.TAKE_PROFIT
        if not long and price <= pos.take_profit:
            return ExitReason.TAKE_PROFIT
        if pos.trailing_armed and pos.best_price > 0:
            give = self.settings.exits.trailing_give_back_frac
            move = abs(pos.best_price - pos.entry_price)
            trigger = (
                pos.best_price - move * give if long else pos.best_price + move * give
            )
            if (long and price <= trigger) or (not long and price >= trigger):
                return ExitReason.TRAILING_PROFIT
        if pos.time_stop_ms and ts_ms >= pos.time_stop_ms:
            return ExitReason.TIME_STOP
        if reversal:
            return ExitReason.SIGNAL_REVERSAL
        return None

    def update_trailing(self, pos: Position, price: float) -> None:
        if not self.settings.exits.trailing_enabled or price <= 0:
            return
        long = pos.side is Side.LONG
        if pos.best_price <= 0:
            pos.best_price = pos.entry_price
        if (long and price > pos.best_price) or (not long and price < pos.best_price):
            pos.best_price = price
        arm = self.settings.exits.trailing_arm_frac
        target_move = abs(pos.take_profit - pos.entry_price) * arm
        if target_move > 0 and abs(pos.best_price - pos.entry_price) >= target_move:
            pos.trailing_armed = True


# --------------------------------------------------------------------------- #
# app/sim/simulator.py
# --------------------------------------------------------------------------- #


class DoubleSimulator:
    """Walks the book from the far side; never fills at mid, always charges fees."""

    def __init__(self, *, settings: Settings | None = None, rng: Random | None = None) -> None:
        self.settings = settings or backtest_settings(BacktestConfig())
        self.rng = rng or Random(7)

    def estimate_slippage_bps(self, side: Side, notional_usd: float, book: OrderBook) -> float:
        levels = book.asks if side is Side.LONG else book.bids
        mid = book.mid
        if not levels or mid <= 0 or notional_usd <= 0:
            return 0.0
        remaining = notional_usd
        cost = 0.0
        qty = 0.0
        for lv in levels:
            take_notional = min(remaining, lv.price * lv.qty)
            take_qty = take_notional / lv.price
            cost += take_qty * lv.price
            qty += take_qty
            remaining -= take_notional
            if remaining <= 0:
                break
        if qty <= 0:
            return 0.0
        avg = cost / qty
        raw = (avg - mid) / mid * 10_000.0
        return raw if side is Side.LONG else -raw

    def execute(
        self,
        req: OrderRequest,
        book: OrderBook,
        info: SymbolInfo,
        *,
        trades: list[Trade] | None = None,
        ts_ms: int | None = None,
        latency_ms: int | None = None,
    ) -> OrderResult:
        buy = req.side is Side.LONG
        levels = book.asks if buy else book.bids
        mid = book.mid
        if req.qty <= 0 or not levels or mid <= 0:
            return OrderResult(
                accepted=False,
                order_type=req.order_type,
                requested_qty=req.qty,
                filled_qty=0.0,
                avg_price=0.0,
                fees_usd=0.0,
                slippage_bps=0.0,
                reject_reason="NO_LIQUIDITY",
            )

        remaining = req.qty
        fills: list[Fill] = []
        cost = 0.0
        max_levels = self.settings.execution.max_book_walk_levels
        adverse = self.settings.execution.adverse_bps / 10_000.0
        fee_bps = self.settings.fees.taker_bps(req.symbol)
        stamp = ts_ms if ts_ms is not None else req.ts_ms

        for lv in levels[:max_levels]:
            if remaining <= 0:
                break
            take = min(remaining, lv.qty)
            price = lv.price * (1.0 + adverse) if buy else lv.price * (1.0 - adverse)
            fee = price * take * fee_bps / 10_000.0
            fills.append(Fill(price=price, qty=take, fee_usd=fee, is_maker=False, ts_ms=stamp))
            cost += price * take
            remaining -= take

        filled = sum(f.qty for f in fills)
        if filled <= 0:
            return OrderResult(
                accepted=False,
                order_type=req.order_type,
                requested_qty=req.qty,
                filled_qty=0.0,
                avg_price=0.0,
                fees_usd=0.0,
                slippage_bps=0.0,
                reject_reason="NO_FILL",
            )
        avg = cost / filled
        raw_slip = (avg - mid) / mid * 10_000.0
        return OrderResult(
            accepted=True,
            order_type=req.order_type,
            requested_qty=req.qty,
            filled_qty=filled,
            avg_price=avg,
            fees_usd=sum(f.fee_usd for f in fills),
            slippage_bps=raw_slip if buy else -raw_slip,
            fills=fills,
            latency_ms=latency_ms
            if latency_ms is not None
            else self.settings.execution.latency_ms,
        )


# --------------------------------------------------------------------------- #
# app/sim/ledger.py
# --------------------------------------------------------------------------- #


class DoubleLedger:
    def __init__(self, name: str, starting_equity: float, simulator_version: str) -> None:
        self.name = name
        self.starting_equity = starting_equity
        self.simulator_version = simulator_version
        self.cash = starting_equity
        self.open_positions: dict[str, Position] = {}
        self.closed_trades: list[ClosedTrade] = []
        self._prices: dict[str, float] = {}
        self._points: list[tuple[int, float]] = []
        self._peak = starting_equity
        self._max_dd = 0.0

    # -- capital ------------------------------------------------------------ #

    def can_afford(self, notional_usd: float) -> bool:
        return self.cash >= notional_usd

    def open(self, pos: Position, entry: OrderResult) -> None:
        self.cash -= entry.avg_price * entry.filled_qty + entry.fees_usd
        self.open_positions[pos.id] = pos
        self._prices[pos.symbol] = pos.entry_price

    def close(
        self, pos: Position, exit_: OrderResult, reason: ExitReason, ts_ms: int
    ) -> ClosedTrade:
        self.open_positions.pop(pos.id, None)
        proceeds = exit_.avg_price * exit_.filled_qty - exit_.fees_usd
        self.cash += proceeds  # capital recycling: notional + net P&L return at once
        qty = exit_.filled_qty or pos.qty
        if pos.side is Side.LONG:
            gross = (exit_.avg_price - pos.entry_price) * qty
        else:
            gross = (pos.entry_price - exit_.avg_price) * qty
        fees = pos.entry_fees_usd + exit_.fees_usd
        notional = pos.entry_price * qty
        slippage = (
            (abs(pos.entry_slippage_bps) + abs(exit_.slippage_bps)) / 10_000.0 * notional
        )
        trade = ClosedTrade(
            id=pos.id,
            symbol=pos.symbol,
            side=pos.side,
            strategy=pos.strategy,
            regime=pos.regime,
            ledger=self.name,
            qty=qty,
            entry_price=pos.entry_price,
            exit_price=exit_.avg_price,
            entry_ms=pos.entry_ms,
            exit_ms=ts_ms,
            notional_usd=notional,
            gross_pnl_usd=gross,
            entry_fee_usd=pos.entry_fees_usd,
            exit_fee_usd=exit_.fees_usd,
            slippage_usd=slippage,
            net_pnl_usd=gross - fees,
            exit_reason=reason,
            risk_usd=pos.risk_usd,
            expected_net_edge_bps=pos.expected_net_edge_bps,
            simulator_version=self.simulator_version,
        )
        self.closed_trades.append(trade)
        return trade

    def mark(self, prices: dict[str, float], ts_ms: int) -> None:
        self._prices.update(prices)
        for pos in self.open_positions.values():
            price = self._prices.get(pos.symbol)
            if price:
                pos.last_price = price
        eq = self.equity
        self._peak = max(self._peak, eq)
        self._max_dd = max(self._max_dd, self._peak - eq)
        self._points.append((ts_ms, eq))

    # -- views -------------------------------------------------------------- #

    @property
    def equity(self) -> float:
        return self.cash + sum(
            (self._prices.get(p.symbol) or p.entry_price) * p.qty
            for p in self.open_positions.values()
        )

    @property
    def exposure_usd(self) -> float:
        return sum(
            (self._prices.get(p.symbol) or p.entry_price) * p.qty
            for p in self.open_positions.values()
        )

    @property
    def unrealized_pnl(self) -> float:
        return sum(
            p.unrealized_usd(self._prices.get(p.symbol) or p.entry_price)
            for p in self.open_positions.values()
        )

    @property
    def realized_pnl(self) -> float:
        return sum(t.net_pnl_usd for t in self.closed_trades)

    def realized_since(self, since_ms: int) -> float:
        return sum(t.net_pnl_usd for t in self.closed_trades if t.exit_ms >= since_ms)

    def loss_streak(self) -> int:
        streak = 0
        for t in reversed(self.closed_trades):
            if t.is_win:
                break
            streak += 1
        return streak

    def equity_points(self) -> list[tuple[int, float]]:
        return list(self._points)

    def stats(self, *, strategy: str | None = None, since_ms: int | None = None) -> PerfStats:
        trades = [
            t
            for t in self.closed_trades
            if (strategy is None or t.strategy == strategy)
            and (since_ms is None or t.exit_ms >= since_ms)
        ]
        s = PerfStats()
        if not trades:
            return s
        wins = [t for t in trades if t.is_win]
        losses = [t for t in trades if not t.is_win]
        s.trades = len(trades)
        s.wins = len(wins)
        s.losses = len(losses)
        s.gross_pnl = sum(t.gross_pnl_usd for t in trades)
        s.fees = sum(t.fees_usd for t in trades)
        s.slippage = sum(t.slippage_usd for t in trades)
        s.net_pnl = sum(t.net_pnl_usd for t in trades)
        s.avg_win = sum(t.net_pnl_usd for t in wins) / len(wins) if wins else 0.0
        s.avg_loss = sum(t.net_pnl_usd for t in losses) / len(losses) if losses else 0.0
        win_sum = sum(t.net_pnl_usd for t in wins)
        loss_sum = abs(sum(t.net_pnl_usd for t in losses))
        s.profit_factor = (
            win_sum / loss_sum if loss_sum > 0 else (float("inf") if win_sum > 0 else 0.0)
        )
        s.expectancy = s.net_pnl / s.trades
        s.max_drawdown = self._max_dd
        s.avg_holding_seconds = sum(t.holding_seconds for t in trades) / s.trades
        s.capital_turnover = (
            sum(t.notional_usd for t in trades) / self.starting_equity
            if self.starting_equity
            else 0.0
        )
        return s


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def make_components(
    cfg: BacktestConfig,
    *,
    strategies: Sequence[Any] | None = None,
    settings: Settings | None = None,
) -> Components:
    s = settings or backtest_settings(cfg)
    strats = list(strategies) if strategies is not None else [AlwaysLongStrategy()]
    return Components(
        features=DoubleFeatureEngine(settings=s),
        regime=DoubleRegimeClassifier(settings=s),
        strategies=strats,
        ranker=DoubleRanker(settings=s),
        risk=DoubleRiskEngine(settings=s),
        exits=DoubleExitEngine(settings=s),
        simulator=DoubleSimulator(settings=s, rng=Random(cfg.seed)),
        ledger_factory=DoubleLedger,
        risk_state_cls=DoubleRiskState,
    )
