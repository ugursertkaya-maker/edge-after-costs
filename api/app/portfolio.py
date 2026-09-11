"""Portfolio state, correlation, and capital allocation (V2).

V1 asked *"are we already holding three positions?"*.  That question is
economically meaningless: three $125 positions with 2% stops risk $7.50, and so
do thirty $12 positions with 0.2% stops, but only one of those books is
diversified.  A count is a proxy for risk, and a bad one.

So the count is gone.  What decides whether another position may open is:

* **capital** — cash that is actually deployable after the reserve
  (:attr:`PortfolioState.available_usd`), never the last dollar;
* **risk** — the sum of modelled losses if every stop is hit at once, capped at
  ``portfolio.total_open_risk_pct`` of equity;
* **correlation** — because eight simultaneous crypto longs are *one large
  crypto long*, and a naive sum of eight small risks would report one eighth of
  the truth (see :meth:`CorrelationEngine.correlated_risk`);
* **direction** — beta-adjusted net exposure, so "many small positions" cannot
  quietly become one enormous directional bet;
* **the exchange minimum** — a position under ``min_notional`` is not a small
  position, it is a rejected order.

``portfolio.max_open_positions`` (default 25) survives only as an emergency
ceiling.  It is checked **last**, so any rejection it produces means every
economic budget still had room — which is the signal that the ceiling, not the
economics, is binding, and that the ceiling should be raised rather than the
book trusted.

**Position size is not risk.**  A $20 position with a 1% stop risks ~$0.20 plus
costs.  Every function here keeps notional and risk in separate variables for
exactly that reason.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from app.types import (
    Allocation,
    Bar,
    CostBreakdown,
    NetTargets,
    Opportunity,
    OrderBook,
    Position,
    PortfolioState,
    RejectReason,
    RiskVerdict,
    Side,
    SymbolInfo,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.config import Settings

_BPS = 10_000.0
_EPS = 1e-9

# A symbol we have no return history for is assumed to move with everything
# else.  Assuming independence would understate the book's true risk exactly
# when we know least about it, which is the wrong direction to be wrong in.
_UNKNOWN_RHO = 1.0

# Dynamic allocation shaping constants.  Every one of these maps onto an input
# the spec names explicitly (edge, volatility, liquidity, correlation); they are
# bounded multipliers rather than tiers, so no position is ever sized by a label.
_EDGE_WEIGHT_MIN = 0.5
_EDGE_WEIGHT_MAX = 2.0
_VOL_WEIGHT_MIN = 0.5
_VOL_WEIGHT_MAX = 1.5
# Never ask the visible book for more than this fraction of the resting size
# within 10 bps of mid: past that, our own order is the slippage.
_LIQUIDITY_FRACTION = 0.10
# A candidate perfectly correlated with the open book gets at most half the
# capital an uncorrelated one would.
_CORRELATION_PENALTY = 0.5


# --------------------------------------------------------------------------- #
# Correlation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CorrelationView:
    """Pairwise correlation over ``portfolio.correlation_window_bars``, plus
    each symbol's beta to BTC.

    ``correlations`` is keyed by an ordered tuple ``(a, b)`` with ``a < b``;
    use :meth:`rho`, which normalises the key order for you.  A symbol absent
    from ``btc_beta`` is a symbol we had no usable history for — callers that
    care about being safe rather than optimistic treat it as unknown and assume
    the worst (see :data:`_UNKNOWN_RHO`).
    """

    correlations: dict[tuple[str, str], float] = field(default_factory=dict)
    btc_beta: dict[str, float] = field(default_factory=dict)

    def known(self, symbol: str) -> bool:
        return symbol in self.btc_beta

    def rho(self, a: str, b: str) -> float:
        """Measured correlation between two symbols; 1.0 for a symbol with itself."""
        if a == b:
            return 1.0
        key = (a, b) if a < b else (b, a)
        value = self.correlations.get(key)
        return 0.0 if value is None else value

    def beta(self, symbol: str) -> float:
        """Sensitivity to BTC.  1.0 when unmeasured — assume it moves with the market."""
        return self.btc_beta.get(symbol, 1.0)

    def cluster(self, symbols: list[str], threshold: float) -> list[list[str]]:
        """Single-linkage clusters of symbols whose |rho| reaches ``threshold``.

        Single linkage is deliberate: if A moves with B and B moves with C, then
        a stop-out cascade reaches C whether or not A and C look related in the
        sample.  Order is deterministic — clusters come back in the order their
        first member appears in ``symbols``.
        """
        unique: list[str] = []
        for s in symbols:
            if s not in unique:
                unique.append(s)

        parent = {s: s for s in unique}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i, a in enumerate(unique):
            for b in unique[i + 1 :]:
                if abs(self.rho(a, b)) >= threshold:
                    ra, rb = find(a), find(b)
                    if ra != rb:
                        parent[rb] = ra

        groups: dict[str, list[str]] = {}
        for s in unique:
            groups.setdefault(find(s), []).append(s)
        # Deterministic: order clusters by where their first member appeared.
        return sorted(groups.values(), key=lambda g: unique.index(g[0]))


class CorrelationEngine:
    """Measures how much of the book is really one bet."""

    def __init__(self, *, settings: "Settings | None" = None) -> None:
        if settings is None:
            from app.config import get_settings

            settings = get_settings()
        self.settings = settings
        self.cfg = settings.portfolio

    # ------------------------------------------------------------------ #
    # Measurement
    # ------------------------------------------------------------------ #

    def update(self, bars_by_symbol: dict[str, list[Bar]]) -> CorrelationView:
        """Recompute the pairwise correlation matrix and BTC betas.

        Returns are simple close-to-close over the trailing
        ``correlation_window_bars``; series are aligned from the *end*, because
        the most recent overlapping bars are the ones a correlated stop-out
        cascade would actually run through.
        """
        window = max(int(self.cfg.correlation_window_bars), 2)
        returns: dict[str, list[float]] = {}
        for symbol in sorted(bars_by_symbol):
            rets = _returns(bars_by_symbol[symbol], window)
            if len(rets) >= 3:  # two points make any correlation exactly +-1
                returns[symbol] = rets

        symbols = sorted(returns)
        correlations: dict[tuple[str, str], float] = {}
        for i, a in enumerate(symbols):
            for b in symbols[i + 1 :]:
                ra, rb = _align(returns[a], returns[b])
                if len(ra) >= 3:
                    correlations[(a, b)] = _pearson(ra, rb)

        btc = _btc_symbol(symbols)
        btc_beta: dict[str, float] = {}
        for s in symbols:
            if btc is None:
                btc_beta[s] = 1.0
            elif s == btc:
                btc_beta[s] = 1.0
            else:
                ra, rb = _align(returns[s], returns[btc])
                btc_beta[s] = _beta(ra, rb) if len(ra) >= 3 else 1.0

        return CorrelationView(correlations=correlations, btc_beta=btc_beta)

    # ------------------------------------------------------------------ #
    # Aggregation
    # ------------------------------------------------------------------ #

    def correlated_risk(
        self, positions: list[Position], view: CorrelationView | None
    ) -> float:
        """Risk aggregated across correlated positions — **not** a naive sum.

        The formula is the standard quadratic form::

            correlated_risk = sqrt( w' R w )
                            = sqrt( sum_i sum_j w_i * w_j * rho(i, j) )

        where ``w_i`` is position *i*'s modelled dollar loss at its stop and
        ``R`` is the correlation matrix.  Two limiting cases show why this is
        the only defensible aggregation:

        * **perfectly correlated** (rho = 1 everywhere): ``w'Rw = (sum w)^2``,
          so the result is ``sum w``.  Eight $1 risks that all move together
          aggregate to $8 — one large position, which is the truth.
        * **uncorrelated** (rho = 0 off the diagonal): ``w'Rw = sum w^2``, so
          the result is ``sqrt(8) ~ $2.83``.  Independent risks add in
          quadrature; claiming $8 there would over-reserve.

        A naive sum reports $8 in both cases (over-cautious when diversified);
        counting each position separately against a per-trade cap reports $1
        (catastrophically optimistic when concentrated).  Neither is usable.

        Negative correlations can in principle make the form negative if the
        measured matrix is not positive semi-definite; the result is floored at
        zero rather than allowed to become imaginary.
        """
        if not positions:
            return 0.0
        symbols = [p.symbol for p in positions]
        weights = [max(float(p.risk_usd), 0.0) for p in positions]
        return self.aggregate(symbols, weights, view)

    def aggregate(
        self,
        symbols: Sequence[str],
        weights: Sequence[float],
        view: CorrelationView | None,
    ) -> float:
        """:meth:`correlated_risk` over raw ``(symbol, risk_usd)`` vectors.

        Used by the allocator to price a *candidate* into the book before the
        position exists.
        """
        if not symbols:
            return 0.0
        total = 0.0
        for i, a in enumerate(symbols):
            for j, b in enumerate(symbols):
                total += weights[i] * weights[j] * _rho_safe(view, a, b)
        return math.sqrt(total) if total > 0.0 else 0.0

    def directional_exposure_pct(
        self,
        positions: list[Position],
        equity: float,
        view: CorrelationView | None,
    ) -> float:
        """Beta-adjusted net directional exposure as a percentage of equity.

        Longs count positive, shorts negative, each scaled by its beta to BTC —
        a 2-beta altcoin long is two units of market exposure, not one.  The
        magnitude is returned, because a net-short book is exactly as
        directional as a net-long one.
        """
        if equity <= 0:
            return 0.0
        net = 0.0
        for p in positions:
            sign = 1.0 if p.side is Side.LONG else -1.0
            beta = view.beta(p.symbol) if view is not None else 1.0
            net += sign * beta * float(p.notional_usd)
        return abs(net) / equity * 100.0


# --------------------------------------------------------------------------- #
# Portfolio
# --------------------------------------------------------------------------- #


class PortfolioEngine:
    """Builds portfolio state, answers "may we open this?", and allocates."""

    def __init__(
        self,
        *,
        settings: "Settings | None" = None,
        correlation: CorrelationEngine | None = None,
    ) -> None:
        if settings is None:
            from app.config import get_settings

            settings = get_settings()
        self.settings = settings
        self.cfg = settings.portfolio
        self.correlation = correlation or CorrelationEngine(settings=settings)
        # The most recent view, so can_open()/allocate() can price a candidate
        # into the book without being handed the bars a second time.
        self.view: CorrelationView | None = None

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #

    def state(
        self,
        ledger: Any,
        *,
        ts_ms: int,
        bars_by_symbol: dict[str, list[Bar]] | None = None,
    ) -> PortfolioState:
        """Snapshot the whole book at ``ts_ms``.

        The reserve is a fraction of **equity**, not of cash: as cash drains
        into positions a cash-based reserve shrinks with it and stops reserving
        anything, which is the opposite of what a reserve is for.
        """
        if bars_by_symbol:
            self.view = self.correlation.update(bars_by_symbol)

        positions = _positions_of(ledger)
        equity = float(getattr(ledger, "equity", 0.0))
        cash = float(getattr(ledger, "cash", 0.0))
        deployed = float(
            getattr(ledger, "exposure_usd", None)
            if getattr(ledger, "exposure_usd", None) is not None
            else sum(p.notional_usd for p in positions)
        )

        return PortfolioState(
            ts_ms=ts_ms,
            equity=equity,
            cash=cash,
            deployed_usd=deployed,
            reserve_required_usd=max(equity, 0.0) * self.cfg.cash_reserve_pct / 100.0,
            open_risk_usd=sum(max(float(p.risk_usd), 0.0) for p in positions),
            correlated_risk_usd=self.correlation.correlated_risk(positions, self.view),
            directional_exposure_pct=self.correlation.directional_exposure_pct(
                positions, equity, self.view
            ),
            positions=positions,
        )

    # ------------------------------------------------------------------ #
    # The gate
    # ------------------------------------------------------------------ #

    def can_open(
        self,
        opp: Opportunity,
        state: PortfolioState,
        *,
        risk_usd: float,
        notional_usd: float,
    ) -> RejectReason | None:
        """``None`` means yes.  Otherwise the first budget that refuses.

        Called twice per decision: once with zeroes, before sizing, to catch a
        budget that is *already* exhausted, and once with the real numbers
        afterwards, because correlation and directional exposure cannot be
        known until the size is.

        The emergency count ceiling is checked **last** on purpose: if it is the
        reason, every economic budget still had room, and that fact should be
        visible rather than hidden behind an arbitrary number.
        """
        cfg = self.cfg
        symbol = opp.symbol
        side = opp.signal.side
        equity = max(state.equity, 0.0)

        same_symbol = sum(1 for p in state.positions if p.symbol == symbol)
        if same_symbol >= cfg.max_positions_per_symbol:
            return RejectReason.DUPLICATE_SYMBOL

        # Cash reserve — set aside first, spent never.
        if state.available_usd <= _EPS or notional_usd > state.available_usd + _EPS:
            return RejectReason.CASH_RESERVE

        risk_budget = equity * cfg.total_open_risk_pct / 100.0
        if state.open_risk_usd + max(risk_usd, 0.0) > risk_budget + _EPS:
            return RejectReason.PORTFOLIO_RISK_BUDGET

        projected_corr = self.projected_correlated_risk(
            state, symbol=symbol, risk_usd=risk_usd
        )
        if projected_corr > equity * cfg.max_correlated_risk_pct / 100.0 + _EPS:
            return RejectReason.CORRELATED_EXPOSURE

        projected_dir = self.projected_directional_pct(
            state, symbol=symbol, side=side, notional_usd=notional_usd
        )
        if projected_dir > cfg.max_directional_exposure_pct + _EPS:
            return RejectReason.DIRECTIONAL_EXPOSURE

        if len(state.positions) >= cfg.max_open_positions:
            return RejectReason.MAX_POSITIONS

        return None

    def projected_correlated_risk(
        self, state: PortfolioState, *, symbol: str, risk_usd: float
    ) -> float:
        """Correlated risk of the book *with* a candidate risking ``risk_usd``."""
        symbols = [p.symbol for p in state.positions] + [symbol]
        weights = [max(float(p.risk_usd), 0.0) for p in state.positions] + [
            max(float(risk_usd), 0.0)
        ]
        return self.correlation.aggregate(symbols, weights, self.view)

    def projected_directional_pct(
        self, state: PortfolioState, *, symbol: str, side: Side, notional_usd: float
    ) -> float:
        """Directional exposure of the book *with* a candidate of ``notional_usd``."""
        equity = max(state.equity, 0.0)
        if equity <= 0:
            return 0.0
        net = 0.0
        for p in state.positions:
            sign = 1.0 if p.side is Side.LONG else -1.0
            beta = self.view.beta(p.symbol) if self.view is not None else 1.0
            net += sign * beta * float(p.notional_usd)
        sign = 1.0 if side is Side.LONG else -1.0
        beta = self.view.beta(symbol) if self.view is not None else 1.0
        net += sign * beta * max(float(notional_usd), 0.0)
        return abs(net) / equity * 100.0

    # ------------------------------------------------------------------ #
    # Allocation
    # ------------------------------------------------------------------ #

    def allocate(
        self,
        ranked_opportunities: list[Opportunity],
        state: PortfolioState,
        *,
        risk_engine: Any,
        infos: Mapping[str, SymbolInfo],
        books: Mapping[str, OrderBook],
        targets_by_symbol: Mapping[str, NetTargets] | None = None,
    ) -> list[Allocation]:
        """Rank-ordered greedy allocation across **all** qualified opportunities.

        Walks the ranked list once, sizes each candidate through the real risk
        engine, shapes it by ``allocation_mode``, and checks it against a
        portfolio state that is updated as allocations accumulate — so the
        budget is genuinely consumed and the tenth candidate faces the book the
        first nine created.  Deterministic: same inputs, same list, every time.

        Account-level blocks (kill switch, loss limits, cooldown, data health)
        are the caller's business — ask ``RiskEngine.blocked_reason`` before
        calling this; there is no account state to check them against here.

        "Many *allowed*" is not "many *taken*": every candidate still passes the
        risk engine's own gates independently, so a list of twenty rubbish
        setups allocates nothing.
        """
        if not ranked_opportunities:
            return []

        # The risk engine runs its own portfolio gate; make sure it is looking
        # at the same correlation view we are, or it can veto on stale data.
        peer = getattr(risk_engine, "portfolio", None)
        if isinstance(peer, PortfolioEngine) and peer is not self:
            peer.view = self.view

        working = _copy_state(state)
        allocations: list[Allocation] = []
        mode = (self.cfg.allocation_mode or "dynamic").lower()
        remaining = len(ranked_opportunities)
        ref_atr = _mean_atr_bps(ranked_opportunities)
        mean_edge = _mean_edge_bps(ranked_opportunities)

        for rank, opp in enumerate(ranked_opportunities, start=1):
            remaining -= 1
            if len(working.positions) >= self.cfg.max_open_positions:
                break
            if working.available_usd < self._floor_notional(None) - _EPS:
                # Nothing further can clear the exchange minimum: stop walking.
                break

            info = infos.get(opp.symbol)
            book = books.get(opp.symbol)
            if info is None or book is None:
                continue

            verdict = self._size(risk_engine, opp, working, info=info, book=book)
            if verdict is None or not verdict.approved or verdict.qty <= 0:
                continue

            entry = verdict.notional_usd / verdict.qty if verdict.qty > 0 else 0.0
            if entry <= 0:
                continue

            target_notional = self._mode_notional(
                mode,
                opp,
                working,
                verdict,
                book=book,
                remaining=remaining,
                ref_atr_bps=ref_atr,
                mean_edge_bps=mean_edge,
            )
            qty, notional, risk_usd, notes = self._shrink(
                verdict, target_notional, info=info, entry=entry
            )
            if qty <= 0:
                continue

            reason = self.can_open(
                opp, working, risk_usd=risk_usd, notional_usd=notional
            )
            if reason is not None:
                continue

            targets = (targets_by_symbol or {}).get(opp.symbol) or self._synth_targets(
                opp, entry=entry, notional_usd=notional, verdict=verdict
            )
            allocations.append(
                Allocation(
                    opportunity=opp,
                    qty=qty,
                    notional_usd=notional,
                    risk_usd=risk_usd,
                    targets=targets,
                    rank=rank,
                    notes=[f"mode={mode}", *notes, *verdict.notes],
                )
            )
            working = self._commit(working, opp, entry=entry, qty=qty,
                                   notional_usd=notional, risk_usd=risk_usd)

        return allocations

    # -- allocation helpers --------------------------------------------- #

    def _size(
        self,
        risk_engine: Any,
        opp: Opportunity,
        working: PortfolioState,
        *,
        info: SymbolInfo,
        book: OrderBook,
    ) -> RiskVerdict | None:
        from app.risk import RiskState

        rs = RiskState(
            equity=working.equity,
            open_positions=working.positions,
            realized_pnl_today=0.0,
            realized_pnl_week=0.0,
            loss_streak=0,
            cooldown_until_ms=0,
            kill_switch=False,
            data_healthy=True,
            ts_ms=working.ts_ms,
            portfolio=working,
        )
        return risk_engine.evaluate(opp, rs, info=info, book=book)

    def _mode_notional(
        self,
        mode: str,
        opp: Opportunity,
        working: PortfolioState,
        verdict: RiskVerdict,
        *,
        book: OrderBook,
        remaining: int,
        ref_atr_bps: float,
        mean_edge_bps: float,
    ) -> float:
        """The notional this *mode* wants, before the risk-sized cap is applied.

        Every mode is a downward shaping of what the risk engine already
        approved — a mode can spread capital thinner, never risk more.
        """
        cfg = self.cfg
        if mode == "few_large":
            # Take everything risk allows.  Capital, not a slot count, is what
            # makes the book small: $400 deployable at $125 a position is three.
            return min(verdict.notional_usd, cfg.max_position_usd)

        if mode == "many_small":
            # Sit on the floor and let breadth do the work.
            return max(cfg.min_position_usd, 0.0)

        # "dynamic": size purely from the inputs the spec names.
        share = working.available_usd / max(remaining + 1, 1)
        edge_w = _clamp(
            opp.net_edge_bps / mean_edge_bps if mean_edge_bps > 0 else 1.0,
            _EDGE_WEIGHT_MIN,
            _EDGE_WEIGHT_MAX,
        )
        atr = max(opp.features.atr_bps, _EPS)
        vol_w = _clamp(ref_atr_bps / atr if ref_atr_bps > 0 else 1.0,
                       _VOL_WEIGHT_MIN, _VOL_WEIGHT_MAX)
        corr_w = 1.0 - _CORRELATION_PENALTY * self._max_rho_to_book(working, opp.symbol)
        target = share * edge_w * vol_w * corr_w

        liquidity_cap = book.depth_usd("ask") * _LIQUIDITY_FRACTION
        if liquidity_cap > 0:
            target = min(target, liquidity_cap)
        return _clamp(target, cfg.min_position_usd, cfg.max_position_usd)

    def _max_rho_to_book(self, working: PortfolioState, symbol: str) -> float:
        if not working.positions:
            return 0.0
        return max(
            abs(_rho_safe(self.view, symbol, p.symbol)) for p in working.positions
        )

    def _floor_notional(self, info: SymbolInfo | None) -> float:
        """The smallest notional we are ever allowed to send."""
        floor = max(self.cfg.min_position_usd, self.settings.risk.min_notional_usd)
        if info is not None:
            floor = max(floor, info.min_notional)
        return floor

    def _shrink(
        self,
        verdict: RiskVerdict,
        target_notional: float,
        *,
        info: SymbolInfo,
        entry: float,
    ) -> tuple[float, float, float, list[str]]:
        """Scale a risk-approved verdict **down** to the mode's target.

        Risk scales with the position, so a proportional shrink is exact: half
        the quantity is half the dollars at the stop.  We never scale up — the
        risk engine's number is a ceiling, not a suggestion.
        """
        notes: list[str] = []
        qty = verdict.qty
        floor = self._floor_notional(info)

        if target_notional < verdict.notional_usd - _EPS:
            scale = max(target_notional, 0.0) / verdict.notional_usd
            qty = info.floor_qty(verdict.qty * scale + info.step_size * 1e-9)
            notes.append(f"alloc:target_notional={target_notional:.2f}")

        if qty <= 0 or qty < info.min_qty:
            return 0.0, 0.0, 0.0, notes
        notional = qty * entry
        if notional < floor - _EPS:
            # A mode that *aims* at the minimum lands just under it once the lot
            # size is floored: target $10.00, get 0.09998 units, get $9.999.
            # Refusing there would make "many_small" allocate literally nothing.
            # Step the quantity up to the smallest lot that clears the floor —
            # never past what the risk engine approved, which stays the ceiling.
            stepped = self._step_up_to_floor(qty, floor, info=info, entry=entry)
            if stepped is not None and stepped <= verdict.qty + _EPS:
                qty = stepped
                notional = qty * entry
                notes.append("alloc:stepped_up_to_min_notional")
            else:
                # Genuinely impossible: risk will not fund even one minimum lot.
                notes.append(f"alloc:below_min_notional({floor:.2f})")
                return 0.0, 0.0, 0.0, notes

        share = qty / verdict.qty if verdict.qty > 0 else 1.0
        return qty, notional, verdict.risk_usd * share, notes

    @staticmethod
    def _step_up_to_floor(
        qty: float, floor: float, *, info: SymbolInfo, entry: float
    ) -> float | None:
        """Smallest lot-aligned quantity worth at least ``floor`` dollars."""
        import math

        if entry <= 0 or info.step_size <= 0:
            return None
        needed = floor / entry
        steps = math.ceil(needed / info.step_size - 1e-9)
        candidate = round(steps * info.step_size, 10)
        if candidate < info.min_qty or candidate * entry < floor - _EPS:
            return None
        return candidate

    def _commit(
        self,
        working: PortfolioState,
        opp: Opportunity,
        *,
        entry: float,
        qty: float,
        notional_usd: float,
        risk_usd: float,
    ) -> PortfolioState:
        """Fold an allocation into the working state so the budget is consumed."""
        pos = Position(
            id=f"alloc:{opp.symbol}",
            symbol=opp.symbol,
            side=opp.signal.side,
            strategy=opp.signal.strategy,
            qty=qty,
            entry_price=entry,
            entry_ms=working.ts_ms,
            notional_usd=notional_usd,
            risk_usd=risk_usd,
            take_profit=0.0,
            stop_loss=0.0,
            entry_fees_usd=0.0,
            entry_slippage_bps=opp.slippage_bps,
            expected_net_edge_bps=opp.net_edge_bps,
        )
        positions = [*working.positions, pos]
        return replace(
            working,
            cash=working.cash - notional_usd,
            deployed_usd=working.deployed_usd + notional_usd,
            open_risk_usd=working.open_risk_usd + risk_usd,
            correlated_risk_usd=self.correlation.correlated_risk(positions, self.view),
            directional_exposure_pct=self.correlation.directional_exposure_pct(
                positions, working.equity, self.view
            ),
            positions=positions,
        )

    def _synth_targets(
        self,
        opp: Opportunity,
        *,
        entry: float,
        notional_usd: float,
        verdict: RiskVerdict,
    ) -> NetTargets:
        """A minimal NetTargets for callers that have no profit engine wired in.

        The real one comes from the fee-aware profit engine and is passed in via
        ``targets_by_symbol``; this stand-in keeps ``Allocation.targets``
        non-optional without inventing a better number than we have.
        """
        p = self.settings.profit
        cost_bps = (
            opp.fee_bps + opp.spread_cost_bps + opp.slippage_bps + p.latency_buffer_bps
        )
        net_tp_pct = p.target_pct()
        gross_required_bps = net_tp_pct * 100.0 + cost_bps
        long = opp.signal.side is Side.LONG
        sign = 1.0 if long else -1.0
        costs = CostBreakdown(
            entry_fee_usd=notional_usd * opp.fee_bps / 2.0 / _BPS,
            exit_fee_usd=notional_usd * opp.fee_bps / 2.0 / _BPS,
            spread_cost_usd=notional_usd * opp.spread_cost_bps / _BPS,
            entry_slippage_usd=notional_usd * opp.slippage_bps / 2.0 / _BPS,
            exit_slippage_usd=notional_usd * opp.slippage_bps / 2.0 / _BPS,
            latency_buffer_usd=notional_usd * p.latency_buffer_bps / _BPS,
        )
        return NetTargets(
            profile=p.profile,
            net_tp_pct=net_tp_pct,
            break_even_price=entry * (1.0 + sign * cost_bps / _BPS),
            net_tp_price=entry * (1.0 + sign * gross_required_bps / _BPS),
            stop_price=verdict.stop_loss,
            emergency_stop_price=entry
            * (1.0 - sign * self.settings.exits.emergency_stop_pct / 100.0),
            gross_move_required_bps=gross_required_bps,
            expected_net_profit_usd=notional_usd * net_tp_pct / 100.0,
            expected_gross_profit_usd=notional_usd * gross_required_bps / _BPS,
            costs=costs,
            notes=["synthesized: no profit engine targets supplied"],
        )


# --------------------------------------------------------------------------- #
# Maths helpers
# --------------------------------------------------------------------------- #


def _rho_safe(view: CorrelationView | None, a: str, b: str) -> float:
    """Correlation with the *conservative* fallback applied.

    With no view at all, or for a symbol we have no history for, assume the
    positions move together.  Being wrong that way over-reserves; being wrong
    the other way is how "many small positions" becomes one enormous bet.
    """
    if a == b:
        return 1.0
    if view is None or not view.known(a) or not view.known(b):
        return _UNKNOWN_RHO
    return view.rho(a, b)


def _returns(bars: list[Bar], window: int) -> list[float]:
    closes = [b.close for b in bars[-(window + 1) :] if b.close > 0]
    return [
        (closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))
    ]


def _align(a: list[float], b: list[float]) -> tuple[list[float], list[float]]:
    n = min(len(a), len(b))
    return a[-n:], b[-n:]


def _pearson(a: list[float], b: list[float]) -> float:
    n = len(a)
    if n < 2:
        return 0.0
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    if da <= _EPS or db <= _EPS:
        # A series that never moved has no correlation to measure.  Zero is the
        # honest answer; the unknown-symbol rule is what guards the risky case.
        return 0.0
    return _clamp(num / (da * db), -1.0, 1.0)


def _beta(a: list[float], market: list[float]) -> float:
    n = len(market)
    if n < 2:
        return 1.0
    mm = sum(market) / n
    var = sum((x - mm) ** 2 for x in market)
    if var <= _EPS:
        return 1.0
    ma = sum(a) / n
    cov = sum((x - ma) * (y - mm) for x, y in zip(a, market))
    return cov / var


def _btc_symbol(symbols: Iterable[str]) -> str | None:
    for s in sorted(symbols):
        if s.startswith("BTC"):
            return s
    return None


def _positions_of(ledger: Any) -> list[Position]:
    raw = getattr(ledger, "open_positions", [])
    if isinstance(raw, dict):
        return list(raw.values())
    return list(raw)


def _copy_state(state: PortfolioState) -> PortfolioState:
    return replace(state, positions=list(state.positions))


def _mean_atr_bps(opps: list[Opportunity]) -> float:
    vals = [o.features.atr_bps for o in opps if o.features.atr_bps > 0]
    return sum(vals) / len(vals) if vals else 0.0


def _mean_edge_bps(opps: list[Opportunity]) -> float:
    vals = [o.net_edge_bps for o in opps if o.net_edge_bps > 0]
    return sum(vals) / len(vals) if vals else 0.0


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
