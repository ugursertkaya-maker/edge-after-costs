"""The proof that V1 cannot place a real order.

This is the test the whole safety case rests on, so it attacks the claim from
four directions rather than asserting a flag.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import LIVE, PAPER, Settings, reload_settings
from app.safety import LiveTradingDisabled, assert_paper_mode, source_tree_is_paper_only

APP_ROOT = Path(__file__).resolve().parent.parent / "app"


def test_mode_is_paper_by_default():
    assert reload_settings().mode == PAPER


def test_assert_paper_mode_rejects_live():
    live = Settings(mode=LIVE)
    with pytest.raises(LiveTradingDisabled):
        assert_paper_mode(live)


def test_source_tree_has_no_live_order_path():
    violations = source_tree_is_paper_only(APP_ROOT)
    assert violations == [], "live-trading code found:\n" + "\n".join(violations)


def test_no_module_imports_an_authenticated_client():
    """A signed request needs a secret and an HMAC. Neither exists in the tree."""
    banned = ("X-MBX-APIKEY", "api_secret", "apiSecret", "/api/v3/order", "/sapi/")
    offenders: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        if path.name == "safety.py":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for needle in banned:
            if needle in text:
                offenders.append(f"{path.relative_to(APP_ROOT)}: {needle}")
    assert offenders == [], "\n".join(offenders)


def test_worker_refuses_to_construct_in_live_mode(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", LIVE)
    reload_settings()
    try:
        from app.worker.main import TradingWorker

        with pytest.raises(LiveTradingDisabled):
            TradingWorker()
    finally:
        monkeypatch.setenv("TRADING_MODE", PAPER)
        reload_settings()


def test_api_refuses_to_start_in_live_mode(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", LIVE)
    reload_settings()
    try:
        from app.main import create_app

        with pytest.raises(LiveTradingDisabled):
            create_app()
    finally:
        monkeypatch.setenv("TRADING_MODE", PAPER)
        reload_settings()


def test_guard_actually_catches_a_planted_violation(tmp_path):
    planted = tmp_path / "evil.py"
    planted.write_text(
        'HEADERS = {"X-MBX-APIKEY": key}\n'
        'URL = "/api/v3/order"\n'
    )
    violations = source_tree_is_paper_only(tmp_path)
    assert violations, "the guard failed to notice an obvious live-order path"
