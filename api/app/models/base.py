"""Declarative base and the column conventions every table follows.

Production runs on Postgres, but every test runs on in-memory SQLite, so the
column vocabulary here is deliberately narrow: ``Integer``/``BigInteger`` for
ids and epoch-millisecond timestamps, ``Float`` for prices, sizes and money,
``String`` for enum-ish values (we store ``Enum.value``, never a DB enum type)
and ``sa.JSON`` for free-form payloads.  Nothing Postgres-only ever appears,
and indexes are declared explicitly so both backends get the same plan.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint/index names so migrations and drops are stable.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}

# Free-form payload columns.  ``sa.JSON`` maps to JSON on Postgres and to a
# TEXT round-trip on SQLite, which keeps the test suite backend-free.
JSONMap = sa.JSON().with_variant(sa.JSON(), "sqlite")


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {
        dict[str, Any]: sa.JSON,
        list[Any]: sa.JSON,
    }

    def as_dict(self) -> dict[str, Any]:
        """Plain-Python view of the row — used by the API layer and tests."""
        return {c.key: getattr(self, c.key) for c in sa.inspect(self).mapper.column_attrs}

    def __repr__(self) -> str:  # pragma: no cover - debugging sugar
        cls = type(self).__name__
        return f"<{cls} id={getattr(self, 'id', None)!r}>"


class IdMixin:
    """Surrogate integer primary key.  Business keys get their own unique index."""

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)


class TsMixin:
    """Epoch-millisecond event time.

    Indexed on every table that has it because every dashboard query, retention
    sweep and rolling-window aggregate filters or orders on it.
    """

    ts_ms: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, index=True)


__all__ = ["Base", "IdMixin", "TsMixin", "JSONMap", "NAMING_CONVENTION"]
