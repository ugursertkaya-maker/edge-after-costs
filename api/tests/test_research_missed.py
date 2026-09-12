"""Missed-opportunity audit tests.

The audit uses future data by construction, so two kinds of test live here and
the second matters more than the first:

* **Arithmetic.**  A known winner is labelled MISSED_WINNER, a known loser
  CORRECT_REJECTION, the stop is probed before the target when one bar contains
  both, and ``missed_net_bps`` is checked against a figure worked out by hand.
* **The leakage boundary.**  ``test_no_live_module_imports_the_missed_audit``
  walks the **transitive** import closure of ``app/worker/``, ``app/risk.py``
  and ``app/predict/engine.py`` with ``ast`` and fails if this module appears
  anywhere in it.  It is written on the assumption that someone will later try
  to use a MISSED_WINNER label as a feature — because it is the obvious thing
  to try, and it is the one thing that would make every number downstream a
  lie.

Everything is offline, deterministic and runs on hand-built bars.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.research.missed import (
    DEFAULT_IDLE_THRESHOLD_PCT,
    FORBIDDEN_IMPORTERS,
    LeakageError,
    MissedAudit,
    Outcome,
    RejectedCandidate,
    assert_research_context,
    audit_rejections,
    format_audit,
    normalise_rejection,
)
from app.types import Bar

MINUTE = 60_000
T0 = 1_700_000_000_000
API_DIR = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Hand-built bars
# --------------------------------------------------------------------------- #


def bar(i: int, *, high: float, low: float, close: float, symbol: str = "BTCUSD") -> Bar:
    open_ms = T0 + i * MINUTE
    return Bar(
        symbol=symbol,
        open_ms=open_ms,
        close_ms=open_ms + MINUTE - 1,
        open=close,
        high=high,
        low=low,
        close=close,
        volume=10.0,
        quote_volume=1_000.0,
        trades=50,
        taker_buy_quote=550.0,
    )


def flat_bars(n: int, price: float = 100.0, symbol: str = "BTCUSD") -> list[Bar]:
    return [bar(i, high=price, low=price, close=price, symbol=symbol) for i in range(n)]


def candidate(
    *,
    ts_ms: int = T0,
    reason: str = "EDGE_BELOW_MIN",
    price: float = 100.0,
    target_bps: float = 50.0,
    stop_bps: float = 50.0,
    idle_pct: float = 90.0,
    side: str = "LONG",
    symbol: str = "BTCUSD",
) -> RejectedCandidate:
    return RejectedCandidate(
        symbol=symbol,
        ts_ms=ts_ms,
        reason=reason,
        price=price,
        target_bps=target_bps,
        stop_bps=stop_bps,
        side=side,
        idle_pct=idle_pct,
    )


# --------------------------------------------------------------------------- #
# 1. Classification
# --------------------------------------------------------------------------- #


def test_a_known_winner_is_labelled_missed_winner():
    # Rejected at 100.0 aiming for +50 bps gross.  Round trip 4 bps, so the NET
    # target sits at 100 * (1 + 54/10000) = 100.54.  Bar 1 reaches 100.60.
    bars = [bar(0, high=100.0, low=100.0, close=100.0),
            bar(1, high=100.60, low=99.95, close=100.55),
            bar(2, high=100.70, low=100.4, close=100.6)]
    audit = audit_rejections(
        {"BTCUSD": bars}, [candidate()], horizon_minutes=60, round_trip_bps=4.0
    )
    assert audit.missed_winners == 1
    assert audit.correct_rejections == 0
    assert audit.by_reason == {"EDGE_BELOW_MIN": (1, 0)}
    assert audit.labelled[0].outcome is Outcome.MISSED_WINNER
    # A winner nets the strategy's own gross target: the round trip is already
    # paid for inside the price it had to reach.
    assert audit.missed_net_bps == pytest.approx(50.0)


def test_a_known_loser_is_labelled_correct_rejection():
    bars = [bar(0, high=100.0, low=100.0, close=100.0),
            bar(1, high=100.05, low=99.40, close=99.45)]   # -50 bps stop hit
    audit = audit_rejections(
        {"BTCUSD": bars}, [candidate()], horizon_minutes=60, round_trip_bps=4.0
    )
    assert audit.correct_rejections == 1
    assert audit.missed_winners == 0
    # Stopped out: the strategy's stop plus the round trip it would have paid.
    assert audit.missed_net_bps == pytest.approx(-54.0)


def test_the_stop_is_probed_before_the_target_within_one_bar():
    """One bar contains both.  The engine assumes the stop first; so does this,
    or a MISSED_WINNER would mean something weaker than a backtest win."""
    bars = [bar(0, high=100.0, low=100.0, close=100.0),
            bar(1, high=101.0, low=99.0, close=100.0)]
    audit = audit_rejections(
        {"BTCUSD": bars}, [candidate()], horizon_minutes=60, round_trip_bps=4.0
    )
    assert audit.missed_winners == 0
    assert audit.correct_rejections == 1


def test_a_setup_that_resolves_neither_way_is_marked_to_the_last_close():
    # Drifts to 100.20 (+20 bps) without reaching the 54 bps net target or the
    # 50 bps stop.  Net of a 4 bps round trip that is +16 bps — a move that did
    # not pay, so the refusal stands.
    bars = [bar(0, high=100.0, low=100.0, close=100.0),
            bar(1, high=100.20, low=99.95, close=100.20)]
    audit = audit_rejections(
        {"BTCUSD": bars}, [candidate()], horizon_minutes=60, round_trip_bps=4.0
    )
    assert audit.correct_rejections == 1
    assert audit.missed_net_bps == pytest.approx(16.0)


def test_the_horizon_bounds_how_far_forward_the_audit_may_look():
    bars = flat_bars(3) + [bar(3, high=101.0, low=100.0, close=101.0)]
    near = audit_rejections(
        {"BTCUSD": bars}, [candidate()], horizon_minutes=2, round_trip_bps=4.0
    )
    far = audit_rejections(
        {"BTCUSD": bars}, [candidate()], horizon_minutes=10, round_trip_bps=4.0
    )
    assert near.missed_winners == 0
    assert far.missed_winners == 1


def test_the_decision_bar_itself_is_never_used_to_judge_the_decision():
    """Bar 0 already contains the target.  Using it would be lookahead of the
    crudest kind: the decision was made on that bar's close."""
    bars = [bar(0, high=110.0, low=100.0, close=100.0), bar(1, high=100.0,
                                                            low=100.0, close=100.0)]
    audit = audit_rejections(
        {"BTCUSD": bars}, [candidate(ts_ms=T0)], horizon_minutes=60, round_trip_bps=4.0
    )
    assert audit.missed_winners == 0


def test_a_short_rejection_is_judged_in_the_right_direction():
    bars = [bar(0, high=100.0, low=100.0, close=100.0),
            bar(1, high=100.05, low=99.40, close=99.45)]
    audit = audit_rejections(
        {"BTCUSD": bars},
        [candidate(side="SHORT")],
        horizon_minutes=60,
        round_trip_bps=4.0,
    )
    assert audit.missed_winners == 1


# --------------------------------------------------------------------------- #
# 2. missed_net_bps, by hand
# --------------------------------------------------------------------------- #


def test_missed_net_bps_arithmetic_by_hand():
    """Three refusals, one of each kind, added up on paper.

    round trip = 4 bps, target = 50 bps gross, stop = 50 bps gross.

      winner  -> +50 bps   (reached 100 * (1 + 54/10000))
      stopped -> -54 bps   (-50 stop, and the 4 bps round trip paid anyway)
      timeout -> +16 bps   (closed +20 bps, less the 4 bps round trip)
      ------------------------------------------------------------------
      total      +12 bps   mean +4.0 bps
    """
    winner = flat_bars(1) + [bar(1, high=100.60, low=99.95, close=100.55)]
    stopped = flat_bars(1, symbol="ETHUSD") + [
        bar(1, high=100.05, low=99.40, close=99.45, symbol="ETHUSD")
    ]
    timeout = flat_bars(1, symbol="SOLUSD") + [
        bar(1, high=100.20, low=99.95, close=100.20, symbol="SOLUSD")
    ]
    audit = audit_rejections(
        {"BTCUSD": winner, "ETHUSD": stopped, "SOLUSD": timeout},
        [
            candidate(symbol="BTCUSD", reason="EDGE_BELOW_MIN"),
            candidate(symbol="ETHUSD", reason="NEGATIVE_EDGE"),
            candidate(symbol="SOLUSD", reason="NEGATIVE_EDGE"),
        ],
        horizon_minutes=60,
        round_trip_bps=4.0,
    )
    assert audit.rejections_examined == 3
    assert audit.missed_winners == 1
    assert audit.correct_rejections == 2
    assert audit.missed_net_bps == pytest.approx(50.0 - 54.0 + 16.0)
    assert audit.missed_net_bps == pytest.approx(12.0)
    assert audit.avg_net_bps == pytest.approx(4.0)
    assert audit.winner_net_bps == pytest.approx(50.0)
    assert audit.rejection_net_bps == pytest.approx(-38.0)
    assert audit.by_reason == {"EDGE_BELOW_MIN": (1, 0), "NEGATIVE_EDGE": (0, 2)}


def test_a_negative_missed_net_bps_reads_as_the_gate_being_correct():
    stopped = flat_bars(1) + [bar(1, high=100.05, low=99.40, close=99.45)]
    audit = audit_rejections(
        {"BTCUSD": stopped}, [candidate()], horizon_minutes=60, round_trip_bps=4.0
    )
    assert audit.missed_net_bps < 0
    assert audit.verdict.startswith("GATE CORRECT")


def test_a_strongly_positive_missed_net_bps_reads_as_the_gate_being_the_problem():
    winner = flat_bars(1) + [bar(1, high=100.60, low=99.95, close=100.55)]
    audit = audit_rejections(
        {"BTCUSD": winner}, [candidate()], horizon_minutes=60, round_trip_bps=4.0
    )
    assert audit.missed_net_bps > 0
    assert audit.verdict.startswith("GATE SUSPECT")


# --------------------------------------------------------------------------- #
# 3. Windows, and the split by reason
# --------------------------------------------------------------------------- #


def test_only_high_idle_periods_are_audited():
    bars = flat_bars(1) + [bar(1, high=100.60, low=99.95, close=100.55)]
    audit = audit_rejections(
        {"BTCUSD": bars},
        [candidate(idle_pct=10.0)],   # book was busy; the refusal says nothing
        horizon_minutes=60,
        round_trip_bps=4.0,
    )
    assert audit.windows == 0
    assert audit.rejections_examined == 0
    assert "high-idle window" in " ".join(audit.notes)


def test_the_idle_threshold_is_forty_percent_and_is_exclusive():
    bars = flat_bars(1) + [bar(1, high=100.60, low=99.95, close=100.55)]
    assert DEFAULT_IDLE_THRESHOLD_PCT == 40.0
    at = audit_rejections({"BTCUSD": bars}, [candidate(idle_pct=40.0)],
                          horizon_minutes=60, round_trip_bps=4.0)
    over = audit_rejections({"BTCUSD": bars}, [candidate(idle_pct=40.1)],
                            horizon_minutes=60, round_trip_bps=4.0)
    assert at.rejections_examined == 0
    assert over.rejections_examined == 1


def test_windows_are_counted_in_buckets_not_per_rejection():
    bars = flat_bars(400)
    rejections = [candidate(ts_ms=T0 + i * MINUTE) for i in range(10)]
    rejections += [candidate(ts_ms=T0 + (200 + i) * MINUTE) for i in range(10)]
    audit = audit_rejections(
        {"BTCUSD": bars}, rejections, horizon_minutes=60, round_trip_bps=4.0,
        window_minutes=60,
    )
    # Two clusters, one hour apart by more than one bucket.
    assert audit.windows == 2
    assert audit.rejections_examined == 20


def test_the_split_by_reason_is_what_the_audit_is_for():
    winner = flat_bars(1) + [bar(1, high=100.60, low=99.95, close=100.55)]
    loser = flat_bars(1, symbol="ETHUSD") + [
        bar(1, high=100.05, low=99.40, close=99.45, symbol="ETHUSD")
    ]
    audit = audit_rejections(
        {"BTCUSD": winner, "ETHUSD": loser},
        [
            candidate(symbol="BTCUSD", reason="CORRELATED_EXPOSURE"),
            candidate(symbol="BTCUSD", reason="CORRELATED_EXPOSURE"),
            candidate(symbol="ETHUSD", reason="NEGATIVE_EDGE"),
        ],
        horizon_minutes=60,
        round_trip_bps=4.0,
    )
    assert audit.by_reason["CORRELATED_EXPOSURE"] == (2, 0)
    assert audit.by_reason["NEGATIVE_EDGE"] == (0, 1)
    text = format_audit(audit)
    assert "CORRELATED_EXPOSURE" in text and "NEGATIVE_EDGE" in text


# --------------------------------------------------------------------------- #
# 4. Degenerate input
# --------------------------------------------------------------------------- #


def test_an_empty_rejection_list_produces_an_empty_audit_not_a_crash():
    audit = audit_rejections(
        {"BTCUSD": flat_bars(10)}, [], horizon_minutes=60, round_trip_bps=4.0
    )
    assert isinstance(audit, MissedAudit)
    assert (audit.windows, audit.rejections_examined) == (0, 0)
    assert audit.missed_winners == 0 and audit.correct_rejections == 0
    assert audit.missed_net_bps == 0.0
    assert audit.by_reason == {}
    assert "nothing to audit" in " ".join(audit.notes)
    assert "NO EVIDENCE" in audit.verdict
    assert "MISSED-OPPORTUNITY AUDIT" in format_audit(audit)


def test_no_bars_at_all_produces_an_empty_audit():
    audit = audit_rejections({}, [candidate()], horizon_minutes=60, round_trip_bps=4.0)
    assert audit.rejections_examined == 1
    assert audit.undecidable == 1
    assert audit.missed_winners == 0 and audit.correct_rejections == 0


def test_an_unjudgeable_rejection_is_never_counted_as_correct():
    """The failure mode this forbids: an audit that cannot see the future
    quietly reporting every refusal as vindicated."""
    audit = audit_rejections(
        {"BTCUSD": flat_bars(1)},   # only the decision bar, nothing after
        [candidate()],
        horizon_minutes=60,
        round_trip_bps=4.0,
    )
    assert audit.undecidable == 1
    assert audit.correct_rejections == 0
    assert audit.labelled[0].outcome is Outcome.UNDECIDABLE


def test_rows_without_a_usable_geometry_are_dropped_not_invented():
    rows = [
        {"symbol": "BTCUSD", "ts_ms": T0, "reason": "X", "price": 0.0,
         "target_bps": 50.0, "stop_bps": 50.0},
        {"symbol": "BTCUSD", "ts_ms": T0, "reason": "X", "price": 100.0,
         "target_bps": 50.0, "stop_bps": 0.0},
    ]
    audit = audit_rejections(
        {"BTCUSD": flat_bars(5)}, rows, horizon_minutes=60, round_trip_bps=4.0
    )
    assert audit.rejections_supplied == 2
    assert audit.rejections_examined == 0
    assert "invented stop" in " ".join(audit.notes)


def test_a_zero_horizon_has_no_forward_window():
    audit = audit_rejections(
        {"BTCUSD": flat_bars(10)}, [candidate()], horizon_minutes=0, round_trip_bps=4.0
    )
    assert audit.rejections_examined == 0
    assert "no forward window" in " ".join(audit.notes)


def test_normalise_accepts_mappings_objects_and_the_dataclass():
    row = {"symbol": "BTCUSD", "ts_ms": T0, "reject_reason": "RISK_BUDGET",
           "price": 100.0, "target_bps": 30.0, "stop_bps": 20.0, "idle_pct": 88.0}
    got = normalise_rejection(row)
    assert got is not None and got.reason == "RISK_BUDGET" and got.idle_pct == 88.0
    assert normalise_rejection(candidate()) == candidate()
    assert normalise_rejection({"symbol": ""}) is None


def test_the_audit_is_json_safe():
    d = audit_rejections({}, [], horizon_minutes=60, round_trip_bps=4.0).as_dict()
    import json

    assert json.loads(json.dumps(d))["missed_net_bps"] == 0.0


# --------------------------------------------------------------------------- #
# 5. The leakage boundary
# --------------------------------------------------------------------------- #

#: The live decision path.  Nothing here may reach the audit, at any depth.
LIVE_ROOTS = ("app/worker", "app/risk.py", "app/predict/engine.py")


def _module_name(path: Path) -> str:
    rel = path.relative_to(API_DIR).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports_of(path: Path) -> set[str]:
    """Every module this file imports, module-scope or lazily inside a function."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    pkg = _module_name(path).rsplit(".", 1)[0]
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative
                base = pkg
                for _ in range(node.level - 1):
                    base = base.rsplit(".", 1)[0]
                mod = f"{base}.{node.module}" if node.module else base
            else:
                mod = node.module or ""
            if mod:
                out.add(mod)
                out.update(f"{mod}.{a.name}" for a in node.names)
    return out


def _path_for(module: str) -> Path | None:
    if not module.startswith("app"):
        return None
    base = API_DIR / Path(*module.split("."))
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.exists():
            return cand
    return None


def _closure(roots) -> set[str]:
    """Transitive ``app.*`` import closure of the given files."""
    seen: set[str] = set()
    stack: list[Path] = []
    for root in roots:
        p = API_DIR / root
        if p.is_dir():
            stack.extend(sorted(p.rglob("*.py")))
        elif p.exists():
            stack.append(p)
    while stack:
        path = stack.pop()
        name = _module_name(path)
        if name in seen:
            continue
        seen.add(name)
        for mod in _imports_of(path):
            if mod in seen:
                continue
            child = _path_for(mod)
            if child is not None:
                stack.append(child)
            else:
                seen.add(mod)
    return seen


def test_no_live_module_imports_the_missed_audit():
    """Written on the assumption that someone will try to use this as a feature.

    A MISSED_WINNER label is computed from bars *after* the decision.  Feeding
    one back into the entry path would produce a bot that appears to predict the
    future in backtest and does nothing of the kind live.  This test is the
    guarantee; the runtime guard in the module is only the tripwire.
    """
    closure = _closure(LIVE_ROOTS)
    assert "app.research.missed" not in closure, (
        "app.research.missed reached the live decision path via: "
        + ", ".join(sorted(m for m in closure if "missed" in m))
    )
    assert not any("research.missed" in m for m in closure)
    # Sanity: the closure is real, not an empty set that passes vacuously.
    assert "app.risk" in closure and "app.types" in closure
    assert len(closure) > 10


def test_the_audit_does_not_import_the_decision_stack():
    """The other direction: the audit must not be able to *call* a live gate.

    Re-deriving a rejection would mean the audit decided which candidates to
    judge, and a post-hoc labeller that picks its own sample is not an audit.
    """
    imports = _imports_of(API_DIR / "app/research/missed.py")
    for banned in ("app.risk", "app.ranker", "app.predict", "app.strategies",
                   "app.worker", "app.portfolio"):
        assert not any(m == banned or m.startswith(banned + ".") for m in imports), (
            f"app.research.missed imports {banned}"
        )


@pytest.mark.parametrize("module", FORBIDDEN_IMPORTERS)
def test_the_runtime_guard_refuses_a_live_importer(module):
    class FakeFrame:
        def __init__(self, name):
            self.frame = type("F", (), {"f_globals": {"__name__": name}})()

    with pytest.raises(LeakageError) as exc:
        assert_research_context([FakeFrame(module + ".main")])
    assert "future data" in str(exc.value)


def test_the_runtime_guard_allows_research_and_scripts():
    class FakeFrame:
        def __init__(self, name):
            self.frame = type("F", (), {"f_globals": {"__name__": name}})()

    assert_research_context(
        [FakeFrame("app.research.profiles"), FakeFrame("__main__"), FakeFrame("")]
    )


def test_the_forbidden_list_covers_the_three_paths_the_spec_names():
    for name in ("app.worker", "app.risk", "app.predict"):
        assert name in FORBIDDEN_IMPORTERS
