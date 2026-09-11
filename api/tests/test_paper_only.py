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
    # Import before switching the mode: ``app.main`` builds an app at module
    # scope, so importing it under LIVE raises during the import itself and the
    # assertion below would never be reached.
    from app.main import create_app

    monkeypatch.setenv("TRADING_MODE", LIVE)
    reload_settings()
    try:
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


# --------------------------------------------------------------------------- #
# The guard must not cry wolf on ordinary English
# --------------------------------------------------------------------------- #


def test_a_cache_signature_outside_the_trading_path_is_not_a_violation(tmp_path):
    """A guard that fires on ordinary words gets switched off by someone.

    "signature" is also a cache fingerprint, a file magic number and a
    function's parameter list. A research module using it that way took the
    whole API down once; it must not again.
    """
    research = tmp_path / "research"
    research.mkdir()
    (research / "matrix.py").write_text(
        "def cache_key(grid):\n"
        "    signature = f'{grid.interval}:{grid.size}'\n"
        "    return FeatureGrid(signature=signature)\n"
    )
    assert source_tree_is_paper_only(tmp_path) == []


def test_a_request_signature_inside_the_trading_path_is_still_caught(tmp_path):
    """Narrowing the rule must not blunt it where it matters."""
    exchange = tmp_path / "exchange"
    exchange.mkdir()
    (exchange / "signer.py").write_text(
        "def place(params, key):\n"
        "    params['signature'] = sign(params)\n"
        "    return post(url, signature=params['signature'])\n"
    )
    violations = source_tree_is_paper_only(tmp_path)
    assert violations, "a signed order in exchange/ must still be caught"
    assert any("signature" in v for v in violations)


def test_a_request_signature_next_to_the_exchange_is_still_caught(tmp_path):
    """Outside the trading path, exchange vocabulary on the line is enough."""
    (tmp_path / "helper.py").write_text(
        "URL = 'https://api.binance.us/api/v3/order'\n"
        "def go(p):\n"
        "    return requests.post(URL, params={'signature': s}, signature=s)\n"
    )
    assert source_tree_is_paper_only(tmp_path)
