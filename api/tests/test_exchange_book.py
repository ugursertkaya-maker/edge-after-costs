"""Order-book maintenance: the Binance diff-depth protocol, exactly."""

from __future__ import annotations

from app.exchange.book import BookManager

SYM = "BTCUSD"
BIDS = [(61000.0, 0.5), (60999.0, 1.2), (60998.0, 2.0)]
ASKS = [(61001.0, 0.4), (61002.0, 1.1), (61003.0, 3.0)]
T0 = 1_725_000_000_000


def seeded(**kwargs) -> BookManager:
    """A manager with a snapshot at lastUpdateId=100 already applied."""
    bm = BookManager(**kwargs)
    bm.apply_snapshot(SYM, 100, BIDS, ASKS, T0)
    return bm


# --------------------------------------------------------------------------- #
# snapshot + happy path
# --------------------------------------------------------------------------- #


def test_snapshot_sorts_and_exposes_the_book():
    book = seeded().get(SYM)
    assert [lv.price for lv in book.bids] == [61000.0, 60999.0, 60998.0]
    assert [lv.price for lv in book.asks] == [61001.0, 61002.0, 61003.0]
    assert book.last_update_id == 100
    assert book.last_update_ms == T0
    assert book.desynced is False
    assert book.is_valid()


def test_first_diff_must_straddle_the_snapshot_id_then_stream_continues():
    bm = seeded()
    assert bm.apply_diff(SYM, 99, 105, [(61000.0, 0.9)], [], T0 + 100) is True
    assert bm.apply_diff(SYM, 106, 110, [], [(61001.0, 0.2)], T0 + 200) is True

    book = bm.get(SYM)
    assert book.bids[0].qty == 0.9
    assert book.asks[0].qty == 0.2
    assert book.last_update_id == 110
    assert book.last_update_ms == T0 + 200
    assert book.desynced is False
    assert bm.needs_resync() == []


def test_first_diff_that_does_not_cover_the_snapshot_desyncs():
    bm = seeded()
    # The snapshot is at 100 but the stream resumes at 150: we missed 101..149.
    assert bm.apply_diff(SYM, 150, 160, [], [], T0 + 100) is False
    assert bm.get(SYM).desynced is True
    assert bm.needs_resync() == [SYM]


def test_new_levels_insert_in_price_order():
    bm = seeded()
    bm.apply_diff(SYM, 101, 102, [(60999.5, 0.3)], [(61001.5, 0.7)], T0 + 100)
    book = bm.get(SYM)
    assert [lv.price for lv in book.bids] == [61000.0, 60999.5, 60999.0, 60998.0]
    assert [lv.price for lv in book.asks] == [61001.0, 61001.5, 61002.0, 61003.0]


# --------------------------------------------------------------------------- #
# ordering rules
# --------------------------------------------------------------------------- #


def test_events_already_covered_by_the_snapshot_are_dropped_not_fatal():
    bm = seeded()
    assert bm.apply_diff(SYM, 90, 95, [(61000.0, 9.9)], [], T0 + 50) is True

    book = bm.get(SYM)
    assert book.bids[0].qty == 0.5, "a stale event must not touch the book"
    assert book.last_update_id == 100
    assert book.desynced is False
    assert bm.stats()["symbols"][SYM]["diffs_dropped"] == 1


def test_sequence_gap_returns_false_sets_desynced_and_is_recorded():
    bm = seeded()
    assert bm.apply_diff(SYM, 101, 110, [], [], T0 + 100) is True
    assert bm.apply_diff(SYM, 115, 120, [(61000.0, 7.0)], [], T0 + 200) is False

    book = bm.get(SYM)
    assert book.desynced is True
    assert book.bids[0].qty == 0.5, "a gapped event must not be applied"
    assert bm.needs_resync() == [SYM]
    assert bm.stats()["symbols"][SYM]["desyncs"] == 1
    assert "gap" in bm.desync_events[-1][1]


def test_a_fresh_snapshot_clears_the_desync():
    bm = seeded()
    bm.apply_diff(SYM, 101, 110, [], [], T0 + 100)
    bm.apply_diff(SYM, 115, 120, [], [], T0 + 200)
    assert bm.needs_resync() == [SYM]

    bm.apply_snapshot(SYM, 130, BIDS, ASKS, T0 + 300)
    assert bm.get(SYM).desynced is False
    assert bm.needs_resync() == []
    assert bm.apply_diff(SYM, 131, 132, [], [], T0 + 400) is True


# --------------------------------------------------------------------------- #
# buffering before the snapshot
# --------------------------------------------------------------------------- #


def test_diffs_arriving_before_the_snapshot_are_buffered_and_replayed():
    bm = BookManager()
    # These land while the REST snapshot is still in flight.
    assert bm.apply_diff(SYM, 95, 99, [(60990.0, 5.0)], [], T0 - 200) is True
    assert bm.apply_diff(SYM, 100, 104, [(61000.0, 0.75)], [], T0 - 100) is True
    assert bm.apply_diff(SYM, 105, 108, [], [(61001.0, 0.25)], T0 - 50) is True
    assert bm.get(SYM).last_update_id == 0

    bm.apply_snapshot(SYM, 101, BIDS, ASKS, T0)

    book = bm.get(SYM)
    # 95..99 is entirely inside the snapshot and is discarded; 100..104 straddles
    # it and is applied; 105..108 follows contiguously.
    assert 60990.0 not in [lv.price for lv in book.bids]
    assert book.bids[0].qty == 0.75
    assert book.asks[0].qty == 0.25
    assert book.last_update_id == 108
    assert book.desynced is False
    counters = bm.stats()["symbols"][SYM]
    assert counters["diffs_buffered"] == 3
    assert counters["diffs_applied"] == 2
    assert counters["buffered"] == 0


def test_buffered_replay_that_gaps_desyncs_immediately():
    bm = BookManager()
    bm.apply_diff(SYM, 140, 150, [], [], T0 - 100)
    bm.apply_snapshot(SYM, 100, BIDS, ASKS, T0)
    assert bm.get(SYM).desynced is True
    assert bm.needs_resync() == [SYM]


# --------------------------------------------------------------------------- #
# level mechanics
# --------------------------------------------------------------------------- #


def test_zero_quantity_removes_a_level():
    bm = seeded()
    bm.apply_diff(SYM, 101, 102, [(61000.0, 0.0)], [(61002.0, 0.0)], T0 + 100)

    book = bm.get(SYM)
    assert [lv.price for lv in book.bids] == [60999.0, 60998.0]
    assert [lv.price for lv in book.asks] == [61001.0, 61003.0]
    assert book.best_bid == 60999.0


def test_book_is_truncated_to_max_levels():
    bm = BookManager(max_levels=2)
    bm.apply_snapshot(SYM, 100, BIDS, ASKS, T0)
    book = bm.get(SYM)
    assert len(book.bids) == 2 and len(book.asks) == 2
    assert [lv.price for lv in book.bids] == [61000.0, 60999.0]


# --------------------------------------------------------------------------- #
# bookTicker / partial depth
# --------------------------------------------------------------------------- #


def test_book_ticker_builds_the_top_of_book_for_a_warm_symbol():
    bm = BookManager()
    bm.apply_book_ticker("ethusd", 2500.0, 3.0, 2500.5, 4.0, T0)

    book = bm.get("ETHUSD")
    assert book.best_bid == 2500.0 and book.bids[0].qty == 3.0
    assert book.best_ask == 2500.5 and book.asks[0].qty == 4.0
    assert book.last_update_ms == T0
    assert book.desynced is False
    assert bm.needs_resync() == [], "a bookTicker-only symbol never needs a resync"

    bm.apply_book_ticker("ETHUSD", 2501.0, 1.0, 2501.5, 2.0, T0 + 100)
    book = bm.get("ETHUSD")
    assert book.best_bid == 2501.0 and len(book.bids) == 1
    assert book.best_ask == 2501.5 and len(book.asks) == 1


def test_book_ticker_refreshes_a_matching_top_of_a_diff_book():
    bm = seeded()
    bm.apply_book_ticker(SYM, 61000.0, 8.0, 61001.0, 9.0, T0 + 100)

    book = bm.get(SYM)
    assert book.bids[0].qty == 8.0 and book.asks[0].qty == 9.0
    assert len(book.bids) == 3, "deeper levels must survive"
    assert book.last_update_ms == T0 + 100


def test_book_ticker_never_corrupts_a_healthy_diff_book():
    bm = seeded()
    bm.apply_book_ticker(SYM, 60995.0, 8.0, 61010.0, 9.0, T0 + 100)

    book = bm.get(SYM)
    assert [lv.price for lv in book.bids] == [61000.0, 60999.0, 60998.0]
    assert [lv.price for lv in book.asks] == [61001.0, 61002.0, 61003.0]
    assert bm.stats()["symbols"][SYM]["book_tickers_ignored"] == 1


def test_crossed_or_garbage_book_ticker_is_ignored():
    bm = BookManager()
    bm.apply_book_ticker("XRPUSD", 0.6, 1.0, 0.5, 1.0, T0)  # ask below bid
    bm.apply_book_ticker("XRPUSD", 0.0, 1.0, 0.5, 1.0, T0)
    book = bm.get("XRPUSD")
    assert book is not None and not book.bids and not book.asks
    assert bm.stats()["symbols"]["XRPUSD"]["book_tickers_ignored"] == 2


def test_partial_depth_feeds_warm_symbols_but_not_a_healthy_diff_book():
    bm = BookManager()
    bm.apply_partial("adausd", 55, [(0.40, 100.0)], [(0.41, 120.0)], T0)
    warm = bm.get("ADAUSD")
    assert warm.best_bid == 0.40 and warm.best_ask == 0.41
    assert warm.last_update_id == 55

    hot = seeded(max_levels=50)
    hot.apply_partial(SYM, 999, [(1.0, 1.0)], [(2.0, 1.0)], T0 + 100)
    assert hot.get(SYM).best_bid == 61000.0
    assert hot.stats()["symbols"][SYM]["partials"] == 0

    # ...but once it is desynced, a partial is better than nothing.
    hot.mark_desynced(SYM, "test")
    hot.apply_partial(SYM, 999, [(60000.0, 1.0)], [(60001.0, 1.0)], T0 + 200)
    assert hot.get(SYM).best_bid == 60000.0


# --------------------------------------------------------------------------- #
# bookkeeping
# --------------------------------------------------------------------------- #


def test_get_returns_none_for_an_unknown_symbol():
    assert BookManager().get("NOPEUSD") is None


def test_stats_counts_per_symbol_and_in_total():
    bm = seeded()
    bm.apply_diff(SYM, 101, 102, [], [], T0 + 10)
    bm.apply_book_ticker("ETHUSD", 2500.0, 1.0, 2500.5, 1.0, T0 + 20)
    bm.mark_desynced(SYM, "manual")

    stats = bm.stats()
    assert set(stats["symbols"]) == {SYM, "ETHUSD"}
    assert stats["symbols"][SYM]["snapshots"] == 1
    assert stats["symbols"][SYM]["diffs_applied"] == 1
    assert stats["symbols"][SYM]["mode"] == "diff"
    assert stats["symbols"]["ETHUSD"]["mode"] == "ticker"
    assert stats["totals"]["book_tickers"] == 1
    assert stats["totals"]["desyncs"] == 1
    assert stats["desynced_symbols"] == [SYM]
    assert stats["tracked"] == 2
