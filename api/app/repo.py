"""Persistence helpers the worker calls.

Thin and synchronous on purpose.  The worker loop runs on one core with a hard
memory cap, so writes are *batched* (one executemany per snapshot) and reads are
aggregated in SQL rather than pulled into Python.  Every function takes an open
``Session`` and never commits — the caller owns the transaction boundary via
``app.db.get_session``, which keeps one evaluation cycle atomic.

Nothing here imports a strategy, a simulator or the exchange client: this layer
only translates the dataclasses in ``app.types`` into rows and back.
"""

from __future__ import annotations

import dataclasses
import enum
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app import types as t
from app.models import (
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

# --------------------------------------------------------------------------- #
# Value coercion
# --------------------------------------------------------------------------- #


def _val(value: Any, default: Any = "") -> Any:
    """Enum -> its value, ``None`` -> ``default``, everything else unchanged."""
    if value is None:
        return default
    return value.value if isinstance(value, enum.Enum) else value


def _jsonable(value: Any) -> Any:
    """Recursively convert dataclasses/enums into JSON-storable primitives."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, enum.Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    return str(value)


def _regime_name(obj: Any, default: str = t.Regime.UNKNOWN.value) -> str:
    """Pull a regime name out of a RegimeState, Regime, dict or plain string."""
    if obj is None:
        return default
    if isinstance(obj, t.Regime):
        return obj.value
    if isinstance(obj, str):
        return obj
    inner = getattr(obj, "regime", None)
    if inner is None and isinstance(obj, Mapping):
        inner = obj.get("regime")
    if inner is None:
        return default
    return _regime_name(inner, default)


def _rows_for(model: type, rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalise dicts to one uniform key set so the insert stays executemany.

    A ragged batch forces SQLAlchemy to split the INSERT per distinct key set;
    filling every column from the model's own defaults keeps it to one round
    trip regardless of what the caller left out.
    """
    columns = {c.key: c for c in sa.inspect(model).columns if c.key != "id"}
    out: list[dict[str, Any]] = []
    for row in rows:
        rec: dict[str, Any] = {}
        for key, col in columns.items():
            if key in row:
                rec[key] = _val(row[key], None)
            else:
                default = getattr(col.default, "arg", None) if col.default is not None else None
                rec[key] = default if not callable(default) else None
        out.append(rec)
    return out


def _bulk_insert(session: Session, model: type, rows: Sequence[Mapping[str, Any]]) -> int:
    if not rows:
        return 0
    payload = _rows_for(model, rows)
    # Core-level insert against the Table, not the ORM entity: the ORM insert
    # path re-groups rows by which nullable columns happen to be NULL, which
    # silently turns one executemany into N single-row statements.
    session.execute(sa.insert(model.__table__), payload)
    return len(payload)


# --------------------------------------------------------------------------- #
# Universe / market data
# --------------------------------------------------------------------------- #


def upsert_markets(session: Session, entries: Iterable[Any], *, ts_ms: int | None = None) -> int:
    """Insert or refresh one row per symbol.  Returns the number touched.

    Accepts ``UniverseEntry`` objects (which carry ``.info`` and ``.tier``) or
    bare ``SymbolInfo``.  ``first_seen_ms`` is preserved across refreshes so the
    dashboard can show how long a pair has been in the universe.
    """
    items = list(entries)
    if not items:
        return 0
    now = t.now_ms() if ts_ms is None else ts_ms

    parsed: list[tuple[str, Any, str]] = []
    for entry in items:
        info = getattr(entry, "info", entry)
        symbol = getattr(entry, "symbol", None) or getattr(info, "symbol", None)
        if not symbol:
            continue
        parsed.append((symbol, info, str(getattr(entry, "tier", "warm"))))

    existing = {
        m.symbol: m
        for m in session.scalars(
            sa.select(Market).where(Market.symbol.in_([p[0] for p in parsed]))
        )
    }

    for symbol, info, tier in parsed:
        row = existing.get(symbol)
        if row is None:
            row = Market(symbol=symbol, first_seen_ms=now)
            session.add(row)
            existing[symbol] = row
        row.base_asset = getattr(info, "base_asset", "") or ""
        row.quote_asset = getattr(info, "quote_asset", "") or ""
        row.status = getattr(info, "status", "TRADING") or "TRADING"
        row.tick_size = float(getattr(info, "tick_size", 0.0) or 0.0)
        row.step_size = float(getattr(info, "step_size", 0.0) or 0.0)
        row.min_qty = float(getattr(info, "min_qty", 0.0) or 0.0)
        row.min_notional = float(getattr(info, "min_notional", 0.0) or 0.0)
        row.tier = tier
        row.active = True
        row.last_seen_ms = now

    session.flush()
    return len(parsed)


def record_market_stats(session: Session, rows: list[dict]) -> int:
    """Bulk-insert aggregated microstructure rows.  One statement, one flush."""
    return _bulk_insert(session, MarketStat, rows)


# --------------------------------------------------------------------------- #
# Signals / decisions
# --------------------------------------------------------------------------- #


def record_signals(
    session: Session,
    signals: Iterable[t.Signal],
    features_by_symbol: Mapping[str, Any] | None = None,
) -> list[int]:
    """Persist signals plus their feature payloads.  Returns the new signal ids.

    ``features_by_symbol`` may hold ``Features``, ``RegimeState``, a dict, or a
    ``(features, regime)`` pair — whatever the caller has to hand; the regime
    name is extracted if present and the whole thing is stored as JSON.
    """
    sigs = list(signals)
    if not sigs:
        return []
    features_by_symbol = features_by_symbol or {}

    rows: list[Signal] = []
    payloads: list[Any] = []
    for sig in sigs:
        extra = features_by_symbol.get(sig.symbol)
        rows.append(
            Signal(
                ts_ms=int(sig.ts_ms),
                symbol=sig.symbol,
                strategy=sig.strategy,
                side=_val(sig.side, t.Side.LONG.value),
                score=float(sig.score),
                target_bps=float(sig.target_bps),
                stop_bps=float(sig.stop_bps),
                horizon_seconds=int(sig.horizon_seconds),
                regime=_regime_name(extra),
                reasons=_jsonable(list(sig.reasons)),
            )
        )
        payloads.append(extra)

    session.add_all(rows)
    session.flush()  # assigns ids so the feature rows can point at them

    feature_rows = [
        {"signal_id": row.id, "payload": _jsonable(payload)}
        for row, payload in zip(rows, payloads, strict=True)
        if payload is not None
    ]
    _bulk_insert(session, SignalFeature, feature_rows)
    return [int(r.id) for r in rows]


def record_decisions(session: Session, rows: list[dict]) -> int:
    """Bulk-insert ranked verdicts, including every rejection."""
    return _bulk_insert(session, StrategyDecision, rows)


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


def record_order(
    session: Session,
    result: t.OrderResult,
    *,
    position_id: str | None,
    symbol: str,
    side: t.Side | str,
    leg: str,
    ts_ms: int,
) -> int:
    """Persist one simulated order and its per-level fills.  Returns order id."""
    order = PaperOrder(
        ts_ms=int(ts_ms),
        position_id=position_id,
        symbol=symbol,
        side=_val(side, t.Side.LONG.value),
        order_type=_val(result.order_type, t.OrderType.MARKET.value),
        requested_qty=float(result.requested_qty),
        filled_qty=float(result.filled_qty),
        avg_price=float(result.avg_price),
        fees_usd=float(result.fees_usd),
        slippage_bps=float(result.slippage_bps),
        latency_ms=int(result.latency_ms),
        accepted=bool(result.accepted),
        reject_reason=_val(result.reject_reason, None),
        leg=leg,
    )
    session.add(order)
    session.flush()

    fills = [
        {
            "order_id": order.id,
            "price": float(f.price),
            "qty": float(f.qty),
            "fee_usd": float(f.fee_usd),
            "is_maker": bool(f.is_maker),
            "ts_ms": int(f.ts_ms or ts_ms),
        }
        for f in result.fills
    ]
    _bulk_insert(session, PaperFill, fills)
    return int(order.id)


def open_position(session: Session, pos: t.Position) -> None:
    """Write (or refresh) the OPEN row for an in-memory position."""
    row = session.scalar(sa.select(Position).where(Position.position_id == pos.id))
    if row is None:
        row = Position(position_id=pos.id)
        session.add(row)
    row.ledger = pos.ledger
    row.symbol = pos.symbol
    row.side = _val(pos.side, t.Side.LONG.value)
    row.strategy = pos.strategy
    row.regime = _regime_name(pos.regime)
    row.qty = float(pos.qty)
    row.entry_price = float(pos.entry_price)
    row.entry_ms = int(pos.entry_ms)
    row.notional_usd = float(pos.notional_usd)
    row.risk_usd = float(pos.risk_usd)
    row.take_profit = float(pos.take_profit)
    row.stop_loss = float(pos.stop_loss)
    row.entry_fees_usd = float(pos.entry_fees_usd)
    row.entry_slippage_bps = float(pos.entry_slippage_bps)
    row.expected_net_edge_bps = float(pos.expected_net_edge_bps)
    row.status = "OPEN"
    row.closed_ms = None
    row.meta = _jsonable(pos.meta)
    session.flush()


def close_position(session: Session, trade: t.ClosedTrade) -> None:
    """Append the closed round trip and flip its position row to CLOSED.

    The position row is matched on ``ClosedTrade.id`` because the ledger carries
    the position id through into the trade id; if the position was never
    persisted (a backtest replay, say) the trade is still recorded.
    """
    session.add(
        Trade(
            trade_id=trade.id,
            ledger=trade.ledger,
            symbol=trade.symbol,
            side=_val(trade.side, t.Side.LONG.value),
            strategy=trade.strategy,
            regime=_regime_name(trade.regime),
            qty=float(trade.qty),
            entry_price=float(trade.entry_price),
            exit_price=float(trade.exit_price),
            entry_ms=int(trade.entry_ms),
            exit_ms=int(trade.exit_ms),
            notional_usd=float(trade.notional_usd),
            gross_pnl_usd=float(trade.gross_pnl_usd),
            entry_fee_usd=float(trade.entry_fee_usd),
            exit_fee_usd=float(trade.exit_fee_usd),
            slippage_usd=float(trade.slippage_usd),
            net_pnl_usd=float(trade.net_pnl_usd),
            exit_reason=_val(trade.exit_reason, ""),
            risk_usd=float(trade.risk_usd),
            expected_net_edge_bps=float(trade.expected_net_edge_bps),
            holding_seconds=float(trade.holding_seconds),
            r_multiple=float(trade.r_multiple),
            simulator_version=trade.simulator_version,
        )
    )
    session.execute(
        sa.update(Position)
        .where(Position.position_id == trade.id)
        .values(status="CLOSED", closed_ms=int(trade.exit_ms))
        .execution_options(synchronize_session="evaluate")
    )
    session.flush()


# --------------------------------------------------------------------------- #
# Performance
# --------------------------------------------------------------------------- #


def record_equity(
    session: Session,
    ledger_name: str,
    *,
    cash: float,
    equity: float,
    realized: float,
    unrealized: float,
    exposure: float,
    open_positions: int,
    ts_ms: int,
    simulator_version: str,
) -> None:
    session.add(
        EquitySnapshot(
            ts_ms=int(ts_ms),
            ledger=ledger_name,
            cash=float(cash),
            equity=float(equity),
            realized_pnl=float(realized),
            unrealized_pnl=float(unrealized),
            exposure_usd=float(exposure),
            open_positions=int(open_positions),
            simulator_version=simulator_version,
        )
    )
    session.flush()


def record_strategy_performance(
    session: Session,
    ledger_name: str,
    window: str,
    stats_by_strategy: Mapping[str, t.PerfStats],
    ts_ms: int,
) -> int:
    """Append one performance row per strategy for ``window``."""
    rows = []
    for strategy, st in stats_by_strategy.items():
        rows.append(
            {
                "ts_ms": int(ts_ms),
                "ledger": ledger_name,
                "strategy": strategy,
                "window": window,
                "trades": int(st.trades),
                "wins": int(st.wins),
                "win_rate": float(st.win_rate),
                "avg_win": float(st.avg_win),
                "avg_loss": float(st.avg_loss),
                "profit_factor": float(st.profit_factor),
                "expectancy": float(st.expectancy),
                "fees": float(st.fees),
                "net_pnl": float(st.net_pnl),
                "max_drawdown": float(st.max_drawdown),
                "avg_holding_seconds": float(st.avg_holding_seconds),
                "capital_turnover": float(st.capital_turnover),
            }
        )
    return _bulk_insert(session, StrategyPerformance, rows)


# --------------------------------------------------------------------------- #
# Event log
# --------------------------------------------------------------------------- #


def log_event(session: Session, level: str, source: str, message: str, **detail: Any) -> None:
    ts_ms = detail.pop("ts_ms", None)
    session.add(
        SystemEvent(
            ts_ms=int(ts_ms if ts_ms is not None else t.now_ms()),
            level=str(level).upper(),
            source=source,
            message=message,
            detail=_jsonable(detail) or None,
        )
    )
    session.flush()


def log_risk_event(
    session: Session,
    kind: str,
    *,
    symbol: str | None = None,
    strategy: str | None = None,
    **detail: Any,
) -> None:
    ts_ms = detail.pop("ts_ms", None)
    session.add(
        RiskEvent(
            ts_ms=int(ts_ms if ts_ms is not None else t.now_ms()),
            kind=str(_val(kind)),
            symbol=symbol,
            strategy=strategy,
            detail=_jsonable(detail) or None,
        )
    )
    session.flush()


def log_data_quality(session: Session, symbol: str | None, kind: str, **detail: Any) -> None:
    ts_ms = detail.pop("ts_ms", None)
    session.add(
        DataQualityEvent(
            ts_ms=int(ts_ms if ts_ms is not None else t.now_ms()),
            symbol=symbol,
            kind=str(_val(kind)).upper(),
            detail=_jsonable(detail) or None,
        )
    )
    session.flush()


# --------------------------------------------------------------------------- #
# Backtests
# --------------------------------------------------------------------------- #


def save_backtest_run(
    session: Session, result: Any, *, label: str, kind: str, config: Any
) -> int:
    """Store a ``BacktestResult`` or a ``WalkForwardResult``.  Returns the id.

    Walk-forward results carry ``in_sample``/``out_of_sample`` stats and a list
    of windows instead of a single trade list, so both shapes are read
    defensively rather than by isinstance — the backtest module owns those
    dataclasses, not this one.
    """
    trades = list(getattr(result, "trades", []) or [])
    windows = list(getattr(result, "windows", []) or [])
    symbols = sorted({getattr(tr, "symbol", "") for tr in trades if getattr(tr, "symbol", "")})

    start_ms = int(getattr(result, "start_ms", 0) or 0)
    end_ms = int(getattr(result, "end_ms", 0) or 0)
    if windows and not (start_ms or end_ms):
        firsts = [w[0] for w in windows if w]
        lasts = [w[-1] for w in windows if w]
        start_ms = min((int(getattr(w, "start_ms", 0) or 0) for w in firsts), default=0)
        end_ms = max((int(getattr(w, "end_ms", 0) or 0) for w in lasts), default=0)

    row = BacktestRun(
        ts_ms=t.now_ms(),
        label=label,
        kind=kind,
        symbols=_jsonable(symbols),
        start_ms=start_ms,
        end_ms=end_ms,
        config=_jsonable(config),
        stats=_jsonable(getattr(result, "stats", None)),
        per_strategy=_jsonable(getattr(result, "per_strategy", None)),
        in_sample=_jsonable(getattr(result, "in_sample", None)),
        out_of_sample=_jsonable(getattr(result, "out_of_sample", None)),
    )
    session.add(row)
    session.flush()
    return int(row.id)


# --------------------------------------------------------------------------- #
# Adaptive layer input (§22)
# --------------------------------------------------------------------------- #


def rolling_expectancy(
    session: Session,
    ledger: str,
    *,
    window_ms: int,
    min_samples: int = 10,
    now_ms: int | None = None,
) -> dict[tuple[str, str, str], tuple[float, int]]:
    """Realised expectancy per (strategy, symbol, regime) over a trailing window.

    Expectancy is returned in **bps of notional** —
    ``mean(net_pnl_usd / notional_usd) * 10_000`` — so a $500 ledger and a
    $5,000 one are directly comparable, which is the whole point of feeding it
    to ``ranker.ExpectancyProvider``.

    Groups thinner than ``min_samples`` are omitted rather than returned with a
    low count: a two-trade sample is noise, and letting it steer sizing is how a
    scalper turns a good week into a bad month.  Aggregation happens in SQL so
    the worker never loads the trade table into memory.

    ``now_ms`` defaults to wall clock; tests (and replays) pass it explicitly.
    ``window_ms <= 0`` means "all history".
    """
    ref = t.now_ms() if now_ms is None else now_ms
    conditions = [Trade.ledger == ledger, Trade.notional_usd > 0]
    if window_ms > 0:
        conditions.append(Trade.exit_ms >= ref - window_ms)

    stmt = (
        sa.select(
            Trade.strategy,
            Trade.symbol,
            Trade.regime,
            sa.func.avg(Trade.net_pnl_usd / Trade.notional_usd) * 10_000.0,
            sa.func.count(Trade.id),
        )
        .where(sa.and_(*conditions))
        .group_by(Trade.strategy, Trade.symbol, Trade.regime)
        .having(sa.func.count(Trade.id) >= min_samples)
    )

    return {
        (strategy, symbol, regime): (float(expectancy_bps or 0.0), int(n))
        for strategy, symbol, regime, expectancy_bps, n in session.execute(stmt)
    }


__all__ = [
    "upsert_markets",
    "record_market_stats",
    "record_signals",
    "record_decisions",
    "record_order",
    "open_position",
    "close_position",
    "record_equity",
    "record_strategy_performance",
    "log_event",
    "log_risk_event",
    "log_data_quality",
    "save_backtest_run",
    "rolling_expectancy",
]
