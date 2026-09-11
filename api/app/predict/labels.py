"""Triple-barrier labelling that is **NET of costs**, multi-horizon, leak-free.

Everything downstream — the feature matrix, the five benchmarked models, the
calibration curve, the EV gate — learns from what this module calls a winner.
So the one thing that must never happen is a label that says ``TP_BEFORE_SL``
on a move that does not clear its own round trip.  That would teach every model
in Phase 4 to trade unprofitably, and it would do it silently.

The arithmetic, spelled out
---------------------------

``BarrierSpec.net_target_pct`` is a **NET** number: ``0.15`` means *0.15% in the
pocket after the entry fee, the exit fee, the spread and the slippage*.  The
price that actually triggers the take-profit therefore sits further away than
0.15% by exactly ``round_trip_bps``::

    gross_tp_bps = net_target_pct * 100 + round_trip_bps
    tp_price     = entry * (1 + gross_tp_bps / 10_000)

and the loss side works the other way — costs make a loss *worse*, so the NET
loss realised at the stop is the gross drop **plus** the round trip::

    sl_price   = entry * (1 - stop_gross_bps / 10_000)
    net_at_sl  = -(stop_gross_bps + round_trip_bps)

Which is why ``stop_kind="fixed_net"`` puts its stop *closer* than its nominal
band (``gross = stop_value - round_trip_bps``) while the take-profit sits
*further* than its nominal target.  The asymmetry is the cost, and it is the
whole point of this module.

Conventions that the rest of the codebase already uses and that are mirrored
here rather than re-invented:

* **Long only.**  V1 trades spot; a short label would need a borrow model that
  does not exist.  The geometry is symmetric if a short side is ever added.
* **Entry at the close of the decision bar.**  ``Label.ts_ms`` is that bar's
  ``close_ms`` — the instant the decision is made *and* the instant the entry
  price is observed.  The forward walk starts at the next bar, so a label can
  never see a price that the decision did not.
* **The stop wins a tie.**  When both barriers lie inside the same bar, the
  bar data cannot say which came first, so the **stop** is assumed to have hit
  first.  ``app.exits`` already resolves the same ambiguity the same way.
* **ATR is horizon-scaled with ``sqrt``**: ``mult x 1m-ATR x sqrt(horizon_min)``,
  identical to ``app.research.tournament`` and ``app.profit``.
* **Fills are never optimistic.**  A take-profit fills at ``tp_price`` even if
  the bar opened above it; a stop fills at ``min(sl_price, bar.open)``, because
  a bar that *opens* through the stop could not have filled at the stop.

When ``NO_EDGE`` is emitted — exhaustively
------------------------------------------

``NO_EDGE`` means *the barrier geometry is unreachable given the data*, never
"nothing happened".  "Nothing happened" is ``TIMEOUT``.  A silent ``NO_EDGE``
that should have been a ``TIMEOUT`` would quietly distort the class balance, so
the list is closed and every case is counted separately in
:attr:`OutcomeArrays.no_edge_reason`:

``EMPTY_WINDOW``
    No bar closes inside ``(ts_ms, ts_ms + horizon]``.  The last bar of the
    series always lands here.
``BAD_PRICE``
    The entry price is non-positive or non-finite.
``DEGENERATE_STOP``
    The stop distance resolves to zero or less: the ATR/vol warm-up has not
    finished, the realized-vol window is not full, the recent swing is flat,
    or a ``fixed_net`` band is entirely eaten by its own round trip.  A
    zero-width stop would stop out instantly and label the entry a loser, so
    it is refused instead.
``CENSORED``
    The horizon runs past the end of the data **and** no barrier was touched
    inside the bars that do exist.  The outcome is genuinely unknown — calling
    it a ``TIMEOUT`` would be inventing a result — so the last ``horizon``
    minutes of any series produce ``NO_EDGE`` rather than free negatives.
``UNREACHABLE``
    Only when the caller supplies ``max_gross_move_bps``: the gross move the
    target needs exceeds what the caller says the instrument does at this
    horizon.  Never inferred from the path, because inferring it from the path
    is how ``TIMEOUT`` turns into ``NO_EDGE`` by accident.

The cost floor
--------------

``docs/PHASE3_REPORT.md`` measured it: **NET targets below ~10 bps produced
literally zero trades in 120 days** on Binance.US — they never clear their own
round trip.  So a ``BarrierSpec`` refuses, at construction, both

* a net target below its own ``round_trip_bps``, and
* a net target below :data:`MIN_NET_TARGET_BPS` (10 bps).

Loudly, with a ``ValueError``, because a spec that cannot pay for itself is not
a research question, it is a bug.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np

from app.types import Bar

__all__ = [
    "Outcome",
    "NoEdgeReason",
    "BarrierSpec",
    "Label",
    "OutcomeArrays",
    "DEFAULT_NET_TARGET_PCTS",
    "DEFAULT_HORIZONS_MINUTES",
    "DEFAULT_ROUND_TRIP_BPS",
    "MIN_NET_TARGET_BPS",
    "STOP_KINDS",
    "default_specs",
    "atr_bps_series",
    "realized_vol_bps_series",
    "build_labels",
    "label_matrix",
    "resolve_specs",
    "class_balance",
]


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #


class Outcome(str, enum.Enum):
    TP_BEFORE_SL = "TP_BEFORE_SL"
    SL_BEFORE_TP = "SL_BEFORE_TP"
    TIMEOUT = "TIMEOUT"
    NO_EDGE = "NO_EDGE"


class NoEdgeReason(str, enum.Enum):
    """Why a ``NO_EDGE`` was emitted.  Diagnostics, not part of ``Label``."""

    NONE = "NONE"
    EMPTY_WINDOW = "EMPTY_WINDOW"
    BAD_PRICE = "BAD_PRICE"
    DEGENERATE_STOP = "DEGENERATE_STOP"
    CENSORED = "CENSORED"
    UNREACHABLE = "UNREACHABLE"


#: Integer codes used inside the numpy walk; the enums above are the public face.
_TP, _SL, _TO, _NE = 0, 1, 2, 3
_OUTCOME_BY_CODE = (
    Outcome.TP_BEFORE_SL,
    Outcome.SL_BEFORE_TP,
    Outcome.TIMEOUT,
    Outcome.NO_EDGE,
)
_REASON_BY_CODE = (
    NoEdgeReason.NONE,
    NoEdgeReason.EMPTY_WINDOW,
    NoEdgeReason.BAD_PRICE,
    NoEdgeReason.DEGENERATE_STOP,
    NoEdgeReason.CENSORED,
    NoEdgeReason.UNREACHABLE,
)
_R_NONE, _R_EMPTY, _R_PRICE, _R_STOP, _R_CENSORED, _R_UNREACHABLE = 0, 1, 2, 3, 4, 5

#: The stop geometries this module understands.
STOP_KINDS: tuple[str, ...] = ("atr", "vol", "structure", "fixed_net")

#: The hard floor from ``docs/PHASE3_REPORT.md``.  Below this, 120 days of real
#: data produced zero trades: the target never clears its own round trip.
MIN_NET_TARGET_BPS = 10.0

#: The default NET ladder, in percent.  Starts at the floor and goes up.
DEFAULT_NET_TARGET_PCTS: tuple[float, ...] = (
    0.10, 0.15, 0.20, 0.25, 0.35, 0.50, 0.75, 1.00,
)

#: The horizons Phase 3 measured, minus the two it proved hopeless (2m, 5m is
#: kept because it is the boundary case the report has to show).
DEFAULT_HORIZONS_MINUTES: tuple[int, ...] = (5, 10, 15, 25, 40, 60, 90)

#: Binance.US Tier 0, as costed in Phase 3: 1 bps/side taker fee (2 bps), ~2 bps
#: slippage and ~0.5 bps of spread.  ``docs/ECONOMICS.md`` puts the round trip
#: at 3-6 bps; this is the middle of it.  Always pass the measured number when
#: one is available — this default exists so a spec is never silently free.
DEFAULT_ROUND_TRIP_BPS = 4.5

_MS_PER_MINUTE = 60_000
_BPS = 10_000.0

#: Wilder ATR period and realized-vol window, mirroring ``app.features``.
_ATR_PERIOD = 14
_RV_WINDOW = 30


def _fmt(value: float) -> str:
    text = f"{float(value):g}"
    return text


@dataclass(frozen=True)
class BarrierSpec:
    """One triple-barrier geometry.  Immutable, self-validating, hashable.

    ``net_target_pct``
        NET of every cost, in **percent**: ``0.15`` is 0.15% == 15 bps NET.
    ``stop_kind`` / ``stop_value``
        * ``"atr"`` — ``stop_value x 1m-ATR-bps x sqrt(horizon_minutes)``, a
          **gross** price distance.
        * ``"vol"`` — ``stop_value x realized-vol-bps x sqrt(horizon_minutes)``,
          gross, with realized vol the stdev of the last 30 one-minute log
          returns (``app.features`` convention).
        * ``"structure"`` — the recent swing: the lowest low of the last
          ``int(stop_value)`` bars including the entry bar, as a gross distance
          from the entry price.
        * ``"fixed_net"`` — a **NET** bps band.  The gross distance is
          ``stop_value - round_trip_bps``, because the costs are paid on the
          way out of a loser too.
    ``round_trip_bps``
        Entry fee + exit fee + spread + slippage, in bps.  The cost the label
        has to clear before it is allowed to call anything a winner.
    """

    net_target_pct: float
    stop_kind: str
    stop_value: float
    horizon_minutes: int
    round_trip_bps: float

    def __post_init__(self) -> None:
        if self.stop_kind not in STOP_KINDS:
            raise ValueError(
                f"unknown stop_kind {self.stop_kind!r}; expected one of {STOP_KINDS}"
            )
        for name in ("net_target_pct", "stop_value", "round_trip_bps"):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite, got {value!r}")
        if self.horizon_minutes <= 0:
            raise ValueError(
                f"horizon_minutes must be positive, got {self.horizon_minutes!r}"
            )
        if self.stop_value <= 0:
            raise ValueError(f"stop_value must be positive, got {self.stop_value!r}")
        if self.round_trip_bps < 0:
            raise ValueError(
                f"round_trip_bps must not be negative, got {self.round_trip_bps!r}"
            )

        net_bps = self.net_target_bps
        if net_bps <= 0:
            raise ValueError(
                f"net_target_pct must be positive, got {self.net_target_pct!r}"
            )
        # The two refusals that exist to stop a model learning to lose money.
        if net_bps + 1e-9 < float(self.round_trip_bps):
            raise ValueError(
                f"net target {net_bps:g} bps cannot clear its own round trip "
                f"{float(self.round_trip_bps):g} bps: the gross move required "
                f"({net_bps + float(self.round_trip_bps):g} bps) is more than "
                "double the payoff.  Refusing to label it — see docs/ECONOMICS.md."
            )
        if net_bps + 1e-9 < MIN_NET_TARGET_BPS:
            raise ValueError(
                f"net target {net_bps:g} bps is below the {MIN_NET_TARGET_BPS:g} bps "
                "floor; NET targets below ~10 bps produced literally zero trades in "
                "120 days on this venue (docs/PHASE3_REPORT.md).  Refusing."
            )
        if self.stop_kind == "fixed_net" and (
            float(self.stop_value) - float(self.round_trip_bps) <= 0
        ):
            raise ValueError(
                f"fixed_net stop band {float(self.stop_value):g} bps is entirely "
                f"consumed by the {float(self.round_trip_bps):g} bps round trip: the "
                "stop would sit at or above the entry price."
            )

    # -- derived ----------------------------------------------------------- #

    @property
    def net_target_bps(self) -> float:
        """The NET target in bps.  ``0.15`` percent -> ``15.0``."""
        return float(self.net_target_pct) * 100.0

    @property
    def gross_target_bps(self) -> float:
        """What price must actually do: the NET target **plus** the round trip."""
        return self.net_target_bps + float(self.round_trip_bps)

    @property
    def horizon_ms(self) -> int:
        return int(self.horizon_minutes) * _MS_PER_MINUTE

    def tp_price(self, entry_price: float) -> float:
        return float(entry_price) * (1.0 + self.gross_target_bps / _BPS)

    def key(self) -> str:
        return (
            f"x=net_fixed:{_fmt(self.net_target_bps)}"
            f"|s={self.stop_kind}:{_fmt(self.stop_value)}"
            f"|h={int(self.horizon_minutes)}"
            f"|rt={_fmt(self.round_trip_bps)}"
        )


@dataclass(frozen=True)
class Label:
    """One supervised example.  ``label_end_ms`` is what purging runs on."""

    symbol: str
    ts_ms: int
    spec_key: str
    outcome: Outcome
    net_return_bps: float
    hit_ms: int          # 0 == no barrier was touched
    entry_price: float
    tp_price: float
    sl_price: float
    label_end_ms: int

    @property
    def is_win(self) -> bool:
        return self.outcome is Outcome.TP_BEFORE_SL

    @property
    def usable(self) -> bool:
        """``NO_EDGE`` rows carry no supervision and must not be trained on."""
        return self.outcome is not Outcome.NO_EDGE


# --------------------------------------------------------------------------- #
# Default ladder
# --------------------------------------------------------------------------- #


def default_specs(
    *,
    round_trip_bps: float = DEFAULT_ROUND_TRIP_BPS,
    targets_pct: Sequence[float] = DEFAULT_NET_TARGET_PCTS,
    horizons_minutes: Sequence[int] = DEFAULT_HORIZONS_MINUTES,
    stop_kind: str = "atr",
    stop_value: float = 2.0,
) -> list[BarrierSpec]:
    """The ladder: every NET target x every horizon, one stop geometry.

    ``atr:2`` is the baseline stop Phase 3 ran, kept so the label study and the
    tournament are describing the same trade.
    """
    return [
        BarrierSpec(
            net_target_pct=float(t),
            stop_kind=stop_kind,
            stop_value=float(stop_value),
            horizon_minutes=int(h),
            round_trip_bps=float(round_trip_bps),
        )
        for h in horizons_minutes
        for t in targets_pct
    ]


# --------------------------------------------------------------------------- #
# Volatility series — causal, and matching app.features exactly
# --------------------------------------------------------------------------- #


def atr_bps_series(bars: Sequence[Bar]) -> np.ndarray:
    """Wilder ATR(14) in bps, one value per bar, using bars ``0..i`` only.

    Identical to ``app.features._fold_bar``: the first 14 true ranges seed the
    average, then Wilder smoothing runs; before the seed is full the ATR is
    ``0.0``, which callers must treat as *unknown* (it produces a
    ``DEGENERATE_STOP`` here, never a zero-width stop).
    """
    n = len(bars)
    out = np.zeros(n, dtype=np.float64)
    atr = 0.0
    ready = False
    seed: list[float] = []
    prev_close = 0.0
    for i, bar in enumerate(bars):
        close = float(bar.close)
        if not math.isfinite(close) or close <= 0:
            prev_close = 0.0
            continue
        if i > 0 and prev_close > 0:
            tr = max(
                float(bar.high) - float(bar.low),
                abs(float(bar.high) - prev_close),
                abs(float(bar.low) - prev_close),
            )
            tr = max(0.0, tr)
            if ready:
                atr += (tr - atr) / _ATR_PERIOD
            else:
                seed.append(tr)
                if len(seed) >= _ATR_PERIOD:
                    atr = sum(seed) / _ATR_PERIOD
                    ready = True
                    seed.clear()
        out[i] = (atr / close * _BPS) if atr > 0 else 0.0
        prev_close = close
    return out


def realized_vol_bps_series(bars: Sequence[Bar]) -> np.ndarray:
    """Stdev (ddof=1) of the last 30 one-minute log returns, in bps.

    ``0.0`` until 30 returns exist, which again means *unknown*, not *calm*.
    """
    n = len(bars)
    out = np.zeros(n, dtype=np.float64)
    if n < _RV_WINDOW + 1:
        return out
    close = np.array([float(b.close) for b in bars], dtype=np.float64)
    if np.any(close <= 0) or not np.all(np.isfinite(close)):
        # Fall back to a defensive loop-free path: a non-positive close makes
        # the log undefined, so the whole series is reported as unknown.
        return out
    logret = np.diff(np.log(close))
    windows = np.lib.stride_tricks.sliding_window_view(logret, _RV_WINDOW)
    out[_RV_WINDOW:] = windows.std(axis=1, ddof=1) * _BPS
    return out


def _rolling_min(values: np.ndarray, window: int) -> np.ndarray:
    """Lowest value over the trailing ``window`` samples, inclusive of ``i``."""
    window = max(1, int(window))
    if window == 1 or values.size == 0:
        return values.copy()
    out = np.empty_like(values)
    if values.size >= window:
        view = np.lib.stride_tricks.sliding_window_view(values, window)
        out[window - 1 :] = view.min(axis=1)
        head = min(window - 1, values.size)
        out[:head] = np.minimum.accumulate(values[:head])
    else:
        out[:] = np.minimum.accumulate(values)
    return out


# --------------------------------------------------------------------------- #
# The walk
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OutcomeArrays:
    """One spec's labels in columnar form.

    ``label_matrix`` over 120 days x 56 specs would materialise ~10M ``Label``
    objects and several GB; the array form is what the class-balance study and
    the feature matrix actually want.  :meth:`labels` converts when objects are
    genuinely needed.
    """

    spec: BarrierSpec
    ts_ms: np.ndarray
    outcome: np.ndarray          # int8 codes, see _OUTCOME_BY_CODE
    no_edge_reason: np.ndarray   # int8 codes, see _REASON_BY_CODE
    net_return_bps: np.ndarray
    hit_ms: np.ndarray
    entry_price: np.ndarray
    tp_price: np.ndarray
    sl_price: np.ndarray
    label_end_ms: np.ndarray

    @property
    def spec_key(self) -> str:
        return self.spec.key()

    def counts(self) -> dict[Outcome, int]:
        raw = np.bincount(self.outcome.astype(np.int64), minlength=4)
        return {_OUTCOME_BY_CODE[i]: int(raw[i]) for i in range(4)}

    def no_edge_counts(self) -> dict[NoEdgeReason, int]:
        raw = np.bincount(self.no_edge_reason.astype(np.int64), minlength=len(_REASON_BY_CODE))
        return {
            _REASON_BY_CODE[i]: int(raw[i])
            for i in range(len(_REASON_BY_CODE))
            if i != _R_NONE
        }

    def labels(self, symbol: str, *, include_no_edge: bool = True) -> list[Label]:
        key = self.spec_key
        out: list[Label] = []
        for i in range(self.ts_ms.size):
            code = int(self.outcome[i])
            if code == _NE and not include_no_edge:
                continue
            out.append(
                Label(
                    symbol=symbol,
                    ts_ms=int(self.ts_ms[i]),
                    spec_key=key,
                    outcome=_OUTCOME_BY_CODE[code],
                    net_return_bps=float(self.net_return_bps[i]),
                    hit_ms=int(self.hit_ms[i]),
                    entry_price=float(self.entry_price[i]),
                    tp_price=float(self.tp_price[i]),
                    sl_price=float(self.sl_price[i]),
                    label_end_ms=int(self.label_end_ms[i]),
                )
            )
        return out


@dataclass
class _SpecState:
    spec: BarrierSpec
    tp_price: np.ndarray
    sl_price: np.ndarray
    stop_gross_bps: np.ndarray
    steps: np.ndarray            # forward bars available inside the horizon
    horizon_end_ms: np.ndarray
    last_idx: np.ndarray
    complete: np.ndarray         # the horizon is covered by the data
    valid: np.ndarray
    reason: np.ndarray
    outcome: np.ndarray
    hit_idx: np.ndarray
    max_steps: int


def _price_arrays(bars: Sequence[Bar]) -> dict[str, np.ndarray]:
    n = len(bars)
    arrays = {
        "open": np.empty(n, dtype=np.float64),
        "high": np.empty(n, dtype=np.float64),
        "low": np.empty(n, dtype=np.float64),
        "close": np.empty(n, dtype=np.float64),
        "close_ms": np.empty(n, dtype=np.int64),
    }
    for i, bar in enumerate(bars):
        arrays["open"][i] = bar.open
        arrays["high"][i] = bar.high
        arrays["low"][i] = bar.low
        arrays["close"][i] = bar.close
        arrays["close_ms"][i] = bar.close_ms
    return arrays


def resolve_specs(
    bars: Sequence[Bar],
    *,
    specs: Sequence[BarrierSpec],
    atr_bps: np.ndarray | Sequence[float] | None = None,
    vol_bps: np.ndarray | Sequence[float] | None = None,
    max_gross_move_bps: float | None = None,
) -> dict[str, OutcomeArrays]:
    """Label every bar as a candidate entry, for every spec, in **one** walk.

    The forward walk is shared: bar ``i+k`` is read once per offset ``k`` and
    every still-unresolved spec is tested against it.  Nothing re-walks the
    path per spec.

    Raises if ``bars`` is not sorted by ``close_ms`` — a mis-ordered series
    would make ``searchsorted`` silently wrong, and silently wrong is the one
    failure mode this module cannot have.
    """
    specs = list(specs)
    if not specs:
        return {}
    seen: set[str] = set()
    for spec in specs:
        key = spec.key()
        if key in seen:
            raise ValueError(f"duplicate spec key {key!r}")
        seen.add(key)

    n = len(bars)
    if n == 0:
        raise ValueError("bars is empty: there is nothing to label")

    px = _price_arrays(bars)
    close_ms = px["close_ms"]
    if n > 1 and np.any(np.diff(close_ms) <= 0):
        raise ValueError("bars must be strictly increasing in close_ms")

    close = px["close"]
    high = px["high"]
    low = px["low"]
    open_ = px["open"]
    index = np.arange(n, dtype=np.int64)

    needs_atr = any(s.stop_kind == "atr" for s in specs)
    needs_vol = any(s.stop_kind == "vol" for s in specs)
    atr = (
        np.asarray(atr_bps, dtype=np.float64)
        if atr_bps is not None
        else (atr_bps_series(bars) if needs_atr else np.zeros(n))
    )
    vol = (
        np.asarray(vol_bps, dtype=np.float64)
        if vol_bps is not None
        else (realized_vol_bps_series(bars) if needs_vol else np.zeros(n))
    )
    if atr.shape != (n,) or vol.shape != (n,):
        raise ValueError("atr_bps/vol_bps must have one value per bar")

    price_ok = np.isfinite(close) & (close > 0)
    swing_cache: dict[int, np.ndarray] = {}

    states: list[_SpecState] = []
    for spec in specs:
        horizon_end = close_ms + spec.horizon_ms
        last_idx = np.searchsorted(close_ms, horizon_end, side="right") - 1
        steps = last_idx - index
        complete = horizon_end <= close_ms[-1]

        scale = math.sqrt(float(spec.horizon_minutes))
        if spec.stop_kind == "atr":
            stop_bps = float(spec.stop_value) * atr * scale
        elif spec.stop_kind == "vol":
            stop_bps = float(spec.stop_value) * vol * scale
        elif spec.stop_kind == "structure":
            window = max(1, int(round(float(spec.stop_value))))
            swing = swing_cache.get(window)
            if swing is None:
                swing = _rolling_min(low, window)
                swing_cache[window] = swing
            with np.errstate(invalid="ignore", divide="ignore"):
                stop_bps = np.where(
                    price_ok, (close - swing) / np.where(price_ok, close, 1.0) * _BPS, 0.0
                )
        else:  # fixed_net — the band is NET, so the gross stop sits closer
            stop_bps = np.full(
                n, float(spec.stop_value) - float(spec.round_trip_bps), dtype=np.float64
            )
        stop_bps = np.where(np.isfinite(stop_bps), stop_bps, 0.0)

        entry = np.where(price_ok, close, 1.0)
        tp_price = entry * (1.0 + spec.gross_target_bps / _BPS)
        sl_price = entry * (1.0 - stop_bps / _BPS)

        reason = np.zeros(n, dtype=np.int8)
        valid = np.ones(n, dtype=bool)

        if max_gross_move_bps is not None and spec.gross_target_bps > float(
            max_gross_move_bps
        ):
            valid[:] = False
            reason[:] = _R_UNREACHABLE
        else:
            bad_price = ~price_ok
            reason[bad_price] = _R_PRICE
            valid &= ~bad_price

            bad_stop = valid & ~(stop_bps > 0)
            reason[bad_stop] = _R_STOP
            valid &= ~bad_stop

            empty = valid & (steps <= 0)
            reason[empty] = _R_EMPTY
            valid &= ~empty

        outcome = np.where(valid, np.int8(_TO), np.int8(_NE)).astype(np.int8)
        states.append(
            _SpecState(
                spec=spec,
                tp_price=tp_price,
                sl_price=sl_price,
                stop_gross_bps=stop_bps,
                steps=steps,
                horizon_end_ms=horizon_end,
                last_idx=last_idx,
                complete=complete,
                valid=valid,
                reason=reason,
                outcome=outcome,
                hit_idx=np.full(n, -1, dtype=np.int64),
                max_steps=int(steps.max()) if n else 0,
            )
        )

    # --- the shared forward walk ------------------------------------------ #
    unresolved = [st.valid.copy() for st in states]
    k_max = max(st.max_steps for st in states)
    for k in range(1, k_max + 1):
        m = n - k
        if m <= 0:
            break
        hi = high[k:]
        lo = low[k:]
        for st, alive in zip(states, unresolved):
            if k > st.max_steps:
                continue
            active = alive[:m] & (st.steps[:m] >= k)
            if not active.any():
                continue
            sl_hit = active & (lo <= st.sl_price[:m])
            tp_hit = active & ~sl_hit & (hi >= st.tp_price[:m])
            if sl_hit.any():
                idx = np.nonzero(sl_hit)[0]
                st.outcome[idx] = _SL
                st.hit_idx[idx] = idx + k
                alive[idx] = False
            if tp_hit.any():
                idx = np.nonzero(tp_hit)[0]
                st.outcome[idx] = _TP
                st.hit_idx[idx] = idx + k
                alive[idx] = False
        if not any(a.any() for a in unresolved):
            break

    # --- settle ------------------------------------------------------------ #
    out: dict[str, OutcomeArrays] = {}
    for st, alive in zip(states, unresolved):
        spec = st.spec
        rt = float(spec.round_trip_bps)

        # Right-censored: the horizon runs past the data and nothing was hit.
        censored = alive & ~st.complete
        if censored.any():
            st.outcome[censored] = _NE
            st.reason[censored] = _R_CENSORED

        net = np.zeros(n, dtype=np.float64)
        hit_ms = np.zeros(n, dtype=np.int64)
        label_end = st.horizon_end_ms.copy()

        is_tp = st.outcome == _TP
        is_sl = st.outcome == _SL
        is_to = st.outcome == _TO
        is_ne = st.outcome == _NE

        # TP: the price is set so the realised NET is exactly the target.  A bar
        # that opened above it still fills at the target — never optimistic.
        net[is_tp] = spec.net_target_bps

        # SL: the fill is the worse of the stop price and the bar's open, so a
        # bar that gapped through the stop is charged for the gap.
        if is_sl.any():
            idx = np.nonzero(is_sl)[0]
            bar_idx = st.hit_idx[idx]
            fill = np.minimum(st.sl_price[idx], open_[bar_idx])
            gross = (fill - close[idx]) / close[idx] * _BPS
            net[idx] = gross - rt

        # Timeout: exit at the close of the last bar inside the horizon.
        if is_to.any():
            idx = np.nonzero(is_to)[0]
            exit_px = close[st.last_idx[idx]]
            net[idx] = (exit_px - close[idx]) / close[idx] * _BPS - rt

        hit_any = is_tp | is_sl
        if hit_any.any():
            idx = np.nonzero(hit_any)[0]
            touched = close_ms[st.hit_idx[idx]]
            hit_ms[idx] = touched
            label_end[idx] = touched

        # NO_EDGE has no forward window at all: it closes where it opens.
        label_end[is_ne] = close_ms[is_ne]

        out[spec.key()] = OutcomeArrays(
            spec=spec,
            ts_ms=close_ms.copy(),
            outcome=st.outcome,
            no_edge_reason=st.reason,
            net_return_bps=net,
            hit_ms=hit_ms,
            entry_price=close.copy(),
            tp_price=st.tp_price,
            sl_price=st.sl_price,
            label_end_ms=label_end,
        )
    return out


# --------------------------------------------------------------------------- #
# The public, object-shaped API
# --------------------------------------------------------------------------- #


def build_labels(
    bars: Sequence[Bar],
    *,
    spec: BarrierSpec,
    symbol: str,
    atr_bps_series: np.ndarray | Sequence[float] | None = None,
    vol_bps_series: np.ndarray | Sequence[float] | None = None,
    max_gross_move_bps: float | None = None,
    include_no_edge: bool = True,
) -> list[Label]:
    """Label every bar in ``bars`` as a candidate long entry under ``spec``."""
    arrays = resolve_specs(
        bars,
        specs=[spec],
        atr_bps=atr_bps_series,
        vol_bps=vol_bps_series,
        max_gross_move_bps=max_gross_move_bps,
    )
    return arrays[spec.key()].labels(symbol, include_no_edge=include_no_edge)


def label_matrix(
    bars: Sequence[Bar],
    *,
    specs: Sequence[BarrierSpec] | None = None,
    symbol: str,
    atr_bps_series: np.ndarray | Sequence[float] | None = None,
    vol_bps_series: np.ndarray | Sequence[float] | None = None,
    max_gross_move_bps: float | None = None,
    include_no_edge: bool = True,
) -> dict[str, list[Label]]:
    """``{spec_key: labels}`` for many specs over **one** shared forward walk."""
    specs = list(specs) if specs is not None else default_specs()
    arrays = resolve_specs(
        bars,
        specs=specs,
        atr_bps=atr_bps_series,
        vol_bps=vol_bps_series,
        max_gross_move_bps=max_gross_move_bps,
    )
    return {
        key: value.labels(symbol, include_no_edge=include_no_edge)
        for key, value in arrays.items()
    }


def class_balance(labels: Iterable[Label]) -> dict[Outcome, int]:
    """Counts per :class:`Outcome`.  Every outcome is present, even at zero."""
    counts = {outcome: 0 for outcome in Outcome}
    for label in labels:
        counts[label.outcome] += 1
    return counts
