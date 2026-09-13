"""HTTP surface: routing prefix, auth gating, and the dashboard contract."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import reload_settings

PREFIX = "/netedge/api"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "PAPER")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    reload_settings()
    from app.main import create_app

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    reload_settings()


def test_health_is_public_and_declares_paper_mode(client):
    r = client.get(f"{PREFIX}/health")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "PAPER"
    assert body["live_orders_enabled"] is False
    assert body["exchange"] == "binanceus"


def test_dashboard_requires_auth(client):
    assert client.get(f"{PREFIX}/dashboard").status_code == 401


def test_login_rejects_bad_credentials(client):
    r = client.post(
        f"{PREFIX}/auth/login", json={"email": "admin@test.local", "password": "wrong"}
    )
    assert r.status_code == 401


def test_login_then_dashboard(client):
    r = client.post(
        f"{PREFIX}/auth/login",
        json={"email": "admin@test.local", "password": "test-password"},
    )
    assert r.status_code == 200, r.text

    r = client.get(f"{PREFIX}/dashboard")
    assert r.status_code == 200
    body = r.json()
    for key in (
        "mode", "live_orders_enabled", "header", "metrics", "scanner",
        "positions", "trades", "strategies", "equity_curve", "health", "ledgers",
    ):
        assert key in body, f"missing dashboard key: {key}"
    assert body["live_orders_enabled"] is False
    assert body["metrics"]["equity"] == pytest.approx(500.0)


def test_empty_state_is_explicitly_stale_not_fake(client):
    client.post(
        f"{PREFIX}/auth/login",
        json={"email": "admin@test.local", "password": "test-password"},
    )
    body = client.get(f"{PREFIX}/dashboard").json()
    assert body["stale"] is True
    assert body["scanner"] == []
    assert body["trades"] == []
    assert body["metrics"]["trades"] == 0


def test_safety_endpoint_reports_a_clean_tree(client):
    client.post(
        f"{PREFIX}/auth/login",
        json={"email": "admin@test.local", "password": "test-password"},
    )
    body = client.get(f"{PREFIX}/system/safety").json()
    assert body["clean"] is True
    assert body["source_scan_violations"] == []
    assert body["exchange_credentials_configured"] is False
    assert body["withdrawal_permission"] is False


def test_unknown_ledger_falls_back_without_crashing(client):
    client.post(
        f"{PREFIX}/auth/login",
        json={"email": "admin@test.local", "password": "test-password"},
    )
    r = client.get(f"{PREFIX}/dashboard", params={"ledger": "nonsense"})
    assert r.status_code == 200
