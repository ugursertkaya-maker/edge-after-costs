"""Queue-aware maker replay — deterministic, offline, hand-written tape.

Every fixture here is a short sequence of book updates and prints built so the
expected outcome is obvious by inspection.  No RNG, no wall clock, no network,
no files except the ones the format tests write into ``tmp_path``.

The test that matters most is :func:`test_touched_is_not_filled`.  It is the
one the whole module exists to make true.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
from argparse import Namespace
from pathlib import Path

import pytest

from app.research.maker_replay import (
    ADVERSE_SELECTION_HORIZON,
    MIN_QUOTES,
    MakerReplay,
    QuoteSignal,
    ReplayBook,
    TapeEvent,
    TapeSlice,
    iter_tape_messages,
    load_tape,
    parse_message,
    perf_stats,
    signals_from_trades,
    tape_coverage,
)
from app.types import ClosedTrade, ExitReason, Side

SYM = "BTCUSD"
TICK = 0.01
T0 = 1_700_000_000_000


# --------------------------------------------------------------------------- #
# fixture builders — small enough to read, explicit enough to reason about
# --------------------------------------------------------------------------- #


def ticker(ts_ms: int, bid: float, bid_qty: float, ask: float, ask_qty: float) -> TapeEvent:
    return TapeEvent(
        ts_ms=ts_ms, symbol=SYM, kind="ticker", bids=((bid, bid_qty),), asks=((ask, ask_qty),)
    )


def depth(ts_ms: int, *, bids=(), asks=()) -> TapeEvent:
    return TapeEvent(
        ts_ms=ts_ms, symbol=SYM, kind="depth", bids=tuple(bids), asks=tuple(asks)
    )


def seller_hits(ts_ms: int, price: float, qty: float) -> TapeEvent:
    """A print whose aggressor is a SELLER — the only thing that fills our bid."""
    return TapeEvent(
        ts_ms=ts_ms, symbol=SYM, kind="trade", price=price, qty=qty, buyer_is_maker=True
    )


def buyer_lifts(ts_ms: int, price: float, qty: float) -> TapeEvent:
    """A print whose aggressor is a BUYER — it can never fill our bid."""
    return TapeEvent(
        ts_ms=ts_ms, symbol=SYM, kind="trade", price=price, qty=qty, buyer_is_maker=False
    )


def tape(*events: TapeEvent) -> TapeSlice:
    return TapeSlice(symbol=SYM, events=sorted(events, key=lambda e: e.ts_ms), tick_size=TICK)


def book_with(bid: float, bid_qty: float, ask: float, ask_qty: float) -> ReplayBook:
    b = ReplayBook(SYM)
    b.apply(ticker(T0, bid, bid_qty, ask, ask_qty))
    return b


@pytest.fixture
def replay(settings):
    """Real settings (Tier-0 fees: 0 bps maker, 1 bps taker on BTCUSD)."""
    return MakerReplay(settings=settings, tape_dir="/nonexistent")


# --------------------------------------------------------------------------- #
# 1. touched is NOT filled — the central lie of naive maker backtests
# --------------------------------------------------------------------------- #


def test_touched_is_not_filled(replay):
    """Price reached our level and traded there — but not enough to reach us.

    A bar-based backtest sees ``low <= 100.00`` and books a fill.  There were
    5 units in front of us and only 3 traded.  We are still in the queue.
    """
    book = book_with(100.00, 5.0, 100.02, 5.0)
    t = tape(
        ticker(T0, 100.00, 5.0, 100.02, 5.0),
        seller_hits(T0 + 1_000, 100.00, 3.0),   # touches our level, 3 of 5 ahead
        ticker(T0 + 9_000, 100.00, 2.0, 100.02, 5.0),
    )

    out = replay.simulate_quote(
        symbol=SYM, side=Side.LONG, price=100.00, qty=1.0,
        from_ms=T0, timeout_ms=5_000, book_state=book, tape=t,
    )

    assert out.touched is True, "the price did reach our level"
    assert out.filled is False, "but nothing traded through to us"
    assert out.fill_qty == 0.0
    assert out.missed_reason == "timeout"
    # Two units of the original five are still in front of us.
    assert out.queue_ahead_usd == pytest.approx(2.0 * 100.00)


def test_touch_without_any_trade_is_not_a_fill(replay):
    """The quote sits at the touch, the touch never trades.  No fill, no touch."""
    book = book_with(100.00, 4.0, 100.02, 4.0)
    t = tape(
        ticker(T0, 100.00, 4.0, 100.02, 4.0),
        ticker(T0 + 2_000, 100.00, 4.0, 100.02, 4.0),
        ticker(T0 + 9_000, 100.00, 4.0, 100.02, 4.0),
    )
    out = replay.simulate_quote(
        symbol=SYM, side=Side.LONG, price=100.00, qty=1.0,
        from_ms=T0, timeout_ms=5_000, book_state=book, tape=t,
    )
    assert (out.filled, out.touched) == (False, False)
    assert out.missed_reason == "timeout"


def test_wrong_side_aggressor_cannot_fill_our_bid(replay):
    """Buyers lifting the ask do not fill a resting bid, however much prints."""
    book = book_with(100.00, 0.0, 100.02, 5.0)
    t = tape(
        ticker(T0, 100.00, 0.0, 100.02, 5.0),
        buyer_lifts(T0 + 1_000, 100.00, 50.0),
        ticker(T0 + 9_000, 100.00, 0.0, 100.02, 5.0),
    )
    out = replay.simulate_quote(
        symbol=SYM, side=Side.LONG, price=100.00, qty=1.0,
        from_ms=T0, timeout_ms=5_000, book_state=book, tape=t,
    )
    assert out.filled is False
    assert out.touched is False


# --------------------------------------------------------------------------- #
# 2. queue ahead must be consumed first
# --------------------------------------------------------------------------- #


def test_fills_only_after_the_queue_ahead_is_consumed(replay):
    """5 ahead of us, we want 2.  The first 3 do nothing; the next 4 fill us."""
    book = book_with(100.00, 5.0, 100.02, 5.0)
    t = tape(
        ticker(T0, 100.00, 5.0, 100.02, 5.0),
        seller_hits(T0 + 1_000, 100.00, 3.0),   # queue 5 -> 2, still nothing for us
        seller_hits(T0 + 2_000, 100.00, 4.0),   # eats the last 2, then 2 for us
        ticker(T0 + 9_000, 100.00, 1.0, 100.02, 5.0),
    )

    out = replay.simulate_quote(
        symbol=SYM, side=Side.LONG, price=100.00, qty=2.0,
        from_ms=T0, timeout_ms=5_000, book_state=book, tape=t,
    )

    assert out.filled is True
    assert out.fill_qty == pytest.approx(2.0)
    assert out.fill_price == pytest.approx(100.00)
    assert out.fill_ms == T0 + 2_000
    assert out.wait_ms == 2_000
    assert out.queue_ahead_start_usd == pytest.approx(500.0)
    assert out.queue_ahead_usd == pytest.approx(0.0)
    assert out.partial is False


def test_queue_shrinks_as_trades_consume_the_level(replay):
    """The queue in front of us decays print by print, and we can watch it."""
    book = book_with(100.00, 6.0, 100.02, 5.0)
    seen: list[float] = []
    for cut in (0, 1, 2, 3):
        events = [ticker(T0, 100.00, 6.0, 100.02, 5.0)]
        events += [seller_hits(T0 + 1_000 * (i + 1), 100.00, 2.0) for i in range(cut)]
        events.append(ticker(T0 + 4_500, 100.00, 6.0 - 2.0 * cut, 100.02, 5.0))
        out = replay.simulate_quote(
            symbol=SYM, side=Side.LONG, price=100.00, qty=1.0,
            from_ms=T0, timeout_ms=5_000, book_state=book, tape=tape(*events),
        )
        seen.append(out.queue_ahead_usd)

    assert seen == [
        pytest.approx(600.0),
        pytest.approx(400.0),
        pytest.approx(200.0),
        pytest.approx(0.0),
    ]
    assert seen == sorted(seen, reverse=True), "queue ahead only ever shrinks"


def test_trade_through_sweeps_the_level_and_fills_us(replay):
    """A print *below* our bid means the level was swept on the way past."""
    book = book_with(100.00, 5.0, 100.02, 5.0)
    t = tape(
        ticker(T0, 100.00, 5.0, 100.02, 5.0),
        seller_hits(T0 + 500, 99.98, 4.0),   # printed through us
        ticker(T0 + 9_000, 99.97, 3.0, 99.99, 3.0),
    )
    out = replay.simulate_quote(
        symbol=SYM, side=Side.LONG, price=100.00, qty=1.5,
        from_ms=T0, timeout_ms=5_000, book_state=book, tape=t,
    )
    assert out.filled is True
    assert out.fill_qty == pytest.approx(1.5)
    assert out.queue_ahead_usd == pytest.approx(0.0)


def test_short_side_is_mirrored(replay):
    """A resting ask is lifted by buyers, and only by buyers."""
    book = book_with(100.00, 5.0, 100.02, 4.0)
    t = tape(
        ticker(T0, 100.00, 5.0, 100.02, 4.0),
        seller_hits(T0 + 500, 100.02, 10.0),    # wrong aggressor for an ask
        buyer_lifts(T0 + 1_000, 100.02, 3.0),   # eats 3 of the 4 ahead
        buyer_lifts(T0 + 2_000, 100.02, 2.0),   # last 1 ahead, then 1 for us
        ticker(T0 + 9_000, 100.00, 5.0, 100.02, 1.0),
    )
    out = replay.simulate_quote(
        symbol=SYM, side=Side.SHORT, price=100.02, qty=1.0,
        from_ms=T0, timeout_ms=5_000, book_state=book, tape=t,
    )
    assert out.filled is True
    assert out.fill_qty == pytest.approx(1.0)
    assert out.fill_ms == T0 + 2_000


# --------------------------------------------------------------------------- #
# 3. partial fill
# --------------------------------------------------------------------------- #


def test_partial_fill_takes_only_what_printed(replay):
    """We want 2.0, the tape only gives 0.6 through our level.  We get 0.6."""
    book = book_with(100.00, 0.0, 100.02, 5.0)  # nobody ahead of us
    t = tape(
        ticker(T0, 100.00, 0.0, 100.02, 5.0),
        seller_hits(T0 + 1_000, 100.00, 0.6),
        ticker(T0 + 9_000, 100.00, 0.0, 100.02, 5.0),
    )
    out = replay.simulate_quote(
        symbol=SYM, side=Side.LONG, price=100.00, qty=2.0,
        from_ms=T0, timeout_ms=5_000, book_state=book, tape=t,
    )
    assert out.filled is True
    assert out.partial is True
    assert out.fill_qty == pytest.approx(0.6)
    assert out.requested_qty == pytest.approx(2.0)
    assert out.missed_reason == "partial_fill"


def test_partial_then_completed_by_a_later_print(replay):
    book = book_with(100.00, 0.0, 100.02, 5.0)
    t = tape(
        ticker(T0, 100.00, 0.0, 100.02, 5.0),
        seller_hits(T0 + 1_000, 100.00, 0.6),
        seller_hits(T0 + 2_000, 100.00, 0.9),
        ticker(T0 + 9_000, 100.00, 0.0, 100.02, 5.0),
    )
    out = replay.simulate_quote(
        symbol=SYM, side=Side.LONG, price=100.00, qty=1.0,
        from_ms=T0, timeout_ms=5_000, book_state=book, tape=t,
    )
    assert out.fill_qty == pytest.approx(1.0)
    assert out.partial is False
    assert out.missed_reason is None
    assert out.fill_ms == T0 + 2_000


# --------------------------------------------------------------------------- #
# 4. aging / cancellation
# --------------------------------------------------------------------------- #


def test_quote_older_than_the_timeout_is_cancelled(replay):
    """The fill arrives one second after we would have pulled the quote."""
    book = book_with(100.00, 0.0, 100.02, 5.0)
    t = tape(
        ticker(T0, 100.00, 0.0, 100.02, 5.0),
        seller_hits(T0 + 6_000, 100.00, 10.0),   # after the 5s timeout
        ticker(T0 + 9_000, 100.00, 0.0, 100.02, 5.0),
    )
    out = replay.simulate_quote(
        symbol=SYM, side=Side.LONG, price=100.00, qty=1.0,
        from_ms=T0, timeout_ms=5_000, book_state=book, tape=t,
    )
    assert out.filled is False
    assert out.missed_reason == "timeout"
    assert out.wait_ms == 5_000
    assert out.resolved is True


def test_the_same_print_inside_a_longer_timeout_does_fill(replay):
    """Same tape, longer patience — the timeout is doing the work, not luck."""
    book = book_with(100.00, 0.0, 100.02, 5.0)
    t = tape(
        ticker(T0, 100.00, 0.0, 100.02, 5.0),
        seller_hits(T0 + 6_000, 100.00, 10.0),
        ticker(T0 + 9_000, 100.00, 0.0, 100.02, 5.0),
    )
    out = replay.simulate_quote(
        symbol=SYM, side=Side.LONG, price=100.00, qty=1.0,
        from_ms=T0, timeout_ms=30_000, book_state=book, tape=t,
    )
    assert out.filled is True
    assert out.wait_ms == 6_000


# --------------------------------------------------------------------------- #
# 5. cancellations we cannot see, and which way the estimate errs
# --------------------------------------------------------------------------- #


def test_cancellation_ahead_of_us_is_an_estimate_bounded_by_the_ratio(settings):
    """Level 5 (all ahead of us) grows to 12, then shrinks to 8 with no prints.

    Four units cancelled somewhere in a queue we cannot see inside.  The
    optimistic model puts all four in front of us (queue 5 -> 1), the
    pessimistic model none (queue stays 5), and the default splits it (-> 3).
    Nothing in the public feed can settle it, which is the point.
    """
    book = book_with(100.00, 5.0, 100.02, 5.0)
    events = tape(
        ticker(T0, 100.00, 5.0, 100.02, 5.0),
        depth(T0 + 500, bids=((100.00, 12.0),)),   # 7 joined behind us
        depth(T0 + 1_000, bids=((100.00, 8.0),)),  # 4 cancelled, whose?
        ticker(T0 + 9_000, 100.00, 8.0, 100.02, 5.0),
    )
    got = {}
    for ratio in (0.0, 0.5, 1.0):
        r = MakerReplay(settings=settings, tape_dir="/nonexistent", cancel_ahead_ratio=ratio)
        out = r.simulate_quote(
            symbol=SYM, side=Side.LONG, price=100.00, qty=1.0,
            from_ms=T0, timeout_ms=5_000, book_state=book, tape=events,
        )
        got[ratio] = out.queue_ahead_usd

    assert got[0.0] == pytest.approx(500.0)   # pessimistic: nobody ahead left
    assert got[0.5] == pytest.approx(300.0)
    assert got[1.0] == pytest.approx(100.0)   # optimistic
    assert got[0.0] > got[0.5] > got[1.0]


def test_queue_ahead_can_never_exceed_the_published_level(replay):
    """If the level collapses to 1, at most 1 can be in front of us."""
    book = book_with(100.00, 9.0, 100.02, 5.0)
    t = tape(
        ticker(T0, 100.00, 9.0, 100.02, 5.0),
        depth(T0 + 500, bids=((100.00, 1.0),)),
        ticker(T0 + 9_000, 100.00, 1.0, 100.02, 5.0),
    )
    out = replay.simulate_quote(
        symbol=SYM, side=Side.LONG, price=100.00, qty=1.0,
        from_ms=T0, timeout_ms=5_000, book_state=book, tape=t,
    )
    assert out.queue_ahead_usd == pytest.approx(100.0)


def test_an_event_that_does_not_mention_our_level_leaves_the_queue_alone(replay):
    """A depth update elsewhere in the book is not evidence about our queue."""
    book = book_with(100.00, 5.0, 100.02, 5.0)
    t = tape(
        ticker(T0, 100.00, 5.0, 100.02, 5.0),
        depth(T0 + 500, bids=((99.50, 3.0),), asks=((100.50, 2.0),)),
        ticker(T0 + 9_000, 100.00, 5.0, 100.02, 5.0),
    )
    out = replay.simulate_quote(
        symbol=SYM, side=Side.LONG, price=100.00, qty=1.0,
        from_ms=T0, timeout_ms=5_000, book_state=book, tape=t,
    )
    assert out.queue_ahead_usd == pytest.approx(500.0)


# --------------------------------------------------------------------------- #
# 6. markout / adverse selection
# --------------------------------------------------------------------------- #


def test_markout_is_measured_at_every_horizon_on_a_known_path(replay):
    """Fill a buy at 100.00, then walk the mid: +5 bps, +10 bps, -10 bps."""
    book = book_with(100.00, 0.0, 100.02, 5.0)
    t = tape(
        ticker(T0, 100.00, 0.0, 100.02, 5.0),
        seller_hits(T0 + 1_000, 100.00, 2.0),                 # fill at t+1s
        ticker(T0 + 2_000, 100.04, 1.0, 100.06, 1.0),         # +1s  mid 100.05
        ticker(T0 + 6_000, 100.09, 1.0, 100.11, 1.0),         # +5s  mid 100.10
        ticker(T0 + 31_000, 99.89, 1.0, 99.91, 1.0),          # +30s mid 99.90
    )
    out = replay.simulate_quote(
        symbol=SYM, side=Side.LONG, price=100.00, qty=1.0,
        from_ms=T0, timeout_ms=5_000, book_state=book, tape=t,
    )

    assert out.filled is True
    assert out.markout("1s") == pytest.approx(5.0, abs=1e-6)
    assert out.markout("5s") == pytest.approx(10.0, abs=1e-6)
    assert out.markout("30s") == pytest.approx(-10.0, abs=1e-6)
    # adverse_selection_bps is signed AGAINST us at the reference horizon.
    assert ADVERSE_SELECTION_HORIZON == "5s"
    assert out.adverse_selection_bps == pytest.approx(-out.markout("5s"))


def test_markout_sign_is_mirrored_for_a_sell(replay):
    """Selling at 100.02 and watching the mid fall is markout in our favour."""
    book = book_with(100.00, 5.0, 100.02, 0.0)
    t = tape(
        ticker(T0, 100.00, 5.0, 100.02, 0.0),
        buyer_lifts(T0 + 500, 100.02, 2.0),
        ticker(T0 + 6_000, 99.91, 1.0, 99.93, 1.0),   # mid 99.92, 10 bps down
    )
    out = replay.simulate_quote(
        symbol=SYM, side=Side.SHORT, price=100.02, qty=1.0,
        from_ms=T0, timeout_ms=5_000, book_state=book, tape=t,
    )
    assert out.filled is True
    assert out.markout("5s") == pytest.approx(9.998, abs=0.01)
    assert out.adverse_selection_bps < 0  # not adversely selected


def test_unreached_horizon_is_absent_not_zero(replay):
    """A horizon the tape never reaches must not be reported as a flat zero."""
    book = book_with(100.00, 0.0, 100.02, 5.0)
    t = tape(
        ticker(T0, 100.00, 0.0, 100.02, 5.0),
        seller_hits(T0 + 500, 100.00, 2.0),
        ticker(T0 + 2_000, 100.04, 1.0, 100.06, 1.0),
    )
    out = replay.simulate_quote(
        symbol=SYM, side=Side.LONG, price=100.00, qty=1.0,
        from_ms=T0, timeout_ms=5_000, book_state=book, tape=t,
    )
    assert out.filled is True
    assert out.markout("1s") is not None
    assert out.markout("5s") is None
    assert out.markout("30s") is None
    assert "5s" not in out.markout_bps


# --------------------------------------------------------------------------- #
# 7. post-only
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("side", "price"),
    [(Side.LONG, 100.02), (Side.LONG, 100.05), (Side.SHORT, 100.00), (Side.SHORT, 99.98)],
)
def test_post_only_quote_that_would_cross_is_rejected(replay, side, price):
    """The exchange rejects it, so we do too — never a silent taker conversion."""
    book = book_with(100.00, 5.0, 100.02, 5.0)
    t = tape(
        ticker(T0, 100.00, 5.0, 100.02, 5.0),
        seller_hits(T0 + 100, 100.00, 50.0),
        buyer_lifts(T0 + 200, 100.02, 50.0),
    )
    out = replay.simulate_quote(
        symbol=SYM, side=side, price=price, qty=1.0,
        from_ms=T0, timeout_ms=5_000, book_state=book, tape=t,
    )
    assert out.filled is False
    assert out.missed_reason == "would_cross"
    assert out.fill_qty == 0.0
    assert out.fee_usd == 0.0


def test_quote_price_never_improves_past_the_touch(replay):
    book = book_with(100.00, 5.0, 100.02, 5.0)
    assert replay.quote_price(Side.LONG, book, 0, TICK) == pytest.approx(100.00)
    assert replay.quote_price(Side.LONG, book, 2, TICK) == pytest.approx(99.98)
    assert replay.quote_price(Side.SHORT, book, 0, TICK) == pytest.approx(100.02)
    assert replay.quote_price(Side.SHORT, book, 2, TICK) == pytest.approx(100.04)


# --------------------------------------------------------------------------- #
# 8. fees — a maker fill never pays the taker rate
# --------------------------------------------------------------------------- #


def test_a_maker_fill_never_pays_the_taker_fee(replay, settings):
    book = book_with(100.00, 0.0, 100.02, 5.0)
    t = tape(
        ticker(T0, 100.00, 0.0, 100.02, 5.0),
        seller_hits(T0 + 500, 100.00, 2.0),
        ticker(T0 + 9_000, 100.00, 0.0, 100.02, 5.0),
    )
    out = replay.simulate_quote(
        symbol=SYM, side=Side.LONG, price=100.00, qty=1.0,
        from_ms=T0, timeout_ms=5_000, book_state=book, tape=t,
    )
    maker_bps = settings.fees.maker_bps(SYM)
    taker_bps = settings.fees.taker_bps(SYM)
    assert taker_bps > maker_bps, "Tier-0 BTCUSD: 0% maker, 0.01% taker"
    assert out.fee_usd == pytest.approx(100.00 * 1.0 * maker_bps / 10_000.0)
    assert out.fee_usd < 100.00 * 1.0 * taker_bps / 10_000.0


def test_compare_charges_maker_and_taker_arms_different_entry_fees(replay, settings):
    sl = _filling_tape()
    sig = _signal(side=Side.LONG, ts_ms=T0, exit_price=100.50)
    cmp = replay.compare(
        None, None, tape={SYM: sl}, signals=[sig], timeout_ms=5_000, offset_ticks=0
    )
    assert cmp.maker_fills == 1
    # The maker arm pays the maker rate on the way in; both pay taker on the way
    # out, because only the entry execution style varies here.
    assert cmp.maker.fees < cmp.taker.fees


# --------------------------------------------------------------------------- #
# 9. compare(): maker / taker / hybrid
# --------------------------------------------------------------------------- #


def _filling_tape() -> TapeSlice:
    """A tape where a bid at the touch does get filled, then the price rises."""
    return tape(
        ticker(T0 - 1, 100.00, 0.0, 100.02, 5.0),
        ticker(T0, 100.00, 0.0, 100.02, 5.0),
        seller_hits(T0 + 500, 100.00, 5.0),
        ticker(T0 + 2_000, 100.20, 1.0, 100.22, 1.0),
        ticker(T0 + 40_000, 100.48, 1.0, 100.52, 1.0),
        ticker(T0 + 120_000, 100.48, 1.0, 100.52, 1.0),
    )


def _empty_tape(*, until_ms: int = 120_000) -> TapeSlice:
    """A tape with a book but no prints at all: a maker quote can never fill."""
    return tape(
        *[
            ticker(T0 + step, 100.00, 5.0, 100.02, 5.0)
            for step in range(0, until_ms + 1, 1_000)
        ]
    )


def _signal(*, side: Side, ts_ms: int, exit_price: float, edge_bps: float = 0.0) -> QuoteSignal:
    return QuoteSignal(
        symbol=SYM,
        side=side,
        ts_ms=ts_ms,
        qty=1.0,
        exit_ms=ts_ms + 60_000,
        exit_price=exit_price,
        taker_entry_price=100.02,
        expected_net_edge_bps=edge_bps,
    )


def test_unfilled_maker_signal_costs_us_the_move_we_missed(replay):
    """The maker did not trade; the taker made money.  That is a real cost."""
    sl = _empty_tape()
    sig = _signal(side=Side.LONG, ts_ms=T0, exit_price=100.52)
    cmp = replay.compare(
        None, None, tape={SYM: sl}, signals=[sig], timeout_ms=5_000, offset_ticks=1
    )

    assert cmp.maker_fills == 0
    assert cmp.maker.trades == 0
    assert cmp.taker.trades == 1
    assert cmp.taker.net_pnl > 0
    # The opportunity cost is exactly the NET the taker banked on that signal.
    assert cmp.maker_opportunity_cost_usd == pytest.approx(cmp.taker.net_pnl)
    assert cmp.maker_net_after_opportunity_cost == pytest.approx(-cmp.taker.net_pnl)
    assert cmp.maker.net_pnl == 0.0, "the maker arm itself booked nothing"
    assert cmp.miss_reasons.get("timeout") == 1


def test_dodging_a_loser_is_credited_not_hidden(replay):
    """Opportunity cost is signed: a miss on a losing signal is a *benefit*."""
    sl = _empty_tape()
    sig = _signal(side=Side.LONG, ts_ms=T0, exit_price=99.50)
    cmp = replay.compare(
        None, None, tape={SYM: sl}, signals=[sig], timeout_ms=5_000, offset_ticks=1
    )
    assert cmp.taker.net_pnl < 0
    assert cmp.maker_opportunity_cost_usd < 0
    assert cmp.maker_net_after_opportunity_cost > 0


def test_hybrid_crosses_only_when_the_taker_recomputation_still_clears(replay, settings):
    """Two identical misses; only the one whose NET edge survives is crossed."""
    sl = _empty_tape()
    gate = settings.risk.min_net_edge_bps
    rich = _signal(side=Side.LONG, ts_ms=T0, exit_price=100.52, edge_bps=gate + 9.0)
    thin = _signal(
        side=Side.LONG, ts_ms=T0 + 30_000, exit_price=100.52, edge_bps=max(0.0, gate - 0.5)
    )

    cmp = replay.compare(
        None, None, tape={SYM: sl}, signals=[rich, thin], timeout_ms=5_000, offset_ticks=1
    )

    assert cmp.maker_fills == 0, "no prints in this tape, so nothing rests into a fill"
    assert cmp.hybrid_crosses == 1, "only the signal that still clears the gate crosses"
    assert cmp.hybrid_cross_rate == pytest.approx(0.5)
    assert cmp.hybrid.trades == 1
    assert cmp.maker.trades == 0
    assert cmp.taker.trades == 2


def test_hybrid_does_not_cross_when_the_move_was_already_consumed(replay, settings):
    """The edge ran away while we waited, so crossing now buys nothing."""
    gate = settings.risk.min_net_edge_bps
    sl = tape(
        ticker(T0, 100.00, 5.0, 100.02, 5.0),
        # +40 bps in our favour before the quote times out: the edge is gone.
        ticker(T0 + 4_000, 100.40, 5.0, 100.42, 5.0),
        ticker(T0 + 120_000, 100.40, 5.0, 100.42, 5.0),
    )
    sig = _signal(side=Side.LONG, ts_ms=T0, exit_price=100.90, edge_bps=gate + 9.0)
    cmp = replay.compare(
        None, None, tape={SYM: sl}, signals=[sig], timeout_ms=5_000, offset_ticks=1
    )
    assert cmp.maker_fills == 0
    assert cmp.hybrid_crosses == 0
    assert cmp.hybrid.trades == 0


def test_hybrid_is_the_maker_fill_when_the_maker_fills(replay):
    sl = _filling_tape()
    sig = _signal(side=Side.LONG, ts_ms=T0, exit_price=100.52)
    cmp = replay.compare(
        None, None, tape={SYM: sl}, signals=[sig], timeout_ms=5_000, offset_ticks=0
    )
    assert cmp.maker_fills == 1
    assert cmp.hybrid_maker_fills == 1
    assert cmp.hybrid_crosses == 0
    assert cmp.hybrid.net_pnl == pytest.approx(cmp.maker.net_pnl)


def test_maker_entry_beats_taker_entry_on_the_same_filled_signal(replay):
    """When it fills, resting is cheaper — that is the whole hypothesis."""
    sl = _filling_tape()
    sig = _signal(side=Side.LONG, ts_ms=T0, exit_price=100.52)
    cmp = replay.compare(
        None, None, tape={SYM: sl}, signals=[sig], timeout_ms=5_000, offset_ticks=0
    )
    assert cmp.maker.net_pnl > cmp.taker.net_pnl
    assert cmp.maker.slippage < 0, "resting behind the touch is price improvement"
    assert cmp.maker_fill_rate == pytest.approx(1.0)


def test_compare_accepts_a_duck_typed_arm_of_closed_trades(replay):
    """No import of tournament.py: anything with ``.trades`` works."""

    class _Arm:
        trades = [
            ClosedTrade(
                id="x", symbol=SYM, side=Side.LONG, strategy="s", regime="r",
                ledger="base", qty=1.0, entry_price=100.02, exit_price=100.52,
                entry_ms=T0, exit_ms=T0 + 60_000, notional_usd=100.02,
                gross_pnl_usd=0.5, entry_fee_usd=0.0, exit_fee_usd=0.0,
                slippage_usd=0.0, net_pnl_usd=0.5, exit_reason=ExitReason.TIME_STOP,
                risk_usd=1.0, expected_net_edge_bps=5.0, simulator_version="v",
            )
        ]

    cmp = replay.compare(_Arm(), None, tape={SYM: _filling_tape()}, timeout_ms=5_000,
                         offset_ticks=0)
    assert cmp.signals == 1
    assert cmp.taker.trades == 1
    assert cmp.taker.net_pnl == pytest.approx(0.5), "the arm's own NET is preserved"


def test_signals_from_trades_is_time_ordered():
    trades = [
        ClosedTrade(
            id=str(i), symbol=SYM, side=Side.LONG, strategy="s", regime="r", ledger="b",
            qty=1.0, entry_price=100.0, exit_price=100.5, entry_ms=T0 + ms,
            exit_ms=T0 + ms + 1000, notional_usd=100.0, gross_pnl_usd=0.5,
            entry_fee_usd=0.0, exit_fee_usd=0.0, slippage_usd=0.0, net_pnl_usd=0.5,
            exit_reason=ExitReason.TIME_STOP, risk_usd=1.0, expected_net_edge_bps=1.0,
            simulator_version="v",
        )
        for i, ms in enumerate((5_000, 1_000, 3_000))
    ]
    got = signals_from_trades(trades)
    assert [s.ts_ms - T0 for s in got] == [1_000, 3_000, 5_000]


# --------------------------------------------------------------------------- #
# 10. insufficient data is a result, not a zero
# --------------------------------------------------------------------------- #


def test_empty_tape_directory_yields_no_slices_and_an_explicit_shortfall(tmp_path):
    slices = load_tape(tmp_path)
    assert slices == {}
    cov = tape_coverage(slices)
    assert cov.sufficient is False
    reasons = cov.reasons()
    assert reasons and "no tape found" in reasons[0]


def test_short_tape_is_flagged_rather_than_concluded_from(replay, tmp_path):
    """Twenty minutes of tape must not present itself as an answer."""
    sl = _filling_tape()
    cov = tape_coverage({SYM: sl})
    assert cov.sufficient is False
    assert cov.hours < 1.0
    joined = " ".join(cov.reasons())
    assert "need 24h" in joined
    assert "more" in joined


def test_comparison_with_too_few_signals_is_marked_insufficient(replay):
    sl = _filling_tape()
    sig = _signal(side=Side.LONG, ts_ms=T0, exit_price=100.52)
    cmp = replay.compare(
        None, None, tape={SYM: sl}, signals=[sig], timeout_ms=5_000, offset_ticks=0
    )
    assert cmp.signals < MIN_QUOTES
    assert cmp.insufficient_data is True
    assert cmp.verdict().startswith("INSUFFICIENT DATA")


def test_comparison_with_no_signals_does_not_report_a_confident_zero(replay):
    cmp = replay.compare(None, None, tape={SYM: _filling_tape()}, signals=[])
    assert cmp.insufficient_data is True
    assert cmp.maker_fill_rate == 0.0
    assert "INSUFFICIENT DATA" in cmp.verdict()


def test_a_quote_open_when_the_tape_ends_is_unresolved_not_a_miss(replay):
    """The recorder started five minutes ago; that must not deflate fill rates."""
    book = book_with(100.00, 5.0, 100.02, 5.0)
    t = tape(
        ticker(T0, 100.00, 5.0, 100.02, 5.0),
        ticker(T0 + 1_000, 100.00, 5.0, 100.02, 5.0),   # tape ends here
    )
    out = replay.simulate_quote(
        symbol=SYM, side=Side.LONG, price=100.00, qty=1.0,
        from_ms=T0, timeout_ms=30_000, book_state=book, tape=t,
    )
    assert out.filled is False
    assert out.resolved is False
    assert out.missed_reason == "tape_exhausted"


def test_unresolved_quotes_are_excluded_from_the_fill_rate_denominator(replay):
    """One fill, one unknown — that is 100%, not 50%."""
    sl = tape(
        ticker(T0, 100.00, 0.0, 100.02, 5.0),
        seller_hits(T0 + 500, 100.00, 5.0),
        ticker(T0 + 60_000, 100.00, 0.0, 100.02, 5.0),
    )
    filled = _signal(side=Side.LONG, ts_ms=T0, exit_price=100.02)
    dangling = _signal(side=Side.LONG, ts_ms=T0 + 59_000, exit_price=100.02)
    cmp = replay.compare(
        None, None, tape={SYM: sl}, signals=[filled, dangling],
        timeout_ms=30_000, offset_ticks=0,
    )
    assert cmp.maker_unresolved == 1
    assert cmp.maker_fills == 1
    assert cmp.maker_fill_rate == pytest.approx(1.0)
    assert any("excluded from the fill-rate denominator" in n for n in cmp.notes)


def test_verdict_names_adverse_selection_rather_than_leaving_it_inferred(replay):
    """A high fill rate with negative markout must READ as the worse outcome."""
    from app.research.maker_replay import ExecutionComparison
    from app.types import PerfStats

    bad = ExecutionComparison(
        taker=PerfStats(), maker=PerfStats(), hybrid=PerfStats(),
        maker_fill_rate=0.92, hybrid_cross_rate=0.0,
        markout_bps={"5s": -3.2}, signals=100, maker_fills=92,
    )
    text = bad.verdict()
    assert "ADVERSE SELECTION" in text
    assert "LOWER fill rate" in text

    flat = ExecutionComparison(
        taker=PerfStats(), maker=PerfStats(), hybrid=PerfStats(),
        maker_fill_rate=0.11, hybrid_cross_rate=0.0,
        markout_bps={"5s": 0.0}, signals=100, maker_fills=11,
    )
    assert "FLAT" in flat.verdict()


# --------------------------------------------------------------------------- #
# 11. the tape format itself
# --------------------------------------------------------------------------- #


def _write_tape(directory: Path, name: str, rows: list[dict]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    with gzip.open(path, "wt") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    return path


def _depth_msg(ts: int, symbol: str = SYM) -> dict:
    return {
        "r": ts,
        "m": {
            "stream": f"{symbol.lower()}@depth@100ms",
            "data": {
                "e": "depthUpdate", "E": ts - 10, "s": symbol,
                "U": 1, "u": 2,
                "b": [["100.00000000", "5.00000000"]],
                "a": [["100.02000000", "5.00000000"]],
            },
        },
    }


def test_reader_matches_recorder_replay(tmp_path):
    """Our reader and ``app.recorder.replay`` must agree on a complete file.

    A reader that drifts from its writer produces a dataset nobody can use, so
    the agreement is pinned rather than assumed.
    """
    from app.recorder import replay as recorder_replay

    rows = [_depth_msg(T0 + i * 100) for i in range(20)]
    _write_tape(tmp_path, "tape-20260911-05.ndjson.gz", rows)

    theirs = list(recorder_replay(tmp_path))
    ours = [(r, m) for r, m, _ in iter_tape_messages(tmp_path)]
    assert ours == theirs
    assert len(ours) == 20


def test_reader_tolerates_the_hour_still_being_written(tmp_path):
    """The current file is an unterminated gzip member.  Read what is there.

    This is not hypothetical: it is exactly the state of ``/data/tape`` at any
    moment, and it is the file with the freshest data in it.
    """
    rows = [_depth_msg(T0 + i * 100) for i in range(50)]
    path = _write_tape(tmp_path, "tape-20260911-06.ndjson.gz", rows)
    raw = path.read_bytes()
    path.write_bytes(raw[: int(len(raw) * 0.6)])   # chop the tail

    truncated: list[str] = []
    got = list(iter_tape_messages(tmp_path, on_truncated=truncated.append))
    assert 0 < len(got) < 50
    assert truncated == [path.name]

    slices = load_tape(tmp_path)
    assert SYM in slices
    assert slices[SYM].truncated_files == (path.name,)


def test_parse_message_handles_every_stream_the_recorder_captures():
    prec: dict[str, list[int]] = {}
    depth_ev = parse_message(_depth_msg(T0)["m"], T0, prec)
    assert depth_ev is not None and depth_ev.kind == "depth"
    assert depth_ev.bids == ((100.0, 5.0),)

    # bookTicker frames carry no "e": the kind comes from the stream name.
    tick_ev = parse_message(
        {
            "stream": "btcusd@bookTicker",
            "data": {"s": SYM, "b": "100.00", "B": "3", "a": "100.02", "A": "4"},
        },
        T0,
        prec,
    )
    assert tick_ev is not None and tick_ev.kind == "ticker"
    assert tick_ev.bids == ((100.0, 3.0),)

    trade_ev = parse_message(
        {
            "stream": "btcusd@aggTrade",
            "data": {"e": "aggTrade", "s": SYM, "p": "100.01", "q": "0.5",
                     "T": T0 - 5, "m": True},
        },
        T0,
        prec,
    )
    assert trade_ev is not None and trade_ev.kind == "trade"
    assert trade_ev.aggressor is Side.SHORT   # buyer_is_maker => seller aggressed

    assert parse_message({"stream": "btcusd@kline_1m", "data": {"e": "kline"}}, T0, prec) is None
    assert parse_message(None, T0, prec) is None
    assert parse_message({}, T0, prec) is None


def test_tick_size_is_inferred_from_the_prices_on_the_tape(tmp_path):
    rows = [_depth_msg(T0 + i * 100) for i in range(5)]
    _write_tape(tmp_path, "tape-20260911-07.ndjson.gz", rows)
    slices = load_tape(tmp_path)
    assert slices[SYM].tick_size == pytest.approx(0.01)


def test_coverage_counts_prints_per_symbol(tmp_path):
    rows = [_depth_msg(T0 + i * 1000) for i in range(3)]
    rows.append(
        {
            "r": T0 + 4000,
            "m": {"stream": "btcusd@aggTrade",
                  "data": {"e": "aggTrade", "s": SYM, "p": "100.01", "q": "1",
                           "T": T0 + 3990, "m": True}},
        }
    )
    _write_tape(tmp_path, "tape-20260911-08.ndjson.gz", rows)
    cov = tape_coverage(load_tape(tmp_path))
    assert cov.symbols[SYM]["trades"] == 1
    assert cov.symbols[SYM]["depth"] == 3
    assert cov.sufficient is False


# --------------------------------------------------------------------------- #
# 12. perf_stats
# --------------------------------------------------------------------------- #


def test_perf_stats_is_empty_for_no_trades():
    s = perf_stats([])
    assert (s.trades, s.net_pnl, s.profit_factor, s.max_drawdown) == (0, 0.0, 0.0, 0.0)


def test_perf_stats_drawdown_follows_the_net_equity_path():
    def t(i: int, net: float) -> ClosedTrade:
        return ClosedTrade(
            id=str(i), symbol=SYM, side=Side.LONG, strategy="s", regime="r", ledger="b",
            qty=1.0, entry_price=100.0, exit_price=100.0, entry_ms=T0 + i,
            exit_ms=T0 + i + 1, notional_usd=100.0, gross_pnl_usd=net,
            entry_fee_usd=0.0, exit_fee_usd=0.0, slippage_usd=0.0, net_pnl_usd=net,
            exit_reason=ExitReason.TIME_STOP, risk_usd=1.0, expected_net_edge_bps=0.0,
            simulator_version="v",
        )

    s = perf_stats([t(0, 5.0), t(1, -3.0), t(2, -1.0), t(3, 2.0)])
    assert s.trades == 4
    assert s.net_pnl == pytest.approx(3.0)
    assert s.max_drawdown == pytest.approx(4.0)
    assert s.profit_factor == pytest.approx(7.0 / 4.0)


# --------------------------------------------------------------------------- #
# 13. the CLI
# --------------------------------------------------------------------------- #


def _load_cli():
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_execution_study.py"
    spec = importlib.util.spec_from_file_location("run_execution_study_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _cli_args(cli, tmp_path, **over):
    args = cli.build_parser().parse_args(
        ["--tape-dir", str(tmp_path), "--symbols", SYM,
         "--timeout-seconds", "5,15", "--offset-ticks", "0,1"]
    )
    for k, v in over.items():
        setattr(args, k, v)
    return args


def test_cli_on_an_empty_directory_says_insufficient_data(tmp_path):
    cli = _load_cli()
    payload = cli.run(_cli_args(cli, tmp_path))
    assert payload["sufficient"] is False
    assert payload["conclusion"].startswith("INSUFFICIENT DATA")
    assert payload["grid"] == []
    assert payload["comparison"] == {}


def test_cli_on_a_short_tape_reports_the_shortfall_and_no_conclusion(tmp_path):
    """Five minutes of tape: machinery runs, verdict refuses."""
    cli = _load_cli()
    rows: list[dict] = []
    for i in range(300):  # 5 minutes at 1 Hz
        ts = T0 + i * 1_000
        rows.append(_depth_msg(ts))
        if i % 10 == 0:
            rows.append(
                {
                    "r": ts + 1,
                    "m": {"stream": "btcusd@aggTrade",
                          "data": {"e": "aggTrade", "s": SYM, "p": "100.00",
                                   "q": "1", "T": ts, "m": True}},
                }
            )
    _write_tape(tmp_path, "tape-20260911-09.ndjson.gz", rows)

    payload = cli.run(
        _cli_args(cli, tmp_path, cadence_seconds=30, hold_seconds=60, warmup_seconds=5)
    )

    assert payload["sufficient"] is False
    assert payload["coverage"]["hours"] < 24.0
    assert payload["conclusion"].startswith("INSUFFICIENT DATA")
    joined = " ".join(payload["insufficient_reasons"])
    assert "need 24h" in joined
    assert "more" in joined
    # The machinery still ran, so the report shows what it *will* measure.
    assert payload["grid"], "the quote grid should still be exercised"
    assert all("fill_rate" in row for row in payload["grid"])

    lines: list[str] = []
    cli.render(payload, log=lines.append)
    text = "\n".join(lines)
    assert "INSUFFICIENT DATA" in text
    assert "no conclusion is drawn" in text
    assert "touched-but-not-filled" in text


def test_cli_json_output_is_serialisable(tmp_path):
    cli = _load_cli()
    rows = [_depth_msg(T0 + i * 1_000) for i in range(60)]
    _write_tape(tmp_path, "tape-20260911-10.ndjson.gz", rows)
    payload = cli.run(_cli_args(cli, tmp_path, warmup_seconds=1, cadence_seconds=5,
                                hold_seconds=10))
    encoded = json.dumps(payload, default=str)
    assert "INSUFFICIENT DATA" in encoded


def test_cli_main_returns_zero_on_insufficient_data(tmp_path, capsys):
    """Not enough tape is a finding, not a crash."""
    cli = _load_cli()
    code = cli.main(["--tape-dir", str(tmp_path), "--symbols", SYM, "--out", "text"])
    assert code == 0
    assert "INSUFFICIENT DATA" in capsys.readouterr().out


def test_cli_parses_the_documented_flags():
    cli = _load_cli()
    args = cli.build_parser().parse_args(
        ["--tape-dir", "/data/tape", "--symbols", "BTCUSD,ETHUSD,SOLUSD",
         "--timeout-seconds", "5,15,30,60", "--offset-ticks", "0,1,2", "--out", "json"]
    )
    assert args.tape_dir == "/data/tape"
    assert cli._ints(args.timeout_seconds) == [5, 15, 30, 60]
    assert cli._ints(args.offset_ticks) == [0, 1, 2]
    assert args.out == "json"


# --------------------------------------------------------------------------- #
# 14. determinism
# --------------------------------------------------------------------------- #


def test_simulate_quote_is_deterministic(replay):
    book = book_with(100.00, 3.0, 100.02, 5.0)
    t = _filling_tape()
    runs = [
        replay.simulate_quote(
            symbol=SYM, side=Side.LONG, price=100.00, qty=1.0,
            from_ms=T0, timeout_ms=5_000, book_state=book, tape=t,
        )
        for _ in range(5)
    ]
    assert all(r == runs[0] for r in runs)
