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
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.types import ExitReason, Features, OrderBook, Position, Side, Signal, SymbolInfo

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.config import Settings

_BPS = 10_000.0


class ExitEngine:
    """Computes entry-time levels and, tick by tick, decides when to leave."""

    def __init__(self, *, settings: "Settings | None" = None) -> None:
        if settings is None:
            from app.config import get_settings

            settings = get_settings()
        self.settings = settings
        self.cfg = settings.exits

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
    ) -> ExitReason | None:
        """Return the reason to exit now, or ``None`` to hold.

        Priority is fixed and deliberate: losses first (STOP_LOSS), then
        realised targets (TAKE_PROFIT), then giving back an open profit
        (TRAILING_PROFIT), then a changed opinion (SIGNAL_REVERSAL), then the
        clock (TIME_STOP).  Two conditions at once always resolve to the one
        that costs the least to be wrong about.
        """
        price = self.exit_price(pos, book)
        if price <= 0:
            # No executable price on our side of the book: do not invent one.
            return None

        long = pos.side is Side.LONG

        if pos.stop_loss > 0:
            if (long and price <= pos.stop_loss) or (not long and price >= pos.stop_loss):
                return ExitReason.STOP_LOSS

        if pos.take_profit > 0:
            if (long and price >= pos.take_profit) or (
                not long and price <= pos.take_profit
            ):
                return ExitReason.TAKE_PROFIT

        if self._trailing_hit(pos, price):
            return ExitReason.TRAILING_PROFIT

        if reversal:
            return ExitReason.SIGNAL_REVERSAL

        if self._time_stop_hit(pos, price, ts_ms):
            return ExitReason.TIME_STOP

        return None

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
