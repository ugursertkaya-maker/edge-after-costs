"""End-to-end proof of §19: the same $500 trades over and over.

This drives the *real* risk engine, exit engine, simulator and ledger through
twenty complete round trips on a synthetic book.  It is the test that would
fail if capital were ever locked up after a close — which on a $500 account is
the difference between three trades a day and thirty.
"""

from __future__ import annotations

import random

import pytest

from app.config import Settings
from app.exits import ExitEngine
from app.risk import RiskEngine, RiskState
from app.sim.ledger import Ledger
from app.sim.simulator import PaperSimulator
from app.types import (
    BookLevel,
    ExitReason,
    Features,
    Opportunity,
    OrderBook,
    OrderRequest,
    OrderType,
    Position,
    Regime,
    RegimeState,
    Side,
    Signal,
    SymbolInfo,
)

TS = 1_700_000_000_000
DAY_MS = 86_400_000
STARTING = 500.0
N_TRADES = 20

INFO = SymbolInfo(
    symbol="BTCUSD", base_asset="BTC", quote_asset="USD", status="TRADING",
    tick_size=0.01, step_size=0.00001, min_qty=0.00001, min_notional=1.0,
)


def book(mid: float, ts_ms: int, *, qty: float = 20.0, levels: int = 12) -> OrderBook:
    half = 0.01
    return OrderBook(
        symbol="BTCUSD",
        bids=[BookLevel(round(mid - half - i * 0.01, 2), qty) for i in range(levels)],
        asks=[BookLevel(round(mid + half + i * 0.01, 2), qty) for i in range(levels)],
        last_update_ms=ts_ms,
    )


def features(price: float, ts_ms: int) -> Features:
    return Features(
        symbol="BTCUSD", ts_ms=ts_ms, price=price, mid=price, microprice=price,
        spread_bps=2.0, book_age_ms=50, depth_bid_usd=20_000.0, depth_ask_usd=20_000.0,
        imbalance=0.05, atr_bps=200.0, realized_vol_bps=180.0, vol_percentile=0.5,
        ret_1m_bps=4.0, ret_5m_bps=8.0, ret_15m_bps=10.0, ema_fast=price * 1.001,
        ema_slow=price * 0.999, ema_slope_bps=1.0, trend_strength=0.6, range_pos=0.5,
        bb_width_bps=60.0, compression=0.3, volume_ratio=1.3, trade_flow_imbalance=0.2,
        trade_flow_accel=0.1, vwap=price, vwap_dev_bps=1.0,
        quote_volume_24h=80_000_000.0, trades_24h=200_000, bars_available=240,
    )


def opportunity(price: float, ts_ms: int) -> Opportunity:
    sig = Signal(
        strategy="momentum_scalp", symbol="BTCUSD", side=Side.LONG, score=0.72,
        target_bps=200.0, stop_bps=200.0, ts_ms=ts_ms,
    )
    return Opportunity(
        signal=sig,
        features=features(price, ts_ms),
        regime=RegimeState(regime=Regime.TRENDING, confidence=0.8),
        gross_edge_bps=34.0, fee_bps=20.0, spread_cost_bps=2.0, slippage_bps=2.0,
        net_edge_bps=10.0, rank_score=1.0, expected_hold_seconds=600,
    )


def test_twenty_round_trips_recycle_the_same_capital() -> None:
    settings = Settings()
    risk = RiskEngine(settings=settings)
    exits = ExitEngine(settings=settings)
    sim = PaperSimulator(settings=settings, rng=random.Random(20260910))
    led = Ledger("base", STARTING, settings.simulator_version)

    price = 100.0
    traded_notional = 0.0
    wins = 0

    for i in range(N_TRADES):
        ts = TS + i * (DAY_MS // 10)          # twenty trades across two days
        entry_book = book(price, ts)

        state = RiskState(
            equity=led.equity,
            open_positions=list(led.open_positions.values()),
            realized_pnl_today=0.0,
            realized_pnl_week=0.0,
            loss_streak=led.loss_streak(),
            cooldown_until_ms=0,
            kill_switch=False,
            data_healthy=True,
            ts_ms=ts,
        )
        verdict = risk.evaluate(opportunity(price, ts), state, info=INFO, book=entry_book)
        assert verdict.approved, f"trade {i} rejected: {verdict.reject_reason}"

        cash_before = led.cash
        assert led.can_afford(verdict.notional_usd)

        entry = sim.execute(
            OrderRequest(
                symbol="BTCUSD", side=Side.LONG, order_type=OrderType.MARKET,
                qty=verdict.qty, ts_ms=ts,
            ),
            entry_book,
            INFO,
            ts_ms=ts,
        )
        assert entry.accepted and entry.filled_qty > 0

        pos = Position(
            id=f"t{i}", symbol="BTCUSD", side=Side.LONG, strategy="momentum_scalp",
            qty=entry.filled_qty, entry_price=entry.avg_price, entry_ms=ts,
            notional_usd=entry.notional, risk_usd=verdict.risk_usd,
            take_profit=verdict.take_profit, stop_loss=verdict.stop_loss,
            entry_fees_usd=entry.fees_usd, entry_slippage_bps=entry.slippage_bps,
            target_bps=200.0, stop_bps=200.0,
        )
        led.open(pos, entry)

        # Capital is committed while the position is open...
        assert led.cash == pytest.approx(cash_before - entry.notional - entry.fees_usd)
        assert led.cash < cash_before
        assert led.exposure_usd > 0

        # ...and the market resolves it, alternately into profit and into loss.
        win = i % 2 == 0
        exit_mid = pos.take_profit * 1.0005 if win else pos.stop_loss * 0.9995
        exit_ts = ts + 600_000
        exit_book = book(exit_mid, exit_ts)

        reason = exits.check(pos, exit_book, None, exit_ts)
        assert reason is (ExitReason.TAKE_PROFIT if win else ExitReason.STOP_LOSS)

        exit_order = sim.execute(
            OrderRequest(
                symbol="BTCUSD", side=Side.SHORT, order_type=OrderType.MARKET,
                qty=pos.qty, ts_ms=exit_ts,
            ),
            exit_book,
            INFO,
            ts_ms=exit_ts,
        )
        assert exit_order.accepted
        trade = led.close(pos, exit_order, reason, exit_ts)

        # THE REQUIREMENT: proceeds are back in cash the moment we close.
        assert led.open_positions == {}
        assert led.exposure_usd == 0.0
        assert led.cash == pytest.approx(
            cash_before - entry.notional - entry.fees_usd + exit_order.notional - exit_order.fees_usd
        )
        assert led.cash == pytest.approx(cash_before + trade.net_pnl_usd)
        assert led.cash == pytest.approx(led.equity)

        # And it is spendable immediately — the next iteration proves it by sizing
        # against this equity and passing can_afford.
        assert led.can_afford(min(verdict.notional_usd, led.cash))

        traded_notional += trade.notional_usd
        wins += 1 if trade.is_win else 0
        price = exit_mid

    # ---- the whole loop ------------------------------------------------
    assert len(led.closed_trades) == N_TRADES
    assert wins == N_TRADES // 2
    assert led.cash == pytest.approx(STARTING + led.realized_pnl)

    stats = led.stats()
    assert stats.trades == N_TRADES
    assert stats.capital_turnover == pytest.approx(traded_notional / STARTING)
    # $500 did several times its own size in business — that is the point.
    assert stats.capital_turnover > 3.0
    assert traded_notional > 3 * STARTING

    end_ms = max(t.exit_ms for t in led.closed_trades)
    days = (end_ms - TS) / DAY_MS
    assert 1.5 < days < 2.5
    per_day = led.stats(since_ms=TS).capital_turnover
    assert per_day == pytest.approx((traded_notional / STARTING) / days)


def test_capital_is_never_created_or_destroyed_by_the_loop() -> None:
    """Ending cash is starting cash plus every net P&L, to the cent."""
    settings = Settings()
    sim = PaperSimulator(settings=settings, rng=random.Random(7))
    led = Ledger("base", STARTING, settings.simulator_version)

    price = 100.0
    for i in range(N_TRADES):
        ts = TS + i * 60_000
        b = book(price, ts)
        entry = sim.execute(
            OrderRequest(symbol="BTCUSD", side=Side.LONG, order_type=OrderType.MARKET, qty=1.0, ts_ms=ts),
            b, INFO, ts_ms=ts,
        )
        pos = Position(
            id=f"x{i}", symbol="BTCUSD", side=Side.LONG, strategy="s", qty=entry.filled_qty,
            entry_price=entry.avg_price, entry_ms=ts, notional_usd=entry.notional,
            risk_usd=2.0, take_profit=price * 1.01, stop_loss=price * 0.99,
            entry_fees_usd=entry.fees_usd, entry_slippage_bps=entry.slippage_bps,
        )
        led.open(pos, entry)
        exit_book = book(price * 1.002, ts + 1_000)
        exit_order = sim.execute(
            OrderRequest(symbol="BTCUSD", side=Side.SHORT, order_type=OrderType.MARKET, qty=pos.qty, ts_ms=ts + 1_000),
            exit_book, INFO, ts_ms=ts + 1_000,
        )
        led.close(pos, exit_order, ExitReason.TAKE_PROFIT, ts + 1_000)

    assert led.cash == pytest.approx(STARTING + sum(t.net_pnl_usd for t in led.closed_trades))
    assert led.equity == pytest.approx(led.cash)
    # Twenty ~$100 round trips on a $500 account: 4x turnover.
    assert led.stats().capital_turnover == pytest.approx(4.0, abs=0.05)


def test_a_closed_position_frees_a_slot_for_the_next_symbol() -> None:
    """Recycling is not only cash: the concurrency budget is returned too."""
    settings = Settings()
    risk = RiskEngine(settings=settings)
    sim = PaperSimulator(settings=settings, rng=random.Random(3))
    led = Ledger("base", STARTING, settings.simulator_version)
    max_positions = settings.risk.max_concurrent_positions

    def state_now(ts: int) -> RiskState:
        return RiskState(
            equity=led.equity, open_positions=list(led.open_positions.values()),
            realized_pnl_today=0.0, realized_pnl_week=0.0, loss_streak=0,
            cooldown_until_ms=0, kill_switch=False, data_healthy=True, ts_ms=ts,
        )

    b = book(100.0, TS)
    for i in range(max_positions):
        entry = sim.execute(
            OrderRequest(symbol="BTCUSD", side=Side.LONG, order_type=OrderType.MARKET, qty=0.5, ts_ms=TS),
            b, INFO, ts_ms=TS,
        )
        led.open(
            Position(
                id=f"p{i}", symbol=f"S{i}USD", side=Side.LONG, strategy="s", qty=entry.filled_qty,
                entry_price=entry.avg_price, entry_ms=TS, notional_usd=entry.notional,
                risk_usd=0.1, take_profit=101.0, stop_loss=99.0,
                entry_fees_usd=entry.fees_usd, entry_slippage_bps=entry.slippage_bps,
            ),
            entry,
        )

    assert not risk.evaluate(opportunity(100.0, TS), state_now(TS), info=INFO, book=b).approved

    victim = led.open_positions["p0"]
    exit_order = sim.execute(
        OrderRequest(symbol="BTCUSD", side=Side.SHORT, order_type=OrderType.MARKET, qty=victim.qty, ts_ms=TS + 1),
        b, INFO, ts_ms=TS + 1,
    )
    led.close(victim, exit_order, ExitReason.TIME_STOP, TS + 1)

    assert risk.evaluate(opportunity(100.0, TS), state_now(TS), info=INFO, book=b).approved
