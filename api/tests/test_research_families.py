"""Tests for the ten Phase 3 strategy families.

Deterministic and offline: no network, no clock, no RNG.  Every scenario is a
hand-built feature block with numbers taken from the measured venue (1m ATR
2.2 bps), so a threshold that looks reasonable here is reasonable there.

What is actually being asserted, family by family:

* it fires on a setup built for its own hypothesis, **on both sides**, and the
  two sides score identically — an asymmetric score would mean the family is
  really a long-only proxy for drift;
* it is silent on flat data, on an empty tape, and at cold start;
* ``score`` is graded — a weaker version of the same setup scores strictly
  lower;
* **every parameter changes behaviour.**  A parameter that does nothing is a
  bug that quadruples the search space for free, and with 444 combinations in
  play that is the difference between a search and a lottery;
* nothing returns NaN or infinity, and nothing raises when a feature is
  missing.
"""

from __future__ import annotations

import math

import pytest

from app.research.families import (
    FAMILIES,
    FAMILY_BY_NAME,
    REQUIRED_FEATURES,
    ParamSpace,
    ResearchSignal,
    total_combinations,
)
from app.types import Side

# Measured on Binance.US BTC/USD (docs/ECONOMICS.md).  Every scenario below is
# scaled off this number rather than off a round one.
ATR = 2.2
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

# Pinned so the number quoted in the report is the number the code produces.
EXPECTED_TOTAL_COMBINATIONS = 444

# Families whose signal depends on the trade tape being non-empty.
TAPE_FAMILIES = ("volume_impulse", "orderflow_imbalance")


# --------------------------------------------------------------------------- #
# Feature double
# --------------------------------------------------------------------------- #


class _FakeMTFFeatures:
    """Stand-in for MTFFeatures until features_mtf.py lands.

    Same four fields and the same ``get`` contract as the frozen dataclass in
    PHASE3_SPEC.md; :func:`make_features` prefers the real class whenever it is
    importable, so these tests switch over to it by themselves.
    """

    def __init__(self, symbol: str, ts_ms: int, price: float, values: dict):
        self.symbol = symbol
        self.ts_ms = ts_ms
        self.price = price
        self.values = values

    def get(self, name: str, default: float = 0.0) -> float:
        return self.values.get(name, default)


def _mtf_class():
    try:
        from app.research.features_mtf import MTFFeatures  # noqa: WPS433
    except Exception:  # pragma: no cover - module not written yet
        return None
    return MTFFeatures


def make_features(values: dict, *, price: float = PRICE, ts_ms: int = TS):
    """Build a feature block, using the real MTFFeatures when it exists."""
    cls = _mtf_class()
    if cls is not None:
        try:
            return cls(symbol=SYMBOL, ts_ms=ts_ms, price=price, values=dict(values))
        except TypeError:  # pragma: no cover - signature drift
            pass
    return _FakeMTFFeatures(SYMBOL, ts_ms, price, dict(values))


# Directional feature prefixes: these flip sign when a scenario is mirrored.
_SIGNED_PREFIXES = (
    "ret_", "return_", "mom_",
    "vwap_dev_", "mean_dev_", "dev_from_mean_",
    "zscore_", "z_", "bb_z_",
    "ema_slope_",
)
_SIGNED_EXACT = ("trade_flow_imbalance", "flow_imbalance", "aggressor_imbalance",
                 "trade_flow_accel", "flow_accel")
_POSITION_PREFIX = "range_pos"


def mirror(values: dict) -> dict:
    """Reflect a scenario through the mid: a long setup becomes its short twin."""
    out = {}
    for key, value in values.items():
        if key.startswith(_POSITION_PREFIX):
            out[key] = 1.0 - value
        elif key in _SIGNED_EXACT or key.startswith(_SIGNED_PREFIXES):
            out[key] = -value
        else:
            out[key] = value
    return out


# --------------------------------------------------------------------------- #
# Scenarios — one strong and one weak setup per family
# --------------------------------------------------------------------------- #

_BASE = {"atr_1m_bps": ATR}


def _v(**kw) -> dict:
    out = dict(_BASE)
    out.update(kw)
    return out


SCENARIOS: dict[str, dict] = {
    # A — a 1m impulse of 6 bps (2.7x the expected 2.2) running faster than the
    # 5m leg that agrees with it.
    "momentum_continuation": {
        "params": {"impulse_window": "1m", "min_impulse_atr": 1.0, "accel_ratio": 1.25},
        "strong": _v(ret_5s_bps=0.4, ret_15s_bps=0.9, ret_30s_bps=1.4,
                     ret_1m_bps=6.0, ret_3m_bps=7.0, ret_5m_bps=8.0,
                     ret_15m_bps=9.0, trend_strength=0.6),
        # Same acceleration, a third of the impulse.
        "weak": _v(ret_5s_bps=0.2, ret_15s_bps=0.4, ret_30s_bps=0.7,
                   ret_1m_bps=3.0, ret_3m_bps=3.5, ret_5m_bps=4.0,
                   ret_15m_bps=4.5, trend_strength=0.6),
    },
    # B — a tight 5m coil left behind by a 5 bps minute.
    "breakout": {
        "params": {"range_window": "5m", "min_compression": 0.35, "break_atr": 1.25},
        "strong": _v(compression_3m=0.6, compression_5m=0.7, compression_15m=0.55,
                     range_pos_3m=0.97, range_pos_5m=0.99, range_pos_15m=0.96,
                     ret_1m_bps=5.0, trade_flow_imbalance=0.4),
        "weak": _v(compression_3m=0.4, compression_5m=0.45, compression_15m=0.4,
                   range_pos_3m=0.97, range_pos_5m=0.99, range_pos_15m=0.96,
                   ret_1m_bps=3.2, trade_flow_imbalance=0.1),
    },
    # C — an uptrend that retraced 6 bps over 3m and has started back up.
    "trend_pullback": {
        "params": {"trend_min": 0.45, "pullback_atr": 1.0, "resume_atr": 0.2},
        "strong": _v(ema_slope_5m_bps=1.0, trend_strength=0.7,
                     ret_3m_bps=-6.0, ret_30s_bps=1.5),
        "weak": _v(ema_slope_5m_bps=1.0, trend_strength=0.5,
                   ret_3m_bps=-4.0, ret_30s_bps=0.5),
    },
    # D — 2.8 sigma above the 5m mean with the trend flat and flow turning.
    "mean_reversion": {
        "params": {"band_window": "5m", "extension_sigma": 2.0, "max_trend": 0.6},
        "strong": _v(zscore_3m=2.4, zscore_5m=2.8, zscore_15m=2.1,
                     trend_strength=0.2, trade_flow_accel=-0.3),
        "weak": _v(zscore_3m=2.1, zscore_5m=2.2, zscore_15m=2.05,
                   trend_strength=0.5, trade_flow_accel=-0.1),
    },
    # E — 12 prints in a minute (the venue averages 2.19) at volume z 4, and
    # the price actually moved.
    "volume_impulse": {
        "params": {"min_trades": 5, "vol_z_min": 2.5, "response_atr": 1.0},
        "strong": _v(trade_count_1m=12, quote_volume_1m_usd=9000.0,
                     volume_z_1m=4.0, ret_1m_bps=5.0),
        "weak": _v(trade_count_1m=8, quote_volume_1m_usd=4000.0,
                   volume_z_1m=3.0, ret_1m_bps=3.0),
    },
    # F — 30 prints, $6k, three quarters of it on one side.
    "orderflow_imbalance": {
        "params": {"imb_min": 0.4, "min_trades": 10, "min_notional_usd": 2000.0},
        "strong": _v(trade_count_1m=30, quote_volume_1m_usd=6000.0,
                     trade_flow_imbalance=0.75, trade_flow_accel=0.3),
        "weak": _v(trade_count_1m=30, quote_volume_1m_usd=3000.0,
                   trade_flow_imbalance=0.5, trade_flow_accel=0.1),
    },
    # G — 25 bps above the 15m VWAP, which is ~2.9 typical 15m moves.
    "vwap_reversion": {
        "params": {"vwap_window": "15m", "dev_atr": 2.0, "max_trend": 0.5},
        "strong": _v(vwap_dev_5m_bps=12.0, vwap_dev_15m_bps=25.0,
                     vwap_dev_60m_bps=40.0, trend_strength=0.2,
                     trade_flow_accel=-0.3),
        "weak": _v(vwap_dev_5m_bps=9.0, vwap_dev_15m_bps=19.0,
                   vwap_dev_60m_bps=30.0, trend_strength=0.4,
                   trade_flow_accel=-0.1),
    },
    # H — volatility 3.2x its slower baseline, at the 90th percentile.
    "volatility_expansion": {
        "params": {"expansion_ratio": 2.0, "min_vol_pct": 0.7, "direction_atr": 1.0},
        "strong": _v(vol_expansion_ratio=3.2, vol_percentile=0.9, ret_1m_bps=5.0),
        "weak": _v(vol_expansion_ratio=2.3, vol_percentile=0.75, ret_1m_bps=3.0),
    },
    # I — a 30s leg 3.9x its expected size that has already started giving back.
    "short_term_reversal": {
        "params": {"window": "30s", "ext_atr": 2.5, "stall_frac": 0.25},
        "strong": _v(ret_5s_bps=-0.3, ret_15s_bps=-0.8, ret_30s_bps=6.0,
                     ret_1m_bps=7.0, trend_strength=0.2),
        "weak": _v(ret_5s_bps=-0.15, ret_15s_bps=-0.4, ret_30s_bps=4.5,
                   ret_1m_bps=5.0, trend_strength=0.5),
    },
    # J — momentum up, price below VWAP, buy-side flow: all three legs agree.
    "hybrid_multifactor": {
        "params": {"momentum_tilt": 0.5, "min_conviction": 0.3, "flow_weight": 0.25},
        "strong": _v(ret_1m_bps=5.0, vwap_dev_5m_bps=-10.0,
                     trade_flow_imbalance=0.6),
        "weak": _v(ret_1m_bps=3.0, vwap_dev_5m_bps=-6.0,
                   trade_flow_imbalance=0.3),
    },
}


def scenario(name: str) -> dict:
    return SCENARIOS[name]


def fire(name: str, values: dict, params: dict | None = None) -> ResearchSignal | None:
    family = FAMILY_BY_NAME[name]
    return family.signal(make_features(values), params or scenario(name)["params"])


# --------------------------------------------------------------------------- #
# Registry
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
    # The tournament measures which family belongs in which regime.  Hardcoding
    # it here would be answering that question with an assumption.
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


# --------------------------------------------------------------------------- #
# Firing, both sides
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", EXPECTED_FAMILY_NAMES)
def test_family_fires_long_on_its_own_setup(name):
    sig = fire(name, scenario(name)["strong"])
    assert sig is not None, f"{name} did not fire on its own strong setup"
    assert sig.side is Side.LONG or sig.side is Side.SHORT
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
    assert long_sig.side is not short_sig.side, f"{name} did not flip side"
    assert {long_sig.side, short_sig.side} == {Side.LONG, Side.SHORT}
    # Mirrored input must produce a mirrored opinion of identical conviction;
    # an asymmetry here means the family is really a long-only drift proxy.
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
        assert 0.0 < sig.score <= 1.0
        assert math.isfinite(sig.score)
    # Not saturated at the top either — a family pinned at 1.0 is binary again.
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
    assert seen > 0, f"{name} never fired anywhere in its grid"


# --------------------------------------------------------------------------- #
# Every parameter earns its place in the search space
# --------------------------------------------------------------------------- #


def _outcome(family, features, params):
    sig = family.signal(features, params)
    if sig is None:
        return None
    return (sig.side, round(sig.score, 9))


@pytest.mark.parametrize("name", EXPECTED_FAMILY_NAMES)
def test_every_parameter_changes_behaviour(name):
    """A parameter that does nothing is a bug: it multiplies the search space
    without adding a hypothesis, and inflates the multiple-testing correction
    the report has to apply."""
    family = FAMILY_BY_NAME[name]
    sc = scenario(name)
    features = make_features(sc["strong"])
    base = dict(sc["params"])

    for param, candidates in family.param_space.grid.items():
        outcomes = set()
        for value in candidates:
            params = dict(base)
            params[param] = value
            outcomes.add(_outcome(family, features, params))
        assert len(outcomes) > 1, (
            f"{name}.{param} has no effect on its own scenario "
            f"({len(candidates)} values, one outcome: {outcomes})"
        )


# --------------------------------------------------------------------------- #
# Silence: flat, cold, empty tape, missing features
# --------------------------------------------------------------------------- #

FLAT = {
    "atr_1m_bps": ATR,
    "atr_5m_bps": ATR * math.sqrt(5.0),
    "ret_5s_bps": 0.0, "ret_15s_bps": 0.0, "ret_30s_bps": 0.0,
    "ret_1m_bps": 0.0, "ret_3m_bps": 0.0, "ret_5m_bps": 0.0, "ret_15m_bps": 0.0,
    "ema_slope_5m_bps": 0.0, "trend_strength": 0.0,
    "compression_3m": 0.0, "compression_5m": 0.0, "compression_15m": 0.0,
    "range_pos_3m": 0.5, "range_pos_5m": 0.5, "range_pos_15m": 0.5,
    "zscore_3m": 0.0, "zscore_5m": 0.0, "zscore_15m": 0.0,
    "bb_width_3m_bps": 4.0, "bb_width_5m_bps": 9.0, "bb_width_15m_bps": 16.0,
    "vwap_dev_5m_bps": 0.0, "vwap_dev_15m_bps": 0.0, "vwap_dev_60m_bps": 0.0,
    "trade_count_1m": 2.0, "quote_volume_1m_usd": 753.0,
    "volume_z_1m": 0.0, "volume_ratio_1m": 1.0,
    "vol_percentile": 0.4, "vol_expansion_ratio": 1.0,
    "trade_flow_imbalance": 0.0, "trade_flow_accel": 0.0,
}


@pytest.mark.parametrize("name", EXPECTED_FAMILY_NAMES)
def test_silent_on_flat_data_across_the_whole_grid(name):
    family = FAMILY_BY_NAME[name]
    features = make_features(FLAT)
    for params in family.param_space.combinations():
        assert family.signal(features, params) is None, f"{name} fired on flat data"


@pytest.mark.parametrize("name", EXPECTED_FAMILY_NAMES)
def test_silent_at_cold_start(name):
    """No volatility estimate yet, or no price: no opinion."""
    family = FAMILY_BY_NAME[name]
    sc = scenario(name)
    cold_variants = [
        make_features({k: v for k, v in sc["strong"].items() if k != "atr_1m_bps"}),
        make_features({**sc["strong"], "atr_1m_bps": 0.0}),
        make_features(sc["strong"], price=0.0),
        make_features({}),
    ]
    for features in cold_variants:
        for params in family.param_space.combinations():
            assert family.signal(features, params) is None, f"{name} fired cold"


@pytest.mark.parametrize("name", TAPE_FAMILIES)
def test_tape_families_are_silent_on_an_empty_tape(name):
    """55% of this venue's minutes contain zero trades.  A tape family must
    return None on those, never degrade into 'always fires'."""
    family = FAMILY_BY_NAME[name]
    sc = scenario(name)
    empty = dict(sc["strong"])
    empty.update({"trade_count_1m": 0.0, "quote_volume_1m_usd": 0.0})
    features = make_features(empty)
    for params in family.param_space.combinations():
        assert family.signal(features, params) is None, f"{name} fired on an empty tape"


@pytest.mark.parametrize("name", TAPE_FAMILIES)
def test_tape_families_are_silent_when_tape_density_is_absent(name):
    """Not merely zero — *missing*.  An absent trade count is not permission."""
    family = FAMILY_BY_NAME[name]
    sc = scenario(name)
    without = {
        k: v for k, v in sc["strong"].items()
        if k not in {"trade_count_1m", "trades_1m", "n_trades_1m", "trades_per_min",
                     "quote_volume_1m_usd"}
    }
    features = make_features(without)
    for params in family.param_space.combinations():
        assert family.signal(features, params) is None, f"{name} fired without a tape"


def test_an_empty_tape_silences_only_the_tape_families():
    """Proof that the previous two tests test the tape gate and not the setup:
    the same block still produces signals from the price-only families."""
    values = dict(SCENARIOS["momentum_continuation"]["strong"])
    values.update({"trade_count_1m": 0.0, "quote_volume_1m_usd": 0.0})
    features = make_features(values)
    sig = FAMILY_BY_NAME["momentum_continuation"].signal(
        features, SCENARIOS["momentum_continuation"]["params"]
    )
    assert sig is not None


# --------------------------------------------------------------------------- #
# Robustness: missing, NaN and infinite features
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", EXPECTED_FAMILY_NAMES)
def test_missing_features_never_raise(name):
    family = FAMILY_BY_NAME[name]
    blocks = [
        make_features({}),
        make_features({"atr_1m_bps": ATR}),
        make_features({"atr_bps": ATR, "price": PRICE}),
    ]
    for features in blocks:
        for params in family.param_space.combinations():
            assert family.signal(features, params) is None


@pytest.mark.parametrize("name", EXPECTED_FAMILY_NAMES)
def test_nan_and_inf_features_never_raise_and_never_leak_into_score(name):
    family = FAMILY_BY_NAME[name]
    sc = scenario(name)
    poisons = (float("nan"), float("inf"), float("-inf"))
    for poison in poisons:
        # Whole block poisoned.
        everything = {k: poison for k in sc["strong"]}
        # One value at a time poisoned, on top of a working setup.
        blocks = [make_features(everything)]
        blocks += [
            make_features({**sc["strong"], key: poison}) for key in sc["strong"]
        ]
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
    explode when handed a partial dict or an unknown key."""
    family = FAMILY_BY_NAME[name]
    features = make_features(scenario(name)["strong"])
    assert family.signal(features, {}) is None or True          # must not raise
    assert family.signal(features, {"nonsense": 1}) is None or True
    full = family.param_space.defaults()
    assert _outcome(family, features, {}) == _outcome(family, features, full)


# --------------------------------------------------------------------------- #
# Family-specific behaviour worth pinning
# --------------------------------------------------------------------------- #


def test_momentum_requires_the_slower_leg_to_agree():
    sc = SCENARIOS["momentum_continuation"]
    values = dict(sc["strong"])
    values["ret_5m_bps"] = -8.0                     # 1m up, 5m down
    assert fire("momentum_continuation", values) is None


def test_momentum_requires_acceleration_not_just_size():
    sc = SCENARIOS["momentum_continuation"]
    values = dict(sc["strong"])
    values["ret_5m_bps"] = 40.0                     # the 1m leg is decelerating
    assert fire("momentum_continuation", values) is None


def test_breakout_needs_compression_as_well_as_a_move():
    sc = SCENARIOS["breakout"]
    values = dict(sc["strong"])
    values["compression_5m"] = 0.05                 # a move inside a wide range
    assert fire("breakout", values) is None


def test_breakout_needs_the_edge_of_the_range():
    sc = SCENARIOS["breakout"]
    values = dict(sc["strong"])
    values["range_pos_5m"] = 0.55                   # mid-range: not a breakout
    assert fire("breakout", values) is None


def test_pullback_rejects_a_retrace_deep_enough_to_be_a_reversal():
    sc = SCENARIOS["trend_pullback"]
    values = dict(sc["strong"])
    values["ret_3m_bps"] = -40.0
    assert fire("trend_pullback", values) is None


def test_pullback_waits_for_the_turn():
    sc = SCENARIOS["trend_pullback"]
    values = dict(sc["strong"])
    values["ret_30s_bps"] = -1.5                    # still falling
    assert fire("trend_pullback", values) is None


def test_mean_reversion_refuses_to_fade_a_strong_trend():
    sc = SCENARIOS["mean_reversion"]
    values = dict(sc["strong"])
    values["trend_strength"] = 0.95
    assert fire("mean_reversion", values) is None


def test_mean_reversion_falls_back_to_band_width_when_no_zscore_is_published():
    """The band is mean +/- 2 sigma, so a 20 bps width means sigma = 5 bps."""
    values = {
        "atr_1m_bps": ATR,
        "bb_width_5m_bps": 20.0,
        "vwap_dev_5m_bps": 14.0,                    # 2.8 sigma
        "trend_strength": 0.2,
        "trade_flow_accel": -0.3,
    }
    sig = fire("mean_reversion", values)
    assert sig is not None and sig.side is Side.SHORT
    weaker = dict(values, vwap_dev_5m_bps=11.0)     # 2.2 sigma
    weak_sig = fire("mean_reversion", weaker)
    assert weak_sig is not None and weak_sig.score < sig.score


def test_mean_reversion_is_silent_when_no_deviation_feature_exists():
    values = {"atr_1m_bps": ATR, "trend_strength": 0.2}
    assert fire("mean_reversion", values) is None


def test_volume_impulse_needs_a_price_response_not_just_volume():
    sc = SCENARIOS["volume_impulse"]
    values = dict(sc["strong"])
    values["ret_1m_bps"] = 0.2                      # volume with no move
    assert fire("volume_impulse", values) is None


def test_volume_ratio_fallback_is_capped_against_the_v2_divide_by_zero_bug():
    """V2 reported volume ratios like 1291x because the baseline is usually
    zero here.  The ratio fallback is capped, so an absurd reading cannot buy
    more conviction than a merely large one."""
    base = {
        "atr_1m_bps": ATR, "trade_count_1m": 12, "ret_1m_bps": 5.0,
        "quote_volume_1m_usd": 9000.0,
    }
    params = {"min_trades": 5, "vol_z_min": 2.5, "response_atr": 1.0}
    big = fire("volume_impulse", dict(base, volume_ratio_1m=50.0), params)
    absurd = fire("volume_impulse", dict(base, volume_ratio_1m=1291.0), params)
    assert big is not None and absurd is not None
    assert big.score == pytest.approx(absurd.score)


def test_orderflow_needs_notional_behind_the_imbalance():
    sc = SCENARIOS["orderflow_imbalance"]
    values = dict(sc["strong"])
    values["quote_volume_1m_usd"] = 50.0            # one-sided, but it is dust
    assert fire("orderflow_imbalance", values) is None


def test_vwap_reversion_refuses_a_trending_market():
    sc = SCENARIOS["vwap_reversion"]
    values = dict(sc["strong"])
    values["trend_strength"] = 0.9
    assert fire("vwap_reversion", values) is None


def test_volatility_expansion_falls_back_to_the_atr_ratio():
    """atr_1m * sqrt(5) / atr_5m is 1.0 under diffusion scaling and rises when
    the one-minute series wakes up."""
    quiet = {"atr_1m_bps": ATR, "atr_5m_bps": ATR * math.sqrt(5.0),
             "vol_percentile": 0.9, "ret_1m_bps": 5.0}
    assert fire("volatility_expansion", quiet) is None
    loud = {"atr_1m_bps": 5.0, "atr_5m_bps": 5.0, "vol_percentile": 0.9,
            "ret_1m_bps": 12.0}
    sig = fire("volatility_expansion", loud)
    assert sig is not None and sig.side is Side.LONG


def test_volatility_expansion_requires_vol_to_actually_be_high():
    sc = SCENARIOS["volatility_expansion"]
    values = dict(sc["strong"])
    values["vol_percentile"] = 0.2                  # a blip inside a dead tape
    assert fire("volatility_expansion", values) is None


def test_short_term_reversal_requires_the_move_to_have_stopped():
    sc = SCENARIOS["short_term_reversal"]
    values = dict(sc["strong"])
    values["ret_15s_bps"] = 2.0                     # still extending
    assert fire("short_term_reversal", values) is None


def test_short_term_reversal_fades_the_move():
    sig = fire("short_term_reversal", SCENARIOS["short_term_reversal"]["strong"])
    assert sig is not None and sig.side is Side.SHORT


def test_short_term_reversal_is_silent_without_sub_minute_returns():
    values = {"atr_1m_bps": ATR, "ret_30s_bps": 6.0, "trend_strength": 0.2}
    assert fire("short_term_reversal", values) is None


def test_hybrid_tilt_moves_between_two_opposed_hypotheses():
    """Momentum up while price is *above* VWAP: the two legs disagree, so the
    tilt decides the side.  That is the whole point of the family."""
    values = {"atr_1m_bps": ATR, "ret_1m_bps": 12.0, "vwap_dev_5m_bps": 30.0}
    chase = fire("hybrid_multifactor", values,
                 {"momentum_tilt": 1.0, "min_conviction": 0.3, "flow_weight": 0.0})
    fade = fire("hybrid_multifactor", values,
                {"momentum_tilt": 0.0, "min_conviction": 0.3, "flow_weight": 0.0})
    assert chase is not None and fade is not None
    assert chase.side is Side.LONG
    assert fade.side is Side.SHORT


def test_hybrid_reads_features_only_and_never_calls_another_family():
    """Families are evaluated independently before any blending, so the hybrid
    must not reach into A-I: its conviction has to be reproducible from the
    feature block and its own three parameters alone."""
    import inspect

    from app.research import families as mod

    source = inspect.getsource(mod.HybridMultifactor)
    for other in EXPECTED_FAMILY_NAMES:
        if other == "hybrid_multifactor":
            continue
        assert other not in source
    for cls in ("MomentumContinuation", "Breakout", "TrendPullback", "MeanReversion",
                "VolumeImpulse", "OrderflowImbalance", "VwapReversion",
                "VolatilityExpansion", "ShortTermReversal", "FAMILIES"):
        assert cls not in source


def test_hybrid_min_conviction_is_the_few_excellent_trades_dial():
    values = SCENARIOS["hybrid_multifactor"]["weak"]
    loose = fire("hybrid_multifactor", values,
                 {"momentum_tilt": 0.5, "min_conviction": 0.3, "flow_weight": 0.25})
    strict = fire("hybrid_multifactor", values,
                  {"momentum_tilt": 0.5, "min_conviction": 0.75, "flow_weight": 0.25})
    assert loose is not None
    assert strict is None


# --------------------------------------------------------------------------- #
# Cross-module contract checks (active as soon as the sibling modules land)
# --------------------------------------------------------------------------- #


def test_feature_names_exist_in_the_published_feature_block():
    pytest.importorskip("app.research.features_mtf")
    from app.research.features_mtf import FEATURE_NAMES

    published = set(FEATURE_NAMES)
    missing = {
        logical: aliases
        for logical, aliases in REQUIRED_FEATURES.items()
        if not published.intersection(aliases)
    }
    assert not missing, (
        "families.py reads features that features_mtf.py does not publish: "
        f"{sorted(missing)}"
    )


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
    assert sig.side in (Side.LONG, Side.SHORT)


def test_param_space_is_frozen():
    space = FAMILIES[0].param_space
    assert isinstance(space, ParamSpace)
    with pytest.raises(Exception):
        space.name = "mutated"  # type: ignore[misc]
