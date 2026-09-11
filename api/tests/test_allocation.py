"""The allocation engine: spreading $500 across many small positions.

`test_portfolio.py` proves the individual budgets refuse correctly. This file
proves the thing that walks the ranked list and actually spends the money:
that it consumes the budget as it goes, that it stops when the budget is gone,
that the three allocation modes really differ, and that a list of rubbish
allocates nothing no matter how long it is.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.config import RiskConfig, Settings
from app.risk import RiskEngine
from app.types import Side

from tests.test_portfolio import (
    TS,
    make_book,
    make_engine,
    make_info,
    make_opp,
    make_state,
    settings_with,
    symbols,
)


def risk_engine(settings: Settings | None = None) -> RiskEngine:
    return RiskEngine(settings=settings or Settings())


def allocate(
    eng,
    opportunities,
    *,
    cash: float = 500.0,
    positions=(),
    settings: Settings | None = None,
):
    state = make_state(eng, cash=cash, positions=positions)
    infos = {o.symbol: make_info(o.symbol) for o in opportunities}
    books = {o.symbol: make_book(o.symbol) for o in opportunities}
    return eng.allocate(
        opportunities,
        state,
        risk_engine=risk_engine(settings or eng.settings),
        infos=infos,
        books=books,
    )


def ranked(n: int, **over):
    """n distinct candidates, strictly decreasing in edge so rank order is clear."""
    return [
        make_opp(sym, net_edge_bps=80.0 - i * 2.0, rank_score=10.0 - i, **over)
        for i, sym in enumerate(symbols(n))
    ]


# --------------------------------------------------------------------------- #
# The headline behaviour
# --------------------------------------------------------------------------- #


def test_many_small_positions_open_from_one_pass() -> None:
    """V1 opened one position per pass. V2 must fill the book in one go."""
    eng = make_engine()
    out = allocate(eng, ranked(12))
    assert len(out) > 3, "risk and cash should admit far more than the old 1-3 cap"
    assert all(a.notional_usd > 0 and a.qty > 0 for a in out)


def test_allocation_preserves_rank_order() -> None:
    eng = make_engine()
    out = allocate(eng, ranked(10))
    assert [a.rank for a in out] == sorted(a.rank for a in out)
    assert out[0].opportunity.symbol == "C00USD", "the best idea funds first"


def test_allocation_is_deterministic() -> None:
    eng = make_engine()
    first = allocate(eng, ranked(10))
    second = allocate(make_engine(), ranked(10))
    assert [(a.opportunity.symbol, round(a.qty, 10)) for a in first] == [
        (a.opportunity.symbol, round(a.qty, 10)) for a in second
    ]


def test_the_budget_is_genuinely_consumed_as_it_walks() -> None:
    """The tenth candidate must face the book the first nine created."""
    eng = make_engine()
    out = allocate(eng, ranked(20))
    equity = 500.0
    budget = equity * eng.settings.portfolio.total_open_risk_pct / 100.0
    assert sum(a.risk_usd for a in out) <= budget + 1e-6
    reserve = equity * eng.settings.portfolio.cash_reserve_pct / 100.0
    assert sum(a.notional_usd for a in out) <= equity - reserve + 1e-6


def test_allocation_stops_rather_than_overspending() -> None:
    """A long list does not keep allocating past the budget."""
    eng = make_engine()
    few = allocate(eng, ranked(6))
    many = allocate(eng, ranked(40))
    assert len(many) >= len(few)
    budget = 500.0 * eng.settings.portfolio.total_open_risk_pct / 100.0
    assert sum(a.risk_usd for a in many) <= budget + 1e-6


# --------------------------------------------------------------------------- #
# "Many allowed" is not "many taken"
# --------------------------------------------------------------------------- #


def test_an_empty_ranked_list_allocates_nothing() -> None:
    assert make_engine().allocate([], make_state(make_engine()), risk_engine=risk_engine(),
                                 infos={}, books={}) == []


def test_twenty_negative_edge_candidates_allocate_nothing() -> None:
    """Permission to open many is not permission to open bad ones."""
    eng = make_engine()
    out = allocate(eng, [make_opp(s, net_edge_bps=-5.0) for s in symbols(20)])
    assert out == []


def test_short_candidates_are_never_allocated_on_spot() -> None:
    eng = make_engine()
    out = allocate(eng, [make_opp(s, side=Side.SHORT) for s in symbols(8)])
    assert out == []


def test_a_symbol_without_metadata_is_skipped_not_crashed() -> None:
    eng = make_engine()
    opps = ranked(4)
    state = make_state(eng, cash=500.0)
    infos = {o.symbol: make_info(o.symbol) for o in opps[1:]}   # first one missing
    books = {o.symbol: make_book(o.symbol) for o in opps}
    out = eng.allocate(opps, state, risk_engine=risk_engine(), infos=infos, books=books)
    assert all(a.opportunity.symbol != opps[0].symbol for a in out)


def test_nothing_is_allocated_below_the_exchange_minimum() -> None:
    eng = make_engine()
    for a in allocate(eng, ranked(20)):
        info = make_info(a.opportunity.symbol)
        assert a.notional_usd >= info.min_notional
        assert a.qty >= info.min_qty


# --------------------------------------------------------------------------- #
# Allocation modes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", ["few_large", "many_small", "dynamic"])
def test_every_mode_allocates_something(mode: str) -> None:
    eng = make_engine({"allocation_mode": mode})
    out = allocate(eng, ranked(12), settings=eng.settings)
    assert out, f"{mode} allocated nothing"
    assert all(f"mode={mode}" in a.notes for a in out)


def test_the_three_modes_build_visibly_different_books() -> None:
    """One identical ranked list, three shapes — otherwise the comparison in
    docs/V2_SPEC.md section H.2 would be measuring nothing."""
    books = {}
    for mode in ("few_large", "many_small", "dynamic"):
        eng = make_engine({"allocation_mode": mode})
        out = allocate(eng, ranked(16), settings=eng.settings)
        books[mode] = out

    sizes = {m: [round(a.notional_usd, 4) for a in out] for m, out in books.items()}
    assert sizes["few_large"] != sizes["many_small"], "modes are indistinguishable"

    avg = {m: (sum(v) / len(v) if v else 0.0) for m, v in sizes.items()}
    assert avg["few_large"] > avg["many_small"], (
        f"few_large should hold bigger positions than many_small: {avg}"
    )
    assert len(books["many_small"]) >= len(books["few_large"])


def test_position_bounds_are_honoured_in_every_mode() -> None:
    cfg = {"min_position_usd": 10.0, "max_position_usd": 60.0}
    for mode in ("few_large", "many_small", "dynamic"):
        eng = make_engine({**cfg, "allocation_mode": mode})
        for a in allocate(eng, ranked(14), settings=eng.settings):
            assert a.notional_usd <= cfg["max_position_usd"] + 1e-6, mode


# --------------------------------------------------------------------------- #
# Risk, not size
# --------------------------------------------------------------------------- #


def test_allocated_risk_is_far_smaller_than_allocated_capital() -> None:
    """A $20 position with a tight stop is not $20 of risk — the whole premise."""
    eng = make_engine()
    out = allocate(eng, ranked(12))
    assert out
    deployed = sum(a.notional_usd for a in out)
    at_risk = sum(a.risk_usd for a in out)
    assert at_risk < deployed / 5, (
        f"deployed ${deployed:.2f} should dwarf ${at_risk:.2f} of modelled loss"
    )


def test_a_tighter_stop_buys_more_positions_for_the_same_risk_budget() -> None:
    eng = make_engine()
    wide = allocate(eng, ranked(20, atr_bps=200.0, target_bps=300.0))
    tight = allocate(eng, ranked(20, atr_bps=20.0, target_bps=40.0))
    assert len(tight) >= len(wide)


def test_every_allocation_carries_targets_and_a_stop() -> None:
    for a in allocate(make_engine(), ranked(10)):
        assert a.targets is not None
        assert a.targets.stop_price > 0
        assert a.targets.net_tp_price > 0
        assert a.risk_usd > 0


def test_an_already_loaded_book_allocates_less_than_an_empty_one() -> None:
    from tests.test_portfolio import make_position

    eng = make_engine()
    held = [
        make_position(f"p{i}", f"H{i:02d}USD", notional=40.0, risk=1.0)
        for i in range(3)
    ]
    empty = allocate(eng, ranked(15))
    loaded = allocate(eng, ranked(15), cash=380.0, positions=held)
    assert len(loaded) <= len(empty)
    assert sum(a.risk_usd for a in loaded) <= sum(a.risk_usd for a in empty) + 1e-9
