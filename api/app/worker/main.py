"""The autonomous loop.

    live market data -> scanner -> rank -> strategies -> signal -> net edge
    -> risk -> sizing -> paper execution -> monitor -> exit -> capital released
    -> scan again

Three ledgers ($500 / $1,000 / $5,000) see exactly the same decisions; only the
sizing differs, so the comparison is apples to apples.  The $500 ledger is the
one the system is tuned for and the one the dashboard defaults to.

Nothing in this file can place a real order.  It imports the simulator, never an
authenticated client, and `assert_paper_mode()` runs before the loop starts.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal as os_signal
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings, get_settings
from app.exchange.book import BookManager
from app.exchange.rest import BinanceUSRest
from app.exchange.ws import MarketDataFeed
from app.exits import ExitEngine
from app.features import FeatureEngine
from app.ranker import OpportunityRanker
from app.regime import RegimeClassifier
from app.risk import RiskEngine, RiskState
from app.runtime import RuntimeStore
from app.safety import PAPER_ONLY_BANNER, assert_paper_mode
from app.sim.ledger import Ledger
from app.sim.simulator import PaperSimulator
from app.strategies import get_strategies
from app.types import (
    Decision,
    ExitReason,
    Features,
    MarketSnapshot,
    Opportunity,
    OrderRequest,
    OrderType,
    Position,
    Regime,
    RegimeState,
    Side,
    Signal,
    SymbolInfo,
    now_ms,
)
from app.universe import UniverseEntry, UniverseSelector

log = logging.getLogger("cryptobot.worker")

LEDGER_LABELS = {"base": "$500", "l1000": "$1.000", "l5000": "$5.000"}


@dataclass
class ScannerRow:
    """One line of the live scanner table, exactly as the dashboard shows it."""

    symbol: str
    price: float
    long_score: float
    short_score: float
    regime: str
    momentum: float
    volume_ratio: float
    spread_bps: float
    imbalance: float
    net_edge_bps: float
    strategy: str | None
    decision: str
    reject_reason: str | None = None
    depth_usd: float = 0.0
    book_age_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "price": self.price,
            "long_score": round(self.long_score, 3),
            "short_score": round(self.short_score, 3),
            "regime": self.regime,
            "momentum": round(self.momentum, 2),
            "volume_ratio": round(self.volume_ratio, 2),
            "spread_bps": round(self.spread_bps, 2),
            "imbalance": round(self.imbalance, 3),
            "net_edge_bps": round(self.net_edge_bps, 2),
            "strategy": self.strategy,
            "decision": self.decision,
            "reject_reason": self.reject_reason,
            "depth_usd": round(self.depth_usd, 0),
            "book_age_ms": self.book_age_ms,
        }


@dataclass
class WorkerStats:
    evaluations: int = 0
    signals_seen: int = 0
    opportunities: int = 0
    entries: int = 0
    exits: int = 0
    rejects: dict[str, int] = field(default_factory=dict)
    last_evaluation_ms: int = 0
    started_ms: int = field(default_factory=now_ms)


class TradingWorker:
    """Owns every piece of live state.  One instance per worker container."""

    def __init__(self, *, settings: Settings | None = None, store: RuntimeStore | None = None) -> None:
        self.s = settings or get_settings()
        assert_paper_mode(self.s)

        self.books = BookManager(settings=self.s)
        self.rest = BinanceUSRest(settings=self.s)
        self.feed = MarketDataFeed(books=self.books, settings=self.s)
        self.universe_selector = UniverseSelector(settings=self.s)
        self.features = FeatureEngine(settings=self.s)
        self.regime = RegimeClassifier(settings=self.s)
        self.strategies = get_strategies()
        self.ranker = OpportunityRanker(settings=self.s, expectancy=self._expectancy)
        self.risk = RiskEngine(settings=self.s)
        self.exits = ExitEngine(settings=self.s)
        self.sim = PaperSimulator(settings=self.s)
        self.store = store or RuntimeStore(settings=self.s)

        self.ledgers: dict[str, Ledger] = {
            "base": Ledger("base", self.s.risk.starting_equity, self.s.simulator_version),
        }
        for eq in self.s.risk.comparison_ledgers:
            name = f"l{int(eq)}"
            self.ledgers[name] = Ledger(name, eq, self.s.simulator_version)

        self.symbol_info: dict[str, SymbolInfo] = {}
        self.universe: list[UniverseEntry] = []
        self.scanner: list[ScannerRow] = []
        self.stats = WorkerStats()
        self._expectancy_cache: dict[tuple[str, str, str], tuple[float, int]] = {}
        self._expectancy_refreshed_ms = 0
        self._cooldown_until_ms = 0
        self._universe_refreshed_ms = 0
        self._stop = asyncio.Event()
        self._db_ok = False

    # ------------------------------------------------------------------ #
    # Adaptive layer (§22): rolling, evidence-based, deliberately dumb.
    # ------------------------------------------------------------------ #

    def _expectancy(self, strategy: str, symbol: str, regime: str) -> tuple[float, int]:
        return self._expectancy_cache.get((strategy, symbol, regime), (0.0, 0))

    def _refresh_expectancy(self) -> None:
        try:
            from app.db import get_session
            from app.repo import rolling_expectancy

            with get_session() as session:
                self._expectancy_cache = rolling_expectancy(
                    session, "base", window_ms=7 * 24 * 3600 * 1000, min_samples=10
                )
            self._db_ok = True
        except Exception as exc:  # the bot must keep trading without the DB
            log.warning("expectancy refresh failed: %s", exc)
            self._db_ok = False
        self._expectancy_refreshed_ms = now_ms()

    # ------------------------------------------------------------------ #
    # Bootstrap
    # ------------------------------------------------------------------ #

    async def bootstrap(self) -> None:
        log.info(PAPER_ONLY_BANNER)
        try:
            from app.db import init_db

            init_db()
            self._db_ok = True
        except Exception as exc:
            log.warning("database not ready, continuing without persistence: %s", exc)

        self.symbol_info = await self.rest.exchange_info()
        log.info("exchange metadata: %d symbols", len(self.symbol_info))
        await self.refresh_universe()
        self._refresh_expectancy()

    async def refresh_universe(self) -> None:
        try:
            entries = await self.universe_selector.refresh(self.rest)
        except Exception as exc:
            log.warning("universe refresh failed: %s", exc)
            return
        if not entries:
            log.warning("universe refresh returned nothing; keeping previous set")
            return
        self.universe = entries
        hot = [e.symbol for e in entries if e.tier == "hot"]
        warm = [e.symbol for e in entries if e.tier != "hot"]
        self.feed.set_universe(hot, warm)
        await self._seed_bars([e.symbol for e in entries])
        self._universe_refreshed_ms = now_ms()
        log.info("universe: %d symbols (hot=%s)", len(entries), ",".join(hot))
        self._persist(lambda session, repo: repo.upsert_markets(session, entries))

    async def _seed_bars(self, symbols: list[str]) -> None:
        """Warm the feature engine from REST history.

        A cold feed needs ~30 minutes of live stream before any regime can be
        classified, so without this every deploy and every restart silently
        costs half an hour of trading. Failures are non-fatal: the stream will
        fill the history in eventually.
        """
        for symbol in symbols:
            if not hasattr(self.feed, "seed_bars"):
                return
            try:
                bars = await self.rest.klines(symbol, "1m", limit=240)
            except Exception as exc:
                log.warning("kline seed failed for %s: %s", symbol, exc)
                continue
            added = self.feed.seed_bars(symbol, bars)
            if added:
                log.info("seeded %d historical 1m bars for %s", added, symbol)

    # ------------------------------------------------------------------ #
    # Persistence helper — never let a DB problem stop trading.
    # ------------------------------------------------------------------ #

    def _persist(self, fn) -> None:
        try:
            from app import repo
            from app.db import get_session

            with get_session() as session:
                fn(session, repo)
            self._db_ok = True
        except Exception as exc:
            self._db_ok = False
            log.debug("persist skipped: %s", exc)

    # ------------------------------------------------------------------ #
    # Snapshot assembly
    # ------------------------------------------------------------------ #

    def _snapshot(self, entry: UniverseEntry, ts_ms: int) -> MarketSnapshot | None:
        book = self.books.get(entry.symbol)
        if book is None:
            return None
        # CLOSED bars only. The feed will happily hand over the in-progress
        # candle, but the feature engine folds each bar into permanent state
        # keyed by close time: a partial bar would be absorbed as if it were
        # final (so volume_ratio reads ~0 all minute, killing every
        # volume-confirmation strategy) and the real closed bar for that minute
        # would then be skipped as already seen. Intra-minute responsiveness
        # comes from the book and the trade tape, which are live regardless.
        bars = self._closed_bars(entry.symbol)
        trades = self.feed.recent_trades(entry.symbol, limit=300)
        return MarketSnapshot(
            symbol=entry.symbol,
            ts_ms=ts_ms,
            book=book,
            bars_1m=bars,
            recent_trades=trades,
            info=entry.info,
            quote_volume_24h=entry.quote_volume_24h,
            trades_24h=entry.trades_24h,
        )

    def _closed_bars(self, symbol: str, limit: int = 240):
        try:
            return self.feed.bars(symbol, limit=limit, include_partial=False)
        except TypeError:
            # A feed without the keyword (test doubles) — take what it gives.
            return self.feed.bars(symbol, limit=limit)

    # ------------------------------------------------------------------ #
    # One evaluation pass
    # ------------------------------------------------------------------ #

    def _risk_state(self, ledger: Ledger, ts_ms: int) -> RiskState:
        day_start = ts_ms - (ts_ms % 86_400_000)
        week_start = ts_ms - 7 * 86_400_000
        health = self.feed.health()
        return RiskState(
            equity=ledger.equity,
            open_positions=list(ledger.open_positions.values()),
            realized_pnl_today=ledger.realized_since(day_start),
            realized_pnl_week=ledger.realized_since(week_start),
            loss_streak=ledger.loss_streak(),
            cooldown_until_ms=self._cooldown_until_ms,
            kill_switch=self.store.kill_switch(),
            data_healthy=health.connected and not health.stale_symbols,
            ts_ms=ts_ms,
        )

    def evaluate_once(self, ts_ms: int | None = None) -> list[Opportunity]:
        """Scan every universe symbol and return the ranked, qualifying set.

        Split out from the loop so tests can drive a single pass deterministically.
        """
        ts_ms = ts_ms or now_ms()
        rows: list[ScannerRow] = []
        opportunities: list[Opportunity] = []
        stats_rows: list[dict[str, Any]] = []

        for entry in self.universe:
            snap = self._snapshot(entry, ts_ms)
            if snap is None:
                rows.append(
                    ScannerRow(
                        symbol=entry.symbol, price=0.0, long_score=0.0, short_score=0.0,
                        regime=Regime.UNKNOWN.value, momentum=0.0, volume_ratio=0.0,
                        spread_bps=0.0, imbalance=0.0, net_edge_bps=0.0, strategy=None,
                        decision=Decision.NO_TRADE.value, reject_reason="DATA_FAILURE",
                    )
                )
                continue

            feats = self.features.compute(snap)
            regime = self.regime.classify(feats)
            best, long_score, short_score, symbol_opps = self._evaluate_symbol(
                feats, regime, snap, ts_ms
            )
            opportunities.extend(symbol_opps)

            decision, reject = self._row_decision(best, feats, regime)
            rows.append(
                ScannerRow(
                    symbol=entry.symbol,
                    price=feats.price,
                    long_score=long_score,
                    short_score=short_score,
                    regime=regime.regime.value,
                    momentum=feats.ret_5m_bps,
                    volume_ratio=feats.volume_ratio,
                    spread_bps=feats.spread_bps,
                    imbalance=feats.imbalance,
                    net_edge_bps=best.net_edge_bps if best else 0.0,
                    strategy=best.signal.strategy if best else None,
                    decision=decision,
                    reject_reason=reject,
                    depth_usd=feats.depth_bid_usd + feats.depth_ask_usd,
                    book_age_ms=feats.book_age_ms,
                )
            )
            stats_rows.append(
                {
                    "symbol": entry.symbol, "ts_ms": ts_ms, "price": feats.price,
                    "spread_bps": feats.spread_bps, "depth_bid_usd": feats.depth_bid_usd,
                    "depth_ask_usd": feats.depth_ask_usd, "imbalance": feats.imbalance,
                    "atr_bps": feats.atr_bps, "realized_vol_bps": feats.realized_vol_bps,
                    "volume_ratio": feats.volume_ratio,
                    "quote_volume_24h": feats.quote_volume_24h,
                    "trades_24h": feats.trades_24h, "regime": regime.regime.value,
                }
            )

        self.scanner = rows
        self.stats.evaluations += 1
        self.stats.last_evaluation_ms = ts_ms
        self.stats.opportunities += len(opportunities)
        if stats_rows:
            self._persist(lambda s, r: r.record_market_stats(s, stats_rows))
        return self.ranker.rank(opportunities)

    def _evaluate_symbol(
        self, feats: Features, regime: RegimeState, snap: MarketSnapshot, ts_ms: int
    ) -> tuple[Opportunity | None, float, float, list[Opportunity]]:
        long_score = short_score = 0.0
        opps: list[Opportunity] = []
        taker = self.s.fees.taker_bps(feats.symbol)
        maker = self.s.fees.maker_bps(feats.symbol)

        for strategy in self.strategies:
            if regime.regime not in strategy.allowed_regimes:
                continue
            try:
                signal = strategy.evaluate(feats, regime)
            except Exception as exc:
                log.warning("strategy %s failed on %s: %s", strategy.name, feats.symbol, exc)
                continue
            if signal is None:
                continue
            self.stats.signals_seen += 1
            if signal.side is Side.LONG:
                long_score = max(long_score, signal.score)
            else:
                short_score = max(short_score, signal.score)

            notional = self._probe_notional()
            slip = self.sim.estimate_slippage_bps(signal.side, notional, snap.book)
            opp = self.ranker.build(
                signal, feats, regime, taker_bps=taker, maker_bps=maker, slippage_bps=slip
            )
            opps.append(opp)

        qualified = [o for o in opps if o.signal.side is Side.LONG and self.ranker.qualifies(o)]
        best = max(qualified, key=lambda o: o.rank_score) if qualified else None
        return best, long_score, short_score, opps

    def _probe_notional(self) -> float:
        """Notional used to pre-estimate slippage before sizing is known."""
        base = self.ledgers["base"]
        return max(
            self.s.risk.min_notional_usd,
            base.equity * self.s.risk.max_position_pct_equity / 100.0 * 0.5,
        )

    def _row_decision(
        self, best: Opportunity | None, feats: Features, regime: RegimeState
    ) -> tuple[str, str | None]:
        if regime.regime is Regime.ABNORMAL:
            return Decision.NO_TRADE.value, "ABNORMAL_VOLATILITY"
        if best is None:
            return Decision.NO_TRADE.value, None
        state = self._risk_state(self.ledgers["base"], now_ms())
        blocked = self.risk.blocked_reason(state)
        if blocked is not None:
            return Decision.WATCH.value, blocked.value
        return Decision.BUY.value, None

    # ------------------------------------------------------------------ #
    # Entries
    # ------------------------------------------------------------------ #

    def try_enter(self, ranked: list[Opportunity], ts_ms: int) -> int:
        """Open at most one new position per ledger per pass.

        Deliberate: one decision at a time keeps sizing honest against the
        equity that actually exists, and stops a single noisy tick from filling
        the whole book.
        """
        opened = 0
        decisions: list[dict[str, Any]] = []
        for ledger in self.ledgers.values():
            state = self._risk_state(ledger, ts_ms)
            if self.risk.blocked_reason(state) is not None:
                continue
            for opp in ranked:
                book = self.books.get(opp.symbol)
                info = self.symbol_info.get(opp.symbol)
                if book is None or info is None:
                    continue
                verdict = self.risk.evaluate(opp, state, info=info, book=book)
                if not verdict.approved:
                    self.stats.rejects[
                        verdict.reject_reason.value if verdict.reject_reason else "UNKNOWN"
                    ] = self.stats.rejects.get(
                        verdict.reject_reason.value if verdict.reject_reason else "UNKNOWN", 0
                    ) + 1
                    if ledger.name == "base":
                        decisions.append(self._decision_row(opp, ts_ms, False, verdict.reject_reason))
                    continue

                pos = self._open_position(ledger, opp, verdict, book, info, ts_ms)
                if pos is not None:
                    opened += 1
                    if ledger.name == "base":
                        decisions.append(self._decision_row(opp, ts_ms, True, None))
                break  # one entry per ledger per pass

        if decisions:
            self._persist(lambda s, r: r.record_decisions(s, decisions))
        return opened

    def _decision_row(self, opp: Opportunity, ts_ms: int, chosen: bool, reject) -> dict[str, Any]:
        return {
            "ts_ms": ts_ms, "symbol": opp.symbol, "strategy": opp.signal.strategy,
            "decision": Decision.BUY.value if chosen else Decision.NO_TRADE.value,
            "regime": opp.regime.regime.value, "gross_edge_bps": opp.gross_edge_bps,
            "fee_bps": opp.fee_bps, "spread_cost_bps": opp.spread_cost_bps,
            "slippage_bps": opp.slippage_bps, "net_edge_bps": opp.net_edge_bps,
            "rank_score": opp.rank_score, "chosen": chosen,
            "reject_reason": reject.value if reject else None,
            "notes": opp.notes,
        }

    def _open_position(
        self, ledger: Ledger, opp: Opportunity, verdict, book, info: SymbolInfo, ts_ms: int
    ) -> Position | None:
        req = OrderRequest(
            symbol=opp.symbol, side=opp.signal.side, order_type=OrderType.MARKET,
            qty=verdict.qty, client_id=str(uuid.uuid4()), ts_ms=ts_ms,
        )
        result = self.sim.execute(
            req, book, info, trades=self.feed.recent_trades(opp.symbol, limit=50), ts_ms=ts_ms
        )
        if not result.accepted or result.filled_qty <= 0:
            return None

        pos = Position(
            id=f"{ledger.name}-{uuid.uuid4().hex[:12]}",
            symbol=opp.symbol,
            side=opp.signal.side,
            strategy=opp.signal.strategy,
            qty=result.filled_qty,
            entry_price=result.avg_price,
            entry_ms=ts_ms,
            notional_usd=result.notional,
            risk_usd=verdict.risk_usd * (result.filled_qty / max(verdict.qty, 1e-12)),
            take_profit=verdict.take_profit,
            stop_loss=verdict.stop_loss,
            entry_fees_usd=result.fees_usd,
            entry_slippage_bps=result.slippage_bps,
            ledger=ledger.name,
            regime=opp.regime.regime.value,
            target_bps=opp.signal.target_bps,
            stop_bps=opp.signal.stop_bps,
            expected_net_edge_bps=opp.net_edge_bps,
            best_price=result.avg_price,
            last_price=result.avg_price,
            time_stop_ms=ts_ms + self.s.exits.time_stop_seconds * 1000,
        )
        ledger.open(pos, result)
        self.stats.entries += 1
        log.info(
            "ENTRY %s %s %s qty=%.6f @ %.6f notional=$%.2f risk=$%.2f edge=%.1fbps",
            ledger.name, opp.symbol, opp.signal.strategy, pos.qty, pos.entry_price,
            pos.notional_usd, pos.risk_usd, opp.net_edge_bps,
        )
        self._persist(
            lambda s, r: (
                r.record_order(s, result, position_id=pos.id, symbol=pos.symbol,
                               side=pos.side.value, leg="ENTRY", ts_ms=ts_ms),
                r.open_position(s, pos),
            )
        )
        return pos

    # ------------------------------------------------------------------ #
    # Position monitoring / exits
    # ------------------------------------------------------------------ #

    def monitor_once(self, ts_ms: int | None = None) -> int:
        ts_ms = ts_ms or now_ms()
        closed = 0
        for ledger in self.ledgers.values():
            prices = {}
            for pos in list(ledger.open_positions.values()):
                book = self.books.get(pos.symbol)
                if book is None or not book.is_valid():
                    continue
                exit_price = book.best_bid if pos.side is Side.LONG else book.best_ask
                prices[pos.symbol] = exit_price
                pos.last_price = exit_price
                self.exits.update_trailing(pos, exit_price)

                reason = self.exits.check(pos, book, None, ts_ms)
                if reason is None:
                    continue
                if self._close_position(ledger, pos, book, reason, ts_ms):
                    closed += 1
            if prices:
                ledger.mark(prices, ts_ms)
        return closed

    def _close_position(self, ledger: Ledger, pos: Position, book, reason: ExitReason, ts_ms: int) -> bool:
        info = self.symbol_info.get(pos.symbol)
        if info is None:
            return False
        exit_side = Side.SHORT if pos.side is Side.LONG else Side.LONG
        req = OrderRequest(
            symbol=pos.symbol, side=exit_side, order_type=OrderType.MARKET,
            qty=pos.qty, client_id=str(uuid.uuid4()), reduce_only=True, ts_ms=ts_ms,
        )
        result = self.sim.execute(
            req, book, info, trades=self.feed.recent_trades(pos.symbol, limit=50), ts_ms=ts_ms
        )
        if not result.accepted or result.filled_qty <= 0:
            # A market we cannot exit is a risk event, not a silent no-op.
            self._persist(
                lambda s, r: r.log_risk_event(
                    s, "EXIT_FAILED", symbol=pos.symbol, strategy=pos.strategy,
                    reason=result.reject_reason,
                )
            )
            return False

        trade = ledger.close(pos, result, reason, ts_ms)
        self.stats.exits += 1
        log.info(
            "EXIT  %s %s %s net=$%.4f fees=$%.4f reason=%s hold=%.0fs",
            ledger.name, trade.symbol, trade.strategy, trade.net_pnl_usd,
            trade.fees_usd, reason.value, trade.holding_seconds,
        )
        if ledger.name == "base":
            self._apply_cooldown(ledger, ts_ms)
        self._persist(
            lambda s, r: (
                r.record_order(s, result, position_id=pos.id, symbol=pos.symbol,
                               side=exit_side.value, leg="EXIT", ts_ms=ts_ms),
                r.close_position(s, trade),
            )
        )
        return True

    def _apply_cooldown(self, ledger: Ledger, ts_ms: int) -> None:
        if ledger.loss_streak() >= self.s.risk.loss_streak_len:
            self._cooldown_until_ms = ts_ms + self.s.risk.loss_streak_cooldown_minutes * 60_000
            log.warning(
                "loss streak %d — pausing new entries for %d minutes",
                ledger.loss_streak(), self.s.risk.loss_streak_cooldown_minutes,
            )
            self._persist(
                lambda s, r: r.log_risk_event(
                    s, "LOSS_STREAK_COOLDOWN", streak=ledger.loss_streak(),
                    until_ms=self._cooldown_until_ms,
                )
            )

    # ------------------------------------------------------------------ #
    # Dashboard payload
    # ------------------------------------------------------------------ #

    def dashboard_payload(self, ledger_name: str = "base") -> dict[str, Any]:
        ledger = self.ledgers.get(ledger_name) or self.ledgers["base"]
        ts = now_ms()
        day_start = ts - (ts % 86_400_000)
        stats = ledger.stats()
        health = self.feed.health()

        positions = []
        for pos in ledger.open_positions.values():
            price = pos.last_price or pos.entry_price
            positions.append(
                {
                    "id": pos.id, "symbol": pos.symbol, "side": pos.side.value,
                    "entry_price": pos.entry_price, "current_price": price,
                    "notional_usd": round(pos.notional_usd, 2),
                    "risk_usd": round(pos.risk_usd, 2),
                    "take_profit": pos.take_profit, "stop_loss": pos.stop_loss,
                    "unrealized_usd": round(pos.unrealized_usd(price), 4),
                    "fees_usd": round(pos.entry_fees_usd, 4),
                    "holding_seconds": round(pos.holding_seconds(ts), 0),
                    "strategy": pos.strategy,
                }
            )

        trades = [
            {
                "id": t.id, "symbol": t.symbol, "strategy": t.strategy,
                "entry_ms": t.entry_ms, "exit_ms": t.exit_ms,
                "entry_price": t.entry_price, "exit_price": t.exit_price,
                "gross_pnl_usd": round(t.gross_pnl_usd, 4),
                "fees_usd": round(t.fees_usd, 4),
                "slippage_usd": round(t.slippage_usd, 4),
                "net_pnl_usd": round(t.net_pnl_usd, 4),
                "exit_reason": t.exit_reason.value,
            }
            for t in ledger.closed_trades[-200:][::-1]
        ]

        strategies = []
        for name in sorted({t.strategy for t in ledger.closed_trades}):
            st = ledger.stats(strategy=name)
            strategies.append(
                {
                    "strategy": name, "trades": st.trades, "win_rate": round(st.win_rate, 4),
                    "avg_win": round(st.avg_win, 4), "avg_loss": round(st.avg_loss, 4),
                    "profit_factor": round(st.profit_factor, 3),
                    "expectancy": round(st.expectancy, 4), "fees": round(st.fees, 4),
                    "net_pnl": round(st.net_pnl, 4), "max_drawdown": round(st.max_drawdown, 4),
                    "avg_holding_seconds": round(st.avg_holding_seconds, 0),
                    "capital_turnover": round(st.capital_turnover, 3),
                }
            )

        points = ledger.equity_points()
        return {
            "mode": self.s.mode,
            "live_orders_enabled": False,
            "simulator_version": self.s.simulator_version,
            "exchange": self.s.exchange.name,
            "generated_ms": ts,
            "header": {
                "starting_equity": ledger.starting_equity,
                "data_connected": health.connected,
            },
            "metrics": {
                "equity": round(ledger.equity, 4),
                "cash": round(ledger.cash, 4),
                "pnl_today": round(ledger.realized_since(day_start), 4),
                "pnl_total": round(ledger.realized_pnl + ledger.unrealized_pnl, 4),
                "open_positions": len(ledger.open_positions),
                "trades": stats.trades,
                "win_rate": round(stats.win_rate, 4),
                "profit_factor": round(stats.profit_factor, 3),
                "expectancy": round(stats.expectancy, 4),
                "max_drawdown": round(stats.max_drawdown, 4),
                "capital_turnover": round(stats.capital_turnover, 3),
                "avg_holding_seconds": round(stats.avg_holding_seconds, 0),
            },
            "scanner": [r.as_dict() for r in self.scanner],
            "positions": positions,
            "trades": trades,
            "strategies": strategies,
            "equity_curve": {
                "points": [[p[0], round(p[1], 4)] for p in points[-2000:]],
                "realized": [],
                "version_boundaries": [[self.stats.started_ms, self.s.simulator_version]],
            },
            "health": {
                "ws_connected": health.connected,
                "rest_ok": True,
                "book_age_ms": max(
                    (b.age_ms(ts) for b in (self.books.get(e.symbol) for e in self.universe) if b),
                    default=0,
                ),
                "updates_per_sec": round(health.updates_per_sec, 1),
                "reconnects": health.reconnects,
                "dropped": health.dropped,
                "worker_cpu_pct": _cpu_pct(),
                "worker_mem_mb": _mem_mb(),
                "db_ok": self._db_ok,
                "last_evaluation_ms": self.stats.last_evaluation_ms,
                "mode": self.s.mode,
                "desynced": health.desynced_symbols,
                "stale": health.stale_symbols,
                "evaluations": self.stats.evaluations,
                "entries": self.stats.entries,
                "exits": self.stats.exits,
                "rejects": self.stats.rejects,
            },
            "ledgers": [
                {
                    "name": n, "label": LEDGER_LABELS.get(n, n),
                    "starting_equity": lg.starting_equity, "equity": round(lg.equity, 2),
                }
                for n, lg in self.ledgers.items()
            ],
        }

    def publish(self) -> None:
        for name in self.ledgers:
            self.store.publish_dashboard(name, self.dashboard_payload(name))
        self.store.beat()

    # ------------------------------------------------------------------ #
    # Loops
    # ------------------------------------------------------------------ #

    async def _eval_loop(self) -> None:
        interval = self.s.worker.evaluate_interval_ms / 1000.0
        while not self._stop.is_set():
            started = time.perf_counter()
            try:
                ts = now_ms()
                ranked = self.evaluate_once(ts)
                self.try_enter(ranked, ts)
                self.publish()
            except Exception:
                log.exception("evaluation pass failed")
            elapsed = time.perf_counter() - started
            # Never busy-spin: if a pass is slow, back off instead of stacking.
            await self._sleep(max(interval, elapsed * 1.5) - elapsed)

    async def _monitor_loop(self) -> None:
        interval = self.s.worker.position_monitor_interval_ms / 1000.0
        while not self._stop.is_set():
            try:
                self.monitor_once()
            except Exception:
                log.exception("position monitor failed")
            await self._sleep(interval)

    async def _housekeeping_loop(self) -> None:
        while not self._stop.is_set():
            await self._sleep(self.s.worker.snapshot_interval_seconds)
            try:
                ts = now_ms()
                for name, ledger in self.ledgers.items():
                    self._persist(
                        lambda s, r, n=name, lg=ledger: r.record_equity(
                            s, n, cash=lg.cash, equity=lg.equity,
                            realized=lg.realized_pnl, unrealized=lg.unrealized_pnl,
                            exposure=lg.exposure_usd, open_positions=len(lg.open_positions),
                            ts_ms=ts, simulator_version=self.s.simulator_version,
                        )
                    )
                if ts - self._expectancy_refreshed_ms > 300_000:
                    self._refresh_expectancy()
                if ts - self._universe_refreshed_ms > self.s.universe.refresh_seconds * 1000:
                    await self.refresh_universe()
                if ts % 3_600_000 < self.s.worker.snapshot_interval_seconds * 1000:
                    self._persist(lambda s, r: r.apply_retention(s, now_ms=ts))
            except Exception:
                log.exception("housekeeping failed")

    async def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    async def run(self) -> None:
        await self.bootstrap()
        tasks = [
            asyncio.create_task(self.feed.run(self._stop), name="feed"),
            asyncio.create_task(self._eval_loop(), name="eval"),
            asyncio.create_task(self._monitor_loop(), name="monitor"),
            asyncio.create_task(self._housekeeping_loop(), name="housekeeping"),
        ]
        try:
            await self._stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.rest.close()

    def stop(self) -> None:
        self._stop.set()


def _cpu_pct() -> float:
    try:
        import psutil

        return round(psutil.Process(os.getpid()).cpu_percent(interval=None), 1)
    except Exception:
        return 0.0


def _mem_mb() -> float:
    try:
        import psutil

        return round(psutil.Process(os.getpid()).memory_info().rss / 1e6, 1)
    except Exception:
        return 0.0


async def amain() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    worker = TradingWorker()
    loop = asyncio.get_running_loop()
    for sig in (os_signal.SIGTERM, os_signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.stop)
    await worker.run()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
