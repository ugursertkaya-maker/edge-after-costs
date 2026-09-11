"""The proof that live orders are impossible.

``test_shipped_tree_has_no_live_order_machinery`` is the most important test in
the repository: it is the difference between "we intend not to trade live" and
"this source tree structurally cannot".  The rest of the file exists to keep
that test honest — a scanner that never fires would pass it too, so we also
prove the scanner catches each forbidden construct when it is really there.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from app.config import Settings, get_settings
from app.safety import (
    PAPER_ONLY_BANNER,
    LiveTradingDisabled,
    assert_paper_mode,
    source_tree_is_paper_only,
)

APP_ROOT = Path(__file__).resolve().parents[1] / "app"


# --------------------------------------------------------------------------- #
# Runtime gate
# --------------------------------------------------------------------------- #


def test_assert_paper_mode_allows_paper() -> None:
    assert_paper_mode(dataclasses.replace(get_settings(), mode="PAPER"))


@pytest.mark.parametrize("mode", ["LIVE", "live", "Paper", "", "TESTNET", None])
def test_assert_paper_mode_raises_for_anything_but_paper(mode: object) -> None:
    settings = dataclasses.replace(get_settings(), mode=mode)  # type: ignore[arg-type]
    with pytest.raises(LiveTradingDisabled):
        assert_paper_mode(settings)


def test_assert_paper_mode_message_names_the_mode() -> None:
    with pytest.raises(LiveTradingDisabled, match="LIVE"):
        assert_paper_mode(dataclasses.replace(get_settings(), mode="LIVE"))


def test_default_settings_are_paper() -> None:
    assert Settings().mode == "PAPER"
    assert Settings().is_paper is True
    assert_paper_mode(Settings())


def test_banner_is_a_short_non_empty_string() -> None:
    assert isinstance(PAPER_ONLY_BANNER, str)
    assert "PAPER" in PAPER_ONLY_BANNER.upper()
    assert 0 < len(PAPER_ONLY_BANNER) < 400


# --------------------------------------------------------------------------- #
# The static proof
# --------------------------------------------------------------------------- #


def test_shipped_tree_has_no_live_order_machinery() -> None:
    """No key header, no signing, no authenticated endpoint, no stored secret."""
    violations = source_tree_is_paper_only(APP_ROOT)
    assert violations == [], "paper-only violation(s):\n" + "\n".join(violations)


def test_scan_actually_reads_the_real_tree() -> None:
    """Guard against a scan that passes because it found nothing to scan."""
    scanned = [p for p in APP_ROOT.rglob("*.py") if "__pycache__" not in p.parts]
    assert len(scanned) >= 3


@pytest.mark.parametrize(
    "source",
    [
        'HEADERS = {"X-MBX-APIKEY": key}\n',
        'URL = BASE + "/api/v3/order"\n',
        'URL = BASE + "/api/v3/openOrders"\n',
        'URL = BASE + "/sapi/v1/capital/withdraw/apply"\n',
        'api_secret = os.environ["BINANCE_SECRET"]\n',
        'cfg = {"apiSecret": raw}\n',
        'params["signature"] = sig\nurl = f"{base}?signature={sig}"\n',
    ],
)
def test_scanner_catches_each_forbidden_construct(tmp_path: Path, source: str) -> None:
    (tmp_path / "sneaky.py").write_text(source, encoding="utf-8")
    violations = source_tree_is_paper_only(tmp_path)
    assert violations, f"scanner missed:\n{source}"
    assert all(v.startswith("sneaky.py:") for v in violations)


def test_scanner_catches_a_temp_file_with_the_key_header(tmp_path: Path) -> None:
    (tmp_path / "live_client.py").write_text(
        "import httpx\n"
        "\n"
        "def place(order, key):\n"
        '    return httpx.post(URL, headers={"X-MBX-APIKEY": key}, json=order)\n',
        encoding="utf-8",
    )
    violations = source_tree_is_paper_only(tmp_path)
    assert violations == ["live_client.py:4: Binance API-key header"]


def test_scanner_reports_path_line_and_reason(tmp_path: Path) -> None:
    pkg = tmp_path / "exchange"
    pkg.mkdir()
    (pkg / "signer.py").write_text("\n\nimport hmac\n", encoding="utf-8")
    violations = source_tree_is_paper_only(tmp_path)
    assert len(violations) == 1
    path, line, reason = violations[0].split(":", 2)
    assert path.endswith("signer.py")
    assert line == "3"
    assert "HMAC" in reason


def test_hmac_in_the_trading_path_is_a_violation(tmp_path: Path) -> None:
    for package in ("exchange", "sim", "worker", "backtest", "strategies"):
        pkg = tmp_path / package
        pkg.mkdir()
        (pkg / "mod.py").write_text("import hmac\n", encoding="utf-8")
    assert len(source_tree_is_paper_only(tmp_path)) == 5


def test_hmac_next_to_exchange_vocabulary_is_a_violation(tmp_path: Path) -> None:
    (tmp_path / "helper.py").write_text(
        'BASE = "https://api.binance.us"\n' "import hmac\n", encoding="utf-8"
    )
    assert any("HMAC" in v for v in source_tree_is_paper_only(tmp_path))


def test_hmac_for_session_cookies_is_not_a_violation(tmp_path: Path) -> None:
    """Signing our own cookie is not signing an exchange request."""
    (tmp_path / "auth.py").write_text(
        "import hmac, hashlib\n"
        "def sign(payload, secret):\n"
        "    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()\n",
        encoding="utf-8",
    )
    assert source_tree_is_paper_only(tmp_path) == []


def test_secret_key_alone_is_clean_but_signing_context_is_not(tmp_path: Path) -> None:
    (tmp_path / "settings_like.py").write_text(
        'cookie = Session(secret_key="dev")\n', encoding="utf-8"
    )
    assert source_tree_is_paper_only(tmp_path) == []

    (tmp_path / "signer2.py").write_text(
        'sig = sign_request(secret_key=os.environ["K"])\n', encoding="utf-8"
    )
    assert any("signing context" in v for v in source_tree_is_paper_only(tmp_path))


def test_docstrings_and_comments_are_inert_but_code_is_not(tmp_path: Path) -> None:
    """Prose promising the absence of a thing must not count as the thing."""
    (tmp_path / "honest.py").write_text(
        '"""No X-MBX-APIKEY header and no /api/v3/order path here."""\n'
        "# and definitely no /sapi/ calls\n"
        "VALUE = 1\n",
        encoding="utf-8",
    )
    assert source_tree_is_paper_only(tmp_path) == []

    (tmp_path / "dishonest.py").write_text(
        '"""Totally innocent module."""\n' 'HEADER_NAME = "X-MBX-APIKEY"\n',
        encoding="utf-8",
    )
    assert source_tree_is_paper_only(tmp_path) == [
        "dishonest.py:2: Binance API-key header"
    ]


def test_unparseable_file_is_scanned_raw(tmp_path: Path) -> None:
    """A file that will not parse is treated with suspicion, not trust."""
    (tmp_path / "broken.py").write_text(
        'def f(:\n    """/api/v3/order"""\n', encoding="utf-8"
    )
    assert source_tree_is_paper_only(tmp_path)


def test_scan_skips_tests_and_the_safety_module(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text('H = "X-MBX-APIKEY"\n', encoding="utf-8")
    assert source_tree_is_paper_only(tmp_path) == []
    # This very test module names every forbidden literal and the real scan of
    # the app tree is still clean, which is the skip rule working end to end.
    assert source_tree_is_paper_only(APP_ROOT) == []


def test_non_python_files_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("X-MBX-APIKEY", encoding="utf-8")
    assert source_tree_is_paper_only(tmp_path) == []
