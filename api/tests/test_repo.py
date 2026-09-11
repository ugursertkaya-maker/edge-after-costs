"""Persistence helpers: round trips, batching, and the expectancy maths."""

from __future__ import annotations

import dataclasses

import pytest
import sqlalchemy as sa

from app import repo
from app.db import make_engine
from app.models import (
    Base,
    BacktestRun,
    DataQualityEvent,
    EquitySnapshot,
    Market,
    MarketStat,
    PaperFill,
    PaperOrder,
    Position,
    RiskEvent,
    Signal,
    SignalFeature,
    StrategyDecision,
    StrategyPerformance,
    SystemEvent,
    Trade,
)
from app.types import (
    ClosedTrade,
    Decision,
    ExitReason,
    Fill,
    OrderResult,
    OrderType,
    PerfStats,
    Regime,
    RegimeState,
    RejectReason,
    Side,
    Signal as SignalDC,
    SymbolInfo,
)
from app.types import Position as PositionDC

T0 = 1_800_000_000_000
INFO = SymbolInfo(
    symbol="BTCUSD",
    base_asset="BTC",
    quote_asset="USD",
    status="TRADING",
    tick_size=0.01,
    step_size=0.00001,
    min_qty=0.00001,
    min_notional=1.0,
)


@pytest.fixture
def engine():
    eng = make_engine("sqlite://")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    with sa.orm.Session(engine, expire_on_commit=False) as s:
        yield s


def _count(session, model) -> int:
    return session.scalar(sa.select(sa.func.count()).select_from(model))


# --------------------------------------------------------------------------- #
# Universe
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class _UniverseEntry:
    """Stand-in for ``universe.UniverseEntry`` — only the fields repo reads."""

    symbol: str
    info: SymbolInfo
    tier: str


def test_upsert_markets_inserts_then_updates_in_place(session):
    assert repo.upsert_markets(session, [_UniverseEntry("BTCUSD", INFO, "hot")], ts_ms=T0) == 1

    demoted = dataclasses.replace(INFO, min_notional=5.0)
    assert (
        repo.upsert_markets(
            session, [_UniverseEntry("BTCUSD", demoted, "warm")], ts_ms=T0 + 60_000
        )
        == 1
    )

    rows = list(session.scalars(sa.select(Market)))
    assert len(rows) == 1
    row = rows[0]
    assert row.tier == "warm"
    assert row.min_notional == 5.0
    assert row.base_asset == "BTC" and row.quote_asset == "USD"
    assert row.first_seen_ms == T0, "first_seen_ms must survive a refresh"
    assert row.last_seen_ms == T0 + 60_000
    assert row.active is True


def test_upsert_markets_accepts_bare_symbol_info(session):
    assert repo.upsert_markets(session, [INFO], ts_ms=T0) == 1
    assert session.scalar(sa.select(Market.symbol)) == "BTCUSD"
    assert repo.upsert_markets(session, [], ts_ms=T0) == 0


# --------------------------------------------------------------------------- #
# Batched writes
# --------------------------------------------------------------------------- #


def _statement_log(engine) -> list[tuple[str, bool]]:
    seen: list[tuple[str, bool]] = []

    @sa.event.listens_for(engine, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):
        seen.append((statement, executemany))

    return seen


def test_record_market_stats_writes_1000_rows_in_one_statement(engine, session):
    rows = [
        {
            "ts_ms": T0 + i * 1_000,
            "symbol": "BTCUSD",
            "price": 60_000.0 + i,
            "spread_bps": 2.0,
            "depth_bid_usd": 9_000.0,
            "depth_ask_usd": 8_500.0,
            "imbalance": 0.03,
            "atr_bps": 14.0,
            "realized_vol_bps": 11.0,
            "volume_ratio": 1.2,
            "quote_volume_24h": 5e8,
            "trades_24h": 120_000,
            "regime": Regime.TRENDING,
        }
        for i in range(1000)
    ]
    seen = _statement_log(engine)
    assert repo.record_market_stats(session, rows) == 1000
    session.flush()

    inserts = [s for s, many in seen if "INSERT INTO market_stats" in s]
    assert len(inserts) == 1, f"expected one batched INSERT, saw {len(inserts)}"
    assert all(many for s, many in seen if "INSERT INTO market_stats" in s)

    assert _count(session, MarketStat) == 1000
    first = session.scalars(sa.select(MarketStat).order_by(MarketStat.ts_ms)).first()
    assert first.regime == "TRENDING"  # enums are stored as their value
    assert first.price == 60_000.0
    assert repo.record_market_stats(session, []) == 0


def test_record_decisions_is_batched_and_fills_defaults(engine, session):
    rows = [
        {
            "ts_ms": T0,
            "symbol": "BTCUSD",
            "strategy": "momentum_scalp",
            "decision": Decision.NO_TRADE,
            "regime": Regime.RANGING,
            "gross_edge_bps": 18.0,
            "net_edge_bps": -3.0,
            "reject_reason": RejectReason.NEGATIVE_EDGE,
        },
        {
            "ts_ms": T0,
            "symbol": "ETHUSD",
            "strategy": "breakout_scalp",
            "decision": Decision.BUY,
            "net_edge_bps": 9.0,
            "chosen": True,
        },
    ]
    seen = _statement_log(engine)
    assert repo.record_decisions(session, rows) == 2
    session.flush()

    assert len([s for s, _ in seen if "INSERT INTO strategy_decisions" in s]) == 1
    stored = list(session.scalars(sa.select(StrategyDecision).order_by(StrategyDecision.symbol)))
    btc, eth = stored
    assert btc.decision == "NO_TRADE" and btc.reject_reason == "NEGATIVE_EDGE"
    assert btc.chosen is False and btc.fee_bps == 0.0  # defaults applied
    assert eth.decision == "BUY" and eth.chosen is True
    assert eth.regime == "UNKNOWN"


# --------------------------------------------------------------------------- #
# Full round trip
# --------------------------------------------------------------------------- #


def _signal(symbol: str = "BTCUSD", strategy: str = "momentum_scalp") -> SignalDC:
    return SignalDC(
        strategy=strategy,
        symbol=symbol,
        side=Side.LONG,
        score=0.72,
        target_bps=26.0,
        stop_bps=18.0,
        ts_ms=T0,
        horizon_seconds=600,
        reasons=["ema_cross", "flow_positive"],
    )


def test_signal_to_trade_round_trip(session):
    # 1. signal + features -------------------------------------------------- #
    regime = RegimeState(regime=Regime.TRENDING, confidence=0.8, reasons=["slope"])
    ids = repo.record_signals(
        session,
        [_signal()],
        {"BTCUSD": {"regime": regime, "features": {"atr_bps": 14.0, "imbalance": 0.12}}},
    )
    assert len(ids) == 1
    sig_row = session.get(Signal, ids[0])
    assert sig_row.symbol == "BTCUSD"
    assert sig_row.side == "LONG"
    assert sig_row.score == pytest.approx(0.72)
    assert sig_row.target_bps == 26.0 and sig_row.stop_bps == 18.0
    assert sig_row.horizon_seconds == 600
    assert sig_row.regime == "TRENDING"
    assert sig_row.reasons == ["ema_cross", "flow_positive"]

    feat = session.scalar(sa.select(SignalFeature).where(SignalFeature.signal_id == ids[0]))
    assert feat.payload["regime"]["regime"] == "TRENDING"
    assert feat.payload["features"]["atr_bps"] == 14.0

    # 2. decision ----------------------------------------------------------- #
    repo.record_decisions(
        session,
        [
            {
                "ts_ms": T0,
                "symbol": "BTCUSD",
                "strategy": "momentum_scalp",
                "decision": Decision.BUY,
                "regime": Regime.TRENDING,
                "gross_edge_bps": 19.0,
                "fee_bps": 20.0,
                "spread_cost_bps": 2.0,
                "slippage_bps": 1.5,
                "net_edge_bps": -4.5,
                "rank_score": 0.61,
                "chosen": True,
                "notes": {"p_hit": 0.62},
            }
        ],
    )
    dec = session.scalar(sa.select(StrategyDecision))
    assert dec.net_edge_bps == -4.5 and dec.notes == {"p_hit": 0.62}

    # 3. entry order + fills ------------------------------------------------ #
    entry = OrderResult(
        accepted=True,
        order_type=OrderType.MARKET,
        requested_qty=0.001,
        filled_qty=0.001,
        avg_price=60_010.0,
        fees_usd=0.06,
        slippage_bps=1.7,
        fills=[
            Fill(price=60_005.0, qty=0.0006, fee_usd=0.036, is_maker=False, ts_ms=T0 + 250),
            Fill(price=60_017.5, qty=0.0004, fee_usd=0.024, is_maker=False, ts_ms=T0 + 250),
        ],
        latency_ms=250,
    )
    order_id = repo.record_order(
        session, entry, position_id="pos-1", symbol="BTCUSD", side=Side.LONG,
        leg="ENTRY", ts_ms=T0 + 250,
    )
    order = session.get(PaperOrder, order_id)
    assert order.leg == "ENTRY" and order.accepted is True
    assert order.order_type == "MARKET" and order.side == "LONG"
    assert order.filled_qty == 0.001 and order.avg_price == 60_010.0
    assert order.latency_ms == 250 and order.reject_reason is None
    fills = list(session.scalars(sa.select(PaperFill).where(PaperFill.order_id == order_id)))
    assert len(fills) == 2
    assert sum(f.qty for f in fills) == pytest.approx(0.001)
    assert sum(f.fee_usd for f in fills) == pytest.approx(order.fees_usd)

    # 4. open position ------------------------------------------------------ #
    pos = PositionDC(
        id="pos-1",
        symbol="BTCUSD",
        side=Side.LONG,
        strategy="momentum_scalp",
        qty=0.001,
        entry_price=60_010.0,
        entry_ms=T0 + 250,
        notional_usd=60.01,
        risk_usd=2.5,
        take_profit=60_170.0,
        stop_loss=59_900.0,
        entry_fees_usd=0.06,
        entry_slippage_bps=1.7,
        ledger="base",
        regime=Regime.TRENDING.value,
        expected_net_edge_bps=7.5,
        meta={"opportunity_rank": 1},
    )
    repo.open_position(session, pos)
    prow = session.scalar(sa.select(Position).where(Position.position_id == "pos-1"))
    assert prow.status == "OPEN" and prow.closed_ms is None
    assert prow.side == "LONG" and prow.regime == "TRENDING"
    assert prow.notional_usd == pytest.approx(60.01)
    assert prow.meta == {"opportunity_rank": 1}

    repo.open_position(session, pos)  # idempotent: no duplicate row
    assert _count(session, Position) == 1

    # 5. exit order --------------------------------------------------------- #
    exit_result = OrderResult(
        accepted=True,
        order_type=OrderType.MARKET,
        requested_qty=0.001,
        filled_qty=0.001,
        avg_price=60_180.0,
        fees_usd=0.06,
        slippage_bps=2.1,
        fills=[Fill(price=60_180.0, qty=0.001, fee_usd=0.06, is_maker=False, ts_ms=T0 + 90_000)],
        latency_ms=250,
    )
    repo.record_order(
        session, exit_result, position_id="pos-1", symbol="BTCUSD", side=Side.LONG,
        leg="EXIT", ts_ms=T0 + 90_000,
    )
    assert _count(session, PaperOrder) == 2

    # 6. close -------------------------------------------------------------- #
    closed = ClosedTrade(
        id="pos-1",
        symbol="BTCUSD",
        side=Side.LONG,
        strategy="momentum_scalp",
        regime=Regime.TRENDING.value,
        ledger="base",
        qty=0.001,
        entry_price=60_010.0,
        exit_price=60_180.0,
        entry_ms=T0 + 250,
        exit_ms=T0 + 90_250,
        notional_usd=60.01,
        gross_pnl_usd=0.17,
        entry_fee_usd=0.06,
        exit_fee_usd=0.06,
        slippage_usd=0.02,
        net_pnl_usd=0.05,
        exit_reason=ExitReason.TAKE_PROFIT,
        risk_usd=2.5,
        expected_net_edge_bps=7.5,
        simulator_version="sim-1.0.0",
    )
    repo.close_position(session, closed)

    trow = session.scalar(sa.select(Trade))
    assert trow.trade_id == "pos-1" and trow.exit_reason == "TAKE_PROFIT"
    assert trow.net_pnl_usd == pytest.approx(0.05)
    assert trow.holding_seconds == pytest.approx(90.0)
    assert trow.r_multiple == pytest.approx(0.05 / 2.5)
    assert trow.simulator_version == "sim-1.0.0"

    prow = session.scalar(sa.select(Position).where(Position.position_id == "pos-1"))
    assert prow.status == "CLOSED" and prow.closed_ms == T0 + 90_250


def test_record_signals_accepts_feature_dataclasses_and_no_features(session):
    regime = RegimeState(regime=Regime.BREAKOUT, confidence=0.5)
    ids = repo.record_signals(session, [_signal("ETHUSD")], {"ETHUSD": regime})
    assert session.get(Signal, ids[0]).regime == "BREAKOUT"

    ids2 = repo.record_signals(session, [_signal("SOLUSD")], {})
    assert session.get(Signal, ids2[0]).regime == "UNKNOWN"
    assert _count(session, SignalFeature) == 1  # nothing stored for the bare signal

    assert repo.record_signals(session, [], {}) == []


def test_record_order_persists_a_rejection_with_no_fills(session):
    rejected = OrderResult(
        accepted=False,
        order_type=OrderType.LIMIT,
        requested_qty=0.01,
        filled_qty=0.0,
        avg_price=0.0,
        fees_usd=0.0,
        slippage_bps=0.0,
        reject_reason=RejectReason.BELOW_MIN_NOTIONAL.value,
    )
    oid = repo.record_order(
        session, rejected, position_id=None, symbol="BTCUSD", side=Side.LONG,
        leg="ENTRY", ts_ms=T0,
    )
    row = session.get(PaperOrder, oid)
    assert row.accepted is False
    assert row.reject_reason == "BELOW_MIN_NOTIONAL"
    assert row.position_id is None
    assert _count(session, PaperFill) == 0


# --------------------------------------------------------------------------- #
# Performance + event log
# --------------------------------------------------------------------------- #


def test_record_equity_and_strategy_performance(session):
    repo.record_equity(
        session, "base", cash=440.0, equity=500.5, realized=0.5, unrealized=0.0,
        exposure=60.0, open_positions=1, ts_ms=T0, simulator_version="sim-1.0.0",
    )
    snap = session.scalar(sa.select(EquitySnapshot))
    assert snap.ledger == "base" and snap.equity == 500.5 and snap.open_positions == 1

    stats = {
        "momentum_scalp": PerfStats(
            trades=10, wins=6, losses=4, net_pnl=3.2, fees=1.1, expectancy=0.32,
            profit_factor=1.6, avg_win=1.0, avg_loss=-0.6, max_drawdown=-1.2,
            avg_holding_seconds=420.0, capital_turnover=2.5,
        ),
        "mean_reversion": PerfStats(trades=0),
    }
    assert repo.record_strategy_performance(session, "base", "rolling_24h", stats, T0) == 2
    rows = {
        r.strategy: r
        for r in session.scalars(sa.select(StrategyPerformance))
    }
    assert rows["momentum_scalp"].win_rate == pytest.approx(0.6)
    assert rows["momentum_scalp"].window == "rolling_24h"
    assert rows["mean_reversion"].trades == 0 and rows["mean_reversion"].win_rate == 0.0


def test_event_logging_helpers(session):
    repo.log_event(session, "warning", "worker", "book stale", symbol="BTCUSD", age_ms=4200)
    repo.log_risk_event(
        session, RejectReason.DAILY_LOSS_LIMIT, symbol="BTCUSD", strategy="momentum_scalp",
        ts_ms=T0, loss_pct=3.4,
    )
    repo.log_data_quality(session, "ETHUSD", "desync", ts_ms=T0, gap=12)

    ev = session.scalar(sa.select(SystemEvent))
    assert ev.level == "WARNING" and ev.detail == {"symbol": "BTCUSD", "age_ms": 4200}
    assert ev.ts_ms > 0

    risk = session.scalar(sa.select(RiskEvent))
    assert risk.kind == "DAILY_LOSS_LIMIT" and risk.ts_ms == T0
    assert risk.detail == {"loss_pct": 3.4}

    dq = session.scalar(sa.select(DataQualityEvent))
    assert dq.kind == "DESYNC" and dq.symbol == "ETHUSD" and dq.detail == {"gap": 12}


@dataclasses.dataclass
class _BacktestResult:
    """Stand-in for ``backtest.engine.BacktestResult``."""

    label: str
    start_ms: int
    end_ms: int
    trades: list
    stats: PerfStats
    per_strategy: dict
    equity_curve: list
    starting_equity: float
    ending_equity: float


def test_save_backtest_run_stores_config_and_stats(session):
    result = _BacktestResult(
        label="jan",
        start_ms=T0,
        end_ms=T0 + 86_400_000,
        trades=[],
        stats=PerfStats(trades=12, wins=7, net_pnl=4.5),
        per_strategy={"momentum_scalp": PerfStats(trades=12, wins=7)},
        equity_curve=[(T0, 500.0)],
        starting_equity=500.0,
        ending_equity=504.5,
    )
    run_id = repo.save_backtest_run(
        session, result, label="jan", kind="backtest",
        config={"seed": 7, "strategies": ("momentum_scalp",)},
    )
    row = session.get(BacktestRun, run_id)
    assert row.kind == "backtest" and row.label == "jan"
    assert row.start_ms == T0 and row.end_ms == T0 + 86_400_000
    assert row.stats["trades"] == 12 and row.stats["net_pnl"] == 4.5
    assert row.per_strategy["momentum_scalp"]["wins"] == 7
    assert row.config == {"seed": 7, "strategies": ["momentum_scalp"]}
    assert row.in_sample is None and row.out_of_sample is None


# --------------------------------------------------------------------------- #
# rolling_expectancy
# --------------------------------------------------------------------------- #


def _trade(
    session,
    *,
    tid: str,
    strategy: str,
    symbol: str,
    regime: str,
    net_pnl: float,
    notional: float,
    exit_ms: int,
    ledger: str = "base",
) -> None:
    session.add(
        Trade(
            trade_id=tid, ledger=ledger, symbol=symbol, side="LONG", strategy=strategy,
            regime=regime, qty=1.0, entry_price=100.0, exit_price=100.0,
            entry_ms=exit_ms - 60_000, exit_ms=exit_ms, notional_usd=notional,
            net_pnl_usd=net_pnl, risk_usd=1.0,
        )
    )


WINDOW = 24 * 3_600_000


def test_rolling_expectancy_matches_hand_computed_bps(session):
    # 10 trades, +1% on notional 100 five times and -0.5% five times.
    # mean(net/notional) = (5*0.01 + 5*-0.005)/10 = 0.0025 -> 25 bps
    for i in range(5):
        _trade(session, tid=f"w{i}", strategy="momentum_scalp", symbol="BTCUSD",
               regime="TRENDING", net_pnl=1.0, notional=100.0, exit_ms=T0 - i * 60_000)
    for i in range(5):
        _trade(session, tid=f"l{i}", strategy="momentum_scalp", symbol="BTCUSD",
               regime="TRENDING", net_pnl=-0.5, notional=100.0, exit_ms=T0 - i * 60_000)
    session.flush()

    out = repo.rolling_expectancy(session, "base", window_ms=WINDOW, now_ms=T0)
    assert set(out) == {("momentum_scalp", "BTCUSD", "TRENDING")}
    exp_bps, n = out[("momentum_scalp", "BTCUSD", "TRENDING")]
    assert n == 10
    assert exp_bps == pytest.approx(25.0)


def test_rolling_expectancy_normalises_by_notional_not_by_dollars(session):
    """A $1 win on $10 is worth far more than a $1 win on $1,000."""
    for i in range(10):
        _trade(session, tid=f"s{i}", strategy="a", symbol="SMALL", regime="RANGING",
               net_pnl=1.0, notional=10.0, exit_ms=T0 - i * 1_000)
        _trade(session, tid=f"b{i}", strategy="a", symbol="BIG", regime="RANGING",
               net_pnl=1.0, notional=1_000.0, exit_ms=T0 - i * 1_000)
    session.flush()

    out = repo.rolling_expectancy(session, "base", window_ms=WINDOW, now_ms=T0)
    assert out[("a", "SMALL", "RANGING")][0] == pytest.approx(1_000.0)
    assert out[("a", "BIG", "RANGING")][0] == pytest.approx(10.0)


def test_rolling_expectancy_drops_groups_below_min_samples(session):
    for i in range(9):
        _trade(session, tid=f"thin{i}", strategy="flow_impulse", symbol="ETHUSD",
               regime="BREAKOUT", net_pnl=2.0, notional=100.0, exit_ms=T0 - i * 1_000)
    for i in range(10):
        _trade(session, tid=f"thick{i}", strategy="flow_impulse", symbol="SOLUSD",
               regime="BREAKOUT", net_pnl=2.0, notional=100.0, exit_ms=T0 - i * 1_000)
    session.flush()

    out = repo.rolling_expectancy(session, "base", window_ms=WINDOW, now_ms=T0)
    assert ("flow_impulse", "ETHUSD", "BREAKOUT") not in out
    assert out[("flow_impulse", "SOLUSD", "BREAKOUT")] == (pytest.approx(200.0), 10)

    relaxed = repo.rolling_expectancy(
        session, "base", window_ms=WINDOW, min_samples=9, now_ms=T0
    )
    assert relaxed[("flow_impulse", "ETHUSD", "BREAKOUT")][1] == 9


def test_rolling_expectancy_respects_window_ledger_and_regime_split(session):
    for i in range(10):
        _trade(session, tid=f"in{i}", strategy="a", symbol="BTCUSD", regime="TRENDING",
               net_pnl=1.0, notional=100.0, exit_ms=T0 - i * 1_000)
        _trade(session, tid=f"old{i}", strategy="a", symbol="BTCUSD", regime="RANGING",
               net_pnl=5.0, notional=100.0, exit_ms=T0 - WINDOW - 60_000)
        _trade(session, tid=f"other{i}", strategy="a", symbol="BTCUSD", regime="TRENDING",
               net_pnl=9.0, notional=100.0, exit_ms=T0 - i * 1_000, ledger="rich")
    session.flush()

    out = repo.rolling_expectancy(session, "base", window_ms=WINDOW, now_ms=T0)
    assert set(out) == {("a", "BTCUSD", "TRENDING")}
    assert out[("a", "BTCUSD", "TRENDING")] == (pytest.approx(100.0), 10)

    rich = repo.rolling_expectancy(session, "rich", window_ms=WINDOW, now_ms=T0)
    assert rich[("a", "BTCUSD", "TRENDING")][0] == pytest.approx(900.0)

    unbounded = repo.rolling_expectancy(session, "base", window_ms=0, now_ms=T0)
    assert set(unbounded) == {("a", "BTCUSD", "TRENDING"), ("a", "BTCUSD", "RANGING")}


def test_rolling_expectancy_ignores_zero_notional_rows(session):
    for i in range(10):
        _trade(session, tid=f"ok{i}", strategy="a", symbol="BTCUSD", regime="TRENDING",
               net_pnl=1.0, notional=100.0, exit_ms=T0 - i * 1_000)
    for i in range(3):
        _trade(session, tid=f"bad{i}", strategy="a", symbol="BTCUSD", regime="TRENDING",
               net_pnl=1.0, notional=0.0, exit_ms=T0 - i * 1_000)
    session.flush()

    out = repo.rolling_expectancy(session, "base", window_ms=WINDOW, now_ms=T0)
    assert out[("a", "BTCUSD", "TRENDING")] == (pytest.approx(100.0), 10)


def test_rolling_expectancy_on_empty_table(session):
    assert repo.rolling_expectancy(session, "base", window_ms=WINDOW, now_ms=T0) == {}
