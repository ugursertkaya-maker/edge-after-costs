"""The tape recorder: it must capture faithfully and fail harmlessly."""

from __future__ import annotations

import gzip
import json

from app.recorder import TapeRecorder, replay


def messages(n: int) -> list[dict]:
    return [
        {"stream": "btcusd@aggTrade", "data": {"p": f"{100 + i}", "q": "0.5", "T": i, "m": i % 2 == 0}}
        for i in range(n)
    ]


def test_records_and_replays_exactly(tmp_path):
    rec = TapeRecorder(tmp_path)
    msgs = messages(50)
    for i, m in enumerate(msgs):
        rec.record(m, 1_700_000_000_000 + i)
    rec.close()

    out = list(replay(tmp_path))
    assert len(out) == 50
    assert [m for _, m in out] == msgs
    assert [r for r, _ in out] == [1_700_000_000_000 + i for i in range(50)]


def test_receive_time_is_stored_separately_from_exchange_time(tmp_path):
    """The gap between them is the latency a maker order actually faces, so it
    must survive into the recording rather than being collapsed."""
    rec = TapeRecorder(tmp_path)
    rec.record({"E": 1_700_000_000_000, "s": "BTCUSD"}, 1_700_000_000_250)
    rec.close()

    (received, msg), = list(replay(tmp_path))
    assert received == 1_700_000_000_250
    assert msg["E"] == 1_700_000_000_000


def test_files_roll_on_the_hour(tmp_path):
    rec = TapeRecorder(tmp_path)
    hour = 3_600_000
    rec.record({"a": 1}, 1_700_000_000_000)
    rec.flush(1_700_000_000_000)
    rec.record({"a": 2}, 1_700_000_000_000 + hour)
    rec.flush(1_700_000_000_000 + hour)
    rec.close()

    files = sorted(tmp_path.glob("tape-*.ndjson.gz"))
    assert len(files) == 2
    assert [m["a"] for _, m in replay(tmp_path)] == [1, 2]


def test_reopening_the_same_hour_appends_rather_than_truncates(tmp_path):
    """A worker restart inside the hour must not erase the hour."""
    first = TapeRecorder(tmp_path)
    first.record({"a": 1}, 1_700_000_000_000)
    first.close()

    second = TapeRecorder(tmp_path)
    second.record({"a": 2}, 1_700_000_000_001)
    second.close()

    assert [m["a"] for _, m in replay(tmp_path)] == [1, 2]


def test_replay_windows_by_receive_time(tmp_path):
    rec = TapeRecorder(tmp_path)
    for i in range(20):
        rec.record({"i": i}, 1_700_000_000_000 + i * 1000)
    rec.close()

    window = list(replay(tmp_path, start_ms=1_700_000_005_000, end_ms=1_700_000_009_000))
    assert [m["i"] for _, m in window] == [5, 6, 7, 8, 9]


def test_unserialisable_input_is_dropped_not_raised(tmp_path):
    rec = TapeRecorder(tmp_path)
    rec.record({"bad": object()}, 1_700_000_000_000)
    rec.record({"good": 1}, 1_700_000_000_001)
    rec.close()

    assert rec.stats.dropped == 1
    assert [m for _, m in replay(tmp_path)] == [{"good": 1}]


def test_a_disabled_recorder_is_a_no_op(tmp_path):
    rec = TapeRecorder(tmp_path, enabled=False)
    for m in messages(10):
        rec.record(m, 1_700_000_000_000)
    rec.close()
    assert list(tmp_path.glob("*")) == []
    assert rec.stats.messages == 0


def test_a_write_failure_disables_recording_instead_of_raising(tmp_path, monkeypatch):
    """A recorder problem must never take the trading loop down with it."""
    rec = TapeRecorder(tmp_path)

    def boom(*_a, **_k):
        raise OSError("disk on fire")

    monkeypatch.setattr(rec, "_open", boom)
    rec.record({"a": 1}, 1_700_000_000_000)
    rec.flush(1_700_000_000_000)

    assert rec.enabled is False
    assert "disk on fire" in (rec.stats.disabled_reason or "")
    rec.record({"a": 2}, 1_700_000_000_001)   # still silent


def test_a_nearly_full_disk_stops_recording(tmp_path, monkeypatch):
    import shutil as _shutil

    rec = TapeRecorder(tmp_path)
    rec.record({"a": 1}, 1_700_000_000_000)
    rec.flush(1_700_000_000_000)

    class Usage:
        free = 1024 ** 3  # 1 GB, under the floor

    monkeypatch.setattr(_shutil, "disk_usage", lambda _p: Usage())
    rec._last_sweep = 0.0
    rec._sweep(force=True)

    assert rec.enabled is False
    assert "free" in (rec.stats.disabled_reason or "")


def test_retention_deletes_old_files_but_never_the_open_one(tmp_path, monkeypatch):
    import os

    rec = TapeRecorder(tmp_path, retention_hours=1)
    rec.record({"a": 1}, 1_700_000_000_000)
    rec.flush(1_700_000_000_000)

    stale = tmp_path / "tape-19990101-00.ndjson.gz"
    with gzip.open(stale, "wt") as fh:
        fh.write(json.dumps({"r": 0, "m": {}}) + "\n")
    os.utime(stale, (0, 0))

    rec._last_sweep = 0.0
    rec._sweep(force=True)

    assert not stale.exists()
    assert rec.enabled is True
    assert list(tmp_path.glob("tape-*.ndjson.gz"))


def test_inventory_reports_what_is_on_disk(tmp_path):
    rec = TapeRecorder(tmp_path)
    for i in range(100):
        rec.record(messages(1)[0], 1_700_000_000_000 + i)
    rec.close()

    inv = rec.inventory()
    assert inv["files"] == 1
    assert inv["mb"] >= 0
    assert inv["newest"] and inv["newest"].startswith("tape-")


# --------------------------------------------------------------------------- #
# Worker integration: the tape must be replayable, not just large
# --------------------------------------------------------------------------- #


def test_the_worker_records_rest_snapshots_as_well_as_diffs(tmp_path, monkeypatch):
    """Depth diffs are deltas against a REST snapshot that never appears in the
    stream. Recording only the diffs produces a tape nobody can replay."""
    monkeypatch.setenv("TRADING_MODE", "PAPER")
    monkeypatch.setenv("RECORD_TAPE", "true")
    monkeypatch.setenv("TAPE_DIR", str(tmp_path))
    from app.config import reload_settings

    reload_settings()
    from app.runtime import RuntimeStore
    from app.worker.main import TradingWorker

    store = RuntimeStore(client=None)
    store._client = None
    w = TradingWorker(store=store)

    w.books.apply_snapshot("BTCUSD", 42, [(100.0, 1.0)], [(100.1, 2.0)], 1_700_000_000_000)
    w.feed.ingest_message(
        {"stream": "btcusd@aggTrade", "data": {"p": "100.05", "q": "0.1", "T": 1, "m": False}},
        1_700_000_000_100,
    )
    w.recorder.close()
    reload_settings()

    kinds = [m.get("e") or m.get("stream") for _, m in replay(tmp_path)]
    assert "__snapshot" in kinds, "the snapshot was not recorded"
    assert "btcusd@aggTrade" in kinds, "the stream message was not recorded"

    snap = next(m for _, m in replay(tmp_path) if m.get("e") == "__snapshot")
    assert snap["s"] == "BTCUSD" and snap["lastUpdateId"] == 42
    assert snap["b"] == [[100.0, 1.0]] and snap["a"] == [[100.1, 2.0]]


def test_recording_the_snapshot_does_not_break_the_book(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "PAPER")
    monkeypatch.setenv("RECORD_TAPE", "true")
    monkeypatch.setenv("TAPE_DIR", str(tmp_path))
    from app.config import reload_settings

    reload_settings()
    from app.runtime import RuntimeStore
    from app.worker.main import TradingWorker

    store = RuntimeStore(client=None)
    store._client = None
    w = TradingWorker(store=store)
    w.books.apply_snapshot("BTCUSD", 42, [(100.0, 1.0)], [(100.1, 2.0)], 1_700_000_000_000)
    w.recorder.close()
    reload_settings()

    book = w.books.get("BTCUSD")
    assert book is not None and book.best_bid == 100.0 and book.best_ask == 100.1


def test_the_stream_is_synced_so_a_live_file_can_be_read(tmp_path, monkeypatch):
    """gzip buffers internally: without a periodic sync a reader tailing the
    current hour sees nothing, and a crash loses minutes of tape."""
    import time as _time

    rec = TapeRecorder(tmp_path)
    rec.record({"a": 1}, 1_700_000_000_000)
    rec.flush(1_700_000_000_000)

    rec._last_sync = _time.time() - 120          # force the sync branch
    rec.record({"a": 2}, 1_700_000_000_001)
    rec.flush(1_700_000_000_001)

    # Readable without closing the recorder.
    seen = [m["a"] for _, m in replay(tmp_path)]
    assert seen == [1, 2]
    rec.close()
