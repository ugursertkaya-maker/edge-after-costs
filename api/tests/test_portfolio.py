"""Portfolio state and the capital/risk gate that replaced the position count.

The numbers are chosen so every assertion can be checked with a calculator:
equity $500, a 20% reserve is $100, a 3% open-risk budget is $15, and a 2%
correlated-risk cap is $10.

This module also owns the shared fixtures for ``test_correlation.py`` and
``test_allocation.py`` — they import from here rather than each growing their
own slightly different $500 account.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.config import PortfolioConfig, RiskConfig, Settings
from app.portfolio import (
    DEFAULT_TARGET_DEPLOYED_PCT,
    DEPLOYMENT_BANDS,
    MANY_SMALL_MAX_PCT_EQUITY,
    TARGET_BOOK_MAX,
    TARGET_BOOK_MIN,
    CorrelationEngine,
    PortfolioEngine,
)
from app.types import (
    BookLevel,
    Features,
    Opportunity,
    OrderBook,
    Position,
    PortfolioState,
    Regime,
    RegimeState,
    RejectReason,
    Side,
    Signal,
    SymbolInfo,
)

TS = 1_700_000_000_000


# --------------------------------------------------------------------------- #
# Shared fixtures
# --------------------------------------------------------------------------- #


def make_info(symbol: str = "BTCUSD", **over) -> SymbolInfo:
    kwargs = dict(
        symbol=symbol,
        base_asset=symbol[:-3],
        quote_asset="USD",
        status="TRADING",
        tick_size=0.01,
        step_size=0.00001,
        min_qty=0.00001,
        min_notional=1.0,
    )
    kwargs.update(over)
    return SymbolInfo(**kwargs)  # type: ignore[arg-type]


def make_book(
    symbol: str = "BTCUSD",
    *,
    bid: float = 99.99,
    ask: float = 100.01,
    qty: float = 5.0,
    levels: int = 12,
    ts_ms: int = TS,
) -> OrderBook:
    return OrderBook(
        symbol=symbol,
        bids=[BookLevel(round(bid - i * 0.01, 2), qty) for i in range(levels)],
        asks=[BookLevel(round(ask + i * 0.01, 2), qty) for i in range(levels)],
        last_update_ms=ts_ms,
    )


def make_features(symbol: str = "BTCUSD", **over) -> Features:
    base: dict[str, object] = dict(
        symbol=symbol, ts_ms=TS, price=100.0, mid=100.0, microprice=100.0,
        spread_bps=2.0, book_age_ms=50, depth_bid_usd=6_000.0, depth_ask_usd=6_000.0,
        imbalance=0.0, atr_bps=30.0, realized_vol_bps=25.0, vol_percentile=0.5,
        ret_1m_bps=3.0, ret_5m_bps=6.0, ret_15m_bps=9.0, ema_fast=100.1,
        ema_slow=99.9, ema_slope_bps=1.0, trend_strength=0.6, range_pos=0.5,
        bb_width_bps=60.0, compression=0.3, volume_ratio=1.2,
        trade_flow_imbalance=0.2, trade_flow_accel=0.1, vwap=100.0,
        vwap_dev_bps=1.0, quote_volume_24h=50_000_000.0, trades_24h=100_000,
        bars_available=240,
    )
    base.update(over)
    return Features(**base)  # type: ignore[arg-type]


def make_opp(
    symbol: str = "BTCUSD",
    *,
    net_edge_bps: float = 40.0,
    atr_bps: float = 30.0,
    target_bps: float = 45.0,
    slippage_bps: float = 2.0,
    side: Side = Side.LONG,
    rank_score: float = 1.0,
) -> Opportunity:
    """A tight-stop, positive-edge candidate: the V2 normal case.

    A 30 bps stop is tight, so the *risk* in dollars is small even when the
    position is not.  That gap is the whole reason a count cap is the wrong
    limit.
    """
    sig = Signal(
        strategy="momentum_scalp",
        symbol=symbol,
        side=side,
        score=0.7,
        target_bps=target_bps,
        stop_bps=atr_bps,
        ts_ms=TS,
    )
    return Opportunity(
        signal=sig,
        features=make_features(symbol, atr_bps=atr_bps),
        regime=RegimeState(regime=Regime.TRENDING, confidence=0.8),
        gross_edge_bps=net_edge_bps + 24.0,
        fee_bps=20.0,
        spread_cost_bps=2.0,
        slippage_bps=slippage_bps,
        net_edge_bps=net_edge_bps,
        rank_score=rank_score,
        expected_hold_seconds=1_500,
    )


def make_position(
    pid: str,
    symbol: str,
    *,
    notional: float = 25.0,
    risk: float = 0.25,
    side: Side = Side.LONG,
) -> Position:
    return Position(
        id=pid,
        symbol=symbol,
        side=side,
        strategy="momentum_scalp",
        qty=notional / 100.0,
        entry_price=100.0,
        entry_ms=TS,
        notional_usd=notional,
        risk_usd=risk,
        take_profit=100.5,
        stop_loss=99.7,
        entry_fees_usd=0.0,
        entry_slippage_bps=0.0,
    )


class FakeLedger:
    """The three attributes ``PortfolioEngine.state`` actually reads."""

    def __init__(self, *, cash: float, positions=(), equity: float | None = None):
        self.cash = float(cash)
        self.open_positions = {p.id: p for p in positions}
        self._equity = (
            float(equity)
            if equity is not None
            else self.cash + sum(p.notional_usd for p in self.open_positions.values())
        )

    @property
    def equity(self) -> float:
        return self._equity

    @property
    def exposure_usd(self) -> float:
        return sum(p.notional_usd for p in self.open_positions.values())


def settings_with(portfolio: dict | None = None, risk: dict | None = None) -> Settings:
    s = Settings()
    if portfolio:
        s = dataclasses.replace(s, portfolio=PortfolioConfig(**portfolio))
    if risk:
        s = dataclasses.replace(s, risk=RiskConfig(**risk))
    return s


def make_engine(portfolio: dict | None = None, risk: dict | None = None) -> PortfolioEngine:
    return PortfolioEngine(settings=settings_with(portfolio, risk))


def make_state(
    eng: PortfolioEngine,
    *,
    cash: float = 500.0,
    positions=(),
    equity: float | None = None,
    bars=None,
) -> PortfolioState:
    ledger = FakeLedger(cash=cash, positions=positions, equity=equity)
    return eng.state(ledger, ts_ms=TS, bars_by_symbol=bars)


def symbols(n: int) -> list[str]:
    """n distinct, deterministic symbol names."""
    return [f"C{i:02d}USD" for i in range(n)]


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


def test_state_reports_cash_reserve_deployment_and_risk() -> None:
    eng = make_engine()
    positions = [
        make_position("a", "ETHUSD", notional=40.0, risk=0.40),
        make_position("b", "SOLUSD", notional=60.0, risk=0.60),
    ]
    st = make_state(eng, cash=400.0, positions=positions, equity=500.0)

    assert st.equity == 500.0
    assert st.cash == 400.0
    assert st.deployed_usd == pytest.approx(100.0)
    assert st.reserve_required_usd == pytest.approx(100.0)   # 20% of equity
    assert st.available_usd == pytest.approx(300.0)          # cash minus reserve
    assert st.open_risk_usd == pytest.approx(1.00)
    assert st.open_risk_pct == pytest.approx(0.20)
    assert len(st.positions) == 2


def test_the_reserve_is_a_fraction_of_equity_not_of_cash() -> None:
    """A cash-based reserve shrinks as cash is deployed and reserves nothing."""
    eng = make_engine()
    drained = make_state(
        eng,
        cash=120.0,
        positions=[make_position("a", "ETHUSD", notional=380.0, risk=1.0)],
        equity=500.0,
    )
    assert drained.reserve_required_usd == pytest.approx(100.0)
    assert drained.available_usd == pytest.approx(20.0)


@pytest.mark.parametrize("pct,reserve", [(10.0, 50.0), (20.0, 100.0), (30.0, 150.0)])
def test_reserve_profiles(pct: float, reserve: float) -> None:
    eng = make_engine({"cash_reserve_pct": pct})
    st = make_state(eng, cash=500.0, equity=500.0)
    assert st.reserve_required_usd == pytest.approx(reserve)
    assert st.available_usd == pytest.approx(500.0 - reserve)


def test_state_accepts_a_ledger_whose_positions_are_a_list() -> None:
    eng = make_engine()

    class ListLedger:
        equity = 500.0
        cash = 450.0
        exposure_usd = 50.0
        open_positions = [make_position("a", "ETHUSD", notional=50.0, risk=0.5)]

    st = eng.state(ListLedger(), ts_ms=TS)
    assert len(st.positions) == 1
    assert st.open_risk_usd == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# The headline behaviour change
# --------------------------------------------------------------------------- #


def test_five_hundred_dollars_holds_far_more_than_three_small_tight_positions() -> None:
    """At defaults, $25 positions with tight stops fill a book of 16.

    This is the V2 change in one assertion.  Under V1 the fourth call would
    have been refused because three is three.  Here the book keeps opening
    until *capital* runs out, and the count ceiling (25) is never reached.
    """
    eng = make_engine()
    names = symbols(20)
    positions: list[Position] = []
    opened = 0
    stopped_by = None
    for i, sym in enumerate(names):
        st = make_state(eng, cash=500.0 - 25.0 * opened, positions=positions, equity=500.0)
        stopped_by = eng.can_open(make_opp(sym), st, risk_usd=0.25, notional_usd=25.0)
        if stopped_by is not None:
            break
        positions.append(make_position(f"p{i}", sym, notional=25.0, risk=0.25))
        opened += 1

    assert opened > 3
    # 14 x $25 = $350 = the 70% directional cap.  A *market exposure* limit is
    # what ends the book, which is the point: an economic budget, not a count.
    assert opened == 14
    assert stopped_by is RejectReason.DIRECTIONAL_EXPOSURE
    assert stopped_by is not RejectReason.MAX_POSITIONS
    assert opened < eng.cfg.max_open_positions


def test_what_binds_is_capital_and_risk_not_the_count() -> None:
    """The ceiling is 25; the book stops at 16 because the cash reserve says so."""
    eng = make_engine()
    names = symbols(16)
    positions = [make_position(f"p{i}", s, notional=25.0, risk=0.25) for i, s in enumerate(names)]
    st = make_state(eng, cash=100.0, positions=positions, equity=500.0)

    assert len(st.positions) < eng.cfg.max_open_positions
    assert st.available_usd == pytest.approx(0.0)
    assert eng.can_open(make_opp("XRPUSD"), st, risk_usd=0.25, notional_usd=25.0) is (
        RejectReason.CASH_RESERVE
    )


def test_the_count_ceiling_is_reported_only_when_nothing_economic_binds() -> None:
    """Raise every economic budget out of the way and the valve is still there."""
    eng = make_engine(
        {
            "max_open_positions": 4,
            "cash_reserve_pct": 0.0,
            "total_open_risk_pct": 100.0,
            "max_correlated_risk_pct": 100.0,
            "max_directional_exposure_pct": 1_000.0,
        }
    )
    names = symbols(4)
    positions = [make_position(f"p{i}", s, notional=1.0, risk=0.01) for i, s in enumerate(names)]
    st = make_state(eng, cash=496.0, positions=positions, equity=500.0)
    assert eng.can_open(make_opp("XRPUSD"), st, risk_usd=0.01, notional_usd=1.0) is (
        RejectReason.MAX_POSITIONS
    )


# --------------------------------------------------------------------------- #
# Cash reserve
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("pct", [10.0, 20.0, 30.0])
def test_the_cash_reserve_binds_and_is_never_spent(pct: float) -> None:
    # Directional exposure is lifted so the reserve is the only thing under test.
    eng = make_engine(
        {"cash_reserve_pct": pct, "max_directional_exposure_pct": 1_000.0}
    )
    reserve = 500.0 * pct / 100.0
    deployable = 500.0 - reserve

    st = make_state(eng, cash=500.0, equity=500.0)
    # Everything up to the reserve is allowed...
    assert eng.can_open(make_opp(), st, risk_usd=0.1, notional_usd=deployable) is None
    # ...and the very next cent is not.
    assert eng.can_open(make_opp(), st, risk_usd=0.1, notional_usd=deployable + 0.01) is (
        RejectReason.CASH_RESERVE
    )


def test_a_fully_deployed_book_cannot_open_even_a_zero_sized_position() -> None:
    eng = make_engine()
    st = make_state(
        eng,
        cash=100.0,
        positions=[make_position("a", "ETHUSD", notional=400.0, risk=1.0)],
        equity=500.0,
    )
    assert st.available_usd == pytest.approx(0.0)
    assert eng.can_open(make_opp(), st, risk_usd=0.0, notional_usd=0.0) is (
        RejectReason.CASH_RESERVE
    )


# --------------------------------------------------------------------------- #
# Total open risk budget
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("pct,budget", [(2.0, 10.0), (3.0, 15.0), (5.0, 25.0)])
def test_total_open_risk_budget_binds_with_its_own_reason(pct: float, budget: float) -> None:
    eng = make_engine(
        {"total_open_risk_pct": pct, "max_correlated_risk_pct": 1_000.0}
    )
    # One incumbent already at stake, then a candidate that just fits / just does not.
    incumbent = make_position("a", "ETHUSD", notional=20.0, risk=budget - 1.0)
    st = make_state(eng, cash=480.0, positions=[incumbent], equity=500.0)

    assert eng.can_open(make_opp(), st, risk_usd=1.0, notional_usd=20.0) is None
    assert eng.can_open(make_opp(), st, risk_usd=1.01, notional_usd=20.0) is (
        RejectReason.PORTFOLIO_RISK_BUDGET
    )


def test_an_exhausted_risk_budget_refuses_before_any_sizing_happens() -> None:
    eng = make_engine({"max_correlated_risk_pct": 1_000.0})
    spent = make_position("a", "ETHUSD", notional=20.0, risk=15.0)  # 3% of $500
    st = make_state(eng, cash=480.0, positions=[spent], equity=500.0)
    assert eng.can_open(make_opp(), st, risk_usd=0.0, notional_usd=0.0) is None
    assert eng.can_open(make_opp(), st, risk_usd=0.01, notional_usd=10.0) is (
        RejectReason.PORTFOLIO_RISK_BUDGET
    )


def test_position_size_and_risk_are_separate_budgets() -> None:
    """$20 of notional is not $20 of risk, and the two caps are independent."""
    eng = make_engine({"max_directional_exposure_pct": 1_000.0})
    st = make_state(eng, cash=500.0, equity=500.0)
    # A $450 position risking 20 cents: refused on cash, not on risk.
    assert eng.can_open(make_opp(), st, risk_usd=0.20, notional_usd=450.0) is (
        RejectReason.CASH_RESERVE
    )
    # A $20 position risking $20: refused on risk, not on cash.
    assert eng.can_open(make_opp(), st, risk_usd=20.0, notional_usd=20.0) is (
        RejectReason.PORTFOLIO_RISK_BUDGET
    )


# --------------------------------------------------------------------------- #
# Correlation and direction
# --------------------------------------------------------------------------- #


def test_correlated_exposure_blocks_a_book_that_is_secretly_one_bet() -> None:
    """Eight $1.40 risks that move together are $11.20, over the $10 cap."""
    eng = make_engine({"total_open_risk_pct": 100.0})
    names = symbols(8)
    positions = [make_position(f"p{i}", s, notional=20.0, risk=1.4) for i, s in enumerate(names)]
    st = make_state(eng, cash=340.0, positions=positions, equity=500.0)

    # No correlation view has been supplied, so the engine assumes the worst.
    assert st.correlated_risk_usd == pytest.approx(11.2)
    assert eng.can_open(make_opp("XRPUSD"), st, risk_usd=0.5, notional_usd=20.0) is (
        RejectReason.CORRELATED_EXPOSURE
    )


def test_directional_exposure_cap_blocks_a_ninth_same_direction_position() -> None:
    eng = make_engine(
        {"total_open_risk_pct": 100.0, "max_correlated_risk_pct": 1_000.0}
    )
    names = symbols(8)
    # 8 x $43.75 = $350 = 70% of $500, exactly at the cap.
    positions = [
        make_position(f"p{i}", s, notional=43.75, risk=0.10) for i, s in enumerate(names)
    ]
    st = make_state(eng, cash=150.0, positions=positions, equity=500.0)
    assert st.directional_exposure_pct == pytest.approx(70.0)

    assert eng.can_open(make_opp("XRPUSD"), st, risk_usd=0.1, notional_usd=10.0) is (
        RejectReason.DIRECTIONAL_EXPOSURE
    )
    # A short would reduce net exposure, so direction is not what stops it.
    assert eng.can_open(
        make_opp("XRPUSD", side=Side.SHORT), st, risk_usd=0.1, notional_usd=10.0
    ) is None


def test_one_position_per_symbol() -> None:
    eng = make_engine()
    st = make_state(
        eng,
        cash=475.0,
        positions=[make_position("a", "BTCUSD", notional=25.0, risk=0.25)],
        equity=500.0,
    )
    assert eng.can_open(make_opp("BTCUSD"), st, risk_usd=0.25, notional_usd=25.0) is (
        RejectReason.DUPLICATE_SYMBOL
    )
    assert eng.can_open(make_opp("ETHUSD"), st, risk_usd=0.25, notional_usd=25.0) is None


def test_an_empty_book_admits_the_first_position() -> None:
    eng = make_engine()
    st = make_state(eng, cash=500.0, equity=500.0)
    assert st.correlated_risk_usd == 0.0
    assert st.directional_exposure_pct == 0.0
    assert eng.can_open(make_opp(), st, risk_usd=2.5, notional_usd=125.0) is None


def test_engines_share_one_correlation_engine_when_told_to() -> None:
    corr = CorrelationEngine(settings=Settings())
    eng = PortfolioEngine(settings=Settings(), correlation=corr)
    assert eng.correlation is corr


# --------------------------------------------------------------------------- #
# The deployment band (V5)
# --------------------------------------------------------------------------- #
#
# The band says how much of equity *should* be working.  It is arithmetic on
# the same $500 account: at the default 20% reserve, $400 is deployable, and an
# 80% band wants $400 working — so band and reserve bind at the same dollar.
# Everything here is a property of the plan alone; what the allocator does with
# it, and what happens to the capital the band never reaches, is
# ``test_idle_attribution.py``.


def make_plan(eng: PortfolioEngine, *, candidates: int, cash: float = 500.0,
              positions=(), target: float | None = None):
    state = make_state(eng, cash=cash, positions=positions, equity=500.0)
    return eng.deployment_plan(state, candidates=candidates, target_pct=target)


@pytest.mark.parametrize(
    "target,budget",
    [(60.0, 300.0), (70.0, 350.0), (80.0, 400.0), (85.0, 400.0)],
)
def test_the_band_budget_is_the_tighter_of_the_band_and_the_reserve(
    target: float, budget: float
) -> None:
    """85% asks for $425 and gets $400: the reserve outranks the band.

    Both are ceilings, and a band that could reach past the reserve would make
    the reserve a suggestion.
    """
    plan = make_plan(make_engine(), candidates=10, target=target)
    assert plan.target_usd == pytest.approx(500.0 * target / 100.0)
    assert plan.budget_usd == pytest.approx(budget)
    assert plan.active


def test_the_default_band_sits_inside_the_sixty_to_eighty_five_range() -> None:
    eng = make_engine()
    assert eng.target_deployed_pct == DEFAULT_TARGET_DEPLOYED_PCT
    assert 60.0 <= eng.target_deployed_pct <= 85.0
    assert DEFAULT_TARGET_DEPLOYED_PCT in DEPLOYMENT_BANDS
    # And it is what allocate() uses when the caller says nothing.
    assert make_plan(eng, candidates=10).target_pct == DEFAULT_TARGET_DEPLOYED_PCT


@pytest.mark.parametrize(
    "candidates,slots",
    [(0, 8), (1, 8), (3, 8), (8, 8), (12, 12), (15, 15), (20, 15), (200, 15)],
)
def test_the_piece_count_is_clamped_to_the_target_book_size(
    candidates: int, slots: int
) -> None:
    """Three ideas are sized as three pieces of an eight-position book.

    Clamped below at 8 so scarcity cannot turn into concentration, and above at
    15 so a long list cannot shave the book into slivers that each pay a full
    round trip for a proportionally smaller edge.
    """
    plan = make_plan(make_engine(), candidates=candidates)
    assert plan.slots == slots
    assert TARGET_BOOK_MIN <= plan.slots <= TARGET_BOOK_MAX
    assert plan.per_position_usd == pytest.approx(400.0 / slots)


def test_fewer_candidates_never_buys_bigger_positions() -> None:
    eng = make_engine()
    sizes = [make_plan(eng, candidates=n).per_position_usd for n in (2, 4, 8, 12, 20)]
    assert sizes == sorted(sizes, reverse=True), sizes
    assert max(sizes) == pytest.approx(50.0), "a tenth of equity is the ceiling"


def test_a_position_is_never_more_than_a_tenth_of_equity_or_less_than_the_floor() -> None:
    eng = make_engine()
    cap = 500.0 * MANY_SMALL_MAX_PCT_EQUITY / 100.0
    for n in range(1, 40):
        per = make_plan(eng, candidates=n).per_position_usd
        assert eng.cfg.min_position_usd - 1e-9 <= per <= cap + 1e-9, n
        assert per <= eng.cfg.max_position_usd + 1e-9


def test_the_hard_cap_still_wins_when_it_is_tighter_than_a_tenth_of_equity() -> None:
    eng = make_engine({"max_position_usd": 20.0})
    assert make_plan(eng, candidates=2).per_position_usd == pytest.approx(20.0)


def test_a_book_already_at_the_band_has_no_budget_left() -> None:
    """$400 of a $500 account already working: the 80% band is full."""
    eng = make_engine()
    plan = make_plan(
        eng,
        candidates=10,
        cash=100.0,
        positions=[make_position("a", "ETHUSD", notional=400.0, risk=1.0)],
    )
    assert plan.budget_usd == pytest.approx(0.0)
    assert plan.per_position_usd == 0.0
    assert not plan.active


def test_a_partly_deployed_book_asks_only_for_the_room_that_is_left() -> None:
    eng = make_engine()
    plan = make_plan(
        eng,
        candidates=10,
        cash=300.0,
        positions=[make_position("a", "ETHUSD", notional=200.0, risk=1.0)],
    )
    # Band room is $400 - $200 = $200; deployable cash is $300 - $100 = $200.
    assert plan.budget_usd == pytest.approx(200.0)
    assert plan.per_position_usd == pytest.approx(20.0)


def test_a_zero_band_deploys_nothing_rather_than_falling_back_to_a_default() -> None:
    plan = make_plan(make_engine(), candidates=10, target=0.0)
    assert plan.budget_usd == 0.0 and not plan.active


def test_an_out_of_range_band_is_clamped_not_believed() -> None:
    assert make_plan(make_engine(), candidates=10, target=500.0).target_pct == 100.0
    assert make_plan(make_engine(), candidates=10, target=-5.0).target_pct == 0.0


def test_the_plan_serialises_for_the_dashboard() -> None:
    import json

    payload = make_plan(make_engine(), candidates=10).as_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert payload["slots"] == 10
