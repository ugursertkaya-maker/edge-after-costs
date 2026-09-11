"""Position sizing and the veto layer.

This file answers one question — *may we open this, and how big?* — and it is
written to say **no** cheaply and **yes** carefully.

Three properties are load-bearing:

1. **Risk is sized from the stop, not from the position.**  We choose how many
   dollars we are willing to lose if the stop is hit (``risk_per_trade_pct`` of
   equity) and derive the quantity from the distance to that stop.  A tight
   stop therefore buys a *larger* position for the *same* risk.  Position size
   and account risk are different numbers and must never be conflated.
2. **Every clamp is recorded.**  When a size is cut, ``RiskVerdict.notes`` says
   which limit bound, so a suspiciously small position can be explained from
   the record instead of re-derived by hand.
3. **There is no martingale and no averaging down in this module.**  Nothing in
   the sizing path reads ``loss_streak``, realised P&L, or the outcome of any
   previous trade.  Losses shrink the next position only through equity, and
   only downwards.  ``loss_streak`` appears exactly once, in
   :meth:`blocked_reason`, where it *stops* trading rather than scaling it.

The account-level blocks (kill switch, loss limits, cooldown, data health) are
also exposed on their own via :meth:`blocked_reason` so the worker can stop
opening positions for a whole cycle without evaluating opportunities it is
going to reject anyway.

**V2 — the position count is no longer a limit.**  When ``RiskState.portfolio``
carries a :class:`~app.types.PortfolioState`, the old
``max_concurrent_positions`` cap is replaced wholesale by
:meth:`app.portfolio.PortfolioEngine.can_open`, and the concurrent-risk and
cash clamps are re-pointed at the portfolio budget and the cash reserve.  Two
consequences are deliberate:

* ``RiskConfig.max_concurrent_risk_pct`` is *superseded*, not intersected, by
  ``PortfolioConfig.total_open_risk_pct``.  Keeping the tighter V1 value would
  reinstate the three-position book under a different name — 1.5% of equity at
  0.5% risk per trade is exactly three trades — which is the behaviour V2
  exists to remove.
* With no ``portfolio`` attached, every path below behaves exactly as it did in
  V1.  That is the compatibility contract the existing tests pin.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.exits import ExitEngine
from app.portfolio import PortfolioEngine
from app.sim.simulator import PaperSimulator
from app.types import (
    Opportunity,
    OrderBook,
    PortfolioState,
    Position,
    Regime,
    RejectReason,
    RiskVerdict,
    Side,
    SymbolInfo,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.config import Settings

_BPS = 10_000.0


@dataclass
class RiskState:
    """Snapshot of the account the risk decision is made against.

    Assembled by the worker from the ledger; the risk engine never reaches for
    state of its own, which is what makes it trivially testable.
    """

    equity: float
    open_positions: list[Position]
    realized_pnl_today: float
    realized_pnl_week: float
    loss_streak: int
    cooldown_until_ms: int
    kill_switch: bool
    data_healthy: bool
    ts_ms: int
    # V2.  Optional and defaulted so every V1 caller and test is untouched:
    # absent means "decide by count, as before", present means "decide by risk
    # and capital".
    portfolio: PortfolioState | None = None


class RiskEngine:
    """The only place that decides size, and the last place that can say no."""

    def __init__(
        self,
        *,
        settings: "Settings | None" = None,
        portfolio: PortfolioEngine | None = None,
    ) -> None:
        if settings is None:
            from app.config import get_settings

            settings = get_settings()
        self.settings = settings
        self.cfg = settings.risk
        self.exits = ExitEngine(settings=settings)
        # Share one engine with the allocator when you can: it is what holds
        # the current correlation view, and a stale view is a wrong veto.
        self.portfolio = portfolio or PortfolioEngine(settings=settings)
        # Used only for its pure, non-mutating book walk, so that the size we
        # approve is pre-checked with exactly the model that will fill it.
        self._sim = PaperSimulator(settings=settings)

    # ------------------------------------------------------------------ #
    # Account-level blocks
    # ------------------------------------------------------------------ #

    def blocked_reason(self, state: RiskState) -> RejectReason | None:
        """Why the account may not open *anything* right now, or ``None``.

        Cheap, opportunity-independent, and checked first inside
        :meth:`evaluate` so the two can never disagree.
        """
        if state.kill_switch:
            return RejectReason.KILL_SWITCH
        if not state.data_healthy:
            return RejectReason.DATA_FAILURE
        if state.ts_ms < state.cooldown_until_ms:
            return RejectReason.LOSS_STREAK_COOLDOWN
        # A loss limit only exists while there is equity to lose: on a zero
        # account the limit collapses to 0 and would fire on a flat day, which
        # is a different (and far less useful) statement than "we are down 3%".
        equity = max(state.equity, 0.0)
        daily_limit = -equity * self.cfg.daily_loss_limit_pct / 100.0
        if daily_limit < 0 and state.realized_pnl_today <= daily_limit:
            return RejectReason.DAILY_LOSS_LIMIT
        weekly_limit = -equity * self.cfg.weekly_loss_limit_pct / 100.0
        if weekly_limit < 0 and state.realized_pnl_week <= weekly_limit:
            return RejectReason.WEEKLY_LOSS_LIMIT
        return None

    # ------------------------------------------------------------------ #
    # The decision
    # ------------------------------------------------------------------ #

    def evaluate(
        self,
        opp: Opportunity,
        state: RiskState,
        *,
        info: SymbolInfo,
        book: OrderBook,
    ) -> RiskVerdict:
        cfg = self.cfg
        notes: list[str] = []
        signal = opp.signal

        if signal.side is not Side.LONG:
            # Spot V1 cannot borrow.  The plumbing is symmetric; the permission
            # is not.
            return _reject(RejectReason.NOT_SUPPORTED_SHORT, notes)

        blocked = self.blocked_reason(state)
        if blocked is not None:
            return _reject(blocked, notes)

        if state.portfolio is None:
            # V1: a hard count cap, which is what V2 exists to delete.
            if len(state.open_positions) >= cfg.max_concurrent_positions:
                return _reject(RejectReason.MAX_POSITIONS, notes)

            same_symbol = [p for p in state.open_positions if p.symbol == opp.symbol]
            if len(same_symbol) >= cfg.max_positions_per_symbol:
                # This is also the anti-averaging-down rule: we never add to a
                # symbol we are already in.
                return _reject(RejectReason.DUPLICATE_SYMBOL, notes)
        else:
            # V2: capital and risk decide.  Zeroes here because the size is not
            # known yet — this pass catches a budget that is *already* spent;
            # the same gate runs again below with the real numbers.
            gate = self.portfolio.can_open(
                opp, state.portfolio, risk_usd=0.0, notional_usd=0.0
            )
            if gate is not None:
                return _reject(gate, notes)

        # ---- market quality ------------------------------------------------
        if not book.is_valid() or book.age_ms(state.ts_ms) > cfg.max_book_age_ms:
            return _reject(RejectReason.STALE_BOOK, notes)
        if book.spread_bps > cfg.max_entry_spread_bps:
            return _reject(RejectReason.WIDE_SPREAD, notes)
        # We buy the asks and will have to sell into the bids, so both sides
        # have to be there: a book we can enter but not leave is a trap.
        depth = min(book.depth_usd("ask"), book.depth_usd("bid"))
        if depth < cfg.min_book_depth_usd:
            return _reject(RejectReason.THIN_DEPTH, notes)

        # ---- edge ----------------------------------------------------------
        if opp.net_edge_bps <= 0:
            return _reject(RejectReason.NEGATIVE_EDGE, notes)
        if opp.net_edge_bps < cfg.min_net_edge_bps:
            return _reject(RejectReason.EDGE_BELOW_MIN, notes)
        if opp.slippage_bps > cfg.max_slippage_bps:
            return _reject(RejectReason.SLIPPAGE_TOO_HIGH, notes)
        if opp.regime.regime is Regime.ABNORMAL:
            return _reject(RejectReason.ABNORMAL_VOLATILITY, notes)

        # ---- levels --------------------------------------------------------
        entry = book.best_ask  # a long enters by lifting the offer
        if entry <= 0:
            return _reject(RejectReason.STALE_BOOK, notes)

        round_trip_cost_bps = opp.fee_bps + opp.spread_cost_bps + opp.slippage_bps
        take_profit, stop_loss = self.exits.initial_levels(
            signal.side,
            entry,
            opp.features,
            signal,
            round_trip_cost_bps=round_trip_cost_bps,
            info=info,
        )
        stop_distance = abs(entry - stop_loss)
        if stop_distance <= 0:
            return _reject(RejectReason.BAD_REWARD_RISK, notes)

        reward_risk = abs(take_profit - entry) / stop_distance
        if reward_risk < self.settings.exits.min_reward_risk:
            notes.append(f"reward_risk={reward_risk:.2f}")
            return _reject(RejectReason.BAD_REWARD_RISK, notes)

        # ---- sizing --------------------------------------------------------
        risk_budget = max(state.equity, 0.0) * cfg.risk_per_trade_pct / 100.0
        if risk_budget <= 0:
            return _reject(RejectReason.RISK_BUDGET, notes)

        # The denominator is what one unit can actually cost us: the distance
        # to the stop plus the slippage we expect to pay getting out of it.
        slippage_allowance = entry * max(opp.slippage_bps, 0.0) / _BPS
        per_unit_risk = stop_distance + slippage_allowance
        qty = risk_budget / per_unit_risk
        notes.append(f"risk_budget_usd={risk_budget:.4f}")
        notes.append(f"stop_distance={stop_distance:.10g}")

        qty, reason = self._apply_clamps(qty, entry, per_unit_risk, state, info, notes)
        if reason is not None:
            return _reject(reason, notes)

        qty = info.floor_qty(qty + info.step_size * 1e-9)
        if qty <= 0 or qty < info.min_qty:
            notes.append("clamp:lot_size")
            return _reject(RejectReason.BELOW_MIN_NOTIONAL, notes)

        notional = qty * entry
        min_notional = max(info.min_notional, cfg.min_notional_usd)
        if state.portfolio is not None:
            floor = self.settings.portfolio.min_position_usd
            if floor > min_notional:
                min_notional = floor
                notes.append(f"clamp:min_position_usd({floor:.2f})")
        if notional < min_notional - 1e-9:
            notes.append(f"clamp:min_notional({min_notional:.2f})")
            return _reject(RejectReason.BELOW_MIN_NOTIONAL, notes)

        # ---- post-sizing checks that depend on the size ---------------------
        expected_edge_usd = notional * opp.net_edge_bps / _BPS
        if expected_edge_usd < cfg.min_net_edge_usd:
            notes.append(f"expected_edge_usd={expected_edge_usd:.4f}")
            return _reject(RejectReason.EDGE_BELOW_MIN, notes)

        sized_slippage = self._sim.estimate_slippage_bps(signal.side, notional, book)
        if sized_slippage > cfg.max_slippage_bps:
            notes.append(f"sized_slippage_bps={sized_slippage:.2f}")
            return _reject(RejectReason.SLIPPAGE_TOO_HIGH, notes)

        risk_usd = qty * per_unit_risk

        if state.portfolio is not None:
            # Correlation and directional exposure cannot be judged until the
            # size exists, so the portfolio gate gets the last word.
            gate = self.portfolio.can_open(
                opp, state.portfolio, risk_usd=risk_usd, notional_usd=notional
            )
            if gate is not None:
                notes.append(f"portfolio_gate={gate.value}")
                return _reject(gate, notes)

        notes.append(f"notional_usd={notional:.4f}")
        return RiskVerdict(
            approved=True,
            qty=qty,
            notional_usd=notional,
            risk_usd=risk_usd,
            take_profit=take_profit,
            stop_loss=stop_loss,
            reject_reason=None,
            notes=notes,
        )

    # ------------------------------------------------------------------ #
    # Clamps
    # ------------------------------------------------------------------ #

    def _apply_clamps(
        self,
        qty: float,
        entry: float,
        per_unit_risk: float,
        state: RiskState,
        info: SymbolInfo,
        notes: list[str],
    ) -> tuple[float, RejectReason | None]:
        """Cut the size down to every budget in turn, noting which one bound.

        Order matters only for the note, not the result: the smallest bound
        always wins.  A budget that is already exhausted (zero or negative
        headroom) is a rejection, not a zero-size fill.

        With a ``PortfolioState`` attached, three of these budgets change hands:
        the absolute position cap gains ``max_position_usd``, the concurrent-risk
        budget becomes the portfolio's ``total_open_risk_pct``, and "available
        cash" becomes cash *after* the reserve.  Without one, every line is the
        V1 line.
        """
        cfg = self.cfg
        pcfg = self.settings.portfolio
        pf = state.portfolio
        equity = max(state.equity, 0.0)
        open_notional = sum(p.notional_usd for p in state.open_positions)

        # 1. single-position cap
        cap = equity * cfg.max_position_pct_equity / 100.0
        qty = _clamp(qty, cap / entry, "clamp:max_position_pct_equity", notes)
        if pf is not None:
            qty = _clamp(
                qty, pcfg.max_position_usd / entry, "clamp:max_position_usd", notes
            )

        # 2. total exposure, counting what is already on
        room = equity * cfg.max_total_exposure_pct / 100.0 - open_notional
        if room <= 0:
            notes.append("clamp:max_total_exposure_pct(exhausted)")
            return 0.0, RejectReason.RISK_BUDGET
        qty = _clamp(qty, room / entry, "clamp:max_total_exposure_pct", notes)

        # 3. the concurrent-risk budget, counting the risk already at stake
        if pf is None:
            risk_room = (
                equity * cfg.max_concurrent_risk_pct / 100.0
                - sum(p.risk_usd for p in state.open_positions)
            )
            if risk_room <= 0:
                notes.append("clamp:max_concurrent_risk_pct(exhausted)")
                return 0.0, RejectReason.RISK_BUDGET
            qty = _clamp(
                qty, risk_room / per_unit_risk, "clamp:max_concurrent_risk_pct", notes
            )
        else:
            risk_room = equity * pcfg.total_open_risk_pct / 100.0 - pf.open_risk_usd
            if risk_room <= 0:
                notes.append("clamp:portfolio_risk_budget(exhausted)")
                return 0.0, RejectReason.PORTFOLIO_RISK_BUDGET
            qty = _clamp(
                qty, risk_room / per_unit_risk, "clamp:portfolio_risk_budget", notes
            )

        # 4. cash actually available, with headroom left for the entry fee.
        fee_factor = 1.0 + self.settings.fees.taker_bps(info.symbol) / _BPS
        if pf is None:
            cash = equity - open_notional
            if cash <= 0:
                notes.append("clamp:available_cash(exhausted)")
                return 0.0, RejectReason.RISK_BUDGET
            qty = _clamp(qty, cash / (entry * fee_factor), "clamp:available_cash", notes)
        else:
            # The reserve is not spendable.  Ever.
            cash = pf.available_usd
            if cash <= 0:
                notes.append("clamp:cash_reserve(exhausted)")
                return 0.0, RejectReason.CASH_RESERVE
            qty = _clamp(qty, cash / (entry * fee_factor), "clamp:cash_reserve", notes)

        if qty <= 0:
            return 0.0, RejectReason.RISK_BUDGET
        return qty, None


def _clamp(qty: float, limit: float, note: str, notes: list[str]) -> float:
    if limit < qty:
        notes.append(note)
        return max(limit, 0.0)
    return qty


def _reject(reason: RejectReason, notes: list[str]) -> RiskVerdict:
    return RiskVerdict(approved=False, reject_reason=reason, notes=notes)
