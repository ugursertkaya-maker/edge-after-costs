"""Paper execution: book walks, partial fills, latency, fees, limit misses."""

from __future__ import annotations

import dataclasses
import random

import pytest

from app.config import ExecConfig, FeeConfig, Settings
from app.sim.simulator import PaperSimulator
from app.types import (
    BookLevel,
    OrderBook,
    OrderRequest,
    OrderType,
    Side,
    SymbolInfo,
    Trade,
)

TS = 1_700_000_000_000

INFO = SymbolInfo(
    symbol="BTCUSD",
    base_asset="BTC",
    quote_asset="USD",
    status="TRADING",
    tick_size=0.01,
    step_size=0.000001,
    min_qty=0.000001,
    min_notional=1.0,
)


def book(
    bids: list[tuple[float, float]] | None = None,
    asks: list[tuple[float, float]] | None = None,
) -> OrderBook:
    bids = bids if bids is not None else [(99.99 - i * 0.01, 1.0) for i in range(10)]
    asks = asks if asks is not None else [(100.01 + i * 0.01, 1.0) for i in range(10)]
    return OrderBook(
        symbol="BTCUSD",
        bids=[BookLevel(p, q) for p, q in bids],
        asks=[BookLevel(p, q) for p, q in asks],
        last_update_ms=TS,
    )


def sim(*, seed: int = 1, exec_over: dict | None = None, fees: FeeConfig | None = None) -> PaperSimulator:
    settings = Settings()
    if exec_over:
        settings = dataclasses.replace(settings, execution=ExecConfig(**exec_over))
    if fees is not None:
        settings = dataclasses.replace(settings, fees=fees)
    return PaperSimulator(settings=settings, rng=random.Random(seed))


def market(qty: float, side: Side = Side.LONG) -> OrderRequest:
    return OrderRequest(
        symbol="BTCUSD", side=side, order_type=OrderType.MARKET, qty=qty, ts_ms=TS
    )


def limit(qty: float, price: float, side: Side = Side.LONG) -> OrderRequest:
    return OrderRequest(
        symbol="BTCUSD",
        side=side,
        order_type=OrderType.LIMIT,
        qty=qty,
        limit_price=price,
        ts_ms=TS,
    )


# --------------------------------------------------------------------------- #
# Market orders: the book walk
# --------------------------------------------------------------------------- #


def test_market_buy_walks_several_levels_and_pays_worse_than_mid() -> None:
    b = book()
    res = sim().execute(market(2.5), b, INFO, latency_ms=0)
    assert res.accepted
    assert res.filled_qty == pytest.approx(2.5)
    assert len(res.fills) == 3  # 1.0 + 1.0 + 0.5
    # (1.0*100.01 + 1.0*100.02 + 0.5*100.03) / 2.5 = 250.045 / 2.5
    assert res.avg_price == pytest.approx(100.018)
    assert res.avg_price > b.mid
    assert res.avg_price > b.best_ask  # worse than the touch, because it walked
    assert res.slippage_bps == pytest.approx(1.8)


def test_market_sell_walks_the_bids_and_receives_worse_than_mid() -> None:
    b = book()
    res = sim().execute(market(2.5, Side.SHORT), b, INFO, latency_ms=0)
    assert res.avg_price == pytest.approx(99.982)
    assert res.avg_price < b.mid
    assert res.slippage_bps > 0


def test_never_fills_at_the_mid() -> None:
    b = book()
    for qty in (0.1, 1.0, 3.0):
        for side in (Side.LONG, Side.SHORT):
            res = sim().execute(market(qty, side), b, INFO, latency_ms=0)
            assert res.avg_price != pytest.approx(b.mid)


def test_taker_slippage_is_never_negative() -> None:
    b = book()
    for side in (Side.LONG, Side.SHORT):
        res = sim().execute(market(0.001, side), b, INFO, latency_ms=0)
        assert res.slippage_bps >= 0


def test_slippage_grows_monotonically_with_size() -> None:
    b = book()
    slips = [sim().execute(market(q), b, INFO, latency_ms=0).slippage_bps for q in (0.5, 2.0, 5.0, 9.0)]
    assert slips == sorted(slips)
    assert slips[0] < slips[-1]
    estimates = [sim().estimate_slippage_bps(Side.LONG, n, b) for n in (50.0, 200.0, 500.0, 900.0)]
    assert estimates == sorted(estimates)


def test_thin_book_yields_an_honest_partial_fill() -> None:
    thin = book(asks=[(100.01, 0.3), (100.02, 0.2)])
    res = sim().execute(market(5.0), thin, INFO, latency_ms=0)
    assert res.accepted
    assert res.partial
    assert res.filled_qty == pytest.approx(0.5)  # all the book had
    assert res.reject_reason == "partial_fill"


def test_never_fills_more_than_the_book_holds() -> None:
    thin = book(asks=[(100.01, 0.4)])
    res = sim().execute(market(100.0), thin, INFO, latency_ms=0)
    assert res.filled_qty == pytest.approx(0.4)


def test_book_walk_is_capped_at_max_levels() -> None:
    deep = book(asks=[(100.01 + i * 0.01, 1.0) for i in range(50)])
    capped = sim(exec_over={"max_book_walk_levels": 3}).execute(
        market(10.0), deep, INFO, latency_ms=0
    )
    assert capped.filled_qty == pytest.approx(3.0)


def test_partial_fill_can_be_disabled() -> None:
    thin = book(asks=[(100.01, 0.3)])
    res = sim(exec_over={"partial_fill_enabled": False}).execute(
        market(5.0), thin, INFO, latency_ms=0
    )
    assert not res.accepted
    assert res.reject_reason == "insufficient_depth_for_full_fill"


@pytest.mark.parametrize(
    "bids,asks",
    [
        ([], [(100.01, 1.0)]),               # no bids
        ([(99.99, 1.0)], []),                # no asks
        ([], []),                            # empty
        ([(100.05, 1.0)], [(100.00, 1.0)]),  # crossed
    ],
)
def test_empty_or_crossed_book_rejects(bids: list, asks: list) -> None:
    res = sim().execute(market(1.0), book(bids=bids, asks=asks), INFO)
    assert not res.accepted
    assert res.reject_reason == "invalid_or_crossed_book"
    assert res.filled_qty == 0.0


def test_desynced_book_rejects() -> None:
    b = book()
    b.desynced = True
    assert sim().execute(market(1.0), b, INFO).reject_reason == "invalid_or_crossed_book"


# --------------------------------------------------------------------------- #
# Latency
# --------------------------------------------------------------------------- #


def test_latency_profiles_produce_monotonically_worse_fills() -> None:
    b = book()
    profiles = Settings().execution.latency_profiles_ms
    assert profiles == (20, 50, 100, 250, 500, 1000)
    prices = [sim().execute(market(1.5), b, INFO, latency_ms=ms).avg_price for ms in profiles]
    assert prices == sorted(prices)
    assert all(a < c for a, c in zip(prices, prices[1:]))
    # Selling: worse means *lower*.
    sells = [
        sim().execute(market(1.5, Side.SHORT), b, INFO, latency_ms=ms).avg_price
        for ms in profiles
    ]
    assert sells == sorted(sells, reverse=True)


def test_latency_drift_is_calibrated_at_250ms() -> None:
    """adverse_bps * (latency/250): the default profile costs exactly adverse_bps."""
    b = book()
    adverse = Settings().execution.adverse_bps
    base = sim().execute(market(1.0), b, INFO, latency_ms=0).avg_price
    at_250 = sim().execute(market(1.0), b, INFO, latency_ms=250).avg_price
    at_500 = sim().execute(market(1.0), b, INFO, latency_ms=500).avg_price
    assert (at_250 - base) / base * 10_000 == pytest.approx(adverse, rel=1e-6)
    assert (at_500 - base) / base * 10_000 == pytest.approx(2 * adverse, rel=1e-6)


def test_latency_is_reported_on_the_result() -> None:
    assert sim().execute(market(1.0), book(), INFO, latency_ms=100).latency_ms == 100
    assert sim().execute(market(1.0), book(), INFO).latency_ms == Settings().execution.latency_ms


# --------------------------------------------------------------------------- #
# Fees
# --------------------------------------------------------------------------- #


def test_market_orders_pay_the_taker_fee() -> None:
    b = book()
    res = sim().execute(market(1.0), b, INFO, latency_ms=0)
    taker = Settings().fees.taker_bps("BTCUSD")
    assert res.fees_usd == pytest.approx(res.notional * taker / 10_000)
    assert all(f.is_maker is False for f in res.fills)


def test_resting_limit_orders_pay_the_maker_fee() -> None:
    b = book()
    trades = [Trade(symbol="BTCUSD", price=99.90, qty=2.0, ts_ms=TS + 500, buyer_is_maker=True)]
    res = sim(exec_over={"limit_miss_ratio": 0.0}).execute(
        limit(1.0, 99.95), b, INFO, trades=trades, ts_ms=TS, latency_ms=0
    )
    assert res.accepted
    maker = Settings().fees.maker_bps("BTCUSD")
    assert res.fees_usd == pytest.approx(res.notional * maker / 10_000)
    assert all(f.is_maker for f in res.fills)


def test_fee_model_is_read_from_settings() -> None:
    tier0 = FeeConfig(model="tier0")
    res = sim(fees=tier0).execute(market(1.0), book(), INFO, latency_ms=0)
    assert res.fees_usd == pytest.approx(res.notional * tier0.taker_bps("BTCUSD") / 10_000)
    standard = FeeConfig(model="standard")
    res_standard = sim(fees=standard).execute(market(1.0), book(), INFO, latency_ms=0)
    assert res.fees_usd < res_standard.fees_usd


# --------------------------------------------------------------------------- #
# Limit orders
# --------------------------------------------------------------------------- #


def test_limit_misses_when_nothing_trades_through_it() -> None:
    b = book()
    assert sim().execute(limit(1.0, 99.50), b, INFO, trades=[], ts_ms=TS).reject_reason == (
        "no_trade_through_limit"
    )
    above = [Trade(symbol="BTCUSD", price=99.60, qty=5.0, ts_ms=TS + 100, buyer_is_maker=True)]
    res = sim().execute(limit(1.0, 99.50), b, INFO, trades=above, ts_ms=TS)
    assert not res.accepted
    assert res.reject_reason == "no_trade_through_limit"


def test_limit_ignores_prints_from_before_the_order_arrived() -> None:
    b = book()
    stale = [Trade(symbol="BTCUSD", price=99.40, qty=5.0, ts_ms=TS - 1, buyer_is_maker=True)]
    res = sim().execute(limit(1.0, 99.50), b, INFO, trades=stale, ts_ms=TS, latency_ms=0)
    assert res.reject_reason == "no_trade_through_limit"


def test_limit_fill_is_capped_by_the_size_that_actually_traded() -> None:
    b = book()
    trades = [Trade(symbol="BTCUSD", price=99.45, qty=0.25, ts_ms=TS + 10, buyer_is_maker=True)]
    res = sim(exec_over={"limit_miss_ratio": 0.0}).execute(
        limit(5.0, 99.50), b, INFO, trades=trades, ts_ms=TS, latency_ms=0
    )
    assert res.accepted
    assert res.filled_qty == pytest.approx(0.25)
    assert res.partial


def test_limit_miss_ratio_is_honoured_over_a_seeded_sample() -> None:
    b = book()
    trades = [Trade(symbol="BTCUSD", price=99.40, qty=50.0, ts_ms=TS + 10, buyer_is_maker=True)]
    s = sim(seed=20260910)
    misses = 0
    n = 2_000
    for _ in range(n):
        res = s.execute(limit(1.0, 99.50), b, INFO, trades=trades, ts_ms=TS, latency_ms=0)
        if res.reject_reason == "queue_position_not_reached":
            misses += 1
    ratio = misses / n
    assert ratio == pytest.approx(Settings().execution.limit_miss_ratio, abs=0.03)


def test_limit_miss_is_deterministic_for_a_given_seed() -> None:
    b = book()
    trades = [Trade(symbol="BTCUSD", price=99.40, qty=50.0, ts_ms=TS + 10, buyer_is_maker=True)]

    def run() -> list[bool]:
        s = sim(seed=99)
        return [
            s.execute(limit(1.0, 99.50), b, INFO, trades=trades, ts_ms=TS).accepted
            for _ in range(50)
        ]

    assert run() == run()


def test_never_misses_when_the_ratio_is_zero() -> None:
    b = book()
    trades = [Trade(symbol="BTCUSD", price=99.40, qty=50.0, ts_ms=TS + 10, buyer_is_maker=True)]
    s = sim(exec_over={"limit_miss_ratio": 0.0})
    for _ in range(20):
        assert s.execute(
            limit(1.0, 99.50), b, INFO, trades=trades, ts_ms=TS, latency_ms=0
        ).accepted


def test_marketable_limit_crosses_as_a_taker_and_respects_its_limit() -> None:
    b = book()
    res = sim().execute(limit(5.0, 100.03), b, INFO, trades=[], ts_ms=TS, latency_ms=0)
    assert res.accepted
    assert all(f.is_maker is False for f in res.fills)
    assert res.filled_qty == pytest.approx(3.0)  # 100.01, 100.02, 100.03 only
    assert res.avg_price <= 100.03
    assert res.fees_usd == pytest.approx(
        res.notional * Settings().fees.taker_bps("BTCUSD") / 10_000
    )


def test_limit_without_a_price_is_rejected() -> None:
    res = sim().execute(
        OrderRequest(symbol="BTCUSD", side=Side.LONG, order_type=OrderType.LIMIT, qty=1.0, ts_ms=TS),
        book(),
        INFO,
    )
    assert res.reject_reason == "limit_price_required"


# --------------------------------------------------------------------------- #
# Exchange filters
# --------------------------------------------------------------------------- #


def test_below_min_notional_is_rejected() -> None:
    info = dataclasses.replace(INFO, min_notional=10.0)
    res = sim().execute(market(0.05), book(), info, latency_ms=0)  # ~$5
    assert not res.accepted
    assert res.reject_reason == "below_min_notional"


def test_below_min_qty_is_rejected() -> None:
    info = dataclasses.replace(INFO, min_qty=0.5, step_size=0.1)
    res = sim().execute(market(0.2), book(), info, latency_ms=0)
    assert not res.accepted
    assert res.reject_reason == "below_min_qty"


def test_fills_respect_the_lot_step() -> None:
    info = dataclasses.replace(INFO, step_size=0.1, min_qty=0.1)
    res = sim().execute(market(1.37), book(), info, latency_ms=0)
    assert res.filled_qty == pytest.approx(1.3)
    assert round(res.filled_qty / 0.1) == pytest.approx(res.filled_qty / 0.1)


def test_non_positive_qty_is_rejected() -> None:
    assert sim().execute(market(0.0), book(), INFO).reject_reason == "non_positive_qty"
    assert sim().execute(market(-1.0), book(), INFO).reject_reason == "non_positive_qty"


# --------------------------------------------------------------------------- #
# estimate_slippage_bps
# --------------------------------------------------------------------------- #


def test_estimate_matches_the_executed_walk() -> None:
    b = book()
    notional = 2.5 * 100.02
    estimated = sim().estimate_slippage_bps(Side.LONG, notional, b)
    executed = sim().execute(market(2.5), b, INFO).slippage_bps
    assert estimated == pytest.approx(executed, rel=0.01)


def test_estimate_does_not_mutate_the_book() -> None:
    b = book()
    before = [(lv.price, lv.qty) for lv in b.asks]
    sim().estimate_slippage_bps(Side.LONG, 500.0, b)
    assert [(lv.price, lv.qty) for lv in b.asks] == before


def test_estimate_is_zero_on_an_unusable_book() -> None:
    assert sim().estimate_slippage_bps(Side.LONG, 100.0, book(bids=[], asks=[])) == 0.0
    assert sim().estimate_slippage_bps(Side.LONG, 0.0, book()) == 0.0


def test_fee_stress_override_beats_both_models() -> None:
    """FEE_STRESS_BPS is the "what if fees doubled" knob and outranks the tier."""
    stress = FeeConfig(model="tier0", stress_bps=25.0)
    res = sim(fees=stress).execute(market(1.0), book(), INFO, latency_ms=0)
    assert res.fees_usd == pytest.approx(res.notional * 25.0 / 10_000)


def test_maker_and_taker_sides_are_charged_separately() -> None:
    """A resting fill pays maker, a crossing fill pays taker — same book, same size."""
    fees = FeeConfig(model="standard", standard_maker_bps=4.0, standard_taker_bps=11.0)
    b = book()
    trades = [Trade(symbol="BTCUSD", price=99.90, qty=2.0, ts_ms=TS + 10, buyer_is_maker=True)]
    rested = sim(fees=fees, exec_over={"limit_miss_ratio": 0.0}).execute(
        limit(1.0, 99.95), b, INFO, trades=trades, ts_ms=TS, latency_ms=0
    )
    crossed = sim(fees=fees).execute(market(1.0), b, INFO, latency_ms=0)
    assert rested.fees_usd == pytest.approx(rested.notional * 4.0 / 10_000)
    assert crossed.fees_usd == pytest.approx(crossed.notional * 11.0 / 10_000)
