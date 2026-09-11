"""Take-profit / stop-loss / trailing / time-stop logic.

Two rules drive everything here:

* **A target that does not clear the round trip is not a target.**  On a $20
  position a 4 bps "profit" is 0.8 cents and the two fee legs alone are 4 cents.
  So every TP is widened until it clears ``round_trip_cost_bps +
  tp_min_bps_over_cost``, whatever model produced it.
* **Exit at the price we would actually get.**  A long is closed by *selling*,
  and a seller lifts nothing — it hits the best **bid**.  Checking a long's stop
  against the mid (or worse, the ask) would show a stop surviving that has in
  fact already been taken out, and would flatter every backtest.  ``check``
  therefore evaluates a LONG against ``book.best_bid`` and a SHORT against
  ``book.best_ask``.

Every level is rounded to the symbol's tick, because an unroundable price is a
price the exchange would never have printed.

V2 adds two things on top, without disturbing either rule:

* **NET targets.**  ``net_levels`` delegates to :class:`app.profit.NetTargetEngine`
  and returns the fee-aware :class:`~app.types.NetTargets`; when a position
  carries them, ``check`` measures the take-profit against
  ``targets.net_tp_price`` instead of the gross ``take_profit``.  A V1 position
  has no ``targets`` and behaves exactly as it did.
* **The 25/60 minute horizon.**  25 minutes is a *checkpoint*, not a lock:
  profit reached at minute 8 is taken at minute 8, a stop hit at minute 2 is
  honoured at minute 2, and at minute 25 a position that is going nowhere either
  yields its capital to a better idea (``ROTATE_CAPITAL``) or stops paying for
  the seat (``EXIT_WEAK``).  At 60 minutes the capital comes back.  Extension
  past that is allowed only for a position whose profit is *already protected* —
  **a loser is never extended to avoid realising it.**
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from app.profit import NetTargetEngine
from app.types import (
    ExitReason,
    Features,
    NetTargets,
    OrderBook,
    Position,
    Side,
    Signal,
    SymbolInfo,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.config import Settings
    from app.profit import FeeProvider

_BPS = 10_000.0

# How strong the live signal has to still be before a *winning* position is
# allowed past the 60-minute stop.  There is no config knob for this because
# extension is meant to be rare: the bar is deliberately high and fixed.
EXTENSION_MIN_SIGNAL_STRENGTH = 0.6


class CheckpointVerdict(str, enum.Enum):
    """What the 25-minute review decided (spec section E)."""

    KEEP = "KEEP"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAIL_PROFIT = "TRAIL_PROFIT"
    EXIT_WEAK = "EXIT_WEAK"
    ROTATE_CAPITAL = "ROTATE_CAPITAL"


class ExitEngine:
    """Computes entry-time levels and, tick by tick, decides when to leave."""

    def __init__(
        self,
        *,
        settings: "Settings | None" = None,
        fees: "FeeProvider | None" = None,
    ) -> None:
        if settings is None:
            from app.config import get_settings

            settings = get_settings()
        self.settings = settings
        self.cfg = settings.exits
        self.horizon = settings.horizon
        self.net_targets = NetTargetEngine(settings=settings, fees=fees)

    # ------------------------------------------------------------------ #
    # Entry-time levels
    # ------------------------------------------------------------------ #

    def initial_levels(
        self,
        side: Side,
        entry_price: float,
        f: Features,
        signal: Signal,
        *,
        round_trip_cost_bps: float,
        info: SymbolInfo,
    ) -> tuple[float, float]:
        """Return ``(take_profit, stop_loss)`` as tick-rounded prices.

        The TP comes from ``settings.exits.tp_model`` and is then floored by the
        cost gate; the SL comes from ``sl_model`` and is then capped by
        ``emergency_stop_pct`` — a stop may always be tighter than the model
        says, never wider.
        """
        if entry_price <= 0:
            raise ValueError("entry_price must be positive")

        tp_bps = self._tp_bps(f, signal, round_trip_cost_bps)
        sl_bps = self._sl_bps(side, entry_price, f, signal)

        if side is Side.LONG:
            tp = entry_price * (1.0 + tp_bps / _BPS)
            sl = entry_price * (1.0 - sl_bps / _BPS)
        else:
            tp = entry_price * (1.0 - tp_bps / _BPS)
            sl = entry_price * (1.0 + sl_bps / _BPS)

        return self._round_levels(side, entry_price, tp, sl, info)

    def _tp_bps(self, f: Features, signal: Signal, round_trip_cost_bps: float) -> float:
        cfg = self.cfg
        model = (cfg.tp_model or "adaptive").lower()
        atr = max(f.atr_bps, 0.0)

        if model == "fixed_pct":
            tp_bps = cfg.tp_fixed_pct * 100.0
        elif model == "atr":
            tp_bps = cfg.tp_atr_mult * atr
        else:  # "adaptive"
            # ATR sets the scale, but we never claim more upside than the
            # strategy itself expects: target_bps is an upper bound, not a
            # suggestion.  The cost floor below then widens whatever remains.
            tp_bps = cfg.tp_atr_mult * atr
            if signal.target_bps > 0:
                tp_bps = min(tp_bps, signal.target_bps)

        floor = max(round_trip_cost_bps, 0.0) + cfg.tp_min_bps_over_cost
        return max(tp_bps, floor)

    def _sl_bps(self, side: Side, entry_price: float, f: Features, signal: Signal) -> float:
        cfg = self.cfg
        model = (cfg.sl_model or "atr").lower()
        atr = max(f.atr_bps, 0.0)
        atr_stop = cfg.sl_atr_mult * atr

        if model == "fixed_pct":
            sl_bps = cfg.sl_fixed_pct * 100.0
        elif model == "structure":
            sl_bps = self._structure_stop_bps(side, entry_price, f)
            if sl_bps <= 0.0:
                sl_bps = atr_stop
        else:  # "atr"
            sl_bps = atr_stop

        if sl_bps <= 0.0:
            # Never leave a position without a real stop: fall back to the
            # strategy's own view, then to a hard floor.
            sl_bps = signal.stop_bps if signal.stop_bps > 0 else 1.0

        # Absolute worst case, always.  emergency_stop_pct only ever tightens.
        return min(sl_bps, cfg.emergency_stop_pct * 100.0)

    @staticmethod
    def _structure_stop_bps(side: Side, entry_price: float, f: Features) -> float:
        """Distance to the nearest structural level, in bps, or 0 if unknown.

        Prefers an explicit swing price published by the feature engine in
        ``extras``; otherwise reconstructs the distance to the edge of the
        recent range from ``range_pos`` and the band width.
        """
        key = "swing_low" if side is Side.LONG else "swing_high"
        swing = f.extras.get(key)
        if swing:
            dist = (entry_price - swing) if side is Side.LONG else (swing - entry_price)
            if dist > 0:
                return dist / entry_price * _BPS

        width = f.bb_width_bps
        if width > 0 and 0.0 <= f.range_pos <= 1.0:
            # range_pos is where price sits in the range: a long's structural
            # stop is the distance down to the bottom of it.
            frac = f.range_pos if side is Side.LONG else (1.0 - f.range_pos)
            dist_bps = frac * width
            if dist_bps > 0:
                return dist_bps
        return 0.0

    def _round_levels(
        self,
        side: Side,
        entry_price: float,
        tp: float,
        sl: float,
        info: SymbolInfo,
    ) -> tuple[float, float]:
        """Round to tick, then guarantee the levels still straddle the entry."""
        tick = info.tick_size if info.tick_size > 0 else 0.0
        tp_r = info.round_price(tp)
        sl_r = info.round_price(sl)
        if side is Side.LONG:
            if tp_r <= entry_price:
                tp_r = info.round_price(entry_price + (tick or entry_price * 1e-4))
            if sl_r >= entry_price:
                sl_r = info.round_price(entry_price - (tick or entry_price * 1e-4))
        else:
            if tp_r >= entry_price:
                tp_r = info.round_price(entry_price - (tick or entry_price * 1e-4))
            if sl_r <= entry_price:
                sl_r = info.round_price(entry_price + (tick or entry_price * 1e-4))
        return tp_r, sl_r

    # ------------------------------------------------------------------ #
    # V2 — fee-aware NET levels
    # ------------------------------------------------------------------ #

    def net_levels(
        self,
        side: Side,
        entry_price: float,
        f: Features,
        signal: Signal,
        *,
        symbol: str,
        notional_usd: float,
        qty: float,
        book: OrderBook | None,
        info: SymbolInfo,
        profile: str | None = None,
        entry_is_maker: bool = False,
        exit_is_maker: bool = False,
        slippage_bps_entry: float = 0.0,
        slippage_bps_exit: float = 0.0,
        stop_price: float | None = None,
    ) -> NetTargets:
        """The fee-aware targets for a position of *this* size.

        Thin by design: the economics live in :class:`app.profit.NetTargetEngine`
        and the exit engine only consumes them, so there is exactly one place
        where "what does this trade have to reach" is decided.

        ``stop_price`` defaults to the stop :meth:`initial_levels` would set, so
        a caller that wants the V1 stop model with V2 targets gets it for free.
        """
        if stop_price is None:
            _, stop_price = self.initial_levels(
                side,
                entry_price,
                f,
                signal,
                round_trip_cost_bps=0.0,
                info=info,
            )
        return self.net_targets.build(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            notional_usd=notional_usd,
            qty=qty,
            book=book,
            info=info,
            features=f,
            signal=signal,
            profile=profile,
            entry_is_maker=entry_is_maker,
            exit_is_maker=exit_is_maker,
            slippage_bps_entry=slippage_bps_entry,
            slippage_bps_exit=slippage_bps_exit,
            stop_price=stop_price,
        )

    # ------------------------------------------------------------------ #
    # Live checks
    # ------------------------------------------------------------------ #

    @staticmethod
    def exit_price(pos: Position, book: OrderBook) -> float:
        """The price this position would *actually* be closed at.

        A LONG is closed by selling into the best bid; a SHORT by buying the
        best ask.  Never the mid — nobody trades at the mid.
        """
        if pos.side is Side.LONG:
            return book.best_bid
        return book.best_ask

    def check(
        self,
        pos: Position,
        book: OrderBook,
        f: Features | None,
        ts_ms: int,
        *,
        reversal: bool = False,
        best_alternative_edge_bps: float = 0.0,
        signal_strength: float = 0.0,
    ) -> ExitReason | None:
        """Return the reason to exit now, or ``None`` to hold.

        Priority is fixed and deliberate: losses first (STOP_LOSS), then
        realised targets (TAKE_PROFIT), then giving back an open profit
        (TRAILING_PROFIT), then a changed opinion (SIGNAL_REVERSAL), then the
        25-minute checkpoint's verdicts, then the clock (TIME_STOP).  Two
        conditions at once always resolve to the one that costs the least to be
        wrong about — which is why a stop hit at minute 2 of a 25-minute window
        is still a stop, not something to review later.

        ``check`` is a pure query with exactly one documented exception: when a
        *winning*, profit-protected position earns an extension past the time
        stop it sets ``pos.extended``, because the deadline it just survived has
        to move with it.

        A V1 position — no ``targets``, no ``checkpoint_ms`` — never reaches the
        checkpoint or the horizon clock and behaves precisely as it did before.
        """
        price = self.exit_price(pos, book)
        if price <= 0:
            # No executable price on our side of the book: do not invent one.
            return None

        long = pos.side is Side.LONG

        if pos.stop_loss > 0:
            if (long and price <= pos.stop_loss) or (not long and price >= pos.stop_loss):
                return ExitReason.STOP_LOSS

        target = self.take_profit_price(pos)
        if target > 0:
            if (long and price >= target) or (not long and price <= target):
                return ExitReason.TAKE_PROFIT

        if self._trailing_hit(pos, price):
            return ExitReason.TRAILING_PROFIT

        if reversal:
            return ExitReason.SIGNAL_REVERSAL

        if not self._horizon_applies(pos):
            # V1 path, untouched.
            if self._time_stop_hit(pos, price, ts_ms):
                return ExitReason.TIME_STOP
            return None

        if self._checkpoint_due(pos, ts_ms):
            verdict = self.checkpoint(
                pos, book, f, ts_ms,
                best_alternative_edge_bps=best_alternative_edge_bps,
            )
            if verdict is CheckpointVerdict.TAKE_PROFIT:
                return ExitReason.TAKE_PROFIT
            if verdict is CheckpointVerdict.EXIT_WEAK:
                return ExitReason.EXIT_WEAK
            if verdict is CheckpointVerdict.ROTATE_CAPITAL:
                return ExitReason.ROTATE_CAPITAL
            # KEEP and TRAIL_PROFIT both hold; arming the trail is the worker's
            # job via update_trailing, so that check() stays side-effect free.

        if ts_ms >= self._horizon_deadline_ms(pos):
            if self._may_extend(pos, price, signal_strength):
                pos.extended = True
                return None
            return ExitReason.TIME_STOP

        return None

    def take_profit_price(self, pos: Position) -> float:
        """The level a take-profit is measured against.

        The NET target when the position carries one — a gross target that does
        not cover its own costs is not a target — and the V1 ``take_profit``
        otherwise, so positions opened before V2 keep working unchanged.
        """
        targets = pos.targets
        if targets is not None and targets.net_tp_price > 0:
            return targets.net_tp_price
        return pos.take_profit

    # ------------------------------------------------------------------ #
    # V2 — the 25/60 minute horizon
    # ------------------------------------------------------------------ #

    def checkpoint(
        self,
        pos: Position,
        book: OrderBook,
        features: Features | None,
        ts_ms: int,
        *,
        best_alternative_edge_bps: float = 0.0,
    ) -> CheckpointVerdict:
        """Reassess a position at the 25-minute mark.

        The order of the verdicts is the order of what it costs to get them
        wrong.  Banking a target that is already there beats everything.  Then
        protecting an open profit whose momentum has gone.  Then the two ways of
        admitting a position is not working: hand the capital to a materially
        better idea, or simply stop paying for the seat.  ``KEEP`` is what is
        left when the trade is still doing what it was opened to do.

        Before the checkpoint is due the answer is always ``KEEP`` — 25 minutes
        is when we *review*, not when we start caring.
        """
        if not self._checkpoint_due(pos, ts_ms):
            return CheckpointVerdict.KEEP

        price = self.exit_price(pos, book)
        if price <= 0:
            return CheckpointVerdict.KEEP

        target = self.take_profit_price(pos)
        long = pos.side is Side.LONG
        if target > 0 and ((long and price >= target) or (not long and price <= target)):
            return CheckpointVerdict.TAKE_PROFIT

        progress_bps = pos.unrealized_bps(price)
        target_bps = self._target_bps(pos)
        progress_frac = progress_bps / target_bps if target_bps > 0 else 0.0

        if self._net_unrealized_usd(pos, price) > 0 and self._momentum_fading(pos, features):
            return CheckpointVerdict.TRAIL_PROFIT

        if progress_frac < self.horizon.rotate_min_progress_frac:
            remaining_bps = max(0.0, target_bps - progress_bps)
            alt = max(0.0, float(best_alternative_edge_bps))
            ratio = max(1.0, float(self.horizon.rotate_if_edge_ratio))
            if alt > 0.0 and alt >= ratio * remaining_bps:
                return CheckpointVerdict.ROTATE_CAPITAL
            return CheckpointVerdict.EXIT_WEAK

        return CheckpointVerdict.KEEP

    # -- horizon helpers --------------------------------------------------- #

    @staticmethod
    def _horizon_applies(pos: Position) -> bool:
        """True for a V2-shaped position.

        The horizon replaces ``exits.time_stop_seconds`` for positions that were
        opened with NET targets or an explicit checkpoint.  Everything older
        keeps the V1 clock, so nothing already in flight changes meaning.
        """
        return pos.targets is not None or pos.checkpoint_ms > 0

    def _checkpoint_due(self, pos: Position, ts_ms: int) -> bool:
        if pos.checkpoint_ms > 0:
            return ts_ms >= pos.checkpoint_ms
        seconds = float(self.horizon.checkpoint_seconds)
        return seconds > 0 and pos.holding_seconds(ts_ms) >= seconds

    def _horizon_deadline_ms(self, pos: Position) -> int:
        """When the capital has to come back, in epoch ms.

        The earlier of the 60-minute horizon and whatever explicit
        ``time_stop_ms`` the position was opened with — a deadline is a ceiling,
        never a licence to hold longer — plus the extension once, and only once,
        a winner has earned it.
        """
        deadline = pos.entry_ms + int(max(0.0, float(self.horizon.time_stop_seconds)) * 1000)
        if pos.time_stop_ms > 0:
            deadline = min(deadline, pos.time_stop_ms)
        if pos.extended:
            deadline += int(max(0.0, float(self.horizon.extension_seconds)) * 1000)
        return deadline

    def _may_extend(self, pos: Position, price: float, signal_strength: float) -> bool:
        """Extension is for protected winners only.

        Every condition here exists to block the same failure: holding a losing
        trade past its deadline in the hope it comes back.  Unrealised profit
        must be positive *net* of the round trip, the trail must already be
        armed so that profit cannot evaporate during the extension, the live
        signal must still be exceptional, and a position may extend once.
        """
        h = self.horizon
        if not h.allow_extension or h.extension_seconds <= 0:
            return False
        if pos.extended:
            return False
        if not (self.cfg.trailing_enabled and pos.trailing_armed):
            return False
        if self._net_unrealized_usd(pos, price) <= 0:
            return False
        return float(signal_strength) >= EXTENSION_MIN_SIGNAL_STRENGTH

    @staticmethod
    def _net_unrealized_usd(pos: Position, price: float) -> float:
        """Open P&L after the modelled round trip — never the gross number.

        With V2 targets the costs are already itemised; without them the entry
        fee is the only honest estimate we have, so the exit leg is assumed to
        cost the same.
        """
        gross = pos.unrealized_usd(price)
        targets = pos.targets
        if targets is not None:
            return gross - targets.costs.total_usd
        return gross - 2.0 * max(0.0, pos.entry_fees_usd)

    @staticmethod
    def _target_bps(pos: Position) -> float:
        """How far this position set out to travel, in bps."""
        targets = pos.targets
        if targets is not None and targets.gross_move_required_bps > 0:
            return targets.gross_move_required_bps
        if pos.target_bps > 0:
            return pos.target_bps
        if pos.take_profit > 0 and pos.entry_price > 0:
            return abs(pos.take_profit - pos.entry_price) / pos.entry_price * _BPS
        return 0.0

    @staticmethod
    def _momentum_fading(pos: Position, f: Features | None) -> bool:
        """Has the reason we are in this trade stopped being true?

        No features at all counts as fading: an open profit we can no longer
        justify is an open profit worth protecting.
        """
        if f is None:
            return True
        direction = 1.0 if pos.side is Side.LONG else -1.0
        return (
            direction * f.ema_slope_bps <= 0.0
            or direction * f.trade_flow_imbalance <= 0.0
        )

    def _trailing_hit(self, pos: Position, price: float) -> bool:
        cfg = self.cfg
        if not cfg.trailing_enabled or not pos.trailing_armed:
            return False
        best = pos.best_price or pos.entry_price
        excursion = (
            (best - pos.entry_price) if pos.side is Side.LONG else (pos.entry_price - best)
        )
        if excursion <= 0:
            return False
        give_back = (best - price) if pos.side is Side.LONG else (price - best)
        return give_back >= cfg.trailing_give_back_frac * excursion

    def _time_stop_hit(self, pos: Position, price: float, ts_ms: int) -> bool:
        cfg = self.cfg
        limit_s = float(cfg.time_stop_seconds)
        if pos.time_stop_ms > 0:
            limit_s = max((pos.time_stop_ms - pos.entry_ms) / 1000.0, 0.0)
        if limit_s <= 0:
            return False

        held = pos.holding_seconds(ts_ms)
        if held > limit_s:
            return True

        # Half-time staleness: capital parked in a trade that is going nowhere
        # is capital not being recycled, which is the whole edge of this bot.
        if held >= limit_s / 2.0:
            target_bps = pos.target_bps
            if target_bps <= 0 and pos.take_profit > 0 and pos.entry_price > 0:
                target_bps = abs(pos.take_profit - pos.entry_price) / pos.entry_price * _BPS
            if target_bps > 0 and pos.unrealized_bps(price) < cfg.stale_exit_frac * target_bps:
                return True
        return False

    def update_trailing(self, pos: Position, price: float) -> None:
        """Track the best excursion and arm the trail once it is worth arming.

        Called by the worker before :meth:`check` so that ``check`` itself stays
        free of side effects.  The trail arms at ``trailing_arm_frac`` of the
        distance to the TP — early enough to protect a move that stalls, late
        enough that ordinary noise does not scratch the trade.
        """
        if not self.cfg.trailing_enabled or price <= 0:
            return
        if pos.best_price <= 0:
            pos.best_price = pos.entry_price
        if pos.side is Side.LONG:
            pos.best_price = max(pos.best_price, price)
            progress = price - pos.entry_price
            span = pos.take_profit - pos.entry_price
        else:
            pos.best_price = min(pos.best_price, price)
            progress = pos.entry_price - price
            span = pos.entry_price - pos.take_profit
        if not pos.trailing_armed and span > 0:
            if progress >= self.cfg.trailing_arm_frac * span:
                pos.trailing_armed = True
