"""The board the allocator chooses from, and the concentration flag.

Phase 6 §4 and §21. Two properties matter more than the rendering:

* a refused symbol is a ROW with a reason, not an absence -- "why did we not
  trade XRP" must be answerable from the screen;
* the BTC concentration flag is a diagnostic and must never change what trades.
"""

from __future__ import annotations

import pytest

from app.research.funnel import STAGES, FunnelReport, SymbolFunnel


class TestFunnelLadder:
    def test_stages_are_ordered_and_unique(self):
        assert len(set(STAGES)) == len(STAGES)
        assert STAGES[0] == "discovered"
        assert STAGES[-1] == "traded"

    def test_counts_that_go_back_up_are_a_bug_not_a_finding(self):
        f = SymbolFunnel("BTCUSD")
        f.reached("discovered")
        f.reached("subscribed", 5)          # more subscribed than discovered
        assert f.check(), "monotonicity violation must be reported"

    def test_a_clean_funnel_has_no_violations(self):
        f = SymbolFunnel("BTCUSD")
        for stage in STAGES:
            f.reached(stage)
        assert f.check() == []

    def test_bottleneck_names_the_wall_not_the_last_thing_that_happened(self):
        f = SymbolFunnel("XRPUSD")
        f.reached("discovered")
        f.reached("subscribed")
        f.dropped("data_healthy", "no market snapshot")
        assert f.bottleneck() == ("data_healthy", "no market snapshot")

    def test_a_symbol_that_never_arrived_says_so(self):
        """A stage can be blocked with no reason recorded there."""
        f = SymbolFunnel("ADAUSD")
        f.reached("discovered")
        stage, reason = f.bottleneck()
        assert stage == "subscribed"
        assert reason == "never reached"

    def test_a_symbol_that_traded_has_no_wall(self):
        f = SymbolFunnel("BTCUSD")
        for stage in STAGES:
            f.reached(stage)
        assert f.bottleneck() == ("traded", "")

    def test_reason_is_inherited_from_the_last_stage_that_recorded_one(self):
        """The wall is often one stage past where the reason was written."""
        f = SymbolFunnel("ADAUSD")
        f.reached("discovered")
        f.reached("subscribed")
        f.dropped("candidate", "absorbs $5.61/min, below the $10.00 minimum order")
        stage, reason = f.bottleneck()
        assert stage == "data_healthy"
        assert "5.61" in reason

    def test_unknown_stage_is_rejected_rather_than_silently_recorded(self):
        f = SymbolFunnel("BTCUSD")
        with pytest.raises(KeyError):
            f.reached("nonsense")
        with pytest.raises(KeyError):
            f.dropped("nonsense", "why")


class TestConcentration:
    def _report(self, traded: dict[str, int]) -> FunnelReport:
        r = FunnelReport()
        for symbol, n in traded.items():
            f = r.for_symbol(symbol)
            for stage in STAGES[:-1]:
                f.reached(stage)
            if n:
                f.reached("traded", n)
        return r

    def test_share_is_a_fraction_of_trades(self):
        r = self._report({"BTCUSD": 58, "ETHUSD": 51, "SOLUSD": 30})
        share = r.concentration()
        assert share["BTCUSD"] == pytest.approx(58 / 139)
        assert sum(share.values()) == pytest.approx(1.0)

    def test_the_live_sample_is_not_btc_dominated(self):
        """41.7%, measured. The brief's premise that BTC dominates is wrong."""
        r = self._report({"BTCUSD": 58, "ETHUSD": 51, "SOLUSD": 30})
        assert r.concentration()["BTCUSD"] < 0.70

    def test_no_trades_yields_no_share_rather_than_a_divide_by_zero(self):
        assert self._report({"BTCUSD": 0}).concentration() == {}

    def test_symbols_that_never_traded_are_absent_not_zero(self):
        r = self._report({"BTCUSD": 10, "ADAUSD": 0})
        assert "ADAUSD" not in r.concentration()

    def test_wall_groups_symbols_by_where_they_died(self):
        r = FunnelReport()
        for sym in ("AAVEUSD", "ALGOUSD"):
            r.for_symbol(sym).reached("discovered")
            r.for_symbol(sym).dropped("subscribed", "24h quote volume too low")
        good = r.for_symbol("BTCUSD")
        for stage in STAGES:
            good.reached(stage)
        assert r.wall() == {"subscribed": 2, "traded": 1}
