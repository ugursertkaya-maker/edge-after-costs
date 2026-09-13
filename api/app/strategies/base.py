"""Strategy base class.

A strategy is a pure opinion generator.  It sees exactly two things — the
:class:`Features` for one symbol and the :class:`RegimeState` that goes with
them — and answers "is there a move here, which way, how far, and where am I
wrong".  It knows nothing about fees, capital, open positions or risk limits;
those belong to the ranker, the risk engine and the exit engine, and a strategy
that peeked at them would silently double-count their logic.

Targets and stops are always derived from ATR, never from constants: a 20 bps
stop is reckless on a quiet pair and meaningless on a violent one.  The floors
below exist only so a degenerate ATR can never produce ``stop_bps <= 0``.
"""

from __future__ import annotations

import math

from ..config import Settings, get_settings
from ..types import Features, Regime, RegimeState, Side, Signal

# Absolute floors.  ATR drives the real numbers; these stop a pathological
# reading (a frozen market, a cold-start ATR) producing a nonsensical order.
#
# MIN_STOP_BPS is a clamp: a stop that is too tight is a stop we widen.
# MIN_TARGET_BPS is a REJECTION: a target below it means the move this strategy
# needs is not on offer, and clamping it up invents the move instead of
# declining the trade.  The two floors are deliberately not symmetric.
MIN_STOP_BPS = 3.0
MIN_TARGET_BPS = 4.0

#: Bars behind ``Features.range_window_bps`` (``features.RANGE_WINDOW``).
RANGE_WINDOW_MINUTES = 20.0
#: A realized high-low span overstates the move a trade can expect to capture:
#: the span is the extreme-to-extreme travel, and an entry does not begin at
#: the low. Half of it is the conservative reading.
RANGE_CAPTURE_FRACTION = 0.5


def horizon_move_bps(f: "Features", horizon_seconds: float) -> float:
    """The move this symbol actually offers over ``horizon_seconds``, in bps.

    ``atr_bps`` is a one-minute measure, and on Binance.US that is the wrong
    instrument: 80.4% of ETHUSD's one-minute bars have ``high == low`` because
    the venue does not trade in most minutes. The ATR collapses toward zero and
    a target derived from it falls under ``MIN_TARGET_BPS``, so no strategy can
    fire -- while the same tape moves a median of 4.45 bps over 5 minutes and
    10.20 over 15.

    Two estimates, and the larger wins:

    * the one-minute ATR scaled by sqrt(time), which is right when minutes are
      full and understates badly when they are empty;
    * the realized range over the engine's window, rescaled to the horizon by
      the same sqrt law and halved, which sees the moves the ATR misses.

    Taking the larger is deliberate. Both are lower bounds on the available
    move for different reasons, and a target that is too small is what stopped
    the trader from ever firing.
    """
    horizon_minutes = max(1.0 / 60.0, float(horizon_seconds) / 60.0)
    scale = math.sqrt(horizon_minutes)
    from_atr = max(0.0, float(f.atr_bps)) * scale
    span = max(0.0, float(getattr(f, "range_window_bps", 0.0)))
    from_range = 0.0
    if span > 0:
        from_range = (
            span * RANGE_CAPTURE_FRACTION
            * math.sqrt(horizon_minutes / RANGE_WINDOW_MINUTES)
        )
    return max(from_atr, from_range)

# A setup whose entry/exit spread eats this much of the target is not a setup.
# Expressed against the target rather than an absolute cap so the test scales
# with the pair's own volatility.
MAX_SPREAD_FRACTION_OF_TARGET = 0.5

# Enough history for ATR(14), EMA(30) and the 30-return realized-vol window.
MIN_BARS = 31


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def scale(x: float, lo: float, hi: float) -> float:
    """Map ``x`` from [lo, hi] onto [0, 1], clipped.  ``hi > lo`` required."""
    if hi <= lo:
        return 0.0
    return clamp((x - lo) / (hi - lo))


class Strategy:
    """Base for the six signal generators.

    Subclasses set :attr:`name`/:attr:`allowed_regimes` and implement
    :meth:`_evaluate`; :meth:`evaluate` owns the regime gate and the readiness
    checks so no subclass can forget them.
    """

    name: str = "base"
    allowed_regimes: frozenset[Regime] = frozenset()
    min_score: float = 0.35
    default_horizon_seconds: int = 600

    def __init__(self, *, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        #: Why the last evaluation produced no signal, with the number that
        #: decided it. A strategy that declines silently is indistinguishable
        #: from a broken one: the dashboard showed zero candidates, zero
        #: rejections and zero trades, and the only honest reading of that
        #: screen was "the worker is dead" -- which it was not.
        self.last_decline: tuple[str, float, float] | None = None

    # -- horizon ----------------------------------------------------------- #

    @property
    def horizon_seconds(self) -> int:
        """This strategy's holding horizon, config first, own default second.

        Only the slow families follow the configured value. flow_impulse and
        book_imbalance keep their own: a 30-second order-flow signal stretched
        into a 30-minute position is a different strategy wearing the same name,
        and the research that produced flow_impulse's only measured alpha was
        done at 32 seconds.
        """
        cfg = getattr(self.settings, "strategy_horizon", None)
        if cfg is None or self.name not in cfg.slow_families:
            return int(self.default_horizon_seconds)
        return int(cfg.slow_horizon_seconds or self.default_horizon_seconds)

    def target_mult(self, own: float) -> float:
        """Multiplier on the horizon move for the target; config may override."""
        cfg = getattr(self.settings, "strategy_horizon", None)
        if cfg is None or self.name not in cfg.slow_families or cfg.target_mult <= 0:
            return own
        return float(cfg.target_mult)

    def stop_mult(self, own: float) -> float:
        cfg = getattr(self.settings, "strategy_horizon", None)
        if cfg is None or self.name not in cfg.slow_families or cfg.stop_mult <= 0:
            return own
        return float(cfg.stop_mult)

    def relaxed(self, threshold: float) -> float:
        """A setup threshold after the entry profile's relaxation.

        Applies to minimum-conviction gates only. Costs, the NET edge gate and
        risk are untouched by design: a profile may widen the mouth of the
        funnel, never the exit.
        """
        cfg = getattr(self.settings, "entry_profile", None)
        if cfg is None:
            return threshold
        return threshold * max(0.0, float(cfg.relax))

    @property
    def effective_min_score(self) -> float:
        cfg = getattr(self.settings, "entry_profile", None)
        if cfg is None:
            return self.min_score
        return self.min_score * max(0.0, float(cfg.min_score_relax))

    def _no(self, condition: str, value: float, threshold: float) -> None:
        """Decline, recording WHICH condition bound and with what number.

        A strategy that returns None tells the operator nothing. "NO_SETUP"
        repeated on a live screen is a black box, and the only way to tell an
        over-restrictive threshold from an empty market is to see the
        distribution of the value that did the refusing.
        """
        self.last_decline = (condition, float(value), float(threshold))
        return None

    # -- gating ------------------------------------------------------------ #

    def accepts(self, regime: RegimeState) -> bool:
        """Regime gate.  ``ABNORMAL`` can never be opted into."""
        if regime.regime is Regime.ABNORMAL:
            return False
        return regime.regime in self.allowed_regimes

    def ready(self, f: Features) -> bool:
        """Enough history and a usable book to have an opinion at all."""
        return (
            f.bars_available >= MIN_BARS
            and f.atr_bps > 0.0
            and f.price > 0.0
            and f.spread_bps > 0.0
        )

    def evaluate(self, f: Features, regime: RegimeState) -> Signal | None:
        if not self.accepts(regime):
            self.last_decline = ("REGIME_NOT_ALLOWED", 0.0, 0.0)
            return None
        if not self.ready(f):
            self.last_decline = ("WARMING_UP", float(f.bars_available), float(MIN_BARS))
            return None
        sig = self._evaluate(f, regime)
        if sig is None and self.last_decline is None:
            # The subclass declined on its own terms without saying why. Better
            # to name that plainly than to leave the screen blank.
            self.last_decline = ("NO_SETUP", 0.0, 0.0)
        return sig

    # -- subclass hook ------------------------------------------------------ #

    def _evaluate(self, f: Features, regime: RegimeState) -> Signal | None:
        raise NotImplementedError

    # -- construction helper ------------------------------------------------ #

    def _signal(
        self,
        f: Features,
        side: Side,
        *,
        score: float,
        target_bps: float,
        stop_bps: float,
        reasons: list[str],
        features_used: dict[str, float] | None = None,
        horizon_seconds: int | None = None,
    ) -> Signal | None:
        """Build a :class:`Signal`, or ``None`` when conviction is too low.

        Also enforces the two invariants every downstream module relies on:
        ``stop_bps > 0`` and a target the quoted spread cannot swallow.
        """
        score = clamp(score)
        floor = self.effective_min_score
        if score < floor:
            self.last_decline = ("SCORE_BELOW_MIN", score, floor)
            return None
        # A target below the floor means the market is not offering a move this
        # strategy can aim at. Clamping it UP does not make the move exist -- it
        # fabricates one, and every downstream number inherits the fiction.
        #
        # Measured 2026-09-12: ETHUSD 1m ATR was 0.131 bps, so book_imbalance's
        # own target was 0.079 bps. The floor rewrote it to 4.0 -- a 51x
        # inflation -- which produced a gross edge of 2.28 bps against a real
        # one of 0.045, and put 198 candidates within an apparent 0.26 bps of a
        # gate they were truly 2.5 bps away from. The floor was written to stop
        # "a pathological reading (a frozen market, a cold-start ATR)" producing
        # a nonsensical order; refusing the setup does that, inventing a target
        # does not.
        #
        # This makes the system trade LESS, not more. That is the point.
        target_bps = float(target_bps)
        if target_bps < MIN_TARGET_BPS:
            self.last_decline = ("TARGET_BELOW_FLOOR", target_bps, MIN_TARGET_BPS)
            return None
        stop_bps = max(float(stop_bps), MIN_STOP_BPS)
        if f.spread_bps > MAX_SPREAD_FRACTION_OF_TARGET * target_bps:
            self.last_decline = (
                "SPREAD_EATS_TARGET", f.spread_bps,
                MAX_SPREAD_FRACTION_OF_TARGET * target_bps,
            )
            return None
        self.last_decline = None
        used = {
            "atr_bps": f.atr_bps,
            "spread_bps": f.spread_bps,
            "trend_strength": f.trend_strength,
            "volume_ratio": f.volume_ratio,
        }
        if features_used:
            used.update(features_used)
        return Signal(
            strategy=self.name,
            symbol=f.symbol,
            side=side,
            score=score,
            target_bps=target_bps,
            stop_bps=stop_bps,
            ts_ms=f.ts_ms,
            horizon_seconds=int(horizon_seconds or self.default_horizon_seconds),
            reasons=reasons,
            features_used=used,
        )
