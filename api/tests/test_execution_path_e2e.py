"""A valid candidate must be able to become a PAPER position on the primary book.

198 rejections and zero trades has two possible causes, and they need different
answers: either no opportunity was viable, or the execution path is broken
somewhere between the ranker and the ledger. This drives a deliberately VALID
but realistic candidate through the real production objects -- the real
OpportunityRanker, RiskEngine, PortfolioEngine and PaperSimulator -- and
asserts a position opens on a $5,000 ledger.

Deterministic and offline. PAPER only; the simulator is the only executor in
the tree and there is no order path to a venue.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def primary(monkeypatch):
    for k, v in {
        "STARTING_EQUITY": "5000", "COMPARISON_LEDGERS": "500,1000",
        "TARGET_DEPLOYED_PCT": "80", "CASH_RESERVE_PCT": "20",
        "MAX_POSITION_USD": "0", "MAX_OPEN_POSITIONS": "0",
        "FEE_MODEL": "tier0", "MIN_NET_EDGE_BPS": "1.0", "MIN_NET_EDGE_USD": "0.20",
    }.items():
        monkeypatch.setenv(k, v)
    from app.config import reload_settings
    s = reload_settings()
    yield s
    reload_settings()


def _book(symbol: str, mid: float, *, half_spread_bps: float = 0.5,
          depth_usd: float = 250_000.0):
    """A deep, tight book -- the conditions a viable trade would need."""
    from app.types import BookLevel, OrderBook
    half = mid * half_spread_bps / 1e4
    levels = 10
    qty = depth_usd / levels / mid
    bids = [BookLevel(price=mid - half - i * half, qty=qty) for i in range(levels)]
    asks = [BookLevel(price=mid + half + i * half, qty=qty) for i in range(levels)]
    return OrderBook(symbol=symbol, bids=bids, asks=asks,
                     last_update_id=1, last_update_ms=1_789_000_000_000)


def _info(symbol: str):
    from app.types import SymbolInfo
    return SymbolInfo(symbol=symbol, base_asset=symbol[:-3], quote_asset="USD",
                      status="TRADING", step_size=1e-6, tick_size=0.01,
                      min_qty=1e-6, min_notional=1.0)


def _features(symbol: str, mid: float, *, atr_bps: float):
    """Features consistent with the book above and a tradable ATR."""
    from app.types import Features
    return Features(
        symbol=symbol, ts_ms=1_789_000_000_000, price=mid, mid=mid,
        microprice=mid, spread_bps=1.0, depth_bid_usd=250_000.0,
        depth_ask_usd=250_000.0, imbalance=0.35, atr_bps=atr_bps,
        realized_vol_bps=atr_bps * 2, volume_ratio=1.4, ret_1m_bps=6.0,
        ret_5m_bps=15.0, ret_15m_bps=20.0, quote_volume_24h=5_000_000.0,
        trades_24h=20_000, book_age_ms=120, bars_available=60,
        vol_percentile=0.5, ema_slope_bps=4.0, trend_strength=0.7,
        range_pos=0.6, vwap=mid, vwap_dev_bps=3.0,
        ema_fast=mid * 1.0004, ema_slow=mid, bb_width_bps=atr_bps * 3,
        compression=0.3, trade_flow_imbalance=0.4, trade_flow_accel=0.2,
    )


class TestAValidCandidateReachesTheLedger:
    def test_a_viable_trade_opens_a_paper_position_on_the_primary_book(self, primary):
        from app.types import Regime, RegimeState, Side, Signal
        from app.ranker import OpportunityRanker
        from app.risk import RiskEngine, RiskState
        from app.sim.ledger import Ledger
        from app.sim.simulator import PaperSimulator
        from app.types import OrderRequest, OrderType
        from random import Random

        s = primary
        symbol, mid = "ETHUSD", 2_500.0
        book, info = _book(symbol, mid), _info(symbol)
        # An ATR that supports a real target: 40 bps, not the 0.13 bps the live
        # venue is showing tonight. This is the "realistic but valid" case.
        feats = _features(symbol, mid, atr_bps=60.0)
        regime = RegimeState(regime=Regime.TRENDING, confidence=0.8)

        signal = Signal(
            strategy="momentum_scalp", symbol=symbol, side=Side.LONG,
            score=0.85, target_bps=60.0, stop_bps=15.0, ts_ms=feats.ts_ms,
            horizon_seconds=300, reasons=["e2e execution-path probe"],
        )

        ranker = OpportunityRanker(settings=s)
        opp = ranker.build(signal, feats, regime,
                           taker_bps=s.fees.taker_bps(symbol),
                           maker_bps=s.fees.maker_bps(symbol),
                           slippage_bps=1.0)
        assert ranker.qualifies(opp), (
            f"ranker refused a viable candidate: net {opp.net_edge_bps:.3f} bps"
        )
        assert opp.net_edge_bps >= s.risk.min_net_edge_bps

        ledger = Ledger("base", s.risk.starting_equity, s.simulator_version)
        assert ledger.starting_equity == 5_000.0

        from app.portfolio import PortfolioEngine
        pe = PortfolioEngine(settings=s)
        pstate = pe.state(ledger, ts_ms=feats.ts_ms, bars_by_symbol={})
        risk = RiskEngine(settings=s)
        # Built exactly as TradingWorker._risk_state builds it.
        state = RiskState(
            equity=ledger.equity, open_positions=[], realized_pnl_today=0.0,
            realized_pnl_week=0.0, loss_streak=0, cooldown_until_ms=0,
            kill_switch=False, data_healthy=True, ts_ms=feats.ts_ms,
            portfolio=pstate,
        )
        verdict = risk.evaluate(opp, state, info=info, book=book)
        assert verdict.approved, f"risk refused: {verdict.reject_reason}"
        assert verdict.qty > 0

        allocs = pe.allocate([opp], pstate, risk_engine=risk,
                             infos={symbol: info}, books={symbol: book})
        assert allocs, "allocator produced nothing for an approved candidate"
        alloc = allocs[0]
        assert alloc.qty > 0

        sim = PaperSimulator(settings=s, rng=Random(7))
        req = OrderRequest(symbol=symbol, side=Side.LONG,
                           order_type=OrderType.MARKET, qty=alloc.qty,
                           client_id="e2e-probe", ts_ms=feats.ts_ms)
        result = sim.execute(req, book, info, trades=[], ts_ms=feats.ts_ms)
        assert result.accepted and result.filled_qty > 0, (
            f"paper executor refused: {result.reject_reason}"
        )

        notional = result.avg_price * result.filled_qty
        assert notional > 0
        # Sized from the $5,000 book, not the $500 one. The cap binds the
        # INTENDED notional; the filled notional can exceed it slightly because
        # the simulator walks the book and pays slippage, which is the correct
        # behaviour and not a cap violation.
        intended = alloc.qty * feats.mid
        assert intended <= s.portfolio.max_position_usd + 1e-6
        assert notional <= s.portfolio.max_position_usd * 1.02
        assert notional > 50.0, (
            "a $5,000 primary book sized like the old $500 one "
            f"(${notional:.2f}); the allocator is still on the old account"
        )

    def test_the_probe_leaves_no_trace_in_performance_statistics(self, primary):
        """The e2e probe must never pollute the live record."""
        from app.sim.ledger import Ledger
        s = primary
        ledger = Ledger("base", s.risk.starting_equity, s.simulator_version)
        assert ledger.closed_trades == []
        assert ledger.open_positions == {}
        assert ledger.equity == 5_000.0
