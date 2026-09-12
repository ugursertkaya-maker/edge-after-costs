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
import json
import logging
import os
import signal as os_signal
import time
import uuid
from pathlib import Path
from dataclasses import dataclass, field, replace
from typing import Any

from app.config import Settings, get_settings
from app.exchange.book import BookManager
from app.exchange.rest import BinanceUSRest
from app.exchange.ws import MarketDataFeed
from app.exits import CheckpointVerdict, ExitEngine
from app.features import FeatureEngine
from app import holding
from app.portfolio import PortfolioEngine
from app.queue import CapitalRecycler
from app.profit import NetTargetEngine
from app.ranker import OpportunityRanker
from app.recorder import TapeRecorder
from app.research.champion import ChampionStore
from app.research.funnel import STAGES as FUNNEL_STAGES, FunnelReport
from app.regime import RegimeClassifier
from app.risk import RiskEngine, RiskState
from app.runtime import RuntimeStore
from app.safety import PAPER_ONLY_BANNER, assert_paper_mode
from app.sim.ledger import Ledger
from app.sim.simulator import PaperSimulator
from app.strategies import get_strategies
from app.types import (
    Allocation,
    CostBreakdown,
    Decision,
    ExitReason,
    Features,
    MarketSnapshot,
    Opportunity,
    OrderRequest,
    OrderType,
    NetTargets,
    Position,
    PortfolioState,
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
    maker_fills: int = 0
    maker_misses: int = 0
    checkpoints: int = 0
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
        self.portfolio = PortfolioEngine(settings=self.s)
        # Close -> release -> rescan, measured end to end. ``rescan`` is
        # ``evaluate_once`` itself, so a close re-ranks in the same call rather
        # than waiting out an eval interval.
        self.recycler = CapitalRecycler(
            portfolio=self.portfolio,
            rescan=self.evaluate_once,
            emit=lambda kind, detail: self._persist(
                lambda s, r: r.log_risk_event(s, kind, **detail)
            ),
        )
        self.profit = NetTargetEngine(settings=self.s)
        self.risk = RiskEngine(settings=self.s, portfolio=self.portfolio)
        self.exits = ExitEngine(settings=self.s)
        self.sim = PaperSimulator(settings=self.s)
        self.store = store or RuntimeStore(settings=self.s)
        self.recorder = TapeRecorder(
            self.s.recorder.directory,
            enabled=self.s.recorder.enabled,
            retention_hours=self.s.recorder.retention_hours,
            roll_seconds=self.s.recorder.roll_seconds,
        )
        self._install_recorder()
        self.champions = ChampionStore(self.s.research.directory)
        self.champion = self.champions.load()
        if self.champion is not None:
            # Live P&L is split on the simulator version, so a promoted
            # champion must run under its own version from the first tick.
            self.s = replace(self.s, simulator_version=self.champion.simulator_version)
            log.info("running champion: %s", self.champion.describe())

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
        self._features_cache: dict[str, Features] = {}
        # The funnel accumulates across evaluations rather than describing the
        # current instant. "Candidate counts by symbol" is a count over time;
        # a snapshot of one 1-second pass answers a different question and
        # makes a symbol that qualifies once an hour look identical to one that
        # never qualifies at all.
        self._funnel = FunnelReport()
        # Phase 6 §17. Binance.US spot cannot short, so a short signal is
        # refused at the risk gate -- but it is still a prediction the system
        # made, and discarding it silently throws away exactly the information
        # needed to know how much edge lives on the side we cannot trade.
        # Counted per strategy, with the edge it claimed, never executed.
        self._unexecutable_shorts: dict[str, dict[str, float]] = {}
        self._ranked: list[Opportunity] = []
        self._stop = asyncio.Event()
        self._db_ok = False

    def _install_recorder(self) -> None:
        """Tap the feed's ingest point without touching the feed itself.

        Wrapping rather than editing keeps the recorder strictly additive: if it
        is disabled, or if the feed is a test double without ``ingest_message``,
        nothing changes on the hot path.
        """
        if not self.recorder.enabled:
            return
        inner = getattr(self.feed, "ingest_message", None)
        if not callable(inner):
            return

        def recording_ingest(msg, ts_ms, _inner=inner):
            self.recorder.record(msg, ts_ms)
            return _inner(msg, ts_ms)

        self.feed.ingest_message = recording_ingest  # type: ignore[method-assign]

        # Depth diffs alone cannot rebuild an absolute book — they are deltas
        # against a REST snapshot that arrives outside the stream. Recording the
        # diffs without the snapshots would produce a tape nobody can replay, so
        # tap the snapshot path too and write it as a synthetic stream event.
        snapshot = getattr(self.books, "apply_snapshot", None)
        if callable(snapshot):

            def recording_snapshot(symbol, last_update_id, bids, asks, ts_ms,
                                   _inner=snapshot):
                self.recorder.record(
                    {
                        "e": "__snapshot",
                        "s": symbol,
                        "lastUpdateId": last_update_id,
                        "b": [[p, q] for p, q in bids],
                        "a": [[p, q] for p, q in asks],
                        "E": ts_ms,
                    },
                    ts_ms,
                )
                return _inner(symbol, last_update_id, bids, asks, ts_ms)

            self.books.apply_snapshot = recording_snapshot  # type: ignore[method-assign]

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
        # The tier path, not ``refresh()``.  Phase 5 built HOT/WARM/COLD tiering
        # over the whole venue, tested it, and never connected it: the worker
        # kept calling ``refresh()``, which ranks on raw ``score`` (liquidity
        # first), truncates at ``max_symbols`` and applies no capacity gate.
        # That is why ADAUSD sat in HOT for a whole phase and never traded --
        # its flow absorbs $5.61 a minute against a $10 minimum order, and
        # nothing in the old path could say so.  ``refresh_tiers`` ranks on
        # ``net_score`` (cost first) and drops pairs we cannot trade at any size.
        try:
            entries = await self.universe_selector.refresh_tiers(self.rest)
        except Exception as exc:
            log.warning("tiered universe refresh failed: %s", exc)
            entries = []
        if not entries:
            # Degrade to the old path rather than going blind: a venue hiccup
            # during a depth probe must not empty the universe.
            try:
                entries = await self.universe_selector.refresh(self.rest)
            except Exception as exc:
                log.warning("universe refresh failed: %s", exc)
                return
        if not entries:
            log.warning("universe refresh returned nothing; keeping previous set")
            return
        self.universe = entries
        # COLD is a watchlist, not a subscription. The old `tier != "hot"` put
        # every cold pair on the websocket, which is exactly the CPU the tiering
        # exists to avoid on a 2-vCPU host.
        try:
            from app.universe import symbols_by_tier

            tiers = symbols_by_tier(entries)
            hot, warm = tiers["hot"], tiers["warm"]
        except Exception:
            hot = [e.symbol for e in entries if e.tier == "hot"]
            warm = [e.symbol for e in entries if e.tier == "warm"]
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
            portfolio=self.portfolio.state(
                ledger, ts_ms=ts_ms, bars_by_symbol=self._bars_map()
            ),
        )

    def _bars_map(self) -> dict[str, list]:
        """Closed bars per universe symbol — what correlation is measured on."""
        return {e.symbol: self._closed_bars(e.symbol) for e in self.universe}

    def _books_map(self) -> dict[str, Any]:
        out = {}
        for entry in self.universe:
            book = self.books.get(entry.symbol)
            if book is not None:
                out[entry.symbol] = book
        return out

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
            self._features_cache[entry.symbol] = feats
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
        self._ranked = self.ranker.rank(opportunities)
        self._record_funnel(rows)
        self.recycler.note_rescan(ts_ms)
        return self._ranked

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
        """Allocate capital across **every** qualifying opportunity.

        V1 opened at most one position per pass. V2 hands the whole ranked list
        to the portfolio engine, which walks it in order, sizes each candidate
        through the real risk engine, and checks it against a portfolio state
        that is updated as allocations accumulate — so the tenth candidate faces
        the book the first nine created, and the binding constraint is the risk
        and cash budget rather than an arbitrary position count.

        "Many allowed" is not "many taken": a list of twenty poor setups
        allocates nothing.
        """
        opened = 0
        decisions: list[dict[str, Any]] = []
        books = self._books_map()

        for ledger in self.ledgers.values():
            state = self._risk_state(ledger, ts_ms)
            blocked = self.risk.blocked_reason(state)
            if blocked is not None or state.portfolio is None:
                continue

            allocations = self.portfolio.allocate(
                ranked,
                state.portfolio,
                risk_engine=self.risk,
                infos=self.symbol_info,
                books=books,
            )
            chosen = {a.opportunity.symbol for a in allocations}

            for alloc in allocations:
                pos = self._open_position(ledger, alloc, ts_ms)
                if pos is not None:
                    opened += 1
                    if ledger.name == "base":
                        decisions.append(
                            self._decision_row(alloc.opportunity, ts_ms, True, None)
                        )

            if ledger.name == "base":
                for opp in ranked:
                    if opp.symbol in chosen:
                        continue
                    book = books.get(opp.symbol)
                    info = self.symbol_info.get(opp.symbol)
                    if book is None or info is None:
                        continue
                    verdict = self.risk.evaluate(opp, state, info=info, book=book)
                    if verdict.approved:
                        continue
                    name = (
                        verdict.reject_reason.value
                        if verdict.reject_reason
                        else "UNKNOWN"
                    )
                    self.stats.rejects[name] = self.stats.rejects.get(name, 0) + 1
                    if name == "NOT_SUPPORTED_SHORT":
                        self._note_unexecutable_short(opp)
                    decisions.append(
                        self._decision_row(opp, ts_ms, False, verdict.reject_reason)
                    )

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
        self, ledger: Ledger, alloc: Allocation, ts_ms: int
    ) -> Position | None:
        """Fill an allocation, maker-first where the execution style allows it.

        A post-only entry costs 0% maker on Binance.US and pays no spread, which
        is the single biggest cost line V1 measured. It also may not fill — and
        a missed maker order is never silently converted into a market order: it
        has to re-qualify on taker economics first.
        """
        opp = alloc.opportunity
        book = self.books.get(opp.symbol)
        info = self.symbol_info.get(opp.symbol)
        if book is None or info is None or not book.is_valid():
            return None

        result, is_maker = self._execute_entry(opp, alloc, book, info, ts_ms)
        if result is None or not result.accepted or result.filled_qty <= 0:
            return None

        filled_frac = result.filled_qty / max(alloc.qty, 1e-12)
        targets = self._build_targets(
            opp, alloc, result.avg_price, result.filled_qty, book, info,
            entry_is_maker=is_maker, entry_slippage_bps=result.slippage_bps,
        )

        pos = Position(
            id=f"{ledger.name}-{uuid.uuid4().hex[:12]}",
            symbol=opp.symbol,
            side=opp.signal.side,
            strategy=opp.signal.strategy,
            qty=result.filled_qty,
            entry_price=result.avg_price,
            entry_ms=ts_ms,
            notional_usd=result.notional,
            risk_usd=alloc.risk_usd * filled_frac,
            take_profit=targets.net_tp_price if targets else alloc.targets.net_tp_price,
            stop_loss=targets.stop_price if targets else alloc.targets.stop_price,
            entry_fees_usd=result.fees_usd,
            entry_slippage_bps=result.slippage_bps,
            ledger=ledger.name,
            regime=opp.regime.regime.value,
            target_bps=opp.signal.target_bps,
            stop_bps=opp.signal.stop_bps,
            expected_net_edge_bps=opp.net_edge_bps,
            best_price=result.avg_price,
            last_price=result.avg_price,
            targets=targets,
            break_even_price=targets.break_even_price if targets else 0.0,
            entry_is_maker=is_maker,
            checkpoint_ms=ts_ms + self.s.horizon.checkpoint_seconds * 1000,
            time_stop_ms=ts_ms + self.s.horizon.time_stop_seconds * 1000,
        )
        # Snapshot the trigger BEFORE the position is persisted.  ``check()``
        # self-arms on the first monitored tick, but that is ~500ms late and
        # after ``open_position`` has already written ``meta``, so the baseline
        # would not survive a restart -- and a decay baseline taken half a
        # second into the trade is a baseline of a slightly different market.
        holding.arm(
            pos,
            self._features_cache.get(opp.symbol),
            book=book,
            entry_score=float(getattr(opp.signal, "score", 0.0) or 0.0),
            ts_ms=ts_ms,
        )
        ledger.open(pos, result)
        self.stats.entries += 1
        if ledger.name == "base":
            # Closes close_to_entry_ms and claims the episode, so the same
            # impulse cannot be re-entered from the queue.
            self.recycler.note_entry(
                pos.symbol, ts_ms=ts_ms,
                episode_id=str(pos.meta.get("episode_id") or "") or None,
            )
        log.info(
            "ENTRY %s %s %s %s qty=%.6f @ %.6f notional=$%.2f risk=$%.2f "
            "net_tp=%.6f be=%.6f edge=%.1fbps",
            ledger.name, opp.symbol, opp.signal.strategy,
            "MAKER" if is_maker else "TAKER", pos.qty, pos.entry_price,
            pos.notional_usd, pos.risk_usd, pos.take_profit,
            pos.break_even_price, opp.net_edge_bps,
        )
        self._persist(
            lambda s, r: (
                r.record_order(s, result, position_id=pos.id, symbol=pos.symbol,
                               side=pos.side.value, leg="ENTRY", ts_ms=ts_ms),
                r.open_position(s, pos),
            )
        )
        return pos

    def _execute_entry(self, opp, alloc, book, info, ts_ms):
        """Try post-only first, fall back to taker only if it still qualifies."""
        trades = self.feed.recent_trades(opp.symbol, limit=50)
        style = self.s.execution_style

        if style.maker_first:
            limit_price = self._maker_price(opp.signal.side, book, info)
            if limit_price > 0:
                req = OrderRequest(
                    symbol=opp.symbol, side=opp.signal.side,
                    order_type=OrderType.LIMIT, qty=alloc.qty,
                    limit_price=limit_price, client_id=str(uuid.uuid4()), ts_ms=ts_ms,
                )
                res = self._sim_execute(req, book, info, trades, ts_ms, post_only=True)
                if res is not None and res.accepted and res.filled_qty > 0:
                    self.stats.maker_fills += 1
                    return res, True
                self.stats.maker_misses += 1
                if not style.allow_taker_fallback:
                    return None, False
                if not self._taker_still_qualifies(opp, alloc, book, info):
                    self.stats.rejects["TAKER_NOT_QUALIFIED"] = (
                        self.stats.rejects.get("TAKER_NOT_QUALIFIED", 0) + 1
                    )
                    return None, False

        req = OrderRequest(
            symbol=opp.symbol, side=opp.signal.side, order_type=OrderType.MARKET,
            qty=alloc.qty, client_id=str(uuid.uuid4()), ts_ms=ts_ms,
        )
        return self._sim_execute(req, book, info, trades, ts_ms), False

    def _sim_execute(self, req, book, info, trades, ts_ms, *, post_only: bool = False):
        try:
            if post_only:
                return self.sim.execute(
                    req, book, info, trades=trades, ts_ms=ts_ms, post_only=True
                )
            return self.sim.execute(req, book, info, trades=trades, ts_ms=ts_ms)
        except TypeError:
            # A simulator without post-only support: taker only.
            if post_only:
                return None
            return self.sim.execute(req, book, info, trades=trades, ts_ms=ts_ms)
        except Exception:
            log.exception("simulated execution failed for %s", req.symbol)
            return None

    def _maker_price(self, side: Side, book, info: SymbolInfo) -> float:
        """Rest inside our own side of the book, never crossing it."""
        ticks = max(0, self.s.execution_style.maker_offset_ticks)
        if side is Side.LONG:
            price = book.best_bid - ticks * info.tick_size
        else:
            price = book.best_ask + ticks * info.tick_size
        return info.round_price(price) if price > 0 else 0.0

    def _taker_still_qualifies(self, opp, alloc, book, info) -> bool:
        """A missed maker order may only cross if taker economics still work."""
        slip = self.sim.estimate_slippage_bps(
            opp.signal.side, alloc.notional_usd, book
        )
        entry = book.best_ask if opp.signal.side is Side.LONG else book.best_bid
        if entry <= 0:
            return False
        targets = self._build_targets(
            opp, alloc, entry, alloc.qty, book, info,
            entry_is_maker=False, entry_slippage_bps=slip,
        )
        if targets is None:
            return True
        return self.profit.qualifies(targets, notional_usd=alloc.notional_usd)

    def _build_targets(
        self, opp, alloc, entry_price, qty, book, info,
        *, entry_is_maker: bool, entry_slippage_bps: float,
    ) -> NetTargets | None:
        try:
            return self.profit.build(
                symbol=opp.symbol,
                side=opp.signal.side,
                entry_price=entry_price,
                notional_usd=entry_price * qty,
                qty=qty,
                book=book,
                info=info,
                features=opp.features,
                signal=opp.signal,
                entry_is_maker=entry_is_maker,
                exit_is_maker=False,
                slippage_bps_entry=entry_slippage_bps,
                slippage_bps_exit=entry_slippage_bps,
                stop_price=alloc.targets.stop_price if alloc.targets else None,
            )
        except Exception:
            log.exception("net target build failed for %s", opp.symbol)
            return alloc.targets

    # ------------------------------------------------------------------ #
    # Position monitoring / exits
    # ------------------------------------------------------------------ #

    def _live_score(self, pos: Position, feats: Features | None) -> float | None:
        """What the originating strategy scores this symbol *now*.

        Only consulted for strategies whose profile re-evaluates the entry
        score; for the slow ones entry scoring and position management are
        separate concerns and a drifting score is not a reason to exit.
        """
        if feats is None:
            return None
        profile = holding.profile_for(pos.strategy)
        if not profile.reeval_entry_score:
            return None
        regime = self.regime.classify(feats)
        for strat in self.strategies:
            if strat.name != pos.strategy:
                continue
            try:
                sig = strat.evaluate(feats, regime)
            except Exception:
                return None
            if sig is None or sig.side is not pos.side:
                # No signal, or a signal the other way, is a score of zero for
                # *this* position rather than an absence of information.
                return 0.0
            return float(getattr(sig, "score", 0.0) or 0.0)
        return None

    def _predicted_ev_bps(self, pos: Position, feats: Features | None) -> float | None:
        """The predictive engine's current EV for holding this position.

        ``None`` when there is no champion model or it declines to score, which
        is the common case: the engine fails closed by design and Phase 4 never
        promoted a champion.
        """
        engine = getattr(self, "predictor", None)
        if engine is None or feats is None:
            return None
        regime = self.regime.classify(feats)
        try:
            verdict = engine.evaluate(feats, regime, side=pos.side)
        except Exception:
            return None
        if verdict is None:
            return None
        ev = getattr(verdict, "ev_bps", None)
        return None if ev is None else float(ev)

    def monitor_once(self, ts_ms: int | None = None) -> int:
        """Mark, trail, checkpoint and exit every open position.

        Exit priority is unchanged and deliberate: a stop hit at minute two is
        honoured at minute two, and profit that arrives before the 25-minute
        checkpoint is taken rather than held for the checkpoint's sake.
        """
        ts_ms = ts_ms or now_ms()
        closed = 0
        for ledger in self.ledgers.values():
            prices = {}
            for pos in list(ledger.open_positions.values()):
                book = self.books.get(pos.symbol)
                if book is None or not book.is_valid():
                    continue
                exit_price = self.exits.exit_price(pos, book)
                prices[pos.symbol] = exit_price
                pos.last_price = exit_price
                self.exits.update_trailing(pos, exit_price)

                feats = self._features_cache.get(pos.symbol)
                # The live entry score and the model's current EV, so the
                # holding policy can tell "the setup is gone" from "the clock
                # ran out".  Both are None when unavailable rather than 0.0:
                # a missing measurement must not read as a maximally bearish
                # one and close the position.
                reason = self.exits.check(
                    pos, book, feats, ts_ms,
                    live_score=self._live_score(pos, feats),
                    predicted_ev_bps=self._predicted_ev_bps(pos, feats),
                )

                if reason is None and self._checkpoint_due(pos, ts_ms):
                    reason = self._run_checkpoint(pos, book, feats, ts_ms)

                if reason is None:
                    continue
                if self._close_position(ledger, pos, book, reason, ts_ms):
                    closed += 1
            if prices:
                ledger.mark(prices, ts_ms)
        return closed

    def _checkpoint_due(self, pos: Position, ts_ms: int) -> bool:
        return (
            not pos.checkpoint_done
            and pos.checkpoint_ms > 0
            and ts_ms >= pos.checkpoint_ms
        )

    def _run_checkpoint(self, pos: Position, book, feats, ts_ms) -> ExitReason | None:
        """Translate the 25-minute verdict into an action.

        KEEP and TRAIL_PROFIT leave the position open — the checkpoint is a
        reassessment, not a deadline.
        """
        try:
            verdict = self.exits.checkpoint(
                pos, book, feats, ts_ms,
                best_alternative_edge_bps=self._best_alternative_edge(pos.symbol),
            )
        except Exception:
            log.exception("checkpoint failed for %s", pos.symbol)
            return None

        pos.checkpoint_done = True
        self.stats.checkpoints += 1
        log.info(
            "CHECKPOINT %s %s -> %s (net %.4f after %.0fs)",
            pos.ledger, pos.symbol, verdict.value,
            self._position_net_usd(pos), pos.holding_seconds(ts_ms),
        )
        return {
            CheckpointVerdict.TAKE_PROFIT: ExitReason.TAKE_PROFIT,
            CheckpointVerdict.EXIT_WEAK: ExitReason.EXIT_WEAK,
            CheckpointVerdict.ROTATE_CAPITAL: ExitReason.ROTATE_CAPITAL,
        }.get(verdict)

    def _opportunity_board(self) -> list[dict[str, Any]]:
        """The ranked candidate table the allocator actually chooses from.

        Phase 6 §4.  Two things make this more than a prettier scanner:

        * **rejections are rows, not absences.**  A symbol that was evaluated
          and refused appears with its reason, so "why did we not trade XRP"
          is answered on the screen instead of inferred from silence.
        * **rank is over the whole board**, so BTC holding rank 3 is visible as
          rank 3.  Nothing here gives any symbol priority; the order is
          ``net_edge_bps`` and nothing else.
        """
        board: list[dict[str, Any]] = []
        ranked_symbols: set[str] = set()
        for i, opp in enumerate(self._ranked, start=1):
            ranked_symbols.add(opp.symbol)
            board.append({
                "rank": i,
                "symbol": opp.symbol,
                "strategy": opp.signal.strategy,
                "side": opp.signal.side.value,
                "net_edge_bps": round(opp.net_edge_bps, 3),
                "net_ev_pct": round(opp.net_edge_bps / 100.0, 4),
                "gross_edge_bps": round(opp.gross_edge_bps, 3),
                "cost_bps": round(
                    opp.fee_bps + opp.spread_cost_bps + opp.slippage_bps, 3
                ),
                "expected_hold_seconds": opp.expected_hold_seconds,
                "sample_size": opp.sample_size,
                # A ranked candidate is a BUY or a SELL; the scanner's own
                # vocabulary, so the board and the scanner agree.
                "decision": (Decision.BUY.value if opp.signal.side is Side.LONG
                             else Decision.SELL.value),
                "reject_reason": None,
            })
        # Everything the scanner saw and refused, in the same table.  Ranked
        # candidates are skipped rather than deduped by symbol: the same symbol
        # can legitimately have a qualifying long and a refused short.
        for row in self.scanner:
            if row.symbol in ranked_symbols:
                continue
            board.append({
                "rank": None,
                "symbol": row.symbol,
                "strategy": row.strategy,
                "side": None,
                "net_edge_bps": round(row.net_edge_bps, 3),
                "net_ev_pct": round(row.net_edge_bps / 100.0, 4),
                "gross_edge_bps": None,
                "cost_bps": None,
                "expected_hold_seconds": None,
                "sample_size": 0,
                "decision": row.decision,
                "reject_reason": row.reject_reason,
            })
        return board

    def _note_unexecutable_short(self, opp: Opportunity) -> None:
        """Record a short we predicted and cannot trade on spot (§17).

        Kept apart from the executed book on purpose: mixing them would let a
        side we never traded flatter or damn a P&L we actually realised. The
        edge recorded is what the ranker expected, not an outcome -- nothing
        here claims the trade would have won.
        """
        key = opp.signal.strategy
        row = self._unexecutable_shorts.setdefault(
            key,
            # ``best`` starts at -inf, not 0.0: seeding it at zero reports a
            # best of 0.0 for a strategy whose every short had negative edge,
            # which is a number nothing observed. Caught on the first live
            # sample -- four shorts averaging -19.6 bps, "best" reading 0.0.
            {"signals": 0.0, "sum_net_edge_bps": 0.0,
             "best_net_edge_bps": float("-inf")},
        )
        row["signals"] += 1.0
        row["sum_net_edge_bps"] += float(opp.net_edge_bps)
        row["best_net_edge_bps"] = max(
            row["best_net_edge_bps"], float(opp.net_edge_bps)
        )

    def _short_side_block(self) -> dict[str, Any]:
        """What the long-only constraint is costing us in observed signal."""
        out: dict[str, Any] = {"by_strategy": {}, "total_signals": 0}
        for strategy, row in sorted(self._unexecutable_shorts.items()):
            n = int(row["signals"])
            out["by_strategy"][strategy] = {
                "signals": n,
                "mean_net_edge_bps": round(row["sum_net_edge_bps"] / n, 3) if n else 0.0,
                "best_net_edge_bps": (
                    round(row["best_net_edge_bps"], 3)
                    if row["best_net_edge_bps"] > float("-inf") else None
                ),
                "executable": False,
                "note": "spot is long-only; recorded, never traded, never faked",
            }
            out["total_signals"] += n
        return out

    def _record_funnel(self, rows: list[ScannerRow]) -> None:
        """Fold one evaluation pass into the cumulative funnel."""
        report = self._funnel
        for entry in self.universe:
            f = report.for_symbol(entry.symbol)
            f.reached("discovered")
            f.reached("subscribed")
            if not entry.capacity_ok:
                f.dropped(
                    "candidate",
                    f"absorbs ${entry.capacity_max_usd:,.2f}/min, below the "
                    f"${entry.min_tradable_usd:,.2f} minimum order",
                )
        for row in rows:
            f = report.for_symbol(row.symbol)
            if row.reject_reason == "DATA_FAILURE":
                f.dropped("data_healthy", "no market snapshot")
                continue
            f.reached("data_healthy")
            f.reached("features")
            f.reached("strategy_evaluated")
            # Vacuously passed when there is no champion to consult: Phase 4
            # never promoted one, and naming it a wall would hide the real one.
            f.reached("model_evaluated")
            if row.strategy:
                f.reached("candidate")
            else:
                # "We looked and nothing fired" is a different fact from "we
                # never looked", and the funnel is worthless if it renders them
                # with the same words.
                f.dropped("candidate", "no strategy produced a signal")
            if row.reject_reason:
                f.dropped("ranked", row.reject_reason)
        for opp in self._ranked:
            # Every stage up to ``ranked`` precedes ranking in the pipeline, so
            # reaching it implies reaching them. Marking only ``ranked`` leaves
            # cost_gate..correlation_gate at zero behind a non-zero, which is
            # the monotonicity violation ``check()`` exists to catch.
            f = report.for_symbol(opp.symbol)
            for stage in FUNNEL_STAGES[: FUNNEL_STAGES.index("ranked") + 1]:
                f.reached(stage)

    def _symbol_funnel(self, ledger: Ledger) -> dict[str, Any]:
        """The cumulative funnel, plus the BTC share and its cause.

        Phase 6 §21 asks for a concentration flag *and* its cause.  The flag on
        its own would be a number without an explanation, which is the failure
        mode this project keeps finding in its own reports.  Never used to
        suppress a symbol -- if BTC earns the rank, BTC trades.

        Counts accumulate in :meth:`_record_funnel` across every evaluation.
        Trades are counted here because the ledger is the authority on them and
        re-counting per pass would multiply one trade by the number of passes
        that observed it.
        """
        report = self._funnel
        traded: dict[str, int] = {}
        for trade in ledger.closed_trades:
            traded[trade.symbol] = traded.get(trade.symbol, 0) + 1
        for pos in ledger.open_positions.values():
            traded[pos.symbol] = traded.get(pos.symbol, 0) + 1
        for symbol, n in traded.items():
            f = report.for_symbol(symbol)
            f.stages["traded"] = n

        data = report.as_dict()
        share = data["concentration"]
        btc = sum(v for k, v in share.items() if k.startswith("BTC"))
        total = sum(traded.values())
        data["btc_trade_share"] = round(btc, 4)
        data["sample_size"] = total
        # A share computed from a handful of trades is noise; say so rather
        # than flagging on it.  60 is roughly where a 70% reading stops being
        # luck.
        data["btc_concentration_flag"] = bool(btc > 0.70 and total >= 60)
        data["distinct_symbols_traded"] = len(share)
        return data

    def _best_alternative_edge(self, held_symbol: str) -> float:
        """Edge of the best idea we are *not* already in — what rotation costs."""
        return max(
            (
                o.net_edge_bps
                for o in self._ranked
                if o.symbol != held_symbol
            ),
            default=0.0,
        )

    @staticmethod
    def _position_net_usd(pos: Position) -> float:
        """Unrealized P&L with both fee legs already charged."""
        price = pos.last_price or pos.entry_price
        gross = pos.unrealized_usd(price)
        exit_fee = (
            pos.targets.costs.exit_fee_usd if pos.targets else pos.entry_fees_usd
        )
        return gross - pos.entry_fees_usd - exit_fee

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
        # Release and re-rank inside this call, not on the next tick. The
        # ledger already credited cash; what was stale for a tick is the
        # PORTFOLIO view -- correlated risk, directional exposure, the reserve,
        # DUPLICATE_SYMBOL -- and that is the view that decides whether the next
        # candidate is allowed in. Only the base ledger drives the scan; the
        # comparison ledgers follow the same decisions and must not each
        # trigger their own rescan.
        if ledger.name == "base":
            self.recycler.on_close(ledger, pos, trade=trade, ts_ms=ts_ms)
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

        positions = [self._position_row(pos, ts) for pos in ledger.open_positions.values()]

        trades = [self._trade_row(tr) for tr in ledger.closed_trades[-200:][::-1]]

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

        tournament, finalists = self._research_payload()
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
                # The three numbers that must never be conflated: what the price
                # move earned, what capturing it cost, and what actually landed.
                "profit_before_costs": round(stats.gross_pnl, 4),
                "total_costs": round(stats.fees + stats.slippage, 4),
                "real_net_profit": round(stats.net_pnl, 4),
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
                "maker_fills": self.stats.maker_fills,
                "maker_misses": self.stats.maker_misses,
                "checkpoints": self.stats.checkpoints,
                "recorder": self.recorder.stats.as_dict(),
                "tape": self.recorder.inventory(),
                "rejects": self.stats.rejects,
            },
            "portfolio": self._portfolio_block(ledger, ts),
            "capital": self._capital_block(ledger, ts),
            "board": self._opportunity_board(),
            "funnel": self._symbol_funnel(ledger),
            "opportunities": self.recycler.queue.as_dict(),
            "short_side": self._short_side_block(),
            "recycling": self._recycling_block(ledger, ts),
            "missed": _read_json(
                Path(self.s.research.directory) / "missed.json"
            ),
            "tournament": tournament,
            "finalists": finalists,
            "champion": self._champion_payload(ledger),
            "ledgers": [
                {
                    "name": n, "label": LEDGER_LABELS.get(n, n),
                    "starting_equity": lg.starting_equity, "equity": round(lg.equity, 2),
                }
                for n, lg in self.ledgers.items()
            ],
        }

    def _position_row(self, pos: Position, ts: int) -> dict[str, Any]:
        price = pos.last_price or pos.entry_price
        gross = pos.unrealized_usd(price)
        costs = pos.targets.costs if pos.targets else CostBreakdown(
            entry_fee_usd=pos.entry_fees_usd
        )
        exit_fee = costs.exit_fee_usd or pos.entry_fees_usd
        total_cost = pos.entry_fees_usd + exit_fee + costs.spread_cost_usd + costs.slippage_usd
        age = pos.holding_seconds(ts)
        return {
            "id": pos.id, "symbol": pos.symbol, "side": pos.side.value,
            "strategy": pos.strategy,
            "entry_price": pos.entry_price, "current_price": price,
            "notional_usd": round(pos.notional_usd, 2),
            "risk_usd": round(pos.risk_usd, 4),
            "account_risk_usd": round(pos.risk_usd, 4),
            "take_profit": pos.take_profit, "stop_loss": pos.stop_loss,
            "net_tp_price": pos.targets.net_tp_price if pos.targets else pos.take_profit,
            "net_tp_pct": pos.targets.net_tp_pct if pos.targets else None,
            "break_even_price": pos.break_even_price or None,
            "gross_pnl_usd": round(gross, 4),
            "entry_fee_usd": round(pos.entry_fees_usd, 4),
            "expected_exit_fee_usd": round(exit_fee, 4),
            "spread_cost_usd": round(costs.spread_cost_usd, 4),
            "slippage_usd": round(costs.slippage_usd, 4),
            "total_cost_usd": round(total_cost, 4),
            "net_pnl_usd": round(gross - total_cost, 4),
            "unrealized_usd": round(gross, 4),
            "fees_usd": round(pos.entry_fees_usd, 4),
            "maker_or_taker": "MAKER" if pos.entry_is_maker else "TAKER",
            "holding_seconds": round(age, 0),
            "age_seconds": round(age, 0),
            "expected_hold_window": [
                self.s.horizon.checkpoint_seconds, self.s.horizon.time_stop_seconds
            ],
            "checkpoint_status": self._checkpoint_status(pos, ts),
            "time_stop_ms": pos.time_stop_ms,
            "signal_strength": round(pos.expected_net_edge_bps, 2),
            "extended": pos.extended,
        }

    def _checkpoint_status(self, pos: Position, ts: int) -> str:
        if pos.checkpoint_done:
            return "TRAIL" if pos.trailing_armed else "KEEP"
        if pos.checkpoint_ms and ts >= pos.checkpoint_ms:
            return "DUE"
        return "PENDING"

    def _trade_row(self, t) -> dict[str, Any]:
        total_cost = t.fees_usd + abs(t.slippage_usd)
        return {
            "id": t.id, "symbol": t.symbol, "strategy": t.strategy,
            "entry_ms": t.entry_ms, "exit_ms": t.exit_ms,
            "entry_price": t.entry_price, "exit_price": t.exit_price,
            "notional_usd": round(t.notional_usd, 2),
            "gross_pnl_usd": round(t.gross_pnl_usd, 4),
            "entry_fee_usd": round(t.entry_fee_usd, 4),
            "exit_fee_usd": round(t.exit_fee_usd, 4),
            "fees_usd": round(t.fees_usd, 4),
            "slippage_usd": round(t.slippage_usd, 4),
            "total_cost_usd": round(total_cost, 4),
            "net_pnl_usd": round(t.net_pnl_usd, 4),
            "account_risk_usd": round(t.risk_usd, 4),
            "holding_seconds": round(t.holding_seconds, 0),
            "exit_reason": t.exit_reason.value,
            "maker_or_taker": t.__dict__.get("maker_or_taker", "TAKER"),
        }

    def _portfolio_block(self, ledger: Ledger, ts: int) -> dict[str, Any]:
        try:
            st = self.portfolio.state(ledger, ts_ms=ts, bars_by_symbol=self._bars_map())
        except Exception:
            log.exception("portfolio state failed")
            return {}
        sizes = sorted(p.notional_usd for p in st.positions)
        ages = [p.holding_seconds(ts) for p in st.positions]
        cp = self.s.horizon.checkpoint_seconds
        stop = self.s.horizon.time_stop_seconds
        stats = ledger.stats()
        deployed = st.deployed_usd or 1.0
        return {
            "open_positions": len(st.positions),
            "capital_deployed": round(st.deployed_usd, 2),
            "available_cash": round(st.available_usd, 2),
            "cash_reserve": round(st.reserve_required_usd, 2),
            "open_risk_usd": round(st.open_risk_usd, 4),
            "open_risk_pct": round(st.open_risk_pct, 3),
            "correlated_exposure_pct": round(
                st.correlated_risk_usd / st.equity * 100.0 if st.equity else 0.0, 3
            ),
            "directional_exposure_pct": round(st.directional_exposure_pct, 2),
            "avg_position_usd": round(sum(sizes) / len(sizes), 2) if sizes else 0.0,
            "median_position_usd": round(sizes[len(sizes) // 2], 2) if sizes else 0.0,
            "avg_holding_seconds": round(sum(ages) / len(ages), 0) if ages else 0.0,
            "positions_under_25m": sum(1 for a in ages if a < cp),
            "positions_25_60m": sum(1 for a in ages if cp <= a < stop),
            "positions_over_60m": sum(1 for a in ages if a >= stop),
            "capital_turnover_per_day": round(stats.capital_turnover, 3),
            "net_profit_per_dollar_deployed": round(stats.net_pnl / deployed, 6),
            "allocation_mode": self.s.portfolio.allocation_mode,
            "net_tp_profile": self.s.profit.profile,
            "max_open_positions": self.s.portfolio.max_open_positions,
            "total_open_risk_budget_pct": self.s.portfolio.total_open_risk_pct,
        }

    def _research_payload(self) -> tuple[dict[str, Any] | None, list[Any]]:
        """Tournament output, if a search has been published to this box.

        Read fresh each time rather than cached: a tournament result arriving
        mid-session should show up without a restart, and the files are small.
        """
        base = Path(self.s.research.directory)
        tournament = _read_json(base / self.s.research.tournament_file)
        finalists = _read_json(base / self.s.research.finalists_file)
        if isinstance(finalists, dict):
            finalists = finalists.get("finalists", [])
        return (
            tournament if isinstance(tournament, dict) else None,
            finalists if isinstance(finalists, list) else [],
        )

    def _champion_payload(self, ledger: Ledger) -> dict[str, Any] | None:
        """The live champion, with its out-of-sample claim next to what it is
        actually doing. They will disagree; seeing the gap is the point."""
        if self.champion is None:
            return None
        live = ledger.stats(strategy=self.champion.family) if self.champion.family else ledger.stats()
        return {
            "strategy": self.champion.describe(),
            "key": self.champion.key,
            "family": self.champion.family,
            "symbol": self.champion.symbol,
            "regime": self.champion.regime,
            "horizon_seconds": self.champion.horizon_seconds,
            "version": self.champion.simulator_version,
            "started_ms": self.champion.promoted_ms,
            "paper_equity": round(ledger.equity, 4),
            "oos_profit_factor": self.champion.oos_profit_factor,
            "oos_expectancy": self.champion.oos_expectancy,
            "oos_trades": self.champion.oos_trades,
            "live_profit_factor": round(live.profit_factor, 3),
            "expectancy": round(live.expectancy, 4),
            "max_drawdown": round(live.max_drawdown, 4),
            "trades": live.trades,
            "gate_reasons": list(self.champion.gate_reasons),
        }

    def _recycling_block(self, ledger: Ledger, ts: int) -> dict[str, Any]:
        """Close -> release -> rescan -> redeploy, with the latencies.

        Phase 6 §22.  The point of the panel is that idle capital after a close
        has a duration attached to it, so "we freed the money and then sat on
        it" cannot pass as "there was nothing to buy".
        """
        block: dict[str, Any] = dict(self.recycler.latency())
        last = self.recycler.last
        block["last"] = last.as_dict() if last is not None else None
        block["events"] = [e.as_dict() for e in self.recycler.events[-25:]]
        block["available_cash_usd"] = round(ledger.cash, 2)
        block["qualified_opportunities"] = len(self._ranked)
        # Idle duration measured from the last release, not from process start:
        # an account that has never traded is not "idle for six hours".
        released = last.capital_released_ms if last is not None else 0
        block["idle_seconds"] = round((ts - released) / 1000.0, 1) if released else None
        nxt = self._ranked[0] if self._ranked else None
        block["next_candidate"] = (
            {"symbol": nxt.symbol, "strategy": nxt.signal.strategy,
             "net_edge_bps": round(nxt.net_edge_bps, 3)} if nxt else None
        )
        block["next_entry_strategy"] = nxt.signal.strategy if nxt else None
        return block

    def _capital_block(self, ledger: Ledger, ts: int) -> dict[str, Any] | None:
        """Capital utilisation, with every idle dollar attributed to a reason.

        The dashboard contract names `idle_usd` / `idle_by_reason` while
        `IdleAttribution` emits `total_idle_usd` / `by_reason`; the mapping
        happens here rather than in either owner's module.
        """
        try:
            from app.portfolio import attribute_idle

            state = self.portfolio.state(
                ledger, ts_ms=ts, bars_by_symbol=self._bars_map()
            )
            target = getattr(self.s.portfolio, "target_deployed_pct", 80.0)
            att = attribute_idle(
                state,
                ranked=self._ranked,
                rejections=self.stats.rejects,
                target_pct=target,
                equity=ledger.equity,
                # Lets the attribution separate a scanner that has not run
                # from a market with nothing in it, and charge a genuine
                # accounting imbalance to CAPITAL_STATE_BUG instead of
                # laundering it through a policy bucket.
                context=self.recycler.idle_context(
                    ts_ms=ts,
                    blocked_reason=self.risk.blocked_reason(
                        self._risk_state(ledger, ts)
                    ),
                ),
            )
        except Exception:
            log.exception("idle attribution failed")
            return None

        stats = ledger.stats()
        day_start = ts - (ts % 86_400_000)
        deployed = state.deployed_usd
        sizes = [p.notional_usd for p in state.positions]
        ages = [p.holding_seconds(ts) for p in state.positions]
        return {
            "equity": round(ledger.equity, 4),
            "deployed_usd": round(deployed, 2),
            "deployed_pct": round(state.deployed_pct, 3),
            "idle_usd": round(att.total_idle_usd, 2),
            "idle_pct": round(getattr(att, "idle_pct", 0.0), 3),
            "target_pct": round(float(target), 2),
            "open_positions": len(state.positions),
            "qualified_opportunities": len(self._ranked),
            "rejected_opportunities": sum(self.stats.rejects.values()),
            "turnover_per_day": round(stats.capital_turnover, 3),
            "avg_position_usd": round(sum(sizes) / len(sizes), 2) if sizes else 0.0,
            "avg_holding_seconds": round(sum(ages) / len(ages), 0) if ages else 0.0,
            "net_pnl_today": round(ledger.realized_since(day_start), 4),
            "net_pnl_per_100_deployed": (
                round(stats.net_pnl / deployed * 100.0, 4) if deployed > 0 else 0.0
            ),
            "idle_by_reason": {k: round(v, 2) for k, v in att.by_reason.items()},
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
            self.recorder.close()
            await self.rest.close()

    def stop(self) -> None:
        self._stop.set()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


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
