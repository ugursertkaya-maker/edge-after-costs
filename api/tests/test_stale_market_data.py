"""Stale market data can never reach a decision — proved, not asserted in prose.

Three production incidents in this project came from the same root: a book that
was not being updated still reached the feature engine, the regime classifier
read the resulting nonsense as ABNORMAL, and every strategy refused the symbol
for the wrong reason. Once the guard existed, a fourth variant appeared — a
HEALTHY 1-second WARM feed rejected by a threshold calibrated for the 100ms HOT
stream.

Every test here is deterministic and offline: no network, no clock races, no
shared state between cases.
"""

from __future__ import annotations

import pytest

from app.types import (
    Bar, MarketSnapshot, OrderBook, BookLevel, RejectReason, SymbolInfo,
)
from app.universe import UniverseEntry
from app.worker.main import (
    WARM_SNAPSHOT_INTERVAL_MS, WARM_STALENESS_SNAPSHOTS, TradingWorker,
)

NOW = 1_789_000_000_000


def _info(symbol: str = "BTCUSD") -> SymbolInfo:
    return SymbolInfo(symbol=symbol, base_asset=symbol[:-3], quote_asset="USD",
                      status="TRADING", step_size=1e-5, tick_size=0.01,
                      min_qty=1e-5, min_notional=1.0)


def _entry(symbol: str = "BTCUSD", tier: str = "hot") -> UniverseEntry:
    return UniverseEntry(
        symbol=symbol, info=_info(symbol), quote_volume_24h=1e6, trades_24h=4000,
        spread_bps=1.0, volatility_bps=300.0, depth_usd=50_000.0, score=1.0, tier=tier,
    )


def _book(symbol: str = "BTCUSD", *, last_update_ms: int) -> OrderBook:
    return OrderBook(
        symbol=symbol,
        bids=[BookLevel(price=100.0, qty=10.0)],
        asks=[BookLevel(price=100.1, qty=10.0)],
        last_update_id=1,
        last_update_ms=last_update_ms,
    )


class _Feed:
    """Minimal stand-in: the guard only reads `universe` and `connected`."""

    def __init__(self, universe=("BTCUSD",), connected=True):
        self.universe = list(universe)
        self.connected = connected


class _Books:
    def __init__(self):
        self.resync_requests: list[tuple[str, str]] = []
        self.desynced: list[tuple[str, str]] = []

    def request_resync(self, symbol, why):
        self.resync_requests.append((symbol, why))

    def mark_desynced(self, symbol, why):   # must NOT be used for staleness
        self.desynced.append((symbol, why))


def _worker(*, feed=None, books=None) -> TradingWorker:
    """A worker with only the collaborators the guard touches."""
    from app.config import get_settings

    w = TradingWorker.__new__(TradingWorker)
    w.s = get_settings()
    w._stale_books = {}
    w.feed = feed or _Feed()
    w.books = books or _Books()
    return w


class TestFreshBooksAreUsed:
    def test_a_fresh_hot_book_passes(self):
        w = _worker()
        book = _book(last_update_ms=NOW - 200)
        assert w._check_book_freshness(_entry(), book, NOW) is None
        assert w.stale_market_data() == []

    def test_exactly_at_the_budget_is_still_fresh(self):
        w = _worker()
        budget = w._freshness_budget_ms(_entry())
        book = _book(last_update_ms=NOW - budget)
        assert w._check_book_freshness(_entry(), book, NOW) is None

    def test_a_healthy_warm_feed_is_not_called_stale(self):
        """The regression: SOLUSD reported stale at 3,530ms on a working feed."""
        w = _worker(feed=_Feed(universe=("SOLUSD",)))
        book = _book("SOLUSD", last_update_ms=NOW - 3_530)
        entry = _entry("SOLUSD", tier="warm")
        assert w._check_book_freshness(entry, book, NOW) is None


class TestStaleBooksAreRejectedBeforeFeatures:
    def test_a_stale_hot_book_is_rejected(self):
        w = _worker()
        book = _book(last_update_ms=NOW - 60_000)
        rec = w._check_book_freshness(_entry(), book, NOW)
        assert rec is not None
        assert rec["reason"] == RejectReason.STALE_MARKET_DATA.value

    def test_the_record_carries_every_field_needed_to_diagnose_it(self):
        w = _worker()
        book = _book(last_update_ms=NOW - 60_000)
        rec = w._check_book_freshness(_entry(), book, NOW)
        for field in ("symbol", "tier", "book_age_ms", "last_update_ms",
                      "current_time_ms", "freshness_budget_ms", "subscribed",
                      "websocket_connected", "since_ms", "passes"):
            assert field in rec, f"{field} missing from the stale record"
        assert rec["book_age_ms"] == 60_000
        assert rec["current_time_ms"] == NOW
        assert rec["subscribed"] is True
        assert rec["websocket_connected"] is True

    def test_an_unpopulated_book_reports_no_age_rather_than_a_sentinel(self):
        """600,000ms was features.NO_BOOK_AGE_MS, and reading it as a real
        measurement is how this was misdiagnosed twice."""
        w = _worker()
        rec = w._check_book_freshness(_entry(), _book(last_update_ms=0), NOW)
        assert rec["never_updated"] is True
        assert rec["book_age_ms"] is None
        assert rec["book_age_ms"] != 600_000

    def test_snapshot_returns_nothing_for_a_stale_book(self):
        """No snapshot means no features, no regime, no signal, no candidate."""
        w = _worker()
        w.books = _Books()
        w.books.get = lambda s: _book(last_update_ms=NOW - 60_000)  # type: ignore
        assert w._snapshot(_entry(), NOW) is None

    def test_a_stale_symbol_cannot_reach_the_strategies(self):
        """The pipeline gate: _snapshot is the only path into features."""
        import inspect
        src = inspect.getsource(TradingWorker.evaluate_once)
        gate = src[: src.index("self.features.compute")]
        assert "_snapshot(" in gate
        assert "continue" in gate


class TestRecovery:
    def test_a_stale_symbol_requests_a_resync(self):
        books = _Books()
        w = _worker(books=books)
        w._check_book_freshness(_entry(), _book(last_update_ms=NOW - 60_000), NOW)
        assert books.resync_requests and books.resync_requests[0][0] == "BTCUSD"

    def test_staleness_never_invalidates_the_book(self):
        """mark_desynced sets is_valid() false, and monitor_once skips any
        symbol whose book is invalid -- so using it here would silently
        disable the stop-loss on an open position while the feed struggles."""
        books = _Books()
        w = _worker(books=books)
        w._check_book_freshness(_entry(), _book(last_update_ms=NOW - 60_000), NOW)
        assert books.desynced == [], "a stale book must stay usable for exits"

    def test_it_keeps_reporting_until_fresh_data_resumes(self):
        w = _worker()
        stale = _book(last_update_ms=NOW - 60_000)
        for i in range(3):
            w._check_book_freshness(_entry(), stale, NOW + i)
        assert w.stale_market_data()[0]["passes"] == 3
        assert w.stale_market_data()[0]["since_ms"] == NOW

    def test_it_becomes_eligible_again_on_the_first_fresh_update(self):
        w = _worker()
        w._check_book_freshness(_entry(), _book(last_update_ms=NOW - 60_000), NOW)
        assert w.stale_market_data() != []
        fresh = _book(last_update_ms=NOW + 59_900)
        assert w._check_book_freshness(_entry(), fresh, NOW + 60_000) is None
        assert w.stale_market_data() == []

    def test_recovery_does_not_leak_into_other_symbols(self):
        w = _worker(feed=_Feed(universe=("BTCUSD", "ETHUSD")))
        w._check_book_freshness(_entry("BTCUSD"), _book("BTCUSD", last_update_ms=0), NOW)
        w._check_book_freshness(_entry("ETHUSD"), _book("ETHUSD", last_update_ms=0), NOW)
        assert len(w.stale_market_data()) == 2
        w._check_book_freshness(
            _entry("ETHUSD"), _book("ETHUSD", last_update_ms=NOW - 10), NOW)
        assert [r["symbol"] for r in w.stale_market_data()] == ["BTCUSD"]


class TestSubscriptionStateIsRecorded:
    def test_an_unsubscribed_symbol_says_so(self):
        w = _worker(feed=_Feed(universe=("ETHUSD",)))
        rec = w._check_book_freshness(_entry("BTCUSD"), _book(last_update_ms=0), NOW)
        assert rec["subscribed"] is False

    def test_a_dead_websocket_says_so(self):
        w = _worker(feed=_Feed(connected=False))
        rec = w._check_book_freshness(_entry(), _book(last_update_ms=0), NOW)
        assert rec["websocket_connected"] is False


class TestTierAwareBudget:
    def test_hot_uses_the_configured_limit(self):
        w = _worker()
        assert w._freshness_budget_ms(_entry(tier="hot")) == w.s.risk.max_book_age_ms

    def test_warm_follows_its_own_snapshot_cadence(self):
        w = _worker()
        warm = w._freshness_budget_ms(_entry(tier="warm"))
        assert warm == max(w.s.risk.max_book_age_ms,
                           WARM_SNAPSHOT_INTERVAL_MS * WARM_STALENESS_SNAPSHOTS)
        assert warm > 3_530

    def test_warm_still_catches_a_dead_feed(self):
        w = _worker()
        assert w._freshness_budget_ms(_entry(tier="warm")) < 30_000
