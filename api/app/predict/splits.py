"""Chronological train/validate/test windows, with purging and an embargo.

Two rules make the difference between a result and a leak, and both live here.

**Purge.**  A triple-barrier label looks *forward*, up to its horizon.  So a
training sample whose ``label_end_ms`` reaches into the validation or the test
window was decided by prices the model is about to be scored on.  It has seen
the future.  It is dropped.  This is not a rounding detail: at a 90-minute
horizon on 1-minute bars, the last 90 training samples before every boundary
are contaminated, and they are exactly the samples nearest the evaluation data,
which is where an overfit model does its best work.

**Embargo.**  Overlap is not the only leak.  The sample at ``boundary - 1m`` and
the sample at ``boundary + 1m`` share nearly all of their features and most of
their path; serial correlation walks straight across an instantaneous cut.  So
a band of ``embargo_minutes`` immediately **after** each boundary is removed as
well.  It is removed from the *later* segment — the validation and test sides —
because shrinking the evaluation set is honest, while trimming the training set
and keeping the correlated evaluation rows would flatter the score.

Both counts are recorded on the :class:`Split` so the report can state them
instead of asserting that purging happened.

Conventions
-----------

* Every window is **half-open**, ``[lo, hi)``.  A label at exactly ``hi``
  belongs to the next window, never to both.
* A training label whose window closes at *exactly* the validation boundary is
  **purged**.  Its last observed price is the first instant of validation, and
  "it only just touched it" is still touching it.
* Windows move forward by ``step_days``; nothing is ever shuffled, sampled, or
  reordered.  There is no random state in this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence, TypeVar

__all__ = ["Split", "rolling_splits", "apply_purge", "MS_PER_DAY", "MS_PER_MINUTE"]

MS_PER_MINUTE = 60_000
MS_PER_DAY = 86_400_000

T = TypeVar("T")


@dataclass(frozen=True)
class Split:
    """One walk-forward window.  ``purged``/``embargoed`` are filled by
    :func:`apply_purge` — they are accounting, not identity, so they are
    excluded from equality and hashing."""

    name: str
    train: tuple[int, int]
    validate: tuple[int, int]
    test: tuple[int, int]
    purged: int = field(default=0, compare=False)
    embargoed: int = field(default=0, compare=False)

    @property
    def start_ms(self) -> int:
        return self.train[0]

    @property
    def end_ms(self) -> int:
        return self.test[1]

    @property
    def dropped(self) -> int:
        return int(self.purged) + int(self.embargoed)

    def _record(self, *, purged: int, embargoed: int) -> None:
        """Write the accounting fields.  Frozen for callers, not for the one
        function whose job is to compute them."""
        object.__setattr__(self, "purged", int(purged))
        object.__setattr__(self, "embargoed", int(embargoed))


def _window(lo: int, days: float) -> tuple[int, int]:
    return (int(lo), int(lo + round(days * MS_PER_DAY)))


def rolling_splits(
    start_ms: int,
    end_ms: int,
    *,
    train_days: float,
    validate_days: float,
    test_days: float,
    step_days: float,
    max_horizon_minutes: int,
    embargo_minutes: int = 0,
) -> list[Split]:
    """Walk-forward windows across ``[start_ms, end_ms)``.  Deterministic.

    The windows are contiguous — train, then validate, then test, with no gap —
    because the gap is *not* a fixed band: it is whatever each label's own
    horizon needs, and :func:`apply_purge` removes exactly that.

    ``max_horizon_minutes`` and ``embargo_minutes`` are still required here so
    the geometry can be refused up front: a segment shorter than the band that
    purging and the embargo will remove from it is not a window, it is an empty
    set with a name.  A window that would run past ``end_ms`` is not emitted —
    a truncated test window is a smaller sample pretending to be a full one.
    """
    start_ms = int(start_ms)
    end_ms = int(end_ms)
    if end_ms <= start_ms:
        raise ValueError(f"end_ms ({end_ms}) must be after start_ms ({start_ms})")
    for name, value in (
        ("train_days", train_days),
        ("validate_days", validate_days),
        ("test_days", test_days),
        ("step_days", step_days),
    ):
        if value <= 0:
            raise ValueError(f"{name} must be positive, got {value!r}")
    if max_horizon_minutes <= 0:
        raise ValueError(
            f"max_horizon_minutes must be positive, got {max_horizon_minutes!r}"
        )
    if embargo_minutes < 0:
        raise ValueError(
            f"embargo_minutes must not be negative, got {embargo_minutes!r}"
        )

    horizon_ms = int(max_horizon_minutes) * MS_PER_MINUTE
    embargo_ms = int(embargo_minutes) * MS_PER_MINUTE

    train_ms = int(round(train_days * MS_PER_DAY))
    validate_ms = int(round(validate_days * MS_PER_DAY))
    test_ms = int(round(test_days * MS_PER_DAY))

    if train_ms <= horizon_ms:
        raise ValueError(
            f"train_days={train_days} is {train_ms}ms, which purging at a "
            f"{max_horizon_minutes}m horizon ({horizon_ms}ms) would consume entirely"
        )
    if validate_ms <= horizon_ms + embargo_ms:
        raise ValueError(
            f"validate_days={validate_days} is {validate_ms}ms, shorter than the "
            f"purge+embargo band ({horizon_ms + embargo_ms}ms) it has to survive"
        )
    if test_ms <= embargo_ms:
        raise ValueError(
            f"test_days={test_days} is {test_ms}ms, shorter than the "
            f"{embargo_ms}ms embargo that is removed from its head"
        )

    step_ms = int(round(step_days * MS_PER_DAY))
    span = train_ms + validate_ms + test_ms
    if start_ms + span > end_ms:
        raise ValueError(
            f"the window needs {span}ms but only {end_ms - start_ms}ms of data "
            "is available; shorten train/validate/test or widen the range"
        )

    splits: list[Split] = []
    cursor = start_ms
    i = 0
    while cursor + span <= end_ms:
        train = (cursor, cursor + train_ms)
        validate = (train[1], train[1] + validate_ms)
        test = (validate[1], validate[1] + test_ms)
        splits.append(
            Split(name=f"split{i:02d}", train=train, validate=validate, test=test)
        )
        cursor += step_ms
        i += 1
    return splits


def _ts_and_end(row: Any) -> tuple[int, int]:
    ts = int(getattr(row, "ts_ms"))
    end = int(getattr(row, "label_end_ms", ts) or ts)
    return ts, max(ts, end)


def apply_purge(
    labels: Sequence[T],
    split: Split,
    *,
    max_horizon_minutes: int,
    embargo_minutes: int,
) -> tuple[list[T], list[T], list[T]]:
    """Split ``labels`` into ``(train, validate, test)``, purged and embargoed.

    Input order is preserved.  Anything outside all three windows is simply not
    in this split and is dropped without being counted as purged — it was never
    contaminated, it is just somewhere else in time.

    ``max_horizon_minutes`` is a contract check, not a parameter: a label whose
    own window is longer than the horizon the split was built for means the two
    were built from different specs, and every purge decision made here would
    be wrong.  That raises rather than silently under-purging.
    """
    if max_horizon_minutes <= 0:
        raise ValueError(
            f"max_horizon_minutes must be positive, got {max_horizon_minutes!r}"
        )
    if embargo_minutes < 0:
        raise ValueError(
            f"embargo_minutes must not be negative, got {embargo_minutes!r}"
        )

    horizon_ms = int(max_horizon_minutes) * MS_PER_MINUTE
    embargo_ms = int(embargo_minutes) * MS_PER_MINUTE

    train_lo, train_hi = split.train
    val_lo, val_hi = split.validate
    test_lo, test_hi = split.test

    train: list[T] = []
    validate: list[T] = []
    test: list[T] = []
    purged = 0
    embargoed = 0

    for row in labels:
        ts, end = _ts_and_end(row)
        if end - ts > horizon_ms:
            raise ValueError(
                f"label at {ts} closes at {end}, {end - ts}ms later, which exceeds "
                f"the {horizon_ms}ms max horizon this split was built for; the "
                "labels and the split disagree about the geometry"
            )

        if train_lo <= ts < train_hi:
            # Looks into validation (or beyond) -> it has seen the future.
            if end >= val_lo:
                purged += 1
                continue
            train.append(row)
        elif val_lo <= ts < val_hi:
            if ts < val_lo + embargo_ms:
                embargoed += 1
                continue
            if end >= test_lo:
                purged += 1
                continue
            validate.append(row)
        elif test_lo <= ts < test_hi:
            if ts < test_lo + embargo_ms:
                embargoed += 1
                continue
            test.append(row)
        # else: outside this split entirely.

    split._record(purged=purged, embargoed=embargoed)
    return train, validate, test
