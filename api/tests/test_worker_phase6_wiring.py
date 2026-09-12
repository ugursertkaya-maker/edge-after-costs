"""The Phase 6 components are reachable from the worker.

This project's recurring defect is not broken code, it is correct code that
production never calls: Phase 5's tiering was built, tested and left
unreachable for an entire phase. These tests assert the wiring itself, because
a passing unit test for `CapitalRecycler` says nothing about whether closing a
position calls it.
"""

from __future__ import annotations

import inspect

import pytest

from app.worker import main as worker_main


def _source(fn) -> str:
    return inspect.getsource(fn)


class TestUniverseUsesTheTierPath:
    def test_refresh_universe_calls_refresh_tiers(self):
        src = _source(worker_main.TradingWorker.refresh_universe)
        assert "refresh_tiers" in src

    def test_the_old_path_survives_as_a_fallback(self):
        """A failed depth probe must degrade, not empty the universe."""
        src = _source(worker_main.TradingWorker.refresh_universe)
        assert "universe_selector.refresh(" in src


class TestHoldingIsArmedAtEntry:
    def test_open_position_arms_the_holding_snapshot(self):
        src = _source(worker_main.TradingWorker._open_position)
        assert "holding.arm(" in src

    def test_arming_happens_before_the_ledger_persists_the_position(self):
        """meta is written by open_position; a later snapshot is not durable."""
        src = _source(worker_main.TradingWorker._open_position)
        assert src.index("holding.arm(") < src.index("ledger.open(")

    def test_monitor_passes_live_score_and_predicted_ev(self):
        src = _source(worker_main.TradingWorker.monitor_once)
        assert "live_score=" in src
        assert "predicted_ev_bps=" in src


class TestCapitalRecycling:
    def test_worker_builds_a_recycler(self):
        src = _source(worker_main.TradingWorker.__init__)
        assert "CapitalRecycler(" in src

    def test_rescan_is_evaluate_once_itself(self):
        """Otherwise 'event-driven rescan' waits for the next tick anyway."""
        src = _source(worker_main.TradingWorker.__init__)
        assert "rescan=self.evaluate_once" in src

    def test_closing_a_position_releases_and_rescans(self):
        src = _source(worker_main.TradingWorker._close_position)
        assert "recycler.on_close(" in src

    def test_only_the_base_ledger_triggers_a_rescan(self):
        """The comparison ledgers mirror the same decisions; three rescans per
        close would be three times the work for one decision."""
        src = _source(worker_main.TradingWorker._close_position)
        head = src[: src.index("recycler.on_close(")]
        assert 'ledger.name == "base"' in head

    def test_entry_claims_the_episode(self):
        src = _source(worker_main.TradingWorker._open_position)
        assert "recycler.note_entry(" in src


class TestDashboardPayload:
    def test_payload_carries_every_phase6_block(self):
        src = _source(worker_main.TradingWorker.snapshot_payload) \
            if hasattr(worker_main.TradingWorker, "snapshot_payload") else None
        if src is None:
            src = inspect.getsource(worker_main.TradingWorker)
        for key in ('"board"', '"funnel"', '"opportunities"', '"recycling"'):
            assert key in src, f"{key} missing from the dashboard payload"

    def test_idle_attribution_receives_the_recycler_context(self):
        """Without it, SCANNER_DELAY and CAPITAL_STATE_BUG can never fire."""
        src = _source(worker_main.TradingWorker._capital_block)
        assert "context=self.recycler.idle_context(" in src


class TestFunnelLadderStaysMonotone:
    """A ranked symbol must not leave earlier stages at zero.

    This failed intermittently in production: only the symbols that actually
    ranked tripped it, so an empty `violations` list meant "nothing ranked this
    second" rather than "the instrumentation is sound".
    """

    def test_ranking_marks_every_preceding_stage(self):
        from app.research.funnel import STAGES, FunnelReport

        report = FunnelReport()
        f = report.for_symbol("SOLUSD")
        for stage in ("discovered", "subscribed", "data_healthy", "features",
                      "strategy_evaluated", "model_evaluated"):
            f.reached(stage)
        # What the worker now does for a ranked opportunity.
        for stage in STAGES[: STAGES.index("ranked") + 1]:
            if f.stages.get(stage, 0) == 0:
                f.reached(stage)

        assert report.check() == []
        assert f.bottleneck()[0] == "selected"

    def test_the_worker_marks_the_whole_prefix(self):
        src = _source(worker_main.TradingWorker._record_funnel)
        assert 'FUNNEL_STAGES[: FUNNEL_STAGES.index("ranked") + 1]' in src


class TestShortSideIsRecordedNotDiscarded:
    """Phase 6 §17: spot is long-only, but a short signal is still a prediction.

    Discarding it silently throws away exactly the information needed to know
    how much edge lives on the side we cannot trade. Never faked, never mixed
    into the realised book.
    """

    def test_the_reject_path_records_the_short(self):
        src = _source(worker_main.TradingWorker.try_enter)
        assert '"NOT_SUPPORTED_SHORT"' in src
        assert "_note_unexecutable_short(" in src

    def test_the_block_marks_them_unexecutable(self):
        src = _source(worker_main.TradingWorker._short_side_block)
        assert '"executable": False' in src

    def test_it_records_expected_edge_not_an_outcome(self):
        """Nothing here may claim the trade would have won.

        Asserted on the recorded field names rather than by grepping the
        source for words -- the first version of this test failed on its own
        docstring, which is a test of prose, not of behaviour.
        """
        worker = worker_main.TradingWorker.__new__(worker_main.TradingWorker)
        worker._unexecutable_shorts = {}

        class _Sig:
            strategy = "flow_impulse"

        class _Opp:
            signal = _Sig()
            net_edge_bps = 7.5

        worker_main.TradingWorker._note_unexecutable_short(worker, _Opp())
        row = worker._unexecutable_shorts["flow_impulse"]
        assert set(row) == {"signals", "sum_net_edge_bps", "best_net_edge_bps"}
        assert row["signals"] == 1.0
        assert row["sum_net_edge_bps"] == 7.5

    def test_payload_carries_the_short_side_block(self):
        src = inspect.getsource(worker_main.TradingWorker)
        assert '"short_side"' in src


class TestShortSideBestEdgeIsObserved:
    """A 'best' of 0.0 for a strategy whose every short had negative edge is a
    number nothing observed. Caught on the first live sample."""

    def _worker(self):
        w = worker_main.TradingWorker.__new__(worker_main.TradingWorker)
        w._unexecutable_shorts = {}
        return w

    def _opp(self, edge: float, strategy: str = "book_imbalance"):
        class _Sig:
            pass
        sig = _Sig()
        sig.strategy = strategy
        class _Opp:
            pass
        o = _Opp()
        o.signal = sig
        o.net_edge_bps = edge
        return o

    def test_best_reflects_the_least_bad_observation_not_zero(self):
        w = self._worker()
        for edge in (-25.0, -19.5, -12.0):
            worker_main.TradingWorker._note_unexecutable_short(w, self._opp(edge))
        block = worker_main.TradingWorker._short_side_block(w)
        row = block["by_strategy"]["book_imbalance"]
        assert row["best_net_edge_bps"] == -12.0
        assert row["mean_net_edge_bps"] == pytest.approx(-18.833, abs=1e-3)
        assert row["signals"] == 3

    def test_no_signals_means_no_row(self):
        block = worker_main.TradingWorker._short_side_block(self._worker())
        assert block == {"by_strategy": {}, "total_signals": 0}
