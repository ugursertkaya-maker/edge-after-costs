"""Triple-barrier labelling — hand-built paths where the answer is obvious.

Every path in this file is short enough to check by eye.  The prices are round
numbers and the spec arithmetic is spelled out in the test, so a failure here
points at the labeller, never at the fixture.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from app.predict.labels import (
    DEFAULT_HORIZONS_MINUTES,
    DEFAULT_NET_TARGET_PCTS,
    MIN_NET_TARGET_BPS,
    BarrierSpec,
    NoEdgeReason,
    Outcome,
    atr_bps_series,
    build_labels,
    class_balance,
    default_specs,
    label_matrix,
    realized_vol_bps_series,
    resolve_specs,
)
from app.types import Bar

SYMBOL = "BTCUSD"
MINUTE = 60_000
T0 = (1_756_000_000_000 // MINUTE) * MINUTE


def make_bars(rows, *, symbol: str = SYMBOL, t0: int = T0) -> list[Bar]:
    """``rows`` are ``(open, high, low, close)`` tuples, one per minute."""
    bars = []
    for i, (o, h, l, c) in enumerate(rows):
        open_ms = t0 + i * MINUTE
        bars.append(
            Bar(
                symbol=symbol,
                open_ms=open_ms,
                close_ms=open_ms + MINUTE - 1,
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                volume=1.0,
                quote_volume=float(c),
                trades=1,
                taker_buy_quote=0.0,
            )
        )
    return bars


def flat(price: float):
    return (price, price, price, price)


# A deliberately simple geometry: 15 bps NET target, 5 bps round trip, so the
# take-profit needs a **20 bps gross** move.  The stop is a 25 bps NET band, so
# it sits 20 bps below the entry (the 5 bps round trip is paid on the way out).
SPEC = BarrierSpec(
    net_target_pct=0.15,
    stop_kind="fixed_net",
    stop_value=25.0,
    horizon_minutes=3,
    round_trip_bps=5.0,
)


def first_label(bars, spec=SPEC, **kw):
    return build_labels(bars, spec=spec, symbol=SYMBOL, **kw)[0]


# --------------------------------------------------------------------------- #
# The geometry itself
# --------------------------------------------------------------------------- #


def test_gross_barriers_sit_further_out_than_the_net_target_by_the_round_trip():
    bars = make_bars([flat(100.0)] * 5)
    label = first_label(bars)

    assert SPEC.net_target_bps == pytest.approx(15.0)
    assert SPEC.gross_target_bps == pytest.approx(20.0)  # 15 net + 5 round trip
    assert label.entry_price == pytest.approx(100.0)
    assert label.tp_price == pytest.approx(100.0 * 1.0020)
    assert label.sl_price == pytest.approx(100.0 * 0.9980)


def test_tp_touched_before_sl_is_a_win_with_the_exact_hit_time():
    bars = make_bars(
        [
            flat(100.0),                      # entry bar, close 100
            (100.0, 100.10, 99.90, 100.0),    # nothing touched
            (100.0, 100.30, 99.90, 100.25),   # high 100.30 >= tp 100.20
            flat(100.0),
        ]
    )
    label = first_label(bars)

    assert label.outcome is Outcome.TP_BEFORE_SL
    assert label.hit_ms == bars[2].close_ms
    assert label.label_end_ms == bars[2].close_ms
    # A NET target is NET: realising it pays exactly the target, cost removed.
    assert label.net_return_bps == pytest.approx(15.0)


def test_sl_touched_before_tp_is_a_loss_costing_the_band_plus_nothing_extra():
    bars = make_bars(
        [
            flat(100.0),
            (100.0, 100.10, 99.50, 99.60),    # low 99.50 <= sl 99.80
            (100.0, 101.00, 99.90, 100.90),   # a win that arrives too late
        ]
    )
    label = first_label(bars)

    assert label.outcome is Outcome.SL_BEFORE_TP
    assert label.hit_ms == bars[1].close_ms
    assert label.label_end_ms == bars[1].close_ms
    # 20 bps gross drop + 5 bps round trip = the 25 bps NET band.
    assert label.net_return_bps == pytest.approx(-25.0)


def test_neither_barrier_touched_times_out_at_the_horizon_end():
    bars = make_bars([flat(100.0)] + [(100.0, 100.15, 99.85, 100.10)] * 5)
    label = first_label(bars)

    assert label.outcome is Outcome.TIMEOUT
    assert label.hit_ms == 0
    assert label.label_end_ms == bars[0].close_ms + SPEC.horizon_ms
    # Exit at the close of the last bar inside the horizon, minus the round trip.
    assert label.net_return_bps == pytest.approx(10.0 - 5.0)


def test_both_barriers_inside_one_bar_is_pessimistic_and_labels_the_stop():
    """The tie-break the rest of the codebase already uses: the stop hit first."""
    bars = make_bars(
        [
            flat(100.0),
            (100.0, 100.50, 99.50, 100.30),   # touches tp 100.02 *and* sl 99.98
            flat(100.0),
        ]
    )
    label = first_label(bars)

    assert label.outcome is Outcome.SL_BEFORE_TP
    assert label.hit_ms == bars[1].close_ms


def test_a_move_that_clears_the_net_target_but_not_the_costs_is_not_a_winner():
    """The single most damaging bug this module could have.

    The path rises 15 bps — exactly the NET target — and stops there.  15 bps
    gross does not pay a 5 bps round trip *and* a 15 bps target, so it is not a
    win: it is a timeout worth 10 bps net.
    """
    up = 100.0 * 1.0015                        # +15 bps, the NET target level
    bars = make_bars(
        [
            flat(100.0),
            (100.0, up, 99.99, up),
            (up, up, up, up),
            (up, up, up, up),
        ]
    )
    label = first_label(bars)

    assert up < label.tp_price                 # the gross trigger is further out
    assert label.outcome is not Outcome.TP_BEFORE_SL
    assert label.outcome is Outcome.TIMEOUT
    assert label.net_return_bps == pytest.approx(10.0)
    assert label.net_return_bps < SPEC.net_target_bps


def test_the_smallest_move_that_does_clear_the_costs_is_a_winner():
    """The other side of the same boundary, so the test above is not vacuous."""
    up = 100.0 * 1.0020                        # +20 bps gross == 15 bps net
    bars = make_bars([flat(100.0), (100.0, up, 99.99, up), flat(up), flat(up)])
    label = first_label(bars)

    assert label.outcome is Outcome.TP_BEFORE_SL
    assert label.net_return_bps == pytest.approx(15.0)


def test_a_stop_gapped_through_costs_more_than_the_nominal_band():
    """A bar that *opens* below the stop could not have filled at the stop."""
    bars = make_bars([flat(100.0), (99.00, 99.10, 98.90, 99.00), flat(99.0)])
    label = first_label(bars)

    assert label.outcome is Outcome.SL_BEFORE_TP
    # Filled at the open (99.00 = -100 bps), plus the 5 bps round trip.
    assert label.net_return_bps == pytest.approx(-105.0)
    assert label.net_return_bps < -25.0


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


def test_a_five_bps_target_against_a_six_bps_round_trip_is_refused():
    with pytest.raises(ValueError, match="round trip"):
        BarrierSpec(
            net_target_pct=0.05,
            stop_kind="fixed_net",
            stop_value=20.0,
            horizon_minutes=25,
            round_trip_bps=6.0,
        )


def test_a_target_below_the_ten_bps_floor_is_refused_even_when_costs_are_tiny():
    with pytest.raises(ValueError, match="floor"):
        BarrierSpec(
            net_target_pct=0.08,
            stop_kind="fixed_net",
            stop_value=20.0,
            horizon_minutes=25,
            round_trip_bps=1.0,
        )


def test_a_fixed_net_stop_band_consumed_by_its_own_round_trip_is_refused():
    with pytest.raises(ValueError, match="consumed"):
        BarrierSpec(
            net_target_pct=0.15,
            stop_kind="fixed_net",
            stop_value=4.0,
            horizon_minutes=25,
            round_trip_bps=5.0,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"stop_kind": "made_up"},
        {"stop_value": 0.0},
        {"horizon_minutes": 0},
        {"round_trip_bps": -1.0},
        {"net_target_pct": float("nan")},
    ],
)
def test_structurally_invalid_specs_are_refused(kwargs):
    base = dict(
        net_target_pct=0.15,
        stop_kind="atr",
        stop_value=2.0,
        horizon_minutes=25,
        round_trip_bps=4.0,
    )
    base.update(kwargs)
    with pytest.raises(ValueError):
        BarrierSpec(**base)


def test_the_default_ladder_starts_at_the_floor_and_goes_up():
    assert DEFAULT_NET_TARGET_PCTS[0] == pytest.approx(0.10)
    assert DEFAULT_NET_TARGET_PCTS == tuple(sorted(DEFAULT_NET_TARGET_PCTS))
    assert DEFAULT_NET_TARGET_PCTS[0] * 100.0 >= MIN_NET_TARGET_BPS
    assert DEFAULT_NET_TARGET_PCTS == (0.10, 0.15, 0.20, 0.25, 0.35, 0.50, 0.75, 1.00)

    specs = default_specs(round_trip_bps=4.5)
    assert len(specs) == len(DEFAULT_NET_TARGET_PCTS) * len(DEFAULT_HORIZONS_MINUTES)
    assert len({s.key() for s in specs}) == len(specs)


def test_unsorted_or_empty_bars_are_refused():
    with pytest.raises(ValueError, match="empty"):
        build_labels([], spec=SPEC, symbol=SYMBOL)

    bars = make_bars([flat(100.0)] * 4)
    scrambled = [bars[2], bars[0], bars[1], bars[3]]
    with pytest.raises(ValueError, match="increasing"):
        build_labels(scrambled, spec=SPEC, symbol=SYMBOL)


def test_duplicate_specs_are_refused():
    bars = make_bars([flat(100.0)] * 5)
    with pytest.raises(ValueError, match="duplicate"):
        label_matrix(bars, specs=[SPEC, SPEC], symbol=SYMBOL)


# --------------------------------------------------------------------------- #
# NO_EDGE — each documented reason, and nothing else
# --------------------------------------------------------------------------- #


def test_the_last_bar_has_an_empty_forward_window():
    bars = make_bars([flat(100.0)] * 4)
    labels = build_labels(bars, spec=SPEC, symbol=SYMBOL)
    arrays = resolve_specs(bars, specs=[SPEC])[SPEC.key()]

    assert labels[-1].outcome is Outcome.NO_EDGE
    assert labels[-1].label_end_ms == labels[-1].ts_ms
    assert labels[-1].hit_ms == 0
    assert labels[-1].net_return_bps == 0.0
    assert arrays.no_edge_counts()[NoEdgeReason.EMPTY_WINDOW] == 1


def test_a_truncated_horizon_with_no_touch_is_censored_not_a_timeout():
    """The last H minutes of a series must not become free negatives."""
    spec = BarrierSpec(
        net_target_pct=0.15,
        stop_kind="fixed_net",
        stop_value=25.0,
        horizon_minutes=10,
        round_trip_bps=5.0,
    )
    bars = make_bars([flat(100.0)] * 4)          # only 3 forward bars of 10
    arrays = resolve_specs(bars, specs=[spec])[spec.key()]
    labels = arrays.labels(SYMBOL)

    assert all(lbl.outcome is Outcome.NO_EDGE for lbl in labels)
    counts = arrays.no_edge_counts()
    assert counts[NoEdgeReason.CENSORED] == 3
    assert counts[NoEdgeReason.EMPTY_WINDOW] == 1


def test_a_touch_inside_a_truncated_window_is_still_a_real_label():
    spec = BarrierSpec(
        net_target_pct=0.15,
        stop_kind="fixed_net",
        stop_value=25.0,
        horizon_minutes=10,
        round_trip_bps=5.0,
    )
    bars = make_bars([flat(100.0), (100.0, 100.30, 99.90, 100.25)])
    label = build_labels(bars, spec=spec, symbol=SYMBOL)[0]

    assert label.outcome is Outcome.TP_BEFORE_SL
    assert label.hit_ms == bars[1].close_ms


def test_an_atr_stop_during_warmup_is_no_edge_not_an_instant_loss():
    """A zero ATR must never produce a zero-width stop that labels a loser."""
    spec = BarrierSpec(
        net_target_pct=0.15,
        stop_kind="atr",
        stop_value=2.0,
        horizon_minutes=3,
        round_trip_bps=5.0,
    )
    rows = [(100.0, 100.2, 99.8, 100.0) for _ in range(40)]
    bars = make_bars(rows)
    arrays = resolve_specs(bars, specs=[spec])[spec.key()]
    labels = arrays.labels(SYMBOL)

    warmup = [lbl for lbl in labels[:14]]
    assert all(lbl.outcome is Outcome.NO_EDGE for lbl in warmup)
    assert arrays.no_edge_counts()[NoEdgeReason.DEGENERATE_STOP] == 14
    assert not any(lbl.outcome is Outcome.SL_BEFORE_TP for lbl in warmup)
    # Once the ATR is seeded the geometry is real again.
    assert labels[20].sl_price < labels[20].entry_price


def test_an_unreachable_target_is_no_edge_only_when_the_caller_says_so():
    bars = make_bars([flat(100.0)] * 6)
    arrays = resolve_specs(bars, specs=[SPEC], max_gross_move_bps=5.0)[SPEC.key()]

    labels = arrays.labels(SYMBOL)
    assert all(lbl.outcome is Outcome.NO_EDGE for lbl in labels)
    assert arrays.no_edge_counts()[NoEdgeReason.UNREACHABLE] == len(bars)

    # Without the ceiling the same path is an ordinary timeout.
    plain = resolve_specs(bars, specs=[SPEC])[SPEC.key()].labels(SYMBOL)
    assert plain[0].outcome is Outcome.TIMEOUT


# --------------------------------------------------------------------------- #
# label_end_ms and leakage
# --------------------------------------------------------------------------- #


def test_label_end_ms_is_the_touch_on_a_hit_and_the_horizon_end_on_a_timeout():
    hit = make_bars([flat(100.0), flat(100.0), (100.0, 100.9, 99.99, 100.5)])
    timeout = make_bars([flat(100.0)] * 6)

    hit_label = first_label(hit)
    timeout_label = first_label(timeout)

    assert hit_label.label_end_ms == hit_label.hit_ms == hit[2].close_ms
    assert timeout_label.hit_ms == 0
    assert timeout_label.label_end_ms == timeout[0].close_ms + SPEC.horizon_ms


@pytest.mark.parametrize("tail", [[flat(1000.0)] * 6, [flat(1.0)] * 6])
def test_a_label_is_unchanged_when_bars_after_its_window_are_rewritten(tail):
    """The strongest statement this file makes: no label sees past its end.

    The tail is replaced with a 10x spike and with a 99% crash.  Every label
    whose window closed before the rewrite must be byte-identical.
    """
    head = [
        flat(100.0),
        (100.0, 100.10, 99.90, 100.0),
        (100.0, 100.30, 99.90, 100.25),   # TP for the label at index 0
        flat(100.0),
        flat(100.0),
        flat(100.0),
    ]
    original = build_labels(make_bars(head + [flat(100.0)] * 6), spec=SPEC, symbol=SYMBOL)
    rewritten = build_labels(make_bars(head + tail), spec=SPEC, symbol=SYMBOL)

    cutoff = make_bars(head)[-1].close_ms
    changed_from_ms = T0 + len(head) * MINUTE
    assert changed_from_ms > cutoff

    survivors = [lbl for lbl in original if lbl.label_end_ms <= cutoff]
    assert survivors, "the fixture must contain labels that close before the rewrite"
    by_ts = {lbl.ts_ms: lbl for lbl in rewritten}
    for label in survivors:
        assert by_ts[label.ts_ms] == label


def test_a_timeout_label_does_not_see_the_bar_after_its_horizon():
    bars = make_bars([flat(100.0)] * 8)
    base = first_label(bars)
    assert base.outcome is Outcome.TIMEOUT

    # Bar 4 closes after the 3-minute horizon of the label at index 0.
    moved = make_bars([flat(100.0)] * 4 + [(100.0, 200.0, 100.0, 200.0)] + [flat(200.0)] * 3)
    assert moved[4].close_ms > base.label_end_ms
    assert first_label(moved) == base


# --------------------------------------------------------------------------- #
# label_matrix — one walk, many specs
# --------------------------------------------------------------------------- #


def _random_walk(n: int, *, seed: int = 7, start: float = 100.0) -> list[Bar]:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 0.0006, size=n)
    close = start * np.exp(np.cumsum(steps))
    rows = []
    prev = start
    for i, c in enumerate(close):
        wick = abs(rng.normal(0.0, 0.0004)) * c
        hi = max(prev, c) + wick
        lo = min(prev, c) - wick
        rows.append((prev, hi, lo, c))
        prev = c
    return make_bars(rows)


def test_label_matrix_matches_per_spec_labelling_exactly():
    bars = _random_walk(600)
    specs = [
        BarrierSpec(
            net_target_pct=t,
            stop_kind=kind,
            stop_value=value,
            horizon_minutes=h,
            round_trip_bps=4.5,
        )
        for t, kind, value, h in (
            (0.10, "atr", 2.0, 5),
            (0.25, "vol", 3.0, 25),
            (0.50, "structure", 20.0, 40),
            (0.15, "fixed_net", 30.0, 15),
        )
    ]
    matrix = label_matrix(bars, specs=specs, symbol=SYMBOL)

    assert set(matrix) == {s.key() for s in specs}
    for spec in specs:
        assert matrix[spec.key()] == build_labels(bars, spec=spec, symbol=SYMBOL)


def test_class_balance_is_reported_and_is_not_degenerate_on_a_realistic_path():
    bars = _random_walk(4000)
    # A symmetric geometry: 19.5 bps gross up, 19.5 bps gross down.
    spec = BarrierSpec(
        net_target_pct=0.15,
        stop_kind="fixed_net",
        stop_value=24.0,
        horizon_minutes=25,
        round_trip_bps=4.5,
    )
    assert spec.gross_target_bps == pytest.approx(spec.stop_value - spec.round_trip_bps)
    labels = build_labels(bars, spec=spec, symbol=SYMBOL)
    counts = class_balance(labels)

    assert sum(counts.values()) == len(bars)
    usable = sum(counts[o] for o in (Outcome.TP_BEFORE_SL, Outcome.SL_BEFORE_TP, Outcome.TIMEOUT))
    assert usable > 0.8 * len(bars)
    for outcome in (Outcome.TP_BEFORE_SL, Outcome.SL_BEFORE_TP):
        assert counts[outcome] > 0.05 * usable, f"{outcome} is degenerate: {counts}"
    # A symmetric random walk must not produce a lopsided win rate.
    wins = counts[Outcome.TP_BEFORE_SL]
    losses = counts[Outcome.SL_BEFORE_TP]
    assert 0.4 < wins / (wins + losses) < 0.6


def test_no_edge_labels_are_flagged_and_can_be_excluded():
    bars = _random_walk(200)
    spec = BarrierSpec(
        net_target_pct=0.15,
        stop_kind="atr",
        stop_value=2.0,
        horizon_minutes=10,
        round_trip_bps=4.5,
    )
    with_ne = build_labels(bars, spec=spec, symbol=SYMBOL)
    without = build_labels(bars, spec=spec, symbol=SYMBOL, include_no_edge=False)

    assert len(without) < len(with_ne)
    assert all(lbl.usable for lbl in without)
    assert all(lbl.outcome is not Outcome.NO_EDGE for lbl in without)


# --------------------------------------------------------------------------- #
# The volatility series the stops are built from
# --------------------------------------------------------------------------- #


def test_atr_series_is_zero_until_wilder_is_seeded_then_matches_the_true_range():
    rows = [(100.0, 100.5, 99.5, 100.0) for _ in range(30)]
    atr = atr_bps_series(make_bars(rows))

    assert atr[:14].tolist() == [0.0] * 14     # bar 0 has no TR; 14 TRs seed it
    # Every true range is 1.0 on a 100.0 close -> 100 bps.
    assert atr[14] == pytest.approx(100.0)
    assert atr[-1] == pytest.approx(100.0)


def test_realized_vol_series_is_zero_until_the_window_is_full():
    bars = _random_walk(100)
    vol = realized_vol_bps_series(bars)

    assert vol[:30].tolist() == [0.0] * 30
    assert vol[30] > 0.0
    assert all(math.isfinite(v) for v in vol)


def test_a_vol_stop_scales_with_the_square_root_of_the_horizon():
    bars = _random_walk(400)
    short = BarrierSpec(
        net_target_pct=0.15, stop_kind="vol", stop_value=3.0,
        horizon_minutes=25, round_trip_bps=4.5,
    )
    long = BarrierSpec(
        net_target_pct=0.15, stop_kind="vol", stop_value=3.0,
        horizon_minutes=100, round_trip_bps=4.5,
    )
    arrays = resolve_specs(bars, specs=[short, long])
    i = 200
    short_dist = arrays[short.key()].entry_price[i] - arrays[short.key()].sl_price[i]
    long_dist = arrays[long.key()].entry_price[i] - arrays[long.key()].sl_price[i]

    assert long_dist / short_dist == pytest.approx(math.sqrt(100 / 25), rel=1e-9)


# --------------------------------------------------------------------------- #
# The vectorised walk against a naive reference
# --------------------------------------------------------------------------- #


def _reference_labels(bars, spec: BarrierSpec, atr: np.ndarray, vol: np.ndarray):
    """The same rules written the slow, obvious way.  No numpy cleverness.

    If this and ``resolve_specs`` ever disagree, the vectorised walk is wrong:
    this version is the readable statement of the contract.
    """
    out = []
    n = len(bars)
    scale = math.sqrt(spec.horizon_minutes)
    lows = [b.low for b in bars]
    for i, bar in enumerate(bars):
        entry = bar.close
        horizon_end = bar.close_ms + spec.horizon_ms
        if spec.stop_kind == "atr":
            stop_bps = spec.stop_value * atr[i] * scale
        elif spec.stop_kind == "vol":
            stop_bps = spec.stop_value * vol[i] * scale
        elif spec.stop_kind == "structure":
            window = int(round(spec.stop_value))
            swing = min(lows[max(0, i - window + 1) : i + 1])
            stop_bps = (entry - swing) / entry * 10_000.0
        else:
            stop_bps = spec.stop_value - spec.round_trip_bps
        tp = entry * (1 + spec.gross_target_bps / 10_000.0)
        sl = entry * (1 - stop_bps / 10_000.0)

        window_bars = [b for b in bars[i + 1 :] if b.close_ms <= horizon_end]
        if entry <= 0 or stop_bps <= 0 or not window_bars:
            out.append((Outcome.NO_EDGE, 0, bar.close_ms, 0.0))
            continue

        outcome, hit_ms, net = None, 0, 0.0
        for b in window_bars:
            if b.low <= sl:
                fill = min(sl, b.open)
                outcome = Outcome.SL_BEFORE_TP
                hit_ms = b.close_ms
                net = (fill - entry) / entry * 10_000.0 - spec.round_trip_bps
                break
            if b.high >= tp:
                outcome = Outcome.TP_BEFORE_SL
                hit_ms = b.close_ms
                net = spec.net_target_bps
                break
        if outcome is None:
            if horizon_end > bars[-1].close_ms:
                out.append((Outcome.NO_EDGE, 0, bar.close_ms, 0.0))
                continue
            outcome = Outcome.TIMEOUT
            net = (window_bars[-1].close - entry) / entry * 10_000.0 - spec.round_trip_bps
            out.append((outcome, 0, horizon_end, net))
            continue
        out.append((outcome, hit_ms, hit_ms, net))
    return out


@pytest.mark.parametrize(
    "kind,value",
    [("atr", 2.0), ("vol", 3.0), ("structure", 20.0), ("fixed_net", 30.0)],
)
def test_the_vectorised_walk_matches_a_naive_reference_bar_by_bar(kind, value):
    bars = _random_walk(1200, seed=3)
    atr = atr_bps_series(bars)
    vol = realized_vol_bps_series(bars)
    spec = BarrierSpec(
        net_target_pct=0.20,
        stop_kind=kind,
        stop_value=value,
        horizon_minutes=20,
        round_trip_bps=4.5,
    )
    labels = build_labels(bars, spec=spec, symbol=SYMBOL)
    expected = _reference_labels(bars, spec, atr, vol)

    assert len(labels) == len(expected)
    for label, (outcome, hit_ms, end_ms, net) in zip(labels, expected):
        assert label.outcome is outcome, label.ts_ms
        assert label.hit_ms == hit_ms, label.ts_ms
        assert label.label_end_ms == end_ms, label.ts_ms
        assert label.net_return_bps == pytest.approx(net, abs=1e-9), label.ts_ms


def test_a_ragged_series_respects_the_horizon_in_milliseconds_not_in_bars():
    """Bars are not guaranteed to be one minute apart; the window is a clock."""
    bars = make_bars([flat(100.0)] * 3)
    gapped = list(bars[:2]) + [
        Bar(
            symbol=SYMBOL,
            open_ms=bars[2].open_ms + 10 * MINUTE,
            close_ms=bars[2].close_ms + 10 * MINUTE,
            open=100.0,
            high=200.0,
            low=100.0,
            close=200.0,
            volume=1.0,
            quote_volume=100.0,
            trades=1,
            taker_buy_quote=0.0,
        )
    ]
    spec = BarrierSpec(
        net_target_pct=0.15,
        stop_kind="fixed_net",
        stop_value=25.0,
        horizon_minutes=2,
        round_trip_bps=5.0,
    )
    label = build_labels(gapped, spec=spec, symbol=SYMBOL)[0]

    # The 2-minute window closes before the gapped bar, so its spike is invisible.
    assert label.outcome is Outcome.TIMEOUT
    assert label.label_end_ms == gapped[0].close_ms + 2 * MINUTE
