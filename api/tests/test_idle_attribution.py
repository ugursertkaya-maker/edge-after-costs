"""Why $350 of a $500 account is sitting idle — and the arithmetic that proves it.

Phase 3 measured a book of two concurrent positions with 99.5% of capital idle,
and reported that all three allocation modes produced byte-identical books
because there was nothing to allocate across.  That is a finding about the
*absence* of positions, and the absence of a position leaves no record: a count
of zero looks the same whether nothing qualified, everything correlated, or the
budget was spent.

So every idle dollar is charged to exactly one :class:`IdleReason`, and the
charge is checked here with a calculator rather than with a fixture:

    equity $500 · 20% reserve = $100 · deployable $400 · 80% band = $400
    a book short of 8 positions charges one refusal $400/8 = $50

Every number below falls out of that line.  Nothing here touches the network,
the clock, or a random number: the same book attributes the same dollars every
time, which is the only way the dashboard number is worth reading.

The file also covers deployment *targeting*, because the band and the
attribution are two halves of the same statement — the band says how much
capital should be working, the attribution says where the rest went, and the
pair is only honest if the band is a stopping condition rather than a quota.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.portfolio import (
    DEFAULT_TARGET_DEPLOYED_PCT,
    DEPLOYMENT_BANDS,
    MANY_SMALL_MAX_PCT_EQUITY,
    TARGET_BOOK_MAX,
    TARGET_BOOK_MIN,
    IdleAttribution,
    IdleAttributionError,
    IdleReason,
    attribute_idle,
    idle_reason_for,
)
from app.risk import RiskEngine
from app.types import RejectReason, Side

from tests.test_portfolio import (
    make_book,
    make_engine,
    make_info,
    make_opp,
    make_position,
    make_state,
    symbols,
)

# The whole file is arithmetic against this one account.
EQUITY = 500.0
RESERVE = 100.0        # 20% of equity
DEPLOYABLE = 400.0     # cash after the reserve
SLOT = 50.0            # $400 band / 8 slots — one position's worth


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def ranked(n: int, **over):
    """n candidates, strictly decreasing in edge, so rank order is unambiguous."""
    return [
        make_opp(sym, net_edge_bps=80.0 - i * 2.0, rank_score=10.0 - i, **over)
        for i, sym in enumerate(symbols(n))
    ]


def junk(n: int, *, offset: int = 0):
    """n candidates the risk engine must refuse on their own economics."""
    return [make_opp(s, net_edge_bps=-5.0) for s in symbols(offset + n)[offset:]]


def attribute(eng, state, *, ranked=(), rejections=None, target_pct=80.0, equity=None):
    return attribute_idle(
        state,
        ranked=ranked,
        rejections=rejections,
        target_pct=target_pct,
        equity=equity,
        settings=eng.settings,
    )


def refuse(opps, reason: RejectReason) -> dict[str, RejectReason]:
    return {o.symbol: reason for o in opps}


def allocate_detailed(eng, opps, *, cash: float = 500.0, positions=(), target=None):
    """Run a real allocation pass and hand back the book *and* the refusals."""
    state = make_state(eng, cash=cash, positions=positions)
    result = eng.allocate_detailed(
        opps,
        state,
        risk_engine=RiskEngine(settings=eng.settings),
        infos={o.symbol: make_info(o.symbol) for o in opps},
        books={o.symbol: make_book(o.symbol) for o in opps},
        target_pct=target,
    )
    # The state the allocator *would* have produced, so the attribution sees
    # the book the pass actually built rather than the one it started from.
    after = state
    for a in result.allocations:
        after = eng._commit(
            after,
            a.opportunity,
            entry=a.notional_usd / a.qty,
            qty=a.qty,
            notional_usd=a.notional_usd,
            risk_usd=a.risk_usd,
        )
    return result, after


# --------------------------------------------------------------------------- #
# The reconciliation — the property everything else depends on
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "cash,positions,ranked_n,reason",
    [
        (500.0, 0, 0, None),                               # the empty Phase 3 book
        (500.0, 0, 3, RejectReason.NEGATIVE_EDGE),
        (500.0, 0, 8, RejectReason.CORRELATED_EXPOSURE),
        (500.0, 0, 40, RejectReason.THIN_DEPTH),           # more refusals than money
        (375.0, 5, 6, RejectReason.PORTFOLIO_RISK_BUDGET),  # a partly deployed book
        (120.0, 1, 4, RejectReason.BELOW_MIN_NOTIONAL),    # almost nothing spendable
        (0.0, 1, 5, RejectReason.CASH_RESERVE),            # fully deployed
    ],
)
def test_the_attribution_sums_exactly_to_the_idle_balance(
    cash: float, positions: int, ranked_n: int, reason
) -> None:
    """Six hand-built books, one invariant: the buckets *are* the idle cash.

    Not "approximately", and not "after a residual bucket soaks up the
    difference" — a residual bucket is how a modelling error becomes a number
    on a dashboard that nobody can contradict.
    """
    eng = make_engine()
    held = [
        make_position(f"p{i}", f"H{i:02d}USD", notional=(EQUITY - cash) / max(positions, 1))
        for i in range(positions)
    ]
    state = make_state(eng, cash=cash, positions=held, equity=EQUITY)
    opps = ranked(ranked_n)

    att = attribute(
        eng,
        state,
        ranked=opps,
        rejections=refuse(opps, reason) if reason else {},
    )

    assert sum(att.by_reason.values()) == pytest.approx(att.total_idle_usd, abs=1e-9)
    assert att.total_idle_usd == pytest.approx(cash)
    assert att.residual() == pytest.approx(0.0, abs=1e-9)
    assert set(att.by_reason) == {r.value for r in IdleReason}, "stable dashboard keys"
    assert all(v >= 0.0 for v in att.by_reason.values())


def test_a_book_that_does_not_reconcile_raises_instead_of_fudging() -> None:
    """The failure mode the spec forbids, asserted as a failure.

    ``RuntimeError`` rather than ``assert`` on purpose: ``python -O`` strips
    asserts, and this check must survive into production.
    """
    broken = IdleAttribution(
        total_idle_usd=400.0,
        by_reason={r.value: 0.0 for r in IdleReason} | {"CASH_RESERVE": 399.0},
        deployed_usd=100.0,
        deployed_pct=20.0,
        target_pct=80.0,
        equity=EQUITY,
    )
    assert broken.residual() == pytest.approx(-1.0)
    with pytest.raises(IdleAttributionError) as excinfo:
        broken.check()
    assert "reconcile" in str(excinfo.value)
    # ...and the honest version of the same book does not raise.
    broken.by_reason["NO_QUALIFIED_SIGNAL"] = 1.0
    assert broken.check() is broken


def test_every_reject_reason_maps_to_exactly_one_idle_reason() -> None:
    """A new RejectReason must not be able to appear without a decision.

    Without this, adding a gate would silently enlarge ``NO_QUALIFIED_SIGNAL``
    and the dashboard would report "no opportunities" for a book that was
    refusing opportunities all day.
    """
    from app.portfolio import _REJECT_TO_IDLE

    missing = [r.name for r in RejectReason if r not in _REJECT_TO_IDLE]
    assert missing == [], f"unmapped RejectReason: {missing}"
    assert all(isinstance(v, IdleReason) for v in _REJECT_TO_IDLE.values())


def test_an_unparseable_reason_is_absence_of_a_decision_not_a_new_bucket() -> None:
    assert idle_reason_for(None) is IdleReason.NO_QUALIFIED_SIGNAL
    assert idle_reason_for("SOMETHING_NEW") is IdleReason.NO_QUALIFIED_SIGNAL
    # Both spellings of an input we *can* read are read.
    assert idle_reason_for(RejectReason.THIN_DEPTH) is IdleReason.LIQUIDITY_LIMIT
    assert idle_reason_for("THIN_DEPTH") is IdleReason.LIQUIDITY_LIMIT
    assert idle_reason_for("EXECUTION_COST") is IdleReason.EXECUTION_COST


# --------------------------------------------------------------------------- #
# Each reason, reachable on its own, charged the right dollars
# --------------------------------------------------------------------------- #


def test_the_phase_three_case_zero_candidates_is_all_no_qualified_signal() -> None:
    """The shape the evidence says to expect: nothing ranked, nothing bought.

    $500 idle = $100 reserve + $400 that had nothing to buy.  This is the
    reading that says "expand the universe": the constraint is supply.
    """
    eng = make_engine()
    state = make_state(eng, cash=500.0, equity=EQUITY)

    att = attribute(eng, state, ranked=[], rejections={})

    assert att.usd(IdleReason.NO_QUALIFIED_SIGNAL) == pytest.approx(DEPLOYABLE)
    assert att.usd(IdleReason.CASH_RESERVE) == pytest.approx(RESERVE)
    assert all(
        att.usd(r) == 0.0
        for r in IdleReason
        if r not in (IdleReason.NO_QUALIFIED_SIGNAL, IdleReason.CASH_RESERVE)
    )
    # 100% of *deployable* cash, which is the number the spec asks for.
    assert att.usd(IdleReason.NO_QUALIFIED_SIGNAL) / DEPLOYABLE == pytest.approx(1.0)
    assert att.dominant_reason is IdleReason.NO_QUALIFIED_SIGNAL
    assert att.deployed_usd == 0.0
    assert att.idle_pct == pytest.approx(100.0)


def test_an_all_correlated_list_is_correlation_limit_not_no_qualified_signal() -> None:
    """Candidates existed.  Every one would have doubled a bet we already hold.

    This is the distinction the whole module turns on: "nothing to buy" and
    "everything is the same thing we already own" are opposite diagnoses with
    opposite fixes, and a count of zero positions cannot tell them apart.
    """
    eng = make_engine()
    state = make_state(eng, cash=500.0, equity=EQUITY)
    opps = ranked(8)

    att = attribute(
        eng, state, ranked=opps, rejections=refuse(opps, RejectReason.CORRELATED_EXPOSURE)
    )

    # Eight refusals x $50 = the whole $400 band.
    assert att.usd(IdleReason.CORRELATION_LIMIT) == pytest.approx(DEPLOYABLE)
    assert att.usd(IdleReason.NO_QUALIFIED_SIGNAL) == 0.0
    assert att.usd(IdleReason.CASH_RESERVE) == pytest.approx(RESERVE)
    assert att.dominant_reason is IdleReason.CORRELATION_LIMIT


def test_a_second_position_in_a_symbol_we_hold_is_correlation_not_absence() -> None:
    """DUPLICATE_SYMBOL is not merely correlated with the book; it *is* the book."""
    eng = make_engine()
    state = make_state(eng, cash=500.0, equity=EQUITY)
    opps = ranked(4)
    att = attribute(
        eng, state, ranked=opps, rejections=refuse(opps, RejectReason.DUPLICATE_SYMBOL)
    )
    assert att.usd(IdleReason.CORRELATION_LIMIT) == pytest.approx(4 * SLOT)
    assert att.usd(IdleReason.NO_QUALIFIED_SIGNAL) == pytest.approx(DEPLOYABLE - 4 * SLOT)


@pytest.mark.parametrize(
    "reason,bucket",
    [
        (RejectReason.NEGATIVE_EDGE, IdleReason.EXECUTION_COST),
        (RejectReason.EDGE_BELOW_MIN, IdleReason.EXECUTION_COST),
        (RejectReason.SLIPPAGE_TOO_HIGH, IdleReason.EXECUTION_COST),
        (RejectReason.WIDE_SPREAD, IdleReason.EXECUTION_COST),
        (RejectReason.THIN_DEPTH, IdleReason.LIQUIDITY_LIMIT),
        (RejectReason.BELOW_MIN_NOTIONAL, IdleReason.LIQUIDITY_LIMIT),
        (RejectReason.PORTFOLIO_RISK_BUDGET, IdleReason.RISK_LIMIT),
        (RejectReason.RISK_BUDGET, IdleReason.RISK_LIMIT),
        (RejectReason.MAX_POSITIONS, IdleReason.RISK_LIMIT),
        (RejectReason.CORRELATED_EXPOSURE, IdleReason.CORRELATION_LIMIT),
        (RejectReason.DIRECTIONAL_EXPOSURE, IdleReason.CORRELATION_LIMIT),
        (RejectReason.CASH_RESERVE, IdleReason.CASH_RESERVE),
        (RejectReason.STALE_BOOK, IdleReason.NO_QUALIFIED_SIGNAL),
    ],
)
def test_each_reason_is_reachable_in_isolation(reason, bucket) -> None:
    """Eight identical refusals fill the band, and land in exactly one bucket."""
    eng = make_engine()
    state = make_state(eng, cash=500.0, equity=EQUITY)
    opps = ranked(8)

    att = attribute(eng, state, ranked=opps, rejections=refuse(opps, reason))

    expected = DEPLOYABLE + (RESERVE if bucket is IdleReason.CASH_RESERVE else 0.0)
    assert att.usd(bucket) == pytest.approx(expected)
    assert att.by_reject_reason == {reason.value: pytest.approx(DEPLOYABLE)}


def test_a_mixed_book_splits_the_dollars_the_way_the_calculator_does() -> None:
    """Five refusals, five different reasons, $50 each, and the rest unspent.

        C00 NEGATIVE_EDGE        -> EXECUTION_COST      $50
        C01 CORRELATED_EXPOSURE  -> CORRELATION_LIMIT   $50
        C02 THIN_DEPTH           -> LIQUIDITY_LIMIT     $50
        C03 PORTFOLIO_RISK_BUDGET-> RISK_LIMIT          $50
        C04 CASH_RESERVE         -> CASH_RESERVE        $50 + $100 reserve
        (list exhausted)         -> NO_QUALIFIED_SIGNAL $150
                                                        ----
                                                        $500
    """
    eng = make_engine()
    state = make_state(eng, cash=500.0, equity=EQUITY)
    opps = ranked(5)
    rejections = {
        opps[0].symbol: RejectReason.NEGATIVE_EDGE,
        opps[1].symbol: RejectReason.CORRELATED_EXPOSURE,
        opps[2].symbol: RejectReason.THIN_DEPTH,
        opps[3].symbol: RejectReason.PORTFOLIO_RISK_BUDGET,
        opps[4].symbol: RejectReason.CASH_RESERVE,
    }

    att = attribute(eng, state, ranked=opps, rejections=rejections)

    assert att.usd(IdleReason.EXECUTION_COST) == pytest.approx(50.0)
    assert att.usd(IdleReason.CORRELATION_LIMIT) == pytest.approx(50.0)
    assert att.usd(IdleReason.LIQUIDITY_LIMIT) == pytest.approx(50.0)
    assert att.usd(IdleReason.RISK_LIMIT) == pytest.approx(50.0)
    assert att.usd(IdleReason.CASH_RESERVE) == pytest.approx(150.0)
    assert att.usd(IdleReason.NO_QUALIFIED_SIGNAL) == pytest.approx(150.0)
    assert sum(att.by_reason.values()) == pytest.approx(500.0)


def test_more_refusals_than_money_charges_the_money_in_rank_order() -> None:
    """$20 of band against four refusals: the top two own it, the rest own nothing.

    Capital is charged in the order it would have been *consumed*, so a refusal
    ranked ninth cannot be blamed for dollars the first eight had already
    spent.  $20 buys two minimum positions, so two refusals are charged $10 and
    the rest are charged nothing at all.
    """
    eng = make_engine()
    held = [make_position("big", "HLDUSD", notional=380.0, risk=1.0)]
    state = make_state(eng, cash=120.0, positions=held, equity=EQUITY)
    assert state.available_usd == pytest.approx(20.0)

    opps = ranked(4)
    att = attribute(
        eng,
        state,
        ranked=opps,
        rejections={
            opps[0].symbol: RejectReason.NEGATIVE_EDGE,
            opps[1].symbol: RejectReason.THIN_DEPTH,
            opps[2].symbol: RejectReason.CORRELATED_EXPOSURE,
            opps[3].symbol: RejectReason.CORRELATED_EXPOSURE,
        },
    )

    assert att.usd(IdleReason.EXECUTION_COST) == pytest.approx(10.0)
    assert att.usd(IdleReason.LIQUIDITY_LIMIT) == pytest.approx(10.0)
    assert att.usd(IdleReason.CORRELATION_LIMIT) == 0.0, "the money never reached them"
    assert att.usd(IdleReason.CASH_RESERVE) == pytest.approx(100.0)
    assert sum(att.by_reason.values()) == pytest.approx(120.0)


def test_a_refusal_outside_the_ranked_list_is_walked_rather_than_dropped() -> None:
    eng = make_engine()
    state = make_state(eng, cash=500.0, equity=EQUITY)
    opps = ranked(2)
    att = attribute(
        eng,
        state,
        ranked=opps,
        rejections={
            opps[0].symbol: RejectReason.NEGATIVE_EDGE,
            "ORPHANUSD": RejectReason.THIN_DEPTH,
        },
    )
    assert att.usd(IdleReason.EXECUTION_COST) == pytest.approx(SLOT)
    assert att.usd(IdleReason.LIQUIDITY_LIMIT) == pytest.approx(SLOT)


@pytest.mark.parametrize(
    "rejections",
    [
        {"C00USD": RejectReason.THIN_DEPTH},
        [("C00USD", RejectReason.THIN_DEPTH)],
        [("C00USD", "THIN_DEPTH")],
    ],
)
def test_rejections_may_arrive_in_any_shape_the_worker_finds_convenient(
    rejections,
) -> None:
    eng = make_engine()
    state = make_state(eng, cash=500.0, equity=EQUITY)
    att = attribute(eng, state, ranked=ranked(1), rejections=rejections)
    assert att.usd(IdleReason.LIQUIDITY_LIMIT) == pytest.approx(SLOT)


# --------------------------------------------------------------------------- #
# The cash reserve
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("pct", [10.0, 20.0, 30.0])
def test_the_reserve_is_never_spent_and_is_always_attributed_to_cash_reserve(
    pct: float,
) -> None:
    """Whatever else happens, the reserve is idle and it is idle on purpose."""
    eng = make_engine({"cash_reserve_pct": pct})
    reserve = EQUITY * pct / 100.0
    state = make_state(eng, cash=500.0, equity=EQUITY)
    opps = ranked(8)

    for reason in (
        RejectReason.NEGATIVE_EDGE,
        RejectReason.CORRELATED_EXPOSURE,
        RejectReason.THIN_DEPTH,
    ):
        att = attribute(eng, state, ranked=opps, rejections=refuse(opps, reason))
        assert att.usd(IdleReason.CASH_RESERVE) >= reserve - 1e-9, (
            "the reserve was attributed to the market instead of to policy"
        )
        assert sum(att.by_reason.values()) == pytest.approx(500.0)


def test_the_allocator_never_spends_the_reserve_and_the_attribution_says_so() -> None:
    """End to end: allocate for real, then explain what is left."""
    eng = make_engine()
    opps = ranked(20)
    result, after = allocate_detailed(eng, opps)

    assert result.deployed_usd <= DEPLOYABLE + 1e-9
    assert after.cash >= RESERVE - 1e-9, "the reserve was spent"

    att = eng.attribute_idle(after, ranked=opps, rejections=result.rejections)
    assert att.usd(IdleReason.CASH_RESERVE) >= RESERVE - 1e-9
    assert sum(att.by_reason.values()) == pytest.approx(att.total_idle_usd)


def test_capital_the_band_deliberately_leaves_alone_is_cash_reserve() -> None:
    """A 60% band on an empty $500 book holds back $200, and says why.

    $100 of that is the reserve and $100 is the band's own decision; neither is
    the market's fault, so neither may be charged to it.  Otherwise lowering
    the band would *manufacture* evidence that the universe is too small.
    """
    eng = make_engine()
    state = make_state(eng, cash=500.0, equity=EQUITY)

    att = attribute(eng, state, ranked=[], rejections={}, target_pct=60.0)

    assert att.usd(IdleReason.CASH_RESERVE) == pytest.approx(200.0)
    assert att.usd(IdleReason.NO_QUALIFIED_SIGNAL) == pytest.approx(300.0)
    assert att.target_pct == 60.0
    assert att.target_gap_pct == pytest.approx(60.0)


# --------------------------------------------------------------------------- #
# Percentages agree with the ledger
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("cash,deployed", [(500.0, 0.0), (350.0, 150.0), (0.0, 500.0)])
def test_deployed_pct_and_idle_pct_are_consistent_with_the_ledgers_equity(
    cash: float, deployed: float
) -> None:
    eng = make_engine()
    held = (
        [make_position("a", "ETHUSD", notional=deployed, risk=1.0)] if deployed else []
    )
    state = make_state(eng, cash=cash, positions=held, equity=EQUITY)

    att = attribute(eng, state, ranked=[], rejections={})

    assert att.equity == pytest.approx(EQUITY)
    assert att.deployed_usd == pytest.approx(deployed)
    assert att.deployed_pct == pytest.approx(deployed / EQUITY * 100.0)
    assert att.idle_pct == pytest.approx(cash / EQUITY * 100.0)
    assert att.deployed_pct + att.idle_pct == pytest.approx(100.0)
    assert att.as_dict()["idle_pct"] == pytest.approx(att.idle_pct)


def test_percentages_do_not_explode_on_a_zero_equity_account() -> None:
    eng = make_engine()
    state = make_state(eng, cash=0.0, equity=0.0)
    att = attribute(eng, state, ranked=[], rejections={}, equity=0.0)
    assert att.total_idle_usd == 0.0
    assert att.deployed_pct == 0.0 and att.idle_pct == 0.0
    assert att.dominant_reason is None


def test_as_dict_is_json_shaped_for_the_dashboard() -> None:
    import json

    eng = make_engine()
    opps = ranked(3)
    state = make_state(eng, cash=500.0, equity=EQUITY)
    att = attribute(eng, state, ranked=opps, rejections=refuse(opps, RejectReason.NEGATIVE_EDGE))

    payload = att.as_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert payload["dominant_reason"] == IdleReason.NO_QUALIFIED_SIGNAL.value
    assert payload["by_reason"]["EXECUTION_COST"] == pytest.approx(150.0)


# --------------------------------------------------------------------------- #
# Deployment targeting — a stopping condition, never a quota
# --------------------------------------------------------------------------- #


def test_a_target_of_85_pct_with_two_qualifying_candidates_does_not_open_a_third() -> None:
    """The load-bearing test for "aggressive is not a lower bar".

    Two good candidates and five negative-edge ones, at the most aggressive
    band.  The band wants $425 working.  It gets $74.68, because that is what
    qualified — and the other $350 is *explained*, not filled.
    """
    eng = make_engine()
    good = ranked(2)
    bad = junk(5, offset=2)
    result, after = allocate_detailed(eng, good + bad, target=85.0)

    assert len(result.allocations) == 2, "the band manufactured a position"
    assert {a.opportunity.symbol for a in result.allocations} == {
        o.symbol for o in good
    }
    assert all(r is RejectReason.NEGATIVE_EDGE for r in result.rejections.values())
    assert after.deployed_pct < 85.0

    att = eng.attribute_idle(after, ranked=good + bad, rejections=result.rejections, target_pct=85.0)
    # Five refusals x $50 of a $325.32 band, and the remainder had no candidate.
    assert att.usd(IdleReason.EXECUTION_COST) == pytest.approx(250.0)
    assert att.usd(IdleReason.CASH_RESERVE) == pytest.approx(RESERVE)
    assert att.usd(IdleReason.NO_QUALIFIED_SIGNAL) == pytest.approx(75.32, abs=0.01)
    assert sum(att.by_reason.values()) == pytest.approx(att.total_idle_usd)
    assert att.target_gap_pct > 0.0, "the gap is the finding, not a failure"


def test_the_band_stops_the_walk_once_it_is_reached() -> None:
    """Below target and still qualifying: keep going.  At target: stop.

    Directional exposure is lifted so the band is the only thing under test.
    """
    eng = make_engine({"max_directional_exposure_pct": 1_000.0})
    opps = ranked(12)

    small, _ = allocate_detailed(eng, opps, target=20.0)
    big, _ = allocate_detailed(eng, opps, target=80.0)

    assert small.deployed_usd < big.deployed_usd
    assert len(small.allocations) <= len(big.allocations)
    # A stopping condition overshoots by at most the last position it opened.
    assert small.deployed_usd >= EQUITY * 0.20
    assert small.deployed_usd <= EQUITY * 0.20 + eng.cfg.max_position_usd


@pytest.mark.parametrize("band", DEPLOYMENT_BANDS)
def test_a_wider_band_never_deploys_less_than_a_narrower_one(band: float) -> None:
    eng = make_engine({"max_directional_exposure_pct": 1_000.0})
    opps = ranked(12)
    narrow, _ = allocate_detailed(eng, opps, target=60.0)
    wide, _ = allocate_detailed(eng, opps, target=band)
    assert wide.deployed_usd >= narrow.deployed_usd - 1e-9
    assert wide.plan.target_pct == band


def test_the_band_does_not_lower_the_bar_to_fill_itself() -> None:
    """Twenty candidates that cannot pay for their own round trip, 85% band.

    The most aggressive band with the largest possible list still allocates
    nothing, and charges every dollar to EXECUTION_COST — which is the reading
    that says "more markets will not help".
    """
    eng = make_engine()
    bad = junk(20)
    result, after = allocate_detailed(eng, bad, target=85.0)

    assert result.allocations == []
    att = eng.attribute_idle(after, ranked=bad, rejections=result.rejections, target_pct=85.0)
    assert att.dominant_reason is IdleReason.EXECUTION_COST
    assert att.usd(IdleReason.EXECUTION_COST) == pytest.approx(DEPLOYABLE)
    assert att.usd(IdleReason.NO_QUALIFIED_SIGNAL) == 0.0


def test_a_short_candidate_is_absence_of_a_tradable_signal_not_a_cost() -> None:
    eng = make_engine()
    shorts = [make_opp(s, side=Side.SHORT) for s in symbols(8)]
    result, after = allocate_detailed(eng, shorts)
    assert result.allocations == []
    att = eng.attribute_idle(after, ranked=shorts, rejections=result.rejections)
    assert att.usd(IdleReason.NO_QUALIFIED_SIGNAL) == pytest.approx(DEPLOYABLE)


# --------------------------------------------------------------------------- #
# Many small positions
# --------------------------------------------------------------------------- #


def test_a_rich_candidate_list_builds_a_book_of_8_to_15_small_positions() -> None:
    """The shape the spec asks for, from the sizing rather than from a quota.

    Twenty qualifying candidates on $500: 8-15 positions, none below the
    exchange minimum, none above a tenth of equity, and the whole book inside
    the 3% open-risk budget.
    """
    eng = make_engine()
    result, after = allocate_detailed(eng, ranked(20))
    notionals = [a.notional_usd for a in result.allocations]

    assert TARGET_BOOK_MIN <= len(notionals) <= TARGET_BOOK_MAX, notionals
    assert min(notionals) >= eng.cfg.min_position_usd - 1e-9
    assert max(notionals) <= EQUITY * MANY_SMALL_MAX_PCT_EQUITY / 100.0 + 1e-9
    assert max(notionals) <= eng.cfg.max_position_usd + 1e-9
    # "Many small" is a claim about the typical position, not just the extremes.
    assert sum(notionals) / len(notionals) <= 50.0

    risk_budget = EQUITY * eng.cfg.total_open_risk_pct / 100.0
    assert after.open_risk_usd <= risk_budget + 1e-9
    assert result.risk_usd <= risk_budget + 1e-9


@pytest.mark.parametrize("min_usd,max_usd", [(10.0, 50.0), (5.0, 25.0), (15.0, 125.0)])
def test_the_per_position_bounds_hold_whatever_they_are_set_to(
    min_usd: float, max_usd: float
) -> None:
    eng = make_engine({"min_position_usd": min_usd, "max_position_usd": max_usd})
    result, _ = allocate_detailed(eng, ranked(16))
    assert result.allocations
    for a in result.allocations:
        assert a.notional_usd >= min(min_usd, 10.0) - 1e-9   # exchange floor still applies
        assert a.notional_usd <= max_usd + 1e-9


def test_scarce_candidates_are_sized_as_a_small_book_not_as_big_bets() -> None:
    """Three ideas must not become three $130 positions.

    Sizing divides the band by at least ``TARGET_BOOK_MIN``, so fewer ideas
    means a smaller book — not a more concentrated one.  Concentrating on
    scarcity is exactly how "deploy more capital" becomes "take more risk".
    """
    eng = make_engine()
    result, _ = allocate_detailed(eng, ranked(3))
    assert len(result.allocations) == 3
    assert max(a.notional_usd for a in result.allocations) <= (
        EQUITY * MANY_SMALL_MAX_PCT_EQUITY / 100.0 + 1e-9
    )
    assert result.deployed_usd < EQUITY * DEFAULT_TARGET_DEPLOYED_PCT / 100.0


# --------------------------------------------------------------------------- #
# The allocator's own record
# --------------------------------------------------------------------------- #


def test_every_unfunded_candidate_carries_a_reason() -> None:
    """No candidate may fall off the walk unexplained.

    An unexplained candidate is silently reattributed to NO_QUALIFIED_SIGNAL by
    the residue rule, which is the one reading the measurement must not get
    wrong by accident.
    """
    eng = make_engine()
    opps = ranked(20)
    result, _ = allocate_detailed(eng, opps)

    funded = {a.opportunity.symbol for a in result.allocations}
    unfunded = [o.symbol for o in opps if o.symbol not in funded]
    # The band was never reached here (directional exposure binds first), so
    # every unfunded candidate must have a recorded refusal.
    assert result.plan.target_usd > result.deployed_usd
    assert sorted(result.rejections) == sorted(unfunded)
    assert not (funded & set(result.rejections)), "funded and refused at once"


def test_allocate_and_allocate_detailed_agree() -> None:
    eng = make_engine()
    opps = ranked(12)
    state = make_state(eng, cash=500.0)
    kwargs = dict(
        risk_engine=RiskEngine(settings=Settings()),
        infos={o.symbol: make_info(o.symbol) for o in opps},
        books={o.symbol: make_book(o.symbol) for o in opps},
    )
    plain = eng.allocate(opps, state, **kwargs)
    detailed = eng.allocate_detailed(opps, state, **kwargs)
    assert [(a.opportunity.symbol, a.qty) for a in plain] == [
        (a.opportunity.symbol, a.qty) for a in detailed.allocations
    ]
    assert len(detailed) == len(plain)


def test_a_candidate_without_metadata_is_recorded_not_silently_dropped() -> None:
    eng = make_engine()
    opps = ranked(4)
    state = make_state(eng, cash=500.0)
    result = eng.allocate_detailed(
        opps,
        state,
        risk_engine=RiskEngine(settings=Settings()),
        infos={o.symbol: make_info(o.symbol) for o in opps[1:]},  # first is missing
        books={o.symbol: make_book(o.symbol) for o in opps},
    )
    assert result.rejections[opps[0].symbol] is RejectReason.DATA_FAILURE
    assert all(a.opportunity.symbol != opps[0].symbol for a in result.allocations)


def test_the_whole_cycle_reconciles_on_a_partly_deployed_book() -> None:
    """Allocate onto a book that already holds three positions, then explain it."""
    eng = make_engine()
    held = [
        make_position(f"h{i}", f"H{i:02d}USD", notional=40.0, risk=1.0) for i in range(3)
    ]
    opps = ranked(15)
    result, after = allocate_detailed(eng, opps, cash=380.0, positions=held)

    att = eng.attribute_idle(after, ranked=opps, rejections=result.rejections)
    assert sum(att.by_reason.values()) == pytest.approx(att.total_idle_usd, abs=1e-9)
    assert att.deployed_usd == pytest.approx(120.0 + result.deployed_usd)
    assert att.deployed_pct + att.idle_pct == pytest.approx(100.0)
