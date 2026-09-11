"""Chronological splits, purging and the embargo.

The tests that matter here are the boundary ones: a label that ends one
millisecond before the cut is kept, a label that ends *on* the cut is dropped,
and the counts on the ``Split`` say exactly how many of each there were.
"""

from __future__ import annotations

import pytest

from app.predict.labels import BarrierSpec, Label, Outcome, build_labels
from app.predict.splits import MS_PER_DAY, MS_PER_MINUTE, Split, apply_purge, rolling_splits

from tests.test_predict_labels import SYMBOL, _random_walk

DAY = MS_PER_DAY
MINUTE = MS_PER_MINUTE
T0 = 1_700_000_000_000


def label_at(ts_ms: int, end_ms: int | None = None, *, key: str = "spec") -> Label:
    return Label(
        symbol=SYMBOL,
        ts_ms=int(ts_ms),
        spec_key=key,
        outcome=Outcome.TIMEOUT,
        net_return_bps=0.0,
        hit_ms=0,
        entry_price=100.0,
        tp_price=100.2,
        sl_price=99.8,
        label_end_ms=int(ts_ms if end_ms is None else end_ms),
    )


def simple_split(*, train_days=10, validate_days=2, test_days=2) -> Split:
    train = (T0, T0 + train_days * DAY)
    validate = (train[1], train[1] + validate_days * DAY)
    test = (validate[1], validate[1] + test_days * DAY)
    return Split(name="fixture", train=train, validate=validate, test=test)


# --------------------------------------------------------------------------- #
# rolling_splits
# --------------------------------------------------------------------------- #


def test_windows_are_chronological_contiguous_and_move_forward():
    splits = rolling_splits(
        T0,
        T0 + 120 * DAY,
        train_days=30,
        validate_days=7,
        test_days=7,
        step_days=14,
        max_horizon_minutes=90,
        embargo_minutes=60,
    )

    assert splits, "120 days must fit at least one 30/7/7 window"
    for split in splits:
        assert split.train[0] < split.train[1] == split.validate[0]
        assert split.validate[0] < split.validate[1] == split.test[0]
        assert split.test[0] < split.test[1]
        assert split.train[1] - split.train[0] == 30 * DAY
        assert split.validate[1] - split.validate[0] == 7 * DAY
        assert split.test[1] - split.test[0] == 7 * DAY

    for earlier, later in zip(splits, splits[1:]):
        assert later.train[0] == earlier.train[0] + 14 * DAY
        assert later.test[0] > earlier.test[0]

    assert splits[-1].test[1] <= T0 + 120 * DAY


def test_rolling_splits_is_deterministic():
    kwargs = dict(
        train_days=30,
        validate_days=7,
        test_days=7,
        step_days=7,
        max_horizon_minutes=90,
        embargo_minutes=30,
    )
    first = rolling_splits(T0, T0 + 120 * DAY, **kwargs)
    second = rolling_splits(T0, T0 + 120 * DAY, **kwargs)

    assert first == second
    assert [s.name for s in first] == [f"split{i:02d}" for i in range(len(first))]


def test_a_truncated_final_window_is_not_emitted():
    splits = rolling_splits(
        T0,
        T0 + 60 * DAY,
        train_days=30,
        validate_days=7,
        test_days=7,
        step_days=7,
        max_horizon_minutes=60,
        embargo_minutes=0,
    )
    # 44 days of span stepping 7 days fits three times inside 60 days; a fourth
    # would end at day 65 and is not emitted rather than being truncated.
    assert len(splits) == 3
    assert all(s.test[1] <= T0 + 60 * DAY for s in splits)
    assert splits[-1].test[1] + 7 * DAY > T0 + 60 * DAY


@pytest.mark.parametrize(
    "kwargs, match",
    [
        (dict(train_days=0.0001, max_horizon_minutes=1440), "purging"),
        (dict(validate_days=0.02, max_horizon_minutes=60, embargo_minutes=60), "band"),
        (dict(test_days=0.02, embargo_minutes=120), "embargo"),
        (dict(train_days=200.0), "available"),
        (dict(step_days=0), "step_days"),
        (dict(embargo_minutes=-1), "embargo_minutes"),
        (dict(max_horizon_minutes=0), "max_horizon_minutes"),
    ],
)
def test_geometry_that_cannot_survive_purging_is_refused(kwargs, match):
    base = dict(
        train_days=30.0,
        validate_days=7.0,
        test_days=7.0,
        step_days=7.0,
        max_horizon_minutes=90,
        embargo_minutes=0,
    )
    base.update(kwargs)
    with pytest.raises(ValueError, match=match):
        rolling_splits(T0, T0 + 120 * DAY, **base)


def test_an_inverted_range_is_refused():
    with pytest.raises(ValueError, match="after"):
        rolling_splits(
            T0 + DAY,
            T0,
            train_days=1,
            validate_days=1,
            test_days=1,
            step_days=1,
            max_horizon_minutes=5,
        )


# --------------------------------------------------------------------------- #
# apply_purge — the part that makes or breaks the whole phase
# --------------------------------------------------------------------------- #


def test_a_training_label_whose_window_overlaps_the_evaluation_data_is_dropped():
    split = simple_split()
    boundary = split.validate[0]

    into_validate = label_at(boundary - 30 * MINUTE, boundary + 30 * MINUTE)
    keeps = label_at(boundary - 30 * MINUTE, boundary - MINUTE)
    train, _, _ = apply_purge(
        [into_validate, keeps], split, max_horizon_minutes=60, embargo_minutes=0
    )

    assert train == [keeps]
    assert split.purged == 1
    assert split.embargoed == 0


def test_a_training_label_whose_window_reaches_the_test_window_is_dropped():
    """A horizon long enough to jump the whole validation window still leaks."""
    split = simple_split()
    horizon = 3 * 24 * 60           # three days, longer than validate + test
    into_test = label_at(split.validate[0] - 30 * MINUTE, split.test[0] + MINUTE)
    keeps = label_at(split.validate[0] - 30 * MINUTE, split.validate[0] - MINUTE)

    train, _, _ = apply_purge(
        [into_test, keeps], split, max_horizon_minutes=horizon, embargo_minutes=0
    )

    assert train == [keeps]
    assert split.purged == 1


def test_a_training_label_that_closes_before_the_boundary_is_kept():
    split = simple_split()
    boundary = split.validate[0]
    label = label_at(boundary - 90 * MINUTE, boundary - 1)

    train, _, _ = apply_purge(
        [label], split, max_horizon_minutes=90, embargo_minutes=0
    )

    assert train == [label]
    assert split.purged == 0


def test_the_boundary_case_is_decided_a_window_closing_on_the_cut_is_purged():
    """One millisecond decides it, and the decision is written down.

    A label whose window closes at *exactly* the first instant of validation
    observed that instant.  "Only just touched it" is still touched, so it goes.
    """
    split = simple_split()
    boundary = split.validate[0]

    on_the_cut = label_at(boundary - 60 * MINUTE, boundary)
    one_ms_earlier = label_at(boundary - 60 * MINUTE, boundary - 1)

    train, _, _ = apply_purge(
        [on_the_cut, one_ms_earlier], split, max_horizon_minutes=60, embargo_minutes=0
    )

    assert train == [one_ms_earlier]
    assert split.purged == 1


def test_a_validation_label_reaching_into_test_is_purged_too():
    split = simple_split()
    boundary = split.test[0]
    reaches = label_at(boundary - 10 * MINUTE, boundary + MINUTE)
    clean = label_at(boundary - 10 * MINUTE, boundary - MINUTE)

    _, validate, _ = apply_purge(
        [reaches, clean], split, max_horizon_minutes=60, embargo_minutes=0
    )

    assert validate == [clean]
    assert split.purged == 1


def test_the_embargo_removes_the_band_after_each_boundary_and_counts_it():
    split = simple_split()
    embargo = 60  # minutes

    inside_validate_band = label_at(split.validate[0] + 10 * MINUTE)
    outside_validate_band = label_at(split.validate[0] + 61 * MINUTE)
    inside_test_band = label_at(split.test[0] + 59 * MINUTE)
    outside_test_band = label_at(split.test[0] + 120 * MINUTE)
    train_row = label_at(split.train[0] + DAY)

    rows = [
        train_row,
        inside_validate_band,
        outside_validate_band,
        inside_test_band,
        outside_test_band,
    ]
    train, validate, test = apply_purge(
        rows, split, max_horizon_minutes=90, embargo_minutes=embargo
    )

    assert train == [train_row]
    assert validate == [outside_validate_band]
    assert test == [outside_test_band]
    assert split.embargoed == 2
    assert split.purged == 0
    assert split.dropped == 2


def test_the_first_label_exactly_on_the_embargo_edge_is_kept():
    split = simple_split()
    edge = label_at(split.test[0] + 60 * MINUTE)
    just_inside = label_at(split.test[0] + 60 * MINUTE - 1)

    _, _, test = apply_purge(
        [edge, just_inside], split, max_horizon_minutes=90, embargo_minutes=60
    )

    assert test == [edge]
    assert split.embargoed == 1


def test_labels_outside_every_window_are_dropped_without_being_counted():
    split = simple_split()
    before = label_at(split.train[0] - DAY)
    after = label_at(split.test[1] + DAY)
    inside = label_at(split.train[0] + DAY)

    train, validate, test = apply_purge(
        [before, inside, after], split, max_horizon_minutes=90, embargo_minutes=0
    )

    assert (train, validate, test) == ([inside], [], [])
    assert split.purged == 0
    assert split.embargoed == 0


def test_input_order_is_preserved():
    split = simple_split()
    rows = [label_at(split.train[0] + i * DAY) for i in (3, 1, 2)]
    train, _, _ = apply_purge(rows, split, max_horizon_minutes=90, embargo_minutes=0)
    assert train == rows


def test_a_label_longer_than_the_split_horizon_is_a_contract_error():
    split = simple_split()
    too_long = label_at(split.train[0] + DAY, split.train[0] + DAY + 120 * MINUTE)

    with pytest.raises(ValueError, match="max horizon"):
        apply_purge([too_long], split, max_horizon_minutes=90, embargo_minutes=0)


def test_accounting_fields_do_not_change_split_identity():
    a = simple_split()
    b = simple_split()
    apply_purge([label_at(a.validate[0] - MINUTE, a.test[0] + MINUTE)], a,
                max_horizon_minutes=3 * 24 * 60, embargo_minutes=0)

    assert a.purged == 1
    assert b.purged == 0
    assert a == b                      # purged/embargoed are accounting, not identity
    assert hash(a) == hash(b)


# --------------------------------------------------------------------------- #
# End to end, against real labels
# --------------------------------------------------------------------------- #


def test_no_real_label_survives_purging_with_a_window_into_the_next_segment():
    bars = _random_walk(6000, seed=11)
    spec = BarrierSpec(
        net_target_pct=0.25,
        stop_kind="fixed_net",
        stop_value=34.5,
        horizon_minutes=60,
        round_trip_bps=4.5,
    )
    labels = build_labels(bars, spec=spec, symbol=SYMBOL, include_no_edge=False)
    assert labels

    start = bars[0].close_ms
    split = Split(
        name="e2e",
        train=(start, start + 3000 * MINUTE),
        validate=(start + 3000 * MINUTE, start + 4500 * MINUTE),
        test=(start + 4500 * MINUTE, start + 6000 * MINUTE),
    )
    train, validate, test = apply_purge(
        labels, split, max_horizon_minutes=60, embargo_minutes=30
    )

    assert train and validate and test
    assert all(lbl.label_end_ms < split.validate[0] for lbl in train)
    assert all(lbl.label_end_ms < split.test[0] for lbl in validate)
    assert all(lbl.ts_ms >= split.validate[0] + 30 * MINUTE for lbl in validate)
    assert all(lbl.ts_ms >= split.test[0] + 30 * MINUTE for lbl in test)
    assert split.purged > 0 and split.embargoed > 0
    # Nothing is invented: every kept row was in the input.
    kept = {id(lbl) for lbl in train + validate + test}
    assert kept.issubset({id(lbl) for lbl in labels})
