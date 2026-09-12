"""HOT/WARM/COLD tiering, the cost-aware ranking, and the widen-or-not table.

Everything here is hand-built and offline.  The arithmetic in the scoring tests
is written out rather than re-derived from the module's own constants: a test
that computes the expected value the same way the code does would pass however
the weights were changed, which is precisely what these are supposed to catch.

The economics being asserted are the ones in docs/ECONOMICS.md — a Tier-0 pair
costs ~1 bps a side and a standard pair 10, so a pair three times as deep can
still be the worse thing to trade.  That is not a preference, it is the finding
the whole system is built around, and the ranking has to reproduce it.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import replace

import pytest

from app.config import Settings, UniverseConfig
from app.types import BookLevel, OrderBook, SymbolInfo
from app.universe import (
    DEFAULT_LIQUIDITY_BARS,
    EDGE_RATIO_FULL_MARK,
    Tier,
    TierPolicy,
    UniverseEntry,
    UniverseSelector,
    break_even_hit_rate,
    edge_ratio,
    expected_move_bps,
    format_liquidity_profile,
    liquidity_profile,
    round_trip_bps,
    symbols_by_tier,
    tier_for,
)

# Real Tier-0 symbols, because the fee table is keyed by symbol and a made-up
# "AAAUSD" would silently price at the standard 10 bps.
TIER0 = ("BTCUSD", "ETHUSD", "SOLUSD", "ADAUSD", "AVAXUSD", "DOTUSD", "NEARUSD",
         "ATOMUSD", "SUIUSD", "LPTUSD", "VETUSD", "POLUSD", "ONEUSD", "FETUSD",
         "BNBUSD")
# ...and real non-Tier-0 ones, at 10 bps a side.
STANDARD = ("XRPUSDT", "LINKUSDT", "DOGEUSDT", "LTCUSDT", "AAVEUSDT", "UNIUSDT",
            "ALGOUSDT", "ICPUSDT", "FILUSDT", "XLMUSDT")


def info(symbol: str, base: str | None = None, quote: str | None = None) -> SymbolInfo:
    quote = quote or ("USDT" if symbol.endswith("USDT") else "USD")
    return SymbolInfo(
        symbol=symbol,
        base_asset=base or symbol[: -len(quote)],
        quote_asset=quote,
        status="TRADING",
        tick_size=0.01, step_size=0.001, min_qty=0.01, min_notional=1.0,
    )


def ticker(
    symbol: str,
    *,
    quote_volume: float = 10_000_000.0,
    count: int = 50_000,
    high: float = 102.0,
    low: float = 98.0,
    wap: float = 100.0,
    last: float = 100.0,
    spread_bps: float | None = 2.0,
) -> dict:
    """A 24h row.  ``spread_bps`` fills bidPrice/askPrice around a mid of 100."""
    raw = {
        "symbol": symbol,
        "quoteVolume": f"{quote_volume}",
        "count": count,
        "highPrice": f"{high}",
        "lowPrice": f"{low}",
        "weightedAvgPrice": f"{wap}",
        "lastPrice": f"{last}",
    }
    if spread_bps is not None:
        half = 100.0 * spread_bps / 2.0 / 10_000.0
        raw["bidPrice"] = f"{100.0 - half}"
        raw["askPrice"] = f"{100.0 + half}"
    return raw


def book(symbol: str, *, bid=99.99, ask=100.01, qty=1_000.0) -> OrderBook:
    ob = OrderBook(symbol=symbol)
    ob.bids = [BookLevel(bid, qty), BookLevel(bid - 0.01, qty)]
    ob.asks = [BookLevel(ask, qty), BookLevel(ask + 0.01, qty)]
    ob.last_update_id = 1
    ob.last_update_ms = 1
    return ob


def selector(**universe_kwargs) -> UniverseSelector:
    """Thresholds pinned: these tests are about tiering, not about the defaults."""
    kwargs = {
        "restrict_to_fee_tier0": False,
        "min_quote_volume_24h": 2_000_000.0,
        "min_trades_24h": 200,
    }
    kwargs.update(universe_kwargs)
    return UniverseSelector(settings=replace(Settings(), universe=UniverseConfig(**kwargs)))


def venue(symbols, **ticker_kwargs):
    """``(tickers, infos)`` for a list of symbols, all with identical metrics."""
    infos = {s: info(s) for s in symbols}
    return [ticker(s, **ticker_kwargs) for s in symbols], infos


# --------------------------------------------------------------------------- #
# the economics helpers
# --------------------------------------------------------------------------- #


def test_expected_move_scales_the_daily_range_down_to_the_horizon():
    # range 400 bps -> 400 * 0.26653 * sqrt(60/1440) = 21.76 bps at 60 minutes.
    assert expected_move_bps(400.0, 60.0) == pytest.approx(21.7618, abs=1e-3)
    # Halving the horizon does not halve the move: sqrt(t), not t.
    assert expected_move_bps(400.0, 15.0) == pytest.approx(21.7618 / 2.0, abs=1e-3)
    assert expected_move_bps(0.0, 60.0) == 0.0
    assert expected_move_bps(400.0, 0.0) == 0.0


def test_expected_move_reproduces_the_measured_binance_us_medians():
    """The conversion is an estimate; this is the check that it is not a guess.

    docs/ECONOMICS.md Part 4 measured BTC/USD's median 60-minute move at 19.7
    bps over 242 days.  BTC/USD's daily high-low range runs ~300-400 bps, and
    the inferred figure has to land in the same place or the tiering is ranking
    on a number that means nothing.
    """
    assert expected_move_bps(340.0, 60.0) == pytest.approx(19.7, abs=1.5)
    assert expected_move_bps(340.0, 25.0) == pytest.approx(12.9, abs=1.5)


def test_round_trip_counts_both_fees_and_one_spread():
    fees = Settings().fees
    # Tier 0: 1 bps taker a side, so 2 + the 2 bps spread crossed once.
    assert round_trip_bps("BTCUSD", 2.0, fees) == pytest.approx(4.0)
    # Standard: 10 bps a side. The same spread, ten times the fee.
    assert round_trip_bps("XRPUSDT", 2.0, fees) == pytest.approx(22.0)
    # Maker on this venue is free, so the passive round trip is just the spread.
    assert round_trip_bps("BTCUSD", 2.0, fees, maker=True) == pytest.approx(2.0)
    assert round_trip_bps("BTCUSD", -5.0, fees) == pytest.approx(2.0), "no negative spreads"


def test_edge_ratio_and_the_hit_rate_it_implies():
    assert edge_ratio(20.0, 4.0) == pytest.approx(5.0)
    assert edge_ratio(0.0, 4.0) == 0.0
    assert edge_ratio(20.0, 0.0) == math.inf, "a free round trip is unbounded edge"

    # p > 0.5 + costs / (2 * E|move|), i.e. 0.5 + 1 / (2 * ratio).
    assert break_even_hit_rate(5.0) == pytest.approx(0.60)
    assert break_even_hit_rate(2.0) == pytest.approx(0.75)
    assert break_even_hit_rate(math.inf) == pytest.approx(0.50)
    assert break_even_hit_rate(0.0) == 1.0
    # The default HOT bar is the "demanding but not absurd" line in ECONOMICS.md.
    assert break_even_hit_rate(TierPolicy().hot_min_edge_ratio) == pytest.approx(0.625)


# --------------------------------------------------------------------------- #
# tier_for
# --------------------------------------------------------------------------- #


def entry(symbol: str, *, volatility=400.0, round_trip=4.0, **kw) -> UniverseEntry:
    move = expected_move_bps(volatility)
    return UniverseEntry(
        symbol=symbol,
        info=info(symbol),
        quote_volume_24h=kw.get("quote_volume", 10_000_000.0),
        trades_24h=kw.get("trades", 50_000),
        spread_bps=2.0,
        volatility_bps=volatility,
        depth_usd=100_000.0,
        score=0.5,
        tier=Tier.WARM.value,
        round_trip_bps=round_trip,
        expected_move_bps=move,
        edge_ratio=edge_ratio(move, round_trip),
        net_score=0.5,
    )


def test_every_tier_is_reachable_from_rank_alone():
    policy = TierPolicy()
    good = entry("BTCUSD", round_trip=2.5)  # ratio ~8.7, clears every bar
    assert tier_for(good, policy, 0) is Tier.HOT
    assert tier_for(good, policy, policy.hot_max - 1) is Tier.HOT
    assert tier_for(good, policy, policy.hot_max) is Tier.WARM
    assert tier_for(good, policy, policy.hot_max + policy.warm_max - 1) is Tier.WARM
    assert tier_for(good, policy, policy.hot_max + policy.warm_max) is Tier.COLD
    assert tier_for(good, policy, 10_000) is Tier.COLD


def test_economics_demote_but_never_promote():
    policy = TierPolicy()
    # Ratio 2.0: the move covers the round trip twice, which needs a 75% hit
    # rate. Good enough to watch closely, not good enough for a HOT slot.
    marginal = entry("XRPUSDT", round_trip=expected_move_bps(400.0) / 2.0)
    assert marginal.edge_ratio == pytest.approx(2.0)
    assert tier_for(marginal, policy, 0) is Tier.WARM, "rank 0 cannot buy a HOT slot"

    # Ratio 0.9: the median move does not pay for the trade at all.
    hopeless = entry("LINKUSDT", round_trip=expected_move_bps(400.0) / 0.9)
    assert tier_for(hopeless, policy, 0) is Tier.COLD

    # ...and a cheap pair ranked 400th is still COLD. Economics only demote.
    assert tier_for(entry("BTCUSD"), policy, 400) is Tier.COLD


def test_tier_for_reads_the_horizon_from_the_policy():
    """The same pair is a different proposition at 2 minutes and at 60."""
    e = entry("BTCUSD", round_trip=4.0)
    assert tier_for(e, TierPolicy(horizon_minutes=60.0), 0) is Tier.HOT
    # At 2 minutes the move is 1/5.5th the size and no longer covers the cost.
    assert tier_for(e, TierPolicy(horizon_minutes=2.0), 0) is Tier.COLD


def test_tier_for_tolerates_a_negative_rank():
    assert tier_for(entry("BTCUSD"), TierPolicy(), -3) is Tier.HOT


# --------------------------------------------------------------------------- #
# cost-aware ranking
# --------------------------------------------------------------------------- #


def test_a_cheaper_pair_outranks_a_marginally_deeper_expensive_one():
    """The BTCUSD/BTCUSDT problem, with the arithmetic written out.

    Both pairs: 400 bps daily range, 2 bps spread, floor $2M.
      liquidity  = log10(volume / 2e6) / log10(100)
      volatility = (400 - 8) / (1500 - 8)        = 0.2627346
      tightness  = 1 - 2 / 8                     = 0.75
      move(60m)  = 400 * 0.2665270 * sqrt(1/24)  = 21.761842 bps
      cost       = min(1, (move / round_trip) / 8)

    BTCUSD  $10M, Tier-0, round trip 2*1 + 2 =  4 bps -> ratio 5.440461
    ETHUSDT $12M, standard, round trip 2*10 + 2 = 22 bps -> ratio 0.989175
    """
    sel = selector()
    tickers = [
        ticker("BTCUSD", quote_volume=10_000_000.0),
        ticker("ETHUSDT", quote_volume=12_000_000.0),
    ]
    infos = {"BTCUSD": info("BTCUSD"), "ETHUSDT": info("ETHUSDT")}
    out = {e.symbol: e for e in sel.select_tiers(tickers, infos)}

    btc, eth = out["BTCUSD"], out["ETHUSDT"]
    assert btc.round_trip_bps == pytest.approx(4.0)
    assert eth.round_trip_bps == pytest.approx(22.0)
    assert btc.edge_ratio == pytest.approx(5.440461, abs=1e-5)
    assert eth.edge_ratio == pytest.approx(0.989175, abs=1e-5)

    # 0.40*0.6800576 + 0.30*0.3494850 + 0.20*0.2627346 + 0.10*0.75
    assert btc.net_score == pytest.approx(0.5044154, abs=1e-6)
    # 0.40*0.1236468 + 0.30*0.3890756 + 0.20*0.2627346 + 0.10*0.75
    assert eth.net_score == pytest.approx(0.2937283, abs=1e-6)
    assert btc.net_score > eth.net_score

    # The legacy score says the opposite, which is the whole point: on volume
    # alone the 10 bps/side pair looks like the better market.  (It scores the
    # pessimistic 8 bps placeholder spread, not the quoted 2, so its tightness
    # term is 0 for both: 0.45*liquidity + 0.35*0.2627346.)
    assert btc.score == pytest.approx(0.2492254, abs=1e-6)
    assert eth.score == pytest.approx(0.2670411, abs=1e-6)
    assert eth.score > btc.score
    assert [e.symbol for e in sel.select(tickers, infos)] == ["ETHUSDT", "BTCUSD"]
    assert [e.symbol for e in sel.select_tiers(tickers, infos)] == ["BTCUSD", "ETHUSDT"]


def test_the_dedupe_keeps_the_affordable_leg_not_the_busiest_one():
    """BTCUSDT out-turns BTCUSD 3.5x on the real venue and costs 8x as much."""
    sel = selector()
    infos = {"BTCUSD": info("BTCUSD"), "BTCUSDT": info("BTCUSDT", base="BTC")}
    tickers = [
        ticker("BTCUSD", quote_volume=1_400_000_0.0),
        ticker("BTCUSDT", quote_volume=4_900_000_0.0),
    ]
    assert [e.symbol for e in sel.select(tickers, infos)] == ["BTCUSDT"]
    assert [e.symbol for e in sel.select_tiers(tickers, infos)] == ["BTCUSD"]


def test_score_without_a_round_trip_is_unchanged():
    sel = selector()
    assert sel.score(10_000_000.0, 400.0, 2.0) == sel.score(
        quote_volume=10_000_000.0, volatility_bps=400.0, spread_bps=2.0
    )
    # The cost-aware variant stays inside [0, 1] at both extremes.
    assert sel.score(1.0, 0.0, 1_000.0, round_trip_bps=10_000.0) == pytest.approx(0.0)
    assert sel.score(1e12, 10_000.0, 0.0, round_trip_bps=0.0) == pytest.approx(1.0)
    # A free round trip scores a full cost point rather than dividing by zero.
    assert sel.score(10e6, 400.0, 2.0, round_trip_bps=0.0) > sel.score(
        10e6, 400.0, 2.0, round_trip_bps=10.0
    )


def test_the_cost_term_saturates_at_the_full_mark():
    sel = selector()
    move = expected_move_bps(400.0)
    at_mark = sel.score(10e6, 400.0, 2.0, round_trip_bps=move / EDGE_RATIO_FULL_MARK)
    beyond = sel.score(10e6, 400.0, 2.0, round_trip_bps=move / (EDGE_RATIO_FULL_MARK * 10))
    assert at_mark == pytest.approx(beyond), "no extra credit above the full mark"


def test_ranking_is_deterministic_under_input_order():
    sel = selector()
    tickers, infos = venue(TIER0[:6] + STANDARD[:6])
    first = sel.select_tiers(tickers, infos)
    second = sel.select_tiers(list(reversed(tickers)), infos)
    assert [(e.symbol, e.tier) for e in first] == [(e.symbol, e.tier) for e in second]
    assert [e.net_score for e in first] == [e.net_score for e in second]


# --------------------------------------------------------------------------- #
# select_tiers
# --------------------------------------------------------------------------- #


def test_every_tier_is_reachable_end_to_end():
    """One pair per tier, separated only by what it costs to trade it."""
    sel = selector()
    symbols = ["BTCUSD", "XRPUSDT", "LINKUSDT"]
    infos = {s: info(s) for s in symbols}
    tickers = [
        # rt 4.0 against a 21.8 bps move -> ratio 5.44, break-even p 59%.
        ticker("BTCUSD", quote_volume=10_000_000.0, spread_bps=2.0),
        # rt 22.0, but an 800 bps day -> 43.5 bps move, ratio 1.98, p 75%:
        # expensive, and only worth a look every fifth cycle.
        ticker("XRPUSDT", quote_volume=9_000_000.0, spread_bps=2.0, high=104.0, low=96.0),
        # rt 60.0 against 21.8 -> ratio 0.36. Watchlist only.
        ticker("LINKUSDT", quote_volume=8_000_000.0, spread_bps=40.0),
    ]
    out = {e.symbol: e.tier for e in sel.select_tiers(tickers, infos)}
    assert out == {"BTCUSD": "hot", "XRPUSDT": "warm", "LINKUSDT": "cold"}


def test_per_tier_caps_are_respected():
    sel = selector()
    tickers, infos = venue(TIER0[:15] + STANDARD[:10])
    policy = TierPolicy(hot_max=3, warm_max=2, cold_max=4)
    out = sel.select_tiers(tickers, infos, policy=policy)
    counts = {t.value: sum(1 for e in out if e.tier == t.value) for t in Tier}
    assert counts["hot"] <= 3 and counts["warm"] <= 2 and counts["cold"] <= 4
    assert len(out) == 9, "everything past the last cap leaves the universe"
    assert [e.tier for e in out] == ["hot"] * 3 + ["warm"] * 2 + ["cold"] * 4
    assert any("every tier full" in why for why in sel.rejections.values())


def test_a_full_hot_tier_spills_into_warm_rather_than_dropping():
    sel = selector()
    tickers, infos = venue(TIER0[:6])
    out = sel.select_tiers(tickers, infos, policy=TierPolicy(hot_max=2))
    assert [e.tier for e in out] == ["hot", "hot", "warm", "warm", "warm", "warm"]


def test_a_pair_whose_cost_exceeds_its_move_never_lands_in_hot():
    """Not at any rank, not with any cap, not even alone on the venue."""
    sel = selector()
    # 40 bps spread + 20 bps of fees against a 21.8 bps median move.
    tickers = [ticker("XRPUSDT", quote_volume=500_000_000.0, spread_bps=40.0)]
    infos = {"XRPUSDT": info("XRPUSDT")}
    out = sel.select_tiers(tickers, infos)
    assert len(out) == 1
    assert out[0].tier == "cold"
    assert out[0].edge_ratio < 1.0
    assert break_even_hit_rate(out[0].edge_ratio) > 0.99

    generous = TierPolicy(hot_max=50, warm_max=50)
    assert sel.select_tiers(tickers, infos, policy=generous)[0].tier == "cold"


def test_exclude_below_edge_ratio_drops_the_hopeless_entirely():
    sel = selector()
    symbols = ["BTCUSD", "XRPUSDT"]
    infos = {s: info(s) for s in symbols}
    tickers = [ticker("BTCUSD"), ticker("XRPUSDT", spread_bps=40.0)]

    kept = sel.select_tiers(tickers, infos)
    assert [e.symbol for e in kept] == ["BTCUSD", "XRPUSDT"]

    pruned = sel.select_tiers(tickers, infos, policy=TierPolicy(exclude_below_edge_ratio=1.0))
    assert [e.symbol for e in pruned] == ["BTCUSD"]
    assert "edge ratio" in sel.rejections["XRPUSDT"]


def test_books_override_the_quoted_spread_in_the_economics():
    sel = selector()
    infos = {"BTCUSD": info("BTCUSD")}
    tickers = [ticker("BTCUSD", spread_bps=2.0)]
    measured = sel.select_tiers(tickers, infos, books={"BTCUSD": book("BTCUSD")})[0]
    assert measured.spread_bps == pytest.approx(2.0, abs=0.01)
    assert measured.round_trip_bps == pytest.approx(4.0, abs=0.02)


def test_a_pair_with_no_quote_falls_back_to_the_pessimistic_spread():
    sel = selector()
    infos = {"BTCUSD": info("BTCUSD")}
    out = sel.select_tiers([ticker("BTCUSD", spread_bps=None)], infos)[0]
    # max_spread_bps (8) + 2 bps of Tier-0 fees.
    assert out.round_trip_bps == pytest.approx(10.0)
    assert out.spread_bps == pytest.approx(8.0)


def test_max_symbols_is_honoured_by_the_tier_path_when_passed():
    sel = selector()
    tickers, infos = venue(TIER0[:10])
    assert len(sel.select_tiers(tickers, infos)) == 10
    capped = sel.select_tiers(tickers, infos, max_symbols=4)
    assert len(capped) == 4
    assert [e.symbol for e in capped] == [e.symbol for e in sel.select_tiers(tickers, infos)][:4]
    assert sel.select_tiers(tickers, infos, max_symbols=0) == []


def test_the_tier_path_is_not_bound_by_the_configured_max_symbols():
    """The old cap ended the universe at 12; now it only ends the scan list."""
    sel = selector(max_symbols=3, hot_symbols=2)
    tickers, infos = venue(TIER0[:10])
    assert len(sel.select(tickers, infos)) == 3, "the legacy path still truncates"
    assert len(sel.select_tiers(tickers, infos)) == 10


def test_entries_carry_the_economics_for_the_dashboard():
    sel = selector()
    e = sel.select_tiers([ticker("BTCUSD")], {"BTCUSD": info("BTCUSD")})[0]
    assert e.fee_tier0 is True
    assert e.fee_bps == pytest.approx(1.0)
    d = e.as_dict()
    assert d["tier"] == "hot" and d["fee_tier0"] is True
    assert d["round_trip_bps"] == pytest.approx(4.0)
    assert d["edge_ratio"] == pytest.approx(5.440461, abs=1e-5)

    other = sel.select_tiers([ticker("XRPUSDT")], {"XRPUSDT": info("XRPUSDT")})[0]
    assert other.fee_tier0 is False and other.fee_bps == pytest.approx(10.0)


def test_tier_is_a_plain_string_so_it_round_trips_through_the_market_row():
    """``repo.upsert_markets`` does ``str(entry.tier)``; a bare Enum breaks it."""
    sel = selector()
    for e in sel.select_tiers(*venue(TIER0[:3])):
        assert isinstance(e.tier, str) and not isinstance(e.tier, Tier)
        assert str(e.tier) in {"hot", "warm", "cold"}
        assert len(str(e.tier)) <= 8, "markets.tier is String(8)"


def test_symbols_by_tier_is_shaped_for_set_universe():
    sel = selector()
    tickers, infos = venue(TIER0[:4] + STANDARD[:2])
    out = symbols_by_tier(sel.select_tiers(tickers, infos, policy=TierPolicy(hot_max=2)))
    assert set(out) == {"hot", "warm", "cold"}
    assert len(out["hot"]) == 2
    assert not set(out["hot"]) & set(out["warm"]) & set(out["cold"])
    assert symbols_by_tier([]) == {"hot": [], "warm": [], "cold": []}


def test_the_policy_knows_its_own_cadence():
    p = TierPolicy()
    assert p.every(Tier.HOT) == 1 and p.every(Tier.WARM) == 5 and p.every(Tier.COLD) == 60
    assert p.cap(Tier.HOT) == 8 and p.cap(Tier.WARM) == 20 and p.cap(Tier.COLD) == 40
    assert p.due(Tier.HOT, 7) is True
    assert p.due(Tier.WARM, 5) is True and p.due(Tier.WARM, 6) is False
    assert p.due(Tier.COLD, 60) is True and p.due(Tier.COLD, 59) is False
    # The frozen contract's positional order still constructs.
    assert TierPolicy(8, 20, 40, 5, 60) == TierPolicy()


# --------------------------------------------------------------------------- #
# the fee-tier restriction, on and off
# --------------------------------------------------------------------------- #


def test_restriction_on_keeps_the_universe_to_pairs_we_can_afford():
    sel = selector(restrict_to_fee_tier0=True)
    tickers, infos = venue(TIER0[:5] + STANDARD[:5])
    out = sel.select_tiers(tickers, infos)
    assert {e.symbol for e in out} == set(TIER0[:5])
    assert all(e.fee_tier0 for e in out)
    assert all(e.tier == "hot" for e in out), "cheap pairs, so every one qualifies"


def test_restriction_off_admits_the_expensive_pairs_but_tiers_them_down():
    """Widening raises the scanned count; it does not raise the qualified one."""
    sel = selector(restrict_to_fee_tier0=False)
    tickers, infos = venue(TIER0[:5] + STANDARD[:5])
    out = sel.select_tiers(tickers, infos)
    by_tier = symbols_by_tier(out)

    assert len(out) == 10, "five more pairs are now visible"
    assert set(by_tier["hot"]) == set(TIER0[:5]), "...and none of them is HOT"
    assert set(by_tier["cold"]) == set(STANDARD[:5])
    assert all(e.edge_ratio < 1.5 for e in out if not e.fee_tier0)


def test_restriction_still_falls_back_rather_than_going_blind():
    sel = selector(restrict_to_fee_tier0=True)
    tickers, infos = venue(STANDARD[:3])
    out = sel.select_tiers(tickers, infos)
    assert {e.symbol for e in out} == set(STANDARD[:3])
    assert all(e.tier == "cold" for e in out), "visible, but not traded on"


def test_the_restriction_bites_before_the_dedupe_in_the_tier_path_too():
    sel = selector(restrict_to_fee_tier0=True)
    infos = {"BTCUSD": info("BTCUSD"), "BTCUSDT": info("BTCUSDT", base="BTC")}
    tickers = [
        ticker("BTCUSD", quote_volume=10_000_000.0),
        ticker("BTCUSDT", quote_volume=900_000_000.0),
    ]
    assert [e.symbol for e in sel.select_tiers(tickers, infos)] == ["BTCUSD"]


# --------------------------------------------------------------------------- #
# refresh_tiers
# --------------------------------------------------------------------------- #


class _FakeRest:
    def __init__(self, infos, tickers, books) -> None:
        self._infos, self._tickers, self._books = infos, tickers, books
        self.depth_calls: list[str] = []

    async def exchange_info(self):
        return self._infos

    async def ticker_24h(self):
        return self._tickers

    async def depth(self, symbol: str, limit: int = 100):
        self.depth_calls.append(symbol)
        if symbol not in self._books:
            raise RuntimeError("no book")
        return self._books[symbol]


def test_refresh_tiers_probes_hot_and_warm_only_and_leaves_cold_on_statistics():
    sel = selector()
    symbols = TIER0[:6]
    infos = {s: info(s) for s in symbols}
    # Descending volume so the ranking is the listed order, not alphabetical.
    tickers = [
        ticker(s, quote_volume=v)
        for s, v in zip(symbols, (90e6, 80e6, 70e6, 60e6, 50e6, 40e6))
    ]
    rest = _FakeRest(infos, tickers, {symbols[0]: book(symbols[0])})
    out = asyncio.run(sel.refresh_tiers(rest, policy=TierPolicy(hot_max=1, warm_max=1)))

    assert rest.depth_calls == [e.symbol for e in out][:2], "only the probe depth"
    assert len(rest.depth_calls) == 2
    assert [e.tier for e in out] == ["hot", "warm", "cold", "cold", "cold", "cold"]
    assert [e.symbol for e in out] == list(symbols)
    # The one pair with a real book carries its measured spread; the rest are
    # judged on 24h statistics and keep the pessimistic placeholder.
    assert out[0].spread_bps == pytest.approx(2.0, abs=0.01)
    assert out[1].spread_bps == pytest.approx(8.0)


def test_refresh_tiers_returns_empty_when_nothing_qualifies():
    sel = selector()
    rest = _FakeRest({}, [ticker("BTCUSD")], {})
    assert asyncio.run(sel.refresh_tiers(rest)) == []
    assert rest.depth_calls == []


# --------------------------------------------------------------------------- #
# the liquidity/cost table
# --------------------------------------------------------------------------- #


def profile_fixture():
    """Nine pairs with hand-chosen volumes, six Tier-0 and three standard.

    Volumes in $:      BTCUSD 5M | ETHUSD 3M | SOLUSD 900k | ADAUSD 300k |
                       AVAXUSD 120k | DOTUSD 40k | XRPUSDT 4M | LINKUSDT 600k |
                       DOGEUSDT 80k
    Trade counts are volume/100 so the trade bar never binds before the
    volume bar does, which keeps the counts below hand-checkable.
    """
    spec = [
        ("BTCUSD", 5_000_000.0), ("ETHUSD", 3_000_000.0), ("SOLUSD", 900_000.0),
        ("ADAUSD", 300_000.0), ("AVAXUSD", 120_000.0), ("DOTUSD", 40_000.0),
        ("XRPUSDT", 4_000_000.0), ("LINKUSDT", 600_000.0), ("DOGEUSDT", 80_000.0),
    ]
    tickers = [ticker(s, quote_volume=v, count=int(v / 100)) for s, v in spec]
    return tickers, {s: info(s) for s, _ in spec}


def test_the_liquidity_table_matches_a_hand_count():
    tickers, infos = profile_fixture()
    profile = liquidity_profile(tickers, infos)
    rows = {(r.min_quote_volume_24h, r.min_trades_24h): r for r in profile}

    # $10k/20: everything.
    r = rows[(10_000.0, 20)]
    assert (r.tier0_count, r.other_count, r.total_count) == (6, 3, 9)
    # $100k/200: DOTUSD ($40k) and DOGEUSDT ($80k) drop out.
    r = rows[(100_000.0, 200)]
    assert (r.tier0_count, r.other_count) == (5, 2)
    # $1M/2,000: BTCUSD, ETHUSD and XRPUSDT only.
    r = rows[(1_000_000.0, 2_000)]
    assert (r.tier0_count, r.other_count) == (2, 1)
    # $5M/10,000: BTCUSD alone, and no standard pair at all.
    r = rows[(5_000_000.0, 10_000)]
    assert (r.tier0_count, r.other_count) == (1, 0)
    assert r.tier0_median_symbol == "BTCUSD"
    assert r.other_median_symbol == "" and r.other_median_round_trip_bps == 0.0


def test_the_table_prices_each_tiers_median_pair():
    tickers, infos = profile_fixture()
    rows = {r.min_quote_volume_24h: r for r in liquidity_profile(tickers, infos)}

    r = rows[100_000.0]
    # Tier-0 survivors sorted by volume: AVAX 120k, ADA 300k, SOL 900k, ETH 3M,
    # BTC 5M -> the median pair is SOLUSD. Standard: LINK 600k, XRP 4M -> the
    # lower-middle of two is LINKUSDT.
    assert r.tier0_median_symbol == "SOLUSD"
    assert r.other_median_symbol == "LINKUSDT"
    # 2 bps spread either way; the difference is entirely the fee.
    assert r.tier0_median_round_trip_bps == pytest.approx(4.0)
    assert r.other_median_round_trip_bps == pytest.approx(22.0)
    # Both medians face the same 21.8 bps median move, so the cheap tier breaks
    # even at a 59% hit rate and the expensive one does not break even at all:
    # its round trip is larger than the move it is trying to catch.
    assert r.tier0_median_expected_move_bps == pytest.approx(21.7618, abs=1e-3)
    assert r.other_median_expected_move_bps == pytest.approx(21.7618, abs=1e-3)
    assert break_even_hit_rate(
        r.tier0_median_expected_move_bps / r.tier0_median_round_trip_bps
    ) == pytest.approx(0.5919, abs=1e-3)
    assert break_even_hit_rate(
        r.other_median_expected_move_bps / r.other_median_round_trip_bps
    ) == 1.0


def test_the_table_counts_pairs_not_rows_and_ignores_the_undeliverable():
    tickers, infos = profile_fixture()
    tickers += [
        ticker("BTCUSD", quote_volume=5_000_000.0),      # duplicate row
        ticker("USDCUSD", quote_volume=50_000_000.0),    # denied base
        ticker("ETHBULLUSD", quote_volume=50_000_000.0), # denylisted substring
        ticker("AAAEUR", quote_volume=50_000_000.0),     # untradable quote
        "garbage", None, {},
    ]
    infos["USDCUSD"] = info("USDCUSD", base="USDC")
    infos["ETHBULLUSD"] = info("ETHBULLUSD", base="ETHBULL")
    infos["AAAEUR"] = info("AAAEUR", base="AAA", quote="EUR")
    infos["HALTED"] = replace(info("BTCUSD"), status="BREAK")

    row = liquidity_profile(tickers, infos)[0]
    assert row.total_count == 9


def test_the_table_works_from_a_bare_ticker_snapshot():
    """No exchangeInfo: the quote asset comes off the symbol suffix."""
    tickers, _ = profile_fixture()
    with_infos = liquidity_profile(*profile_fixture())
    without = liquidity_profile(tickers)
    assert [r.as_dict() for r in without] == [r.as_dict() for r in with_infos]


def test_the_table_honours_custom_bars_and_renders_as_markdown():
    tickers, infos = profile_fixture()
    rows = liquidity_profile(tickers, infos, bars=((250_000.0, 2_500),))
    assert len(rows) == 1
    # Everything from ADAUSD ($300k, 3,000 trades) up: six of the nine.
    assert (rows[0].tier0_count, rows[0].other_count) == (4, 2)

    text = format_liquidity_profile(liquidity_profile(tickers, infos))
    assert text.count("\n") == len(DEFAULT_LIQUIDITY_BARS) + 1
    assert "| $100,000 | 200 | 5 | 2 |" in text
    assert format_liquidity_profile([]).count("\n") == 1


def test_an_empty_snapshot_produces_a_table_of_zeroes():
    rows = liquidity_profile([], {})
    assert len(rows) == len(DEFAULT_LIQUIDITY_BARS)
    assert all(r.total_count == 0 for r in rows)
    assert all(r.tier0_median_symbol == "" for r in rows)


def test_a_hot_slot_freed_by_a_demotion_goes_to_the_next_pair_that_can_use_it():
    """Otherwise the universe runs narrower than the policy allows.

    Rank 0 is a deep but expensive pair the economics send to WARM.  The HOT
    slot it did not take belongs to the cheap pair behind it, not to nobody —
    which is the difference between an 8-pair HOT tier and a 5-pair one on the
    live venue.
    """
    sel = selector()
    symbols = ["XRPUSDT", "LINKUSDT", "BTCUSD", "ETHUSD"]
    infos = {s: info(s) for s in symbols}
    tickers = [
        # Deep and wild enough (1400 bps day, $50M) to out-score the cheap pairs
        # even after the cost term, but a 22 bps round trip against a 76 bps
        # move is ratio 3.5 -> below the HOT bar, so it can only be WARM.
        ticker("XRPUSDT", quote_volume=50_000_000.0, high=107.0, low=93.0),
        ticker("LINKUSDT", quote_volume=40_000_000.0, high=107.0, low=93.0),
        # Cheap, thin and ordinary: ranked below, but affordable.
        ticker("BTCUSD", quote_volume=2_500_000.0),
        ticker("ETHUSD", quote_volume=2_400_000.0),
    ]
    out = sel.select_tiers(tickers, infos, policy=TierPolicy(hot_max=2, warm_max=4))
    assert [e.symbol for e in out][:2] == ["XRPUSDT", "LINKUSDT"], "still ranked first"
    assert symbols_by_tier(out) == {
        "hot": ["BTCUSD", "ETHUSD"],
        "warm": ["XRPUSDT", "LINKUSDT"],
        "cold": [],
    }


def test_a_demotion_does_not_hand_out_more_slots_than_the_caps_allow():
    """Backfilling HOT must not quietly grow the scanned set."""
    sel = selector()
    symbols = list(STANDARD[:3]) + list(TIER0[:6])
    infos = {s: info(s) for s in symbols}
    tickers = [ticker(s, quote_volume=50_000_000.0, high=104.0, low=96.0) for s in STANDARD[:3]]
    tickers += [ticker(s, quote_volume=10_000_000.0) for s in TIER0[:6]]
    out = sel.select_tiers(tickers, infos, policy=TierPolicy(hot_max=2, warm_max=3, cold_max=1))
    counts = {t.value: sum(1 for e in out if e.tier == t.value) for t in Tier}
    assert counts == {"hot": 2, "warm": 3, "cold": 1}
    assert len(out) == 6
