"""Ledger: cash discipline, the equity identity, and the statistics."""

from __future__ import annotations

import pytest

from app.sim.ledger import Ledger
from app.types import ExitReason, Fill, OrderResult, OrderType, Position, Side

TS = 1_700_000_000_000
VERSION = "sim-test"


def ledger(starting: float = 500.0) -> Ledger:
    return Ledger("base", starting, VERSION)


def result(price: float, qty: float, *, fee_bps: float = 10.0, slippage_bps: float = 1.0) -> OrderResult:
    notional = price * qty
    fees = notional * fee_bps / 10_000
    return OrderResult(
        accepted=True,
        order_type=OrderType.MARKET,
        requested_qty=qty,
        filled_qty=qty,
        avg_price=price,
        fees_usd=fees,
        slippage_bps=slippage_bps,
        fills=[Fill(price=price, qty=qty, fee_usd=fees, is_maker=False, ts_ms=TS)],
    )


def position(pid: str = "p1", *, symbol: str = "BTCUSD", strategy: str = "momentum_scalp",
             qty: float = 1.0, entry: float = 100.0, entry_ms: int = TS) -> Position:
    return Position(
        id=pid,
        symbol=symbol,
        side=Side.LONG,
        strategy=strategy,
        qty=qty,
        entry_price=entry,
        entry_ms=entry_ms,
        notional_usd=qty * entry,
        risk_usd=2.5,
        take_profit=entry * 1.01,
        stop_loss=entry * 0.99,
        entry_fees_usd=0.0,
        entry_slippage_bps=0.0,
    )


def open_at(led: Ledger, pid: str, price: float, qty: float, **kw) -> Position:
    pos = position(pid, entry=price, qty=qty, **kw)
    led.open(pos, result(price, qty))
    return pos


# --------------------------------------------------------------------------- #
# Cash
# --------------------------------------------------------------------------- #


def test_open_debits_notional_and_fees() -> None:
    led = ledger()
    entry = result(100.0, 1.0)
    led.open(position(), entry)
    assert led.cash == pytest.approx(500.0 - 100.0 - entry.fees_usd)
    assert led.exposure_usd == pytest.approx(100.0)


def test_close_returns_proceeds_net_of_fees_immediately() -> None:
    """The capital-recycling requirement, in its smallest possible form."""
    led = ledger()
    pos = open_at(led, "p1", 100.0, 1.0)
    cash_while_open = led.cash

    exit_ = result(101.0, 1.0)
    trade = led.close(pos, exit_, ExitReason.TAKE_PROFIT, TS + 60_000)

    assert led.cash == pytest.approx(cash_while_open + 101.0 - exit_.fees_usd)
    assert led.open_positions == {}
    assert trade.net_pnl_usd == pytest.approx(1.0 - 0.1 - 0.101, rel=1e-9)


def test_released_capital_is_immediately_reusable() -> None:
    led = ledger(200.0)
    pos = open_at(led, "p1", 100.0, 1.9)  # $190 of a $200 account
    assert not led.can_afford(150.0)
    led.close(pos, result(100.0, 1.9), ExitReason.TIME_STOP, TS + 1_000)
    assert led.can_afford(150.0)


def test_can_afford_tracks_cash_not_equity() -> None:
    led = ledger(200.0)
    open_at(led, "p1", 100.0, 1.5)
    assert led.equity > led.cash
    assert not led.can_afford(160.0)
    assert led.can_afford(40.0)


def test_opening_an_unfilled_order_is_refused() -> None:
    led = ledger()
    rejected = OrderResult(
        accepted=False,
        order_type=OrderType.MARKET,
        requested_qty=1.0,
        filled_qty=0.0,
        avg_price=0.0,
        fees_usd=0.0,
        slippage_bps=0.0,
    )
    with pytest.raises(ValueError):
        led.open(position(), rejected)
    with pytest.raises(ValueError):
        led.close(open_at(led, "p1", 100.0, 1.0), rejected, ExitReason.STOP_LOSS, TS)


def test_duplicate_position_id_is_refused() -> None:
    led = ledger()
    open_at(led, "p1", 100.0, 1.0)
    with pytest.raises(ValueError):
        led.open(position("p1"), result(100.0, 1.0))


def test_open_adopts_the_actual_fill() -> None:
    """A partial or slipped entry is what we own, not what we asked for."""
    led = ledger()
    pos = position(qty=2.0)
    entry = result(100.05, 1.2)
    led.open(pos, entry)
    assert pos.qty == pytest.approx(1.2)
    assert pos.entry_price == pytest.approx(100.05)
    assert pos.notional_usd == pytest.approx(120.06)
    assert pos.ledger == "base"


def test_partial_exit_keeps_the_remainder_open() -> None:
    led = ledger()
    pos = open_at(led, "p1", 100.0, 2.0)
    trade = led.close(pos, result(101.0, 0.5), ExitReason.TAKE_PROFIT, TS + 1_000)
    assert trade.qty == pytest.approx(0.5)
    assert led.open_positions["p1"].qty == pytest.approx(1.5)
    # gross 0.50, exit fee 10 bps on $50.50, entry fee $0.20 apportioned 25%
    assert trade.net_pnl_usd == pytest.approx(0.5 - 0.05 - 0.0505, rel=1e-9)
    assert led.open_positions["p1"].entry_fees_usd == pytest.approx(0.15)


# --------------------------------------------------------------------------- #
# Equity identity
# --------------------------------------------------------------------------- #


def test_equity_is_cash_plus_marked_positions() -> None:
    led = ledger()
    pos = open_at(led, "p1", 100.0, 1.0)
    led.mark({"BTCUSD": 104.0}, TS + 1_000)
    assert led.equity == pytest.approx(led.cash + 104.0 * pos.qty)
    assert led.unrealized_pnl == pytest.approx(4.0)
    assert led.exposure_usd == pytest.approx(104.0)


def test_equity_is_continuous_across_a_close() -> None:
    led = ledger()
    pos = open_at(led, "p1", 100.0, 1.0)
    led.mark({"BTCUSD": 101.0}, TS + 1_000)
    before = led.equity
    # Closing at the marked price only costs the exit fee.
    exit_ = result(101.0, 1.0)
    led.close(pos, exit_, ExitReason.TAKE_PROFIT, TS + 2_000)
    assert led.equity == pytest.approx(before - exit_.fees_usd)


def test_equity_points_appended_on_mark_and_on_close() -> None:
    led = ledger()
    assert led.equity_points() == []
    pos = open_at(led, "p1", 100.0, 1.0)
    assert led.equity_points() == []  # opening moves cash into inventory, not equity
    led.mark({"BTCUSD": 100.0}, TS + 1_000)
    led.mark({"BTCUSD": 102.0}, TS + 2_000)
    led.close(pos, result(102.0, 1.0), ExitReason.TAKE_PROFIT, TS + 3_000)
    points = led.equity_points()
    assert [ts for ts, _ in points] == [TS + 1_000, TS + 2_000, TS + 3_000]
    assert points[1][1] > points[0][1]


def test_realized_pnl_and_realized_since() -> None:
    led = ledger()
    pos = open_at(led, "p1", 100.0, 1.0)
    led.close(pos, result(102.0, 1.0), ExitReason.TAKE_PROFIT, TS + 1_000)
    pos2 = open_at(led, "p2", 100.0, 1.0)
    led.close(pos2, result(99.0, 1.0), ExitReason.STOP_LOSS, TS + 5_000)
    assert led.realized_pnl == pytest.approx(sum(t.net_pnl_usd for t in led.closed_trades))
    assert led.realized_since(TS + 2_000) == pytest.approx(led.closed_trades[1].net_pnl_usd)
    assert led.realized_since(TS + 9_000) == 0.0


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #


def close_pnl(led: Ledger, pid: str, pnl: float, *, ts_ms: int, strategy: str = "momentum_scalp",
              hold_ms: int = 60_000) -> None:
    """Open at $100 and close at a price that produces exactly ``pnl`` gross."""
    pos = position(pid, entry=100.0, qty=1.0, entry_ms=ts_ms, strategy=strategy)
    led.open(pos, result(100.0, 1.0, fee_bps=0.0, slippage_bps=0.0))
    led.close(
        pos,
        result(100.0 + pnl, 1.0, fee_bps=0.0, slippage_bps=0.0),
        ExitReason.TAKE_PROFIT if pnl > 0 else ExitReason.STOP_LOSS,
        ts_ms + hold_ms,
    )


def test_profit_factor_expectancy_and_averages() -> None:
    led = ledger(1_000.0)
    for i, pnl in enumerate([3.0, -1.0, 5.0, -2.0, 4.0]):
        close_pnl(led, f"p{i}", pnl, ts_ms=TS + i * 100_000)
    s = led.stats()
    assert s.trades == 5
    assert (s.wins, s.losses) == (3, 2)
    assert s.net_pnl == pytest.approx(9.0)
    assert s.avg_win == pytest.approx(4.0)       # (3+5+4)/3
    assert s.avg_loss == pytest.approx(1.5)      # |(-1-2)|/2, a positive magnitude
    assert s.profit_factor == pytest.approx(12.0 / 3.0)
    assert s.expectancy == pytest.approx(9.0 / 5.0)
    assert s.expectancy == pytest.approx(s.win_rate * s.avg_win - (1 - s.win_rate) * s.avg_loss)
    assert s.win_rate == pytest.approx(0.6)
    assert s.avg_holding_seconds == pytest.approx(60.0)


def test_profit_factor_with_no_losses_is_infinite() -> None:
    led = ledger()
    close_pnl(led, "p0", 2.0, ts_ms=TS)
    assert led.stats().profit_factor == float("inf")


def test_stats_on_an_empty_book_are_zeroed() -> None:
    s = ledger().stats()
    assert (s.trades, s.net_pnl, s.profit_factor, s.expectancy, s.max_drawdown) == (0, 0.0, 0.0, 0.0, 0.0)


def test_max_drawdown_is_peak_to_trough_in_usd() -> None:
    led = ledger(1_000.0)
    for i, equity_delta in enumerate([100.0, -30.0, -50.0, 40.0, -10.0]):
        close_pnl(led, f"p{i}", equity_delta, ts_ms=TS + i * 100_000)
    # Equity path: 1100, 1070, 1020, 1060, 1050.  Peak 1100, trough 1020.
    assert [round(e) for _, e in led.equity_points()] == [1100, 1070, 1020, 1060, 1050]
    assert led.stats().max_drawdown == pytest.approx(80.0)


def test_max_drawdown_is_zero_on_a_monotonic_curve() -> None:
    led = ledger()
    for i in range(4):
        close_pnl(led, f"p{i}", 1.0, ts_ms=TS + i * 10_000)
    assert led.stats().max_drawdown == pytest.approx(0.0)


def test_stats_filter_by_strategy_and_window() -> None:
    led = ledger()
    close_pnl(led, "a", 2.0, ts_ms=TS, strategy="momentum_scalp")
    close_pnl(led, "b", -1.0, ts_ms=TS + 100_000, strategy="mean_reversion")
    close_pnl(led, "c", 3.0, ts_ms=TS + 200_000, strategy="momentum_scalp")

    momentum = led.stats(strategy="momentum_scalp")
    assert momentum.trades == 2
    assert momentum.net_pnl == pytest.approx(5.0)

    late = led.stats(since_ms=TS + 170_000)  # after b closed at TS+160_000
    assert late.trades == 1
    assert late.net_pnl == pytest.approx(3.0)


def test_capital_turnover_is_closed_notional_over_starting_equity() -> None:
    led = ledger(500.0)
    for i in range(5):
        close_pnl(led, f"p{i}", 1.0, ts_ms=TS + i * 10_000)  # $100 notional each
    assert led.stats().capital_turnover == pytest.approx(500.0 / 500.0)


def test_capital_turnover_is_per_day_when_a_window_is_given() -> None:
    led = ledger(500.0)
    day = 86_400_000
    for i in range(10):  # $1000 of notional over exactly 2 days
        close_pnl(led, f"p{i}", 0.5, ts_ms=TS + i * (day // 5), hold_ms=0)
    since = TS
    end = max(t.exit_ms for t in led.closed_trades)
    days = (end - since) / day
    assert led.stats(since_ms=since).capital_turnover == pytest.approx(
        (1_000.0 / 500.0) / days
    )


# --------------------------------------------------------------------------- #
# Loss streak
# --------------------------------------------------------------------------- #


def test_loss_streak_counts_back_from_the_most_recent_trade() -> None:
    led = ledger()
    for i, pnl in enumerate([-1.0, 2.0, -1.0, -1.0, -1.0]):
        close_pnl(led, f"p{i}", pnl, ts_ms=TS + i * 10_000)
    assert led.loss_streak() == 3


def test_a_win_resets_the_loss_streak() -> None:
    led = ledger()
    for i, pnl in enumerate([-1.0, -1.0, 1.0]):
        close_pnl(led, f"p{i}", pnl, ts_ms=TS + i * 10_000)
    assert led.loss_streak() == 0
    assert ledger().loss_streak() == 0
