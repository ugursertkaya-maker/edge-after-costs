"""OpportunityRanker: the cost arithmetic, the shrinkage of thin history, the
min-net-edge gate and a deterministic ordering."""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.ranker import (
    HISTORY_WEIGHT,
    P_HIT_CEIL,
    P_HIT_FLOOR,
    REGIME_WEIGHT,
    SHRINKAGE_K,
    SIGNAL_WEIGHT,
    SPREAD_WEIGHT,
    OpportunityRanker,
    shrunk_expectancy,
)
from app.types import Regime

from fixtures.market import make_features, make_regime, make_signal

S = get_settings()


def p_hit(score: float, conf: float) -> float:
    blend = SIGNAL_WEIGHT * score + REGIME_WEIGHT * conf
    return P_HIT_FLOOR + (P_HIT_CEIL - P_HIT_FLOOR) * blend


# --------------------------------------------------------------------------- #
# P(hit)
# --------------------------------------------------------------------------- #


def test_p_hit_blends_score_and_regime_confidence_inside_its_band():
    r = OpportunityRanker()
    assert r.p_hit(make_signal(score=1.0), make_regime(confidence=1.0)) == pytest.approx(
        P_HIT_CEIL
    )
    assert r.p_hit(make_signal(score=0.0), make_regime(confidence=0.0)) == pytest.approx(
        P_HIT_FLOOR
    )
    assert r.p_hit(make_signal(score=0.6), make_regime(confidence=0.5)) == pytest.approx(
        p_hit(0.6, 0.5)
    )


def test_p_hit_clips_out_of_range_inputs():
    r = OpportunityRanker()
    assert r.p_hit(make_signal(score=5.0), make_regime(confidence=9.0)) == pytest.approx(
        P_HIT_CEIL
    )
    assert r.p_hit(
        make_signal(score=-3.0), make_regime(confidence=-1.0)
    ) == pytest.approx(P_HIT_FLOOR)


# --------------------------------------------------------------------------- #
# Cost arithmetic
# --------------------------------------------------------------------------- #


def test_build_subtracts_round_trip_fees_spread_and_slippage():
    r = OpportunityRanker()
    f = make_features(spread_bps=2.0)
    sig = make_signal(score=0.6, target_bps=30.0)
    regime = make_regime(Regime.TRENDING, 0.5)

    opp = r.build(sig, f, regime, taker_bps=10.0, maker_bps=10.0, slippage_bps=2.0)

    expected_gross = 30.0 * p_hit(0.6, 0.5)
    assert opp.gross_edge_bps == pytest.approx(expected_gross)
    assert opp.fee_bps == pytest.approx(20.0)          # both legs
    assert opp.spread_cost_bps == pytest.approx(2.0)   # two half-spreads
    assert opp.slippage_bps == pytest.approx(2.0)
    assert opp.net_edge_bps == pytest.approx(expected_gross - 20.0 - 2.0 - 2.0)
    # 10 bps a side on Binance.US standard tier simply eats a 30 bps scalp.
    assert opp.net_edge_bps < 0


def test_a_resting_leg_pays_only_one_half_spread():
    r = OpportunityRanker()
    f = make_features(spread_bps=6.0)
    sig = make_signal(score=0.6, target_bps=30.0)
    regime = make_regime(Regime.TRENDING, 0.5)

    crossing = r.build(sig, f, regime, taker_bps=1.0, maker_bps=1.0, slippage_bps=0.0)
    resting = r.build(sig, f, regime, taker_bps=1.0, maker_bps=0.0, slippage_bps=0.0)

    assert crossing.spread_cost_bps == pytest.approx(6.0)
    assert resting.spread_cost_bps == pytest.approx(3.0)
    assert resting.net_edge_bps == pytest.approx(crossing.net_edge_bps + 3.0 + 1.0)


def test_tier0_fees_leave_a_positive_edge_on_the_same_signal():
    r = OpportunityRanker()
    f = make_features(spread_bps=2.0)
    sig = make_signal(score=0.6, target_bps=30.0)
    regime = make_regime(Regime.TRENDING, 0.5)

    opp = r.build(sig, f, regime, taker_bps=1.0, maker_bps=0.0, slippage_bps=2.0)
    assert opp.net_edge_bps == pytest.approx(30.0 * p_hit(0.6, 0.5) - 1.0 - 1.0 - 2.0)
    assert opp.net_edge_bps > 0


def test_gross_edge_never_exceeds_the_strategys_own_target():
    r = OpportunityRanker()
    opp = r.build(
        make_signal(score=1.0, target_bps=50.0),
        make_features(),
        make_regime(Regime.TRENDING, 1.0),
        taker_bps=0.0,
        maker_bps=0.0,
        slippage_bps=0.0,
    )
    assert opp.gross_edge_bps < 50.0
    assert opp.gross_edge_bps == pytest.approx(50.0 * P_HIT_CEIL)


def test_negative_costs_are_not_credited_back():
    r = OpportunityRanker()
    opp = r.build(
        make_signal(),
        make_features(spread_bps=-5.0),
        make_regime(),
        taker_bps=-1.0,
        maker_bps=-1.0,
        slippage_bps=-3.0,
    )
    assert opp.fee_bps == 0.0
    assert opp.spread_cost_bps == 0.0
    assert opp.slippage_bps == 0.0


def test_expected_hold_seconds_comes_from_the_signal_horizon():
    r = OpportunityRanker()
    opp = r.build(
        make_signal(horizon_seconds=123),
        make_features(),
        make_regime(),
        taker_bps=0.0,
        maker_bps=0.0,
        slippage_bps=0.0,
    )
    assert opp.expected_hold_seconds == 123


# --------------------------------------------------------------------------- #
# Historical expectancy and shrinkage
# --------------------------------------------------------------------------- #


def test_shrinkage_halves_the_history_at_n_equals_k():
    assert shrunk_expectancy(10.0, 0) == 0.0
    assert shrunk_expectancy(10.0, int(SHRINKAGE_K)) == pytest.approx(5.0)
    assert shrunk_expectancy(10.0, 1) == pytest.approx(10.0 / 26.0)
    assert shrunk_expectancy(10.0, 975) == pytest.approx(9.75)
    # symmetric for losing history
    assert shrunk_expectancy(-10.0, int(SHRINKAGE_K)) == pytest.approx(-5.0)


def test_expectancy_provider_is_asked_for_strategy_symbol_regime():
    seen: list[tuple[str, str, str]] = []

    def provider(strategy: str, symbol: str, regime: str) -> tuple[float, int]:
        seen.append((strategy, symbol, regime))
        return 12.0, 50

    r = OpportunityRanker(expectancy=provider)
    opp = r.build(
        make_signal(strategy="flow_impulse", symbol="ETHUSD"),
        make_features(symbol="ETHUSD"),
        make_regime(Regime.BREAKOUT, 0.7),
        taker_bps=0.0,
        maker_bps=0.0,
        slippage_bps=0.0,
    )
    assert seen == [("flow_impulse", "ETHUSD", "BREAKOUT")]
    assert opp.historical_expectancy_bps == 12.0
    assert opp.sample_size == 50


def test_thin_history_barely_moves_the_rank_score():
    f = make_features()
    sig = make_signal()
    regime = make_regime(Regime.TRENDING, 0.6)
    kwargs = dict(taker_bps=1.0, maker_bps=0.0, slippage_bps=1.0)

    none = OpportunityRanker(expectancy=lambda *_: (0.0, 0)).build(sig, f, regime, **kwargs)
    thin = OpportunityRanker(expectancy=lambda *_: (40.0, 1)).build(sig, f, regime, **kwargs)
    rich = OpportunityRanker(expectancy=lambda *_: (40.0, 400)).build(sig, f, regime, **kwargs)

    assert none.net_edge_bps == thin.net_edge_bps == rich.net_edge_bps
    assert thin.rank_score - none.rank_score < 1.0
    assert rich.rank_score > thin.rank_score + 10.0


def test_rank_score_formula_is_explicit():
    r = OpportunityRanker(expectancy=lambda *_: (10.0, int(SHRINKAGE_K)))
    f = make_features(spread_bps=4.0, depth_bid_usd=50_000.0, depth_ask_usd=50_000.0)
    sig = make_signal(horizon_seconds=300)
    opp = r.build(
        sig, f, make_regime(Regime.TRENDING, 0.5),
        taker_bps=1.0, maker_bps=0.0, slippage_bps=1.0,
    )

    liquidity = 1.0        # both sides far past 5x min_book_depth_usd
    turnover = 0.75        # hold == TURNOVER_REF_SECONDS
    expected = (
        opp.net_edge_bps + HISTORY_WEIGHT * 5.0 - SPREAD_WEIGHT * 4.0
    ) * liquidity * turnover
    assert opp.rank_score == pytest.approx(expected)


def test_thin_books_and_long_holds_are_penalised():
    r = OpportunityRanker()
    regime = make_regime(Regime.TRENDING, 0.6)
    kwargs = dict(taker_bps=0.0, maker_bps=0.0, slippage_bps=0.0)

    deep = r.build(make_signal(), make_features(), regime, **kwargs)
    thin = r.build(
        make_signal(),
        make_features(depth_bid_usd=10.0, depth_ask_usd=10.0),
        regime,
        **kwargs,
    )
    assert thin.net_edge_bps == deep.net_edge_bps
    assert thin.rank_score < deep.rank_score

    quick = r.build(make_signal(horizon_seconds=60), make_features(), regime, **kwargs)
    slow = r.build(make_signal(horizon_seconds=3_600), make_features(), regime, **kwargs)
    assert quick.rank_score > slow.rank_score


# --------------------------------------------------------------------------- #
# qualifies()
# --------------------------------------------------------------------------- #


def build_with_net(net: float, *, regime: Regime = Regime.TRENDING, stop_bps: float = 20.0):
    r = OpportunityRanker()
    opp = r.build(
        make_signal(stop_bps=stop_bps),
        make_features(),
        make_regime(regime, 0.6),
        taker_bps=0.0,
        maker_bps=0.0,
        slippage_bps=0.0,
    )
    opp.net_edge_bps = net
    return r, opp


def test_qualifies_at_the_min_net_edge_boundary():
    minimum = S.risk.min_net_edge_bps
    r, exactly = build_with_net(minimum)
    assert r.qualifies(exactly) is True

    r, just_under = build_with_net(minimum - 1e-9)
    assert r.qualifies(just_under) is False

    r, over = build_with_net(minimum + 1.0)
    assert r.qualifies(over) is True


def test_qualifies_rejects_a_negative_net_edge():
    r, opp = build_with_net(-0.01)
    assert r.qualifies(opp) is False


def test_qualifies_rejects_an_abnormal_regime_whatever_the_edge():
    r, opp = build_with_net(1_000.0, regime=Regime.ABNORMAL)
    assert r.qualifies(opp) is False


def test_qualifies_rejects_a_non_positive_stop():
    r, opp = build_with_net(1_000.0, stop_bps=0.0)
    assert r.qualifies(opp) is False


def test_gross_positive_but_net_negative_is_rejected():
    r = OpportunityRanker()
    opp = r.build(
        make_signal(score=0.9, target_bps=25.0),
        make_features(spread_bps=6.0),
        make_regime(Regime.TRENDING, 0.8),
        taker_bps=10.0,
        maker_bps=10.0,
        slippage_bps=3.0,
    )
    assert opp.gross_edge_bps > 0
    assert opp.net_edge_bps < 0
    assert r.qualifies(opp) is False


# --------------------------------------------------------------------------- #
# rank()
# --------------------------------------------------------------------------- #


def make_opp(rank_score: float, symbol: str, strategy: str = "momentum_scalp"):
    r = OpportunityRanker()
    opp = r.build(
        make_signal(symbol=symbol, strategy=strategy),
        make_features(symbol=symbol),
        make_regime(Regime.TRENDING, 0.6),
        taker_bps=0.0,
        maker_bps=0.0,
        slippage_bps=0.0,
    )
    opp.rank_score = rank_score
    return opp


def test_rank_returns_best_first():
    r = OpportunityRanker()
    opps = [make_opp(1.0, "AAAUSD"), make_opp(9.0, "BBBUSD"), make_opp(5.0, "CCCUSD")]
    assert [o.symbol for o in r.rank(opps)] == ["BBBUSD", "CCCUSD", "AAAUSD"]


def test_rank_breaks_ties_on_symbol_then_strategy():
    r = OpportunityRanker()
    opps = [
        make_opp(3.0, "ZZZUSD", "flow_impulse"),
        make_opp(3.0, "AAAUSD", "momentum_scalp"),
        make_opp(3.0, "AAAUSD", "book_imbalance"),
    ]
    ranked = r.rank(opps)
    assert [(o.symbol, o.signal.strategy) for o in ranked] == [
        ("AAAUSD", "book_imbalance"),
        ("AAAUSD", "momentum_scalp"),
        ("ZZZUSD", "flow_impulse"),
    ]


def test_rank_is_deterministic_regardless_of_input_order():
    r = OpportunityRanker()
    base = [make_opp(2.0, "AAAUSD"), make_opp(2.0, "BBBUSD"), make_opp(7.0, "CCCUSD")]
    forward = [(o.symbol, o.rank_score) for o in r.rank(list(base))]
    backward = [(o.symbol, o.rank_score) for o in r.rank(list(reversed(base)))]
    assert forward == backward


def test_rank_does_not_mutate_its_input():
    r = OpportunityRanker()
    opps = [make_opp(1.0, "BBBUSD"), make_opp(9.0, "AAAUSD")]
    before = [o.symbol for o in opps]
    r.rank(opps)
    assert [o.symbol for o in opps] == before


def test_rank_of_an_empty_list():
    assert OpportunityRanker().rank([]) == []
