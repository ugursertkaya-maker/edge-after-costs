"""Fee-aware NET profit engine (V2).

V1 asked "how far do we think this moves?".  This module asks the only question
that pays rent: *after Binance.US fees, the quoted spread, estimated slippage
and a latency buffer, how much money is actually left, and what price do we
have to reach to bank it?*

Three ideas drive everything here.

**A fixed dollar target is incoherent.**  $0.50 is 5% of a $10 position and
0.25% of a $200 one.  Targets are therefore NET *percentages* of the position
notional, and :class:`NetTargetEngine` solves **backwards** from the required
net dollars, through every cost line, to the gross price that delivers them.
The gross move required is consequently always larger than the net target —
that gap is the entire point of the structure.

**A gross gain is not profit.**  ``break_even_price`` is the price at which the
gross move exactly pays for the round trip: a position closed there nets zero,
not "a small profit".  Anything between the entry and the break-even is a
*loss* wearing a green number.

**Never round in our favour.**  Every price this module produces is rounded to
the symbol tick in the direction that makes the trade *harder*, so a target we
report as reached really was reached.

Costing convention, stated once because double-counting the spread is the
easiest way to build a model that lies: ``spread_cost`` covers *crossing the
touch*, and is charged once per leg that actually crosses (a maker leg rests
and pays none of it).  ``slippage_bps_entry`` / ``slippage_bps_exit`` are the
caller's estimate of impact **beyond** the touch — the book walk past the first
level — not the half-spread again.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from app.types import (
    CostBreakdown,
    Features,
    NetTargets,
    OrderBook,
    Side,
    Signal,
    SymbolInfo,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.config import Settings

_BPS = 10_000.0

__all__ = ["FeeProvider", "NetTargetEngine"]


def _finite(x: float, default: float = 0.0) -> float:
    return float(x) if math.isfinite(x) else default


def _round_away(price: float, tick: float, *, up: bool) -> float:
    """Round *price* to the tick grid, away from the entry.

    ``up`` ceils, otherwise floors.  The relative epsilon keeps a price that is
    already exactly on the grid from being pushed a whole tick further — the
    grid is floating point, and ``100.61 / 0.01 == 10060.999999999998``.
    """
    if tick <= 0 or not math.isfinite(price):
        return price
    n = price / tick
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(n * tick, 10)


class FeeProvider:
    """Per-symbol maker/taker rates, behind an interface that can be swapped.

    Today every rate comes from ``settings.fees``, which models the published
    Binance.US schedule (Tier-0 pairs at 0% maker / 0.01% taker, everything else
    at the standard tier).  That is a *public* schedule, not our account's, and
    the two diverge the moment volume tiers or promotions apply.

    TODO (authenticated phase, deliberately NOT implemented here): replace the
    body of :meth:`refresh` with a call to the account trading-fee endpoint —
    Binance.US ``GET`` on the asset ``query/trading-fee`` resource under the
    authenticated SAPI prefix — and cache its ``makerCommission`` /
    ``takerCommission`` per symbol.  V1/V2 are PAPER-only and the paper-only
    guard in ``app/safety.py`` fails the build on that path, which is exactly
    the intended outcome: the seam exists, the call does not.
    """

    def __init__(self, *, settings: "Settings | None" = None) -> None:
        if settings is None:
            from app.config import get_settings

            settings = get_settings()
        self.settings = settings
        # Populated by an authenticated source later; empty means "ask config".
        self._maker: dict[str, float] = {}
        self._taker: dict[str, float] = {}

    def maker_bps(self, symbol: str) -> float:
        cached = self._maker.get(symbol)
        if cached is not None:
            return cached
        return max(0.0, float(self.settings.fees.maker_bps(symbol)))

    def taker_bps(self, symbol: str) -> float:
        cached = self._taker.get(symbol)
        if cached is not None:
            return cached
        return max(0.0, float(self.settings.fees.taker_bps(symbol)))

    def side_bps(self, symbol: str, *, is_maker: bool) -> float:
        return self.maker_bps(symbol) if is_maker else self.taker_bps(symbol)

    def refresh(self) -> None:
        """No-op today; the seam where account-specific rates will land.

        Dropping the per-account rates in here is the whole extension point:
        every caller already asks this object, so nothing above it changes.
        """
        self._maker.clear()
        self._taker.clear()


class NetTargetEngine:
    """Solves backwards from a required NET profit to the price that pays it."""

    def __init__(
        self,
        *,
        settings: "Settings | None" = None,
        fees: FeeProvider | None = None,
    ) -> None:
        if settings is None:
            from app.config import get_settings

            settings = get_settings()
        self.settings = settings
        self.cfg = settings.profit
        self.fees = fees if fees is not None else FeeProvider(settings=settings)

    # ------------------------------------------------------------------ #
    # Costs
    # ------------------------------------------------------------------ #

    @staticmethod
    def _spread_bps(book: OrderBook | None) -> float:
        if book is None:
            return 0.0
        try:
            s = book.spread_bps
        except (IndexError, ZeroDivisionError):  # pragma: no cover - defensive
            return 0.0
        return max(0.0, _finite(s, 0.0))

    def estimate_costs(
        self,
        *,
        symbol: str,
        notional_usd: float,
        book: OrderBook | None,
        entry_is_maker: bool,
        exit_is_maker: bool,
        slippage_bps_entry: float,
        slippage_bps_exit: float,
    ) -> CostBreakdown:
        """Every dollar standing between a gross price move and money banked.

        Fees are the real per-symbol rates for the leg type actually used; the
        spread is charged only on legs that cross; slippage is whatever the
        caller measured; the latency buffer is the flat insurance premium from
        ``settings.profit.latency_buffer_bps`` for the gap between deciding and
        being filled.
        """
        notional = max(0.0, _finite(notional_usd))
        if notional <= 0:
            return CostBreakdown()

        entry_fee = notional * self.fees.side_bps(symbol, is_maker=entry_is_maker) / _BPS
        exit_fee = notional * self.fees.side_bps(symbol, is_maker=exit_is_maker) / _BPS

        crossing_legs = (0 if entry_is_maker else 1) + (0 if exit_is_maker else 1)
        half_spread_bps = self._spread_bps(book) / 2.0
        spread_cost = notional * half_spread_bps * crossing_legs / _BPS

        entry_slip = notional * max(0.0, _finite(slippage_bps_entry)) / _BPS
        exit_slip = notional * max(0.0, _finite(slippage_bps_exit)) / _BPS
        latency = notional * max(0.0, float(self.cfg.latency_buffer_bps)) / _BPS

        return CostBreakdown(
            entry_fee_usd=entry_fee,
            exit_fee_usd=exit_fee,
            spread_cost_usd=spread_cost,
            entry_slippage_usd=entry_slip,
            exit_slippage_usd=exit_slip,
            latency_buffer_usd=latency,
        )

    # ------------------------------------------------------------------ #
    # Targets
    # ------------------------------------------------------------------ #

    def build(
        self,
        *,
        symbol: str,
        side: Side,
        entry_price: float,
        notional_usd: float,
        qty: float,
        book: OrderBook | None,
        info: SymbolInfo,
        features: Features,
        signal: Signal,
        profile: str | None = None,
        entry_is_maker: bool = False,
        exit_is_maker: bool = False,
        slippage_bps_entry: float = 0.0,
        slippage_bps_exit: float = 0.0,
        stop_price: float | None = None,
    ) -> NetTargets:
        """Return the three prices a position lives by, plus the costs behind them.

        Works backwards:  required net dollars → plus every cost line → the
        gross dollars the move must produce → the price that produces them.
        """
        if entry_price <= 0:
            raise ValueError("entry_price must be positive")

        notional = max(0.0, _finite(notional_usd))
        if notional <= 0:
            notional = max(0.0, _finite(qty)) * entry_price
        size = _finite(qty)
        if size <= 0:
            size = notional / entry_price
        if size <= 0:
            raise ValueError("qty and notional_usd cannot both be zero")

        name = (profile or self.cfg.profile or "base").lower()
        net_pct = float(self.cfg.target_pct(name))
        required_net_usd = notional * net_pct / 100.0

        costs = self.estimate_costs(
            symbol=symbol,
            notional_usd=notional,
            book=book,
            entry_is_maker=entry_is_maker,
            exit_is_maker=exit_is_maker,
            slippage_bps_entry=slippage_bps_entry,
            slippage_bps_exit=slippage_bps_exit,
        )

        long = side is Side.LONG
        tick = info.tick_size if info.tick_size > 0 else 0.0
        notes: list[str] = []

        # --- the two solved prices ------------------------------------- #
        # Break-even: gross profit == total cost, so a position closed here
        # nets exactly zero.  Rounded away from the entry so the reported
        # level is never optimistic.
        be_move = costs.total_usd / size
        break_even = self._level(entry_price, be_move, long=long, tick=tick)

        # NET take-profit: gross profit == cost + the profile's required net.
        tp_move = (costs.total_usd + required_net_usd) / size
        net_tp = self._level(entry_price, tp_move, long=long, tick=tick)

        gross_move_bps = (
            (net_tp - entry_price) if long else (entry_price - net_tp)
        ) / entry_price * _BPS

        # --- stops ------------------------------------------------------ #
        stop = self._resolve_stop(side, entry_price, signal, stop_price, info)
        emergency = self._emergency_stop(side, entry_price, info)

        # --- what the signal claims, bounded by what volatility allows --- #
        atr_bps = max(0.0, _finite(features.atr_bps))
        budget_bps = float(self.cfg.max_target_atr_mult) * atr_bps
        claim_bps = max(0.0, _finite(signal.target_bps))
        if budget_bps > 0 and claim_bps > budget_bps:
            notes.append(
                f"signal target {claim_bps:.1f} bps capped to the ATR budget "
                f"{budget_bps:.1f} bps"
            )
            claim_bps = budget_bps
        expected_gross = notional * claim_bps / _BPS
        expected_net = expected_gross - costs.total_usd

        # --- reachability ----------------------------------------------- #
        reachable = True
        if atr_bps <= 0:
            reachable = False
            notes.append("no ATR estimate: target reachability cannot be verified")
        elif gross_move_bps > budget_bps:
            reachable = False
            notes.append(
                f"gross move required {gross_move_bps:.1f} bps exceeds the ATR "
                f"budget {budget_bps:.1f} bps ({self.cfg.max_target_atr_mult:g} "
                f"x {atr_bps:.1f}) — unreachable inside the horizon"
            )

        notes.append(
            f"net {net_pct:.2f}% = ${required_net_usd:.4f} + costs "
            f"${costs.total_usd:.4f} => gross ${required_net_usd + costs.total_usd:.4f} "
            f"({gross_move_bps:.1f} bps)"
        )

        return NetTargets(
            profile=name,
            net_tp_pct=net_pct,
            break_even_price=break_even,
            net_tp_price=net_tp,
            stop_price=stop,
            emergency_stop_price=emergency,
            gross_move_required_bps=gross_move_bps,
            expected_net_profit_usd=expected_net,
            expected_gross_profit_usd=expected_gross,
            costs=costs,
            entry_is_maker=entry_is_maker,
            exit_is_maker=exit_is_maker,
            reachable=reachable,
            notes=notes,
        )

    # ------------------------------------------------------------------ #
    # Gate
    # ------------------------------------------------------------------ #

    def qualifies(self, targets: NetTargets, *, notional_usd: float) -> bool:
        """Would this trade be worth doing at all?

        Four independent conditions, every one of them a way a "profitable"
        setup is actually a losing one:

        * the target has to be physically reachable inside the horizon;
        * expected NET profit has to be positive (gross-positive is not profit);
        * it has to clear the active profile's NET percentage of *this*
          notional, because a fixed dollar bar means nothing across sizes;
        * and the expected gross has to clear the modelled costs by
          ``uncertainty_margin``, because the model's costs are an estimate and
          a trade whose entire edge is inside the error bar is not an edge.
        """
        notional = max(0.0, _finite(notional_usd))
        if notional <= 0:
            return False
        if not targets.reachable:
            return False

        net = _finite(targets.expected_net_profit_usd, -1.0)
        if net <= 0:
            return False

        required = notional * targets.net_tp_pct / 100.0
        if net < required:
            return False

        margin = max(1.0, float(self.cfg.uncertainty_margin))
        if targets.expected_gross_profit_usd < targets.costs.total_usd * margin:
            return False
        return True

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    @staticmethod
    def _level(entry_price: float, move: float, *, long: bool, tick: float) -> float:
        """Entry +/- *move*, rounded to the tick away from the entry.

        A LONG profits upwards, so its levels ceil; a SHORT profits downwards,
        so its levels floor.  Either way the rounding costs us, never the
        market, and the result is always at least one tick past the entry so a
        "target" can never sit on top of the fill price.
        """
        raw = entry_price + move if long else entry_price - move
        out = _round_away(raw, tick, up=long)
        step = tick if tick > 0 else entry_price * 1e-4
        if long and out <= entry_price:
            out = _round_away(entry_price + step, tick, up=True)
        elif not long and out >= entry_price:
            out = _round_away(entry_price - step, tick, up=False)
        return out

    def _resolve_stop(
        self,
        side: Side,
        entry_price: float,
        signal: Signal,
        stop_price: float | None,
        info: SymbolInfo,
    ) -> float:
        """The caller's stop if it gave one, otherwise the signal's own view."""
        long = side is Side.LONG
        if stop_price is not None and stop_price > 0:
            candidate = stop_price
        else:
            stop_bps = max(0.0, _finite(signal.stop_bps))
            if stop_bps <= 0:
                return self._emergency_stop(side, entry_price, info)
            candidate = (
                entry_price * (1.0 - stop_bps / _BPS)
                if long
                else entry_price * (1.0 + stop_bps / _BPS)
            )
        stop = info.round_price(candidate)
        # A stop is never wider than the emergency stop, and never on the wrong
        # side of the entry.
        emergency = self._emergency_stop(side, entry_price, info)
        if long:
            stop = max(stop, emergency)
            if stop >= entry_price:
                stop = info.round_price(entry_price - (info.tick_size or entry_price * 1e-4))
        else:
            stop = min(stop, emergency)
            if stop <= entry_price:
                stop = info.round_price(entry_price + (info.tick_size or entry_price * 1e-4))
        return stop

    def _emergency_stop(self, side: Side, entry_price: float, info: SymbolInfo) -> float:
        pct = max(0.0, float(self.settings.exits.emergency_stop_pct))
        if side is Side.LONG:
            return info.round_price(entry_price * (1.0 - pct / 100.0))
        return info.round_price(entry_price * (1.0 + pct / 100.0))
