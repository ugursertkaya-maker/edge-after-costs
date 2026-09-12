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
            return None
        if not self.ready(f):
            return None
        return self._evaluate(f, regime)

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
        if score < self.min_score:
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
            return None
        stop_bps = max(float(stop_bps), MIN_STOP_BPS)
        if f.spread_bps > MAX_SPREAD_FRACTION_OF_TARGET * target_bps:
            return None
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
