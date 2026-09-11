"""Paper execution.

The point of this module is to be *pessimistic in the right places*.  A paper
simulator that fills at the mid, in full, instantly, will happily report a
profitable strategy that loses money the moment it meets a real book.  So:

* **Never fill at the mid.**  A buyer lifts the ask and a seller hits the bid.
  The mid is only ever the *reference* we measure slippage against.
* **Never fill more than the book holds.**  A market order walks the opposite
  side level by level, at most ``max_book_walk_levels`` deep, and whatever is
  left over is an honest partial fill (or a reject if nothing filled at all).
* **Charge for latency.**  Between the decision and the exchange seeing the
  order, the market moves against us.  We model that as an adverse price drift
  of ``adverse_bps * (latency_ms / 250)`` applied to every level we consume —
  250 ms is the measured round trip from the VPS, so the configured default
  costs exactly ``adverse_bps`` and every other profile scales linearly from
  it.  It is charged *on top of* the book walk, because the two effects are
  different things: the walk is the liquidity we consume, the drift is the
  liquidity that moved away before we got there.
* **Do not assume a limit order fills.**  A resting order fills only if the
  tape proves price actually traded through it after we placed it, only for as
  much size as actually traded, and even then it is skipped with probability
  ``limit_miss_ratio`` because being *at* the price is not the same as being
  *early enough in the queue* at that price.

``OrderResult.accepted`` means "this order produced a fill".  A limit order
that rested and never filled comes back ``accepted=False`` with a reject reason
that says why — nothing was traded, so there is nothing for the ledger to do.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from app.types import (
    Fill,
    OrderBook,
    OrderRequest,
    OrderResult,
    OrderType,
    Side,
    SymbolInfo,
    Trade,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.config import Settings

_BPS = 10_000.0
# The latency the adverse-drift model is calibrated at (measured VPS round trip).
_REFERENCE_LATENCY_MS = 250.0


def _lot_floor(info: SymbolInfo, qty: float) -> float:
    """``info.floor_qty`` with a tolerance for binary representation error.

    A quantity that is *already* a valid multiple of the step must not lose a
    whole step to ``1.23762 / 0.00001 == 123761.99999``.  Without this, closing
    a position leaves one step of dust behind — which is exactly the kind of
    bug that quietly breaks capital recycling.
    """
    if info.step_size <= 0:
        return qty
    return info.floor_qty(qty + info.step_size * 1e-9)


class PaperSimulator:
    """Turns an :class:`OrderRequest` into a plausible, costed fill."""

    def __init__(
        self,
        *,
        settings: "Settings | None" = None,
        rng: "random.Random | None" = None,
    ) -> None:
        if settings is None:
            from app.config import get_settings

            settings = get_settings()
        self.settings = settings
        self.cfg = settings.execution
        self.fees = settings.fees
        # Seeded by the caller in tests and by the worker in production, so a
        # run is reproducible from its seed alone.
        self.rng = rng if rng is not None else random.Random(0xC0FFEE)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def execute(
        self,
        req: OrderRequest,
        book: OrderBook,
        info: SymbolInfo,
        *,
        trades: list[Trade] | None = None,
        ts_ms: int | None = None,
        latency_ms: int | None = None,
    ) -> OrderResult:
        latency = int(self.cfg.latency_ms if latency_ms is None else latency_ms)
        decision_ms = int(req.ts_ms if ts_ms is None else ts_ms)

        reject = self._preflight(req, book, info, latency)
        if reject is not None:
            return reject

        if req.order_type is OrderType.MARKET:
            return self._execute_market(req, book, info, latency)
        return self._execute_limit(req, book, info, trades or [], decision_ms, latency)

    def estimate_slippage_bps(
        self, side: Side, notional_usd: float, book: OrderBook
    ) -> float:
        """Slippage a taker order of *notional_usd* would pay, in bps of mid.

        Same walk as :meth:`execute` at the configured default latency, with no
        mutation and no randomness, so the risk engine can pre-check a size
        before anyone commits to it.  If the book cannot absorb the notional,
        the unfilled remainder is priced at the deepest level we reached — a
        deliberate under-statement of a situation that would really be worse,
        flagged instead by the depth gate.
        """
        mid = book.mid
        if mid <= 0 or not book.is_valid() or notional_usd <= 0:
            return 0.0

        levels = book.asks if side is Side.LONG else book.bids
        drift = self._drift_factor(side, int(self.cfg.latency_ms))

        remaining = notional_usd
        spent = 0.0
        qty = 0.0
        last_price = 0.0
        for lv in levels[: self.cfg.max_book_walk_levels]:
            price = lv.price * drift
            last_price = price
            level_notional = price * lv.qty
            take = min(level_notional, remaining)
            if take <= 0:
                break
            qty += take / price
            spent += take
            remaining -= take
            if remaining <= 1e-12:
                break

        if qty <= 0:
            return 0.0
        if remaining > 1e-12 and last_price > 0:
            qty += remaining / last_price
            spent += remaining

        avg = spent / qty
        return self._slippage_bps(side, avg, mid)

    # ------------------------------------------------------------------ #
    # Market orders
    # ------------------------------------------------------------------ #

    def _execute_market(
        self, req: OrderRequest, book: OrderBook, info: SymbolInfo, latency: int
    ) -> OrderResult:
        mid = book.mid
        want = _lot_floor(info, req.qty)
        levels = book.asks if req.side is Side.LONG else book.bids
        drift = self._drift_factor(req.side, latency)
        taker_bps = self.fees.taker_bps(req.symbol)

        remaining = want
        fills: list[Fill] = []
        filled = 0.0
        notional = 0.0
        for lv in levels[: self.cfg.max_book_walk_levels]:
            if remaining <= 1e-12:
                break
            take = min(lv.qty, remaining)
            if take <= 0:
                continue
            price = lv.price * drift
            fee = price * take * taker_bps / _BPS
            fills.append(Fill(price=price, qty=take, fee_usd=fee, is_maker=False, ts_ms=req.ts_ms))
            filled += take
            notional += price * take
            remaining -= take

        filled = _lot_floor(info, filled)
        if filled <= 0:
            return self._reject(req, latency, "no_liquidity_in_book")

        if filled < want - 1e-12 and not self.cfg.partial_fill_enabled:
            return self._reject(req, latency, "insufficient_depth_for_full_fill")

        # Re-derive from the (possibly lot-floored) filled quantity so the
        # reported average price, fees and notional stay mutually consistent.
        avg = notional / sum(f.qty for f in fills)
        notional = avg * filled
        fees = notional * taker_bps / _BPS

        if not self._meets_min_notional(notional, info):
            return self._reject(req, latency, "below_min_notional")

        return OrderResult(
            accepted=True,
            order_type=OrderType.MARKET,
            requested_qty=req.qty,
            filled_qty=filled,
            avg_price=avg,
            fees_usd=fees,
            slippage_bps=self._slippage_bps(req.side, avg, mid),
            fills=fills,
            reject_reason=None if filled >= want - 1e-12 else "partial_fill",
            latency_ms=latency,
        )

    # ------------------------------------------------------------------ #
    # Limit orders
    # ------------------------------------------------------------------ #

    def _execute_limit(
        self,
        req: OrderRequest,
        book: OrderBook,
        info: SymbolInfo,
        trades: list[Trade],
        decision_ms: int,
        latency: int,
    ) -> OrderResult:
        limit = req.limit_price
        if limit is None or limit <= 0:
            return self._reject(req, latency, "limit_price_required")

        mid = book.mid
        marketable = (
            limit >= book.best_ask if req.side is Side.LONG else limit <= book.best_bid
        )
        if marketable:
            # Crossing the spread makes us the aggressor: same walk, taker fee,
            # but no fill worse than the limit we set.
            return self._execute_marketable_limit(req, book, info, latency, limit)

        # Resting order: our price is passive, so the tape has to come to us.
        arrival_ms = decision_ms + latency
        through = [
            t
            for t in trades
            if t.ts_ms >= arrival_ms
            and ((t.price <= limit) if req.side is Side.LONG else (t.price >= limit))
        ]
        if not through:
            return self._reject(req, latency, "no_trade_through_limit", order_type=OrderType.LIMIT)

        # Price came to us — but so did everyone else who was already in the
        # queue at this level.
        if self.rng.random() < self.cfg.limit_miss_ratio:
            return self._reject(req, latency, "queue_position_not_reached", order_type=OrderType.LIMIT)

        traded_qty = sum(t.qty for t in through)
        filled = _lot_floor(info, min(_lot_floor(info, req.qty), traded_qty))
        if filled <= 0:
            return self._reject(req, latency, "no_size_traded_through", order_type=OrderType.LIMIT)
        if filled < info.min_qty:
            return self._reject(req, latency, "below_min_qty", order_type=OrderType.LIMIT)

        notional = limit * filled
        if not self._meets_min_notional(notional, info):
            return self._reject(req, latency, "below_min_notional", order_type=OrderType.LIMIT)

        maker_bps = self.fees.maker_bps(req.symbol)
        fees = notional * maker_bps / _BPS
        return OrderResult(
            accepted=True,
            order_type=OrderType.LIMIT,
            requested_qty=req.qty,
            filled_qty=filled,
            avg_price=limit,
            fees_usd=fees,
            # A resting buy below the mid is *price improvement*, so maker
            # slippage is allowed to be negative; only the taker case is
            # clamped at zero.
            slippage_bps=self._signed_slippage_bps(req.side, limit, mid),
            fills=[Fill(price=limit, qty=filled, fee_usd=fees, is_maker=True, ts_ms=arrival_ms)],
            reject_reason=None if filled >= _lot_floor(info, req.qty) - 1e-12 else "partial_fill",
            latency_ms=latency,
        )

    def _execute_marketable_limit(
        self,
        req: OrderRequest,
        book: OrderBook,
        info: SymbolInfo,
        latency: int,
        limit: float,
    ) -> OrderResult:
        mid = book.mid
        want = _lot_floor(info, req.qty)
        levels = book.asks if req.side is Side.LONG else book.bids
        drift = self._drift_factor(req.side, latency)
        taker_bps = self.fees.taker_bps(req.symbol)

        remaining = want
        fills: list[Fill] = []
        notional = 0.0
        for lv in levels[: self.cfg.max_book_walk_levels]:
            if remaining <= 1e-12:
                break
            price = lv.price * drift
            if (req.side is Side.LONG and price > limit) or (
                req.side is Side.SHORT and price < limit
            ):
                break  # beyond our limit: the rest of the order rests unfilled
            take = min(lv.qty, remaining)
            fee = price * take * taker_bps / _BPS
            fills.append(Fill(price=price, qty=take, fee_usd=fee, is_maker=False, ts_ms=req.ts_ms))
            notional += price * take
            remaining -= take

        filled = _lot_floor(info, sum(f.qty for f in fills))
        if filled <= 0:
            return self._reject(req, latency, "no_liquidity_within_limit", order_type=OrderType.LIMIT)

        avg = notional / sum(f.qty for f in fills)
        notional = avg * filled
        if not self._meets_min_notional(notional, info):
            return self._reject(req, latency, "below_min_notional", order_type=OrderType.LIMIT)

        return OrderResult(
            accepted=True,
            order_type=OrderType.LIMIT,
            requested_qty=req.qty,
            filled_qty=filled,
            avg_price=avg,
            fees_usd=notional * taker_bps / _BPS,
            slippage_bps=self._slippage_bps(req.side, avg, mid),
            fills=fills,
            reject_reason=None if filled >= want - 1e-12 else "partial_fill",
            latency_ms=latency,
        )

    # ------------------------------------------------------------------ #
    # Shared helpers
    # ------------------------------------------------------------------ #

    def _preflight(
        self, req: OrderRequest, book: OrderBook, info: SymbolInfo, latency: int
    ) -> OrderResult | None:
        if req.qty <= 0:
            return self._reject(req, latency, "non_positive_qty")
        if not book.is_valid():
            # Empty, crossed or desynced: there is no price we could honestly
            # claim to have traded at.
            return self._reject(req, latency, "invalid_or_crossed_book")
        if _lot_floor(info, req.qty) < info.min_qty or _lot_floor(info, req.qty) <= 0:
            return self._reject(req, latency, "below_min_qty")
        reference = req.limit_price if req.order_type is OrderType.LIMIT and req.limit_price else book.mid
        if not self._meets_min_notional(_lot_floor(info, req.qty) * reference, info):
            return self._reject(req, latency, "below_min_notional")
        return None

    @staticmethod
    def _meets_min_notional(notional: float, info: SymbolInfo) -> bool:
        return info.min_notional <= 0 or notional >= info.min_notional - 1e-9

    def _drift_factor(self, side: Side, latency_ms: int) -> float:
        """Multiplicative adverse drift for the configured latency.

        Always against us: a buyer pays more, a seller receives less.
        """
        drift_bps = self.cfg.adverse_bps * (max(latency_ms, 0) / _REFERENCE_LATENCY_MS)
        if side is Side.LONG:
            return 1.0 + drift_bps / _BPS
        return 1.0 - drift_bps / _BPS

    @staticmethod
    def _signed_slippage_bps(side: Side, price: float, mid: float) -> float:
        if mid <= 0:
            return 0.0
        raw = (price - mid) / mid * _BPS
        return raw if side is Side.LONG else -raw

    def _slippage_bps(self, side: Side, price: float, mid: float) -> float:
        """Taker slippage vs the decision-time mid — never negative."""
        return max(0.0, self._signed_slippage_bps(side, price, mid))

    @staticmethod
    def _reject(
        req: OrderRequest,
        latency: int,
        reason: str,
        *,
        order_type: OrderType | None = None,
    ) -> OrderResult:
        return OrderResult(
            accepted=False,
            order_type=order_type or req.order_type,
            requested_qty=req.qty,
            filled_qty=0.0,
            avg_price=0.0,
            fees_usd=0.0,
            slippage_bps=0.0,
            fills=[],
            reject_reason=reason,
            latency_ms=latency,
        )
