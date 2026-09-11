"""Costing and ranking of signals.

A strategy says "I think this goes 20 bps".  This module answers the only
question that matters: *after* fees, spread and slippage, how many basis points
are actually left, and of the candidates that survive, which one deserves the
capital.

Two numbers come out of here and they mean different things:

``net_edge_bps``
    The honest expected value of the round trip, in bps of notional.  It is a
    hard gate — :meth:`OpportunityRanker.qualifies` rejects anything below
    ``settings.risk.min_net_edge_bps``.

``rank_score``
    A comparison key, not a P&L estimate.  It starts from ``net_edge_bps`` and
    then leans on things that do not belong in an edge estimate but do decide
    which of two positive-edge trades is the better use of a small account:
    what this strategy has historically done on this symbol in this regime,
    how deep the book is, how fast the capital comes back, and how much the
    quoted spread is going to hurt.
"""

from __future__ import annotations

from typing import Callable

from .config import Settings, get_settings
from .types import Features, Opportunity, Regime, RegimeState, Signal

# (strategy, symbol, regime) -> (historical_expectancy_bps, sample_size)
ExpectancyProvider = Callable[[str, str, str], tuple[float, int]]

# --- P(hit) ---------------------------------------------------------------- #
# Conviction is not probability.  A strategy score of 1.0 does not mean the
# target is certain, and a score of 0.0 does not mean it is impossible, so the
# blend is mapped into a deliberately narrow, pessimistic band.
P_HIT_FLOOR = 0.25
P_HIT_CEIL = 0.85
SIGNAL_WEIGHT = 0.70            # how much of P(hit) comes from signal.score
REGIME_WEIGHT = 0.30            # ...and how much from regime confidence

# --- rank_score weights ---------------------------------------------------- #
SHRINKAGE_K = 25.0              # Bayesian n/(n+k); 25 closed trades = half credit
HISTORY_WEIGHT = 0.5            # bps of shrunk history per bps of net edge
SPREAD_WEIGHT = 0.15            # penalty per bps of quoted spread
TURNOVER_REF_SECONDS = 300.0    # hold time at which the turnover factor is 0.75
LIQUIDITY_REF_MULT = 5.0        # depth vs min_book_depth_usd for full credit


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _null_expectancy(strategy: str, symbol: str, regime: str) -> tuple[float, int]:
    """Default provider: no history, so no opinion."""
    return 0.0, 0


def shrunk_expectancy(expectancy_bps: float, sample_size: int,
                      k: float = SHRINKAGE_K) -> float:
    """Pull a small-sample expectancy toward zero.

    Three winning trades is not evidence.  ``n / (n + k)`` is the standard
    Bayesian shrinkage factor for a mean with a zero-centred prior: at n = k the
    historical number gets half its face value, and it only approaches full
    credit in the hundreds.
    """
    n = max(0, int(sample_size))
    if n <= 0:
        return 0.0
    return float(expectancy_bps) * (n / (n + k))


class OpportunityRanker:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        expectancy: ExpectancyProvider | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.expectancy: ExpectancyProvider = expectancy or _null_expectancy

    # -- P(hit) ------------------------------------------------------------ #

    @staticmethod
    def p_hit(signal: Signal, regime: RegimeState) -> float:
        """``floor + (ceil - floor) * (0.7 * score + 0.3 * regime confidence)``.

        Both inputs are clipped to [0, 1] first, so the result is always inside
        [P_HIT_FLOOR, P_HIT_CEIL] and can never make gross edge exceed the
        strategy's own target.
        """
        score = _clamp(float(signal.score), 0.0, 1.0)
        conf = _clamp(float(regime.confidence), 0.0, 1.0)
        blend = SIGNAL_WEIGHT * score + REGIME_WEIGHT * conf
        return P_HIT_FLOOR + (P_HIT_CEIL - P_HIT_FLOOR) * blend

    # -- costing ----------------------------------------------------------- #

    @staticmethod
    def spread_cost_bps(f: Features, *, maker_bps: float) -> float:
        """Half the spread per crossing leg.

        The default assumption is that we cross on the way in *and* on the way
        out — two half-spreads, i.e. the full quoted spread.  A caller passing
        ``maker_bps == 0`` is telling us one leg rests instead of crossing, so
        only one half-spread is paid.
        """
        legs = 1.0 if maker_bps == 0.0 else 2.0
        return max(0.0, float(f.spread_bps)) * 0.5 * legs

    def build(
        self,
        signal: Signal,
        f: Features,
        regime: RegimeState,
        *,
        taker_bps: float,
        maker_bps: float,
        slippage_bps: float,
    ) -> Opportunity:
        p = self.p_hit(signal, regime)
        gross = max(0.0, float(signal.target_bps)) * p

        # Round trip: both legs pay, and the caller decides which rate applies
        # to which leg by what it passes in.
        fee_bps = max(0.0, float(maker_bps)) + max(0.0, float(taker_bps))
        spread_cost = self.spread_cost_bps(f, maker_bps=maker_bps)
        slip = max(0.0, float(slippage_bps))
        net = gross - fee_bps - spread_cost - slip

        hist_bps, n = self.expectancy(signal.strategy, signal.symbol, regime.regime.value)
        hold = max(1, int(signal.horizon_seconds))

        rank_score = self._rank_score(
            net_edge_bps=net,
            f=f,
            hist_bps=float(hist_bps),
            sample_size=int(n),
            hold_seconds=hold,
        )

        notes = [
            f"P(hit) {p:.2f} on score {signal.score:.2f} / regime {regime.confidence:.2f}",
            f"gross {gross:.2f} - fees {fee_bps:.2f} - spread {spread_cost:.2f} "
            f"- slip {slip:.2f} = net {net:.2f} bps",
        ]
        if n > 0:
            notes.append(
                f"history {hist_bps:+.1f} bps over {n} trades "
                f"(shrunk {shrunk_expectancy(hist_bps, n):+.1f})"
            )

        return Opportunity(
            signal=signal,
            features=f,
            regime=regime,
            gross_edge_bps=gross,
            fee_bps=fee_bps,
            spread_cost_bps=spread_cost,
            slippage_bps=slip,
            net_edge_bps=net,
            rank_score=rank_score,
            expected_hold_seconds=hold,
            historical_expectancy_bps=float(hist_bps),
            sample_size=int(n),
            notes=notes,
        )

    # -- ranking ----------------------------------------------------------- #

    def _rank_score(
        self,
        *,
        net_edge_bps: float,
        f: Features,
        hist_bps: float,
        sample_size: int,
        hold_seconds: int,
    ) -> float:
        base = (
            net_edge_bps
            + HISTORY_WEIGHT * shrunk_expectancy(hist_bps, sample_size)
            - SPREAD_WEIGHT * max(0.0, f.spread_bps)
        )
        return base * self._liquidity_factor(f) * self._turnover_factor(hold_seconds)

    def _liquidity_factor(self, f: Features) -> float:
        """0.5 (thin) .. 1.0 (deep), from the shallower side of the book."""
        floor_usd = max(1.0, self.settings.risk.min_book_depth_usd)
        thin_side = min(f.depth_bid_usd, f.depth_ask_usd)
        return 0.5 + 0.5 * _clamp(thin_side / (LIQUIDITY_REF_MULT * floor_usd), 0.0, 1.0)

    @staticmethod
    def _turnover_factor(hold_seconds: int) -> float:
        """0.5 .. 1.0; shorter holds recycle a $500 account faster."""
        ref = TURNOVER_REF_SECONDS
        return 0.5 + 0.5 * (ref / (ref + max(0.0, float(hold_seconds))))

    def qualifies(self, opp: Opportunity) -> bool:
        """Hard gate before the risk engine is even asked.

        Gross-positive / net-negative is a losing setup (brief §3), and an
        ``ABNORMAL`` regime blocks everything regardless of the arithmetic.
        """
        if opp.regime.regime is Regime.ABNORMAL:
            return False
        if opp.signal.stop_bps <= 0:
            return False
        if opp.net_edge_bps < 0.0:
            return False
        return opp.net_edge_bps >= self.settings.risk.min_net_edge_bps

    def rank(self, opps: list[Opportunity]) -> list[Opportunity]:
        """Best first.  Ties break on symbol then strategy, so the ordering is
        reproducible across runs and across backtest replays."""
        return sorted(
            opps,
            key=lambda o: (-o.rank_score, o.signal.symbol, o.signal.strategy),
        )
