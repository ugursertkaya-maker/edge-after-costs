"""Dashboard users.

The bot is paper-only, but the dashboard still exposes the kill switch, so the
login is real: passwords are stored as a passlib hash and never in clear text.
"""

from __future__ import annotations

import sqlalchemy as sa
from passlib.context import CryptContext
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin

_CONTEXT: CryptContext | None = None


def _context() -> CryptContext:
    """Build the hashing context once, degrading if no bcrypt backend exists.

    ``passlib`` needs a working ``bcrypt`` wheel; on a host where that is
    missing or ABI-broken we fall back to ``pbkdf2_sha256`` (pure stdlib) so a
    deployment never dies on the login table.  Both schemes stay verifiable.
    """
    global _CONTEXT
    if _CONTEXT is None:
        try:
            ctx = CryptContext(schemes=["bcrypt", "pbkdf2_sha256"], deprecated="auto")
            ctx.hash("backend-probe")
        except Exception:  # pragma: no cover - only on a broken bcrypt build
            ctx = CryptContext(
                schemes=["bcrypt", "pbkdf2_sha256"],
                default="pbkdf2_sha256",
                deprecated="auto",
            )
        _CONTEXT = ctx
    return _CONTEXT


def hash_password(password: str) -> str:
    # bcrypt silently truncates at 72 bytes; truncate explicitly so a verify
    # can never succeed on a prefix the user did not type.
    return _context().hash(password[:72])


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return bool(_context().verify(password[:72], password_hash))
    except ValueError:
        return False


class User(IdMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(sa.String(255), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(sa.String(255), nullable=False, default="")
    role: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="viewer", index=True)
    created_ms: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)

    def set_password(self, password: str) -> None:
        self.password_hash = hash_password(password)

    def check_password(self, password: str) -> bool:
        return verify_password(password, self.password_hash)


__all__ = ["User", "hash_password", "verify_password"]
