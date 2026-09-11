"""Paper-only enforcement.

This module exists so that "the bot cannot place a real order" is a property we
can *prove*, not a promise we make in a README.  It does two things:

1. ``assert_paper_mode`` — a runtime gate.  Every process (API, worker,
   backtester) calls it before it does anything else, so a mis-set
   ``TRADING_MODE`` stops the process instead of quietly changing behaviour.
2. ``source_tree_is_paper_only`` — a *static* gate.  It scans the shipped
   source for the machinery a live order would need (an API-key header, a
   request-signing helper, an authenticated endpoint path, a stored secret).
   None of that exists in V1, so the scan returns an empty list and a test
   asserts exactly that.  If somebody later adds signing code, the test fails
   before that code can ever run.

The scan is deliberately literal.  It makes exactly two concessions, both
narrow and both documented in code:

* **Comments and bare string statements (docstrings) are blanked first.**
  Inert prose cannot place an order — a module docstring promising "there is no
  ``X-MBX-APIKEY`` header here" is a promise, not a violation — whereas a
  string literal used in an *expression* (``headers={"X-MBX-APIKEY": key}``) is
  exactly how a live request gets built, so those stay in scope.  A file that
  will not parse is scanned raw, i.e. it fails closed.
* **HMAC is judged in context.**  Hashing is not by itself an order path: the
  dashboard signs its own session cookie with ``hmac`` over ``secret_key`` and
  never touches an exchange.  ``hmac`` is therefore a violation when it appears
  in the trading path (``exchange/``, ``sim/``, ``worker/``, ``backtest/``,
  ``strategies/``) or in any file that also mentions the exchange
  (``binance``, ``/api/v3``, ``recvWindow``, an API-key name...).  That is the
  only shape in which HMAC signs a *request*.  Every other pattern — the key
  header, the authenticated endpoints, a stored ``api_secret``, a
  ``signature=`` parameter — is unconditional and file-independent, and a live
  order cannot be built without at least one of them.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.config import Settings

PAPER_ONLY_BANNER = (
    "PAPER-ONLY BUILD — simulated fills only. No API keys, no request signing, "
    "no authenticated exchange endpoints. Public market data in, paper trades out."
)

__all__ = [
    "LiveTradingDisabled",
    "PAPER_ONLY_BANNER",
    "assert_paper_mode",
    "source_tree_is_paper_only",
]


class LiveTradingDisabled(RuntimeError):
    """Raised whenever something tries to run outside PAPER mode."""


def assert_paper_mode(settings: "Settings | None" = None) -> None:
    """Raise :class:`LiveTradingDisabled` unless the mode is exactly ``PAPER``.

    Anything else — a typo, an empty string, ``None``, ``"LIVE"`` — is unsafe.
    There is no "warn and continue" path.
    """
    if settings is None:
        from app.config import get_settings

        settings = get_settings()
    mode = getattr(settings, "mode", None)
    if mode != "PAPER":
        raise LiveTradingDisabled(
            f"V1 is paper-only: refusing to run with TRADING_MODE={mode!r}. "
            "There is no order-submission path in this codebase."
        )


# --------------------------------------------------------------------------- #
# Static scan
# --------------------------------------------------------------------------- #

# (pattern, human-readable reason).  Applied line by line so every violation
# can be reported with a line number.
_FORBIDDEN: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"X-MBX-APIKEY", re.IGNORECASE), "Binance API-key header"),
    (re.compile(r"/api/v3/order"), "authenticated order endpoint"),
    (re.compile(r"/api/v3/openOrders"), "authenticated open-orders endpoint"),
    (re.compile(r"/sapi/"), "authenticated SAPI endpoint"),
    (re.compile(r"\bapi_secret\b", re.IGNORECASE), "stored exchange API secret"),
    (re.compile(r"\bapiSecret\b"), "stored exchange API secret"),
)

# ``signature`` is split into two rules, because the word does two jobs.
#
# As a **query parameter** — ``?signature=`` or ``&signature=``, or the string
# key ``"signature"`` being assigned into a params dict — it is unambiguously a
# signed HTTP request and is a violation anywhere, unconditionally.
#
# As a **bare assignment** it is ordinary English: a cache fingerprint, a file
# magic number, a function's parameter list.  Flagging that everywhere produced
# a false positive on a research cache key and took the whole API down with it,
# and a guard that cries wolf on ordinary words is one somebody eventually
# switches off.  So the bare form is judged in context, exactly as ``hmac`` is.
_SIGNATURE_PARAM = re.compile(
    r"[?&]signature\s*=|[\"\']signature[\"\']\s*[:=]", re.IGNORECASE
)
_SIGNATURE_ASSIGN = re.compile(r"(?<![\w.])signature\s*=")

# ``secret_key=`` on its own is not damning (the web app has a JWT cookie key);
# it is a violation only when it shares a line with signing vocabulary.
_SECRET_KEY_ASSIGN = re.compile(r"(?<![\w.])secret_key\s*=")

# HMAC, judged in context (see the module docstring).
_HMAC = re.compile(r"\bhmac\b", re.IGNORECASE)
_EXCHANGE_CONTEXT = re.compile(
    r"binance|/api/v3|/sapi|recvWindow|X-MBX|api_key|apikey|api_secret|apiSecret",
    re.IGNORECASE,
)
# Directories that make up the order/execution path.  Anything that signs
# anything in here is signing a request, whatever it claims to be doing.
_TRADING_PATH_DIRS = frozenset({"exchange", "sim", "worker", "backtest", "strategies"})
_SIGNING_CONTEXT = re.compile(
    r"sign|digest|signature|hmac|sha256|totp|apikey|api_key", re.IGNORECASE
)

_THIS_FILE = Path(__file__).resolve()


def _should_skip(path: Path) -> bool:
    """Skip this module (it necessarily contains the literals) and all tests."""
    resolved = path.resolve()
    if resolved == _THIS_FILE:
        return True
    parts = resolved.parts
    return "tests" in parts or "__pycache__" in parts


def _in_trading_path(path: Path, root_resolved: Path) -> bool:
    """True when *path* sits in a package that can reach an exchange or a fill."""
    try:
        rel = path.resolve().relative_to(root_resolved)
    except ValueError:
        return False
    return bool(_TRADING_PATH_DIRS.intersection(rel.parts[:-1]))


def _blank(line: str, start_col: int, end_col: int) -> str:
    """Replace a span with spaces, preserving length so offsets stay valid."""
    end_col = min(end_col, len(line))
    if start_col >= end_col:
        return line
    return line[:start_col] + " " * (end_col - start_col) + line[end_col:]


def _mask_inert(text: str) -> list[str]:
    """Return the file's lines with comments and bare string statements blanked.

    Falls back to the untouched lines when the file will not tokenize or parse:
    unparseable source is scanned raw rather than trusted.
    """
    lines = text.splitlines()
    masked = list(lines)

    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                row, col = tok.start
                masked[row - 1] = _blank(masked[row - 1], col, len(masked[row - 1]))
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        return lines

    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return lines

    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr):
            continue
        value = node.value
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        first, last = node.lineno, (node.end_lineno or node.lineno)
        for row in range(first, last + 1):
            if row - 1 >= len(masked):
                break
            start = node.col_offset if row == first else 0
            end = (
                node.end_col_offset
                if row == last and node.end_col_offset is not None
                else len(masked[row - 1])
            )
            masked[row - 1] = _blank(masked[row - 1], start, end)
    return masked


def source_tree_is_paper_only(root: Path) -> list[str]:
    """Scan every ``.py`` under *root*; return ``"path:line: reason"`` strings.

    An empty list is the proof obligation: no key header, no signing helper, no
    authenticated endpoint and no stored exchange secret anywhere in the
    shipped tree.  The safety module itself and everything under ``tests/`` are
    skipped — they have to name the forbidden literals in order to police them.
    """
    root = Path(root)
    violations: list[str] = []
    root_resolved = root.resolve()
    for path in sorted(root.rglob("*.py")):
        if _should_skip(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:  # an unreadable source file is itself suspicious
            violations.append(f"{path}:0: unreadable ({exc})")
            continue
        try:
            shown = str(path.resolve().relative_to(root_resolved))
        except ValueError:
            shown = str(path)
        masked = _mask_inert(text)
        contextual_matters = _in_trading_path(path, root_resolved) or bool(
            _EXCHANGE_CONTEXT.search("\n".join(masked))
        )
        for lineno, line in enumerate(masked, start=1):
            if _SIGNATURE_PARAM.search(line):
                violations.append(
                    f"{shown}:{lineno}: request signature parameter"
                )
            elif contextual_matters and _SIGNATURE_ASSIGN.search(line):
                violations.append(
                    f"{shown}:{lineno}: request signature parameter "
                    "in the exchange/trading path"
                )
            if contextual_matters and _HMAC.search(line):
                violations.append(
                    f"{shown}:{lineno}: HMAC request-signing machinery "
                    "in the exchange/trading path"
                )
            for pattern, reason in _FORBIDDEN:
                if pattern.search(line):
                    violations.append(f"{shown}:{lineno}: {reason}")
            if _SECRET_KEY_ASSIGN.search(line) and _SIGNING_CONTEXT.search(line):
                violations.append(
                    f"{shown}:{lineno}: secret key used in a signing context"
                )
    return violations
