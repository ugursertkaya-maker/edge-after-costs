"""End-to-end exercise of the autonomous loop with synthetic market data.

No network, no database, no Redis.  The worker is driven one pass at a time so
the whole cycle — scan, rank, risk, paper entry, monitor, exit, capital
released — is observable and deterministic.
"""

from __future__ import annotations

import math

import pytest

from app.config import reload_settings
from app.types import (
    Bar,
    Decision,
    OrderBook,
    Side,
    SymbolInfo,
    Trade,
)

SYMBOL = "BTCUSD"
INFO = SymbolInfo(
    symbol=SYMBOL, base_asset="BTC", quote_asset="USD", status="TRADING",
    tick_size=0.01, step_size=0.00001, min_qty=0.00001, min_notional=1.0,
)


def make_book(mid: float, *, spread_bps: float = 2.0, depth_usd: float = 50_000.0) -> OrderBook:
    from app.types import BookLevel

    half = mid * spread_bps / 2 / 10_000.0
    levels = 12
    per_level_usd = depth_usd / levels
    bids = [
        BookLevel(round(mid - half - i * mid * 0.0001, 2), per_level_usd / mid)
        for i in range(levels)
    ]
    asks = [
        BookLevel(round(mid + half + i * mid * 0.0001, 2), per_level_usd / mid)
        for i in range(levels)
    ]
    return OrderBook(symbol=SYMBOL, bids=bids, asks=asks, last_update_id=1, last_update_ms=1_000_000)


def make_bars(n: int, *, start: float, drift_bps_per_bar: float, base_ms: int = 0) -> list[Bar]:
    bars: list[Bar] = []
    price = start
    for i in range(n):
        open_p = price
        price = price * (1 + drift_bps_per_bar / 10_000.0)
        high = max(open_p, price) * 1.0004
        low = min(open_p, price) * 0.9996
        bars.append(
            Bar(
                symbol=SYMBOL,
                open_ms=base_ms + i * 60_000,
                close_ms=base_ms + (i + 1) * 60_000 - 1,
                open=open_p, high=high, low=low, close=price,
                volume=10.0, quote_volume=10.0 * price, trades=400,
                taker_buy_quote=6.0 * price,
            )
        )
    return bars


def make_trades(n: int, *, price: float, ts_ms: int, buy_ratio: float = 0.7) -> list[Trade]:
    out = []
    for i in range(n):
        out.append(
            Trade(
                symbol=SYMBOL, price=price, qty=0.01, ts_ms=ts_ms - (n - i) * 100,
                buyer_is_maker=(i / n) > buy_ratio,
            )
        )
    return out


class FakeFeed:
    """Stands in for MarketDataFeed: same surface, fixed data."""

    def __init__(self, bars, trades):
        self._bars = bars
        self._trades = trades
        self.reconnects = 0

    def set_universe(self, hot, warm):
        self.hot, self.warm = hot, warm

    def bars(self, symbol, limit=240):
        return self._bars[-limit:]

    def recent_trades(self, symbol, limit=200):
        return self._trades[-limit:]

    def health(self):
        from app.exchange.ws import DataHealth

        return DataHealth(
            connected=True, streams=3, updates_per_sec=42.0, reconnects=0, dropped=0,
            last_message_ms=1_000_000, stale_symbols=[], desynced_symbols=[],
        )

    async def run(self, stop=None):
        return None


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "PAPER")
    monkeypatch.setenv("MIN_NET_EDGE_BPS", "0.5")
    monkeypatch.setenv("UNIVERSE_MAX_SYMBOLS", "1")
    reload_settings()

    from app.runtime import RuntimeStore
    from app.universe import UniverseEntry
    from app.worker.main import TradingWorker

    store = RuntimeStore(client=None)
    store._client = None
    w = TradingWorker(store=store)
    # Never let a test touch the database.
    monkeypatch.setattr(w, "_persist", lambda fn: None)

    mid = 60_000.0
    bars = make_bars(180, start=58_000.0, drift_bps_per_bar=6.0)
    trades = make_trades(120, price=mid, ts_ms=1_000_000)
    w.feed = FakeFeed(bars, trades)
    w.symbol_info = {SYMBOL: INFO}
    w.books.apply_snapshot(
        SYMBOL, 1,
        [(lv.price, lv.qty) for lv in make_book(mid).bids],
        [(lv.price, lv.qty) for lv in make_book(mid).asks],
        1_000_000,
    )
    w.universe = [
        UniverseEntry(
            symbol=SYMBOL, info=INFO, quote_volume_24h=900_000_000.0, trades_24h=400_000,
            spread_bps=2.0, volatility_bps=120.0, depth_usd=50_000.0, score=1.0, tier="hot",
        )
    ]
    yield w
    reload_settings()


def test_evaluation_pass_produces_a_scanner_row(worker):
    worker.evaluate_once(1_000_000)
    assert len(worker.scanner) == 1
    row = worker.scanner[0]
    assert row.symbol == SYMBOL
    assert row.decision in {d.value for d in Decision}
    assert math.isfinite(row.net_edge_bps)
    assert math.isfinite(row.price) and row.price > 0


def test_scanner_row_serialises_for_the_dashboard(worker):
    worker.evaluate_once(1_000_000)
    d = worker.scanner[0].as_dict()
    for key in ("symbol", "price", "regime", "decision", "net_edge_bps", "strategy"):
        assert key in d


def test_dashboard_payload_shape(worker):
    worker.evaluate_once(1_000_000)
    payload = worker.dashboard_payload("base")
    assert payload["mode"] == "PAPER"
    assert payload["live_orders_enabled"] is False
    for key in (
        "header", "metrics", "scanner", "positions", "trades", "strategies",
        "equity_curve", "health", "ledgers",
    ):
        assert key in payload
    assert payload["metrics"]["equity"] == pytest.approx(500.0)
    assert {lg["name"] for lg in payload["ledgers"]} == {"base", "l1000", "l5000"}


def test_all_three_ledgers_start_at_their_own_capital(worker):
    assert worker.ledgers["base"].starting_equity == 500.0
    assert worker.ledgers["l1000"].starting_equity == 1000.0
    assert worker.ledgers["l5000"].starting_equity == 5000.0


def test_entry_then_exit_returns_capital(worker):
    """The core requirement: closing a position frees the capital immediately."""
    ts = 1_000_000
    ranked = worker.evaluate_once(ts)
    if not ranked:
        pytest.skip("synthetic market produced no qualifying opportunity")

    ledger = worker.ledgers["base"]
    cash_before = ledger.cash
    opened = worker.try_enter(ranked, ts)
    if opened == 0:
        pytest.skip("risk engine declined the synthetic opportunity")

    assert ledger.cash < cash_before, "opening a position must consume cash"
    pos = next(iter(ledger.open_positions.values()))
    assert pos.stop_loss > 0 and pos.take_profit > 0, "no trade may exist without a stop"
    assert pos.risk_usd > 0

    # Force the exit by moving the book through the take profit.
    tp_mid = pos.take_profit * 1.002
    book = make_book(tp_mid)
    worker.books.apply_snapshot(
        SYMBOL, 2,
        [(lv.price, lv.qty) for lv in book.bids],
        [(lv.price, lv.qty) for lv in book.asks],
        ts + 60_000,
    )
    closed = worker.monitor_once(ts + 60_000)
    assert closed == 1
    assert not ledger.open_positions
    assert ledger.cash > cash_before - 1e-9, "capital was not released on close"
    trade = ledger.closed_trades[-1]
    assert trade.fees_usd > 0, "a round trip must be charged fees"
    assert trade.net_pnl_usd == pytest.approx(
        trade.gross_pnl_usd - trade.fees_usd, abs=1e-6
    )


def test_kill_switch_blocks_new_entries(worker):
    worker.store.set_kill_switch(True)
    ts = 1_000_000
    ranked = worker.evaluate_once(ts)
    assert worker.try_enter(ranked, ts) == 0
    worker.store.set_kill_switch(False)


def test_stale_book_is_never_traded(worker):
    """Age the book past the limit; nothing may be opened against it."""
    stale_ts = 1_000_000 + worker.s.risk.max_book_age_ms * 10
    ranked = worker.evaluate_once(stale_ts)
    assert worker.try_enter(ranked, stale_ts) == 0


def test_no_position_exceeds_the_configured_share_of_equity(worker):
    ts = 1_000_000
    ranked = worker.evaluate_once(ts)
    worker.try_enter(ranked, ts)
    ledger = worker.ledgers["base"]
    for pos in ledger.open_positions.values():
        cap = ledger.starting_equity * worker.s.risk.max_position_pct_equity / 100.0
        assert pos.notional_usd <= cap * 1.02


def test_short_signals_are_never_executed_on_spot(worker):
    ts = 1_000_000
    ranked = worker.evaluate_once(ts)
    worker.try_enter(ranked, ts)
    for ledger in worker.ledgers.values():
        for pos in ledger.open_positions.values():
            assert pos.side is Side.LONG
