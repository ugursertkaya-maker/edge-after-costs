"""The opportunity board, episode identity, and the close → entry loop.

Three properties are load-bearing and the rest of the file supports them:

1. **A queued idea is never deployed on the numbers it was queued with.**  Every
   entry is rebuilt from the live book, the live features and the live fee
   table before anything sizes it (§13).
2. **The same episode can never be entered twice**, and a genuinely new episode
   on the same coin — BTC included — can (§14).
3. **Closing one coin does not block another.**  There is no global cooldown to
   find, and the per-symbol one defaults to off (§15).

Everything runs on the shared $500 fixtures from ``test_portfolio``: equity
$500, a 20% reserve, a book quoted 99.99/100.01.  No clock, no network, no
randomness — the clock is injected so "the rescan happened in the same call" is
asserted against a counter rather than hoped for.
"""

from __future__ import annotations

import dataclasses
import hashlib

import pytest

from app.config import RecycleConfig, Settings
from app.portfolio import IdleReason, PortfolioEngine
from app.queue import (
    CAPITAL_RELEASED,
    CapitalRecycler,
    EpisodeLedger,
    EpisodeReentry,
    EpisodeTracker,
    OpportunityQueue,
    QueuedOpportunity,
    episode_id,
    revalidate,
    revalidate_detailed,
    signal_bucket,
)
from app.types import RejectReason, Side

from tests.test_portfolio import (
    TS,
    FakeLedger,
    make_book,
    make_features,
    make_opp,
    make_position,
    symbols,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


class Clock:
    """A clock that only moves when something asks it to.

    Deliberately not a monotonic fake: every read advances by one millisecond,
    so "the rescan ran inside the close" is the difference between a handful of
    milliseconds and a whole evaluation interval, and the test can tell them
    apart without sleeping.
    """

    def __init__(self, now: int = TS) -> None:
        self.now = int(now)

    def __call__(self) -> int:
        self.now += 1
        return self.now


def settings_with(**recycle) -> Settings:
    s = Settings()
    if recycle:
        s = dataclasses.replace(s, recycle=RecycleConfig(**recycle))
    return s


def queue_with(settings: Settings | None = None, **recycle) -> OpportunityQueue:
    return OpportunityQueue(settings=settings or settings_with(**recycle), clock=Clock())


def episode_for(opp, *, counter: int = 1) -> str:
    return episode_id(
        opp.symbol,
        opp.signal.strategy,
        opp.regime.regime.value,
        signal_bucket(opp.signal.target_bps, 5.0),
        counter,
    )


def queued(
    q: OpportunityQueue, opp=None, *, ts_ms: int = TS, counter: int = 1
) -> QueuedOpportunity:
    opp = opp if opp is not None else make_opp("BTCUSD")
    entry = q.offer(opp, episode_id=episode_for(opp, counter=counter), ts_ms=ts_ms)
    assert entry is not None
    return entry


def books_and_features(*syms: str, **over):
    books = {s: make_book(s, **over.get("book", {})) for s in syms}
    feats = {s: make_features(s, **over.get("features", {})) for s in syms}
    return books, feats


# --------------------------------------------------------------------------- #
# §14 — episode identity
# --------------------------------------------------------------------------- #


def test_episode_id_is_sha1_over_exactly_the_five_named_parts() -> None:
    """The contract is a formula, so it is checked against the formula."""
    expected = hashlib.sha1(b"BTCUSD|flow_impulse|TRENDING|4|7").hexdigest()
    assert episode_id("BTCUSD", "flow_impulse", "TRENDING", 4, 7) == expected
    # Every part participates: change any one and the identity changes.
    base = episode_id("BTCUSD", "flow_impulse", "TRENDING", 4, 7)
    assert episode_id("ETHUSD", "flow_impulse", "TRENDING", 4, 7) != base
    assert episode_id("BTCUSD", "book_imbalance", "TRENDING", 4, 7) != base
    assert episode_id("BTCUSD", "flow_impulse", "RANGING", 4, 7) != base
    assert episode_id("BTCUSD", "flow_impulse", "TRENDING", 5, 7) != base
    assert episode_id("BTCUSD", "flow_impulse", "TRENDING", 4, 8) != base


def test_the_bucket_quantises_so_one_impulse_is_one_identity() -> None:
    """Drift inside a bucket is the same impulse; the id must not move."""
    assert signal_bucket(21.0, 5.0) == signal_bucket(24.9, 5.0) == 4
    assert signal_bucket(25.0, 5.0) == 5
    # Floor, not round: a feature oscillating about a bucket centre stays put.
    assert signal_bucket(22.4, 5.0) == signal_bucket(22.6, 5.0)


def test_the_counter_advances_only_on_a_false_then_true_transition() -> None:
    t = EpisodeTracker(settings=settings_with())
    first = t.observe(
        symbol="BTCUSD", strategy="flow_impulse", regime="TRENDING",
        triggered=True, value=21.0, ts_ms=TS,
    )
    assert first is not None and first.counter == 1

    # The trigger stays true and the feature drifts — even across a bucket
    # boundary, and even into a different regime. Same impulse, same episode.
    for value in (22.0, 24.9, 41.0):
        again = t.observe(
            symbol="BTCUSD", strategy="flow_impulse", regime="RANGING",
            triggered=True, value=value, ts_ms=TS + 1,
        )
        assert again == first, "a live episode must not be re-minted mid-impulse"
    assert t.counter("BTCUSD", "flow_impulse") == 1

    # False does not mint anything either.
    assert t.observe(
        symbol="BTCUSD", strategy="flow_impulse", regime="TRENDING",
        triggered=False, value=0.0, ts_ms=TS + 2,
    ) is None
    assert t.current("BTCUSD", "flow_impulse") is None

    # ...and only now, on the way back up, is it a new episode.
    second = t.observe(
        symbol="BTCUSD", strategy="flow_impulse", regime="TRENDING",
        triggered=True, value=21.0, ts_ms=TS + 3,
    )
    assert second is not None
    assert second.counter == 2
    assert second.episode_id != first.episode_id


def test_the_counter_is_monotone_and_never_reuses_an_identity() -> None:
    """Twenty impulses on the same coin, in the same regime, at the same value."""
    t = EpisodeTracker(settings=settings_with())
    seen = []
    for i in range(20):
        t.stand_down("BTCUSD", "momentum_scalp")
        ep = t.observe(
            symbol="BTCUSD", strategy="momentum_scalp", regime="TRENDING",
            triggered=True, value=21.0, ts_ms=TS + i,
        )
        seen.append(ep.episode_id)
    assert len(set(seen)) == 20
    assert t.counter("BTCUSD", "momentum_scalp") == 20


def test_two_strategies_on_one_coin_are_two_episodes() -> None:
    """One may be right while the other is wrong; merging them lets the loser
    block the winner."""
    t = EpisodeTracker(settings=settings_with())
    a = t.observe_signal(make_opp("BTCUSD").signal, "TRENDING", ts_ms=TS)
    other = make_opp("BTCUSD")
    other.signal.strategy = "book_imbalance"
    b = t.observe_signal(other.signal, "TRENDING", ts_ms=TS)
    assert a.episode_id != b.episode_id
    assert t.counter("BTCUSD", "momentum_scalp") == 1
    assert t.counter("BTCUSD", "book_imbalance") == 1


def test_re_entering_the_same_episode_is_impossible() -> None:
    """The hard invariant: a claimed episode is refused everywhere, and the
    ledger raises rather than quietly allowing a second position."""
    led = EpisodeLedger()
    ep = episode_id("BTCUSD", "momentum_scalp", "TRENDING", 9, 1)

    assert not led.entered(ep)
    led.claim(ep, symbol="BTCUSD", ts_ms=TS)
    assert led.entered(ep)

    with pytest.raises(EpisodeReentry) as caught:
        led.claim(ep, symbol="BTCUSD", ts_ms=TS + 60_000)
    assert ep in str(caught.value)

    # ...and the softer guards agree, so the raise is a backstop and not the
    # normal path: the queue will not even retain it.
    q = OpportunityQueue(settings=settings_with(), episodes=led, clock=Clock())
    assert q.offer(make_opp("BTCUSD"), episode_id=ep, ts_ms=TS) is None
    assert len(q) == 0


def test_a_queued_episode_that_gets_entered_is_dropped_from_the_board() -> None:
    led = EpisodeLedger()
    q = OpportunityQueue(settings=settings_with(), episodes=led, clock=Clock())
    entry = queued(q)
    assert len(q) == 1

    led.claim(entry.episode_id, symbol="BTCUSD", ts_ms=TS)
    assert q.prune(TS + 1) == 1
    assert len(q) == 0
    # And revalidation refuses it even if something still holds a reference.
    fresh, reason = revalidate_detailed(
        entry, book=make_book("BTCUSD"), features=make_features("BTCUSD"),
        fees=None, settings=settings_with(), episodes=led, ts_ms=TS + 1,
    )
    assert fresh is None and reason is RejectReason.DUPLICATE_SYMBOL


def test_the_same_coin_may_be_re_entered_on_a_genuinely_new_episode() -> None:
    """Including BTC. Suppressing the winner is not risk management (§Hard rules)."""
    led = EpisodeLedger()
    t = EpisodeTracker(settings=settings_with())
    q = OpportunityQueue(settings=settings_with(), episodes=led, clock=Clock())
    opp = make_opp("BTCUSD")

    first = t.observe_signal(opp.signal, "TRENDING", ts_ms=TS)
    led.claim(first.episode_id, symbol="BTCUSD", ts_ms=TS)
    assert q.offer(opp, episode_id=first.episode_id, ts_ms=TS) is None

    # The impulse dies and a new one starts an hour later.
    t.stand_down("BTCUSD", "momentum_scalp")
    second = t.observe_signal(opp.signal, "TRENDING", ts_ms=TS + 3_600_000)

    assert second.episode_id != first.episode_id
    assert not led.entered(second.episode_id)
    entry = q.offer(opp, episode_id=second.episode_id, ts_ms=TS + 3_600_000)
    assert entry is not None and entry.symbol == "BTCUSD"


# --------------------------------------------------------------------------- #
# §13 — the board
# --------------------------------------------------------------------------- #


def test_a_qualifying_candidate_that_loses_the_capital_is_retained() -> None:
    """The whole premise: second-best is not bad, it is second."""
    q = queue_with()
    ranked = [make_opp(s, net_edge_bps=40.0 - i) for i, s in enumerate(symbols(4))]
    taken = q.offer_ranked(
        ranked,
        episodes={o.symbol: episode_for(o) for o in ranked},
        ts_ms=TS,
        skip=[ranked[0].symbol],          # the winner got the money
    )
    assert taken == 3
    assert [e.symbol for e in q.entries()] == [o.symbol for o in ranked[1:]]
    assert q.best(TS).symbol == ranked[1].symbol


def test_re_offering_the_same_episode_refreshes_it_rather_than_stacking_it() -> None:
    """A board carrying one idea five times ranks by age, not by merit."""
    q = queue_with()
    opp = make_opp("BTCUSD", net_edge_bps=40.0)
    queued(q, opp, ts_ms=TS)
    better = make_opp("BTCUSD", net_edge_bps=55.0)
    entry = queued(q, better, ts_ms=TS + 5_000)

    assert len(q) == 1
    assert entry.net_ev_bps == pytest.approx(55.0)
    assert entry.ranked_at_ms == TS + 5_000
    assert entry.refreshed == 1


def test_entries_expire_and_the_board_is_bounded() -> None:
    q = queue_with(queue_ttl_seconds=30.0, queue_max_size=3)
    opps = [make_opp(s, net_edge_bps=10.0 + i) for i, s in enumerate(symbols(5))]
    for o in opps:
        queued(q, o, ts_ms=TS)

    # Bounded: the three best survive, the two worst are evicted.
    assert len(q) == 3
    assert {e.symbol for e in q.entries()} == {o.symbol for o in opps[2:]}

    assert q.prune(TS + 29_999) == 0
    assert q.prune(TS + 30_000) == 3
    assert len(q) == 0 and q.best(TS + 30_000) is None


def test_the_queue_can_be_switched_off_entirely() -> None:
    q = queue_with(queue_enabled=False)
    assert q.offer(make_opp("BTCUSD"), episode_id="e1", ts_ms=TS) is None
    assert len(q) == 0


# --------------------------------------------------------------------------- #
# §13 — revalidation: never the queued numbers
# --------------------------------------------------------------------------- #


def test_revalidation_returns_todays_numbers_not_the_queued_ones() -> None:
    """The queued EV is a ranking key and a decay measure. It is never traded."""
    s = settings_with()
    q = queue_with(s)
    entry = queued(q, make_opp("BTCUSD", net_edge_bps=40.0), ts_ms=TS)

    fresh = revalidate(
        entry,
        book=make_book("BTCUSD"),
        features=make_features("BTCUSD"),
        fees=s.fees,
        settings=s,
        ts_ms=TS + 100,
    )
    assert fresh is not None
    assert fresh is not entry.opportunity
    assert fresh.net_edge_bps != pytest.approx(entry.net_ev_bps)
    # The fee line was re-read from the table rather than carried: BTCUSD is a
    # Tier-0 pair, so the round trip is ~1 bps and not the queued 20.
    assert entry.opportunity.fee_bps == pytest.approx(20.0)
    assert fresh.fee_bps == pytest.approx(
        s.fees.maker_bps("BTCUSD") + s.fees.taker_bps("BTCUSD")
    )
    assert any("revalidated" in n for n in fresh.notes)


@pytest.mark.parametrize(
    "what,book_kw,expected",
    [
        ("the spread widened", dict(bid=99.5, ask=100.5), RejectReason.WIDE_SPREAD),
        ("the book emptied", dict(qty=0.05), RejectReason.THIN_DEPTH),
        ("the book went stale", dict(ts_ms=TS - 60_000), RejectReason.STALE_BOOK),
    ],
)
def test_a_market_that_moved_under_the_queue_refuses_the_entry(
    what: str, book_kw: dict, expected: RejectReason
) -> None:
    s = settings_with()
    entry = queued(queue_with(s))
    fresh, reason = revalidate_detailed(
        entry,
        book=make_book("BTCUSD", **book_kw),
        features=make_features("BTCUSD"),
        fees=s.fees,
        settings=s,
        ts_ms=TS + 100,
    )
    assert fresh is None, what
    assert reason is expected


def test_an_edge_that_decayed_below_the_gate_is_refused() -> None:
    """Slippage rose while the idea sat on the board; the idea is now a loss."""
    s = settings_with()
    entry = queued(queue_with(s))
    fresh, reason = revalidate_detailed(
        entry,
        book=make_book("BTCUSD"),
        features=make_features("BTCUSD"),
        fees=s.fees,
        settings=s,
        slippage_bps=400.0,
        ts_ms=TS + 100,
    )
    assert fresh is None
    assert reason is RejectReason.NEGATIVE_EDGE


def test_the_decay_guard_is_configurable_and_off_by_default() -> None:
    s = settings_with(max_ev_decay_pct=20.0)
    entry = queued(queue_with(s))
    # Still comfortably positive, but a long way below what it was queued at.
    kw = dict(
        book=make_book("BTCUSD"), features=make_features("BTCUSD"),
        fees=s.fees, slippage_bps=20.0, ts_ms=TS + 100,
    )
    assert revalidate(entry, settings=s, **kw) is None
    assert Settings().recycle.max_ev_decay_pct == 100.0
    assert revalidate(entry, settings=settings_with(), **kw) is not None


def test_a_queue_entry_with_nothing_to_re_cost_is_never_deployed() -> None:
    """A row carrying only its old EV is exactly the thing §13 forbids."""
    s = settings_with()
    bare = QueuedOpportunity(
        symbol="BTCUSD", strategy="momentum_scalp", episode_id="e1",
        net_ev_bps=40.0, ranked_at_ms=TS, expires_ms=TS + 120_000,
    )
    fresh, reason = revalidate_detailed(
        bare, book=make_book("BTCUSD"), features=make_features("BTCUSD"),
        fees=s.fees, settings=s, ts_ms=TS + 100,
    )
    assert fresh is None and reason is RejectReason.DATA_FAILURE


def test_a_short_is_recorded_and_never_executed() -> None:
    s = settings_with()
    q = queue_with(s)
    entry = queued(q, make_opp("BTCUSD", side=Side.SHORT))
    fresh, reason = revalidate_detailed(
        entry, book=make_book("BTCUSD"), features=make_features("BTCUSD"),
        fees=s.fees, settings=s, ts_ms=TS + 100,
    )
    assert fresh is None and reason is RejectReason.NOT_SUPPORTED_SHORT


def test_take_returns_the_best_entry_that_still_qualifies_today() -> None:
    s = settings_with()
    q = queue_with(s)
    best = make_opp("AAAUSD", net_edge_bps=90.0)
    runner_up = make_opp("BBBUSD", net_edge_bps=50.0)
    queued(q, best)
    queued(q, runner_up)

    books, feats = books_and_features("AAAUSD", "BBBUSD")
    books["AAAUSD"] = make_book("AAAUSD", bid=99.5, ask=100.5)   # spread blew out

    fresh = q.take(books=books, features=feats, fees=s.fees, ts_ms=TS + 100)
    assert fresh is not None and fresh.symbol == "BBBUSD"
    # The winner only lost this round: a spread is not a verdict, so it stays.
    assert {e.symbol for e in q.entries()} == {"AAAUSD", "BBBUSD"}


def test_an_entry_that_is_finished_is_dropped_rather_than_re_examined() -> None:
    s = settings_with()
    q = queue_with(s)
    queued(q, make_opp("AAAUSD", side=Side.SHORT))
    queued(q, make_opp("BBBUSD"))
    books, feats = books_and_features("AAAUSD", "BBBUSD")

    assert q.take(books=books, features=feats, fees=s.fees, ts_ms=TS + 100) is not None
    assert {e.symbol for e in q.entries()} == {"BBBUSD"}
    assert q.dropped[RejectReason.NOT_SUPPORTED_SHORT.value] == 1


# --------------------------------------------------------------------------- #
# §11/§12 — release and rescan, in the same call
# --------------------------------------------------------------------------- #


def recycler(
    *, rescan=None, settings: Settings | None = None, clock: Clock | None = None
) -> tuple[CapitalRecycler, Clock]:
    s = settings or settings_with()
    c = clock or Clock()
    eng = PortfolioEngine(settings=s)
    return (
        CapitalRecycler(portfolio=eng, rescan=rescan, clock=c, settings=s),
        c,
    )


def closed_book(*, cash: float = 500.0, positions=()) -> FakeLedger:
    return FakeLedger(cash=cash, positions=positions)


def test_a_close_triggers_the_rescan_inside_the_same_call() -> None:
    """Not on the next tick. The interval is loop shape, and loop shape is ours."""
    seen: list[int] = []
    rec, clock = recycler(rescan=lambda ts: seen.append(ts) or [make_opp("ETHUSD")])
    pos = make_position("p1", "SOLUSD", notional=50.0, risk=0.5)

    event = rec.on_close(closed_book(cash=550.0), pos, ts_ms=clock())

    assert seen, "the rescan must have run before on_close returned"
    assert event.rescan_handled
    assert event.rescan_triggered_ms >= event.closed_ms
    # A handful of clock reads, not an evaluation interval.
    assert event.close_to_rescan_ms < 100
    assert event.qualified_candidate_existed is True


def test_the_capital_is_released_and_the_book_recomputed_synchronously() -> None:
    rec, clock = recycler()
    pos = make_position("p1", "SOLUSD", notional=50.0, risk=0.50)
    ledger = closed_book(cash=550.0)   # the ledger already credited the exit

    event = rec.on_close(ledger, pos, ts_ms=clock())

    assert event.released_usd == pytest.approx(50.0)
    assert event.risk_released_usd == pytest.approx(0.50)
    assert event.available_usd_after == pytest.approx(550.0 - 0.20 * 550.0)
    assert event.deployed_pct_after == pytest.approx(0.0)
    assert event.release_ms < 100
    assert event.state_bug_usd == 0.0


def test_capital_released_is_emitted_with_the_numbers_on_it() -> None:
    emitted: list[tuple[str, dict]] = []
    s = settings_with()
    rec = CapitalRecycler(
        portfolio=PortfolioEngine(settings=s), emit=lambda k, d: emitted.append((k, d)),
        clock=Clock(), settings=s,
    )
    pos = make_position("p1", "SOLUSD", notional=50.0, risk=0.5)
    rec.on_close(closed_book(cash=550.0), pos, ts_ms=TS)

    assert [k for k, _ in emitted] == [CAPITAL_RELEASED]
    payload = emitted[0][1]
    assert payload["symbol"] == "SOLUSD"
    assert payload["released_usd"] == pytest.approx(50.0)
    assert payload["closed_ms"] == TS


def test_the_entry_that_reuses_the_capital_closes_the_measurement() -> None:
    rec, clock = recycler(rescan=lambda ts: [make_opp("ETHUSD")])
    pos = make_position("p1", "SOLUSD", notional=50.0, risk=0.5)
    event = rec.on_close(closed_book(cash=550.0), pos, ts_ms=clock())

    assert event.close_to_entry_ms is None, "no entry yet is not a zero"
    assert not event.redeployed

    clock.now += 900
    same = rec.note_entry("ETHUSD", ts_ms=clock())
    assert same is event
    assert event.redeployed and event.next_entry_symbol == "ETHUSD"
    assert 900 <= event.close_to_entry_ms <= 1_000

    # A second entry does not re-attach to a close that is already accounted for.
    assert rec.note_entry("ADAUSD", ts_ms=clock()) is None


def test_a_close_that_did_not_return_its_capital_is_a_state_bug() -> None:
    """The position is still on the book after we closed it. That is ours."""
    rec, clock = recycler()
    pos = make_position("p1", "SOLUSD", notional=50.0, risk=0.5)
    stuck = closed_book(cash=500.0, positions=[pos])   # never removed

    event = rec.on_close(stuck, pos, ts_ms=clock())

    assert event.state_bug_usd == pytest.approx(50.0)
    assert any("reconcile" in n for n in event.notes)
    ctx = rec.idle_context(ts_ms=clock())
    assert ctx.state_bug_usd == pytest.approx(50.0)


def test_without_a_rescan_handler_the_wait_is_recorded_rather_than_hidden() -> None:
    rec, clock = recycler(rescan=None)
    pos = make_position("p1", "SOLUSD", notional=50.0, risk=0.5)
    event = rec.on_close(closed_book(cash=550.0), pos, ts_ms=clock())

    assert not event.rescan_handled
    assert any("no rescan handler" in n for n in event.notes)
    # And the attribution is told, so the idle dollars land in SCANNER_DELAY.
    assert rec.idle_context(ts_ms=clock()).scanner_is_behind


def test_the_latency_report_separates_our_delay_from_the_markets() -> None:
    rec, clock = recycler(rescan=lambda ts: [])
    for i in range(3):
        pos = make_position(f"p{i}", "SOLUSD", notional=50.0, risk=0.5)
        rec.on_close(closed_book(cash=550.0), pos, ts_ms=clock())
        if i < 2:
            clock.now += 500
            rec.note_entry("ETHUSD", ts_ms=clock())

    report = rec.latency()
    assert report["closes"] == 3
    assert report["redeployed"] == 2
    assert report["median_close_to_rescan_ms"] < 100
    assert 500 <= report["median_close_to_entry_ms"] <= 600


def test_an_episode_already_traded_can_never_be_claimed_by_an_entry() -> None:
    """note_entry is the last line of defence and it raises rather than logs."""
    rec, clock = recycler()
    ep = episode_id("BTCUSD", "momentum_scalp", "TRENDING", 9, 1)
    rec.note_entry("BTCUSD", ts_ms=clock(), episode_id=ep)
    with pytest.raises(EpisodeReentry):
        rec.note_entry("BTCUSD", ts_ms=clock(), episode_id=ep)


def test_next_candidate_revalidates_before_it_hands_anything_over() -> None:
    s = settings_with()
    rec, clock = recycler(settings=s)
    opp = make_opp("ETHUSD", net_edge_bps=40.0)
    rec.queue.offer(opp, episode_id=episode_for(opp), ts_ms=TS)

    books, feats = books_and_features("ETHUSD")
    fresh = rec.next_candidate(
        books=books, features=feats, fees=s.fees, ts_ms=TS + 100
    )
    assert fresh is not None
    assert fresh.net_edge_bps != pytest.approx(40.0)


# --------------------------------------------------------------------------- #
# §15 — no global cooldown
# --------------------------------------------------------------------------- #


def test_closing_sol_does_not_block_opening_eth() -> None:
    """The §15 headline, asserted with the anti-churn window switched ON.

    Even then it is one symbol's window: there is no code path by which SOL's
    close can reach ETH.
    """
    s = settings_with(symbol_cooldown_seconds=300.0)
    rec, clock = recycler(settings=s)
    rec.on_close(closed_book(cash=550.0), make_position("p1", "SOLUSD"), ts_ms=TS)

    assert rec.cooling("SOLUSD", TS + 1_000) is True
    assert rec.cooling("ETHUSD", TS + 1_000) is False
    assert rec.cooling_symbols(TS + 1_000) == {"SOLUSD"}

    eth = make_opp("ETHUSD")
    rec.queue.offer(eth, episode_id=episode_for(eth), ts_ms=TS)
    books, feats = books_and_features("ETHUSD")
    assert rec.next_candidate(
        books=books, features=feats, fees=s.fees, ts_ms=TS + 1_000
    ) is not None


def test_a_profitable_close_installs_no_cooldown_at_all() -> None:
    """The default build has nothing to trip. Not a short window — none."""
    assert Settings().recycle.symbol_cooldown_seconds == 0.0
    rec, clock = recycler()
    rec.on_close(closed_book(cash=600.0), make_position("p1", "SOLUSD"), ts_ms=TS)

    assert rec.cooling("SOLUSD", TS + 1) is False
    assert rec.cooling_symbols(TS + 1) == set()
    assert rec.cooldown_rejections(TS + 1) == {}

    sol = make_opp("SOLUSD")
    rec.queue.offer(sol, episode_id=episode_for(sol), ts_ms=TS)
    books, feats = books_and_features("SOLUSD")
    assert rec.next_candidate(
        books=books, features=feats, fees=Settings().fees, ts_ms=TS + 1
    ) is not None


def test_the_symbol_cooldown_expires_and_is_reported_as_cooldown() -> None:
    s = settings_with(symbol_cooldown_seconds=60.0)
    rec, clock = recycler(settings=s)
    rec.on_close(closed_book(cash=550.0), make_position("p1", "SOLUSD"), ts_ms=TS)

    assert rec.cooldown_rejections(TS + 59_000) == {"SOLUSD": IdleReason.COOLDOWN}
    assert rec.cooling("SOLUSD", TS + 60_000) is False
