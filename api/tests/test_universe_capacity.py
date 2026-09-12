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

    def test_fails_only_when_untradable_at_any_size(self):
        """ADAUSD: absorbs $5.61 a minute against a $10 minimum order.

        This is the explanation for a real observation -- ADAUSD sat in the HOT
        tier for the whole of Phase 5 and never once traded.
        """
        e = _entry(symbol="ADAUSD", capacity_max_usd=5.61, min_tradable_usd=10.0)
        assert e.capacity_ok is False

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
