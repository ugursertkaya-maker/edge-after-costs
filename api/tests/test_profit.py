"""NET target engine: solving backwards from money to price.

The property under test throughout is the one the V2 spec exists to enforce —
*a gross gain is not profit*.  Every assertion here is about the gap between the
two, and about that gap being on the correct side.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.config import ExecConfig, FeeConfig, ProfitConfig, Settings
from app.profit import FeeProvider, NetTargetEngine
from app.sim.simulator import PaperSimulator
from app.types import (
    BookLevel,
    OrderBook,
    OrderRequest,
    OrderType,
    Side,
    Trade,
)
from fixtures.market import BASE_MS, make_book, make_features, make_info, make_signal

SYMBOL = "BTCUSD"
INFO = make_info(SYMBOL, min_notional=1.0, step_size=0.000001, min_qty=0.000001)


def settings(**over: object) -> Settings:
    return dataclasses.replace(Settings(), **over)  # type: ignore[arg-type]


def engine(**over: object) -> NetTargetEngine:
    return NetTargetEngine(settings=settings(**over))


def build(eng: NetTargetEngine, **over: object):
    kwargs: dict[str, object] = dict(
        symbol=SYMBOL,
        side=Side.LONG,
        entry_price=100.0,
        notional_usd=100.0,
        qty=1.0,
        book=make_book(mid=100.0, spread_bps=2.0),
        info=INFO,
        features=make_features(atr_bps=50.0),
        signal=make_signal(target_bps=65.0, stop_bps=20.0),
    )
    kwargs.update(over)
    return eng.build(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The fee seam
# --------------------------------------------------------------------------- #


def test_fee_provider_returns_the_per_symbol_rate_not_an_average() -> None:
    fees = FeeProvider(settings=settings())
    # BTCUSD is on the published Tier-0 list; a random pair is not.
    assert fees.maker_bps("BTCUSD") == pytest.approx(0.0)
    assert fees.taker_bps("BTCUSD") == pytest.approx(1.0)
    assert fees.taker_bps("NOTATIER0PAIR") == pytest.approx(10.0)


def test_fee_provider_refresh_is_a_no_op_today() -> None:
    """The seam exists; the authenticated call deliberately does not."""
    fees = FeeProvider(settings=settings())
    before = (fees.maker_bps(SYMBOL), fees.taker_bps(SYMBOL))
    fees.refresh()
    assert (fees.maker_bps(SYMBOL), fees.taker_bps(SYMBOL)) == before


def test_maker_legs_pay_neither_the_fee_nor_the_spread() -> None:
    eng = engine()
    taker = eng.estimate_costs(
        symbol=SYMBOL, notional_usd=100.0, book=make_book(mid=100.0, spread_bps=4.0),
        entry_is_maker=False, exit_is_maker=False,
        slippage_bps_entry=0.0, slippage_bps_exit=0.0,
    )
    maker = eng.estimate_costs(
        symbol=SYMBOL, notional_usd=100.0, book=make_book(mid=100.0, spread_bps=4.0),
        entry_is_maker=True, exit_is_maker=True,
        slippage_bps_entry=0.0, slippage_bps_exit=0.0,
    )
    assert taker.spread_cost_usd == pytest.approx(100.0 * 4.0 / 10_000.0)
    assert maker.spread_cost_usd == pytest.approx(0.0)
    assert maker.fees_usd == pytest.approx(0.0)          # Tier-0 maker is 0%
    assert maker.total_usd < taker.total_usd


def test_every_cost_line_is_itemised_not_netted_into_one_number() -> None:
    eng = engine(profit=ProfitConfig(latency_buffer_bps=1.5))
    costs = eng.estimate_costs(
        symbol="NOTATIER0PAIR", notional_usd=200.0,
        book=make_book(mid=100.0, spread_bps=3.0),
        entry_is_maker=False, exit_is_maker=False,
        slippage_bps_entry=2.0, slippage_bps_exit=4.0,
    )
    assert costs.entry_fee_usd == pytest.approx(200.0 * 10.0 / 10_000.0)
    assert costs.exit_fee_usd == pytest.approx(200.0 * 10.0 / 10_000.0)
    assert costs.spread_cost_usd == pytest.approx(200.0 * 3.0 / 10_000.0)
    assert costs.entry_slippage_usd == pytest.approx(200.0 * 2.0 / 10_000.0)
    assert costs.exit_slippage_usd == pytest.approx(200.0 * 4.0 / 10_000.0)
    assert costs.latency_buffer_usd == pytest.approx(200.0 * 1.5 / 10_000.0)
    assert costs.total_usd == pytest.approx(
        costs.fees_usd + costs.slippage_usd
        + costs.spread_cost_usd + costs.latency_buffer_usd
    )


# --------------------------------------------------------------------------- #
# Gross > net, always
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("profile", ("micro", "base", "fast", "extended"))
@pytest.mark.parametrize("notional", (10.0, 15.0, 25.0, 50.0, 100.0, 200.0))
def test_gross_move_required_always_exceeds_the_net_target(
    profile: str, notional: float
) -> None:
    """The whole reason NetTargets exists: the market has to give us more than
    we intend to keep, because the difference is paid to the exchange."""
    eng = engine()
    t = build(eng, profile=profile, notional_usd=notional, qty=notional / 100.0)
    net_target_bps = t.net_tp_pct * 100.0
    assert t.gross_move_required_bps > net_target_bps
    assert t.costs.total_usd > 0.0
    # And the excess is exactly the cost of the round trip, up to tick rounding.
    excess_usd = (t.gross_move_required_bps - net_target_bps) / 10_000.0 * notional
    assert excess_usd >= t.costs.total_usd - 1e-9


def test_a_fixed_dollar_target_is_the_incoherence_being_fixed() -> None:
    """$0.50 is 5% of a $10 position and 0.25% of a $200 one.

    The same NET *percentage* therefore has to produce wildly different dollar
    targets — and the same fraction of notional in both.
    """
    eng = engine()
    small = build(eng, notional_usd=10.0, qty=0.1, profile="base")
    large = build(eng, notional_usd=200.0, qty=2.0, profile="base")

    small_usd = (small.net_tp_price - 100.0) * 0.1
    large_usd = (large.net_tp_price - 100.0) * 2.0

    assert small_usd < 0.15 < 1.0 < large_usd
    assert large_usd > 15.0 * small_usd
    # Coherent where it matters: the same fraction of the capital at risk.
    assert small_usd / 10.0 == pytest.approx(large_usd / 200.0, abs=2e-4)

    # The incoherent alternative, stated explicitly.
    assert 0.50 / 10.0 * 100.0 == pytest.approx(5.0)
    assert 0.50 / 200.0 * 100.0 == pytest.approx(0.25)


def test_rounding_is_never_in_our_favour() -> None:
    coarse = dataclasses.replace(INFO, tick_size=0.5)
    eng = engine()
    t = build(eng, info=coarse)
    exact = 100.0 + (t.costs.total_usd + 100.0 * t.net_tp_pct / 100.0) / 1.0
    assert t.net_tp_price >= exact          # rounded up, never down
    assert t.net_tp_price % 0.5 == pytest.approx(0.0)
    assert t.break_even_price % 0.5 == pytest.approx(0.0)


def test_short_targets_are_mirrored() -> None:
    eng = engine()
    long = build(eng, side=Side.LONG)
    short = build(eng, side=Side.SHORT, signal=make_signal(side=Side.SHORT, target_bps=65.0))
    assert short.net_tp_price < 100.0 < long.net_tp_price
    assert short.break_even_price < 100.0 < long.break_even_price
    assert short.emergency_stop_price > 100.0 > long.emergency_stop_price
    assert short.gross_move_required_bps == pytest.approx(
        long.gross_move_required_bps, abs=1.0
    )


def test_break_even_sits_between_the_entry_and_the_net_target() -> None:
    eng = engine()
    t = build(eng)
    assert 100.0 < t.break_even_price < t.net_tp_price
    assert t.stop_price < 100.0
    assert t.emergency_stop_price <= t.stop_price


# --------------------------------------------------------------------------- #
# Break-even proved by execution, not by arithmetic
# --------------------------------------------------------------------------- #


def _maker_settings() -> Settings:
    """Maker on both legs, no drift, no queue luck: every cent is a fee."""
    return dataclasses.replace(
        Settings(),
        fees=FeeConfig(model="standard", standard_maker_bps=10.0, standard_taker_bps=20.0),
        execution=ExecConfig(adverse_bps=0.0, limit_miss_ratio=0.0),
        profit=ProfitConfig(latency_buffer_bps=0.0),
    )


def _book(bid: float, ask: float) -> OrderBook:
    return OrderBook(
        symbol=SYMBOL,
        bids=[BookLevel(bid, 50.0)],
        asks=[BookLevel(ask, 50.0)],
        last_update_ms=BASE_MS,
    )


def test_closing_at_break_even_nets_about_zero_in_the_simulator() -> None:
    """Not arithmetic that mirrors the implementation — actual simulated fills.

    A maker round trip is used so the only modelled cost is the fee, which is
    the one cost a fill really pays.  If ``break_even_price`` were "entry plus a
    bit" rather than "entry plus the round trip", this comes out visibly
    positive; if it under-charged, visibly negative.
    """
    s = _maker_settings()
    sim = PaperSimulator(settings=s, rng=__import__("random").Random(11))
    eng = NetTargetEngine(settings=s)

    entry_book = _book(99.98, 100.02)
    entry_req = OrderRequest(
        symbol=SYMBOL, side=Side.LONG, order_type=OrderType.LIMIT,
        qty=1.0, limit_price=99.99, ts_ms=BASE_MS,
    )
    seller = Trade(symbol=SYMBOL, price=99.99, qty=5.0, ts_ms=BASE_MS + 10_000,
                   buyer_is_maker=False)
    entry = sim.execute(entry_req, entry_book, INFO, trades=[seller], post_only=True)
    assert entry.accepted and entry.fills[0].is_maker

    targets = eng.build(
        symbol=SYMBOL, side=Side.LONG, entry_price=entry.avg_price,
        notional_usd=entry.notional, qty=entry.filled_qty,
        book=entry_book, info=INFO,
        features=make_features(atr_bps=80.0), signal=make_signal(target_bps=60.0),
        entry_is_maker=True, exit_is_maker=True,
    )

    exit_book = _book(100.10, 100.20)
    exit_req = OrderRequest(
        symbol=SYMBOL, side=Side.SHORT, order_type=OrderType.LIMIT,
        qty=entry.filled_qty, limit_price=targets.break_even_price, ts_ms=BASE_MS + 60_000,
    )
    buyer = Trade(symbol=SYMBOL, price=targets.break_even_price, qty=5.0,
                  ts_ms=BASE_MS + 90_000, buyer_is_maker=True)
    exit_ = sim.execute(exit_req, exit_book, INFO, trades=[buyer], post_only=True)
    assert exit_.accepted and exit_.fills[0].is_maker

    gross = exit_.notional - entry.notional
    net = gross - entry.fees_usd - exit_.fees_usd
    assert abs(net) < 0.01                      # one tick on one unit
    # The gross number alone would have looked like a 20-cent winner.
    assert gross > 0.19
    assert entry.fees_usd + exit_.fees_usd == pytest.approx(gross, abs=0.01)


def test_a_maker_fill_pays_the_maker_rate_and_a_taker_fill_the_taker_rate() -> None:
    s = _maker_settings()
    sim = PaperSimulator(settings=s, rng=__import__("random").Random(3))
    bk = _book(99.98, 100.02)

    rested = sim.execute(
        OrderRequest(symbol=SYMBOL, side=Side.LONG, order_type=OrderType.LIMIT,
                     qty=1.0, limit_price=99.99, ts_ms=BASE_MS),
        bk, INFO,
        trades=[Trade(symbol=SYMBOL, price=99.99, qty=5.0, ts_ms=BASE_MS + 10_000,
                      buyer_is_maker=False)],
        post_only=True,
    )
    crossed = sim.execute(
        OrderRequest(symbol=SYMBOL, side=Side.LONG, order_type=OrderType.MARKET,
                     qty=1.0, ts_ms=BASE_MS),
        bk, INFO,
    )
    assert rested.fees_usd == pytest.approx(rested.notional * 10.0 / 10_000.0)
    assert crossed.fees_usd == pytest.approx(crossed.notional * 20.0 / 10_000.0)
    assert all(f.is_maker for f in rested.fills)
    assert not any(f.is_maker for f in crossed.fills)


# --------------------------------------------------------------------------- #
# qualifies() — the spec's worked example
# --------------------------------------------------------------------------- #


def _worked_example_settings() -> Settings:
    """2 bps a side, no latency buffer: the arithmetic in spec section B."""
    return dataclasses.replace(
        Settings(),
        fees=FeeConfig(model="standard", standard_maker_bps=2.0, standard_taker_bps=2.0),
        profit=ProfitConfig(latency_buffer_bps=0.0),
    )


def test_worked_example_passes_the_base_profile() -> None:
    """$100 position, +$0.65 gross, $0.02+$0.02 fees, $0.07 spread+slippage
    => +$0.54 net, which clears BASE's 0.50% ($0.50)."""
    eng = NetTargetEngine(settings=_worked_example_settings())
    t = build(
        eng,
        profile="base",
        slippage_bps_entry=2.5,
        slippage_bps_exit=2.5,
    )
    assert t.costs.fees_usd == pytest.approx(0.04)
    assert t.costs.spread_cost_usd + t.costs.slippage_usd == pytest.approx(0.07)
    assert t.costs.total_usd == pytest.approx(0.11)
    assert t.expected_gross_profit_usd == pytest.approx(0.65)
    assert t.expected_net_profit_usd == pytest.approx(0.54)
    assert t.net_after_costs_usd == pytest.approx(0.54)
    assert eng.qualifies(t, notional_usd=100.0) is True


def test_the_same_trade_fails_base_once_costs_reach_a_quarter() -> None:
    """+$0.65 gross against $0.25 of costs is +$0.40 net — under BASE's $0.50.

    Nothing about the *move* changed.  Execution alone turned a qualifying
    trade into a non-qualifying one, which is the entire thesis.
    """
    eng = NetTargetEngine(settings=_worked_example_settings())
    t = build(
        eng,
        profile="base",
        slippage_bps_entry=9.5,
        slippage_bps_exit=9.5,
    )
    assert t.costs.total_usd == pytest.approx(0.25)
    assert t.expected_net_profit_usd == pytest.approx(0.40)
    assert t.expected_net_profit_usd > 0          # positive, and still not enough
    assert eng.qualifies(t, notional_usd=100.0) is False


def test_a_positive_gross_with_a_negative_net_never_qualifies() -> None:
    eng = NetTargetEngine(settings=_worked_example_settings())
    t = build(eng, profile="base", slippage_bps_entry=40.0, slippage_bps_exit=40.0)
    assert t.expected_gross_profit_usd > 0
    assert t.expected_net_profit_usd < 0
    assert eng.qualifies(t, notional_usd=100.0) is False


def test_the_uncertainty_margin_rejects_an_edge_inside_the_error_bar() -> None:
    """Net profit that only just exceeds the modelled costs is not an edge."""
    s = dataclasses.replace(
        Settings(),
        fees=FeeConfig(model="standard", standard_maker_bps=2.0, standard_taker_bps=2.0),
        profit=ProfitConfig(latency_buffer_bps=0.0, micro_pct=0.01, uncertainty_margin=1.25),
    )
    eng = NetTargetEngine(settings=s)
    # Costs $0.25; a gross of $0.30 clears the (tiny) profile but not 1.25x costs.
    t = build(eng, profile="micro", slippage_bps_entry=9.5, slippage_bps_exit=9.5,
              signal=make_signal(target_bps=30.0))
    assert t.expected_gross_profit_usd == pytest.approx(0.30)
    assert t.expected_net_profit_usd == pytest.approx(0.05)
    assert t.expected_gross_profit_usd < t.costs.total_usd * 1.25
    assert eng.qualifies(t, notional_usd=100.0) is False

    lenient = NetTargetEngine(
        settings=dataclasses.replace(
            s, profit=ProfitConfig(latency_buffer_bps=0.0, micro_pct=0.01,
                                   uncertainty_margin=1.0)
        )
    )
    assert lenient.qualifies(t, notional_usd=100.0) is True


def test_qualifies_is_false_for_a_zero_notional() -> None:
    eng = NetTargetEngine(settings=_worked_example_settings())
    t = build(eng)
    assert eng.qualifies(t, notional_usd=0.0) is False


# --------------------------------------------------------------------------- #
# Reachability
# --------------------------------------------------------------------------- #


def atr_budget_bps(atr_bps: float, settings: Settings | None = None) -> float:
    """The ATR budget, scaled to the horizon exactly as the engine scales it.

    Derived rather than hardcoded: the budget moved once already when the
    yardstick was corrected from a 1-minute ATR to the 25-60 minute window, and
    a baked-in number would have hidden that rather than caught it.
    """
    import math

    s = settings or Settings()
    horizon_minutes = max(1.0, s.horizon.time_stop_seconds / 60.0)
    return s.profit.max_target_atr_mult * atr_bps * math.sqrt(horizon_minutes)


def test_unreachable_when_the_required_move_exceeds_the_atr_budget() -> None:
    """A target the market cannot reach inside the horizon is a time stop
    wearing a costume."""
    eng = engine()
    t = build(eng, profile="extended", features=make_features(atr_bps=1.0))
    assert t.gross_move_required_bps > atr_budget_bps(1.0)
    assert t.reachable is False
    assert any("ATR budget" in n for n in t.notes)
    assert eng.qualifies(t, notional_usd=100.0) is False


def test_reachable_when_volatility_can_carry_the_move() -> None:
    eng = engine()
    t = build(eng, profile="micro", features=make_features(atr_bps=60.0))
    assert t.reachable is True


def test_the_budget_scales_with_the_horizon_not_the_bar() -> None:
    """The regression: a 60-minute target was measured against a 1-minute ATR,
    which flagged every profile unreachable and stopped the bot trading."""
    assert atr_budget_bps(2.2) > Settings().profit.max_target_atr_mult * 2.2 * 5


def typical_move_bps(atr_bps: float, settings: Settings | None = None) -> float:
    """What the market usually does inside the horizon, diffusion-scaled."""
    import math

    s = settings or Settings()
    return atr_bps * math.sqrt(max(1.0, s.horizon.time_stop_seconds / 60.0))


def test_the_requested_profiles_ask_for_a_tail_move_on_this_venue() -> None:
    """Binance.US BTC/USD: ~2.2 bps of 1m ATR, so ~17 bps in an hour.

    The four profiles the brief asks for are not impossible — but every one of
    them needs *more than the whole typical hour's move*, so they are tail
    outcomes, not normal ones. That is the honest framing: the gate is not
    rejecting them, the market just rarely gets there.
    """
    eng = engine()
    feats = make_features(atr_bps=2.2)
    usual = typical_move_bps(2.2)
    for profile in Settings().profit.requested_profiles:
        t = build(eng, profile=profile, features=feats)
        assert t.gross_move_required_bps > usual, (
            f"{profile} should need more than the typical {usual:.0f} bps hour"
        )


def test_the_venue_calibrated_profiles_sit_inside_a_normal_hour() -> None:
    """0.05% and 0.10% NET are fractions of the typical hourly move, which is
    what lets the bot actually trade here and produce evidence."""
    eng = engine()
    feats = make_features(atr_bps=2.2)
    usual = typical_move_bps(2.2)
    for profile in ("venue_micro", "venue_base"):
        t = build(eng, profile=profile, features=feats)
        assert t.reachable is True
        assert t.gross_move_required_bps < usual, (
            f"{profile} should fit inside the typical {usual:.0f} bps hour"
        )


def test_no_volatility_estimate_means_reachability_is_not_certified() -> None:
    eng = engine()
    t = build(eng, features=make_features(atr_bps=0.0))
    assert t.reachable is False
    assert any("ATR" in n for n in t.notes)


def test_the_signal_claim_is_capped_by_the_atr_budget() -> None:
    eng = engine()
    budget = atr_budget_bps(2.0)
    t = build(
        eng,
        features=make_features(atr_bps=2.0),
        signal=make_signal(target_bps=budget * 4),
    )
    assert t.expected_gross_profit_usd == pytest.approx(100.0 * budget / 10_000.0)
    assert any("capped to the ATR budget" in n for n in t.notes)


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


def test_qty_is_derived_when_only_a_notional_is_given() -> None:
    eng = engine()
    t = build(eng, notional_usd=50.0, qty=0.0)
    assert t.net_tp_price > 100.0
    assert t.gross_move_required_bps > t.net_tp_pct * 100.0


def test_a_non_positive_entry_price_is_refused() -> None:
    eng = engine()
    with pytest.raises(ValueError):
        build(eng, entry_price=0.0)


def test_an_unusable_book_contributes_no_phantom_spread() -> None:
    eng = engine()
    t = build(eng, book=OrderBook(symbol=SYMBOL))
    assert t.costs.spread_cost_usd == pytest.approx(0.0)
