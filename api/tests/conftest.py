"""Shared test setup.

Two jobs:

1.  Make ``app`` and ``fixtures`` importable however pytest is invoked — pytest
    8+ no longer reliably prepends the test directory to ``sys.path``, so
    ``from fixtures.market import ...`` needs this to work from any cwd.
2.  Pin the environment before ``app.config`` is imported anywhere, so no test
    can accidentally pick up production settings. Everything runs offline
    against SQLite with a deliberately dead Redis URL.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_API_DIR = _TESTS_DIR.parent

for _p in (_API_DIR, _TESTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("ENV", "test")
os.environ.setdefault("TRADING_MODE", "PAPER")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/15")  # intentionally dead
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("BOOTSTRAP_ADMIN_EMAIL", "admin@test.local")
os.environ.setdefault("BOOTSTRAP_ADMIN_PASSWORD", "test-password")

import pytest  # noqa: E402


@pytest.fixture
def settings():
    from app.config import reload_settings

    return reload_settings()


@pytest.fixture
def store():
    """A RuntimeStore with no Redis behind it — falls back to its dict."""
    from app.runtime import RuntimeStore

    s = RuntimeStore(client=None)
    s._client = None
    return s
