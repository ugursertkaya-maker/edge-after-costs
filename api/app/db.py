"""Engine, session factory and schema bootstrap.

The engine is created *lazily* on first use, never at import time: the worker,
the API and the test suite all import this module, and only two of them have a
database.  ``make_engine`` exists so tests can drive the exact same models
against in-memory SQLite without touching the process-wide engine.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings, get_settings
from app.models import Base, User, hash_password  # noqa: F401  (registers metadata)
from app.types import now_ms

log = logging.getLogger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def make_engine(url: str, **kwargs: Any) -> Engine:
    """Build an engine for ``url`` with backend-appropriate pooling.

    SQLite gets a ``StaticPool`` with ``check_same_thread=False`` so that an
    in-memory database survives across sessions and threads inside one test;
    the connection-pool knobs below are meaningless (and rejected) there.
    """
    if url.startswith("sqlite"):
        opts: dict[str, Any] = {
            "future": True,
            "connect_args": {"check_same_thread": False},
        }
        if ":memory:" in url or url in ("sqlite://", "sqlite+pysqlite://"):
            opts["poolclass"] = StaticPool
        opts.update(kwargs)
        return sa.create_engine(url, **opts)

    opts = {
        "future": True,
        "pool_pre_ping": True,   # a recycled Postgres connection must not fail a write
        "pool_size": 5,
        "max_overflow": 5,
        "pool_recycle": 1_800,
    }
    opts.update(kwargs)
    return sa.create_engine(url, **opts)


def get_engine(settings: Settings | None = None) -> Engine:
    """Process-wide lazy engine."""
    global _engine
    if _engine is None:
        s = settings or get_settings()
        _engine = make_engine(s.database_url)
    return _engine


def get_sessionmaker(settings: Settings | None = None) -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(settings), expire_on_commit=False, future=True
        )
    return _session_factory


def set_engine(engine: Engine | None) -> None:
    """Swap the process-wide engine (tests, or a re-point after config change)."""
    global _engine, _session_factory
    _engine = engine
    _session_factory = (
        sessionmaker(bind=engine, expire_on_commit=False, future=True) if engine else None
    )


def __getattr__(name: str) -> Any:
    """Expose ``db.engine`` / ``db.SessionLocal`` without creating them at import."""
    if name == "engine":
        return get_engine()
    if name == "SessionLocal":
        return get_sessionmaker()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@contextmanager
def get_session(factory: sessionmaker[Session] | None = None) -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on any exception."""
    session = (factory or get_sessionmaker())()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(engine: Engine | None = None, settings: Settings | None = None) -> Engine:
    """Create every table, then seed the bootstrap admin if one is configured.

    Idempotent: ``create_all`` skips existing tables and the admin is only
    inserted when that email is absent, so it is safe on every worker boot.
    """
    s = settings or get_settings()
    eng = engine or get_engine(s)
    Base.metadata.create_all(eng)
    _seed_admin(eng, s)
    return eng


def _seed_admin(engine: Engine, settings: Settings) -> None:
    email = (settings.bootstrap_admin_email or "").strip().lower()
    password = settings.bootstrap_admin_password
    if not email or not password:
        return
    with Session(engine, expire_on_commit=False) as session:
        existing = session.scalar(sa.select(User).where(User.email == email))
        if existing is not None:
            return
        session.add(
            User(
                email=email,
                password_hash=hash_password(password),
                role="admin",
                created_ms=now_ms(),
            )
        )
        session.commit()
        log.info("seeded bootstrap admin %s", email)


def healthcheck(engine: Engine | None = None) -> bool:
    """``True`` when a trivial query round-trips.  Never raises."""
    try:
        eng = engine or get_engine()
        with eng.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
        return True
    except Exception as exc:  # pragma: no cover - exercised via a broken URL
        log.warning("database healthcheck failed: %s", exc)
        return False


__all__ = [
    "Base",
    "make_engine",
    "get_engine",
    "get_sessionmaker",
    "set_engine",
    "get_session",
    "init_db",
    "healthcheck",
]
