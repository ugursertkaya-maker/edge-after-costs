"""Universe selection: every filter, the USD/USDT dedupe, scoring and tiers."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import Settings, UniverseConfig
from app.exchange.rest import _parse_symbol_info
from app.types import BookLevel, OrderBook, SymbolInfo
from app.universe import UniverseEntry, UniverseSelector

FIXTURES = Path(__file__).parent / "fixtures" / "binanceus"


def fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def infos_from_fixture() -> dict[str, SymbolInfo]:
    raw = fixture("exchange_info")
    parsed = [_parse_symbol_info(s) for s in raw["symbols"]]
    return {i.symbol: i for i in parsed if i is not None}


def info(symbol: str, base: str, quote: str = "USD", status: str = "TRADING") -> SymbolInfo:
    return SymbolInfo(
        symbol=symbol, base_asset=base, quote_asset=quote, status=status,
        tick_size=0.01, step_size=0.001, min_qty=0.01, min_notional=1.0,
    )


def ticker(symbol: str, *, quote_volume=50_000_000.0, count=100_000,
           high=102.0, low=98.0, wap=100.0, last=100.5) -> dict:
    return {
        "symbol": symbol,
        "quoteVolume": f"{quote_volume}",
        "count": count,
        "highPrice": f"{high}",
        "lowPrice": f"{low}",
        "weightedAvgPrice": f"{wap}",
        "lastPrice": f"{last}",
    }


def book(symbol: str, *, bid=99.99, ask=100.01, qty=1_000.0) -> OrderBook:
    ob = OrderBook(symbol=symbol)
    ob.bids = [BookLevel(bid, qty), BookLevel(bid - 0.01, qty)]
    ob.asks = [BookLevel(ask, qty), BookLevel(ask + 0.01, qty)]
    ob.last_update_id = 1
    ob.last_update_ms = 1
    return ob


def selector(**universe_kwargs) -> UniverseSelector:
    if not universe_kwargs:
        return UniverseSelector(settings=Settings())
    return UniverseSelector(
        settings=replace(Settings(), universe=UniverseConfig(**universe_kwargs))
    )


# --------------------------------------------------------------------------- #
# filters, one rule at a time
# --------------------------------------------------------------------------- #


def test_a_healthy_pair_is_selected():
    sel = selector()
    out = sel.select([ticker("AAAUSD")], {"AAAUSD": info("AAAUSD", "AAA")})
    assert [e.symbol for e in out] == ["AAAUSD"]
    assert isinstance(out[0], UniverseEntry)
    assert out[0].quote_volume_24h == 50_000_000.0
    assert out[0].trades_24h == 100_000
    assert out[0].volatility_bps == pytest.approx(400.0)
    assert 0.0 <= out[0].score <= 1.0


def test_non_trading_status_is_rejected():
    sel = selector()
    out = sel.select([ticker("AAAUSD")], {"AAAUSD": info("AAAUSD", "AAA", status="BREAK")})
    assert out == []
    assert "BREAK" in sel.rejections["AAAUSD"]


def test_unlisted_quote_asset_is_rejected():
    sel = selector()
    out = sel.select([ticker("AAAEUR")], {"AAAEUR": info("AAAEUR", "AAA", quote="EUR")})
    assert out == []
    assert "EUR" in sel.rejections["AAAEUR"]


def test_denied_base_assets_are_rejected():
    sel = selector()
    out = sel.select([ticker("USDCUSD")], {"USDCUSD": info("USDCUSD", "USDC")})
    assert out == []
    assert "USDC" in sel.rejections["USDCUSD"]


def test_denied_symbol_substrings_are_rejected():
    sel = selector()
    infos = {
        "ETHBULLUSD": info("ETHBULLUSD", "ETHBULL"),
        "BTCUPUSD": info("BTCUPUSD", "BTCUP"),
    }
    out = sel.select([ticker("ETHBULLUSD"), ticker("BTCUPUSD")], infos)
    assert out == []
    assert set(sel.rejections) == {"ETHBULLUSD", "BTCUPUSD"}


def test_thin_24h_volume_is_rejected():
    # Pinned thresholds: this test is about the rule, not about whatever the
    # live-calibrated defaults happen to be today.
    sel = selector(min_quote_volume_24h=2_000_000.0)
    thin = ticker("AAAUSD", quote_volume=1_999_999.0)
    assert sel.select([thin], {"AAAUSD": info("AAAUSD", "AAA")}) == []
    assert "volume" in sel.rejections["AAAUSD"]

    edge = ticker("AAAUSD", quote_volume=2_000_000.0)
    assert len(sel.select([edge], {"AAAUSD": info("AAAUSD", "AAA")})) == 1


def test_too_few_trades_is_rejected():
    # Pinned thresholds: this test is about the rule, not about whatever the
    # live-calibrated defaults happen to be today.
    sel = selector(min_trades_24h=5_000)
    assert sel.select([ticker("AAAUSD", count=4_999)], {"AAAUSD": info("AAAUSD", "AAA")}) == []
    assert "trade count" in sel.rejections["AAAUSD"]


def test_volatility_band_is_enforced_on_both_sides():
    # Pinned thresholds: this test is about the rule, not about whatever the
    # live-calibrated defaults happen to be today.
    sel = selector(min_volatility_bps=8.0, max_volatility_bps=400.0)
    infos = {"AAAUSD": info("AAAUSD", "AAA")}

    # 0.05% range = 5 bps, below the 8 bps floor
    assert sel.select([ticker("AAAUSD", high=100.025, low=99.975)], infos) == []
    assert "too low" in sel.rejections["AAAUSD"]

    # 5% range = 500 bps, above the 400 bps ceiling
    assert sel.select([ticker("AAAUSD", high=102.5, low=97.5)], infos) == []
    assert "too high" in sel.rejections["AAAUSD"]

    assert len(sel.select([ticker("AAAUSD", high=101.0, low=99.0)], infos)) == 1


def test_volatility_falls_back_to_last_price_without_a_vwap():
    sel = selector()
    raw = ticker("AAAUSD")
    raw["weightedAvgPrice"] = "0"
    out = sel.select([raw], {"AAAUSD": info("AAAUSD", "AAA")})
    assert out[0].volatility_bps == pytest.approx(4.0 / 100.5 * 10_000)


def test_missing_exchange_info_is_rejected():
    sel = selector()
    assert sel.select([ticker("GHOSTUSD")], {}) == []
    assert "exchangeInfo" in sel.rejections["GHOSTUSD"]


def test_wide_spread_and_thin_depth_are_rejected_when_books_are_supplied():
    sel = selector()
    infos = {"AAAUSD": info("AAAUSD", "AAA")}
    tickers = [ticker("AAAUSD")]

    tight = book("AAAUSD", bid=99.99, ask=100.01)  # 2 bps
    assert len(sel.select(tickers, infos, books={"AAAUSD": tight})) == 1

    wide = book("AAAUSD", bid=99.90, ask=100.10)  # 20 bps
    assert sel.select(tickers, infos, books={"AAAUSD": wide}) == []
    assert "spread" in sel.rejections["AAAUSD"]

    thin = book("AAAUSD", bid=99.99, ask=100.01, qty=1.0)  # ~$100 a side
    assert sel.select(tickers, infos, books={"AAAUSD": thin}) == []
    assert "depth" in sel.rejections["AAAUSD"]


def test_book_metrics_are_recorded_on_the_entry():
    sel = selector()
    out = sel.select(
        [ticker("AAAUSD")],
        {"AAAUSD": info("AAAUSD", "AAA")},
        books={"AAAUSD": book("AAAUSD")},
    )
    assert out[0].spread_bps == pytest.approx(2.0, abs=0.01)
    assert out[0].depth_usd > 0


def test_without_a_book_the_entry_carries_the_pessimistic_spread():
    sel = selector()
    out = sel.select([ticker("AAAUSD")], {"AAAUSD": info("AAAUSD", "AAA")})
    assert out[0].spread_bps == Settings().universe.max_spread_bps
    assert out[0].depth_usd == 0.0


# --------------------------------------------------------------------------- #
# dedupe / scoring / tiers / determinism
# --------------------------------------------------------------------------- #


def test_usd_and_usdt_pairs_of_one_base_collapse_to_the_better_one():
    # Fee-tier restriction off: BTCUSD is a Tier-0 pair and BTCUSDT is not,
    # which would decide this before the dedupe rule ever ran.
    sel = selector(restrict_to_fee_tier0=False, min_quote_volume_24h=2_000_000.0)
    infos = {
        "BTCUSD": info("BTCUSD", "BTC", "USD"),
        "BTCUSDT": info("BTCUSDT", "BTC", "USDT"),
    }
    tickers = [
        ticker("BTCUSD", quote_volume=10_000_000.0),
        ticker("BTCUSDT", quote_volume=90_000_000.0),
    ]
    out = sel.select(tickers, infos)
    assert [e.symbol for e in out] == ["BTCUSDT"], "the deeper market wins"

    # ...and with identical metrics the preferred quote asset (USD) wins.
    even = [ticker("BTCUSD"), ticker("BTCUSDT")]
    assert [e.symbol for e in sel.select(even, infos)] == ["BTCUSD"]


def test_score_rewards_liquidity_volatility_and_tightness():
    # The liquidity term is normalised against the configured floor, so pin
    # it or the $10M and $100M cases both saturate at the top of the scale.
    sel = selector(min_quote_volume_24h=2_000_000.0)
    base = sel.score(quote_volume=10_000_000.0, volatility_bps=100.0, spread_bps=4.0)
    assert sel.score(100_000_000.0, 100.0, 4.0) > base
    assert sel.score(10_000_000.0, 300.0, 4.0) > base
    assert sel.score(10_000_000.0, 100.0, 1.0) > base
    assert 0.0 <= sel.score(1.0, 0.0, 1_000.0) <= 1.0
    assert sel.score(10_000_000_000.0, 10_000.0, 0.0) == pytest.approx(1.0)


def test_results_are_sorted_by_score_then_truncated_and_tiered():
    sel = selector(max_symbols=4, hot_symbols=2)
    infos, tickers = {}, []
    # Descending liquidity => descending score.
    for i, volume in enumerate([90e6, 80e6, 70e6, 60e6, 50e6, 40e6]):
        sym = f"C{i}USD"
        infos[sym] = info(sym, f"C{i}")
        tickers.append(ticker(sym, quote_volume=volume))

    out = sel.select(tickers, infos)
    assert [e.symbol for e in out] == ["C0USD", "C1USD", "C2USD", "C3USD"]
    assert [e.tier for e in out] == ["hot", "hot", "warm", "warm"]
    assert all(out[i].score >= out[i + 1].score for i in range(len(out) - 1))


def test_ties_break_deterministically_by_symbol_name():
    sel = selector()
    infos = {s: info(s, s[:-3]) for s in ("ZZZUSD", "AAAUSD", "MMMUSD")}
    tickers = [ticker("ZZZUSD"), ticker("MMMUSD"), ticker("AAAUSD")]
    first = sel.select(tickers, infos)
    assert [e.symbol for e in first] == ["AAAUSD", "MMMUSD", "ZZZUSD"]

    shuffled = [ticker("MMMUSD"), ticker("AAAUSD"), ticker("ZZZUSD")]
    assert [e.symbol for e in sel.select(shuffled, infos)] == [e.symbol for e in first]
    assert [e.score for e in sel.select(shuffled, infos)] == [e.score for e in first]


def test_select_is_pure_and_repeatable_on_the_exchange_fixture():
    # Fee-tier restriction off: this test is about selection mechanics
    # (filters, dedupe, ordering), not about which pairs the economics allow.
    sel = selector(restrict_to_fee_tier0=False)
    infos = infos_from_fixture()
    tickers = fixture("ticker_24hr")

    first = sel.select(tickers, infos)
    second = sel.select(list(reversed(tickers)), infos)

    assert [e.symbol for e in first] == ["BTCUSD", "ETHUSDT", "SOLUSD"]
    assert [e.symbol for e in second] == [e.symbol for e in first]
    assert [e.tier for e in first] == ["hot", "hot", "hot"]
    assert set(sel.rejections) >= {
        "HALTUSD",      # status BREAK
        "USDTUSD",      # denied base asset
        "ETHBULLUSD",   # denylisted substring
        "SLEEPYUSD",    # volatility below the floor
        "WILDUSD",      # volatility above the ceiling
        "GHOSTUSD",     # no exchangeInfo entry
    }
    assert "BTCUSDT" not in [e.symbol for e in first], "deduped against BTCUSD"


def test_garbage_rows_are_skipped_not_fatal():
    sel = selector()
    out = sel.select(
        [None, "nope", {}, {"symbol": ""}, ticker("AAAUSD")],
        {"AAAUSD": info("AAAUSD", "AAA")},
    )
    assert [e.symbol for e in out] == ["AAAUSD"]


# --------------------------------------------------------------------------- #
# refresh()
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


def test_refresh_measures_the_shortlists_books_before_the_final_cut():
    # Restriction off: this is about the two-pass shortlist, and ETHUSDT is not
    # a Tier-0 pair so it would never reach the depth call otherwise.
    sel = selector(restrict_to_fee_tier0=False)
    infos = infos_from_fixture()
    tickers = fixture("ticker_24hr")
    books = {
        "BTCUSD": book("BTCUSD", bid=60_999.0, ask=61_001.0, qty=1.0),   # ~0.33 bps
        "ETHUSDT": book("ETHUSDT", bid=2_500.0, ask=2_530.0, qty=10.0),  # ~119 bps
    }
    rest = _FakeRest(infos, tickers, books)

    out = asyncio.run(sel.refresh(rest))

    assert rest.depth_calls == ["BTCUSD", "ETHUSDT", "SOLUSD"]
    # ETHUSDT is priced out by its spread; SOLUSD's depth call failed, so it is
    # judged on statistics alone and survives.
    assert [e.symbol for e in out] == ["BTCUSD", "SOLUSD"]
    assert out[0].spread_bps == pytest.approx(0.328, abs=0.01)
    assert out[0].tier == "hot"


def test_refresh_returns_empty_when_nothing_qualifies():
    sel = selector()
    rest = _FakeRest({}, fixture("ticker_24hr"), {})
    assert asyncio.run(sel.refresh(rest)) == []
    assert rest.depth_calls == []


def test_tier0_restriction_survives_the_usd_usdt_dedupe():
    """The regression that shipped: BTCUSDT out-volumes BTCUSD, so a dedupe
    running before the fee filter throws away the only pair we can afford."""
    sel = selector(restrict_to_fee_tier0=True, min_quote_volume_24h=2_000_000.0)
    infos = {
        "BTCUSD": info("BTCUSD", "BTC", "USD"),
        "BTCUSDT": info("BTCUSDT", "BTC", "USDT"),
    }
    tickers = [
        ticker("BTCUSD", quote_volume=10_000_000.0),
        ticker("BTCUSDT", quote_volume=900_000_000.0),
    ]
    assert [e.symbol for e in sel.select(tickers, infos)] == ["BTCUSD"]


def test_tier0_restriction_keeps_only_pairs_the_economics_support():
    """A 10 bps/side pair cannot fund an ATR-sized target, so it is not scanned."""
    sel = selector(restrict_to_fee_tier0=True)
    infos = infos_from_fixture()
    out = sel.select(fixture("ticker_24hr"), infos)
    tier0 = Settings().fees.tier0_symbols
    assert out, "restriction must not empty the universe when Tier-0 pairs exist"
    assert all(e.symbol in tier0 for e in out)


def test_tier0_restriction_falls_back_rather_than_going_blind():
    """A stale fee table must not silently stop the bot from seeing any market."""
    sel = selector(restrict_to_fee_tier0=True)
    infos = {"ZZZUSD": info("ZZZUSD", "ZZZ")}
    out = sel.select([ticker("ZZZUSD")], infos)
    assert [e.symbol for e in out] == ["ZZZUSD"]


# --------------------------------------------------------------------------- #
# the legacy path, pinned against the Phase 5 additions
# --------------------------------------------------------------------------- #


def test_select_still_truncates_at_max_symbols_and_only_tags_hot_or_warm():
    """Tiering adds a third tier; ``select`` must not start emitting it.

    Callers of ``select`` — ``refresh``, the worker's ``hot``/``warm`` split,
    ``repo.upsert_markets`` — read ``tier`` as a two-valued field, and
    ``max_symbols`` is the only cap they have.
    """
    sel = selector(max_symbols=3, hot_symbols=1, restrict_to_fee_tier0=False)
    infos, tickers = {}, []
    for i, volume in enumerate([90e6, 80e6, 70e6, 60e6, 50e6]):
        sym = f"C{i}USD"
        infos[sym] = info(sym, f"C{i}")
        tickers.append(ticker(sym, quote_volume=volume))

    out = sel.select(tickers, infos)
    assert [e.symbol for e in out] == ["C0USD", "C1USD", "C2USD"]
    assert [e.tier for e in out] == ["hot", "warm", "warm"]
    assert "cold" not in {e.tier for e in out}


def test_the_legacy_score_is_untouched_by_the_cost_aware_variant():
    """Pinned numbers, not a re-derivation: 0.45/0.35/0.20 on the old terms."""
    sel = selector(min_quote_volume_24h=2_000_000.0)
    # liquidity log10(5)/2 = 0.3494850, volatility (400-8)/1492 = 0.2627346,
    # tightness 1 - 2/8 = 0.75.
    assert sel.score(10_000_000.0, 400.0, 2.0) == pytest.approx(0.3992254, abs=1e-6)
    assert sel.score(10_000_000.0, 400.0, 8.0) == pytest.approx(0.2492254, abs=1e-6)


def test_legacy_entries_still_carry_the_economics_block():
    """``select`` does not rank on cost, but it still reports it.

    The dashboard and the idle-capital attribution both want to know what a
    scanned pair costs even when the scan list was chosen the old way.
    """
    sel = selector(restrict_to_fee_tier0=False, min_quote_volume_24h=2_000_000.0)
    infos = {"BTCUSD": info("BTCUSD", "BTC"), "XRPUSDT": info("XRPUSDT", "XRP", "USDT")}
    out = {e.symbol: e for e in sel.select([ticker("BTCUSD"), ticker("XRPUSDT")], infos)}

    assert out["BTCUSD"].fee_bps == pytest.approx(1.0)
    assert out["BTCUSD"].fee_tier0 is True
    assert out["XRPUSDT"].fee_bps == pytest.approx(10.0)
    assert out["XRPUSDT"].fee_tier0 is False
    # No book and no quoted bid/ask, so both carry the pessimistic 8 bps spread.
    assert out["BTCUSD"].round_trip_bps == pytest.approx(10.0)
    assert out["XRPUSDT"].round_trip_bps == pytest.approx(28.0)
    assert out["BTCUSD"].edge_ratio > out["XRPUSDT"].edge_ratio


def test_an_entry_built_the_old_way_is_still_valid():
    """Every Phase 5 field is defaulted, so nothing that constructs one breaks."""
    e = UniverseEntry(
        symbol="AAAUSD", info=info("AAAUSD", "AAA"), quote_volume_24h=1.0,
        trades_24h=1, spread_bps=1.0, volatility_bps=1.0, depth_usd=1.0,
        score=0.5, tier="hot",
    )
    assert e.round_trip_bps == 0.0 and e.edge_ratio == 0.0 and e.fee_tier0 is False
    assert e.as_dict()["symbol"] == "AAAUSD"
