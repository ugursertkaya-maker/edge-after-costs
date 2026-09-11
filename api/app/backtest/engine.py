"""Bar-driven backtester.

This module contains **no** strategy, sizing or exit logic.  It is a harness:
it turns 1m bars into the exact objects the live worker sees
(``MarketSnapshot`` / ``OrderBook`` / ``Trade``) and then drives the real
``FeatureEngine``, ``RegimeClassifier``, strategies, ``OpportunityRanker``,
``RiskEngine``, ``ExitEngine``, ``PaperSimulator`` and ``Ledger``.  If a number
in the report is wrong, the bug is in the component, not in a second
implementation living here.

Three deliberate pessimisms, because each of them is a lie backtests usually
tell:

1. **No same-bar fills.**  A signal computed from bar *N*'s close fills against
   bar *N + latency_bars*'s open.  ``latency_bars < 1`` is rejected outright.
2. **Stop before target.**  When one bar's range contains both the stop and the
   take-profit, the stop is assumed to have hit first.  The exit engine is
   probed at the *adverse* extreme before the favourable one.
3. **Never fill at mid.**  Every entry and exit goes through ``PaperSimulator``
   against a synthesised ladder, so fees, spread and book-walk slippage are all
   charged.

Book synthesis from a bar
-------------------------
``mid = close`` (entries use the fill bar's ``open``).  The half-spread is
``cfg.spread_bps / 2`` when the caller pins it, otherwise the quoted spread is
estimated as ``max(MIN_SPREAD_BPS, bar.range_bps * SPREAD_RANGE_FRAC)`` and
halved.  The estimator says "a minute that travelled far was a minute that was
expensive to trade"; the 2 bps floor means a synthetic book is never tighter
than a real one on the most liquid pair on the venue.  Depth is laddered
outwards in ``BOOK_LEVELS`` steps sized from the bar's own quote volume,
growing with distance, and skewed by the bar's taker-buy split (see
``DEPTH_SKEW_FROM_FLOW``) so the simulator has something plausible to walk and
``Features.imbalance`` is not pinned to zero.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from random import Random
from typing import Any, Callable, Sequence

from app.config import Settings, get_settings
from app.types import (
    Bar,
    BookLevel,
    ClosedTrade,
    ExitReason,
    Features,
    MarketSnapshot,
    OrderBook,
    OrderRequest,
    OrderType,
    PerfStats,
    Position,
    RegimeState,
    Side,
    Signal,
    SymbolInfo,
    Trade,
)

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "Components",
    "build_components",
    "combine_stats",
    "backtest_settings",
    "real_components_available",
    "run_backtest",
    "synth_book",
    "synth_symbol_info",
    "synth_trades",
]

# --- book synthesis knobs (documented in the module docstring) -------------- #
#: Floor on the *quoted spread* (bid to ask), not on the half-spread.  Binance.US
#: BTCUSD quotes roughly 1 bps top-of-book, so a synthetic book that is never
#: tighter than 2 bps is already pessimistic.  Raise this to 4.0 to floor the
#: half-spread at 2 bps instead — but note that ``max_entry_spread_bps`` defaults
#: to 6.0, so a 4 bps floor spends most of the entry gate on the floor itself and
#: rejects most bars as WIDE_SPREAD before a strategy is even consulted.
MIN_SPREAD_BPS = 2.0
MIN_HALF_SPREAD_BPS = MIN_SPREAD_BPS / 2.0
#: Quoted spread as a fraction of the bar's own high-low range.
SPREAD_RANGE_FRAC = 0.10
MAX_HALF_SPREAD_BPS = 100.0
BOOK_LEVELS = 8
LEVEL_DEPTH_FRAC = 0.02
MIN_LEVEL_NOTIONAL_USD = 1_000.0
#: How far the bar's taker-buy split is allowed to skew resting depth.  A
#: minute in which buyers lifted most of the volume ended with a thinner ask
#: side, so ``imbalance`` leans positive.  This is a *model*, not a
#: measurement — a perfectly symmetric ladder would be worse (it pins
#: ``Features.imbalance`` to exactly 0.0 and silently disables any strategy
#: that reads it), but no book-shape conclusion drawn from a backtest is
#: stronger than this assumption.  Capped well below 1.0 so it can never
#: manufacture a one-sided book.
DEPTH_SKEW_FROM_FLOW = 0.35
SYNTH_TRADES_PER_BAR = 8
HISTORY_BARS = 240
BARS_PER_DAY = 1_440
MS_PER_DAY = 86_400_000


# --------------------------------------------------------------------------- #
# Config / result
# --------------------------------------------------------------------------- #


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
    label: str
    start_ms: int
    end_ms: int
    trades: list[ClosedTrade]
    stats: PerfStats
    per_strategy: dict[str, PerfStats]
    equity_curve: list[tuple[int, float]]
    starting_equity: float
    ending_equity: float
    # Diagnostics.  Defaulted so the frozen constructor order is unaffected.
    symbols: tuple[str, ...] = ()
    bars_processed: int = 0
    signals_generated: int = 0
    entries_approved: int = 0
    entries_filled: int = 0
    rejections: dict[str, int] = field(default_factory=dict)
    open_at_end: int = 0
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Component wiring
# --------------------------------------------------------------------------- #


@dataclass
class Components:
    """The live objects the harness drives.

    Injectable so tests can run offline against doubles while the real modules
    are still being written; ``build_components`` is what production uses.
    """

    features: Any
    regime: Any
    strategies: list[Any]
    ranker: Any
    risk: Any
    exits: Any
    simulator: Any
    ledger_factory: Callable[..., Any]
    risk_state_cls: Any


def real_components_available() -> bool:
    """True when every production module this harness needs is importable."""
    try:
        build_components(BacktestConfig())
    except Exception:
        return False
    return True


def build_components(cfg: BacktestConfig, settings: Settings | None = None) -> Components:
    """Instantiate the real production objects.

    Imported lazily: while sibling modules are still being written, importing
    ``app.backtest.engine`` must not explode.
    """
    from app.exits import ExitEngine
    from app.features import FeatureEngine
    from app.ranker import OpportunityRanker
    from app.regime import RegimeClassifier
    from app.risk import RiskEngine, RiskState
    from app.sim.ledger import Ledger
    from app.sim.simulator import PaperSimulator
    from app.strategies import get_strategies

    s = settings or backtest_settings(cfg)
    if cfg.strategies is not None and len(cfg.strategies) == 0:
        strategies: list[Any] = []
    else:
        strategies = list(get_strategies(cfg.strategies))

    return Components(
        features=FeatureEngine(settings=s),
        regime=RegimeClassifier(settings=s),
        strategies=strategies,
        ranker=OpportunityRanker(settings=s),
        risk=RiskEngine(settings=s),
        exits=ExitEngine(settings=s),
        simulator=PaperSimulator(settings=s, rng=Random(cfg.seed)),
        ledger_factory=Ledger,
        risk_state_cls=RiskState,
    )


def backtest_settings(cfg: BacktestConfig, base: Settings | None = None) -> Settings:
    """A ``Settings`` whose costs and limits match the backtest config.

    ``fee_bps_per_side`` is pushed in through ``FeeConfig.stress_bps`` (a flat
    both-sides override) and ``slippage_bps`` through ``ExecConfig.adverse_bps``
    so the *simulator* charges them — the harness never subtracts a cost by
    hand.
    """
    from dataclasses import replace

    s = base or get_settings()
    return replace(
        s,
        fees=replace(s.fees, stress_bps=cfg.fee_bps_per_side),
        execution=replace(s.execution, adverse_bps=cfg.slippage_bps),
        risk=replace(
            s.risk,
            starting_equity=cfg.starting_equity,
            risk_per_trade_pct=cfg.risk_per_trade_pct,
            max_concurrent_positions=cfg.max_concurrent,
        ),
    )


# --------------------------------------------------------------------------- #
# Synthesis helpers
# --------------------------------------------------------------------------- #


def synth_symbol_info(symbol: str, price: float, quote_asset: str = "USD") -> SymbolInfo:
    """Plausible exchange filters when no ``exchangeInfo`` is available.

    Real filters can be injected into :func:`run_backtest`; these exist so a
    cached-bars-only backtest still exercises the rounding paths.
    """
    if price > 0:
        tick = 10 ** (math.floor(math.log10(price)) - 5)
        tick = min(max(tick, 1e-8), 0.01)
    else:
        tick = 1e-8
    base = symbol[: -len(quote_asset)] if symbol.endswith(quote_asset) else symbol
    return SymbolInfo(
        symbol=symbol,
        base_asset=base or symbol,
        quote_asset=quote_asset,
        status="TRADING",
        tick_size=tick,
        step_size=1e-6,
        min_qty=1e-6,
        min_notional=1.0,
    )


def half_spread_bps_for(bar: Bar, spread_bps: float | None) -> float:
    """See the module docstring for the estimator's rationale."""
    if spread_bps is not None:
        return max(spread_bps / 2.0, 0.0)
    est = max(bar.range_bps * SPREAD_RANGE_FRAC, MIN_SPREAD_BPS) / 2.0
    return min(est, MAX_HALF_SPREAD_BPS)


def synth_book(
    bar: Bar,
    *,
    price: float | None = None,
    spread_bps: float | None = None,
    ts_ms: int | None = None,
    levels: int = BOOK_LEVELS,
) -> OrderBook:
    """A ladder centred on ``price`` (default: the bar's close)."""
    mid = price if price is not None else bar.close
    if mid <= 0:
        return OrderBook(symbol=bar.symbol, last_update_ms=ts_ms or bar.close_ms)

    half_bps = half_spread_bps_for(bar, spread_bps)
    step_bps = max(half_bps, 1.0)
    level_notional = max(MIN_LEVEL_NOTIONAL_USD, bar.quote_volume * LEVEL_DEPTH_FRAC)

    # See DEPTH_SKEW_FROM_FLOW: taker buying eats the ask side.
    if bar.quote_volume > 0 and bar.taker_buy_quote > 0:
        buy_frac = min(max(bar.taker_buy_quote / bar.quote_volume, 0.0), 1.0)
    else:
        buy_frac = 0.5
    skew = (buy_frac - 0.5) * 2.0 * DEPTH_SKEW_FROM_FLOW
    bid_mult = 1.0 + skew
    ask_mult = 1.0 - skew

    bids: list[BookLevel] = []
    asks: list[BookLevel] = []
    for i in range(levels):
        off = mid * (half_bps + i * step_bps) / 10_000.0
        notional = level_notional * (1.0 + 0.5 * i)
        bid_price = mid - off
        ask_price = mid + off
        if bid_price <= 0:
            break
        bids.append(BookLevel(price=bid_price, qty=notional * bid_mult / bid_price))
        asks.append(BookLevel(price=ask_price, qty=notional * ask_mult / ask_price))

    stamp = ts_ms if ts_ms is not None else bar.close_ms
    return OrderBook(
        symbol=bar.symbol,
        bids=bids,
        asks=asks,
        last_update_id=stamp,
        last_update_ms=stamp,
    )


def synth_trades(bar: Bar, rng: Random, count: int = SYNTH_TRADES_PER_BAR) -> list[Trade]:
    """Prints consistent with the bar's volume and taker-buy split.

    ``taker_buy_quote`` decides how many prints were buyer-aggressed; when the
    field is absent we assume a 50/50 split rather than inventing a flow edge.
    """
    if count <= 0 or bar.quote_volume <= 0 or bar.close <= 0:
        return []
    buy_quote = bar.taker_buy_quote if bar.taker_buy_quote > 0 else bar.quote_volume / 2.0
    buy_quote = min(buy_quote, bar.quote_volume)
    sell_quote = max(bar.quote_volume - buy_quote, 0.0)
    n_buy = int(round(count * buy_quote / bar.quote_volume))
    span = max(bar.close_ms - bar.open_ms, 1)
    wobble = (bar.high - bar.low) * 0.25

    out: list[Trade] = []
    for i in range(count):
        frac = i / max(count - 1, 1)
        price = bar.open + (bar.close - bar.open) * frac + rng.uniform(-1.0, 1.0) * wobble
        price = min(max(price, bar.low), bar.high)
        is_buy = i < n_buy
        quote = (buy_quote / n_buy) if is_buy and n_buy else (
            sell_quote / max(count - n_buy, 1)
        )
        if price <= 0:
            continue
        out.append(
            Trade(
                symbol=bar.symbol,
                price=price,
                qty=quote / price,
                ts_ms=bar.open_ms + int(span * frac),
                # buyer_is_maker True => the aggressor was a SELLER.
                buyer_is_maker=not is_buy,
            )
        )
    out.sort(key=lambda t: t.ts_ms)
    return out


# --------------------------------------------------------------------------- #
# Stats aggregation
# --------------------------------------------------------------------------- #


def combine_stats(parts: Sequence[PerfStats]) -> PerfStats:
    """Pool ``PerfStats`` from separate ledgers (walk-forward windows).

    Drawdown cannot be meaningfully concatenated across disjoint equity curves,
    so the *worst window* drawdown is reported rather than a fabricated joint
    figure.
    """
    out = PerfStats()
    win_sum = 0.0
    loss_sum = 0.0
    hold_sum = 0.0
    for p in parts:
        out.trades += p.trades
        out.wins += p.wins
        out.losses += p.losses
        out.gross_pnl += p.gross_pnl
        out.fees += p.fees
        out.slippage += p.slippage
        out.net_pnl += p.net_pnl
        out.capital_turnover += p.capital_turnover
        out.max_drawdown = max(out.max_drawdown, p.max_drawdown)
        win_sum += p.avg_win * p.wins
        loss_sum += abs(p.avg_loss) * p.losses
        hold_sum += p.avg_holding_seconds * p.trades
    out.avg_win = win_sum / out.wins if out.wins else 0.0
    out.avg_loss = -(loss_sum / out.losses) if out.losses else 0.0
    if loss_sum > 0:
        out.profit_factor = win_sum / loss_sum
    else:
        out.profit_factor = float("inf") if win_sum > 0 else 0.0
    out.expectancy = out.net_pnl / out.trades if out.trades else 0.0
    out.avg_holding_seconds = hold_sum / out.trades if out.trades else 0.0
    return out


# --------------------------------------------------------------------------- #
# The harness
# --------------------------------------------------------------------------- #


@dataclass
class _Pending:
    """An approved entry waiting out the latency delay."""

    symbol: str
    fill_index: int
    signal: Signal
    features: Features
    regime: RegimeState
    verdict: Any
    opportunity: Any
    created_ms: int
    #: Provisional position representing the in-flight order.  An approved but
    #: unfilled entry is already committed risk — leaving it out would let the
    #: concurrency and per-symbol caps be breached during the latency window,
    #: which is a bug the live worker (250 ms of latency) would barely show but
    #: a bar-latency backtest shows immediately.
    provisional: Position | None = None


def _day_start_ms(ts_ms: int) -> int:
    return ts_ms - (ts_ms % MS_PER_DAY)


def _week_start_ms(ts_ms: int) -> int:
    """Monday 00:00 UTC.  Epoch day 0 was a Thursday, hence the +3."""
    day = ts_ms // MS_PER_DAY
    return (day - ((day + 3) % 7)) * MS_PER_DAY


class _Backtester:
    def __init__(
        self,
        bars_by_symbol: dict[str, list[Bar]],
        cfg: BacktestConfig,
        *,
        label: str,
        components: Components | None,
        settings: Settings | None,
        infos: dict[str, SymbolInfo] | None,
    ) -> None:
        if cfg.latency_bars < 1:
            raise ValueError(
                "latency_bars must be >= 1: same-bar fills are not a real execution"
            )
        self.cfg = cfg
        self.label = label
        self.rng = Random(cfg.seed)
        self.settings = settings or backtest_settings(cfg)
        if components is None:
            self.c = build_components(cfg, self.settings)
        else:
            # Injected components are built once and reused across walk-forward
            # windows, so the per-window strategy subset has to be applied here
            # rather than at construction time.
            self.c = components
            if cfg.strategies is not None:
                from dataclasses import replace as _replace

                allowed = set(cfg.strategies)
                self.c = _replace(
                    components,
                    strategies=[s for s in components.strategies if s.name in allowed],
                )
        self.infos = dict(infos or {})

        self.series = {
            sym: sorted(bars, key=lambda b: b.open_ms)
            for sym, bars in bars_by_symbol.items()
            if bars
        }
        self.history: dict[str, deque[Bar]] = {
            sym: deque(maxlen=HISTORY_BARS) for sym in self.series
        }
        self.rolling_quote: dict[str, deque[float]] = {
            sym: deque(maxlen=BARS_PER_DAY) for sym in self.series
        }
        self.rolling_trades: dict[str, deque[int]] = {
            sym: deque(maxlen=BARS_PER_DAY) for sym in self.series
        }

        self.ledger = self.c.ledger_factory(
            self.label, cfg.starting_equity, self.settings.simulator_version
        )
        self.pending: list[_Pending] = []
        self.equity_curve: list[tuple[int, float]] = []
        self.last_prices: dict[str, float] = {}
        self.cooldown_until_ms = 0
        self.rejections: dict[str, int] = {}
        self.notes: list[str] = []
        self.bars_processed = 0
        self.signals_generated = 0
        self.entries_approved = 0
        self.entries_filled = 0
        self._seq = 0

    # -- small helpers ------------------------------------------------------ #

    def _info(self, symbol: str, price: float) -> SymbolInfo:
        info = self.infos.get(symbol)
        if info is None:
            info = synth_symbol_info(symbol, price)
            self.infos[symbol] = info
        return info

    def _book(self, bar: Bar, price: float, ts_ms: int) -> OrderBook:
        return synth_book(bar, price=price, spread_bps=self.cfg.spread_bps, ts_ms=ts_ms)

    def _count_reject(self, reason: Any) -> None:
        key = getattr(reason, "value", None) or str(reason)
        self.rejections[key] = self.rejections.get(key, 0) + 1

    def _risk_state(self, ts_ms: int) -> Any:
        committed = list(self.ledger.open_positions.values())
        committed += [p.provisional for p in self.pending if p.provisional is not None]
        return self.c.risk_state_cls(
            equity=self.ledger.equity,
            open_positions=committed,
            realized_pnl_today=self.ledger.realized_since(_day_start_ms(ts_ms)),
            realized_pnl_week=self.ledger.realized_since(_week_start_ms(ts_ms)),
            loss_streak=self.ledger.loss_streak(),
            cooldown_until_ms=self.cooldown_until_ms,
            kill_switch=False,
            data_healthy=True,
            ts_ms=ts_ms,
        )

    # -- main loop ---------------------------------------------------------- #

    def run(self) -> BacktestResult:
        timeline: list[tuple[int, str, int]] = [
            (bar.open_ms, sym, idx)
            for sym, bars in self.series.items()
            for idx, bar in enumerate(bars)
        ]
        timeline.sort()
        if not timeline:
            return self._result(0, 0)

        start_ms = timeline[0][0]
        end_ms = max(self.series[s][-1].close_ms for s in self.series)

        i = 0
        n = len(timeline)
        while i < n:
            ts = timeline[i][0]
            group: list[tuple[str, int]] = []
            while i < n and timeline[i][0] == ts:
                group.append((timeline[i][1], timeline[i][2]))
                i += 1

            # Chronology inside a minute: append history, fill at the open,
            # work the intrabar extremes, mark, then decide at the close.
            for sym, idx in group:
                bar = self.series[sym][idx]
                self.history[sym].append(bar)
                self.rolling_quote[sym].append(bar.quote_volume)
                self.rolling_trades[sym].append(bar.trades)
                self.last_prices[sym] = bar.close
                self.bars_processed += 1

            for sym, idx in group:
                self._fill_pending(sym, idx)
            for sym, idx in group:
                self._work_exits(sym, idx)

            self.ledger.mark(dict(self.last_prices), ts)
            self.equity_curve.append((ts, float(self.ledger.equity)))

            for sym, idx in group:
                self._decide(sym, idx)

        return self._result(start_ms, end_ms)

    # -- phase: entries ----------------------------------------------------- #

    def _fill_pending(self, symbol: str, idx: int) -> None:
        due = [p for p in self.pending if p.symbol == symbol and p.fill_index <= idx]
        if not due:
            return
        self.pending = [p for p in self.pending if not (p.symbol == symbol and p.fill_index <= idx)]
        bar = self.series[symbol][idx]
        for p in due:
            if p.fill_index != idx:  # the bar it should have filled on is gone
                self.notes.append(f"dropped stale pending entry for {symbol}")
                continue
            self._open_position(p, bar)

    def _open_position(self, p: _Pending, bar: Bar) -> None:
        # Entries fill against the *open* of the later bar: the decision was
        # made on the previous close and this is the first price it could have
        # reached.
        ts = bar.open_ms
        price = bar.open
        info = self._info(p.symbol, price)
        book = self._book(bar, price, ts)
        if not book.is_valid():
            return

        qty = float(getattr(p.verdict, "qty", 0.0))
        if qty <= 0:
            return
        if not self.ledger.can_afford(qty * price):
            self._count_reject("NO_FREE_CASH")
            return

        req = OrderRequest(
            symbol=p.symbol,
            side=p.signal.side,
            order_type=OrderType.MARKET,
            qty=qty,
            client_id=f"bt-entry-{self._next_seq()}",
            ts_ms=ts,
        )
        res = self.c.simulator.execute(
            req, book, info, trades=synth_trades(bar, self.rng), ts_ms=ts
        )
        if not res.accepted or res.filled_qty <= 0:
            self._count_reject(res.reject_reason or "SIM_NO_FILL")
            return

        filled = res.filled_qty
        entry_price = res.avg_price
        fill_ratio = filled / qty if qty > 0 else 0.0
        opp = p.opportunity
        round_trip_bps = (
            float(getattr(opp, "fee_bps", 0.0))
            + float(getattr(opp, "spread_cost_bps", 0.0))
            + float(getattr(opp, "slippage_bps", 0.0))
        )

        tp, sl = self._levels(p, entry_price, round_trip_bps, info)
        if tp <= 0 or sl <= 0:
            self._count_reject("NO_EXIT_LEVELS")
            return

        pos = Position(
            id=f"bt-{p.symbol}-{self._next_seq()}",
            symbol=p.symbol,
            side=p.signal.side,
            strategy=p.signal.strategy,
            qty=filled,
            entry_price=entry_price,
            entry_ms=ts,
            notional_usd=entry_price * filled,
            risk_usd=float(getattr(p.verdict, "risk_usd", 0.0)) * fill_ratio,
            take_profit=tp,
            stop_loss=sl,
            entry_fees_usd=res.fees_usd,
            entry_slippage_bps=res.slippage_bps,
            ledger=self.ledger.name,
            regime=p.regime.regime.value,
            best_price=entry_price,
            target_bps=p.signal.target_bps,
            stop_bps=p.signal.stop_bps,
            expected_net_edge_bps=float(getattr(opp, "net_edge_bps", 0.0)),
            time_stop_ms=ts + self.settings.exits.time_stop_seconds * 1000,
            last_price=entry_price,
        )
        self.ledger.open(pos, res)
        self.entries_filled += 1

    def _levels(
        self, p: _Pending, entry_price: float, round_trip_bps: float, info: SymbolInfo
    ) -> tuple[float, float]:
        """Exit levels anchored on the *actual* fill price.

        The risk engine sized the position from the decision-time price; by the
        time we fill, price has moved, so the levels are recomputed by the real
        ``ExitEngine`` around the fill.  The verdict's levels are the fallback.
        """
        try:
            tp, sl = self.c.exits.initial_levels(
                p.signal.side,
                entry_price,
                p.features,
                p.signal,
                round_trip_cost_bps=round_trip_bps,
                info=info,
            )
            if tp > 0 and sl > 0:
                return float(tp), float(sl)
        except Exception as exc:  # pragma: no cover - surfaced as a note
            self.notes.append(f"exits.initial_levels failed: {exc!r}")
        return (
            float(getattr(p.verdict, "take_profit", 0.0)),
            float(getattr(p.verdict, "stop_loss", 0.0)),
        )

    # -- phase: exits ------------------------------------------------------- #

    def _work_exits(self, symbol: str, idx: int) -> None:
        bar = self.series[symbol][idx]
        open_positions = [
            pos
            for pos in list(self.ledger.open_positions.values())
            if pos.symbol == symbol and pos.entry_ms < bar.open_ms
        ]
        if not open_positions:
            return

        info = self._info(symbol, bar.close)
        feats = None
        ts = bar.close_ms
        for pos in open_positions:
            long = pos.side is Side.LONG
            adverse = bar.low if long else bar.high
            favourable = bar.high if long else bar.low

            reason, probe = self._probe(pos, bar, adverse, feats, ts)
            if reason is None:
                try:
                    self.c.exits.update_trailing(pos, favourable)
                except Exception as exc:  # pragma: no cover
                    self.notes.append(f"exits.update_trailing failed: {exc!r}")
                reason, probe = self._probe(pos, bar, favourable, feats, ts)
            if reason is None:
                try:
                    self.c.exits.update_trailing(pos, bar.close)
                except Exception:  # pragma: no cover
                    pass
                reason, probe = self._probe(pos, bar, bar.close, feats, ts)
            if reason is None:
                continue

            self._close_position(pos, bar, reason, probe, info, ts)

    def _probe(
        self, pos: Position, bar: Bar, price: float, feats: Features | None, ts: int
    ) -> tuple[ExitReason | None, float]:
        """Ask the real ``ExitEngine`` what it would do at ``price``."""
        pos.last_price = price
        book = self._book(bar, price, ts)
        if not book.is_valid():
            return None, price
        try:
            return self.c.exits.check(pos, book, feats, ts), price
        except Exception as exc:  # pragma: no cover
            self.notes.append(f"exits.check failed: {exc!r}")
            return None, price

    def _exec_price(self, pos: Position, bar: Bar, reason: ExitReason, probe: float) -> float:
        """Where the exit actually gets done.

        A stop fills at the stop level, or at the open when the bar gapped
        through it.  A target fills at the target, never at the bar's extreme —
        you only get the price you rested at.  Everything else fills at the
        worse of the probe price and the close.
        """
        long = pos.side is Side.LONG
        if reason is ExitReason.STOP_LOSS and pos.stop_loss > 0:
            return min(pos.stop_loss, bar.open) if long else max(pos.stop_loss, bar.open)
        if reason is ExitReason.TAKE_PROFIT and pos.take_profit > 0:
            return pos.take_profit
        return min(probe, bar.close) if long else max(probe, bar.close)

    def _close_position(
        self,
        pos: Position,
        bar: Bar,
        reason: ExitReason,
        probe: float,
        info: SymbolInfo,
        ts: int,
    ) -> None:
        price = self._exec_price(pos, bar, reason, probe)
        book = self._book(bar, price, ts)
        exit_side = Side.SHORT if pos.side is Side.LONG else Side.LONG
        req = OrderRequest(
            symbol=pos.symbol,
            side=exit_side,
            order_type=OrderType.MARKET,
            qty=pos.qty,
            client_id=f"bt-exit-{self._next_seq()}",
            reduce_only=True,
            ts_ms=ts,
        )
        res = self.c.simulator.execute(
            req, book, info, trades=synth_trades(bar, self.rng), ts_ms=ts
        )
        if not res.accepted or res.filled_qty <= 0:
            self.notes.append(f"exit rejected for {pos.symbol}: {res.reject_reason}")
            return
        if res.filled_qty < pos.qty * 0.999:
            self.notes.append(
                f"partial exit fill on {pos.symbol}: {res.filled_qty:.8f}/{pos.qty:.8f}"
            )
        self.ledger.close(pos, res, reason, ts)

        streak_len = self.settings.risk.loss_streak_len
        if streak_len > 0 and self.ledger.loss_streak() >= streak_len:
            self.cooldown_until_ms = max(
                self.cooldown_until_ms,
                ts + self.settings.risk.loss_streak_cooldown_minutes * 60_000,
            )

    # -- phase: decisions --------------------------------------------------- #

    def _decide(self, symbol: str, idx: int) -> None:
        if not self.c.strategies:
            return
        bar = self.series[symbol][idx]
        if idx + self.cfg.latency_bars >= len(self.series[symbol]):
            return  # no future bar left to fill on; refusing to fill on this one

        ts = bar.close_ms
        price = bar.close
        info = self._info(symbol, price)
        book = self._book(bar, price, ts)
        if not book.is_valid():
            return

        hist = list(self.history[symbol])
        snap = MarketSnapshot(
            symbol=symbol,
            ts_ms=ts,
            book=book,
            bars_1m=hist,
            recent_trades=synth_trades(bar, self.rng),
            info=info,
            quote_volume_24h=float(sum(self.rolling_quote[symbol])),
            trades_24h=int(sum(self.rolling_trades[symbol])),
        )

        feats = self.c.features.compute(snap)
        regime = self.c.regime.classify(feats)

        taker = self.settings.fees.taker_bps(symbol)
        maker = self.settings.fees.maker_bps(symbol)
        opps = []
        for strat in self.c.strategies:
            sig = strat.evaluate(feats, regime)
            if sig is None or sig.symbol != symbol:
                continue
            self.signals_generated += 1
            opp = self.c.ranker.build(
                sig,
                feats,
                regime,
                taker_bps=taker,
                maker_bps=maker,
                slippage_bps=self.cfg.slippage_bps,
            )
            if self.c.ranker.qualifies(opp):
                opps.append(opp)
            else:
                self._count_reject("RANKER_DISQUALIFIED")
        if not opps:
            return

        state = self._risk_state(ts)
        blocked = self.c.risk.blocked_reason(state)
        if blocked is not None:
            self._count_reject(blocked)
            return

        for opp in self.c.ranker.rank(opps):
            verdict = self.c.risk.evaluate(opp, state, info=info, book=book)
            if not verdict.approved:
                self._count_reject(verdict.reject_reason or "RISK_REJECTED")
                continue
            self.entries_approved += 1
            self.pending.append(
                _Pending(
                    symbol=symbol,
                    fill_index=idx + self.cfg.latency_bars,
                    signal=opp.signal,
                    features=feats,
                    regime=regime,
                    verdict=verdict,
                    opportunity=opp,
                    created_ms=ts,
                    provisional=self._provisional(opp, verdict, regime, price, ts),
                )
            )
            return  # at most one new position per symbol per bar

    def _provisional(
        self, opp: Any, verdict: Any, regime: RegimeState, price: float, ts: int
    ) -> Position:
        """A stand-in ``Position`` for an approved-but-unfilled entry."""
        qty = float(getattr(verdict, "qty", 0.0))
        return Position(
            id=f"bt-pending-{self._next_seq()}",
            symbol=opp.signal.symbol,
            side=opp.signal.side,
            strategy=opp.signal.strategy,
            qty=qty,
            entry_price=price,
            entry_ms=ts,
            notional_usd=float(getattr(verdict, "notional_usd", 0.0)) or qty * price,
            risk_usd=float(getattr(verdict, "risk_usd", 0.0)),
            take_profit=float(getattr(verdict, "take_profit", 0.0)),
            stop_loss=float(getattr(verdict, "stop_loss", 0.0)),
            entry_fees_usd=0.0,
            entry_slippage_bps=0.0,
            ledger=self.ledger.name,
            regime=regime.regime.value,
            target_bps=opp.signal.target_bps,
            stop_bps=opp.signal.stop_bps,
            last_price=price,
            meta={"pending": True},
        )

    # -- finish ------------------------------------------------------------- #

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _result(self, start_ms: int, end_ms: int) -> BacktestResult:
        trades = list(self.ledger.closed_trades)
        strategies = sorted({t.strategy for t in trades})
        per_strategy = {name: self.ledger.stats(strategy=name) for name in strategies}
        if not self.equity_curve:
            self.equity_curve = [(start_ms, self.cfg.starting_equity)]
        return BacktestResult(
            label=self.label,
            start_ms=start_ms,
            end_ms=end_ms,
            trades=trades,
            stats=self.ledger.stats(),
            per_strategy=per_strategy,
            equity_curve=self.equity_curve,
            starting_equity=self.cfg.starting_equity,
            ending_equity=float(self.ledger.equity),
            symbols=tuple(sorted(self.series)),
            bars_processed=self.bars_processed,
            signals_generated=self.signals_generated,
            entries_approved=self.entries_approved,
            entries_filled=self.entries_filled,
            rejections=dict(sorted(self.rejections.items())),
            open_at_end=len(self.ledger.open_positions),
            notes=self.notes,
        )


def run_backtest(
    bars_by_symbol: dict[str, list[Bar]],
    cfg: BacktestConfig,
    *,
    label: str = "backtest",
    components: Components | None = None,
    settings: Settings | None = None,
    infos: dict[str, SymbolInfo] | None = None,
) -> BacktestResult:
    """Replay ``bars_by_symbol`` through the live decision stack.

    ``components`` exists so the harness can be exercised against test doubles;
    leave it ``None`` and the real production objects are built.
    """
    return _Backtester(
        bars_by_symbol,
        cfg,
        label=label,
        components=components,
        settings=settings,
        infos=infos,
    ).run()


def slice_bars(
    bars_by_symbol: dict[str, list[Bar]], start_ms: int, end_ms: int
) -> dict[str, list[Bar]]:
    """Half-open ``[start_ms, end_ms)`` slice, keyed the same way."""
    out: dict[str, list[Bar]] = {}
    for sym, bars in bars_by_symbol.items():
        kept = [b for b in bars if start_ms <= b.open_ms < end_ms]
        if kept:
            out[sym] = kept
    return out


def bars_span_ms(bars_by_symbol: dict[str, list[Bar]]) -> tuple[int, int]:
    """(first open_ms, last close_ms) across every symbol."""
    opens: list[int] = []
    closes: list[int] = []
    for bars in bars_by_symbol.values():
        if not bars:
            continue
        opens.append(min(b.open_ms for b in bars))
        closes.append(max(b.close_ms for b in bars))
    if not opens:
        return 0, 0
    return min(opens), max(closes)
