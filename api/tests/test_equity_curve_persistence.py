"""The equity curve must survive a restart and an idle account.

Reported as "sayfada grafikler vardı şu anda yok". The data was never lost:
2,827 snapshots spanning 47.7 hours sat in `equity_snapshots` while the
dashboard served `Ledger.equity_points()`, an in-memory list that starts empty
on every boot. The rows had been written since the first commit and read by
nothing.

Two failures, both fixed here: history was not reloaded, and an account with
no open positions never stamped a point at all -- so a quiet bot drew no line
rather than a flat one.
"""

from __future__ import annotations

import pytest

from app.sim.ledger import Ledger


def _ledger() -> Ledger:
    return Ledger("base", 5_000.0, "sim-1.0.0")


class TestSeeding:
    def test_history_is_restored(self):
        lg = _ledger()
        assert lg.equity_points() == []
        n = lg.seed_equity_points([(100, 5000.0), (200, 5010.0)])
        assert n == 2
        assert lg.equity_points() == [(100, 5000.0), (200, 5010.0)]

    def test_seeding_never_overwrites_what_this_process_measured(self):
        """Only points OLDER than anything already stamped are taken."""
        lg = _ledger()
        lg.mark({}, 500)
        before = lg.equity_points()
        lg.seed_equity_points([(100, 1.0), (600, 999.0), (700, 999.0)])
        pts = lg.equity_points()
        assert pts[-1] == before[-1], "live point was rewritten"
        assert (600, 999.0) not in pts and (700, 999.0) not in pts
        assert pts[0] == (100, 1.0)

    def test_seeding_is_ordered_oldest_first(self):
        lg = _ledger()
        lg.mark({}, 900)
        lg.seed_equity_points([(100, 1.0), (200, 2.0)])
        ts = [p[0] for p in lg.equity_points()]
        assert ts == sorted(ts)

    def test_empty_history_is_a_no_op(self):
        lg = _ledger()
        assert lg.seed_equity_points([]) == 0
        assert lg.equity_points() == []


class TestIdleAccountStillHasACurve:
    def test_mark_with_no_positions_still_stamps_a_point(self):
        """An account holding nothing has a flat curve, not an absent one."""
        lg = _ledger()
        lg.mark({}, 1_000)
        lg.mark({}, 2_000)
        assert lg.equity_points() == [(1_000, 5_000.0), (2_000, 5_000.0)]

    def test_the_worker_no_longer_guards_the_stamp(self):
        import inspect
        from app.worker import main

        src = inspect.getsource(main.TradingWorker.monitor_once)
        assert "ledger.mark(prices, ts_ms)" in src
        stamp = src[src.index("ledger.mark(prices, ts_ms)") - 200:]
        assert "if prices:" not in stamp.split("ledger.mark")[0][-40:]

    def test_two_points_are_enough_for_the_chart_to_draw(self):
        """EquityCurve.tsx gates on points.length >= 2."""
        lg = _ledger()
        lg.mark({}, 1)
        assert len(lg.equity_points()) == 1
        lg.mark({}, 2)
        assert len(lg.equity_points()) >= 2


class TestWorkerSeedsAtBoot:
    def test_the_boot_path_calls_the_seed(self):
        import inspect
        from app.worker import main

        assert "_seed_equity_curves()" in inspect.getsource(main.TradingWorker)

    def test_a_failed_seed_never_stops_the_trader(self):
        import inspect
        from app.worker import main

        src = inspect.getsource(main.TradingWorker._seed_equity_curves)
        assert "except Exception" in src
