"""Tests for the ten Phase 3 strategy families.

Deterministic and offline: no network, no clock, no RNG.  Every scenario is a
hand-built feature block using ``features_mtf``'s published names and units,
with numbers taken from the measured venue (1m ATR 2.2 bps), so a threshold
that looks reasonable here is reasonable there.

What is asserted, family by family:

* it fires on a setup built for its own hypothesis, **on both sides**, and the
  two sides score identically — an asymmetric score would mean the family is
  really a long-only proxy for drift;
* it is silent on flat data, at cold start, and — for the tape families — on an
  empty tape;
* ``score`` is graded: a weaker version of the same setup scores strictly
  lower, and the strong one does not saturate at 1.0;
* **every parameter changes behaviour.**  A parameter that does nothing is a
  bug that multiplies the search space for free, and with 444 combinations in
  play that is the difference between a search and a lottery;
* nothing returns NaN or infinity, and nothing raises on a missing feature.

Plus the cross-module checks: every name a family reads exists in
``FEATURE_NAMES``, and the interval set matches the engine's default.
"""

from __future__ import annotations

import math

import pytest

from app.research.families import (
    FAMILIES,
    FAMILY_BY_NAME,
    INTERVALS,
    REQUIRED_FEATURES,
    ParamSpace,
    ResearchSignal,
    feature_names_used,
    total_combinations,
    validate_feature_names,
)
from app.types import Side

# Measured on Binance.US BTC/USD (docs/ECONOMICS.md): 1m ATR 2.2 bps.  The 5m
# and 15m ATRs are the diffusion-scaled equivalents, which is what the venue
# actually does (2.2 * sqrt(60) = 17 bps in an hour).
ATR_1M = 2.2
ATR_5M = ATR_1M * math.sqrt(5.0)
ATR_15M = ATR_1M * math.sqrt(15.0)
ATRS = {"atr_1m": ATR_1M, "atr_5m": ATR_5M, "atr_15m": ATR_15M}

PRICE = 50_000.0
TS = 1_700_000_000_000
SYMBOL = "BTC/USD"

EXPECTED_FAMILY_NAMES = (
    "momentum_continuation",
    "breakout",
    "trend_pullback",
    "mean_reversion",
    "volume_impulse",
    "orderflow_imbalance",
    "vwap_reversion",
    "volatility_expansion",
    "short_term_reversal",
    "hybrid_multifactor",
)

#: Pinned so the number quoted in the report is the number the code produces.
EXPECTED_TOTAL_COMBINATIONS = 444

#: Families that read the trade tape and must refuse when it is empty.
TAPE_FAMILIES = ("volume_impulse", "orderflow_imbalance")


# --------------------------------------------------------------------------- #
# Feature blocks
# --------------------------------------------------------------------------- #


def make_features(values: dict, *, price: float = PRICE, ts_ms: int = TS):
    """Build a real :class:`MTFFeatures` from a literal dict of values."""
    from app.research.features_mtf import MTFFeatures

    return MTFFeatures(symbol=SYMBOL, ts_ms=ts_ms, price=price, values=dict(values))


#: Bases whose sign flips when a scenario is mirrored through the mid.
_SIGNED_BASES = (
    "ret", "ret5", "mom_slope", "accel", "ema_slope",
    "zscore", "vwap_dist", "breakout_dist", "aggr_imb",
)
#: Bases that are reflected rather than negated.
_REFLECTED = {"rsi": lambda v: 100.0 - v, "range_pos": lambda v: 1.0 - v}


def mirror(values: dict) -> dict:
    """Reflect a scenario: a long setup becomes its short twin."""
    out = {}
    for key, value in values.items():
        base = key.rsplit("_", 1)[0]
        if base in _REFLECTED:
            out[key] = _REFLECTED[base](value)
        elif base in _SIGNED_BASES:
            out[key] = -value
        else:
            out[key] = value
    return out


def _v(**kw) -> dict:
    """A feature block seeded with the three ATRs every family needs."""
    out = dict(ATRS)
    out.update(kw)
    return out


# --------------------------------------------------------------------------- #
# Scenarios — one strong and one weak setup per family
# --------------------------------------------------------------------------- #

SCENARIOS: dict[str, dict] = {
    # A — a 6 bps minute (2.7 ATR) inside an agreeing five-bar leg, with the
    # momentum slope still steepening.
    "momentum_continuation": {
        "params": {"interval": "1m", "min_impulse_atr": 1.0, "min_accel_atr": 0.1},
        "strong": _v(ret_1m=6.0, ret5_1m=10.0, accel_1m=1.0,
                     ema_slope_1m=0.5, rsi_1m=65.0,
                     ret_5m=2.0, ret5_5m=6.0, accel_5m=0.2,
                     ema_slope_5m=0.3, rsi_5m=58.0,
                     ret_15m=1.0, ret5_15m=3.0, accel_15m=0.1,
                     ema_slope_15m=0.2, rsi_15m=54.0),
        "weak": _v(ret_1m=3.0, ret5_1m=5.0, accel_1m=0.3,
                   ema_slope_1m=0.2, rsi_1m=55.0,
                   ret_5m=2.0, ret5_5m=6.0, accel_5m=0.2,
                   ema_slope_5m=0.3, rsi_5m=58.0,
                   ret_15m=1.0, ret5_15m=3.0, accel_15m=0.1,
                   ema_slope_15m=0.2, rsi_15m=54.0),
    },
    # B — 5 bps outside a twenty-bar range whose realized vol sits in the tenth
    # percentile: a coil that broke.
    "breakout": {
        "params": {"interval": "1m", "max_rvol_pct": 0.3, "break_atr": 1.0},
        "strong": _v(breakout_dist_1m=5.0, rvol_pct_1m=0.1, aggr_imb_1m=0.4,
                     breakout_dist_5m=0.0, rvol_pct_5m=0.2, aggr_imb_5m=0.3,
                     breakout_dist_15m=0.0, rvol_pct_15m=0.2, aggr_imb_15m=0.2),
        "weak": _v(breakout_dist_1m=2.8, rvol_pct_1m=0.25, aggr_imb_1m=0.1,
                   breakout_dist_5m=0.0, rvol_pct_5m=0.2, aggr_imb_5m=0.3,
                   breakout_dist_15m=0.0, rvol_pct_15m=0.2, aggr_imb_15m=0.2),
    },
    # C — an uptrend (EMA climbing, RSI 68) whose newest bar gave back 1.6 ATR
    # while the five-bar leg stayed positive.
    "trend_pullback": {
        "params": {"interval": "1m", "trend_min": 0.45, "pullback_atr": 1.0},
        "strong": _v(ema_slope_1m=0.6, rsi_1m=68.0, ret_1m=-3.5, ret5_1m=8.0,
                     ema_slope_5m=0.0, rsi_5m=50.0, ret_5m=0.0, ret5_5m=0.0,
                     ema_slope_15m=0.0, rsi_15m=50.0, ret_15m=0.0, ret5_15m=0.0),
        "weak": _v(ema_slope_1m=0.3, rsi_1m=62.0, ret_1m=-2.4, ret5_1m=4.0,
                   ema_slope_5m=0.0, rsi_5m=50.0, ret_5m=0.0, ret5_5m=0.0,
                   ema_slope_15m=0.0, rsi_15m=50.0, ret_15m=0.0, ret5_15m=0.0),
    },
    # D — 2.8 sigma above the twenty-bar mean, no trend, flow already turning.
    "mean_reversion": {
        "params": {"interval": "1m", "extension_sigma": 2.0, "max_trend": 0.6},
        "strong": _v(zscore_1m=2.8, ema_slope_1m=0.1, rsi_1m=52.0,
                     aggr_imb_1m=-0.2, aggr_imb_5m=0.1,
                     zscore_5m=0.5, ema_slope_5m=0.1, rsi_5m=52.0,
                     zscore_15m=0.2, ema_slope_15m=0.1, rsi_15m=51.0,
                     aggr_imb_15m=0.0),
        "weak": _v(zscore_1m=2.2, ema_slope_1m=0.2, rsi_1m=56.0,
                   aggr_imb_1m=-0.05, aggr_imb_5m=0.05,
                   zscore_5m=0.5, ema_slope_5m=0.1, rsi_5m=52.0,
                   zscore_15m=0.2, ema_slope_15m=0.1, rsi_15m=51.0,
                   aggr_imb_15m=0.0),
    },
    # E — twelve prints in a minute on a venue that averages 2.19, volume at
    # five times its baseline, and the price actually moved.
    "volume_impulse": {
        "params": {"min_trades": 5, "min_vol_accel": 2.5, "response_atr": 1.0},
        "strong": _v(trades_1m=12.0, vol_accel_1m=5.0, ret_1m=5.0),
        "weak": _v(trades_1m=8.0, vol_accel_1m=3.0, ret_1m=3.0),
    },
    # F — thirty prints with three quarters of the notional on one side.
    "orderflow_imbalance": {
        "params": {"interval": "1m", "imb_min": 0.4, "min_trades": 10},
        "strong": _v(trades_1m=30.0, aggr_imb_1m=0.75, aggr_imb_5m=0.45,
                     trades_5m=60.0, trades_15m=90.0, aggr_imb_15m=0.2),
        "weak": _v(trades_1m=15.0, aggr_imb_1m=0.5, aggr_imb_5m=0.4,
                   trades_5m=60.0, trades_15m=90.0, aggr_imb_15m=0.2),
    },
    # G — 15 bps above the rolling (sixty-bar = one hour) VWAP, which is nearly
    # a full typical hour's excursion, with no trend to justify it.
    "vwap_reversion": {
        "params": {"interval": "1m", "dist_atr": 4.0, "max_trend": 0.5},
        "strong": _v(vwap_dist_1m=15.0, ema_slope_1m=0.1, rsi_1m=52.0,
                     aggr_imb_1m=-0.2, aggr_imb_5m=0.1,
                     vwap_dist_5m=2.0, ema_slope_5m=0.1, rsi_5m=52.0,
                     vwap_dist_15m=1.0, ema_slope_15m=0.1, rsi_15m=51.0,
                     aggr_imb_15m=0.0),
        "weak": _v(vwap_dist_1m=10.0, ema_slope_1m=0.2, rsi_1m=56.0,
                   aggr_imb_1m=-0.05, aggr_imb_5m=0.05,
                   vwap_dist_5m=2.0, ema_slope_5m=0.1, rsi_5m=52.0,
                   vwap_dist_15m=1.0, ema_slope_15m=0.1, rsi_15m=51.0,
                   aggr_imb_15m=0.0),
    },
    # H — realized vol at the 95th percentile of its own trailing window, with
    # a 2.3 ATR bar to point the way.
    "volatility_expansion": {
        "params": {"interval": "1m", "min_rvol_pct": 0.75, "direction_atr": 1.0},
        "strong": _v(rvol_pct_1m=0.95, ret_1m=5.0, vol_accel_1m=3.0,
                     rvol_pct_5m=0.5, ret_5m=2.0, vol_accel_5m=1.0,
                     rvol_pct_15m=0.5, ret_15m=1.0, vol_accel_15m=1.0),
        "weak": _v(rvol_pct_1m=0.8, ret_1m=3.0, vol_accel_1m=1.5,
                   rvol_pct_5m=0.5, ret_5m=2.0, vol_accel_5m=1.0,
                   rvol_pct_15m=0.5, ret_15m=1.0, vol_accel_15m=1.0),
    },
    # I — a five-bar run four times its diffusion expectation, with the newest
    # bar already giving some of it back.
    "short_term_reversal": {
        "params": {"interval": "1m", "ext_atr": 2.5, "stall_frac": 0.25},
        "strong": _v(ret5_1m=20.0, ret_1m=-1.5, ema_slope_1m=0.1, rsi_1m=52.0,
                     ret5_5m=2.0, ret_5m=0.0, ema_slope_5m=0.1, rsi_5m=52.0,
                     ret5_15m=1.0, ret_15m=0.0, ema_slope_15m=0.1, rsi_15m=51.0),
        "weak": _v(ret5_1m=14.0, ret_1m=-0.8, ema_slope_1m=0.2, rsi_1m=56.0,
                   ret5_5m=2.0, ret_5m=0.0, ema_slope_5m=0.1, rsi_5m=52.0,
                   ret5_15m=1.0, ret_15m=0.0, ema_slope_15m=0.1, rsi_15m=51.0),
    },
    # J — a positive five-bar leg, price below the mean, and buy-side flow:
    # all three legs agree, which is the only way a blend gets full marks.
    "hybrid_multifactor": {
        "params": {"momentum_tilt": 0.5, "min_conviction": 0.3, "flow_weight": 0.25},
        "strong": _v(ret5_1m=12.0, zscore_1m=-2.0, aggr_imb_1m=0.6),
        "weak": _v(ret5_1m=7.0, zscore_1m=-1.2, aggr_imb_1m=0.3),
    },
}


def scenario(name: str) -> dict:
    return SCENARIOS[name]


def fire(name: str, values: dict, params: dict | None = None) -> ResearchSignal | None:
    family = FAMILY_BY_NAME[name]
    return family.signal(make_features(values), params or scenario(name)["params"])


# --------------------------------------------------------------------------- #
# Registry and grids
# --------------------------------------------------------------------------- #


def test_families_registry_has_exactly_ten_unique_names():
    assert len(FAMILIES) == 10
    names = [f.name for f in FAMILIES]
    assert len(set(names)) == 10
    assert tuple(names) == EXPECTED_FAMILY_NAMES


def test_every_family_has_a_scenario():
    assert set(SCENARIOS) == set(EXPECTED_FAMILY_NAMES)


def test_param_space_names_match_family_names():
    for family in FAMILIES:
        assert family.param_space.name == family.name


def test_allowed_regimes_is_none_unless_definitionally_inapplicable():
    # Which family belongs in which regime is what the tournament measures.
    # Hardcoding it here would be answering that question with an assumption.
    for family in FAMILIES:
        assert family.allowed_regimes is None


def test_grids_stay_small_and_defensible():
    for family in FAMILIES:
        grid = family.param_space.grid
        assert 1 <= len(grid) <= 3, f"{family.name} has {len(grid)} parameters"
        for param, values in grid.items():
            assert isinstance(values, tuple), f"{family.name}.{param} must be a tuple"
            assert 3 <= len(values) <= 5, f"{family.name}.{param} has {len(values)}"
            assert len(set(values)) == len(values), f"{family.name}.{param} repeats"
        assert family.param_space.size() <= 100, family.name


def test_total_combination_count_is_measured_and_reported():
    """The search-space size the report quotes, counted two independent ways."""
    per_family = {f.name: f.param_space.size() for f in FAMILIES}
    enumerated = {f.name: len(list(f.param_space.combinations())) for f in FAMILIES}
    assert per_family == enumerated

    total = total_combinations()
    assert total == sum(per_family.values())
    breakdown = ", ".join(f"{k}={v}" for k, v in per_family.items())
    print(f"\nparameter combinations: {breakdown}\nTOTAL = {total}")
    assert total == EXPECTED_TOTAL_COMBINATIONS, (
        f"search space changed: {total} combinations ({breakdown})"
    )


def test_combinations_are_deterministic_complete_and_unique():
    for family in FAMILIES:
        space = family.param_space
        first = list(space.combinations())
        second = list(space.combinations())
        assert first == second                       # deterministic order
        assert len(first) == space.size()
        assert len({tuple(sorted(c.items())) for c in first}) == len(first)
        for combo in first:
            assert set(combo) == set(space.grid)
            for key, value in combo.items():
                assert value in space.grid[key]


def test_param_space_defaults_are_a_valid_combination():
    for family in FAMILIES:
        defaults = family.param_space.defaults()
        assert set(defaults) == set(family.param_space.grid)
        assert defaults in list(family.param_space.combinations())


def test_the_horizon_is_a_search_dimension_not_a_constant():
    """Both prior reports concluded the horizon is the lever, so most families
    must actually search over it."""
    with_interval = [f.name for f in FAMILIES if "interval" in f.param_space.grid]
    assert len(with_interval) >= 8
    for family in FAMILIES:
        values = family.param_space.grid.get("interval")
        if values is not None:
            assert values == INTERVALS


# --------------------------------------------------------------------------- #
# Firing, both sides
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", EXPECTED_FAMILY_NAMES)
def test_family_fires_on_its_own_setup(name):
    sig = fire(name, scenario(name)["strong"])
    assert sig is not None, f"{name} did not fire on its own strong setup"
    assert sig.family == name
    assert sig.symbol == SYMBOL
    assert sig.ts_ms == TS
    assert sig.reasons


@pytest.mark.parametrize("name", EXPECTED_FAMILY_NAMES)
def test_family_is_symmetric_across_sides(name):
    sc = scenario(name)
    long_sig = fire(name, sc["strong"])
    short_sig = fire(name, mirror(sc["strong"]))
    assert long_sig is not None and short_sig is not None
    assert {long_sig.side, short_sig.side} == {Side.LONG, Side.SHORT}
    # A mirrored input must produce a mirrored opinion of identical conviction.
    # Spot is long-only and the risk layer gates that, but an asymmetry here
    # would mean the family is really a long-only proxy for drift.
    assert long_sig.score == pytest.approx(short_sig.score, abs=1e-12)


# --------------------------------------------------------------------------- #
# Grading
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", EXPECTED_FAMILY_NAMES)
def test_score_is_graded_not_binary(name):
    sc = scenario(name)
    strong = fire(name, sc["strong"])
    weak = fire(name, sc["weak"])
    assert strong is not None and weak is not None, f"{name} weak setup went silent"
    assert weak.score < strong.score, f"{name} is not graded"
    assert weak.side is strong.side
    for sig in (strong, weak):
        assert math.isfinite(sig.score)
        assert 0.0 < sig.score <= 1.0
    # Not pinned at the top either: a family that answers 1.0 is binary again,
    # and the tournament has nothing left to threshold on.
    assert strong.score < 1.0


@pytest.mark.parametrize("name", EXPECTED_FAMILY_NAMES)
def test_score_stays_inside_the_unit_interval_across_the_whole_grid(name):
    family = FAMILY_BY_NAME[name]
    sc = scenario(name)
    seen = 0
    for values in (sc["strong"], sc["weak"], mirror(sc["strong"])):
        features = make_features(values)
        for params in family.param_space.combinations():
            sig = family.signal(features, params)
            if sig is None:
                continue
            seen += 1
            assert math.isfinite(sig.score)
            assert 0.0 < sig.score <= 1.0
            assert sig.side in (Side.LONG, Side.SHORT)
    assert seen > 0, f"{name} never fired anywhere in its grid"


# --------------------------------------------------------------------------- #
# Every parameter earns its place in the search space
# --------------------------------------------------------------------------- #


def _outcome(family, features, params):
    sig = family.signal(features, params)
    return None if sig is None else (sig.side, round(sig.score, 9))


@pytest.mark.parametrize("name", EXPECTED_FAMILY_NAMES)
def test_every_parameter_changes_behaviour(name):
    """A parameter that does nothing is a bug: it multiplies the search space
    without adding a hypothesis, and inflates the multiple-testing correction
    the report has to apply to whatever comes out on top."""
    family = FAMILY_BY_NAME[name]
    sc = scenario(name)
    features = make_features(sc["strong"])
    base = dict(sc["params"])

    for param, candidates in family.param_space.grid.items():
        outcomes = set()
        for value in candidates:
            outcomes.add(_outcome(family, features, {**base, param: value}))
        assert len(outcomes) > 1, (
            f"{name}.{param} has no effect on its own scenario "
            f"({len(candidates)} values, one outcome: {outcomes})"
        )


# --------------------------------------------------------------------------- #
# Silence: flat, cold, empty tape, missing features
# --------------------------------------------------------------------------- #

#: A dead market, expressed in the engine's own neutral values: rvol_pct and
#: vol_pct 0.5, vol_accel 1.0, rsi 50, everything signed at 0.0.  Two prints a
#: minute is this venue's actual average.
FLAT = dict(ATRS)
for _iv in INTERVALS:
    FLAT.update({
        f"ret_{_iv}": 0.0, f"ret5_{_iv}": 0.0, f"accel_{_iv}": 0.0,
        f"mom_slope_{_iv}": 0.0, f"ema_slope_{_iv}": 0.0, f"rsi_{_iv}": 50.0,
        f"zscore_{_iv}": 0.0, f"vwap_dist_{_iv}": 0.0, f"breakout_dist_{_iv}": 0.0,
        f"rv_{_iv}": 1.0, f"rvol_pct_{_iv}": 0.5, f"vol_pct_{_iv}": 0.5,
        f"vol_accel_{_iv}": 1.0, f"trades_{_iv}": 2.0, f"aggr_imb_{_iv}": 0.0,
        f"range_pos_{_iv}": 0.5,
    })


@pytest.mark.parametrize("name", EXPECTED_FAMILY_NAMES)
def test_silent_on_flat_data_across_the_whole_grid(name):
    family = FAMILY_BY_NAME[name]
    features = make_features(FLAT)
    for params in family.param_space.combinations():
        assert family.signal(features, params) is None, f"{name} fired on flat data"


#: Families whose entry gate includes a statistic the engine reports with an
#: informative-looking neutral value (``vol_accel`` 1.0, ``rvol_pct`` 0.5) or
#: with a tape reading.  These are the ones that could fire on cold start if a
#: threshold were set on the wrong side of the neutral, so they get their own
#: assertion.
NEUTRAL_GATED_FAMILIES = (
    "breakout", "volume_impulse", "orderflow_imbalance", "volatility_expansion",
)


def _loud_but_neutral() -> dict:
    """Every *signed* feature screaming, every *neutral* statistic unmeasured."""
    loud = dict(FLAT)
    for iv in INTERVALS:
        loud.update({
            f"ret_{iv}": 40.0, f"ret5_{iv}": 90.0, f"accel_{iv}": 5.0,
            f"zscore_{iv}": 9.0, f"vwap_dist_{iv}": 90.0,
            f"breakout_dist_{iv}": 40.0, f"aggr_imb_{iv}": 0.9,
        })
    return loud


@pytest.mark.parametrize("name", NEUTRAL_GATED_FAMILIES)
def test_no_gate_can_be_satisfied_by_a_neutral_reading(name):
    """The engine fills what it cannot measure with documented neutral values —
    ``rvol_pct`` and ``vol_pct`` 0.5, ``vol_accel`` 1.0, ``rsi`` 50 — rather
    than with NaN.  A gate set on the wrong side of one of those fires on cold
    start and measures nothing, which looks exactly like a working strategy.
    So these four must stay silent here no matter how loud the signed features
    get: B's compression ceiling never reaches 0.5, H's expansion floor never
    drops to it, E's volume floor stays above 1.0, and E/F both need prints."""
    family = FAMILY_BY_NAME[name]
    features = make_features(_loud_but_neutral())
    for params in family.param_space.combinations():
        assert family.signal(features, params) is None, (
            f"{name} fired on a neutral volume/volatility reading with {params}"
        )


def test_the_loud_but_neutral_block_is_not_simply_dead():
    """Control for the test above: the block does drive the families that read
    signed features only, so the silence of the other four is a gate working
    and not a feature block that says nothing."""
    features = make_features(_loud_but_neutral())
    fired = {
        family.name
        for family in FAMILIES
        for params in family.param_space.combinations()
        if family.signal(features, params) is not None
    }
    assert fired, "the loud block drives nothing at all"
    assert not fired.intersection(NEUTRAL_GATED_FAMILIES)


@pytest.mark.parametrize("name", EXPECTED_FAMILY_NAMES)
def test_silent_at_cold_start(name):
    """No ATR yet (Wilder needs fourteen bars), or no price: no opinion."""
    family = FAMILY_BY_NAME[name]
    sc = scenario(name)
    no_atr = {k: v for k, v in sc["strong"].items() if not k.startswith("atr_")}
    zero_atr = {**sc["strong"], **{k: 0.0 for k in ATRS}}
    cold = [
        make_features(no_atr),
        make_features(zero_atr),
        make_features(sc["strong"], price=0.0),
        make_features({}),
    ]
    for features in cold:
        for params in family.param_space.combinations():
            assert family.signal(features, params) is None, f"{name} fired cold"


@pytest.mark.parametrize("name", TAPE_FAMILIES)
def test_tape_families_are_silent_on_an_empty_tape(name):
    """55% of this venue's minutes contain zero trades.  ``aggr_imb`` reads 0.0
    both for balanced flow and for no flow at all, so without a trade-count
    gate a family would be reading silence as information."""
    family = FAMILY_BY_NAME[name]
    empty = dict(scenario(name)["strong"])
    empty.update({f"trades_{iv}": 0.0 for iv in INTERVALS})
    features = make_features(empty)
    for params in family.param_space.combinations():
        assert family.signal(features, params) is None, f"{name} fired on an empty tape"


@pytest.mark.parametrize("name", TAPE_FAMILIES)
def test_tape_families_are_silent_when_the_trade_count_is_absent(name):
    """Not merely zero — *missing*.  An absent trade count is not permission."""
    family = FAMILY_BY_NAME[name]
    without = {
        k: v for k, v in scenario(name)["strong"].items()
        if not k.startswith("trades_")
    }
    features = make_features(without)
    for params in family.param_space.combinations():
        assert family.signal(features, params) is None, f"{name} fired without a tape"


def test_an_empty_tape_silences_only_the_tape_families():
    """Proof that the two tests above exercise the tape gate and not the setup:
    the same block still produces a signal from a price-only family."""
    values = dict(SCENARIOS["momentum_continuation"]["strong"])
    values.update({f"trades_{iv}": 0.0 for iv in INTERVALS})
    assert fire("momentum_continuation", values) is not None


@pytest.mark.parametrize("name", EXPECTED_FAMILY_NAMES)
def test_silent_on_a_timeframe_the_engine_was_not_configured_for(name):
    """``MTFFeatures`` carries only the intervals its engine was built with.
    Asked about a missing one, a family must return None, not read a zero."""
    family = FAMILY_BY_NAME[name]
    only_15m = {
        k: v for k, v in scenario(name)["strong"].items() if k.endswith("_15m")
    }
    features = make_features(only_15m)
    for params in family.param_space.combinations():
        if params.get("interval") == "15m":
            continue
        assert family.signal(features, params) is None


# --------------------------------------------------------------------------- #
# Robustness: missing, NaN and infinite features
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", EXPECTED_FAMILY_NAMES)
def test_missing_features_never_raise(name):
    family = FAMILY_BY_NAME[name]
    for features in (make_features({}), make_features(dict(ATRS))):
        for params in family.param_space.combinations():
            assert family.signal(features, params) is None


@pytest.mark.parametrize("name", EXPECTED_FAMILY_NAMES)
def test_nan_and_inf_features_never_raise_and_never_leak_into_score(name):
    """``features_mtf`` guarantees finite values, but a family is the last line
    of defence: a NaN score poisons every comparison the tournament makes with
    it and silently ranks nonsense first."""
    family = FAMILY_BY_NAME[name]
    sc = scenario(name)
    for poison in (float("nan"), float("inf"), float("-inf")):
        blocks = [make_features({k: poison for k in sc["strong"]})]
        blocks += [make_features({**sc["strong"], key: poison}) for key in sc["strong"]]
        for features in blocks:
            for params in family.param_space.combinations():
                sig = family.signal(features, params)
                if sig is None:
                    continue
                assert math.isfinite(sig.score)
                assert 0.0 < sig.score <= 1.0


@pytest.mark.parametrize("name", EXPECTED_FAMILY_NAMES)
def test_partial_params_fall_back_to_grid_defaults(name):
    """The tournament always passes a full combination, but a family must not
    explode when handed a partial dict, an unknown key, or nothing at all."""
    family = FAMILY_BY_NAME[name]
    features = make_features(scenario(name)["strong"])
    full = family.param_space.defaults()
    assert _outcome(family, features, {}) == _outcome(family, features, full)
    assert _outcome(family, features, {"nonsense": 1}) == _outcome(family, features, full)
    assert family.signal(features) == family.signal(features, full)


# --------------------------------------------------------------------------- #
# Family-specific behaviour worth pinning
# --------------------------------------------------------------------------- #


def test_momentum_requires_the_five_bar_leg_to_agree():
    values = dict(SCENARIOS["momentum_continuation"]["strong"], ret5_1m=-10.0)
    assert fire("momentum_continuation", values) is None


def test_momentum_requires_acceleration_not_just_size():
    """A big bar with a flattening slope is an exhaustion print, not a trend."""
    values = dict(SCENARIOS["momentum_continuation"]["strong"], accel_1m=-2.0)
    assert fire("momentum_continuation", values) is None


def test_breakout_needs_compression_as_well_as_a_break():
    values = dict(SCENARIOS["breakout"]["strong"], rvol_pct_1m=0.8)
    assert fire("breakout", values) is None


def test_breakout_is_silent_inside_the_range():
    """breakout_dist is exactly 0.0 inside the prior range, which is the whole
    trigger: no threshold of its own is needed or wanted."""
    values = dict(SCENARIOS["breakout"]["strong"], breakout_dist_1m=0.0)
    assert fire("breakout", values) is None


def test_breakout_fades_with_a_wider_prior_range():
    tight = fire("breakout", dict(SCENARIOS["breakout"]["strong"], rvol_pct_1m=0.05))
    loose = fire("breakout", dict(SCENARIOS["breakout"]["strong"], rvol_pct_1m=0.29))
    assert tight is not None and loose is not None
    assert loose.score < tight.score


def test_pullback_rejects_a_retrace_deep_enough_to_be_a_reversal():
    values = dict(SCENARIOS["trend_pullback"]["strong"], ret_1m=-40.0)
    assert fire("trend_pullback", values) is None


def test_pullback_rejects_a_broken_trend_leg():
    values = dict(SCENARIOS["trend_pullback"]["strong"], ret5_1m=-8.0)
    assert fire("trend_pullback", values) is None


def test_pullback_needs_a_trend_before_the_retrace():
    values = dict(SCENARIOS["trend_pullback"]["strong"], ema_slope_1m=0.05, rsi_1m=51.0)
    assert fire("trend_pullback", values) is None


def test_mean_reversion_refuses_to_fade_a_strong_trend():
    values = dict(SCENARIOS["mean_reversion"]["strong"], ema_slope_1m=2.0, rsi_1m=85.0)
    assert fire("mean_reversion", values) is None


def test_mean_reversion_fades_the_stretch():
    sig = fire("mean_reversion", SCENARIOS["mean_reversion"]["strong"])
    assert sig is not None and sig.side is Side.SHORT


def test_volume_impulse_needs_a_price_response_not_just_volume():
    values = dict(SCENARIOS["volume_impulse"]["strong"], ret_1m=0.2)
    assert fire("volume_impulse", values) is None


def test_volume_impulse_ignores_a_neutral_volume_reading():
    """vol_accel reports 1.0 when its baseline window is too empty to support a
    ratio — the guard that stopped V2's 1291x readings.  Every arm of this grid
    is strictly above 1.0, so a neutral reading can never buy a signal."""
    values = dict(SCENARIOS["volume_impulse"]["strong"], vol_accel_1m=1.0)
    family = FAMILY_BY_NAME["volume_impulse"]
    features = make_features(values)
    for params in family.param_space.combinations():
        assert family.signal(features, params) is None


def test_orderflow_needs_prints_behind_the_imbalance():
    values = dict(SCENARIOS["orderflow_imbalance"]["strong"], trades_1m=4.0)
    assert fire("orderflow_imbalance", values) is None


def test_orderflow_reads_the_side_of_the_aggressor():
    sig = fire("orderflow_imbalance", SCENARIOS["orderflow_imbalance"]["strong"])
    assert sig is not None and sig.side is Side.LONG


def test_vwap_reversion_refuses_a_trending_market():
    values = dict(SCENARIOS["vwap_reversion"]["strong"], ema_slope_1m=2.0, rsi_1m=85.0)
    assert fire("vwap_reversion", values) is None


def test_volatility_expansion_requires_vol_to_be_high_not_merely_moving():
    values = dict(SCENARIOS["volatility_expansion"]["strong"], rvol_pct_1m=0.5)
    assert fire("volatility_expansion", values) is None


def test_volatility_expansion_takes_its_side_from_the_expanding_bar():
    up = fire("volatility_expansion", SCENARIOS["volatility_expansion"]["strong"])
    down = fire("volatility_expansion",
                dict(SCENARIOS["volatility_expansion"]["strong"], ret_1m=-5.0))
    assert up is not None and down is not None
    assert up.side is Side.LONG and down.side is Side.SHORT


def test_short_term_reversal_requires_the_run_to_have_stopped():
    values = dict(SCENARIOS["short_term_reversal"]["strong"], ret_1m=2.0)
    assert fire("short_term_reversal", values) is None


def test_short_term_reversal_fades_the_run():
    sig = fire("short_term_reversal", SCENARIOS["short_term_reversal"]["strong"])
    assert sig is not None and sig.side is Side.SHORT


def test_momentum_and_reversal_are_opposite_hypotheses_on_the_same_data():
    """A and I disagree by construction.  One of them is wrong on this venue and
    the tournament, not this module, decides which."""
    values = _v(ret_1m=6.0, ret5_1m=20.0, accel_1m=1.0, ema_slope_1m=0.5, rsi_1m=65.0)
    momentum = fire("momentum_continuation", values,
                    {"interval": "1m", "min_impulse_atr": 1.0, "min_accel_atr": 0.1})
    reversal = fire("short_term_reversal", dict(values, ret_1m=-1.5),
                    {"interval": "1m", "ext_atr": 2.5, "stall_frac": 0.25})
    assert momentum is not None and reversal is not None
    assert momentum.side is Side.LONG
    assert reversal.side is Side.SHORT


def test_hybrid_tilt_moves_between_two_opposed_hypotheses():
    """A positive five-bar leg while price is *above* the mean: the two legs
    disagree, so the tilt alone decides the side.  That is the family."""
    values = _v(ret5_1m=30.0, zscore_1m=3.0)
    chase = fire("hybrid_multifactor", values,
                 {"momentum_tilt": 1.0, "min_conviction": 0.3, "flow_weight": 0.0})
    fade = fire("hybrid_multifactor", values,
                {"momentum_tilt": 0.0, "min_conviction": 0.3, "flow_weight": 0.0})
    assert chase is not None and fade is not None
    assert chase.side is Side.LONG
    assert fade.side is Side.SHORT


def test_hybrid_min_conviction_is_the_few_excellent_trades_dial():
    values = SCENARIOS["hybrid_multifactor"]["weak"]
    loose = fire("hybrid_multifactor", values,
                 {"momentum_tilt": 0.5, "min_conviction": 0.3, "flow_weight": 0.25})
    strict = fire("hybrid_multifactor", values,
                  {"momentum_tilt": 0.5, "min_conviction": 0.75, "flow_weight": 0.25})
    assert loose is not None
    assert strict is None


def test_hybrid_ignores_the_tape_when_flow_weight_is_zero():
    """The venue's tape is mostly empty, so the family has to stay runnable
    without it."""
    params = {"momentum_tilt": 0.5, "min_conviction": 0.3, "flow_weight": 0.0}
    base = dict(SCENARIOS["hybrid_multifactor"]["strong"], aggr_imb_1m=0.6)
    none = dict(SCENARIOS["hybrid_multifactor"]["strong"], aggr_imb_1m=0.0)
    a = fire("hybrid_multifactor", base, params)
    b = fire("hybrid_multifactor", none, params)
    assert a is not None and b is not None
    assert a.score == pytest.approx(b.score)


def test_hybrid_reads_features_only_and_never_calls_another_family():
    """Families are evaluated independently before any blending, so the hybrid
    must not reach into A-I: its conviction has to be reproducible from the
    feature block and its own three parameters alone."""
    import inspect

    from app.research import families as mod

    source = inspect.getsource(mod.HybridMultifactor)
    for other in EXPECTED_FAMILY_NAMES:
        if other != "hybrid_multifactor":
            assert other not in source
    for cls in ("MomentumContinuation", "Breakout", "TrendPullback", "MeanReversion",
                "VolumeImpulse", "OrderflowImbalance", "VwapReversion",
                "VolatilityExpansion", "ShortTermReversal", "FAMILIES"):
        assert cls not in source


# --------------------------------------------------------------------------- #
# Cross-module contracts
# --------------------------------------------------------------------------- #


def test_every_feature_a_family_reads_is_published_by_features_mtf():
    from app.research.features_mtf import FEATURE_NAMES

    published = set(FEATURE_NAMES)
    unknown = [n for n in feature_names_used() if n not in published]
    assert not unknown, (
        f"families.py reads features features_mtf.py does not publish: {unknown}"
    )
    assert validate_feature_names() == feature_names_used()


def test_validate_feature_names_fails_loudly_on_a_rename(monkeypatch):
    """MTFFeatures.get(name, default) cannot tell a typo from an absent
    feature, so a rename would silently degrade a family into a generator of
    neutral readings.  The check has to be explicit."""
    import app.research.features_mtf as fm

    monkeypatch.setattr(fm, "FEATURE_NAMES", ("atr_1m",), raising=True)
    with pytest.raises(ValueError, match="does not publish"):
        validate_feature_names()


def test_interval_set_matches_the_feature_engine_default():
    from app.research.features_mtf import DEFAULT_INTERVALS

    assert INTERVALS == DEFAULT_INTERVALS


def test_required_features_covers_every_base_used():
    from app.research.features_mtf import FEATURE_BASES

    for base in REQUIRED_FEATURES:
        assert base in FEATURE_BASES


def test_allowed_regimes_type_matches_the_regime_contract():
    pytest.importorskip("app.research.regimes")
    from app.research.regimes import Regime7

    for family in FAMILIES:
        allowed = family.allowed_regimes
        assert allowed is None or (
            isinstance(allowed, frozenset)
            and all(isinstance(r, Regime7) for r in allowed)
        )


def test_research_signal_uses_the_frozen_side_enum():
    sig = fire("momentum_continuation", SCENARIOS["momentum_continuation"]["strong"])
    assert isinstance(sig, ResearchSignal)
    assert isinstance(sig.side, Side)


def test_param_space_is_frozen():
    space = FAMILIES[0].param_space
    assert isinstance(space, ParamSpace)
    with pytest.raises(Exception):
        space.name = "mutated"  # type: ignore[misc]


def test_families_never_look_at_money_risk_or_the_exchange():
    """A family sees a feature block and nothing else.  The cheapest way to
    enforce that is the import list: no fees, no capital, no positions, no risk
    engine, no exchange client can reach this module, so none of them can be
    silently duplicated inside it."""
    import inspect

    from app.research import families as mod

    source = inspect.getsource(mod)
    imports = [
        line.strip() for line in source.splitlines()
        if line.startswith(("from ", "import "))
    ]
    assert imports == [
        "from __future__ import annotations",
        "import itertools",
        "import math",
        "from dataclasses import dataclass, field",
        "from typing import TYPE_CHECKING, Any, Iterator, Mapping, Sequence",
        "from ..types import Side",
    ], imports
