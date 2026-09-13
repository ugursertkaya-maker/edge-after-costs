"""Strategy-specific position management (Phase 6, §7-§10).

    HOLDING POLICY = f(strategy, regime, signal decay, NET P&L, volatility,
                       execution state)

The V2 engine gave every position the same 25/60 minute clock.  That is wrong
in both directions at once, and the live sample says so: `flow_impulse` earned
its 11.93 bps of alpha over a **32-second** median hold (docs/PNL_AUDIT.md §6),
so a 25-minute review is 46 lifetimes too late for it; `momentum_scalp` held 71
seconds and was indistinguishable from market drift, so shortening *its* clock
would only churn faster.  One clock cannot serve both.

Three rules shape everything here:

* **There is no universal minimum hold.**  ``min_hold_seconds`` is zero for the
  flow strategies by design — a 30-second `flow_impulse` trade is the correct
  outcome when the impulse is spent, not a bug to be timed out of.  It is
  non-zero only for the slow strategies, where the trigger features are noisy at
  10-second timescales and an exit on that noise is churn, not information.
* **Entry scoring and position management are different questions.**  For a slow
  strategy a small drop in the entry score is *not* a reason to leave: the score
  answers "would I open this now", the position answers "is the move I am
  already in still developing".  ``reeval_entry_score`` is False for exactly
  those strategies, and a live score handed to the policy is then ignored.
* **Decay is measured, never assumed.**  A position is not stale because a timer
  fired; it is stale because the feature that triggered it has given itself
  back.  :func:`signal_decay` is a pure function of the entry snapshot and the
  live :class:`~app.types.Features`, and the profile's halflife only says *how
  much* retracement is tolerable after how long — an impulse that is still as
  strong as it was at entry never decays out, at any age.

Nothing in this module talks to the network, the database or an order path.  It
reads a :class:`~app.types.Position`, its entry snapshot and the current market,
and returns a reason or ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Callable

from app.types import ExitReason, Features, OrderBook, Position, Side

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.config import HoldingConfig, Settings

#: A trigger smaller than this is indistinguishable from zero, and dividing by
#: it would turn feature noise into an infinite decay ratio.
TRIGGER_EPS = 1e-9

#: Where the entry snapshot lives on ``Position.meta``.  One key, one dict, so
#: the position stays JSON-round-trippable through the recorder.
META_KEY = "holding"


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


# --------------------------------------------------------------------------- #
# Profiles — the frozen contract (spec §7)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HoldingProfile:
    """How one strategy is allowed to hold a position.

    ``max_hold_seconds`` is a ceiling on the strategy's own clock, not a target:
    the position leaves earlier on its stop, its net target, its trail, its
    decay or a broken thesis, and this is only what is left when none of those
    happened.  ``0.0`` means "this strategy has no clock of its own" and the
    portfolio horizon decides, which is what an unknown strategy gets.
    """

    strategy: str
    min_hold_seconds: float          # 0.0 for flow_impulse; never a global floor
    typical_hold_seconds: float
    max_hold_seconds: float
    decay_exit: bool                 # exit when the originating signal decays
    decay_halflife_seconds: float
    trail_after_net_bps: float
    reeval_entry_score: bool         # False for slow strategies: entry != management


# The spec's starting values, to be replaced by measurement — not by taste.
# ``trail_after_net_bps`` is the one column the spec table leaves open; it is
# set from what each strategy has actually been measured to make, because a
# trail armed below the noise floor is a random exit.  Live (PNL_AUDIT §6, raw
# bps over the holding interval): flow_impulse 12.10, momentum_scalp 6.33,
# book_imbalance 2.66.  The round trip is ~2.5 bps at Tier-0, so these are the
# net levels at which there is something left worth protecting.
PROFILES: dict[str, HoldingProfile] = {
    "flow_impulse": HoldingProfile(
        strategy="flow_impulse",
        min_hold_seconds=0.0,
        typical_hold_seconds=30.0,
        max_hold_seconds=180.0,
        decay_exit=True,
        decay_halflife_seconds=20.0,
        trail_after_net_bps=4.0,
        reeval_entry_score=True,
    ),
    "book_imbalance": HoldingProfile(
        strategy="book_imbalance",
        min_hold_seconds=0.0,
        typical_hold_seconds=120.0,
        max_hold_seconds=600.0,
        decay_exit=True,
        decay_halflife_seconds=90.0,
        trail_after_net_bps=2.0,
        reeval_entry_score=True,
    ),
    "momentum_scalp": HoldingProfile(
        strategy="momentum_scalp",
        min_hold_seconds=60.0,
        typical_hold_seconds=600.0,
        max_hold_seconds=2_400.0,
        decay_exit=False,
        decay_halflife_seconds=0.0,
        trail_after_net_bps=4.0,
        reeval_entry_score=False,
    ),
    "breakout_scalp": HoldingProfile(
        strategy="breakout_scalp",
        min_hold_seconds=60.0,
        typical_hold_seconds=900.0,
        max_hold_seconds=3_600.0,
        decay_exit=False,
        decay_halflife_seconds=0.0,
        trail_after_net_bps=6.0,
        reeval_entry_score=False,
    ),
    "trend_pullback": HoldingProfile(
        strategy="trend_pullback",
        min_hold_seconds=120.0,
        typical_hold_seconds=1_500.0,
        max_hold_seconds=3_600.0,
        decay_exit=False,
        decay_halflife_seconds=0.0,
        trail_after_net_bps=8.0,
        reeval_entry_score=False,
    ),
    "mean_reversion": HoldingProfile(
        strategy="mean_reversion",
        min_hold_seconds=60.0,
        typical_hold_seconds=600.0,
        max_hold_seconds=2_400.0,
        decay_exit=False,
        decay_halflife_seconds=0.0,
        trail_after_net_bps=5.0,
        reeval_entry_score=False,
    ),
}

#: What an unprofiled strategy gets: no clock, no decay model, no re-scoring.
#: Deliberately inert — a strategy nobody has measured must not inherit another
#: strategy's timing, and the portfolio horizon remains its only deadline.
DEFAULT_PROFILE = HoldingProfile(
    strategy="",
    min_hold_seconds=0.0,
    typical_hold_seconds=0.0,
    max_hold_seconds=0.0,
    decay_exit=False,
    decay_halflife_seconds=0.0,
    trail_after_net_bps=0.0,
    reeval_entry_score=False,
)


def profile_for_horizon(strategy: str, horizon_seconds: float) -> HoldingProfile:
    """The holding profile, widened when the configured horizon exceeds it.

    The ceilings in :data:`PROFILES` were written for the short-horizon
    configuration. Moving momentum_scalp to a 60-minute horizon while its
    profile still caps the hold at 2,400s would close every position at 40
    minutes and then report the 60-minute arm's results -- measuring a
    strategy nobody deployed, which is the failure mode this project has hit
    repeatedly.

    The ceiling becomes 1.5x the horizon: enough room for the setup to resolve
    past its own deadline, not so much that a dead position is held forever.
    Profiles are only ever WIDENED here; a strategy whose profile already
    allows more than the horizon keeps what it has.
    """
    base = profile_for(strategy)
    horizon = max(0.0, float(horizon_seconds))
    if horizon <= 0:
        return base
    # Widen only when the horizon genuinely exceeds the existing ceiling.
    # Comparing against 1.5x the horizon instead would widen flow_impulse from
    # 180s to 270s just for asking -- a strategy this phase deliberately does
    # not touch, because its only measured alpha was measured at 32 seconds.
    if base.max_hold_seconds >= horizon:
        return base
    needed = horizon * 1.5
    return replace(
        base,
        typical_hold_seconds=max(base.typical_hold_seconds, horizon),
        max_hold_seconds=needed,
    )


def configured_horizon_seconds(strategy: str, settings=None) -> float:
    """The horizon this strategy is configured to hold for, 0 when unset.

    Reads the same config the strategies read, so the exit side and the entry
    side cannot disagree about how long a position is meant to live.
    """
    if settings is None:
        from app.config import get_settings
        settings = get_settings()
    cfg = getattr(settings, "strategy_horizon", None)
    if cfg is None or strategy not in cfg.slow_families:
        return 0.0
    return float(cfg.slow_horizon_seconds or 0.0)


def effective_profile(strategy: str, settings=None) -> HoldingProfile:
    """The profile a position of this strategy is actually managed by.

    This is the accessor every caller should use. ``profile_for`` returns the
    static table, which is right until a horizon is configured and wrong the
    moment one is -- and a 60-minute arm managed by a 40-minute ceiling
    measures a strategy nobody deployed.
    """
    return profile_for_horizon(
        strategy, configured_horizon_seconds(strategy, settings)
    )


def profile_for(strategy: str) -> HoldingProfile:
    """The holding profile for ``strategy``, or the inert default."""
    return PROFILES.get(strategy or "", DEFAULT_PROFILE)


# --------------------------------------------------------------------------- #
# Triggers — what each strategy was actually looking at
# --------------------------------------------------------------------------- #

# One entry per strategy, mirroring the feature each one publishes in its own
# ``features_used``.  Every value is signed so that **positive means the reason
# we are in this trade is still true**, whichever side we are on.  Nothing here
# is invented: each is a field of ``Features`` the strategy itself read.
_TRIGGERS: dict[str, Callable[[float, Features], float]] = {
    # The impulse: aggressive flow in our direction.
    "flow_impulse": lambda d, f: d * f.trade_flow_imbalance,
    # Resting size leaning our way.
    "book_imbalance": lambda d, f: d * f.imbalance,
    # The trend that carried us in, and is supposed to keep carrying us.
    "momentum_scalp": lambda d, f: d * f.ema_slope_bps,
    "trend_pullback": lambda d, f: d * f.ema_slope_bps,
    # A breakout is alive while price holds outside the middle of its range;
    # falling back into the range is the breakout failing, by definition.
    "breakout_scalp": lambda d, f: d * (f.range_pos - 0.5) * 2.0,
    # Mean reversion is short the stretch: a LONG is taken *below* VWAP, so the
    # premise is alive while price is still dislocated in that direction, and
    # the stretch closing is the trade working rather than the trade dying.
    "mean_reversion": lambda d, f: -d * f.vwap_dev_bps,
}


def trigger_value(strategy: str, side: Side, f: Features | None) -> float:
    """The strategy's own trigger feature, signed toward the position.

    ``0.0`` when the strategy has no registered trigger or there are no live
    features — an unknown trigger must never be read as a decayed one.
    """
    fn = _TRIGGERS.get(strategy or "")
    if fn is None or f is None:
        return 0.0
    return float(fn(1.0 if side is Side.LONG else -1.0, f))


# --------------------------------------------------------------------------- #
# The entry snapshot
# --------------------------------------------------------------------------- #


@dataclass
class HoldingState:
    """What the market looked like when this position was opened.

    Decay, execution deterioration and entry-score re-evaluation are all
    *relative* measurements; without this baseline none of them exist, and the
    policy says so by declining to fire rather than by guessing.
    """

    strategy: str
    entry_trigger: float
    entry_score: float
    entry_spread_bps: float
    entry_exit_depth_usd: float
    armed_ms: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "entry_trigger": self.entry_trigger,
            "entry_score": self.entry_score,
            "entry_spread_bps": self.entry_spread_bps,
            "entry_exit_depth_usd": self.entry_exit_depth_usd,
            "armed_ms": self.armed_ms,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "HoldingState":
        return cls(
            strategy=str(raw.get("strategy", "")),
            entry_trigger=float(raw.get("entry_trigger", 0.0)),
            entry_score=float(raw.get("entry_score", 0.0)),
            entry_spread_bps=float(raw.get("entry_spread_bps", 0.0)),
            entry_exit_depth_usd=float(raw.get("entry_exit_depth_usd", 0.0)),
            armed_ms=int(raw.get("armed_ms", 0)),
        )


def exit_side_depth_usd(pos: Position, book: OrderBook | None, f: Features | None) -> float:
    """Resting notional on the side we would have to hit to get out.

    A LONG is closed by selling into the bids, so the bid side is the one whose
    disappearance is our problem.  The live book is preferred; ``Features``
    measures the same 10 bps window and is the fallback.
    """
    bid_side = pos.side is Side.LONG
    if book is not None and book.is_valid():
        return book.depth_usd("bid" if bid_side else "ask", 10.0)
    if f is not None:
        return f.depth_bid_usd if bid_side else f.depth_ask_usd
    return 0.0


def _spread_bps(book: OrderBook | None, f: Features | None) -> float:
    if book is not None and book.is_valid():
        return book.spread_bps
    return f.spread_bps if f is not None else 0.0


def state_of(pos: Position) -> HoldingState | None:
    """The entry snapshot, or ``None`` if this position was never armed."""
    raw = pos.meta.get(META_KEY) if pos.meta else None
    if isinstance(raw, HoldingState):
        return raw
    if isinstance(raw, dict):
        return HoldingState.from_dict(raw)
    return None


def arm(
    pos: Position,
    f: Features | None,
    *,
    book: OrderBook | None = None,
    entry_score: float = 0.0,
    ts_ms: int | None = None,
) -> HoldingState | None:
    """Record the entry snapshot, once.

    Idempotent on purpose: re-arming an open position would re-baseline decay to
    the market as it is *now*, which is exactly how a position that has given
    everything back comes to look untouched.  Returns the existing state when
    there already is one, and ``None`` when there are no features to snapshot.
    """
    existing = state_of(pos)
    if existing is not None:
        return existing
    if f is None:
        return None
    state = HoldingState(
        strategy=pos.strategy,
        entry_trigger=trigger_value(pos.strategy, pos.side, f),
        entry_score=float(entry_score),
        entry_spread_bps=_spread_bps(book, f),
        entry_exit_depth_usd=exit_side_depth_usd(pos, book, f),
        armed_ms=int(ts_ms if ts_ms is not None else pos.entry_ms),
    )
    pos.meta[META_KEY] = state.as_dict()
    return state


# --------------------------------------------------------------------------- #
# Signal decay
# --------------------------------------------------------------------------- #


def trigger_ratio(entry_trigger: float, trigger: float) -> float | None:
    """``trigger / entry_trigger``, or ``None`` when the baseline is unusable.

    A non-positive entry trigger means the snapshot never saw the setup that
    opened the trade (a position armed late, or a strategy with no registered
    trigger).  That is unknown, not spent, and every caller treats it as such.
    """
    if entry_trigger <= TRIGGER_EPS:
        return None
    return trigger / entry_trigger


def signal_decay(entry_trigger: float, trigger: float) -> float:
    """Fraction of the originating trigger that has been given back, in [0, 1].

    ``0.0`` = the signal is at least as strong as it was at entry (a trigger
    that has *grown* is not decayed); ``1.0`` = it is gone or has flipped
    against us.  Unknown baselines return ``0.0`` — see :func:`trigger_ratio`.
    """
    ratio = trigger_ratio(entry_trigger, trigger)
    if ratio is None:
        return 0.0
    return 1.0 - _clamp(ratio)


def decay_tolerance(held_seconds: float, halflife_seconds: float) -> float:
    """How much of the trigger a position of this age is allowed to have lost.

    Halves every halflife: a 20-second-halflife impulse may give back anything
    at all in its first instant, half of itself by 20 s, a quarter by 40 s, an
    eighth by 60 s.  This is what makes decay a *measurement* and not a timer —
    at ``t = 0`` the tolerance is 1.0, so nothing can decay out at entry, and an
    impulse still at full strength has given back nothing and so survives every
    tolerance, however old it is.  A halflife of zero means the strategy has no
    decay model and nothing is ever spent.
    """
    if halflife_seconds <= 0.0:
        return 1.0
    return 0.5 ** (max(held_seconds, 0.0) / halflife_seconds)


def decay_spent(decay: float, held_seconds: float, halflife_seconds: float) -> bool:
    """True when the retracement is larger than this age is allowed to tolerate."""
    return decay > decay_tolerance(held_seconds, halflife_seconds)


# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HoldingVerdict:
    """What the holding policy makes of a position right now.

    ``note`` exists so the worker's log line says *why* rather than repeating
    the enum, and so the dashboard can show the number that moved.
    """

    reason: ExitReason | None = None
    note: str = ""
    decay: float = 0.0
    held_seconds: float = 0.0

    @property
    def should_exit(self) -> bool:
        return self.reason is not None


class HoldingPolicy:
    """Position management for one strategy at a time.

    Order inside :meth:`evaluate` is the order of what it costs to be wrong.
    *Hard* conditions — the ones that say the exit itself is getting more
    expensive, or that the trade is now expected to lose money — are checked
    before ``min_hold_seconds``, because an anti-churn floor is protection
    against noise and never a reason to stay in a trade that has stopped being
    the trade we opened.  Everything else waits out the floor.

    The price levels (stop, net target, trail) are *not* here: they belong to
    :class:`app.exits.ExitEngine`, are checked before this policy is consulted,
    and always win.
    """

    def __init__(self, *, settings: "Settings | None" = None) -> None:
        if settings is None:
            from app.config import get_settings

            settings = get_settings()
        self.settings = settings
        self.cfg: "HoldingConfig" = settings.holding

    # -- the clock --------------------------------------------------------- #

    def hold_scale(self, f: Features | None) -> float:
        """Volatility's effect on how long a thesis is worth waiting for.

        A move that was going to happen happens sooner in a fast market and
        later in a slow one, so the strategy's clock is stretched in quiet
        volatility and compressed in violent volatility.  ``vol_percentile`` is
        already normalised against the pair's own trailing window, which is why
        this can be one rule for every symbol.
        """
        cfg = self.cfg
        if f is None or cfg.vol_scale <= 0.0:
            return 1.0
        raw = 1.0 + cfg.vol_scale * (0.5 - _clamp(f.vol_percentile))
        return _clamp(raw, cfg.vol_scale_min, cfg.vol_scale_max)

    def max_hold_seconds(self, pos: Position, f: Features | None = None) -> float:
        """The strategy's own deadline in seconds, or ``0.0`` if it has none."""
        if not self.cfg.enabled:
            return 0.0
        profile = effective_profile(pos.strategy, getattr(self, 'settings', None))
        if profile.max_hold_seconds <= 0.0:
            return 0.0
        return profile.max_hold_seconds * self.hold_scale(f)

    def trail_after_net_bps(self, pos: Position) -> float:
        if not self.cfg.enabled:
            return 0.0
        return effective_profile(
            pos.strategy, getattr(self, 'settings', None)
        ).trail_after_net_bps

    # -- the decision ------------------------------------------------------ #

    def evaluate(
        self,
        pos: Position,
        book: OrderBook | None,
        f: Features | None,
        ts_ms: int,
        *,
        net_unrealized_bps: float = 0.0,
        live_score: float | None = None,
        predicted_ev_bps: float | None = None,
    ) -> HoldingVerdict:
        """Should this position still exist, and if not, why not."""
        cfg = self.cfg
        profile = effective_profile(pos.strategy, getattr(self, 'settings', None))
        held = pos.holding_seconds(ts_ms)
        state = state_of(pos)

        trigger = trigger_value(pos.strategy, pos.side, f)
        decay = signal_decay(state.entry_trigger, trigger) if state else 0.0
        if not cfg.enabled:
            return HoldingVerdict(decay=decay, held_seconds=held)

        # -- hard: the exit is getting more expensive than the trade is worth.
        exec_note = self._execution_broken(pos, book, f, state)
        if exec_note:
            return HoldingVerdict(
                ExitReason.EXEC_DETERIORATED, exec_note, decay, held
            )

        if predicted_ev_bps is not None and predicted_ev_bps <= cfg.negative_ev_bps:
            return HoldingVerdict(
                ExitReason.NEGATIVE_EV,
                f"predicted EV {predicted_ev_bps:+.1f} bps",
                decay,
                held,
            )

        # -- the anti-churn floor.  Noise is not news.
        if held < profile.min_hold_seconds:
            return HoldingVerdict(decay=decay, held_seconds=held)

        ratio = trigger_ratio(state.entry_trigger, trigger) if state else None

        if ratio is not None and ratio <= -cfg.reversal_frac:
            return HoldingVerdict(
                ExitReason.SIGNAL_REVERSAL,
                f"{pos.strategy} trigger reversed to {ratio:+.2f}x entry",
                decay,
                held,
            )

        if pos.strategy == "mean_reversion" and ratio is not None:
            # Mean reversion owns its horizon: the stretch closing *is* the
            # trade, and the stretch widening past the point where the entry
            # was sized is the structure that justified it being wrong.
            if ratio >= cfg.reversion_invalidate_mult:
                return HoldingVerdict(
                    ExitReason.SIGNAL_REVERSAL,
                    f"stretch widened to {ratio:.2f}x entry",
                    decay,
                    held,
                )
            if ratio <= cfg.reversion_target_frac:
                reached = (
                    ExitReason.TAKE_PROFIT
                    if net_unrealized_bps > 0.0
                    else ExitReason.EXIT_WEAK
                )
                return HoldingVerdict(
                    reached, f"reverted to {ratio:.2f}x entry stretch", decay, held
                )

        if profile.decay_exit and state is not None:
            if decay_spent(decay, held, profile.decay_halflife_seconds):
                return HoldingVerdict(
                    ExitReason.SIGNAL_DECAY,
                    f"{decay:.0%} of the signal given back in {held:.0f}s "
                    f"(tolerance {decay_tolerance(held, profile.decay_halflife_seconds):.0%})",
                    decay,
                    held,
                )

        if (
            profile.reeval_entry_score
            and live_score is not None
            and state is not None
            and state.entry_score > 0.0
            and live_score < cfg.entry_score_exit_frac * state.entry_score
        ):
            # Only where the profile allows it.  For a slow strategy this whole
            # branch is dead code by design: a setup that would no longer be
            # *opened* is not thereby a position that should be *closed*.
            return HoldingVerdict(
                ExitReason.EXIT_WEAK,
                f"entry score {live_score:.2f} vs {state.entry_score:.2f} at entry",
                decay,
                held,
            )

        return HoldingVerdict(decay=decay, held_seconds=held)

    # -- execution state --------------------------------------------------- #

    def _execution_broken(
        self,
        pos: Position,
        book: OrderBook | None,
        f: Features | None,
        state: HoldingState | None,
    ) -> str:
        """Why we can no longer get out at a price worth staying in for.

        Every test is relative to the entry snapshot and then floored by an
        absolute: a spread that doubles from 0.3 to 0.6 bps has "doubled" and
        costs nothing, and a book that was always thin was priced that way at
        entry.  Both conditions have to be true before this is a reason.

        A *stale* book is deliberately not one of these.  Staleness is a fact
        about our own feed, not about the market, it arrives for every symbol at
        once, and "flatten the whole book because the websocket hiccuped" is a
        far more expensive mistake than holding through it.  The worker already
        refuses to act on a book it cannot price; that is the correct response.
        """
        cfg = self.cfg
        if state is None:
            return ""

        spread = _spread_bps(book, f)
        if (
            state.entry_spread_bps > 0.0
            and spread >= cfg.exec_min_spread_bps
            and spread >= cfg.exec_spread_mult * state.entry_spread_bps
        ):
            return f"spread {spread:.1f} bps vs {state.entry_spread_bps:.1f} at entry"

        depth = exit_side_depth_usd(pos, book, f)
        if (
            state.entry_exit_depth_usd > 0.0
            and depth < cfg.exec_depth_collapse_frac * state.entry_exit_depth_usd
            and depth < cfg.exec_depth_notional_mult * max(pos.notional_usd, 0.0)
        ):
            return (
                f"exit-side depth ${depth:,.0f} vs "
                f"${state.entry_exit_depth_usd:,.0f} at entry"
            )

        return ""
