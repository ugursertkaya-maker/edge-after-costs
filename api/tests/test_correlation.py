"""Correlation: the test that matters most.

Eight simultaneous crypto longs are one large crypto long.  A book that
aggregates its risk by *counting positions* reports one eighth of the truth;
one that aggregates by naive summation reports the truth only when everything
moves together and over-reserves badly when it does not.  The quadratic form
``sqrt(w' R w)`` is the only aggregation that gets both ends right, and the two
limiting cases below are the whole reason this module exists.

Return series are built from Hadamard rows so the correlations are exact rather
than approximately-zero-if-you-squint: any two distinct rows of a Hadamard
matrix are orthogonal, so their Pearson correlation is 0 to floating point.
"""

from __future__ import annotations

import math

import pytest

from app.portfolio import CorrelationEngine, CorrelationView
from app.types import Side

from fixtures.market import make_bars
from test_portfolio import (
    make_engine,
    make_position,
    settings_with,
    symbols,
)


# --------------------------------------------------------------------------- #
# Deterministic return series
# --------------------------------------------------------------------------- #


def hadamard(n: int) -> list[list[int]]:
    """Sylvester construction.  Rows 1.. are zero-mean and mutually orthogonal."""
    h = [[1]]
    while len(h) < n:
        h = [row + row for row in h] + [row + [-x for x in row] for row in h]
    return h


H = hadamard(16)


def closes_from(signs: list[int], *, start: float = 100.0, step: float = 0.001) -> list[float]:
    """A close path whose bar-to-bar returns are exactly ``signs * step``."""
    out = [start]
    for s in signs:
        out.append(out[-1] * (1.0 + s * step))
    return out


def bars_for(symbol: str, signs: list[int], *, step: float = 0.001):
    return make_bars(closes_from(signs, step=step), symbol=symbol)


def corr_engine(**portfolio) -> CorrelationEngine:
    return CorrelationEngine(settings=settings_with(portfolio or None))


# --------------------------------------------------------------------------- #
# Measurement
# --------------------------------------------------------------------------- #


def test_identical_series_correlate_perfectly() -> None:
    eng = corr_engine()
    view = eng.update({"BTCUSD": bars_for("BTCUSD", H[1]), "ETHUSD": bars_for("ETHUSD", H[1])})
    assert view.rho("BTCUSD", "ETHUSD") == pytest.approx(1.0, abs=1e-9)
    assert view.rho("ETHUSD", "BTCUSD") == pytest.approx(1.0, abs=1e-9)  # key order free
    assert view.rho("BTCUSD", "BTCUSD") == 1.0


def test_opposite_series_correlate_minus_one() -> None:
    eng = corr_engine()
    inverted = [-x for x in H[1]]
    view = eng.update({"BTCUSD": bars_for("BTCUSD", H[1]), "ETHUSD": bars_for("ETHUSD", inverted)})
    assert view.rho("BTCUSD", "ETHUSD") == pytest.approx(-1.0, abs=1e-6)


def test_orthogonal_series_are_uncorrelated() -> None:
    eng = corr_engine()
    view = eng.update(
        {"BTCUSD": bars_for("BTCUSD", H[1]), "ETHUSD": bars_for("ETHUSD", H[2])}
    )
    assert view.rho("BTCUSD", "ETHUSD") == pytest.approx(0.0, abs=1e-6)


def test_correlation_is_measured_over_the_configured_window_only() -> None:
    """A pair that used to move together and no longer does reports the present."""
    long_ago = H[1] * 6          # 96 bars moving in lockstep
    lately = H[2] * 2            # 32 bars orthogonal to H[1]
    a = H[1] * 6 + H[1] * 2
    b = long_ago + lately
    assert len(a) == len(b)

    recent = CorrelationEngine(settings=settings_with({"correlation_window_bars": 16}))
    view = recent.update({"BTCUSD": bars_for("BTCUSD", a), "ETHUSD": bars_for("ETHUSD", b)})
    assert view.rho("BTCUSD", "ETHUSD") == pytest.approx(0.0, abs=1e-6)

    everything = CorrelationEngine(settings=settings_with({"correlation_window_bars": 200}))
    view2 = everything.update(
        {"BTCUSD": bars_for("BTCUSD", a), "ETHUSD": bars_for("ETHUSD", b)}
    )
    assert view2.rho("BTCUSD", "ETHUSD") > 0.7


def test_a_series_too_short_to_measure_is_left_unknown() -> None:
    eng = corr_engine()
    view = eng.update(
        {"BTCUSD": bars_for("BTCUSD", H[1]), "ETHUSD": make_bars([100.0, 100.5], symbol="ETHUSD")}
    )
    assert view.known("BTCUSD")
    assert not view.known("ETHUSD")


def test_a_flat_series_has_no_correlation_to_measure() -> None:
    eng = corr_engine()
    view = eng.update(
        {"BTCUSD": bars_for("BTCUSD", H[1]), "ETHUSD": make_bars([100.0] * 17, symbol="ETHUSD")}
    )
    assert view.rho("BTCUSD", "ETHUSD") == 0.0


# --------------------------------------------------------------------------- #
# Aggregation — the point of the whole module
# --------------------------------------------------------------------------- #


def perfectly_correlated_view(names: list[str]) -> CorrelationView:
    eng = corr_engine()
    return eng.update({n: bars_for(n, H[1]) for n in names})


def orthogonal_view(names: list[str]) -> CorrelationView:
    eng = corr_engine()
    return eng.update({n: bars_for(n, H[i + 1]) for i, n in enumerate(names)})


def test_eight_perfectly_correlated_longs_are_one_large_long() -> None:
    """$1 of risk eight times over, all moving together, is $8 — not $1.

    This is the failure mode "many small positions" is designed to hide: a
    per-position risk cap sees eight compliant $1 risks; the portfolio sees one
    $8 position.  Anything that reports one eighth of the truth here would let
    the book carry eight times its stated risk.
    """
    eng = corr_engine()
    names = symbols(8)
    view = perfectly_correlated_view(names)
    positions = [make_position(f"p{i}", n, notional=20.0, risk=1.0) for i, n in enumerate(names)]

    aggregated = eng.correlated_risk(positions, view)

    assert aggregated == pytest.approx(8.0, abs=1e-6)
    naive_sum = sum(p.risk_usd for p in positions)
    assert aggregated == pytest.approx(naive_sum, abs=1e-6)
    # The failure we are guarding against, stated explicitly:
    assert aggregated > naive_sum / 8.0 * 7.0
    assert aggregated != pytest.approx(1.0)


def test_eight_uncorrelated_positions_add_in_quadrature() -> None:
    eng = corr_engine()
    names = symbols(8)
    view = orthogonal_view(names)
    positions = [make_position(f"p{i}", n, notional=20.0, risk=1.0) for i, n in enumerate(names)]

    aggregated = eng.correlated_risk(positions, view)

    assert aggregated == pytest.approx(math.sqrt(8.0), abs=1e-5)
    assert aggregated == pytest.approx(2.8284, abs=1e-3)
    # Strictly between "one position" and "one enormous position".
    assert 1.0 < aggregated < 8.0


def test_a_mixed_book_lands_between_the_two_extremes() -> None:
    """Four that move together plus four that do not: sqrt(4^2 + 4) = 4.47."""
    eng = corr_engine()
    cluster = symbols(4)
    independents = [f"I{i}USD" for i in range(4)]
    series = {n: bars_for(n, H[1]) for n in cluster}
    series.update({n: bars_for(n, H[i + 2]) for i, n in enumerate(independents)})
    view = eng.update(series)

    names = cluster + independents
    positions = [make_position(f"p{i}", n, notional=20.0, risk=1.0) for i, n in enumerate(names)]
    aggregated = eng.correlated_risk(positions, view)

    assert aggregated == pytest.approx(math.sqrt(20.0), abs=1e-4)
    assert math.sqrt(8.0) < aggregated < 8.0


def test_perfectly_negatively_correlated_risks_cancel_but_never_go_negative() -> None:
    eng = corr_engine()
    view = eng.update(
        {
            "BTCUSD": bars_for("BTCUSD", H[1]),
            "ETHUSD": bars_for("ETHUSD", [-x for x in H[1]]),
        }
    )
    positions = [
        make_position("a", "BTCUSD", notional=20.0, risk=1.0),
        make_position("b", "ETHUSD", notional=20.0, risk=1.0),
    ]
    aggregated = eng.correlated_risk(positions, view)
    assert aggregated >= 0.0
    assert aggregated == pytest.approx(0.0, abs=1e-3)


def test_correlated_risk_of_an_empty_book_is_zero() -> None:
    assert corr_engine().correlated_risk([], None) == 0.0


def test_with_no_view_at_all_everything_is_assumed_to_move_together() -> None:
    """Unmeasured is not uncorrelated.  Missing data assumes the worst."""
    eng = corr_engine()
    names = symbols(8)
    positions = [make_position(f"p{i}", n, notional=20.0, risk=1.0) for i, n in enumerate(names)]
    assert eng.correlated_risk(positions, None) == pytest.approx(8.0)


def test_a_symbol_missing_from_the_view_is_assumed_to_move_together() -> None:
    eng = corr_engine()
    names = symbols(4)
    view = orthogonal_view(names)          # these four are independent...
    positions = [make_position(f"p{i}", n, notional=20.0, risk=1.0) for i, n in enumerate(names)]
    positions.append(make_position("x", "NEWCOINUSD", notional=20.0, risk=1.0))

    aggregated = eng.correlated_risk(positions, view)
    # ...but the newcomer, which we have no history for, is treated as moving
    # with all of them: 4 + 2*4 + 1 = 13 under the quadratic form.
    assert aggregated == pytest.approx(math.sqrt(13.0), abs=1e-4)
    assert aggregated > math.sqrt(5.0)     # strictly worse than "just add one"


def test_aggregate_prices_a_candidate_before_the_position_exists() -> None:
    eng = corr_engine()
    names = symbols(3)
    view = perfectly_correlated_view(names + ["NEWUSD"])
    assert eng.aggregate(names + ["NEWUSD"], [1.0, 1.0, 1.0, 1.0], view) == pytest.approx(4.0)
    assert eng.aggregate([], [], view) == 0.0


# --------------------------------------------------------------------------- #
# Clustering
# --------------------------------------------------------------------------- #


def test_cluster_groups_symbols_that_move_together() -> None:
    eng = corr_engine()
    series = {n: bars_for(n, H[1]) for n in ("AAAUSD", "BBBUSD")}
    series.update({n: bars_for(n, H[2]) for n in ("CCCUSD", "DDDUSD")})
    view = eng.update(series)

    clusters = view.cluster(["AAAUSD", "BBBUSD", "CCCUSD", "DDDUSD"], 0.65)
    assert clusters == [["AAAUSD", "BBBUSD"], ["CCCUSD", "DDDUSD"]]


def test_cluster_is_single_linkage() -> None:
    """A moves with B, B moves with C: a stop-out cascade reaches C."""
    view = CorrelationView(
        correlations={("A", "B"): 0.9, ("B", "C"): 0.9, ("A", "C"): 0.1},
        btc_beta={"A": 1.0, "B": 1.0, "C": 1.0},
    )
    assert view.cluster(["A", "B", "C"], 0.65) == [["A", "B", "C"]]


def test_cluster_output_is_deterministic_and_handles_singletons() -> None:
    view = CorrelationView(
        correlations={("A", "B"): 0.9},
        btc_beta={"A": 1.0, "B": 1.0, "C": 1.0},
    )
    assert view.cluster(["C", "A", "B", "A"], 0.65) == [["C"], ["A", "B"]]
    assert view.cluster([], 0.65) == []


def test_a_strong_negative_correlation_still_clusters() -> None:
    """Perfectly inverse moves are still one bet, just pointing both ways."""
    view = CorrelationView(correlations={("A", "B"): -0.95}, btc_beta={"A": 1.0, "B": 1.0})
    assert view.cluster(["A", "B"], 0.65) == [["A", "B"]]


# --------------------------------------------------------------------------- #
# Beta to BTC and directional exposure
# --------------------------------------------------------------------------- #


def test_btc_beta_measures_amplitude_not_just_direction() -> None:
    eng = corr_engine()
    double = [x for x in H[1]]
    view = eng.update(
        {
            "BTCUSD": bars_for("BTCUSD", H[1]),
            "ALTUSD": bars_for("ALTUSD", double, step=0.002),   # twice the move
            "HALFUSD": bars_for("HALFUSD", double, step=0.0005),
        }
    )
    assert view.beta("BTCUSD") == pytest.approx(1.0)
    assert view.beta("ALTUSD") == pytest.approx(2.0, abs=1e-3)
    assert view.beta("HALFUSD") == pytest.approx(0.5, abs=1e-3)
    assert view.beta("UNMEASUREDUSD") == 1.0   # assume it moves with the market


def test_directional_exposure_is_beta_adjusted() -> None:
    eng = corr_engine()
    view = eng.update(
        {
            "BTCUSD": bars_for("BTCUSD", H[1]),
            "ALTUSD": bars_for("ALTUSD", H[1], step=0.002),
        }
    )
    positions = [
        make_position("a", "BTCUSD", notional=100.0, risk=1.0),
        make_position("b", "ALTUSD", notional=100.0, risk=1.0),
    ]
    # $200 of notional, but $300 of market exposure: the 2-beta alt counts twice.
    assert eng.directional_exposure_pct(positions, 500.0, view) == pytest.approx(60.0, abs=0.1)
    # Unadjusted, the same book would look like 40%.
    assert eng.directional_exposure_pct(positions, 500.0, None) == pytest.approx(40.0)


def test_directional_exposure_nets_longs_against_shorts() -> None:
    eng = corr_engine()
    positions = [
        make_position("a", "BTCUSD", notional=100.0, risk=1.0, side=Side.LONG),
        make_position("b", "ETHUSD", notional=60.0, risk=1.0, side=Side.SHORT),
    ]
    assert eng.directional_exposure_pct(positions, 500.0, None) == pytest.approx(8.0)


def test_a_net_short_book_is_just_as_directional() -> None:
    eng = corr_engine()
    shorts = [
        make_position(f"p{i}", n, notional=50.0, risk=1.0, side=Side.SHORT)
        for i, n in enumerate(symbols(4))
    ]
    assert eng.directional_exposure_pct(shorts, 500.0, None) == pytest.approx(40.0)


def test_directional_exposure_of_an_empty_or_broke_book_is_zero() -> None:
    eng = corr_engine()
    assert eng.directional_exposure_pct([], 500.0, None) == 0.0
    assert eng.directional_exposure_pct(
        [make_position("a", "BTCUSD", notional=50.0, risk=1.0)], 0.0, None
    ) == 0.0


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def test_portfolio_state_carries_the_measured_view_through() -> None:
    from test_portfolio import make_state

    eng = make_engine({"total_open_risk_pct": 100.0})
    names = symbols(8)
    bars = {n: bars_for(n, H[i + 1]) for i, n in enumerate(names)}
    positions = [make_position(f"p{i}", n, notional=20.0, risk=1.0) for i, n in enumerate(names)]

    st = eng.state.__self__ and make_state(  # noqa: B018 - readability only
        eng, cash=340.0, positions=positions, equity=500.0, bars=bars
    )
    # Measured as independent, so the book aggregates in quadrature instead of
    # the conservative 8.0 the no-data path would have produced.
    assert st.correlated_risk_usd == pytest.approx(math.sqrt(8.0), abs=1e-5)
    assert eng.view is not None
