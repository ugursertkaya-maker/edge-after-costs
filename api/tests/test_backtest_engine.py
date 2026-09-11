"""Harness-correctness tests.

These do not test whether the strategies make money — the doubles have no edge
worth measuring.  They test the three things a backtester lies about: same-bar
fills, optimistic intrabar tie-breaks, and uncharged costs.  Everything is
seeded and offline.
"""

from __future__ import annotations

import pytest

from app.backtest.engine import (
    MIN_SPREAD_BPS,
    BacktestConfig,
    build_components,
    combine_stats,
    half_spread_bps_for,
    real_components_available,
    run_backtest,
    slice_bars,
    synth_book,
    synth_symbol_info,
    synth_trades,
)
from app.backtest.report import format_result
from app.types import ExitReason, PerfStats, Side
from fixtures.doubles import (
    AlwaysLongStrategy,
    DoubleLedger,
    NeverStrategy,
    ScriptedStrategy,
    make_bars,
    make_components,
    range_bars,
    single_bar_series,
    trend_bars,
)

MINUTE = 60_000
T0 = 1_700_000_000_000


def components_capturing(cfg, strategies):
    """Components whose ledger instance we can inspect after the run."""
    created: list[DoubleLedger] = []
    comps = make_components(cfg, strategies=strategies)

    def factory(*args, **kwargs):
        ledger = DoubleLedger(*args, **kwargs)
        created.append(ledger)
        return ledger

    comps.ledger_factory = factory
    return comps, created


def flat_then(specs, *, warmup: int = 10, price: float = 100.0):
    """``warmup`` dead-flat bars, then the hand-written OHLC bars in ``specs``."""
    flat = [(price, price * 1.0005, price * 0.9995, price)] * warmup
    return single_bar_series("BTCUSD", flat + list(specs), start_ms=T0)


# --------------------------------------------------------------------------- #
# Lie #1: same-bar fills
# --------------------------------------------------------------------------- #


def test_latency_bars_below_one_is_rejected():
    cfg = BacktestConfig(latency_bars=0)
    with pytest.raises(ValueError, match="same-bar"):
        run_backtest({"BTCUSD": trend_bars("BTCUSD", 40)}, cfg)


def test_entry_never_fills_on_the_signal_bar():
    bars = flat_then([(100.0, 100.6, 99.4, 100.2)] * 6)
    signal_bar = bars[9]
    fill_bar = bars[10]
    cfg = BacktestConfig(fee_bps_per_side=1.0, spread_bps=2.0, slippage_bps=0.5)
    comps, ledgers = components_capturing(
        cfg, [ScriptedStrategy(fire_at_ms=[signal_bar.close_ms], target_bps=400, stop_bps=200)]
    )
    run_backtest({"BTCUSD": bars}, cfg, components=comps)

    ledger = ledgers[0]
    positions = list(ledger.open_positions.values())
    trades = ledger.closed_trades
    assert positions or trades, "the scripted signal never became a position"
    entry_ms = positions[0].entry_ms if positions else trades[0].entry_ms

    assert entry_ms > signal_bar.close_ms, "filled on the bar that produced the signal"
    assert entry_ms == fill_bar.open_ms, "did not fill on the bar latency_bars later"


def test_latency_of_three_bars_delays_the_fill_by_three_bars():
    bars = flat_then([(100.0, 100.6, 99.4, 100.2)] * 8)
    signal_bar = bars[9]
    cfg = BacktestConfig(
        latency_bars=3, fee_bps_per_side=1.0, spread_bps=2.0, slippage_bps=0.5
    )
    comps, ledgers = components_capturing(
        cfg, [ScriptedStrategy(fire_at_ms=[signal_bar.close_ms], target_bps=400, stop_bps=200)]
    )
    run_backtest({"BTCUSD": bars}, cfg, components=comps)

    ledger = ledgers[0]
    entries = [p.entry_ms for p in ledger.open_positions.values()] + [
        t.entry_ms for t in ledger.closed_trades
    ]
    assert entries == [bars[12].open_ms]


def test_a_signal_with_no_future_bar_to_fill_on_is_dropped():
    """The last bar can produce a signal but never a fill."""
    bars = flat_then([(100.0, 100.6, 99.4, 100.2)])
    last = bars[-1]
    cfg = BacktestConfig(fee_bps_per_side=1.0, spread_bps=2.0)
    comps, ledgers = components_capturing(
        cfg, [ScriptedStrategy(fire_at_ms=[last.close_ms], target_bps=400, stop_bps=200)]
    )
    result = run_backtest({"BTCUSD": bars}, cfg, components=comps)
    assert result.entries_filled == 0
    assert ledgers[0].open_positions == {}


# --------------------------------------------------------------------------- #
# Lie #2: optimistic intrabar tie-break
# --------------------------------------------------------------------------- #


def _one_trade(specs, *, target_bps=400.0, stop_bps=200.0):
    bars = flat_then(specs)
    cfg = BacktestConfig(fee_bps_per_side=1.0, spread_bps=2.0, slippage_bps=0.5)
    comps, ledgers = components_capturing(
        cfg,
        [
            ScriptedStrategy(
                fire_at_ms=[bars[9].close_ms], target_bps=target_bps, stop_bps=stop_bps
            )
        ],
    )
    run_backtest({"BTCUSD": bars}, cfg, components=comps)
    return ledgers[0]


def test_stop_wins_when_one_bar_contains_both_stop_and_target():
    # Entry ~100 on bar 10's open; TP ~ +4%, SL ~ -2%.  Bar 11 sweeps both.
    ledger = _one_trade(
        [
            (100.0, 100.2, 99.8, 100.0),   # bar 10: the entry bar
            (100.0, 105.0, 97.0, 100.0),   # bar 11: touches +5% AND -3%
            (100.0, 100.2, 99.8, 100.0),
        ]
    )
    assert len(ledger.closed_trades) == 1
    trade = ledger.closed_trades[0]
    assert trade.exit_reason is ExitReason.STOP_LOSS, "target was taken ahead of the stop"
    assert trade.net_pnl_usd < 0


def test_target_is_taken_when_the_bar_only_reaches_the_target():
    ledger = _one_trade(
        [
            (100.0, 100.2, 99.8, 100.0),
            (100.0, 105.0, 99.5, 104.5),   # only the upside is touched
            (100.0, 100.2, 99.8, 100.0),
        ]
    )
    assert len(ledger.closed_trades) == 1
    trade = ledger.closed_trades[0]
    assert trade.exit_reason is ExitReason.TAKE_PROFIT
    assert trade.net_pnl_usd > 0


def test_a_target_fills_at_the_target_not_at_the_bar_high():
    ledger = _one_trade(
        [
            (100.0, 100.2, 99.8, 100.0),
            (100.0, 130.0, 99.5, 129.0),   # an absurd spike well past the target
            (100.0, 100.2, 99.8, 100.0),
        ]
    )
    trade = ledger.closed_trades[0]
    assert trade.exit_reason is ExitReason.TAKE_PROFIT
    # ~ +4% target, not the +30% high.  Allow the exit spread/slippage haircut.
    assert trade.exit_price < trade.entry_price * 1.06


def test_a_gap_through_the_stop_fills_at_the_gap_not_at_the_stop():
    ledger = _one_trade(
        [
            (100.0, 100.2, 99.8, 100.0),
            (90.0, 90.5, 89.0, 90.0),      # opened 10% below the stop
            (100.0, 100.2, 99.8, 100.0),
        ]
    )
    trade = ledger.closed_trades[0]
    assert trade.exit_reason is ExitReason.STOP_LOSS
    assert trade.exit_price < 91.0, "a gap must not fill politely at the stop level"


def test_a_position_is_not_exited_on_its_own_entry_bar():
    """The entry bar sweeps both levels; the exit must still wait for bar 11."""
    ledger = _one_trade(
        [
            (100.0, 108.0, 92.0, 100.0),   # bar 10: entry bar, sweeps TP and SL
            (100.0, 100.2, 97.0, 97.5),    # bar 11: the first legitimate stop
            (100.0, 100.2, 99.8, 100.0),
        ]
    )
    assert len(ledger.closed_trades) == 1
    trade = ledger.closed_trades[0]
    assert trade.exit_reason is ExitReason.STOP_LOSS
    assert trade.entry_ms == T0 + 10 * MINUTE
    assert trade.exit_ms >= T0 + 11 * MINUTE, "exited on the bar it entered on"


# --------------------------------------------------------------------------- #
# Lie #3: uncharged costs
# --------------------------------------------------------------------------- #


def _run_costs(fee_bps: float, slippage_bps: float, spread_bps: float = 3.0):
    bars = {"BTCUSD": trend_bars("BTCUSD", 260, seed=3)}
    cfg = BacktestConfig(
        fee_bps_per_side=fee_bps, slippage_bps=slippage_bps, spread_bps=spread_bps
    )
    return run_backtest(bars, cfg, label="costs", components=make_components(cfg))


def test_fees_are_charged_and_pull_net_below_gross():
    result = _run_costs(fee_bps=8.0, slippage_bps=1.0)
    s = result.stats
    assert s.trades > 0
    assert s.fees > 0
    assert s.net_pnl < s.gross_pnl
    assert s.net_pnl == pytest.approx(s.gross_pnl - s.fees, rel=1e-9)


def test_higher_fees_produce_a_worse_result():
    cheap = _run_costs(fee_bps=1.0, slippage_bps=1.0)
    dear = _run_costs(fee_bps=30.0, slippage_bps=1.0)
    assert dear.stats.fees > cheap.stats.fees
    assert dear.stats.net_pnl < cheap.stats.net_pnl
    assert dear.ending_equity < cheap.ending_equity


def test_higher_slippage_produces_a_worse_result():
    tight = _run_costs(fee_bps=5.0, slippage_bps=0.5)
    wide = _run_costs(fee_bps=5.0, slippage_bps=25.0)
    assert wide.stats.slippage > tight.stats.slippage
    assert wide.stats.gross_pnl < tight.stats.gross_pnl
    assert wide.stats.net_pnl < tight.stats.net_pnl


def test_a_wider_spread_produces_a_worse_result():
    tight = _run_costs(fee_bps=5.0, slippage_bps=1.0, spread_bps=1.0)
    wide = _run_costs(fee_bps=5.0, slippage_bps=1.0, spread_bps=5.5)
    assert wide.stats.net_pnl < tight.stats.net_pnl


# --------------------------------------------------------------------------- #
# Book / trade synthesis
# --------------------------------------------------------------------------- #


def test_estimated_spread_is_never_narrower_than_the_floor():
    dead_flat = make_bars("BTCUSD", 1, range_bps=0.0, noise_bps=0.0)[0]
    assert half_spread_bps_for(dead_flat, None) * 2 >= MIN_SPREAD_BPS


def test_estimated_spread_widens_with_the_bar_range():
    calm = make_bars("BTCUSD", 1, range_bps=10.0, noise_bps=0.0, seed=1)[0]
    wild = make_bars("BTCUSD", 1, range_bps=900.0, noise_bps=0.0, seed=1)[0]
    assert half_spread_bps_for(wild, None) > half_spread_bps_for(calm, None)


def test_pinned_spread_is_honoured_exactly():
    bar = make_bars("BTCUSD", 1, range_bps=900.0, seed=1)[0]
    assert half_spread_bps_for(bar, 6.0) == 3.0


def test_synth_book_is_valid_and_never_crosses():
    bar = make_bars("BTCUSD", 1, seed=2)[0]
    book = synth_book(bar)
    assert book.is_valid()
    assert book.best_ask > book.best_bid
    assert book.mid == pytest.approx(bar.close)
    assert [lv.price for lv in book.bids] == sorted(
        (lv.price for lv in book.bids), reverse=True
    )
    assert [lv.price for lv in book.asks] == sorted(lv.price for lv in book.asks)
    assert book.depth_usd("bid") > 0 and book.depth_usd("ask") > 0


def test_synth_trades_respect_the_taker_buy_split():
    from random import Random

    bar = make_bars("BTCUSD", 1, taker_buy_frac=1.0, seed=4)[0]
    trades = synth_trades(bar, Random(1), count=8)
    assert trades and all(t.aggressor is Side.LONG for t in trades)
    assert all(bar.low <= t.price <= bar.high for t in trades)
    assert all(bar.open_ms <= t.ts_ms <= bar.close_ms for t in trades)


def test_synth_symbol_info_rounds_sanely():
    info = synth_symbol_info("BTCUSD", 105_000.0)
    assert info.quote_asset == "USD" and info.base_asset == "BTC"
    assert 0 < info.tick_size <= 0.01
    assert info.floor_qty(1.23456789) <= 1.23456789


# --------------------------------------------------------------------------- #
# Degenerate inputs
# --------------------------------------------------------------------------- #


def test_a_strategy_with_no_signals_produces_no_trades_and_no_crash():
    cfg = BacktestConfig()
    never = NeverStrategy()
    comps = make_components(cfg, strategies=[never])
    result = run_backtest({"BTCUSD": trend_bars("BTCUSD", 120)}, cfg, components=comps)
    assert never.calls > 0, "the strategy was never even consulted"
    assert result.trades == []
    assert result.stats.trades == 0
    assert result.per_strategy == {}
    assert result.ending_equity == pytest.approx(result.starting_equity)
    assert "NO TRADES" in format_result(result)


def test_an_empty_strategy_set_produces_no_trades():
    cfg = BacktestConfig(strategies=())
    comps = make_components(cfg, strategies=[AlwaysLongStrategy()])
    result = run_backtest({"BTCUSD": trend_bars("BTCUSD", 80)}, cfg, components=comps)
    assert result.trades == []
    assert result.signals_generated == 0


def test_empty_input_does_not_crash():
    cfg = BacktestConfig()
    result = run_backtest({}, cfg, components=make_components(cfg))
    assert result.trades == []
    assert result.bars_processed == 0
    assert format_result(result)


def test_a_single_bar_series_does_not_crash():
    cfg = BacktestConfig()
    bars = make_bars("BTCUSD", 1)
    result = run_backtest({"BTCUSD": bars}, cfg, components=make_components(cfg))
    assert result.trades == []


# --------------------------------------------------------------------------- #
# Ledger integration
# --------------------------------------------------------------------------- #


def test_capital_is_recycled_so_more_trades_than_capital_allows_can_run():
    cfg = BacktestConfig(fee_bps_per_side=2.0, spread_bps=3.0, slippage_bps=1.0)
    comps, ledgers = components_capturing(cfg, [AlwaysLongStrategy()])
    result = run_backtest({"BTCUSD": trend_bars("BTCUSD", 400, seed=5)}, cfg, components=comps)
    ledger = ledgers[0]
    turnover = sum(t.notional_usd for t in result.trades)
    assert result.stats.trades >= 3
    assert turnover > cfg.starting_equity, "capital was never recycled"
    assert ledger.cash > 0


def test_pending_entries_count_against_the_concurrency_cap():
    """An approved-but-unfilled entry is committed risk.

    Regression: with a one-bar latency, two symbols could each be approved
    while zero positions were open and then both fill, breaching the cap.
    """
    bars = flat_then([(100.0, 100.6, 99.4, 100.2)] * 6)
    eth = [
        type(b)(**{**b.__dict__, "symbol": "ETHUSD"}) for b in bars
    ]
    fire = [bars[9].close_ms]
    cfg = BacktestConfig(
        max_concurrent=1, fee_bps_per_side=1.0, spread_bps=2.0, slippage_bps=0.5
    )
    comps, ledgers = components_capturing(
        cfg, [ScriptedStrategy(fire_at_ms=fire, target_bps=400, stop_bps=200)]
    )
    result = run_backtest({"BTCUSD": bars, "ETHUSD": eth}, cfg, components=comps)
    assert result.entries_approved == 1, "a second entry was approved during the latency gap"
    assert len(ledgers[0].open_positions) + len(ledgers[0].closed_trades) == 1


def test_concurrent_position_cap_is_respected():
    cfg = BacktestConfig(max_concurrent=1, fee_bps_per_side=2.0, spread_bps=3.0)
    comps, ledgers = components_capturing(cfg, [AlwaysLongStrategy()])
    symbols = {
        "BTCUSD": trend_bars("BTCUSD", 200, seed=6),
        "ETHUSD": trend_bars("ETHUSD", 200, seed=7),
        "SOLUSD": trend_bars("SOLUSD", 200, seed=8),
    }
    run_backtest(symbols, cfg, components=comps)
    assert len(ledgers[0].open_positions) <= 1


def test_equity_curve_is_chronological_and_starts_at_starting_equity():
    cfg = BacktestConfig(fee_bps_per_side=2.0, spread_bps=3.0)
    result = run_backtest(
        {"BTCUSD": trend_bars("BTCUSD", 150)}, cfg, components=make_components(cfg)
    )
    curve = result.equity_curve
    assert curve
    assert [t for t, _ in curve] == sorted(t for t, _ in curve)
    assert curve[0][1] == pytest.approx(cfg.starting_equity)
    assert curve[-1][1] == pytest.approx(result.ending_equity)


def test_per_strategy_stats_sum_to_the_overall_stats():
    cfg = BacktestConfig(fee_bps_per_side=3.0, spread_bps=3.0)
    comps = make_components(
        cfg, strategies=[AlwaysLongStrategy("a"), AlwaysLongStrategy("b", target_bps=200)]
    )
    result = run_backtest(
        {"BTCUSD": trend_bars("BTCUSD", 300, seed=9)}, cfg, components=comps
    )
    assert result.stats.trades == sum(s.trades for s in result.per_strategy.values())
    assert result.stats.net_pnl == pytest.approx(
        sum(s.net_pnl for s in result.per_strategy.values())
    )


def test_results_are_deterministic_for_a_fixed_seed():
    def once():
        cfg = BacktestConfig(seed=42, fee_bps_per_side=4.0)
        return run_backtest(
            {"BTCUSD": trend_bars("BTCUSD", 200), "ETHUSD": range_bars("ETHUSD", 200)},
            cfg,
            components=make_components(cfg),
        )

    a, b = once(), once()
    assert a.ending_equity == b.ending_equity
    assert [t.id for t in a.trades] == [t.id for t in b.trades]
    assert a.stats == b.stats


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def test_slice_bars_is_half_open():
    bars = make_bars("BTCUSD", 10)
    kept = slice_bars({"BTCUSD": bars}, bars[2].open_ms, bars[5].open_ms)["BTCUSD"]
    assert kept == bars[2:5]


def test_combine_stats_pools_counts_and_takes_the_worst_drawdown():
    a = PerfStats(trades=2, wins=2, losses=0, net_pnl=10.0, avg_win=5.0, max_drawdown=1.0)
    b = PerfStats(trades=3, wins=1, losses=2, net_pnl=-6.0, avg_win=4.0, avg_loss=-5.0,
                  max_drawdown=4.0)
    c = combine_stats([a, b])
    assert c.trades == 5 and c.wins == 3 and c.losses == 2
    assert c.net_pnl == pytest.approx(4.0)
    assert c.max_drawdown == 4.0
    assert c.avg_win == pytest.approx((5.0 * 2 + 4.0) / 3)
    assert c.expectancy == pytest.approx(4.0 / 5)


def test_combine_stats_of_nothing_is_empty():
    assert combine_stats([]) == PerfStats()


# --------------------------------------------------------------------------- #
# Integration with the real production stack
# --------------------------------------------------------------------------- #

real_stack = pytest.mark.skipif(
    not real_components_available(),
    reason="the production decision modules are not importable",
)


@real_stack
def test_build_components_wires_the_real_production_objects():
    from app.exits import ExitEngine
    from app.features import FeatureEngine
    from app.ranker import OpportunityRanker
    from app.regime import RegimeClassifier
    from app.risk import RiskEngine
    from app.sim.ledger import Ledger
    from app.sim.simulator import PaperSimulator
    from app.strategies import STRATEGY_NAMES

    c = build_components(BacktestConfig())
    assert isinstance(c.features, FeatureEngine)
    assert isinstance(c.regime, RegimeClassifier)
    assert isinstance(c.ranker, OpportunityRanker)
    assert isinstance(c.risk, RiskEngine)
    assert isinstance(c.exits, ExitEngine)
    assert isinstance(c.simulator, PaperSimulator)
    assert c.ledger_factory is Ledger
    assert tuple(s.name for s in c.strategies) == STRATEGY_NAMES


@real_stack
def test_the_real_stack_produces_signals_and_closed_trades():
    """End-to-end on the real modules: bars in, priced round trips out."""
    cfg = BacktestConfig(fee_bps_per_side=1.0, slippage_bps=1.0)  # tier-0 taker
    bars = {"BTCUSD": trend_bars("BTCUSD", 600, start_price=60_000.0)}
    result = run_backtest(bars, cfg, label="real-stack")

    assert result.signals_generated > 0, "no real strategy fired through the harness"
    assert result.stats.trades > 0, "no signal survived into a closed round trip"
    assert result.stats.fees > 0
    assert result.stats.net_pnl == pytest.approx(
        sum(t.net_pnl_usd for t in result.trades)
    )
    for trade in result.trades:
        assert trade.exit_ms > trade.entry_ms
        assert trade.strategy in result.per_strategy
    assert format_result(result, include_trades=3)


@real_stack
def test_the_real_stack_still_refuses_same_bar_fills():
    cfg = BacktestConfig(fee_bps_per_side=1.0, slippage_bps=1.0)
    bars = trend_bars("BTCUSD", 600, start_price=60_000.0)
    opens = {b.open_ms for b in bars}
    closes = {b.close_ms for b in bars}
    result = run_backtest({"BTCUSD": bars}, cfg, label="real-latency")
    assert result.trades
    for trade in result.trades:
        # Entries land on a bar open, never on the close that produced them.
        assert trade.entry_ms in opens
        assert trade.entry_ms not in closes


@real_stack
def test_the_real_stack_charges_more_at_standard_fees_than_at_tier_zero():
    bars = {"BTCUSD": trend_bars("BTCUSD", 600, start_price=60_000.0)}
    cheap = run_backtest(bars, BacktestConfig(fee_bps_per_side=1.0, slippage_bps=1.0))
    dear = run_backtest(bars, BacktestConfig(fee_bps_per_side=10.0, slippage_bps=1.0))
    # At 10 bps/side the ranker should reject setups it accepts at 1 bps.
    assert dear.stats.trades <= cheap.stats.trades
    assert dear.ending_equity <= cheap.ending_equity
