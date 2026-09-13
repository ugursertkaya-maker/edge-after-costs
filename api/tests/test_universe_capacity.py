"""Capacity: can the pair absorb *us*, and at what size?

Phase 6 asked for "enough liquidity, not maximum liquidity". The measure that
answers it is not the 24h volume ranking -- BTC wins that permanently and it
says nothing about whether a $25 order moves the pair. It is the order as a
share of the flow arriving in the window we actually hold for.

The venue numbers in these tests were measured on 2026-09-12.
"""

from __future__ import annotations

import math

import pytest

from app.config import get_settings
from app.types import SymbolInfo
from app.universe import (
    CAPACITY_COMFORTABLE,
    UniverseEntry,
    capacity_max_usd,
    capacity_share,
)


def _info(symbol: str = "BTCUSD", *, min_notional: float = 1.0) -> SymbolInfo:
    return SymbolInfo(
        symbol=symbol, base_asset=symbol[:-3], quote_asset="USD", status="TRADING",
        step_size=1e-5, tick_size=0.01, min_qty=1e-5, min_notional=min_notional,
    )


def _entry(**kw) -> UniverseEntry:
    base = dict(
        symbol="BTCUSD", info=_info(), quote_volume_24h=1_190_091.0, trades_24h=5_000,
        spread_bps=1.0, volatility_bps=200.0, depth_usd=50_000.0, score=0.5, tier="hot",
    )
    base.update(kw)
    return UniverseEntry(**base)


class TestCapacityShare:
    def test_share_is_order_over_one_minute_of_volume(self):
        # $1,440,000 a day is exactly $1,000 a minute.
        assert capacity_share(1_440_000.0, 50.0) == pytest.approx(0.05)

    def test_a_pair_that_trades_nothing_cannot_absorb_anything(self):
        # inf rather than a large finite number: any order is infinite relative
        # to no flow, and a finite answer would invite a threshold comparison
        # that quietly passes.
        assert capacity_share(0.0, 50.0) == math.inf

    def test_no_order_means_nothing_to_absorb(self):
        assert capacity_share(0.0, 0.0) == 0.0

    def test_measured_venue_reality(self):
        """The numbers that made this gate necessary, on real 24h volumes."""
        # ETH absorbs a $50 order in 4% of a minute; SOL needs a quarter of one.
        assert capacity_share(1_778_425.0, 50.0) < CAPACITY_COMFORTABLE
        assert capacity_share(301_108.0, 50.0) > CAPACITY_COMFORTABLE
        # ADA cannot take $50 at all: 44% of a minute's entire volume.
        assert capacity_share(161_688.0, 50.0) > 0.4


class TestCapacityMaxUsd:
    def test_inverse_of_share(self):
        vol = 1_440_000.0
        assert capacity_max_usd(vol) == pytest.approx(1_000.0 * CAPACITY_COMFORTABLE)

    def test_sizes_rather_than_delists(self):
        """SOL is thin for $50 and fine for $10 -- the answer is $10, not 'no'."""
        cap = capacity_max_usd(301_108.0)
        assert 5.0 < cap < 25.0
        assert capacity_share(301_108.0, cap) == pytest.approx(CAPACITY_COMFORTABLE)


class TestCapacityOk:
    def test_unmeasured_capacity_never_delists(self):
        """A missing measurement must not silently remove a working pair."""
        assert _entry(capacity_max_usd=0.0).capacity_ok is True

    def test_passes_when_the_pair_absorbs_the_minimum_order(self):
        e = _entry(capacity_max_usd=41.32, min_tradable_usd=10.0)
        assert e.capacity_ok is True
        assert e.sizable is True

    def test_fails_only_when_untradable_at_any_size(self):
        """Delisting is judged against the VENUE minimum, not our floor.

        ADAUSD absorbs $5.61 a minute against our $10 floor -- too thin to
        size, but the venue's own minimum is $1 and the pair trades fine. It
        stays listed, subscribed and measured; the allocator refuses it.
        """
        e = _entry(symbol="ADAUSD", info=_info("ADAUSD", min_notional=1.0),
                   capacity_max_usd=5.61, min_tradable_usd=10.0)
        assert e.capacity_ok is True
        assert e.sizable is False

    def test_delists_only_when_the_venue_minimum_cannot_fit(self):
        """AVAXUSD: $0.33 a minute against a $1.00 venue minimum."""
        e = _entry(symbol="AVAXUSD", info=_info("AVAXUSD", min_notional=1.0),
                   capacity_max_usd=0.33, min_tradable_usd=10.0)
        assert e.capacity_ok is False

    def test_a_weekend_lull_does_not_delist_a_pair(self):
        """The 2026-09-12 incident: SOLUSD fell $10.46 -> $6.54 a minute and
        vanished from the universe entirely, unsubscribed and unmeasured."""
        e = _entry(symbol="SOLUSD", info=_info("SOLUSD", min_notional=1.0),
                   capacity_max_usd=6.54, min_tradable_usd=10.0)
        assert e.capacity_ok is True, "a quiet session must not delist a pair"
        assert e.sizable is False, "but we still cannot size it right now"

    def test_exactly_at_the_floor_is_tradable(self):
        assert _entry(capacity_max_usd=10.0, min_tradable_usd=10.0).capacity_ok is True


class TestEntryReportsCapacity:
    def test_as_dict_exposes_both_ends_of_the_range(self):
        d = _entry(capacity_share=0.06, capacity_max_usd=41.32,
                   min_tradable_usd=10.0).as_dict()
        assert d["capacity_share"] == 0.06
        assert d["capacity_max_usd"] == 41.32
        assert d["min_tradable_usd"] == 10.0
        assert d["capacity_ok"] is True

    def test_defaults_keep_old_entries_valid(self):
        """Entries built before Phase 6 must still be valid entries."""
        e = _entry()
        assert e.capacity_share == 0.0
        assert e.capacity_max_usd == 0.0
        assert e.capacity_ok is True


class TestSelectorPopulatesCapacity:
    def test_evaluate_measures_against_the_largest_allowed_position(self):
        from app.universe import UniverseSelector

        settings = get_settings()
        sel = UniverseSelector(settings=settings)
        ticker = {
            "symbol": "BTCUSD", "quoteVolume": "1190091", "count": "5000",
            "highPrice": "77000", "lowPrice": "76000", "weightedAvgPrice": "76500",
            "bidPrice": "76500", "askPrice": "76504",
        }
        entry = sel._evaluate("BTCUSD", ticker, {"BTCUSD": _info()}, None)
        assert entry is not None
        expected = capacity_share(1_190_091.0, settings.portfolio.max_position_usd)
        assert entry.capacity_share == pytest.approx(expected)
        assert entry.capacity_max_usd == pytest.approx(capacity_max_usd(1_190_091.0))
        assert entry.min_tradable_usd >= settings.risk.min_notional_usd


class TestDedupeIsStableAndPrefersTheCheapLeg:
    """The 2026-09-12 incident: the universe oscillated every fifteen minutes.

    SOLUSD scored 0.2982 and SOLUSDT 0.2941 -- a 1.4% gap. A strict `>` let
    tick noise decide which leg was subscribed on each refresh, and every flip
    dropped a warm book and installed a stale one.
    """

    def _leg(self, symbol: str, quote: str, *, net_score: float,
             trip: float, tier0: bool) -> UniverseEntry:
        return _entry(
            symbol=symbol,
            info=SymbolInfo(symbol=symbol, base_asset="SOL", quote_asset=quote,
                            status="TRADING", step_size=1e-3, tick_size=0.01,
                            min_qty=1e-3, min_notional=1.0),
            net_score=net_score, round_trip_bps=trip, fee_tier0=tier0,
        )

    def test_a_near_tie_is_broken_by_cost_not_by_noise(self):
        from app.config import get_settings
        from app.universe import UniverseSelector

        sel = UniverseSelector(settings=get_settings())
        usd = self._leg("SOLUSD", "USD", net_score=0.2982, trip=4.9, tier0=True)
        usdt = self._leg("SOLUSDT", "USDT", net_score=0.2941, trip=21.0, tier0=False)

        assert sel._prefer(usd, usdt, key="net_score") is True
        # And the decisive part: even when noise puts the expensive leg ahead,
        # the cheap one still wins, so the choice does not oscillate.
        usdt_ahead = self._leg("SOLUSDT", "USDT", net_score=0.3010,
                               trip=21.0, tier0=False)
        assert sel._prefer(usdt_ahead, usd, key="net_score") is False
        assert sel._prefer(usd, usdt_ahead, key="net_score") is True

    def test_a_genuinely_better_pair_still_wins(self):
        """The dead band must not freeze a real difference out."""
        from app.config import get_settings
        from app.universe import UniverseSelector

        sel = UniverseSelector(settings=get_settings())
        cheap_but_dead = self._leg("SOLUSD", "USD", net_score=0.10,
                                   trip=4.9, tier0=True)
        clearly_better = self._leg("SOLUSDT", "USDT", net_score=0.40,
                                   trip=21.0, tier0=False)
        assert sel._prefer(clearly_better, cheap_but_dead, key="net_score") is True


class TestCapacityBoundsPositionSize:
    """Capacity was measured for the universe and never applied to sizing.

    Measured in the 120-day replay: average position $1,714.67 into ETHUSD, a
    pair whose own flow absorbs about $22 a minute. The simulator charged the
    book walk honestly and realised slippage came out at 6.85 bps against the
    2.0 the config models -- most of why every activity profile lost money.
    """

    def _engine(self):
        from app.config import get_settings
        from app.portfolio import PortfolioEngine
        return PortfolioEngine(settings=get_settings())

    def test_an_unmeasured_symbol_is_sized_exactly_as_before(self):
        """Absence of a measurement must not become a position block."""
        pe = self._engine()
        assert pe._capacity_cap("ETHUSD") is None

    def test_a_hint_caps_the_symbol(self):
        pe = self._engine()
        pe.capacity_hint("ETHUSD", 22.43)
        assert pe._capacity_cap("ETHUSD") == pytest.approx(22.43)

    def test_a_zero_or_negative_hint_is_ignored(self):
        """A pair reporting no capacity is unmeasured, not un-tradable at 0."""
        pe = self._engine()
        pe.capacity_hint("SOLUSD", 0.0)
        pe.capacity_hint("BTCUSD", -5.0)
        assert pe._capacity_cap("SOLUSD") is None
        assert pe._capacity_cap("BTCUSD") is None

    def test_the_cap_is_case_insensitive(self):
        pe = self._engine()
        pe.capacity_hint("ethusd", 61.75)
        assert pe._capacity_cap("ETHUSD") == pytest.approx(61.75)

    def test_allocate_applies_the_cap(self):
        """Asserted on allocate_detailed, which is where the walk lives --
        allocate() delegates to it, and checking the wrapper would have passed
        while the cap sat on an unused path."""
        import inspect
        from app.portfolio import PortfolioEngine

        assert "allocate_detailed(" in inspect.getsource(PortfolioEngine.allocate)
        src = inspect.getsource(PortfolioEngine.allocate_detailed)
        assert "_capacity_cap(" in src
        assert "min(target_notional, cap)" in src
