"""V2 experiment harness — the three comparisons in V2_SPEC.md section H.

This module does **not** contain a second backtester.  Every arm is one call to
:func:`app.backtest.engine.run_backtest` over the *same* bars; the only thing
that differs between arms is the ``Settings`` object handed to the engine.  If
an arm looks better, the reason is in the settings diff and nowhere else.

Three deliberate refusals, because an experiment harness that always produces a
winner is a random-number generator with a table renderer bolted on:

1. **Win rate is not a criterion.**  ``winner()`` selects on NET expectancy per
   trade.  A 70%-win arm that loses money loses to a 30%-win arm that does not.
2. **An under-sampled arm cannot win.**  Below ``min_trades`` (default 30) the
   expectancy estimate is noise, and the honest answer is "we do not know".
3. **A negative suite has no winner.**  When no arm is net-positive,
   ``winner()`` returns ``None`` and ``table()`` prints why.  Given V1's
   measured result (see ``docs/FINAL_REPORT.md``) this is the *expected*
   outcome, not a failure mode to be engineered around.

Derived portfolio metrics
-------------------------
``BacktestResult`` carries no portfolio panel, so ``idle_cash_pct`` and
``correlated_exposure_pct`` are reconstructed here by sweeping the closed-trade
intervals: time-weighted deployed notional against starting equity, and the
time-weighted net directional notional (all crypto longs are one crypto long,
per spec §D).  They are *derived from trade overlap*, labelled as such in the
table, and are not a substitute for ``PortfolioEngine.state()`` once a live
book exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Sequence

from app.config import Settings, get_settings
from app.types import Bar, Side

from .engine import (
    BacktestConfig,
    BacktestResult,
    Components,
    backtest_settings,
    build_components,
    run_backtest,
)

__all__ = [
    "ArmMetrics",
    "ExperimentArm",
    "ExperimentSuite",
    "DEFAULT_MIN_TRADES",
    "DEFAULT_MAX_DRAWDOWN_PCT",
    "ALLOCATION_MODES",
    "NET_TARGET_PROFILES",
    "RISK_BUDGETS_PCT",
    "CASH_RESERVES_PCT",
    "apply_overrides",
    "allocation_modes",
    "net_target_profiles",
    "risk_and_reserve_grid",
    "run_all",
    "suites_by_name",
]

MS_PER_DAY = 86_400_000

#: Below this many closed trades an expectancy figure is noise, not evidence.
#: The walk-forward report uses the same number for the same reason.
DEFAULT_MIN_TRADES = 30
#: Peak-to-trough equity loss, as a percentage of starting equity, above which
#: an arm is disqualified however good its expectancy looks.
DEFAULT_MAX_DRAWDOWN_PCT = 10.0

#: Spec §B — the four NET take-profit research profiles.
NET_TARGET_PROFILES: tuple[str, ...] = ("micro", "base", "fast", "extended")
#: Spec §H.2 — the three allocation modes.
ALLOCATION_MODES: tuple[str, ...] = ("few_large", "many_small", "dynamic")
#: Spec §H.3.
RISK_BUDGETS_PCT: tuple[float, ...] = (2.0, 3.0, 5.0)
CASH_RESERVES_PCT: tuple[float, ...] = (10.0, 20.0, 30.0)

Runner = Callable[..., BacktestResult]
ComponentFactory = Callable[[BacktestConfig, Settings], Components | None]


# --------------------------------------------------------------------------- #
# Settings overrides
# --------------------------------------------------------------------------- #


def apply_overrides(settings: Settings, overrides: dict[str, Any]) -> Settings:
    """Return a copy of ``settings`` with dotted paths replaced.

    ``{"profit.profile": "micro", "portfolio.cash_reserve_pct": 30.0}``.  Every
    config dataclass is frozen, so this rebuilds the chain with
    :func:`dataclasses.replace` rather than assigning.  An unknown path raises:
    an experiment that silently varies nothing is worse than one that crashes,
    because it produces four identical rows and an unearned winner.
    """
    if not overrides:
        return settings

    # Group by the top-level section so one section is rebuilt once, not once
    # per field ("portfolio.a" and "portfolio.b" must not clobber each other).
    grouped: dict[str, dict[str, Any]] = {}
    direct: dict[str, Any] = {}
    for path, value in overrides.items():
        head, _, rest = path.partition(".")
        if not rest:
            direct[head] = value
        else:
            grouped.setdefault(head, {})[rest] = value

    changes: dict[str, Any] = {}
    for key, value in direct.items():
        if not hasattr(settings, key):
            raise KeyError(f"unknown settings field: {key!r}")
        changes[key] = value

    for section, fields in grouped.items():
        if not hasattr(settings, section):
            raise KeyError(f"unknown settings section: {section!r}")
        node = getattr(settings, section)
        for name, value in fields.items():
            if "." in name:  # nested deeper than one level: recurse
                sub, _, rest = name.partition(".")
                if not hasattr(node, sub):
                    raise KeyError(f"unknown settings field: {section}.{sub!r}")
                node = replace(node, **{sub: apply_overrides(getattr(node, sub), {rest: value})})
                continue
            if not hasattr(node, name):
                raise KeyError(f"unknown settings field: {section}.{name}")
            node = replace(node, **{name: value})
        changes[section] = node

    return replace(settings, **changes)


def _default_component_factory(cfg: BacktestConfig, settings: Settings) -> Components | None:
    """Sentinel default — resolved to :func:`v2_components`, or V1 on failure."""
    try:
        return v2_components(cfg, settings)
    except Exception:
        # The production modules are not importable (or not V2 yet).  Returning
        # ``None`` hands the wiring back to ``run_backtest``, which builds the
        # V1 stack; the arm still runs and the table still says what it means.
        return None


def v2_components(cfg: BacktestConfig, settings: Settings) -> Components:
    """The real production objects, wired the way **V2** decides.

    ``engine.build_components`` is frozen at the V1 wiring: it constructs a
    ``RiskEngine`` with no portfolio and the harness builds a ``RiskState``
    whose ``portfolio`` is ``None``, which is precisely the branch where
    ``RiskEngine.evaluate`` falls back to the V1 *count* cap and ignores
    ``settings.portfolio`` entirely.  Running the §H.3 risk/reserve grid against
    that wiring would compare nine identical numbers.

    So this factory changes exactly two things and nothing else:

    * the ``RiskEngine`` shares one ``PortfolioEngine`` (so the correlation view
      the allocator holds is the one the gate reads), and
    * every ``RiskState`` carries a ``PortfolioState`` snapshotted from the
      live ledger, including the harness's provisional in-flight entries — an
      approved-but-unfilled order is already committed capital and committed
      risk, and leaving it out would let the budgets be breached during the
      latency window.

    No correlation *view* is supplied, deliberately: the only bars available
    here are the whole series, and updating the view from them would let a
    decision at day 3 see day 80.  With no view, ``_rho_safe`` treats every
    pair as fully correlated, which is the conservative direction and matches
    spec §D ("eight simultaneous crypto longs are one large crypto long").
    """
    from app.portfolio import PortfolioEngine
    from app.risk import RiskEngine, RiskState

    comps = build_components(cfg, settings)
    engine = PortfolioEngine(settings=settings)
    box: dict[str, Any] = {}

    base_factory = comps.ledger_factory

    def ledger_factory(*args: Any, **kwargs: Any) -> Any:
        ledger = base_factory(*args, **kwargs)
        box["ledger"] = ledger
        return ledger

    def risk_state(**kw: Any) -> Any:
        ledger = box.get("ledger")
        if ledger is None:
            return RiskState(**kw)
        book = _CommittedBook(ledger, list(kw.get("open_positions") or []))
        return RiskState(**kw, portfolio=engine.state(book, ts_ms=int(kw["ts_ms"])))

    return replace(
        comps,
        risk=RiskEngine(settings=settings, portfolio=engine),
        ledger_factory=ledger_factory,
        risk_state_cls=risk_state,
    )


@dataclass
class _CommittedBook:
    """A ledger view that also counts approved-but-unfilled entries.

    ``PortfolioEngine.state`` is duck-typed on ``equity`` / ``cash`` /
    ``exposure_usd`` / ``open_positions``, so this adapter can widen the book to
    the harness's committed set without touching the ledger.
    """

    ledger: Any
    committed: list[Any]

    @property
    def open_positions(self) -> list[Any]:
        return self.committed

    @property
    def equity(self) -> float:
        return float(getattr(self.ledger, "equity", 0.0))

    @property
    def cash(self) -> float:
        # Cash the ledger still shows as free but which a pending entry has
        # already spoken for.  Counting it as free would under-reserve.
        pending = sum(
            float(p.notional_usd)
            for p in self.committed
            if getattr(p, "meta", None) and p.meta.get("pending")
        )
        return float(getattr(self.ledger, "cash", 0.0)) - pending

    @property
    def exposure_usd(self) -> float:
        return sum(float(p.notional_usd) for p in self.committed)


def arm_settings(
    cfg: BacktestConfig, overrides: dict[str, Any], *, base: Settings | None = None
) -> Settings:
    """The exact ``Settings`` one arm's engine will see.

    Order matters: the backtest config's costs and limits are applied *first*
    (so fees and slippage always match ``cfg``), then the arm's overrides on
    top — the arm's variable is the experiment and must win any collision.
    """
    return apply_overrides(backtest_settings(cfg, base=base or get_settings()), overrides)


# --------------------------------------------------------------------------- #
# Per-arm metrics
# --------------------------------------------------------------------------- #


@dataclass
class ArmMetrics:
    """Every metric spec §H.1/§H.2 names, per arm.

    ``avg_spread_slippage_usd`` is one line because that is how the simulator
    charges it: a taker fill is priced by walking the book from mid, so the
    half-spread crossed and the depth walked arrive as a single deviation from
    mid.  Splitting them here would mean inventing a split the fill never had.
    """

    label: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate_pct: float = 0.0
    avg_gross_usd: float = 0.0
    avg_fees_usd: float = 0.0
    avg_spread_slippage_usd: float = 0.0
    avg_net_usd: float = 0.0
    profit_factor: float = 0.0
    expectancy_usd: float = 0.0
    net_pnl_usd: float = 0.0
    max_drawdown_usd: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_holding_minutes: float = 0.0
    trades_per_day: float = 0.0
    turnover_per_day: float = 0.0
    starting_equity: float = 0.0
    ending_equity: float = 0.0
    span_days: float = 0.0
    # Allocation-suite extras, derived from trade overlap (see module docstring).
    idle_cash_pct: float = 100.0
    correlated_exposure_pct: float = 0.0
    peak_concurrent_positions: int = 0
    ran: bool = False

    @classmethod
    def from_result(cls, label: str, result: BacktestResult | None) -> "ArmMetrics":
        if result is None:
            return cls(label=label, ran=False)
        s = result.stats
        span_ms = max(result.end_ms - result.start_ms, 0)
        days = span_ms / MS_PER_DAY if span_ms else 0.0
        n = s.trades
        eq0 = result.starting_equity or 0.0
        deployed_pct, correlated_pct, peak = _exposure_from_trades(result)
        return cls(
            label=label,
            trades=n,
            wins=s.wins,
            losses=s.losses,
            win_rate_pct=s.win_rate * 100.0,
            avg_gross_usd=s.gross_pnl / n if n else 0.0,
            avg_fees_usd=s.fees / n if n else 0.0,
            avg_spread_slippage_usd=s.slippage / n if n else 0.0,
            avg_net_usd=s.net_pnl / n if n else 0.0,
            profit_factor=s.profit_factor,
            expectancy_usd=s.expectancy,
            net_pnl_usd=s.net_pnl,
            max_drawdown_usd=s.max_drawdown,
            max_drawdown_pct=(s.max_drawdown / eq0 * 100.0) if eq0 > 0 else 0.0,
            avg_holding_minutes=s.avg_holding_seconds / 60.0,
            trades_per_day=(n / days) if days > 0 else 0.0,
            turnover_per_day=(s.capital_turnover / days) if days > 0 else 0.0,
            starting_equity=eq0,
            ending_equity=result.ending_equity,
            span_days=days,
            idle_cash_pct=max(0.0, 100.0 - deployed_pct),
            correlated_exposure_pct=correlated_pct,
            peak_concurrent_positions=peak,
            ran=True,
        )

    def to_dict(self) -> dict[str, Any]:
        out = dict(self.__dict__)
        if out["profit_factor"] == float("inf"):
            out["profit_factor"] = None  # JSON has no infinity
        return out


def _exposure_from_trades(result: BacktestResult) -> tuple[float, float, int]:
    """Time-weighted deployed % of equity, correlated exposure %, peak count.

    A sweep line over ``[entry_ms, exit_ms)`` of every closed trade.  Positions
    still open at the end of the run are not in ``result.trades`` and therefore
    not counted — stated here rather than quietly, because it biases deployed
    capital *down* on a run that ends mid-position.
    """
    trades = result.trades
    eq0 = result.starting_equity
    span_ms = max(result.end_ms - result.start_ms, 0)
    if not trades or eq0 <= 0 or span_ms <= 0:
        return 0.0, 0.0, 0

    events: list[tuple[int, float, float, int]] = []  # ts, d_notional, d_signed, d_count
    for t in trades:
        signed = t.notional_usd if t.side is Side.LONG else -t.notional_usd
        events.append((t.entry_ms, t.notional_usd, signed, 1))
        events.append((t.exit_ms, -t.notional_usd, -signed, -1))
    events.sort(key=lambda e: e[0])

    deployed = 0.0
    signed_net = 0.0
    count = 0
    peak = 0
    prev_ts = events[0][0]
    deployed_ms = 0.0
    correlated_ms = 0.0
    for ts, d_notional, d_signed, d_count in events:
        dt = ts - prev_ts
        if dt > 0:
            deployed_ms += deployed * dt
            correlated_ms += abs(signed_net) * dt
        deployed += d_notional
        signed_net += d_signed
        count += d_count
        peak = max(peak, count)
        prev_ts = ts

    deployed_pct = deployed_ms / span_ms / eq0 * 100.0
    correlated_pct = correlated_ms / span_ms / eq0 * 100.0
    return deployed_pct, correlated_pct, peak


# --------------------------------------------------------------------------- #
# Arms and suites
# --------------------------------------------------------------------------- #


@dataclass
class ExperimentArm:
    label: str
    settings_overrides: dict           # dotted, e.g. {"profit.profile": "micro"}
    result: BacktestResult | None = None
    #: The ``Settings`` the engine actually saw.  Recorded so a reader can
    #: verify the arm varied what it claims to vary.
    settings_used: Settings | None = None
    error: str | None = None

    @property
    def metrics(self) -> ArmMetrics:
        return ArmMetrics.from_result(self.label, self.result)


@dataclass
class ExperimentSuite:
    name: str
    arms: list[ExperimentArm]
    min_trades: int = DEFAULT_MIN_TRADES
    max_drawdown_pct: float = DEFAULT_MAX_DRAWDOWN_PCT
    #: Extra columns only the allocation comparison asks for (spec §H.2).
    show_portfolio_columns: bool = False
    notes: list[str] = field(default_factory=list)

    # -- selection ---------------------------------------------------------- #

    def metrics(self) -> list[ArmMetrics]:
        return [arm.metrics for arm in self.arms]

    def winner(self) -> ExperimentArm | None:
        """The arm with the highest repeatable NET expectancy, or ``None``.

        Never selects on win rate and never on raw net P&L: an arm that made
        more money by taking ten times as many trades at a worse per-trade
        expectancy has not proved anything about the *rule*, only about the
        number of times it was applied.  Refuses outright when nothing is
        net-positive, when the best candidate's sample is too small, or when
        every net-positive arm breached the drawdown ceiling.
        """
        return self._select()[0]

    def why_no_winner(self) -> str | None:
        """Plain-language reason there is no winner, or ``None`` when there is."""
        return self._select()[1]

    def _select(self) -> tuple[ExperimentArm | None, str | None]:
        ran = [a for a in self.arms if a.result is not None]
        if not self.arms:
            return None, "the suite has no arms"
        if not ran:
            return None, "no arm produced a result"

        if all(a.metrics.trades == 0 for a in ran):
            return None, (
                f"no arm opened a single trade across {len(ran)} arms — there is "
                "nothing to compare. A bot that refuses a market with no edge is "
                "behaving correctly, but it also produces no evidence."
            )

        positive = [a for a in ran if a.metrics.expectancy_usd > 0 and a.metrics.net_pnl_usd > 0]
        if not positive:
            best = max(ran, key=lambda a: a.metrics.expectancy_usd)
            m = best.metrics
            return None, (
                f"no arm is net-positive — the best of {len(ran)} ({best.label}) still "
                f"expects {m.expectancy_usd:+.4f}/trade over {m.trades} trades. "
                "Nothing here works; that is the result, not a tuning opportunity."
            )

        sampled = [a for a in positive if a.metrics.trades >= self.min_trades]
        if not sampled:
            best = max(positive, key=lambda a: a.metrics.expectancy_usd)
            m = best.metrics
            return None, (
                f"the best net-positive arm ({best.label}) closed only {m.trades} "
                f"trades — under the {self.min_trades}-trade minimum, so its "
                f"{m.expectancy_usd:+.4f}/trade expectancy is noise, not an edge"
            )

        acceptable = [a for a in sampled if a.metrics.max_drawdown_pct <= self.max_drawdown_pct]
        if not acceptable:
            best = max(sampled, key=lambda a: a.metrics.expectancy_usd)
            m = best.metrics
            return None, (
                f"every net-positive, adequately-sampled arm breached the "
                f"{self.max_drawdown_pct:.0f}% drawdown ceiling — best was "
                f"{best.label} at {m.max_drawdown_pct:.1f}%"
            )

        # Expectancy first; ties broken by shallower drawdown, then more trades.
        best = max(
            acceptable,
            key=lambda a: (
                a.metrics.expectancy_usd,
                -a.metrics.max_drawdown_pct,
                a.metrics.trades,
            ),
        )
        return best, None

    # -- rendering ---------------------------------------------------------- #

    def table(self) -> str:
        return _render_suite(self)

    def to_dict(self) -> dict[str, Any]:
        win = self.winner()
        return {
            "name": self.name,
            "min_trades": self.min_trades,
            "max_drawdown_pct": self.max_drawdown_pct,
            "winner": win.label if win else None,
            "no_winner_reason": self.why_no_winner(),
            "notes": list(dict.fromkeys(self.notes)),
            "arms": [
                {
                    "label": a.label,
                    "overrides": dict(a.settings_overrides),
                    "error": a.error,
                    "metrics": a.metrics.to_dict(),
                }
                for a in self.arms
            ],
        }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

WIDTH = 112


def _rule(char: str = "-") -> str:
    return char * WIDTH


def _pf(v: float) -> str:
    if v == float("inf"):
        return "inf"
    return f"{v:.2f}"


_PANEL_A_HEAD = (
    f"{'arm':<22}{'trades':>7}{'wins':>6}{'loss':>6}{'win%':>7}"
    f"{'avg gross':>11}{'avg fees':>10}{'avg sprd+slip':>15}{'avg NET':>11}"
)
_PANEL_B_HEAD = (
    f"{'arm':<22}{'PF':>7}{'expectancy':>12}{'NET P&L':>11}{'maxDD $':>10}"
    f"{'maxDD %':>9}{'avg hold':>10}{'trd/day':>9}{'turn/day':>10}{'end equity':>12}"
)
_PANEL_C_HEAD = (
    f"{'arm':<22}{'idle cash %':>13}{'correlated %':>14}{'peak open':>11}"
)


def _panel_a(m: ArmMetrics) -> str:
    return (
        f"{m.label[:22]:<22}{m.trades:>7}{m.wins:>6}{m.losses:>6}{m.win_rate_pct:>6.1f}%"
        f"{m.avg_gross_usd:>11.4f}{m.avg_fees_usd:>10.4f}"
        f"{m.avg_spread_slippage_usd:>15.4f}{m.avg_net_usd:>11.4f}"
    )


def _panel_b(m: ArmMetrics) -> str:
    return (
        f"{m.label[:22]:<22}{_pf(m.profit_factor):>7}{m.expectancy_usd:>+12.4f}"
        f"{m.net_pnl_usd:>+11.2f}{m.max_drawdown_usd:>10.2f}{m.max_drawdown_pct:>8.1f}%"
        f"{m.avg_holding_minutes:>9.1f}m{m.trades_per_day:>9.2f}"
        f"{m.turnover_per_day:>10.2f}{m.ending_equity:>12,.2f}"
    )


def _panel_c(m: ArmMetrics) -> str:
    return (
        f"{m.label[:22]:<22}{m.idle_cash_pct:>12.1f}%{m.correlated_exposure_pct:>13.1f}%"
        f"{m.peak_concurrent_positions:>11}"
    )


def _identical_arm_note(suite: ExperimentSuite) -> str | None:
    """Flag arms that varied a setting nothing in the stack reads yet.

    While ``app/profit.py`` and ``app/portfolio.py`` are still being written,
    changing ``profit.profile`` or ``portfolio.allocation_mode`` produces four
    byte-identical runs.  That is a wiring fact, not a finding, and it must be
    said out loud or the table implies the parameter does not matter.
    """
    ran = [a for a in suite.arms if a.result is not None]
    if len(ran) < 2:
        return None
    if all(a.metrics.trades == 0 for a in ran):
        # Nothing was compared, but the reason is "no trades", not "no-op knob".
        return None
    sigs = {
        (
            a.metrics.trades,
            round(a.metrics.net_pnl_usd, 8),
            round(a.metrics.avg_gross_usd, 8),
            round(a.metrics.ending_equity, 8),
        )
        for a in ran
    }
    if len(sigs) > 1:
        return None
    keys = sorted({k for a in suite.arms for k in a.settings_overrides})
    return (
        "every arm produced an identical result — the varied setting "
        f"({', '.join(keys) or 'n/a'}) is not consumed by the decision stack "
        "on this code path, so this suite compares nothing yet"
    )


def _render_suite(suite: ExperimentSuite) -> str:
    out = [
        _rule("="),
        f"EXPERIMENT — {suite.name}",
        _rule("="),
    ]
    if not suite.arms:
        out += ["  (no arms)", "", f"  NO WINNER — {suite.why_no_winner()}"]
        return "\n".join(out)

    metrics = suite.metrics()
    out += ["", _PANEL_A_HEAD, _rule()]
    out += [_panel_a(m) for m in metrics]
    out += ["", _PANEL_B_HEAD, _rule()]
    out += [_panel_b(m) for m in metrics]
    if suite.show_portfolio_columns:
        out += [
            "",
            _PANEL_C_HEAD,
            _rule(),
        ]
        out += [_panel_c(m) for m in metrics]
        out += [
            "  (idle cash and correlated exposure are time-weighted from closed-trade",
            "   overlap — positions still open at the end of the run are not counted)",
        ]

    not_run = [a.label for a in suite.arms if a.result is None]
    if not_run:
        out += ["", f"  arms that did not run: {', '.join(not_run)}"]
    errors = [(a.label, a.error) for a in suite.arms if a.error]
    for label, err in errors:
        out.append(f"  ! {label}: {err}")

    out += ["", _rule()]
    win = suite.winner()
    if win is not None:
        m = win.metrics
        out += [
            f"  WINNER: {win.label}",
            f"    chosen on NET expectancy {m.expectancy_usd:+.4f}/trade over "
            f"{m.trades} trades, max drawdown {m.max_drawdown_pct:.1f}%.",
            f"    overrides: {win.settings_overrides or '{}'}",
            "    Not chosen on win rate and not on total net P&L.",
        ]
        best_wr = max(metrics, key=lambda m_: m_.win_rate_pct)
        if best_wr.label != win.label:
            out.append(
                f"    ({best_wr.label} had the higher win rate, "
                f"{best_wr.win_rate_pct:.1f}% vs {m.win_rate_pct:.1f}%, and did not win.)"
            )
    else:
        out += [f"  NO WINNER — {suite.why_no_winner()}"]

    note = _identical_arm_note(suite)
    if note:
        out += ["", f"  ! {note}"]
    for n in dict.fromkeys(suite.notes):
        out.append(f"  - {n}")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Running a suite
# --------------------------------------------------------------------------- #


def _run_suite(
    name: str,
    arms: Sequence[ExperimentArm],
    bars_by_symbol: dict[str, list[Bar]],
    cfg: BacktestConfig,
    *,
    settings: Settings | None,
    runner: Runner,
    component_factory: ComponentFactory | None,
    min_trades: int,
    max_drawdown_pct: float,
    show_portfolio_columns: bool = False,
    progress: Callable[[str], None] | None = None,
) -> ExperimentSuite:
    suite = ExperimentSuite(
        name=name,
        arms=list(arms),
        min_trades=min_trades,
        max_drawdown_pct=max_drawdown_pct,
        show_portfolio_columns=show_portfolio_columns,
    )
    if not bars_by_symbol:
        suite.notes.append("no bars supplied — every arm was skipped")
        return suite

    fell_back = False
    for arm in suite.arms:
        try:
            s = arm_settings(cfg, arm.settings_overrides, base=settings)
        except KeyError as exc:
            arm.error = f"bad override: {exc}"
            suite.notes.append(f"{arm.label}: {arm.error}")
            continue
        arm.settings_used = s
        if progress:
            progress(f"{name}: running arm {arm.label} ...")
        # Components are rebuilt per arm on purpose: a FeatureEngine carries
        # per-symbol history, so sharing one would leak arm N into arm N+1.
        components = component_factory(cfg, s) if component_factory is not None else None
        if component_factory is _default_component_factory and components is None:
            fell_back = True
        try:
            arm.result = runner(
                bars_by_symbol,
                cfg,
                label=f"{name}/{arm.label}",
                components=components,
                settings=s,
            )
        except Exception as exc:  # pragma: no cover - surfaced in the table
            arm.error = repr(exc)
            suite.notes.append(f"{arm.label} failed: {exc!r}")

    if component_factory is _default_component_factory:
        suite.notes.append(
            "wiring: V1 count cap (the V2 portfolio stack was not importable) —"
            " portfolio.* settings cannot bind"
            if fell_back
            else "wiring: V2 — RiskEngine shares a PortfolioEngine and every"
            " RiskState carries a PortfolioState, so portfolio.* budgets bind"
        )
    return suite


def _arms(pairs: Iterable[tuple[str, dict[str, Any]]]) -> list[ExperimentArm]:
    return [ExperimentArm(label=label, settings_overrides=ov) for label, ov in pairs]


# --------------------------------------------------------------------------- #
# The three comparisons (spec §H)
# --------------------------------------------------------------------------- #


def net_target_profiles(
    bars_by_symbol: dict[str, list[Bar]],
    cfg: BacktestConfig,
    *,
    settings: Settings | None = None,
    runner: Runner = run_backtest,
    component_factory: ComponentFactory | None = _default_component_factory,
    min_trades: int = DEFAULT_MIN_TRADES,
    max_drawdown_pct: float = DEFAULT_MAX_DRAWDOWN_PCT,
    progress: Callable[[str], None] | None = None,
) -> ExperimentSuite:
    """§H.1 — NET take-profit profiles: micro 0.25 / base 0.50 / fast 0.75 / extended 1.00%."""
    base = settings or get_settings()
    labels = {
        p: f"{p} {base.profit.target_pct(p):.2f}% NET" for p in NET_TARGET_PROFILES
    }
    arms = _arms((labels[p], {"profit.profile": p}) for p in NET_TARGET_PROFILES)
    return _run_suite(
        "NET target profiles",
        arms,
        bars_by_symbol,
        cfg,
        settings=settings,
        runner=runner,
        component_factory=component_factory,
        min_trades=min_trades,
        max_drawdown_pct=max_drawdown_pct,
        progress=progress,
    )


def allocation_modes(
    bars_by_symbol: dict[str, list[Bar]],
    cfg: BacktestConfig,
    *,
    settings: Settings | None = None,
    runner: Runner = run_backtest,
    component_factory: ComponentFactory | None = _default_component_factory,
    min_trades: int = DEFAULT_MIN_TRADES,
    max_drawdown_pct: float = DEFAULT_MAX_DRAWDOWN_PCT,
    progress: Callable[[str], None] | None = None,
) -> ExperimentSuite:
    """§H.2 — FEW-LARGE vs MANY-SMALL vs DYNAMIC.  MANY-SMALL is not assumed to win."""
    arms = _arms((mode, {"portfolio.allocation_mode": mode}) for mode in ALLOCATION_MODES)
    return _run_suite(
        "allocation modes",
        arms,
        bars_by_symbol,
        cfg,
        settings=settings,
        runner=runner,
        component_factory=component_factory,
        min_trades=min_trades,
        max_drawdown_pct=max_drawdown_pct,
        show_portfolio_columns=True,
        progress=progress,
    )


def risk_and_reserve_grid(
    bars_by_symbol: dict[str, list[Bar]],
    cfg: BacktestConfig,
    *,
    settings: Settings | None = None,
    runner: Runner = run_backtest,
    component_factory: ComponentFactory | None = _default_component_factory,
    min_trades: int = DEFAULT_MIN_TRADES,
    max_drawdown_pct: float = DEFAULT_MAX_DRAWDOWN_PCT,
    progress: Callable[[str], None] | None = None,
) -> ExperimentSuite:
    """§H.3 — total open risk 2/3/5% crossed with cash reserve 10/20/30%."""
    arms = _arms(
        (
            f"risk {risk:.0f}% / res {reserve:.0f}%",
            {
                "portfolio.total_open_risk_pct": risk,
                "portfolio.cash_reserve_pct": reserve,
            },
        )
        for risk in RISK_BUDGETS_PCT
        for reserve in CASH_RESERVES_PCT
    )
    return _run_suite(
        "risk budget x cash reserve",
        arms,
        bars_by_symbol,
        cfg,
        settings=settings,
        runner=runner,
        component_factory=component_factory,
        min_trades=min_trades,
        max_drawdown_pct=max_drawdown_pct,
        show_portfolio_columns=True,
        progress=progress,
    )


SUITE_BUILDERS: dict[str, Callable[..., ExperimentSuite]] = {
    "profiles": net_target_profiles,
    "allocation": allocation_modes,
    "risk": risk_and_reserve_grid,
}


def suites_by_name(names: Sequence[str]) -> list[Callable[..., ExperimentSuite]]:
    """Look up suite builders by CLI name, preserving the caller's order."""
    out = []
    for n in names:
        key = n.strip().lower()
        if key not in SUITE_BUILDERS:
            raise KeyError(f"unknown suite {n!r}; known: {', '.join(SUITE_BUILDERS)}")
        out.append(SUITE_BUILDERS[key])
    return out


def run_all(
    bars_by_symbol: dict[str, list[Bar]],
    cfg: BacktestConfig,
    *,
    settings: Settings | None = None,
    runner: Runner = run_backtest,
    component_factory: ComponentFactory | None = _default_component_factory,
    min_trades: int = DEFAULT_MIN_TRADES,
    max_drawdown_pct: float = DEFAULT_MAX_DRAWDOWN_PCT,
    only: Sequence[str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> list[ExperimentSuite]:
    """Run every §H comparison (or the subset named in ``only``)."""
    builders = suites_by_name(only) if only else list(SUITE_BUILDERS.values())
    return [
        build(
            bars_by_symbol,
            cfg,
            settings=settings,
            runner=runner,
            component_factory=component_factory,
            min_trades=min_trades,
            max_drawdown_pct=max_drawdown_pct,
            progress=progress,
        )
        for build in builders
    ]


def format_suites(suites: Sequence[ExperimentSuite]) -> str:
    return "\n\n".join(s.table() for s in suites)
