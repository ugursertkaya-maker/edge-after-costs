"""Schema-level guarantees.

These tests exist because production is Postgres but every other test in the
suite runs on in-memory SQLite: if a model ever grows a Postgres-only column
type the whole suite would have to go back to needing a database container.
"""

from __future__ import annotations

import dataclasses

import pytest
import sqlalchemy as sa

from app.config import get_settings
from app.db import get_session, healthcheck, init_db, make_engine
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
    User,
    hash_password,
    verify_password,
)

EXPECTED_TABLES = {
    "markets",
    "market_stats",
    "signals",
    "signal_features",
    "strategy_decisions",
    "paper_orders",
    "paper_fills",
    "positions",
    "trades",
    "equity_snapshots",
    "strategy_performance",
    "risk_events",
    "system_events",
    "data_quality_events",
    "backtest_runs",
    "users",
}

# Everything the models are allowed to use.  Anything else is either
# Postgres-only or silently lossy on SQLite.
ALLOWED_TYPES = (sa.Integer, sa.BigInteger, sa.Float, sa.String, sa.Text, sa.Boolean, sa.JSON)


@pytest.fixture
def engine():
    eng = make_engine("sqlite://")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


def test_every_table_is_declared_and_created(engine):
    assert set(Base.metadata.tables) == EXPECTED_TABLES
    assert set(sa.inspect(engine).get_table_names()) == EXPECTED_TABLES


def test_every_column_type_is_sqlite_and_postgres_portable():
    offenders = []
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if not isinstance(col.type, ALLOWED_TYPES):
                offenders.append(f"{table.name}.{col.name}: {col.type!r}")
    assert offenders == []


def test_every_table_has_an_integer_primary_key():
    for table in Base.metadata.tables.values():
        pk = list(table.primary_key.columns)
        assert len(pk) == 1, table.name
        assert pk[0].name == "id", table.name


# ``*_ms`` names that are durations, not epoch timestamps, and so fit in an INT.
DURATION_COLUMNS = {("paper_orders", "latency_ms")}


def test_ts_ms_columns_are_bigint_and_indexed():
    """Every ms timestamp is queried by range; none may be a 32-bit INT.

    Epoch milliseconds already exceed 2^31, so an ``Integer`` column would
    overflow on Postgres and silently truncate nothing on SQLite — the worst
    kind of portability bug.
    """
    indexed = {
        (idx.table.name, col.name)
        for table in Base.metadata.tables.values()
        for idx in table.indexes
        for col in idx.columns
    }
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if not col.name.endswith("_ms"):
                continue
            if (table.name, col.name) in DURATION_COLUMNS:
                continue
            assert isinstance(col.type, sa.BigInteger), f"{table.name}.{col.name}"
        if "ts_ms" in table.columns:
            assert (table.name, "ts_ms") in indexed, table.name


def test_unique_business_keys_are_enforced(engine):
    cases = [
        (Market, dict(symbol="BTCUSD")),
        (Position, dict(position_id="p-1", symbol="BTCUSD")),
        (Trade, dict(trade_id="t-1", symbol="BTCUSD")),
        (User, dict(email="a@b.c", password_hash="x")),
    ]
    for model, kwargs in cases:
        with sa.orm.Session(engine) as session:
            session.add(model(**kwargs))
            session.commit()
        with sa.orm.Session(engine) as session:
            session.add(model(**kwargs))
            with pytest.raises(sa.exc.IntegrityError):
                session.commit()


def test_json_columns_round_trip_dicts_and_lists(engine):
    with sa.orm.Session(engine) as session:
        session.add(Signal(ts_ms=1, symbol="BTCUSD", strategy="s", reasons=["a", "b"]))
        session.flush()
        session.add(SignalFeature(signal_id=1, payload={"atr_bps": 12.5, "n": 3}))
        session.add(BacktestRun(ts_ms=1, label="x", symbols=["BTCUSD", "ETHUSD"]))
        session.commit()

    with sa.orm.Session(engine) as session:
        assert session.scalar(sa.select(Signal)).reasons == ["a", "b"]
        assert session.scalar(sa.select(SignalFeature)).payload == {"atr_bps": 12.5, "n": 3}
        assert session.scalar(sa.select(BacktestRun)).symbols == ["BTCUSD", "ETHUSD"]


def test_as_dict_exposes_plain_columns(engine):
    row = MarketStat(ts_ms=5, symbol="BTCUSD", price=100.0, spread_bps=3.0)
    d = row.as_dict()
    assert d["symbol"] == "BTCUSD" and d["price"] == 100.0
    assert "id" in d


def test_password_hashing_round_trip():
    h = hash_password("s3cret-paper-trading")
    assert h != "s3cret-paper-trading"
    assert verify_password("s3cret-paper-trading", h)
    assert not verify_password("wrong", h)
    assert not verify_password("anything", "")


def test_user_helpers_hash_on_set():
    u = User(email="x@y.z")
    u.set_password("hunter2")
    assert u.password_hash and u.password_hash != "hunter2"
    assert u.check_password("hunter2")
    assert not u.check_password("hunter3")


def test_init_db_is_idempotent_and_seeds_the_bootstrap_admin():
    settings = dataclasses.replace(
        get_settings(),
        bootstrap_admin_email="Admin@Example.COM",
        bootstrap_admin_password="paper-only",
    )
    engine = make_engine("sqlite://")
    init_db(engine, settings)
    init_db(engine, settings)  # second boot must not duplicate the admin

    with sa.orm.Session(engine) as session:
        users = list(session.scalars(sa.select(User)))
    assert len(users) == 1
    assert users[0].email == "admin@example.com"
    assert users[0].role == "admin"
    assert users[0].check_password("paper-only")
    assert users[0].created_ms > 0
    engine.dispose()


def test_init_db_without_bootstrap_credentials_seeds_nothing():
    settings = dataclasses.replace(
        get_settings(), bootstrap_admin_email="", bootstrap_admin_password=""
    )
    engine = make_engine("sqlite://")
    init_db(engine, settings)
    with sa.orm.Session(engine) as session:
        assert session.scalar(sa.select(sa.func.count(User.id))) == 0
    engine.dispose()


def test_healthcheck_true_on_live_engine_false_on_dead_one(engine):
    assert healthcheck(engine) is True
    dead = sa.create_engine("sqlite:////nonexistent-dir/does-not-exist.db")
    assert healthcheck(dead) is False


def test_get_session_commits_and_rolls_back(engine):
    factory = sa.orm.sessionmaker(bind=engine, expire_on_commit=False)

    with get_session(factory) as session:
        session.add(SystemEvent(ts_ms=1, level="INFO", source="test", message="ok"))

    with sa.orm.Session(engine) as session:
        assert session.scalar(sa.select(sa.func.count(SystemEvent.id))) == 1

    with pytest.raises(RuntimeError):
        with get_session(factory) as session:
            session.add(SystemEvent(ts_ms=2, level="INFO", source="test", message="boom"))
            session.flush()
            raise RuntimeError("boom")

    with sa.orm.Session(engine) as session:
        assert session.scalar(sa.select(sa.func.count(SystemEvent.id))) == 1


def test_every_model_class_is_reexported():
    for model in (
        Market, MarketStat, Signal, SignalFeature, StrategyDecision, PaperOrder,
        PaperFill, Position, Trade, EquitySnapshot, StrategyPerformance, RiskEvent,
        SystemEvent, DataQualityEvent, BacktestRun, User,
    ):
        assert issubclass(model, Base)
