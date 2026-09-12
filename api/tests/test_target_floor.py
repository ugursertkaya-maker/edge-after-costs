"""A target below the floor is a refusal, not a number to invent.

Measured on the live venue 2026-09-12: ETHUSD 1m ATR was 0.131 bps, so
book_imbalance's own target was 0.079 bps. MIN_TARGET_BPS rewrote it to 4.0 --
a 51x inflation -- which turned a real gross edge of 0.045 bps into a reported
2.28, and put 198 candidates within an apparent 0.26 bps of a gate they were
truly 2.5 bps away from.

Every test here asserts the system trades LESS. That is the direction a
correctness fix to an edge calculation should move in.
"""

from __future__ import annotations

import pytest

from app.strategies.base import MIN_STOP_BPS, MIN_TARGET_BPS, Strategy
from app.types import Features, Regime, RegimeState, Side


def _f(atr_bps: float, spread_bps: float = 0.1) -> Features:
    mid = 2_500.0
    return Features(
        symbol="ETHUSD", ts_ms=1_789_000_000_000, price=mid, mid=mid,
        microprice=mid, spread_bps=spread_bps, book_age_ms=100,
        depth_bid_usd=100_000.0, depth_ask_usd=100_000.0, imbalance=0.3,
        atr_bps=atr_bps, realized_vol_bps=atr_bps * 2, vol_percentile=0.5,
        ret_1m_bps=2.0, ret_5m_bps=5.0, ret_15m_bps=8.0,
        ema_fast=mid * 1.0002, ema_slow=mid, ema_slope_bps=2.0,
        trend_strength=0.6, range_pos=0.5, bb_width_bps=atr_bps * 3,
        compression=0.3, volume_ratio=1.2, trade_flow_imbalance=0.3,
        trade_flow_accel=0.1, vwap=mid, vwap_dev_bps=1.0,
        quote_volume_24h=1_000_000.0, trades_24h=5_000, bars_available=60,
    )


class _Probe(Strategy):
    name = "probe"
    min_score = 0.1

    def evaluate(self, f, regime):  # pragma: no cover - unused
        return None


def _signal(target_bps: float, *, atr_bps: float = 40.0, spread_bps: float = 0.1):
    s = _Probe()
    return s._signal(
        f=_f(atr_bps, spread_bps), side=Side.LONG, score=0.8,
        target_bps=target_bps, stop_bps=10.0, reasons=["probe"],
    ) if hasattr(s, "_signal") else None


class TestSubFloorTargetsAreRefused:
    def test_a_target_below_the_floor_produces_no_signal(self):
        """The live case: 0.079 bps from a 0.131 bps ATR."""
        assert _signal(0.079) is None

    def test_a_target_just_below_the_floor_is_still_refused(self):
        assert _signal(MIN_TARGET_BPS - 0.001) is None

    def test_a_target_at_the_floor_is_accepted(self):
        sig = _signal(MIN_TARGET_BPS)
        assert sig is not None
        assert sig.target_bps == pytest.approx(MIN_TARGET_BPS)

    def test_a_real_target_passes_through_unchanged(self):
        sig = _signal(24.0)
        assert sig is not None
        assert sig.target_bps == pytest.approx(24.0), "the target must not be rewritten"

    def test_the_floor_never_inflates_a_target(self):
        """The defect: a 0.079 target became 4.0, a 51x fabrication."""
        for tiny in (0.0, 0.001, 0.079, 1.0, 3.999):
            sig = _signal(tiny)
            assert sig is None, f"target {tiny} was invented into a signal"


class TestTheStopFloorIsStillAClamp:
    def test_a_tight_stop_is_widened_not_refused(self):
        """The two floors are deliberately not symmetric: a stop that is too
        tight is a stop we widen; a target that is too small is no trade."""
        sig = _signal(24.0)
        assert sig is not None
        assert sig.stop_bps >= MIN_STOP_BPS
