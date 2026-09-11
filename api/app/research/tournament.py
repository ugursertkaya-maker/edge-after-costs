"""The strategy tournament — a walk-forward search over the candidate space.

What this module is
-------------------
A *search harness*.  It enumerates candidates
``(family, symbol, params, horizon, exit_profile, stop_profile, regime)``,
executes each one through the existing :func:`app.backtest.engine.run_backtest`
— never a second execution path — and reports **out-of-sample NET expectancy**.

Three rules from ``docs/PHASE3_SPEC.md`` are structural here, not advisory:

1. **Walk-forward is the only thing that counts.**  Every arm is run on
   rolling train/test splits; :class:`ArmResult` carries ``in_sample`` and
   ``out_of_sample`` side by side and the report never prints one alone.
2. **Selection happens on train, never on test.**  The staged screen that
   decides which candidates earn the expensive confirmation schemes ranks on
   the *in-sample* number.  Ranking the screen on out-of-sample would turn the
   out-of-sample number into an in-sample number with extra steps.
3. **Losers are reported.**  Every arm that was evaluated appears in the
   output.  When nothing clears the gates the report says so instead of
   promoting the least-bad arm.

How a candidate is executed
---------------------------
``run_backtest`` drives the real ``FeatureEngine`` / ``RegimeClassifier`` /
ranker / ``RiskEngine`` / ``ExitEngine`` / ``PaperSimulator`` / ``Ledger``
stack through injected :class:`~app.backtest.engine.Components`.  The tournament
injects four adapters and nothing else:

``_CachedFeatures`` / ``_CachedRegime``
    Look-up tables built **once per (symbol, window)** and reused by every
    candidate in that window.  Feature computation is ~66% of a backtest's
    runtime, and it does not depend on the candidate, so computing it per
    candidate would be paying for the same answer thousands of times.
``_CandidateStrategy``
    Turns the family's :class:`ResearchSignal` at a timestamp into the
    :class:`~app.types.Signal` the live stack consumes, applying the entry
    quality floor (``min_score``) and the regime filter.
``_ProfileExits``
    A thin wrapper around the *real* ``ExitEngine``.  It decides only which
    level model the candidate's exit/stop profile means, and it supplies two
    inputs the bar harness cannot: the features at exit time (the engine passes
    ``None``) and the live signal-reversal flag.  Every actual exit decision —
    stop before target, trailing, checkpoint, time stop — stays in
    ``app.exits``.

Efficiency
----------
* features/regimes cached per ``(symbol, window)``;
* a free **signal-supply gate**: a candidate whose family cannot emit enough
  signals to reach the 30-trade minimum is abandoned before any backtest runs.
  The count is taken over the *whole* history, which is an upper bound on what
  the per-window cold-start replay can produce, so the gate can never discard a
  candidate that would have qualified;
* a two-pass schedule: every sampled candidate is screened on the cheapest
  walk-forward scheme, and only candidates that were profitable *in-sample*
  pay for the longer schemes;
* ``--jobs N`` over a process pool, parallel across ``(symbol, window)`` so the
  feature cache is shared by every candidate in a task;
* trades are aggregated inside the worker and only *statistics* cross the
  process boundary.  Full trade lists are retained only for arms that could
  plausibly be finalists (positive out-of-sample, enough trades), because
  ``robustness.py`` needs them and nothing else does.  That retention is
  **all-or-nothing per arm**: a partial trade list is not a cheaper complete
  one, it is a different result under the same name, and it is what made the
  Phase-3 exit sweep report 7/7 for an arm that had not been tested.  See
  :func:`trade_set_mismatch`, which is asserted on every merge.
"""

from __future__ import annotations

import enum
import math
import os
import time
from collections import deque
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from random import Random
from typing import Any, Callable, Iterable, Iterator, Sequence

from app.backtest.engine import (
    BARS_PER_DAY,
    HISTORY_BARS,
    BacktestConfig,
    Components,
    backtest_settings,
    combine_stats,
    run_backtest,
    slice_bars,
    synth_book,
    synth_symbol_info,
    synth_trades,
)
from app.backtest.walkforward import MS_PER_DAY, WalkForwardWindow, plan_windows
from app.config import Settings, get_settings
from app.types import (
    Bar,
    ClosedTrade,
    ExitReason,
    Features,
    MarketSnapshot,
    PerfStats,
    RegimeState,
    Side,
    Signal,
    SymbolInfo,
)

__all__ = [
    "ArmResult",
    "Candidate",
    "ExitProfile",
    "Regime7",
    "SignalPoint",
    "StopProfile",
    "TournamentConfig",
    "WalkForwardScheme",
    "DEFAULT_HORIZONS",
    "DEFAULT_EXIT_PROFILES",
    "DEFAULT_STOP_PROFILES",
    "DEFAULT_NET_TARGET_BPS",
    "DEFAULT_MIN_SCORES",
    "DEFAULT_SCHEMES",
    "enumerate_candidates",
    "run_tournament",
    "per_coin_table",
    "per_regime_table",
    "per_horizon_table",
    "per_exit_table",
    "per_stop_table",
    "entry_quality_table",
    "arms_table",
    "format_tournament",
    "stats_from_trades",
    "load_sources",
    "prepare_window",
    "TradeSetIntegrityError",
    "trade_set_mismatch",
    "validate_trade_set",
]

_BPS = 10_000.0


# --------------------------------------------------------------------------- #
# Cross-module contracts that may not have landed yet
# --------------------------------------------------------------------------- #

try:  # pragma: no cover - exercised by whichever half of the branch is real
    from app.research.regimes import Regime7  # type: ignore
except Exception:  # pragma: no cover - stand-in until regimes.py lands

    class Regime7(str, enum.Enum):
        """Local stand-in for ``app.research.regimes.Regime7``.

        Same members, same values, so a ``Candidate`` built against this enum
        compares equal to one built against the real one (both are ``str``
        enums).  It exists only so this module imports standalone.
        """

        TRENDING_UP = "TRENDING_UP"
        TRENDING_DOWN = "TRENDING_DOWN"
        RANGING = "RANGING"
        VOLATILITY_EXPANSION = "VOLATILITY_EXPANSION"
        LOW_VOLATILITY = "LOW_VOLATILITY"
        BREAKOUT = "BREAKOUT"
        PANIC = "PANIC"


def _regime7_available() -> bool:
    try:  # pragma: no cover - trivial
        import app.research.regimes  # noqa: F401
    except Exception:
        return False
    return True


# --------------------------------------------------------------------------- #
# Exit / stop profiles
# --------------------------------------------------------------------------- #

#: Exit profile kinds.  The spec asks for entries and exits to be searched
#: independently, which is why an exit profile is a first-class axis rather
#: than something a strategy family decides for itself.
#:
#: ``net_fixed``  a fixed **NET** percentage target, solved backwards through
#:                every cost line by ``app.profit.NetTargetEngine``
#: ``atr``        ATR/volatility-scaled, ``mult × 1m-ATR × sqrt(horizon_min)``
#: ``trail``      a deliberately distant target; the trailing stop does the work
#: ``structure``  the far edge of the recent range (Bollinger width × range_pos)
#: ``decay``      hold until the signal decays — the checkpoint/stale-exit path
EXIT_KINDS: tuple[str, ...] = ("net_fixed", "atr", "trail", "structure", "decay")

#: Stop profile kinds: fixed %, ATR, realized-volatility, structure, time-only,
#: and signal reversal.
STOP_KINDS: tuple[str, ...] = ("fixed", "atr", "vol", "structure", "time", "reversal")


@dataclass(frozen=True)
class ExitProfile:
    """A parsed ``exit_profile`` string: ``kind`` plus one scalar."""

    kind: str
    value: float = 0.0

    def __str__(self) -> str:
        return f"{self.kind}:{_fmt_num(self.value)}" if self.value else self.kind

    @staticmethod
    def parse(text: str) -> "ExitProfile":
        kind, value = _split_profile(text)
        if kind not in EXIT_KINDS:
            raise ValueError(f"unknown exit profile kind: {kind!r}")
        return ExitProfile(kind, value)


@dataclass(frozen=True)
class StopProfile:
    """A parsed ``stop_profile`` string: ``kind`` plus one scalar."""

    kind: str
    value: float = 0.0

    def __str__(self) -> str:
        return f"{self.kind}:{_fmt_num(self.value)}" if self.value else self.kind

    @staticmethod
    def parse(text: str) -> "StopProfile":
        kind, value = _split_profile(text)
        if kind not in STOP_KINDS:
            raise ValueError(f"unknown stop profile kind: {kind!r}")
        return StopProfile(kind, value)


def _split_profile(text: str) -> tuple[str, float]:
    raw = str(text).strip()
    if ":" in raw:
        kind, _, val = raw.partition(":")
        return kind.strip(), float(val)
    return raw, 0.0


def _fmt_num(v: float) -> str:
    return f"{v:g}"


# --------------------------------------------------------------------------- #
# The frozen cross-module contracts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, eq=False)
class Candidate:
    """One point of the search space.

    ``params`` is a plain dict (families publish their grids as dicts), so
    equality and hashing go through :meth:`key` rather than the generated
    dataclass versions — a dict field would make the generated ``__hash__``
    explode at the first ``set()``.
    """

    family: str
    symbol: str
    params: dict
    horizon_seconds: int
    exit_profile: str
    stop_profile: str
    regime: Regime7 | None = None

    def key(self) -> str:
        """A stable, unique identity string.

        Stable across processes and runs: ``params`` is rendered in sorted key
        order with ``repr`` values, so two candidates differ in their key if and
        only if they differ in some field.
        """
        params = ",".join(f"{k}={_fmt_param(self.params[k])}" for k in sorted(self.params))
        regime = self.regime.value if self.regime is not None else "any"
        return (
            f"{self.family}|{self.symbol}|{params}"
            f"|h={self.horizon_seconds}|x={self.exit_profile}"
            f"|s={self.stop_profile}|r={regime}"
        )

    # Identity is the key, for both ``==`` and ``hash``.
    def __eq__(self, other: object) -> bool:
        return isinstance(other, Candidate) and self.key() == other.key()

    def __hash__(self) -> int:
        return hash(self.key())

    @property
    def exit(self) -> ExitProfile:
        return ExitProfile.parse(self.exit_profile)

    @property
    def stop(self) -> StopProfile:
        return StopProfile.parse(self.stop_profile)

    @property
    def min_score(self) -> float:
        return float(self.params.get("min_score", 0.0))

    @property
    def horizon_minutes(self) -> float:
        return self.horizon_seconds / 60.0


def _fmt_param(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v)


@dataclass
class ArmResult:
    """Everything measured about one candidate, in and out of sample."""

    candidate: Candidate
    trades: list[ClosedTrade]
    stats: PerfStats
    fees_usd: float
    slippage_usd: float
    windows: list[PerfStats]          # per walk-forward window, out-of-sample
    in_sample: PerfStats
    out_of_sample: PerfStats
    # Defaulted extras — the constructor order above is the frozen contract.
    per_regime: dict[str, PerfStats] = field(default_factory=dict)
    exit_reasons: dict[str, int] = field(default_factory=dict)
    schemes: dict[str, PerfStats] = field(default_factory=dict)
    best_trade_usd: float = 0.0
    worst_trade_usd: float = 0.0
    signals_available: int = 0
    windows_evaluated: int = 0
    windows_positive: int = 0
    abandoned: str | None = None
    notes: list[str] = field(default_factory=list)
    #: The out-of-sample trades specifically.  ``trades`` above happens to hold
    #: the same list today — the tournament only ever retains out-of-sample
    #: trades — but ``robustness.py`` must not have to rely on that coincidence:
    #: gate 4 ("not dominated by one trade") and the cost-stress re-pricing are
    #: both defined on the out-of-sample half, and an arm whose ``trades`` ever
    #: grew to include in-sample round trips would silently answer both wrong.
    #:
    #: **All or nothing.**  This list is either every out-of-sample trade that
    #: ``out_of_sample`` was computed from, or empty.  A partial list is not a
    #: cheaper version of a complete one — it is a different strategy wearing
    #: the same name — so ``_merge`` empties it rather than let it drift.
    out_of_sample_trades: list[ClosedTrade] = field(default_factory=list)
    #: How many merged walk-forward windows contributed to the list above.
    #: Bookkeeping for the all-or-nothing rule: the moment this falls behind
    #: ``windows_evaluated`` the list is incomplete and is dropped.
    trade_windows_kept: int = 0

    @property
    def key(self) -> str:
        return self.candidate.key()

    @property
    def trades_complete(self) -> bool:
        """True when ``out_of_sample_trades`` is the whole out-of-sample set.

        The gates in ``robustness.py`` re-derive this independently, from the
        count and the net, so neither side has to trust the other; this is the
        producing end's own view of the same fact.
        """
        return (
            bool(self.out_of_sample_trades)
            and self.trade_windows_kept == self.windows_evaluated
            and len(self.out_of_sample_trades) == self.out_of_sample.trades
        )

    @property
    def oos_expectancy(self) -> float:
        return self.out_of_sample.expectancy

    @property
    def expectancy_without_best_trade(self) -> float:
        """Gate 4 of the promotion gates, when the trades were retained."""
        s = self.out_of_sample
        if s.trades <= 1 or not self.out_of_sample_trades:
            return s.expectancy
        return (s.net_pnl - self.best_trade_usd) / (s.trades - 1)

    @property
    def majority_windows_positive(self) -> bool:
        if self.windows_evaluated == 0:
            return False
        return self.windows_positive * 2 > self.windows_evaluated


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WalkForwardScheme:
    train_days: int
    test_days: int

    @property
    def label(self) -> str:
        return f"{self.train_days}/{self.test_days}"

    @staticmethod
    def parse(text: str) -> "WalkForwardScheme":
        a, _, b = str(text).strip().partition("/")
        return WalkForwardScheme(int(a), int(b))

    @property
    def cost_weight(self) -> float:
        """Rough relative cost of one window, used to order the schedule."""
        return float(self.train_days + self.test_days)


#: Horizons in minutes.  None of these is a *minimum* hold — early take-profit,
#: early stop, signal invalidation, the trailing exit and the staleness exit all
#: remain live inside the window.  The horizon is the time stop, the point at
#: which the capital comes back regardless.
DEFAULT_HORIZONS: tuple[int, ...] = (2, 5, 10, 15, 25, 40, 60, 90, 120)

#: NET take-profit targets, in bps, for the ``net_fixed`` exit profile.
#:
#: Range justification.  ``docs/ECONOMICS.md`` measures a typical 60-minute move
#: on Binance.US BTC/USD at ~**17 bps** (1m ATR 2.2 bps, diffusion-scaled), and a
#: Tier-0 round trip at ~1 bps of fee plus 0.5-2 bps of spread plus slippage —
#: call it 3-5 bps all-in.  So:
#:   * below ~4 bps a target does not clear its own round trip and is not a
#:     target at all, which is why the ladder starts at 3 (one rung *inside* the
#:     cost wall, deliberately, so the report shows the wall rather than
#:     assuming it);
#:   * 17 bps is the whole typical hour;
#:   * 110 bps is ~6.5x a typical hour, i.e. the ``extended`` 1.00% profile from
#:     V2 — kept so the old profiles remain comparable, and so the report can
#:     show where the trade count collapses to nothing.
#: The rungs are roughly geometric because what matters is the ratio to the
#: cost, not the arithmetic difference.
DEFAULT_NET_TARGET_BPS: tuple[float, ...] = (
    3.0, 5.0, 8.0, 12.0, 17.0, 25.0, 35.0, 50.0, 75.0, 110.0,
)

#: The exit profiles swept by default: the NET ladder above plus one
#: representative of every other family of exit the spec names.
DEFAULT_EXIT_PROFILES: tuple[str, ...] = tuple(
    [f"net_fixed:{_fmt_num(v)}" for v in DEFAULT_NET_TARGET_BPS]
    + ["atr:0.75", "atr:1.5", "atr:3", "trail:0.25", "trail:0.5", "structure", "decay"]
)

#: Stop profiles.  ``fixed`` values are bps; ``atr``/``vol`` are multipliers of
#: the horizon-scaled volatility; ``time`` means "no price stop, the clock is the
#: stop"; ``reversal`` means "leave when the family flips side".
DEFAULT_STOP_PROFILES: tuple[str, ...] = (
    "fixed:10", "fixed:25", "fixed:50",
    "atr:1", "atr:2", "atr:3",
    "vol:1.5", "vol:3",
    "structure", "time", "reversal",
)

#: Entry-quality ladder — the minimum signal score a candidate will act on.
DEFAULT_MIN_SCORES: tuple[float, ...] = (0.0, 0.35, 0.5, 0.65, 0.8, 0.9)

#: Median absolute move by horizon, in bps, measured on the real 1m cache over
#: 242 days by ``scripts/horizon_ceiling.py``.  This replaces the older framing
#: — "the cost of trading is the same size as the move" — which is true at one
#: minute and **wrong** at 25-60 minutes, where the median move is 4-10x the
#: ~3 bps taker round trip.  V1 and V2 both lost money at 3-8 minute average
#: holds; on this table that looks like an artefact of the exit logic, not
#: proof that the venue cannot be traded, and the tournament report must not
#: carry the old conclusion forward.
MEASURED_MEDIAN_MOVE_BPS: dict[str, dict[int, float]] = {
    "BTCUSD": {2: 0.9, 5: 4.5, 25: 12.9, 60: 19.7},
    "ETHUSD": {60: 27.3},
    "SOLUSD": {60: 33.3},
}

#: Fallback when a symbol was not measured: BTC's shape.
_MOVE_FALLBACK = "BTCUSD"


def median_move_bps(symbol: str, horizon_minutes: float) -> float:
    """Typical absolute move over ``horizon_minutes``, in bps.

    Measured where a measurement exists; otherwise diffusion-scaled
    (``sqrt(t)``) from the nearest measured horizon for that symbol, which is
    the same square-root law ``docs/ECONOMICS.md`` uses and which reproduces the
    measured 2/5/25/60-minute BTC points to within a bp or two.
    """
    table = MEASURED_MEDIAN_MOVE_BPS.get(symbol) or MEASURED_MEDIAN_MOVE_BPS[_MOVE_FALLBACK]
    h = max(float(horizon_minutes), 1e-6)
    exact = table.get(int(round(h)))
    if exact is not None:
        return exact
    anchor = min(table, key=lambda k: abs(math.log(k / h)))
    return table[anchor] * math.sqrt(h / anchor)


def required_hit_rate(symbol: str, horizon_minutes: float, round_trip_bps: float) -> float:
    """The win rate a symmetric-payoff arm needs just to break even.

    A strategy that wins and loses the typical move captures
    ``(2p - 1) * E|move| - costs`` per trade, so break-even is
    ``p > 0.5 + costs / (2 * E|move|)``.  Printing this next to a measured win
    rate is the fastest way to tell a plausible result from noise: on BTC at 25
    minutes it is about 67% as a taker and barely above 50% as a maker.
    """
    move = median_move_bps(symbol, horizon_minutes)
    if move <= 0:
        return 1.0
    return min(1.0, 0.5 + max(0.0, round_trip_bps) / (2.0 * move))


DEFAULT_SCHEMES: tuple[WalkForwardScheme, ...] = (
    WalkForwardScheme(30, 7),
    WalkForwardScheme(60, 14),
    WalkForwardScheme(90, 30),
)


@dataclass
class TournamentConfig:
    """Everything the search needs, and nothing it does not."""

    symbols: tuple[str, ...] = ("BTCUSD",)
    families: tuple[str, ...] | None = None          # None => every available
    horizons_minutes: tuple[int, ...] = DEFAULT_HORIZONS
    exit_profiles: tuple[str, ...] = DEFAULT_EXIT_PROFILES
    stop_profiles: tuple[str, ...] = DEFAULT_STOP_PROFILES
    min_scores: tuple[float, ...] = DEFAULT_MIN_SCORES
    regimes: tuple[Regime7 | None, ...] = (None,) + tuple(Regime7)
    schemes: tuple[WalkForwardScheme, ...] = DEFAULT_SCHEMES

    # Baseline for the one-factor-at-a-time sweeps (see enumerate_candidates).
    baseline_horizon_minutes: int = 60
    baseline_exit_profile: str = "net_fixed:17"
    baseline_stop_profile: str = "atr:2"
    baseline_min_score: float = 0.5

    # Costs / execution
    fee_bps_per_side: float = 1.0
    slippage_bps: float = 2.0
    spread_bps: float | None = None
    starting_equity: float = 500.0
    risk_per_trade_pct: float = 0.5
    max_concurrent: int = 3
    latency_bars: int = 1
    min_net_edge_bps: float | None = None

    tape_cache_dir: str | None = None

    # Search budget
    max_candidates: int = 2_000
    max_params_per_family: int = 8
    random_extra_frac: float = 0.35      # share of the budget spent on interactions
    jobs: int = 1
    seed: int = 7

    # Gates / screening
    min_trades: int = 30
    screen_scheme_index: int = 0         # which scheme screens (cheapest first)
    screen_min_in_sample_expectancy: float = 0.0
    #: Retain full out-of-sample trade lists so ``robustness.py`` can gate on
    #: them.  When on, every window ships its trades to the merge — winning and
    #: losing alike, because an arm's trade list must be its *whole*
    #: out-of-sample history or nothing.  Filtering losing windows out of the
    #: pipe is what produced the false Phase-3 finalist and must not come back;
    #: the memory it used to save is instead bounded at the arm level, by
    #: ``max_kept_trade_arms``, or given up entirely by turning this off.
    keep_trades_for_finalists: bool = True
    #: How many arms may hold trades at once.  Once full, later arms keep
    #: statistics only — never a partial list.
    max_kept_trade_arms: int = 40

    # Reporting
    top_n: int = 25
    #: Estimated quoted spread, bps.  Only used to quote the required hit rate
    #: — the simulator charges the real synthesised book, not this number.
    spread_estimate_bps: float = 2.0

    @property
    def round_trip_bps(self) -> float:
        return 2.0 * self.fee_bps_per_side + self.slippage_bps + self.spread_estimate_bps

    def scheme_order(self) -> list[WalkForwardScheme]:
        return sorted(self.schemes, key=lambda s: (s.cost_weight, s.train_days))


# --------------------------------------------------------------------------- #
# Signal sources — where a "family" comes from
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SignalPoint:
    """A family's opinion at one timestamp, stripped to what the search needs."""

    side: Side
    score: float
    ts_ms: int
    reasons: tuple[str, ...] = ()


class SignalSource:
    """A named, parameterised opinion generator.

    Two implementations exist: :class:`ResearchFamilySource` wraps
    ``app.research.families`` (the real Phase-3 families), and
    :class:`LiveStrategySource` wraps the six production strategies in
    ``app.strategies``.  The tournament does not care which it is given, which
    is what lets the harness be developed and tested before ``families.py``
    lands and switch over without touching the search.
    """

    name: str = "source"

    def param_combinations(self, limit: int, seed: int) -> list[dict]:
        raise NotImplementedError

    def signals(self, prepared: "PreparedWindow", params: dict) -> dict[int, SignalPoint]:
        raise NotImplementedError

    def allowed_regimes(self) -> frozenset | None:
        return None


class LiveStrategySource(SignalSource):
    """The six production strategies, presented as single-parameter families.

    Their only searchable parameter is the entry-quality floor, which is the
    axis the spec explicitly asks for ("vary the minimum signal score").  Their
    own internals are not re-parameterised here: that is what
    ``app.research.families`` is for.
    """

    def __init__(self, name: str, *, settings: Settings | None = None) -> None:
        from app.strategies import get_strategies

        self.name = name
        self._strategy = get_strategies([name], settings=settings)[0]

    def param_combinations(self, limit: int, seed: int) -> list[dict]:
        return [{}]

    def signals(self, prepared: "PreparedWindow", params: dict) -> dict[int, SignalPoint]:
        out: dict[int, SignalPoint] = {}
        strat = self._strategy
        # The floor lives in the tournament, not in the strategy, so one pass
        # serves every rung of the entry-quality ladder.
        saved, strat.min_score = strat.min_score, 0.0
        try:
            for ts, f in prepared.features.items():
                sig = strat.evaluate(f, prepared.regimes[ts])
                if sig is None:
                    continue
                out[ts] = SignalPoint(
                    side=sig.side, score=float(sig.score), ts_ms=ts,
                    reasons=tuple(sig.reasons),
                )
        finally:
            strat.min_score = saved
        return out


class ResearchFamilySource(SignalSource):
    """A ``app.research.families.StrategyFamily``, driven by MTF features."""

    def __init__(self, family: Any) -> None:
        self.name = family.name
        self._family = family

    def param_combinations(self, limit: int, seed: int) -> list[dict]:
        space = getattr(self._family, "param_space", None)
        if space is None:
            return [{}]
        combos = list(space.combinations())
        if not combos:
            return [{}]
        if len(combos) <= limit:
            return combos
        # Deterministic thinning: an evenly spaced stride keeps the corners of
        # the grid rather than clustering wherever the RNG happened to land.
        stride = len(combos) / float(limit)
        picked = [combos[min(len(combos) - 1, int(i * stride))] for i in range(limit)]
        seen: list[dict] = []
        for c in picked:
            if c not in seen:
                seen.append(c)
        return seen

    def allowed_regimes(self) -> frozenset | None:
        return getattr(self._family, "allowed_regimes", None)

    def signals(self, prepared: "PreparedWindow", params: dict) -> dict[int, SignalPoint]:
        out: dict[int, SignalPoint] = {}
        mtf = prepared.mtf
        if not mtf:
            return out
        for ts, f in mtf.items():
            sig = self._family.signal(f, dict(params))
            if sig is None:
                continue
            out[ts] = SignalPoint(
                side=sig.side, score=float(sig.score), ts_ms=ts,
                reasons=tuple(getattr(sig, "reasons", ()) or ()),
            )
        return out


def research_families_available() -> bool:
    try:  # pragma: no cover - trivially branch-dependent
        from app.research.families import FAMILIES  # noqa: F401
    except Exception:
        return False
    return True


def load_sources(
    names: Iterable[str] | None = None, *, settings: Settings | None = None
) -> list[SignalSource]:
    """Every requested family, preferring the research families when present.

    ``names=None`` means "all".  When ``app.research.families`` is importable
    its ten families are used; otherwise the six live strategies stand in, so
    the harness always has something real to search.  The report states which
    was used — a tournament run against the fallback is a different experiment
    and must not be quoted as if it were the other one.
    """
    wanted = None if names is None else [n for n in names if n and n != "all"]
    if research_families_available():
        from app.research.families import FAMILIES  # type: ignore

        sources = [ResearchFamilySource(f) for f in FAMILIES]
    else:
        from app.strategies import STRATEGY_NAMES

        sources = [LiveStrategySource(n, settings=settings) for n in STRATEGY_NAMES]
    if wanted:
        by_name = {s.name: s for s in sources}
        missing = [n for n in wanted if n not in by_name]
        if missing:
            raise KeyError(f"unknown families: {', '.join(sorted(missing))}")
        sources = [by_name[n] for n in wanted]
    return sources


# --------------------------------------------------------------------------- #
# Window preparation — the feature cache
# --------------------------------------------------------------------------- #


@dataclass
class PreparedWindow:
    """Everything candidate-independent about one ``(symbol, bar-slice)``.

    Built once and shared by every candidate evaluated on that slice.  The
    replay here reproduces exactly what ``_Backtester._decide`` would have
    constructed — same 240-bar history deque, same synthesised book, same
    rolling 24h aggregates — with one deliberate difference: the synthetic
    trade prints are drawn from a per-bar seeded RNG rather than the
    backtester's shared stream.  That makes the features a pure function of
    ``(symbol, bar, seed)``, which is what allows the cache to be shared
    between candidates at all, and is why ``--jobs 1`` and ``--jobs 4`` return
    identical numbers.
    """

    symbol: str
    bars: list[Bar]
    features: dict[int, Features]
    regimes: dict[int, RegimeState]
    info: SymbolInfo
    #: entry timestamp (the fill bar's ``open_ms``) -> regime label at decision
    regime_at_entry: dict[int, str] = field(default_factory=dict)
    #: decision timestamp -> Regime7 label, when the classifier is available
    regime7: dict[int, str] = field(default_factory=dict)
    mtf: dict[int, Any] = field(default_factory=dict)

    @property
    def start_ms(self) -> int:
        return self.bars[0].open_ms if self.bars else 0

    @property
    def end_ms(self) -> int:
        return self.bars[-1].close_ms if self.bars else 0


def prepare_window(
    symbol: str,
    bars: Sequence[Bar],
    *,
    settings: Settings,
    spread_bps: float | None = None,
    seed: int = 7,
    want_mtf: bool = True,
    tape_cache_dir: str | None = None,
) -> PreparedWindow:
    """Replay ``bars`` cold through the real feature/regime stack.

    ``tape_cache_dir`` switches the multi-timeframe block onto the real tape:
    ``dataset.load_dataset`` rebuilds the 5s/15s/30s/1m/3m/5m/15m bars from
    aggTrades for exactly this window.  Without it the MTF block is built from
    the 1m bars alone, every sub-minute feature reads as missing, and the
    families that live there correctly stay silent — a degradation the report
    states rather than hides.
    """
    from app.features import FeatureEngine
    from app.regime import RegimeClassifier

    bars = sorted(bars, key=lambda b: b.open_ms)
    fe = FeatureEngine(settings=settings)
    rc = RegimeClassifier(settings=settings)

    hist: deque[Bar] = deque(maxlen=HISTORY_BARS)
    rq: deque[float] = deque(maxlen=BARS_PER_DAY)
    rt: deque[int] = deque(maxlen=BARS_PER_DAY)
    info = synth_symbol_info(symbol, bars[0].close if bars else 0.0)

    features: dict[int, Features] = {}
    regimes: dict[int, RegimeState] = {}
    regime7: dict[int, str] = {}
    regime_at_entry: dict[int, str] = {}
    prev_label = ""

    # The multi-timeframe block first: it is what the regime classifier and the
    # research families both read, and it is keyed by the same 1m close
    # timestamp the live Features are.
    mtf: dict[int, Any] = _build_mtf(symbol, bars, settings, tape_cache_dir) if want_mtf else {}
    r7c = _regime7_classifier(settings)
    if r7c is not None:
        for ts, mf in mtf.items():
            label = _classify_regime7(r7c, mf)
            if label:
                regime7[ts] = label

    for bar in bars:
        hist.append(bar)
        rq.append(bar.quote_volume)
        rt.append(bar.trades)
        # The bar that fills a decision taken on the previous close.
        regime_at_entry[bar.open_ms] = prev_label

        ts = bar.close_ms
        book = synth_book(bar, price=bar.close, spread_bps=spread_bps, ts_ms=ts)
        if not book.is_valid():
            prev_label = ""
            continue
        snap = MarketSnapshot(
            symbol=symbol,
            ts_ms=ts,
            book=book,
            bars_1m=list(hist),
            recent_trades=synth_trades(bar, Random(seed * 1_000_003 + bar.open_ms)),
            info=info,
            quote_volume_24h=float(sum(rq)),
            trades_24h=int(sum(rt)),
        )
        f = fe.compute(snap)
        regime = rc.classify(f)
        features[ts] = f
        regimes[ts] = regime
        prev_label = regime7.get(ts) or regime.regime.value

    return PreparedWindow(
        symbol=symbol,
        bars=list(bars),
        features=features,
        regimes=regimes,
        info=info,
        regime_at_entry=regime_at_entry,
        regime7=regime7,
        mtf=mtf,
    )


def _regime7_classifier(settings: Settings) -> Any | None:
    try:  # pragma: no cover - depends on a sibling workstream
        from app.research.regimes import Regime7Classifier  # type: ignore
    except Exception:
        return None
    try:
        return Regime7Classifier()
    except Exception:
        try:
            return Regime7Classifier(settings=settings)  # type: ignore[call-arg]
        except Exception:
            return None


def _classify_regime7(classifier: Any, f: Any) -> str:
    """``(Regime7, confidence)`` -> label, tolerating either return shape."""
    try:
        out = classifier.classify(f)
    except Exception:
        return ""
    regime = out[0] if isinstance(out, tuple) else out
    return getattr(regime, "value", str(regime))


def _aggregate_bars(bars: Sequence[Bar], factor: int) -> list[Bar]:
    """1m bars folded up into ``factor``-minute bars, aligned to the epoch.

    Only used on the bars-only fallback path: with the tape available
    ``load_dataset`` builds every interval from the trades themselves, which is
    strictly better.
    """
    out: list[Bar] = []
    span = factor * 60_000
    bucket: list[Bar] = []
    current = None
    for b in bars:
        key = b.open_ms - (b.open_ms % span)
        if current is None:
            current = key
        if key != current and bucket:
            out.append(_fold(bucket))
            bucket = []
            current = key
        bucket.append(b)
    if bucket:
        out.append(_fold(bucket))
    return out


def _fold(group: list[Bar]) -> Bar:
    return Bar(
        symbol=group[0].symbol,
        open_ms=group[0].open_ms,
        close_ms=group[-1].close_ms,
        open=group[0].open,
        high=max(b.high for b in group),
        low=min(b.low for b in group),
        close=group[-1].close,
        volume=sum(b.volume for b in group),
        quote_volume=sum(b.quote_volume for b in group),
        trades=sum(b.trades for b in group),
        taker_buy_quote=sum(b.taker_buy_quote for b in group),
    )


def _build_mtf(
    symbol: str,
    bars: Sequence[Bar],
    settings: Settings,
    tape_cache_dir: str | None,
) -> dict[int, Any]:
    """MTF features keyed by the 1m close timestamp.

    Degrades to ``{}`` — a family that needs MTF features then produces no
    signals and is abandoned by the signal-supply gate, which is visible in the
    report rather than silently wrong.
    """
    return {int(mf.ts_ms): mf for mf in stream_mtf(symbol, bars, settings, tape_cache_dir)}


def stream_mtf(
    symbol: str,
    bars: Sequence[Bar],
    settings: Settings,
    tape_cache_dir: str | None,
) -> Iterator[Any]:
    """The MTF feature block as a generator, so a long history need not be
    materialised: the signal-supply gate reads 120 days per symbol and holding
    124 floats per minute for all of it would cost about a gigabyte."""
    if not bars:
        return
    try:  # pragma: no cover - depends on a sibling workstream
        from app.research.dataset import Dataset  # type: ignore
        from app.research.features_mtf import MTFFeatureEngine  # type: ignore
    except Exception:
        return

    start_ms, end_ms = bars[0].open_ms, bars[-1].close_ms
    dataset = None
    if tape_cache_dir:
        try:
            from app.research.dataset import load_dataset  # type: ignore

            dataset = load_dataset(symbol, start_ms, end_ms + 1, cache_dir=tape_cache_dir)
            if not dataset.bars.get("1m"):
                dataset = None
        except Exception:
            dataset = None
    if dataset is None:
        try:
            dataset = Dataset(
                symbol=symbol,
                bars={
                    "1m": list(bars),
                    "3m": _aggregate_bars(bars, 3),
                    "5m": _aggregate_bars(bars, 5),
                    "15m": _aggregate_bars(bars, 15),
                },
                trades=[],
                start_ms=start_ms,
                end_ms=end_ms,
            )
        except Exception:
            return
    try:
        engine = MTFFeatureEngine(settings=settings)
        stream = engine.stream(dataset, step="1m")
    except Exception:
        return
    # The dataset is the only strong reference to a 30-day 5s bar series; the
    # engine holds what it needs, so let this frame drop it as we iterate.
    del dataset
    try:
        yield from stream
    except Exception:  # pragma: no cover - a partial block is still usable
        return


# --------------------------------------------------------------------------- #
# Injected components
# --------------------------------------------------------------------------- #


class _CachedFeatures:
    """``FeatureEngine``-shaped read-through into a :class:`PreparedWindow`."""

    def __init__(self, prepared: dict[str, PreparedWindow]) -> None:
        self._prepared = prepared

    def compute(self, snap: MarketSnapshot) -> Features:
        prep = self._prepared[snap.symbol]
        f = prep.features.get(snap.ts_ms)
        if f is None:
            raise KeyError(f"no cached features for {snap.symbol} @ {snap.ts_ms}")
        return f

    def reset(self, symbol: str | None = None) -> None:  # pragma: no cover - trivial
        return None


class _CachedRegime:
    def __init__(self, prepared: dict[str, PreparedWindow]) -> None:
        self._prepared = prepared

    def classify(self, f: Features) -> RegimeState:
        return self._prepared[f.symbol].regimes[f.ts_ms]


class _CandidateStrategy:
    """The family, presented to the backtester as a live strategy.

    Everything candidate-specific about *entry* happens here: the entry-quality
    floor, the regime filter, and the translation of the family's opinion into
    the ``Signal`` the ranker and the risk engine consume.  Target and stop bps
    come from the candidate's exit/stop profiles, because the ranker costs a
    signal against its own declared target and a signal that lies about its
    target would be ranked on a number nothing ever aims at.
    """

    def __init__(
        self,
        candidate: Candidate,
        signals: dict[int, SignalPoint],
        prepared: PreparedWindow,
        levels: "_Levels",
    ) -> None:
        self.name = candidate.family
        self.allowed_regimes = frozenset()
        self.min_score = 0.0
        self._c = candidate
        self._signals = signals
        self._prep = prepared
        self._levels = levels
        self._floor = candidate.min_score
        self._regime = candidate.regime.value if candidate.regime is not None else None

    def evaluate(self, f: Features, regime: RegimeState) -> Signal | None:
        point = self._signals.get(f.ts_ms)
        if point is None or point.score < self._floor:
            return None
        if self._regime is not None:
            label = self._prep.regime7.get(f.ts_ms) or regime.regime.value
            if label != self._regime:
                return None
        target_bps, stop_bps = self._levels.entry_bps(point.side, f)
        if target_bps <= 0 or stop_bps <= 0:
            return None
        return Signal(
            strategy=self.name,
            symbol=f.symbol,
            side=point.side,
            score=float(point.score),
            target_bps=float(target_bps),
            stop_bps=float(stop_bps),
            ts_ms=f.ts_ms,
            horizon_seconds=int(self._c.horizon_seconds),
            reasons=list(point.reasons),
        )


class _Levels:
    """Turns an exit/stop profile into bps, given the features at entry.

    The horizon scaling is diffusion scaling: a 1-minute ATR of 2.2 bps implies
    a typical 60-minute move of ``2.2 * sqrt(60) ~= 17`` bps, which is exactly
    the number ``docs/ECONOMICS.md`` measured.  Scaling an ATR-based target by
    the square root of the holding time is therefore the only version of
    "ATR-scaled" that means anything across a 2-minute and a 120-minute arm.
    """

    #: Hard floors, mirroring app.strategies.base — a degenerate ATR must never
    #: produce a zero-width stop.
    MIN_STOP_BPS = 3.0
    MIN_TARGET_BPS = 4.0

    def __init__(self, candidate: Candidate, *, emergency_stop_bps: float) -> None:
        self.exit = candidate.exit
        self.stop = candidate.stop
        self.minutes = max(candidate.horizon_minutes, 1.0)
        self.scale = math.sqrt(self.minutes)
        self.emergency_bps = max(emergency_stop_bps, self.MIN_STOP_BPS)

    # -- exits -------------------------------------------------------------- #

    def target_bps(self, side: Side, f: Features, *, round_trip_bps: float = 0.0) -> float:
        kind, value = self.exit.kind, self.exit.value
        atr = max(f.atr_bps, 0.0) * self.scale
        if kind == "net_fixed":
            # The NET target plus the round trip it has to clear.  When the
            # NetTargetEngine is in play it solves this properly; this is the
            # fallback path and the number the ranker is shown.
            return max(value + max(round_trip_bps, 0.0), self.MIN_TARGET_BPS)
        if kind == "atr":
            return max(value * atr, self.MIN_TARGET_BPS)
        if kind == "trail":
            # Deliberately out of reach: the trailing stop is the exit, and the
            # target only exists so ``trailing_arm_frac`` has a span to measure.
            return max(3.0 * atr, self.MIN_TARGET_BPS)
        if kind == "structure":
            frac = (1.0 - f.range_pos) if side is Side.LONG else f.range_pos
            dist = max(0.0, frac) * max(f.bb_width_bps, 0.0)
            return max(dist, max(atr, self.MIN_TARGET_BPS))
        # decay
        return max(2.0 * atr, self.MIN_TARGET_BPS)

    def net_target_pct(self) -> float | None:
        """The NET percentage for ``net_fixed``; ``None`` for every other kind."""
        if self.exit.kind != "net_fixed":
            return None
        return self.exit.value / 100.0

    # -- stops -------------------------------------------------------------- #

    def stop_bps(self, side: Side, f: Features) -> float:
        kind, value = self.stop.kind, self.stop.value
        atr = max(f.atr_bps, 0.0) * self.scale
        if kind == "fixed":
            out = value
        elif kind == "atr":
            out = value * atr
        elif kind == "vol":
            out = value * max(f.realized_vol_bps, 0.0) * self.scale
        elif kind == "structure":
            frac = f.range_pos if side is Side.LONG else (1.0 - f.range_pos)
            out = max(0.0, frac) * max(f.bb_width_bps, 0.0)
            if out <= 0:
                out = atr
        elif kind == "time":
            # No meaningful price stop: the clock is the stop.  The emergency
            # cap still applies because an unstopped position is not a strategy.
            out = self.emergency_bps
        else:  # reversal
            out = max(3.0 * atr, self.emergency_bps * 0.5)
        return min(max(out, self.MIN_STOP_BPS), self.emergency_bps)

    def entry_bps(self, side: Side, f: Features) -> tuple[float, float]:
        return self.target_bps(side, f), self.stop_bps(side, f)


class _ProfileExits:
    """The real ``ExitEngine``, told which level model this candidate means.

    Delegation, not replacement.  ``check`` — the stop-before-target ordering,
    the trailing give-back, the checkpoint verdicts and the time stop — is the
    production implementation, unmodified.  This wrapper does three things:

    * chooses whether the NET target engine or the profile's own level model
      produces the entry-time levels;
    * hands ``check`` the **features at exit time**.  The bar harness passes
      ``None`` there, which makes ``_momentum_fading`` unconditionally true and
      would silently disable every signal-decay exit;
    * computes the **signal-reversal** flag, which the harness has no way to
      pass and which the ``reversal`` stop profile is entirely made of.
    """

    def __init__(
        self,
        inner: Any,
        *,
        candidate: Candidate,
        levels: _Levels,
        prepared: PreparedWindow,
        signals: dict[int, SignalPoint],
    ) -> None:
        self._inner = inner
        self._c = candidate
        self._levels = levels
        self._prep = prepared
        self._signals = signals
        self._use_net = levels.net_target_pct() is not None

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)

    # -- entry-time levels -------------------------------------------------- #

    def net_levels(self, side, entry_price, f, signal, **kw):  # type: ignore[no-untyped-def]
        if not self._use_net:
            # Signals the harness to fall through to ``initial_levels``: it only
            # accepts the NetTargets when both prices are positive.
            return _NO_TARGETS
        kw = dict(kw)
        kw.setdefault("stop_price", None)
        if kw["stop_price"] is None:
            kw["stop_price"] = self._stop_price(side, entry_price, f, kw.get("info"))
        return self._inner.net_levels(side, entry_price, f, signal, **kw)

    def initial_levels(self, side, entry_price, f, signal, *, round_trip_cost_bps, info):  # type: ignore[no-untyped-def]
        tp_bps = self._levels.target_bps(side, f, round_trip_bps=round_trip_cost_bps)
        sl_bps = self._levels.stop_bps(side, f)
        if side is Side.LONG:
            tp = entry_price * (1.0 + tp_bps / _BPS)
            sl = entry_price * (1.0 - sl_bps / _BPS)
        else:
            tp = entry_price * (1.0 - tp_bps / _BPS)
            sl = entry_price * (1.0 + sl_bps / _BPS)
        return _round_levels(side, entry_price, tp, sl, info)

    def _stop_price(self, side, entry_price, f, info) -> float:  # type: ignore[no-untyped-def]
        sl_bps = self._levels.stop_bps(side, f)
        if side is Side.LONG:
            sl = entry_price * (1.0 - sl_bps / _BPS)
        else:
            sl = entry_price * (1.0 + sl_bps / _BPS)
        return info.round_price(sl) if info is not None else sl

    # -- live checks -------------------------------------------------------- #

    def check(self, pos, book, f, ts_ms, **kw):  # type: ignore[no-untyped-def]
        if f is None:
            f = self._prep.features.get(ts_ms)
        point = self._signals.get(ts_ms)
        if point is not None:
            kw.setdefault("signal_strength", float(point.score))
            if self._c.stop.kind == "reversal" and point.side is not pos.side:
                kw["reversal"] = True
        return self._inner.check(pos, book, f, ts_ms, **kw)


class _NoTargets:
    """A ``NetTargets``-shaped refusal: both prices zero, so the harness falls
    back to ``initial_levels`` without an exception and without a note."""

    net_tp_price = 0.0
    stop_price = 0.0


_NO_TARGETS = _NoTargets()


def _round_levels(side: Side, entry_price: float, tp: float, sl: float, info: SymbolInfo):
    """Tick-round, then guarantee the levels still straddle the entry."""
    tick = info.tick_size if info is not None and info.tick_size > 0 else 0.0
    tp_r = info.round_price(tp) if info is not None else tp
    sl_r = info.round_price(sl) if info is not None else sl
    bump = tick or entry_price * 1e-4
    if side is Side.LONG:
        if tp_r <= entry_price:
            tp_r = entry_price + bump
        if sl_r >= entry_price:
            sl_r = entry_price - bump
    else:
        if tp_r >= entry_price:
            tp_r = entry_price - bump
        if sl_r <= entry_price:
            sl_r = entry_price + bump
    return tp_r, sl_r


# --------------------------------------------------------------------------- #
# Per-candidate settings
# --------------------------------------------------------------------------- #


def _backtest_config(cfg: TournamentConfig) -> BacktestConfig:
    return BacktestConfig(
        starting_equity=cfg.starting_equity,
        fee_bps_per_side=cfg.fee_bps_per_side,
        spread_bps=cfg.spread_bps,
        slippage_bps=cfg.slippage_bps,
        latency_bars=cfg.latency_bars,
        risk_per_trade_pct=cfg.risk_per_trade_pct,
        max_concurrent=cfg.max_concurrent,
        seed=cfg.seed,
    )


def base_settings(cfg: TournamentConfig, base: Settings | None = None) -> Settings:
    s = backtest_settings(_backtest_config(cfg), base or get_settings())
    if cfg.min_net_edge_bps is not None:
        s = replace(s, risk=replace(s.risk, min_net_edge_bps=cfg.min_net_edge_bps))
    return s


def settings_for(candidate: Candidate, base: Settings) -> Settings:
    """``Settings`` that make this candidate's profiles actually bind.

    The horizon becomes the time stop (with the checkpoint at its midpoint and
    extension disabled — an extension would make the horizon axis meaningless),
    the exit profile chooses the TP model, and the stop profile the SL model.
    """
    ex = candidate.exit
    st = candidate.stop
    horizon_s = int(candidate.horizon_seconds)

    tp_model = "atr"
    tp_atr_mult = base.exits.tp_atr_mult
    trailing_enabled = True
    trailing_arm = base.exits.trailing_arm_frac
    trailing_give_back = base.exits.trailing_give_back_frac
    stale_frac = base.exits.stale_exit_frac

    if ex.kind == "atr":
        tp_atr_mult = ex.value * math.sqrt(max(candidate.horizon_minutes, 1.0))
    elif ex.kind == "trail":
        trailing_arm = 0.15
        trailing_give_back = ex.value
        tp_atr_mult = 3.0 * math.sqrt(max(candidate.horizon_minutes, 1.0))
    elif ex.kind == "decay":
        # Let the staleness rule and the checkpoint do the work.
        stale_frac = 0.6
        tp_atr_mult = 2.0 * math.sqrt(max(candidate.horizon_minutes, 1.0))

    sl_model = {"fixed": "fixed_pct", "structure": "structure"}.get(st.kind, "atr")

    exits = replace(
        base.exits,
        tp_model=tp_model,
        tp_atr_mult=tp_atr_mult,
        sl_model=sl_model,
        trailing_enabled=trailing_enabled,
        trailing_arm_frac=trailing_arm,
        trailing_give_back_frac=trailing_give_back,
        time_stop_seconds=horizon_s,
        stale_exit_frac=stale_frac,
    )
    horizon = replace(
        base.horizon,
        checkpoint_seconds=max(60, horizon_s // 2),
        time_stop_seconds=horizon_s,
        allow_extension=False,
    )
    out = replace(base, exits=exits, horizon=horizon)

    net_pct = _Levels(candidate, emergency_stop_bps=base.exits.emergency_stop_pct * 100.0
                      ).net_target_pct()
    if net_pct is not None:
        out = replace(out, profit=replace(base.profit, profile="base", base_pct=net_pct))
    return out


def components_for(
    candidate: Candidate,
    prepared: PreparedWindow,
    signals: dict[int, SignalPoint],
    *,
    cfg: TournamentConfig,
    settings: Settings,
) -> Components:
    """The live stack, with the four tournament adapters spliced in."""
    from app.exits import ExitEngine
    from app.ranker import OpportunityRanker
    from app.risk import RiskEngine, RiskState
    from app.sim.ledger import Ledger
    from app.sim.simulator import PaperSimulator

    levels = _Levels(candidate, emergency_stop_bps=settings.exits.emergency_stop_pct * 100.0)
    by_symbol = {prepared.symbol: prepared}
    return Components(
        features=_CachedFeatures(by_symbol),
        regime=_CachedRegime(by_symbol),
        strategies=[_CandidateStrategy(candidate, signals, prepared, levels)],
        ranker=OpportunityRanker(settings=settings),
        risk=RiskEngine(settings=settings),
        exits=_ProfileExits(
            ExitEngine(settings=settings),
            candidate=candidate,
            levels=levels,
            prepared=prepared,
            signals=signals,
        ),
        # A fresh RNG per candidate: a simulator shared across candidates would
        # carry its RNG state from one arm into the next and make the result
        # depend on evaluation order.
        simulator=PaperSimulator(settings=settings, rng=Random(cfg.seed)),
        ledger_factory=Ledger,
        risk_state_cls=RiskState,
    )


# --------------------------------------------------------------------------- #
# Candidate enumeration
# --------------------------------------------------------------------------- #


def enumerate_candidates(
    cfg: TournamentConfig,
    *,
    sources: Sequence[SignalSource] | None = None,
    params_by_family: dict[str, list[dict]] | None = None,
) -> list[Candidate]:
    """The declared grid, in a deterministic order, deduplicated by ``key()``.

    The full Cartesian product of every axis is tens of millions of points and
    is not a search anyone can run.  What is enumerated instead is deliberate
    and is exactly this, in this order:

    1. the **baseline** candidate for every ``(family, symbol, params)``;
    2. **one-factor-at-a-time sweeps** — for each of the four searched axes
       (horizon, exit profile, stop profile, entry quality) every value of that
       axis with the other three at baseline, plus the regime axis.  These are
       what produce the per-axis curves the report prints: a curve is only a
       curve if exactly one thing varies along it;
    3. a **seeded random sample of the full product**, which is what catches
       interactions the one-factor sweeps cannot see, sized to fill whatever is
       left of ``max_candidates``.

    So the count is ``|params| x |symbols| x |families|`` baselines, plus the
    OFAT sweeps, plus the interaction sample — never the full product, and the
    report says so.
    """
    if sources is None:
        sources = load_sources(cfg.families)
    if params_by_family is None:
        params_by_family = {
            s.name: s.param_combinations(cfg.max_params_per_family, cfg.seed)
            for s in sources
        }

    base_h = int(cfg.baseline_horizon_minutes) * 60
    base_x = cfg.baseline_exit_profile
    base_s = cfg.baseline_stop_profile
    base_q = cfg.baseline_min_score

    out: list[Candidate] = []
    seen: set[str] = set()

    def add(c: Candidate) -> None:
        k = c.key()
        if k not in seen:
            seen.add(k)
            out.append(c)

    def make(family: str, symbol: str, params: dict, *, h: int, x: str, s: str,
             q: float, r: Regime7 | None) -> Candidate:
        merged = dict(params)
        merged["min_score"] = float(q)
        return Candidate(
            family=family, symbol=symbol, params=merged, horizon_seconds=h,
            exit_profile=x, stop_profile=s, regime=r,
        )

    # Per group first, then interleaved.  ``max_candidates`` truncates a prefix,
    # and a prefix of a family-major ordering would be "every arm of the first
    # three families and none of the other seven" — a budget cut that silently
    # changes which question is being asked.  Round-robin makes the cut
    # proportional instead: every family, symbol and axis thins together.
    groups: list[list[Candidate]] = []
    for src in sources:
        for symbol in cfg.symbols:
            for params in params_by_family.get(src.name, [{}]):
                common = dict(family=src.name, symbol=symbol, params=params)
                group = [make(**common, h=base_h, x=base_x, s=base_s, q=base_q, r=None)]
                for h in cfg.horizons_minutes:
                    group.append(make(**common, h=int(h) * 60, x=base_x, s=base_s,
                                      q=base_q, r=None))
                for x in cfg.exit_profiles:
                    group.append(make(**common, h=base_h, x=x, s=base_s, q=base_q, r=None))
                for s in cfg.stop_profiles:
                    group.append(make(**common, h=base_h, x=base_x, s=s, q=base_q, r=None))
                for q in cfg.min_scores:
                    group.append(make(**common, h=base_h, x=base_x, s=base_s, q=q, r=None))
                for r in cfg.regimes:
                    group.append(make(**common, h=base_h, x=base_x, s=base_s,
                                      q=base_q, r=r))
                groups.append(group)

    for column in range(max((len(g) for g in groups), default=0)):
        for group in groups:
            if column < len(group):
                add(group[column])

    ofat = len(out)
    budget = max(cfg.max_candidates - ofat, 0)
    # The interaction sample never eats the whole remaining budget: the OFAT
    # sweeps are what the per-axis curves are made of, and a truncation that
    # lands mid-sweep would print a curve with holes in it.
    extra = min(budget, int(cfg.max_candidates * max(0.0, min(1.0, cfg.random_extra_frac))))

    if extra > 0:
        rng = Random(cfg.seed)
        # The one-factor sweeps already cover every horizon, 2 minutes
        # included, so the negative result there is reported.  The interaction
        # sample is budget, and budget does not go to horizons whose measured
        # median move is smaller than the round trip it has to pay: on BTC the
        # 2-minute median is 0.9 bps against a ~3 bps cost floor, and on ETH and
        # SOL it is 0.0.  Optimising around a horizon that cannot clear its own
        # costs is not a search, it is a way of spending an afternoon.
        viable = [
            h for h in cfg.horizons_minutes
            if any(median_move_bps(sym, h) > cfg.round_trip_bps for sym in cfg.symbols)
        ] or list(cfg.horizons_minutes)
        family_symbol_params = [
            (s.name, sym, p)
            for s in sources
            for sym in cfg.symbols
            for p in params_by_family.get(s.name, [{}])
        ]
        if family_symbol_params:
            attempts = 0
            while len(out) - ofat < extra and attempts < extra * 20:
                attempts += 1
                family, symbol, params = rng.choice(family_symbol_params)
                add(make(
                    family=family, symbol=symbol, params=params,
                    h=int(rng.choice(viable)) * 60,
                    x=rng.choice(cfg.exit_profiles),
                    s=rng.choice(cfg.stop_profiles),
                    q=float(rng.choice(cfg.min_scores)),
                    r=rng.choice(cfg.regimes),
                ))

    if len(out) > cfg.max_candidates:
        out = out[: cfg.max_candidates]
    return out


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


@dataclass
class _WindowOutcome:
    """One candidate's numbers on one walk-forward window.  Crosses processes."""

    key: str
    scheme: str
    window_index: int
    in_sample: PerfStats
    out_of_sample: PerfStats
    fees_usd: float = 0.0
    slippage_usd: float = 0.0
    per_regime: dict[str, PerfStats] = field(default_factory=dict)
    exit_reasons: dict[str, int] = field(default_factory=dict)
    best_trade_usd: float = 0.0
    worst_trade_usd: float = 0.0
    trades: list[ClosedTrade] = field(default_factory=list)
    abandoned: str | None = None


def stats_from_trades(trades: Sequence[ClosedTrade]) -> PerfStats:
    """``PerfStats`` for an arbitrary subset of trades.

    Mirrors ``Ledger.stats`` exactly, except that drawdown is measured on the
    cumulative net P&L of the subset — a regime bucket has no equity curve of
    its own, and inventing one would be worse than measuring the thing we do
    have.
    """
    s = PerfStats()
    trades = list(trades)
    if not trades:
        return s
    wins = [t for t in trades if t.net_pnl_usd > 0]
    losses = [t for t in trades if t.net_pnl_usd < 0]
    s.trades = len(trades)
    s.wins = len(wins)
    s.losses = len(losses)
    s.gross_pnl = sum(t.gross_pnl_usd for t in trades)
    s.fees = sum(t.fees_usd for t in trades)
    s.slippage = sum(t.slippage_usd for t in trades)
    s.net_pnl = sum(t.net_pnl_usd for t in trades)
    gross_win = sum(t.net_pnl_usd for t in wins)
    gross_loss = abs(sum(t.net_pnl_usd for t in losses))
    s.avg_win = gross_win / len(wins) if wins else 0.0
    s.avg_loss = -(gross_loss / len(losses)) if losses else 0.0
    s.profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_win > 0 else 0.0
    )
    s.expectancy = s.net_pnl / s.trades
    s.avg_holding_seconds = sum(t.holding_seconds for t in trades) / s.trades
    peak = 0.0
    running = 0.0
    worst = 0.0
    for t in sorted(trades, key=lambda t: t.exit_ms):
        running += t.net_pnl_usd
        peak = max(peak, running)
        worst = max(worst, peak - running)
    s.max_drawdown = worst
    return s


class _WindowEvaluator:
    """Evaluates many candidates against one prepared train/test pair."""

    def __init__(
        self,
        symbol: str,
        scheme: WalkForwardScheme,
        window_index: int,
        train_bars: list[Bar],
        test_bars: list[Bar],
        *,
        cfg: TournamentConfig,
        sources: Sequence[SignalSource],
    ) -> None:
        self.symbol = symbol
        self.scheme = scheme
        self.window_index = window_index
        self.cfg = cfg
        self.settings = base_settings(cfg)
        self.sources = {s.name: s for s in sources}
        self.train = prepare_window(
            symbol, train_bars, settings=self.settings,
            spread_bps=cfg.spread_bps, seed=cfg.seed,
            tape_cache_dir=cfg.tape_cache_dir,
        )
        self.test = prepare_window(
            symbol, test_bars, settings=self.settings,
            spread_bps=cfg.spread_bps, seed=cfg.seed,
            tape_cache_dir=cfg.tape_cache_dir,
        )
        self._signal_cache: dict[tuple[str, str, int], dict[int, SignalPoint]] = {}

    def _signals(self, family: str, params: dict, prepared: PreparedWindow):
        pkey = ",".join(f"{k}={_fmt_param(params[k])}" for k in sorted(params)
                        if k != "min_score")
        ck = (family, pkey, id(prepared))
        hit = self._signal_cache.get(ck)
        if hit is None:
            source = self.sources[family]
            stripped = {k: v for k, v in params.items() if k != "min_score"}
            hit = source.signals(prepared, stripped)
            self._signal_cache[ck] = hit
        return hit

    def evaluate(self, candidate: Candidate) -> _WindowOutcome:
        settings = settings_for(candidate, self.settings)
        bt_cfg = _backtest_config(self.cfg)

        in_res = self._run(candidate, self.train, settings, bt_cfg, "IN")
        out_res = self._run(candidate, self.test, settings, bt_cfg, "OUT")

        trades = list(out_res.trades)
        per_regime: dict[str, PerfStats] = {}
        buckets: dict[str, list[ClosedTrade]] = {}
        for t in trades:
            label = self.test.regime_at_entry.get(t.entry_ms) or t.regime or "UNKNOWN"
            buckets.setdefault(label, []).append(t)
        for label, group in buckets.items():
            per_regime[label] = stats_from_trades(group)

        reasons: dict[str, int] = {}
        for t in trades:
            k = t.exit_reason.value if isinstance(t.exit_reason, ExitReason) else str(
                t.exit_reason)
            reasons[k] = reasons.get(k, 0) + 1

        nets = [t.net_pnl_usd for t in trades]
        return _WindowOutcome(
            key=candidate.key(),
            scheme=self.scheme.label,
            window_index=self.window_index,
            in_sample=in_res.stats,
            out_of_sample=out_res.stats,
            fees_usd=out_res.stats.fees,
            slippage_usd=out_res.stats.slippage,
            per_regime=per_regime,
            exit_reasons=reasons,
            best_trade_usd=max(nets) if nets else 0.0,
            worst_trade_usd=min(nets) if nets else 0.0,
            trades=trades,
        )

    def _run(self, candidate, prepared, settings, bt_cfg, tag):  # type: ignore[no-untyped-def]
        signals = self._signals(candidate.family, candidate.params, prepared)
        components = components_for(
            candidate, prepared, signals, cfg=self.cfg, settings=settings
        )
        return run_backtest(
            {prepared.symbol: prepared.bars},
            bt_cfg,
            label=f"{candidate.family}-{self.scheme.label}-W{self.window_index}-{tag}",
            components=components,
            settings=settings,
        )

    def warm_signals(self, candidates: Sequence[Candidate]) -> None:
        """Compute every family/params signal set, then drop the MTF block.

        The MTF feature block is by far the largest thing a prepared window
        holds — 124 floats per minute per window — and once the signals are
        extracted nothing reads it again.  Dropping it here is what keeps four
        parallel workers inside a laptop's memory.
        """
        for c in candidates:
            self._signals(c.family, c.params, self.train)
            self._signals(c.family, c.params, self.test)
        self.train.mtf = {}
        self.test.mtf = {}

    def signal_count(self, candidate: Candidate, prepared: PreparedWindow) -> int:
        pts = self._signals(candidate.family, candidate.params, prepared)
        floor = candidate.min_score
        return sum(1 for p in pts.values() if p.score >= floor)


# --------------------------------------------------------------------------- #
# Parallel driver
# --------------------------------------------------------------------------- #


@dataclass
class _Task:
    symbol: str
    scheme: WalkForwardScheme
    window: WalkForwardWindow
    train_bars: list[Bar]
    test_bars: list[Bar]
    candidates: list[Candidate]
    cfg: TournamentConfig
    family_names: tuple[str, ...]
    keep_trades: bool


def _run_task(task: _Task) -> list[_WindowOutcome]:
    """Process-pool entry point.  Must be module level so it pickles."""
    sources = load_sources(task.family_names)
    ev = _WindowEvaluator(
        task.symbol, task.scheme, task.window.index,
        task.train_bars, task.test_bars,
        cfg=task.cfg, sources=sources,
    )
    ev.warm_signals(task.candidates)
    out: list[_WindowOutcome] = []
    for c in task.candidates:
        try:
            outcome = ev.evaluate(c)
        except Exception as exc:  # pragma: no cover - defensive
            outcome = _WindowOutcome(
                key=c.key(), scheme=task.scheme.label, window_index=task.window.index,
                in_sample=PerfStats(), out_of_sample=PerfStats(),
                abandoned=f"error: {exc!r}",
            )
        # Trades are the bulk of an outcome, so they ride the process-pool pipe
        # only when the search asked for them at all.
        #
        # What used to be here, and must never come back:
        #
        #     if not task.keep_trades or outcome.out_of_sample.net_pnl <= 0:
        #         outcome.trades = []
        #
        # That second clause dropped the trades of every *losing* window while
        # keeping the winners, so an arm's retained trade list was a
        # survivorship-filtered subset of the out-of-sample trades its
        # statistics were computed from.  It is what produced the Phase-3 "7/7
        # PASS" finalist: 45 trades netting +$14.72 kept out of 79 netting
        # +$5.51 (the 7 losing windows held the other 34, netting −$9.21), so
        # the cost stresses degraded a base 2.6x larger than the headline and
        # "fees +25%" printed a *higher* net than the unstressed result.
        #
        # The retention decision belongs one level up, in ``_merge``, where the
        # whole arm is visible and the answer can be all-or-nothing: a trade
        # list that is a biased subset of the result it is filed under is worse
        # than no trade list at all.
        if not task.keep_trades:
            outcome.trades = []
        out.append(outcome)
    return out


def _bars_of(dataset: Any) -> list[Bar]:
    """Accept a research ``Dataset``, a bar list, or a ``{interval: bars}`` dict."""
    if isinstance(dataset, list):
        return list(dataset)
    bars = getattr(dataset, "bars", None)
    if isinstance(bars, dict):
        return list(bars.get("1m") or next(iter(bars.values()), []))
    if isinstance(bars, list):
        return list(bars)
    if isinstance(dataset, dict):
        return list(dataset.get("1m") or [])
    raise TypeError(f"cannot read bars from {type(dataset).__name__}")


def run_tournament(
    datasets: dict[str, Any],
    *,
    config: TournamentConfig,
    on_progress: Callable[[str], None] | None = None,
) -> list[ArmResult]:
    """Search the candidate space and return **every** arm evaluated.

    The schedule is two passes.  Pass 1 screens every sampled candidate on the
    cheapest walk-forward scheme.  Pass 2 re-runs, on the remaining schemes,
    only the candidates whose **in-sample** expectancy cleared
    ``screen_min_in_sample_expectancy`` — selection on the train half, never on
    the test half, because a screen that reads the test number has spent it.
    """
    cfg = config
    started = time.time()
    say = on_progress or (lambda _m: None)

    bars_by_symbol = {
        sym: sorted(_bars_of(d), key=lambda b: b.open_ms)
        for sym, d in datasets.items()
        if sym in cfg.symbols
    }
    bars_by_symbol = {s: b for s, b in bars_by_symbol.items() if b}
    if not bars_by_symbol:
        return []

    sources = load_sources(cfg.families)
    params_by_family = {
        s.name: s.param_combinations(cfg.max_params_per_family, cfg.seed) for s in sources
    }
    candidates = enumerate_candidates(
        replace(cfg, symbols=tuple(bars_by_symbol)),
        sources=sources,
        params_by_family=params_by_family,
    )
    say(f"enumerated {len(candidates)} candidates")

    supply, supply_notes = _signal_supply(bars_by_symbol, cfg, sources, say)

    arms: dict[str, ArmResult] = {}
    for c in candidates:
        arms[c.key()] = ArmResult(
            candidate=c, trades=[], stats=PerfStats(), fees_usd=0.0, slippage_usd=0.0,
            windows=[], in_sample=PerfStats(), out_of_sample=PerfStats(),
            signals_available=_supply_for(supply, c),
        )

    # --- the free gate: not enough signals to ever reach min_trades --------- #
    live: list[Candidate] = []
    for c in candidates:
        arm = arms[c.key()]
        if supply and arm.signals_available < cfg.min_trades:
            arm.abandoned = (
                f"signal supply {arm.signals_available} < min_trades {cfg.min_trades}"
            )
            continue
        live.append(c)
    say(f"{len(live)} candidates survive the signal-supply gate")

    schemes = cfg.scheme_order()
    if not schemes:
        return list(arms.values())

    evaluated_keys: set[str] = set()
    screen = schemes[min(cfg.screen_scheme_index, len(schemes) - 1)]
    outcomes = _evaluate(live, bars_by_symbol, screen, cfg, sources, arms, say)
    _merge(arms, outcomes, cfg)
    evaluated_keys.update(o.key for o in outcomes)

    # --- pass 2: only what looked good ON TRAIN pays for the long schemes --- #
    survivors = [
        c for c in live
        if arms[c.key()].in_sample.trades > 0
        and arms[c.key()].in_sample.expectancy > cfg.screen_min_in_sample_expectancy
    ]
    say(f"{len(survivors)} candidates cleared the in-sample screen")
    for scheme in schemes:
        if scheme.label == screen.label or not survivors:
            continue
        outcomes = _evaluate(survivors, bars_by_symbol, scheme, cfg, sources, arms, say)
        _merge(arms, outcomes, cfg)

    results = list(arms.values())
    for arm in results:
        arm.notes.extend(supply_notes.get(arm.candidate.symbol, []))
    elapsed = time.time() - started
    say(f"tournament finished in {elapsed:.1f}s")
    results.sort(key=lambda a: (-a.out_of_sample.expectancy, a.key))
    return results


def _supply_for(supply: dict[tuple[str, str, str], list[float]], c: Candidate) -> int:
    pkey = ",".join(f"{k}={_fmt_param(c.params[k])}" for k in sorted(c.params)
                    if k != "min_score")
    scores = supply.get((c.symbol, c.family, pkey))
    if scores is None:
        return 0
    floor = c.min_score
    return sum(1 for s in scores if s >= floor)


def _signal_supply(
    bars_by_symbol: dict[str, list[Bar]],
    cfg: TournamentConfig,
    sources: Sequence[SignalSource],
    say: Callable[[str], None],
) -> tuple[dict[tuple[str, str, str], list[float]], dict[str, list[str]]]:
    """Signal scores per ``(symbol, family, params)`` over the whole history.

    This is the cheap gate, and it is a genuine **upper bound**: a per-window
    replay starts its feature engine cold, so it can only ever produce fewer
    signals than a single warm pass over the whole history.  Abandoning a
    candidate whose upper bound is below ``min_trades`` therefore cannot change
    the result of a candidate that would have qualified — the thing the tests
    assert.
    """
    supply: dict[tuple[str, str, str], list[float]] = {}
    notes: dict[str, list[str]] = {}
    settings = base_settings(cfg)
    research = [s for s in sources if isinstance(s, ResearchFamilySource)]
    live = [s for s in sources if not isinstance(s, ResearchFamilySource)]

    for symbol, bars in bars_by_symbol.items():
        say(f"counting signal supply for {symbol} ({len(bars)} bars)")
        combos = {
            src.name: src.param_combinations(cfg.max_params_per_family, cfg.seed)
            for src in sources
        }
        for src in sources:
            for params in combos[src.name]:
                pkey = ",".join(f"{k}={_fmt_param(params[k])}" for k in sorted(params))
                supply.setdefault((symbol, src.name, pkey), [])

        seen_mtf = 0
        for mf in stream_mtf(symbol, bars, settings, cfg.tape_cache_dir):
            seen_mtf += 1
            for src in research:
                for params in combos[src.name]:
                    sig = src._family.signal(mf, dict(params))
                    if sig is None:
                        continue
                    pkey = ",".join(
                        f"{k}={_fmt_param(params[k])}" for k in sorted(params)
                    )
                    supply[(symbol, src.name, pkey)].append(float(sig.score))
        if research and not seen_mtf:
            notes.setdefault(symbol, []).append(
                "MTF features unavailable — research families saw no input"
            )

        if live:
            prepared = prepare_window(
                symbol, bars, settings=settings, spread_bps=cfg.spread_bps,
                seed=cfg.seed, want_mtf=False,
            )
            for src in live:
                for params in combos[src.name]:
                    pkey = ",".join(
                        f"{k}={_fmt_param(params[k])}" for k in sorted(params)
                    )
                    pts = src.signals(prepared, params)
                    supply[(symbol, src.name, pkey)] = [p.score for p in pts.values()]
            del prepared

        for key in list(supply):
            if key[0] == symbol:
                supply[key].sort(reverse=True)
    return supply, notes


def _evaluate(
    candidates: Sequence[Candidate],
    bars_by_symbol: dict[str, list[Bar]],
    scheme: WalkForwardScheme,
    cfg: TournamentConfig,
    sources: Sequence[SignalSource],
    arms: dict[str, ArmResult],
    say: Callable[[str], None],
) -> list[_WindowOutcome]:
    by_symbol: dict[str, list[Candidate]] = {}
    for c in candidates:
        by_symbol.setdefault(c.symbol, []).append(c)

    keep = bool(cfg.keep_trades_for_finalists)

    tasks: list[_Task] = []
    family_names = tuple(s.name for s in sources)
    for symbol, group in by_symbol.items():
        bars = bars_by_symbol[symbol]
        start = bars[0].open_ms
        end = bars[-1].close_ms
        windows = plan_windows(
            start, end + 1, train_days=scheme.train_days, test_days=scheme.test_days
        )
        for w in windows:
            train = slice_bars({symbol: bars}, w.train_start_ms, w.train_end_ms).get(symbol)
            test = slice_bars({symbol: bars}, w.test_start_ms, w.test_end_ms).get(symbol)
            if not train or not test:
                continue
            tasks.append(_Task(
                symbol=symbol, scheme=scheme, window=w,
                train_bars=train, test_bars=test, candidates=list(group),
                cfg=cfg, family_names=family_names, keep_trades=keep,
            ))

    if not tasks:
        say(f"scheme {scheme.label}: history too short — no windows")
        return []

    say(f"scheme {scheme.label}: {len(tasks)} windows x {len(candidates)} candidates")
    out: list[_WindowOutcome] = []
    jobs = max(1, int(cfg.jobs))
    if jobs == 1:
        for i, t in enumerate(tasks, 1):
            out.extend(_run_task(t))
            say(f"  window {i}/{len(tasks)} done")
    else:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            futures = {pool.submit(_run_task, t): t for t in tasks}
            done = 0
            for fut in as_completed(futures):
                out.extend(fut.result())
                done += 1
                say(f"  window {done}/{len(tasks)} done")
    # Deterministic merge order, independent of completion order.
    out.sort(key=lambda o: (o.key, o.scheme, o.window_index))
    return out


class TradeSetIntegrityError(AssertionError):
    """An arm's retained trades do not describe the arm's reported result.

    Raised at the *producing* end, loudly and immediately, because by the time
    ``robustness.py`` sees it the only honest thing left to do is refuse to
    gate — and a search that has already spent two hours deserves to fail on
    the window that broke it rather than in the report.
    """


#: Reconciliation tolerance in USD.  Both sides sum the same per-trade nets,
#: so the only legitimate difference is float association order.
_TRADE_SET_ABS_TOLERANCE_USD = 1e-6
_TRADE_SET_REL_TOLERANCE = 1e-9


def trade_set_mismatch(arm: ArmResult) -> str:
    """Describe how an arm's retained trades fail to match its statistics.

    Empty string when they match — including the legitimate "no trades were
    retained for this arm" case, which is absence, not disagreement.
    """
    trades = arm.out_of_sample_trades
    if not trades:
        if arm.trade_windows_kept:
            return (
                f"no trades retained but trade_windows_kept={arm.trade_windows_kept} "
                "— the bookkeeping says there should be some"
            )
        return ""
    reported_n = int(arm.out_of_sample.trades)
    if len(trades) != reported_n:
        return (
            f"{len(trades)} retained out-of-sample trades against {reported_n} in "
            f"out_of_sample — retained net ${math.fsum(t.net_pnl_usd for t in trades):+.4f} "
            f"vs reported ${arm.out_of_sample.net_pnl:+.4f}"
        )
    if arm.trade_windows_kept != arm.windows_evaluated:
        return (
            f"trades were absorbed from {arm.trade_windows_kept} of "
            f"{arm.windows_evaluated} evaluated windows"
        )
    net = math.fsum(t.net_pnl_usd for t in trades)
    reported_net = float(arm.out_of_sample.net_pnl)
    scale = abs(reported_net) + math.fsum(abs(t.net_pnl_usd) for t in trades)
    tolerance = max(_TRADE_SET_ABS_TOLERANCE_USD, _TRADE_SET_REL_TOLERANCE * scale)
    if abs(net - reported_net) > tolerance:
        return (
            f"retained trades net ${net:+.4f} against a reported out-of-sample net "
            f"of ${reported_net:+.4f} (difference ${net - reported_net:+.4f})"
        )
    return ""


def _assert_trade_set_reconciles(arm: ArmResult) -> None:
    problem = trade_set_mismatch(arm)
    if problem:
        raise TradeSetIntegrityError(f"{arm.key}: {problem}")


def validate_trade_set(
    trades: Sequence[ClosedTrade],
    *,
    symbol: str | None = None,
    window: WalkForwardWindow | None = None,
    regime_at_entry: dict[int, str] | None = None,
) -> list[str]:
    """Structural validity of a trade list, as a list of violations.

    These are the neighbouring questions to the gate-reconciliation one: a
    trade list can have the right count and the right net and still be wrong
    about *which* trades they are.  Each check earns its place by naming a way
    a walk-forward search can leak or double-count:

    ``coin``
        Every trade belongs to the symbol its arm names.  A candidate never
        spans symbols, so a foreign trade is a mis-merge.
    ``window``
        Entry *and* exit fall inside the window the trade is attributed to.  An
        entry before ``test_start_ms`` is a trade taken on training data; an
        exit after ``test_end_ms`` is a result the window could not have known.
    ``duplicates``
        No two trades share ``(symbol, entry_ms, exit_ms)``.  Overlapping
        samples counted as independent observations inflate every aggregate
        and every gate that assumes independence — the Monte Carlo above all.
    ``regime``
        The regime on the trade is the regime at **entry**.  Attributing the
        exit regime would let a trade be filed under a state that only existed
        because the trade had already run.
    """
    problems: list[str] = []
    for t in trades:
        if symbol is not None and t.symbol != symbol:
            problems.append(f"{t.id}: symbol {t.symbol!r}, expected {symbol!r}")
        if window is not None:
            if not (window.test_start_ms <= t.entry_ms < window.test_end_ms):
                problems.append(
                    f"{t.id}: entry {t.entry_ms} outside test window "
                    f"[{window.test_start_ms}, {window.test_end_ms})"
                )
            if t.exit_ms >= window.test_end_ms:
                problems.append(
                    f"{t.id}: exit {t.exit_ms} straddles the end of the test "
                    f"window at {window.test_end_ms}"
                )
            if t.exit_ms < t.entry_ms:
                problems.append(f"{t.id}: exit {t.exit_ms} precedes entry {t.entry_ms}")
        if regime_at_entry is not None:
            expected = regime_at_entry.get(t.entry_ms)
            if expected is not None and t.regime != expected:
                problems.append(
                    f"{t.id}: regime {t.regime!r} is not the regime at entry "
                    f"({expected!r})"
                )

    seen: dict[tuple[str, int, int], int] = {}
    for t in trades:
        k = (t.symbol, t.entry_ms, t.exit_ms)
        seen[k] = seen.get(k, 0) + 1
    for k, n in seen.items():
        if n > 1:
            problems.append(
                f"{k[0]} {k[1]}->{k[2]} appears {n} times — the same round trip "
                "counted as independent observations"
            )
    return problems


def _merge(
    arms: dict[str, ArmResult], outcomes: Sequence[_WindowOutcome], cfg: TournamentConfig
) -> None:
    by_key: dict[str, list[_WindowOutcome]] = {}
    for o in outcomes:
        by_key.setdefault(o.key, []).append(o)

    kept = sum(1 for a in arms.values() if a.out_of_sample_trades)
    for key, group in sorted(by_key.items()):
        arm = arms.get(key)
        if arm is None:
            continue
        group = sorted(group, key=lambda o: (o.scheme, o.window_index))
        # ``windows`` is per-walk-forward-window OUT-OF-SAMPLE stats, and only
        # that: robustness gate 5 counts positive windows off this list, so an
        # in-sample or combined entry here would quietly pass a failing arm.
        prior = [arm.in_sample] if arm.windows_evaluated else []
        arm.windows.extend(o.out_of_sample for o in group)
        arm.in_sample = combine_stats(prior + [o.in_sample for o in group])
        arm.out_of_sample = combine_stats(
            ([arm.out_of_sample] if arm.windows_evaluated else [])
            + [o.out_of_sample for o in group]
        )
        arm.stats = arm.out_of_sample
        arm.fees_usd = arm.out_of_sample.fees
        arm.slippage_usd = arm.out_of_sample.slippage
        arm.windows_evaluated += len(group)
        arm.windows_positive += sum(1 for o in group if o.out_of_sample.net_pnl > 0)
        arm.best_trade_usd = max([arm.best_trade_usd] + [o.best_trade_usd for o in group])
        arm.worst_trade_usd = min([arm.worst_trade_usd] + [o.worst_trade_usd for o in group])
        for o in group:
            scheme_stats = arm.schemes.get(o.scheme)
            arm.schemes[o.scheme] = combine_stats(
                ([scheme_stats] if scheme_stats else []) + [o.out_of_sample]
            )
            for label, st in o.per_regime.items():
                prev = arm.per_regime.get(label)
                arm.per_regime[label] = combine_stats(
                    ([prev] if prev else []) + [st]
                )
            for reason, n in o.exit_reasons.items():
                arm.exit_reasons[reason] = arm.exit_reasons.get(reason, 0) + n

        # Trades are only kept for arms that could plausibly be finalists —
        # robustness.py needs them, and nothing else does.  Everything else
        # keeps statistics only, which is what makes a large search fit in RAM.
        #
        # The rule is **all or nothing**, and it has to be, because ``_merge``
        # is called once per walk-forward scheme: an arm that starts retaining
        # only at the second scheme would end up holding half its own history
        # and reporting gates against it.  Three things can break completeness,
        # and each one empties the list instead of shrinking it:
        #
        #   * the arm was not promising when an earlier group was merged, so
        #     those windows' trades were never absorbed (``contiguous``);
        #   * the global retention cap was already full when it first became
        #     eligible (``under_cap``);
        #   * it stopped being promising, in which case the list goes and the
        #     slot it held is returned to the cap.
        promising = (
            arm.out_of_sample.net_pnl > 0 and arm.out_of_sample.trades >= cfg.min_trades
        )
        already_kept = bool(arm.out_of_sample_trades)
        contiguous = arm.trade_windows_kept == arm.windows_evaluated - len(group)
        under_cap = already_kept or kept < cfg.max_kept_trade_arms
        retain = cfg.keep_trades_for_finalists and promising and contiguous and under_cap
        if retain:
            for o in group:
                arm.out_of_sample_trades.extend(o.trades)
            # ``id`` is a per-backtest-run counter and collides across windows,
            # so it cannot carry the tie-break on its own; entry time does, and
            # the pair is stable.
            arm.out_of_sample_trades.sort(key=lambda t: (t.exit_ms, t.entry_ms, t.id))
            arm.trades = arm.out_of_sample_trades
            arm.trade_windows_kept = arm.windows_evaluated
            if arm.out_of_sample_trades and not already_kept:
                kept += 1
        else:
            if already_kept:
                kept -= 1
            arm.out_of_sample_trades = []
            arm.trades = []
            arm.trade_windows_kept = 0

        _assert_trade_set_reconciles(arm)


# --------------------------------------------------------------------------- #
# Tables and the report
# --------------------------------------------------------------------------- #

WIDTH = 100


def _rule(ch: str = "-") -> str:
    return ch * WIDTH


def _title(text: str) -> str:
    return f"{_rule('=')}\n{text}\n{_rule('=')}"


def _pf(v: float) -> str:
    if v == float("inf"):
        return "  inf"
    return f"{v:5.2f}"


def _evaluated(results: Sequence[ArmResult]) -> list[ArmResult]:
    return [r for r in results if r.windows_evaluated > 0]


def _best(results: Sequence[ArmResult], key: Callable[[ArmResult], Any]) -> ArmResult | None:
    pool = [r for r in results if r.out_of_sample.trades > 0]
    return max(pool, key=key) if pool else None


def per_coin_table(results: Sequence[ArmResult]) -> str:
    """Out-of-sample, per symbol.  Only a subset of coins surviving is an
    expected outcome, so the table is built to make that easy to read."""
    rows = [
        _title("PER-COIN — OUT OF SAMPLE"),
        f"{'symbol':<8}{'arms':>6}{'trades':>8}{'net P&L':>11}{'PF':>7}"
        f"{'expect':>9}{'maxDD':>9}{'fees':>9}{'slip':>9}  {'best family / regime / horizon'}",
        _rule(),
    ]
    evaluated = _evaluated(results)
    if not evaluated:
        return "\n".join(rows + ["(nothing was evaluated)"])

    by_symbol: dict[str, list[ArmResult]] = {}
    for r in evaluated:
        by_symbol.setdefault(r.candidate.symbol, []).append(r)

    survivors = 0
    for symbol in sorted(by_symbol):
        group = by_symbol[symbol]
        pooled = combine_stats([r.out_of_sample for r in group])
        best = _best(group, lambda r: r.out_of_sample.expectancy)
        if best is not None and best.out_of_sample.expectancy > 0:
            survivors += 1
        tag = "-"
        if best is not None:
            c = best.candidate
            regime = c.regime.value if c.regime else "any"
            tag = f"{c.family} / {regime} / {c.horizon_seconds // 60}m"
        rows.append(
            f"{symbol:<8}{len(group):>6}{pooled.trades:>8}{pooled.net_pnl:>11.2f}"
            f"{_pf(pooled.profit_factor):>7}{pooled.expectancy:>9.4f}"
            f"{pooled.max_drawdown:>9.2f}{pooled.fees:>9.2f}{pooled.slippage:>9.2f}  {tag}"
        )
    rows.append(_rule())
    rows.append(
        f"{survivors} of {len(by_symbol)} symbols have at least one arm with positive "
        f"out-of-sample expectancy"
    )
    return "\n".join(rows)


def per_regime_table(results: Sequence[ArmResult]) -> str:
    rows = [
        _title("PER-REGIME — OUT OF SAMPLE"),
        f"{'regime':<24}{'trades':>8}{'net P&L':>11}{'PF':>7}{'win%':>8}"
        f"{'expect':>9}{'avg win':>10}{'avg loss':>10}",
        _rule(),
    ]
    pooled: dict[str, list[PerfStats]] = {}
    for r in _evaluated(results):
        for label, st in r.per_regime.items():
            pooled.setdefault(label, []).append(st)
    if not pooled:
        return "\n".join(rows + ["(no trades in any regime)"])
    for label in sorted(pooled, key=lambda k: -combine_stats(pooled[k]).trades):
        s = combine_stats(pooled[label])
        rows.append(
            f"{label:<24}{s.trades:>8}{s.net_pnl:>11.2f}{_pf(s.profit_factor):>7}"
            f"{s.win_rate * 100:>7.1f}%{s.expectancy:>9.4f}{s.avg_win:>10.4f}"
            f"{s.avg_loss:>10.4f}"
        )
    return "\n".join(rows)


def _axis_table(
    results: Sequence[ArmResult],
    title: str,
    label: str,
    bucket: Callable[[ArmResult], str],
    order: Callable[[str], Any] | None = None,
) -> str:
    rows = [
        _title(title),
        f"{label:<22}{'arms':>6}{'trades':>8}{'net P&L':>11}{'PF':>7}{'win%':>8}"
        f"{'expect':>10}{'maxDD':>9}{'avg hold':>10}",
        _rule(),
    ]
    pooled: dict[str, list[ArmResult]] = {}
    for r in _evaluated(results):
        pooled.setdefault(bucket(r), []).append(r)
    if not pooled:
        return "\n".join(rows + ["(nothing was evaluated)"])
    keys = sorted(pooled, key=order) if order else sorted(pooled)
    for k in keys:
        group = pooled[k]
        s = combine_stats([r.out_of_sample for r in group])
        hold = s.avg_holding_seconds / 60.0
        rows.append(
            f"{k:<22}{len(group):>6}{s.trades:>8}{s.net_pnl:>11.2f}"
            f"{_pf(s.profit_factor):>7}{s.win_rate * 100:>7.1f}%{s.expectancy:>10.4f}"
            f"{s.max_drawdown:>9.2f}{hold:>9.1f}m"
        )
    return "\n".join(rows)


def per_horizon_table(
    results: Sequence[ArmResult], *, config: TournamentConfig | None = None
) -> str:
    """Horizon curve, with the *required* hit rate printed beside the measured
    one.  A win rate below the required one is arithmetic, not bad luck."""
    table = _axis_table(
        results,
        "PER-HORIZON — OUT OF SAMPLE (the horizon is the time stop, not a lock)",
        "horizon",
        lambda r: f"{r.candidate.horizon_seconds // 60}m",
        order=lambda k: int(k[:-1]),
    )
    if config is None:
        return table
    pooled: dict[int, list[ArmResult]] = {}
    for r in _evaluated(results):
        pooled.setdefault(r.candidate.horizon_seconds // 60, []).append(r)
    if not pooled:
        return table
    rows = [
        _rule(),
        "Break-even arithmetic — a symmetric-payoff arm needs "
        f"p > 0.5 + cost / (2 x median move); cost = {config.round_trip_bps:g} bps",
        f"{'horizon':<10}{'median move':>13}{'required win%':>15}{'actual win%':>13}"
        f"{'verdict':>12}",
    ]
    for minutes in sorted(pooled):
        s = combine_stats([r.out_of_sample for r in pooled[minutes]])
        symbols = {r.candidate.symbol for r in pooled[minutes]}
        move = sum(median_move_bps(sym, minutes) for sym in symbols) / len(symbols)
        need = required_hit_rate(sorted(symbols)[0], minutes, config.round_trip_bps) \
            if len(symbols) == 1 else (
                sum(required_hit_rate(sym, minutes, config.round_trip_bps)
                    for sym in symbols) / len(symbols)
            )
        got = s.win_rate
        verdict = "-" if s.trades == 0 else ("clears" if got >= need else "short")
        rows.append(
            f"{str(minutes) + 'm':<10}{move:>12.1f}b{need * 100:>14.1f}%"
            f"{got * 100:>12.1f}%{verdict:>12}"
        )
    return table + "\n" + "\n".join(rows)


def per_exit_table(results: Sequence[ArmResult]) -> str:
    return _axis_table(
        results, "PER-EXIT-PROFILE — OUT OF SAMPLE", "exit profile",
        lambda r: r.candidate.exit_profile,
        order=_profile_sort_key,
    )


def per_stop_table(results: Sequence[ArmResult]) -> str:
    """Stops, with the two failure modes the spec names called out explicitly."""
    table = _axis_table(
        results, "PER-STOP-PROFILE — OUT OF SAMPLE", "stop profile",
        lambda r: r.candidate.stop_profile,
        order=_profile_sort_key,
    )
    pooled: dict[str, list[ArmResult]] = {}
    for r in _evaluated(results):
        pooled.setdefault(r.candidate.stop_profile, []).append(r)
    notes: list[str] = []
    for name in sorted(pooled, key=_profile_sort_key):
        s = combine_stats([r.out_of_sample for r in pooled[name]])
        if s.trades < 10:
            continue
        stops = sum(
            r.exit_reasons.get("STOP_LOSS", 0) for r in pooled[name]
        )
        stop_frac = stops / s.trades if s.trades else 0.0
        if stop_frac > 0.6 and s.win_rate < 0.35:
            notes.append(
                f"  {name}: {stop_frac * 100:.0f}% of exits are stops at a "
                f"{s.win_rate * 100:.0f}% win rate — this stop is inside the noise"
            )
        if s.wins and s.avg_win > 0 and abs(s.avg_loss) > 5.0 * s.avg_win:
            notes.append(
                f"  {name}: average loss {abs(s.avg_loss):.4f} is "
                f"{abs(s.avg_loss) / s.avg_win:.1f}x the average win — one loss "
                f"erases many wins"
            )
    if notes:
        return table + "\n" + _rule() + "\nStop-profile failure modes:\n" + "\n".join(notes)
    return table


def _profile_sort_key(name: str) -> tuple[Any, ...]:
    kind, value = _split_profile(name)
    return (kind, value)


def entry_quality_table(results: Sequence[ArmResult]) -> str:
    """Trade count against expectancy and PF: do 10 excellent trades beat 150
    mediocre ones, and where is the turn?"""
    return _axis_table(
        results, "ENTRY QUALITY — minimum signal score, OUT OF SAMPLE",
        "min score",
        lambda r: f"{r.candidate.min_score:g}",
        order=float,
    )


def per_family_table(results: Sequence[ArmResult]) -> str:
    return _axis_table(
        results, "PER-FAMILY — OUT OF SAMPLE", "family",
        lambda r: r.candidate.family,
    )


def arms_table(
    results: Sequence[ArmResult], *, limit: int = 25,
    config: TournamentConfig | None = None,
) -> str:
    """Every arm, in-sample and out-of-sample side by side.  Losers included.

    ``need%`` is the hit rate this arm's horizon requires to break even at the
    modelled round trip; a ``win%`` below it is arithmetic, not variance.
    """
    rt = config.round_trip_bps if config else 0.0
    rows = [
        _title(f"ARMS — in-sample vs out-of-sample (top {limit} by OOS expectancy)"),
        f"{'':<46}{'|':>2}{'IN-SAMPLE':^24}{'|':>2}{'OUT-OF-SAMPLE':^40}",
        f"{'candidate':<46}{'|':>2}{'trades':>8}{'PF':>7}{'expect':>9}"
        f"{'|':>2}{'trades':>8}{'PF':>7}{'expect':>9}{'win%':>8}{'need%':>8}",
        _rule(),
    ]
    evaluated = _evaluated(results)
    if not evaluated:
        return "\n".join(rows + ["(nothing was evaluated)"])
    shown = sorted(evaluated, key=lambda r: (-r.out_of_sample.expectancy, r.key))[:limit]
    for r in shown:
        i, o = r.in_sample, r.out_of_sample
        name = _short_key(r.candidate)
        need = required_hit_rate(
            r.candidate.symbol, r.candidate.horizon_minutes, rt
        ) if config else 0.0
        rows.append(
            f"{name:<46}{'|':>2}{i.trades:>8}{_pf(i.profit_factor):>7}{i.expectancy:>9.4f}"
            f"{'|':>2}{o.trades:>8}{_pf(o.profit_factor):>7}{o.expectancy:>9.4f}"
            f"{o.win_rate * 100:>7.1f}%{need * 100:>7.1f}%"
        )
    rows.append(_rule())
    losers = sum(1 for r in evaluated if r.out_of_sample.net_pnl <= 0)
    rows.append(
        f"{len(evaluated)} arms evaluated; {losers} were net-negative out-of-sample "
        f"({losers / len(evaluated) * 100:.0f}%)"
    )
    return "\n".join(rows)


def _short_key(c: Candidate) -> str:
    regime = c.regime.value[:6].lower() if c.regime else "any"
    return (
        f"{c.family[:14]}/{c.symbol[:6]}/{c.horizon_seconds // 60}m/"
        f"{c.exit_profile}/{c.stop_profile}/{regime}/q{c.min_score:g}"
    )


def qualifying(results: Sequence[ArmResult], cfg: TournamentConfig) -> list[ArmResult]:
    """Arms that clear the promotion gates this module can evaluate.

    The full gate list lives in ``robustness.py`` (cost stress, Monte Carlo);
    what is checked here is the part that needs only the tournament's own
    numbers: positive out-of-sample expectancy, PF >= 1.20, the minimum sample,
    a majority of positive windows, and not being carried by one trade.
    """
    out: list[ArmResult] = []
    for r in _evaluated(results):
        s = r.out_of_sample
        if s.trades < cfg.min_trades:
            continue
        if s.expectancy <= 0 or s.profit_factor < 1.20:
            continue
        if not r.majority_windows_positive:
            continue
        if r.out_of_sample_trades and r.expectancy_without_best_trade <= 0:
            continue
        out.append(r)
    out.sort(key=lambda r: (-r.out_of_sample.expectancy, r.key))
    return out


def format_tournament(
    results: Sequence[ArmResult],
    *,
    config: TournamentConfig,
    meta: dict[str, Any] | None = None,
) -> str:
    """The full text report.  Headline is out-of-sample or it is not a headline."""
    meta = dict(meta or {})
    evaluated = _evaluated(results)
    abandoned = [r for r in results if r.abandoned]

    lines: list[str] = [_title("STRATEGY TOURNAMENT — PHASE 3")]
    lines.append(f"symbols          : {', '.join(config.symbols)}")
    lines.append(f"period           : {meta.get('period', '-')}")
    lines.append(f"families         : {meta.get('families', '-')}")
    lines.append(f"signal source    : {meta.get('source', '-')}")
    lines.append(
        f"fees             : {config.fee_bps_per_side:g} bps/side   "
        f"slippage {config.slippage_bps:g} bps"
    )
    lines.append(
        "walk-forward     : "
        + ", ".join(s.label for s in config.scheme_order())
        + "  (train/test days; parameters frozen from train to test)"
    )
    lines.append(
        f"candidates       : {len(results)} enumerated, {len(evaluated)} evaluated, "
        f"{len(abandoned)} abandoned early"
    )
    lines.append(f"wall clock       : {meta.get('elapsed', '-')}")
    lines.append(f"jobs             : {config.jobs}")
    lines.append("")
    lines.append(_title("WHAT THE HORIZON ACTUALLY OFFERS (measured, 242 days)"))
    lines.append(
        "Median |move| by horizon, bps: "
        + "; ".join(
            f"{sym} " + ", ".join(f"{h}m={v:g}" for h, v in sorted(tbl.items()))
            for sym, tbl in MEASURED_MEDIAN_MOVE_BPS.items()
        )
    )
    lines.append(
        f"Round trip modelled here: {config.round_trip_bps:g} bps "
        f"({config.fee_bps_per_side:g} bps/side fee + {config.slippage_bps:g} bps "
        f"slippage + {config.spread_estimate_bps:g} bps spread)."
    )
    lines.append(
        "The earlier framing — that the cost of trading is the same size as the "
        "move — holds at one minute and does NOT hold at 25-60 minutes, where the "
        "median move is 4-10x the round trip. V1 and V2 lost money at 3-8 minute "
        "average holds; on this table that is a fact about their exit logic, not "
        "proof that the venue cannot be traded. That older conclusion is not "
        "carried forward here."
    )
    lines.append(
        "The 2-minute horizon is a different matter: 0.9 bps median on BTC and "
        "0.0 on ETH/SOL against a ~3 bps floor. It is swept for completeness and "
        "no search budget is spent optimising around it."
    )
    lines.append("")

    finalists = qualifying(results, config)
    marginal = [
        r for r in evaluated
        if r.out_of_sample.trades >= config.min_trades
        and r.out_of_sample.expectancy > 0
        and 1.0 <= r.out_of_sample.profit_factor < 1.20
        and r not in finalists
    ]

    lines.append(_title("HEADLINE — OUT OF SAMPLE"))
    if finalists:
        lines.append(f"{len(finalists)} arm(s) cleared every tournament gate:")
        for r in finalists[: config.top_n]:
            s = r.out_of_sample
            lines.append(
                f"  {_short_key(r.candidate)}  trades={s.trades} "
                f"PF={_pf(s.profit_factor).strip()} expectancy=${s.expectancy:.4f} "
                f"net=${s.net_pnl:.2f} windows+={r.windows_positive}/"
                f"{r.windows_evaluated}"
            )
        lines.append("")
        lines.append(
            "These are tournament finalists, not champions: the cost-stress, "
            "Monte-Carlo and drawdown gates in robustness.py still apply."
        )
    else:
        pooled = combine_stats([r.out_of_sample for r in evaluated])
        lines.append("NOTHING QUALIFIED.")
        lines.append("")
        lines.append(
            "No arm cleared the out-of-sample gates (expectancy > 0, PF >= 1.20, "
            f">= {config.min_trades} trades, a majority of positive windows, not "
            "carried by one trade)."
        )
        if marginal:
            lines.append(
                f"{len(marginal)} arm(s) were *marginal* — positive expectancy with "
                "PF between 1.00 and 1.20. Marginal is not a finalist:"
            )
            for r in marginal[:5]:
                s = r.out_of_sample
                lines.append(
                    f"  {_short_key(r.candidate)}  trades={s.trades} "
                    f"PF={_pf(s.profit_factor).strip()} expectancy=${s.expectancy:.4f}"
                )
        if evaluated:
            best = min(evaluated, key=lambda r: -r.out_of_sample.expectancy)
            s = best.out_of_sample
            lines.append("")
            lines.append(
                f"The least-bad arm was {_short_key(best.candidate)} at "
                f"${s.expectancy:.4f} per trade over {s.trades} trades. "
                "It is reported here as a diagnostic, not as a result."
            )
            lines.append(
                f"Pooled across every arm: {pooled.trades} out-of-sample trades, "
                f"net ${pooled.net_pnl:.2f}, fees ${pooled.fees:.2f}, "
                f"slippage ${pooled.slippage:.2f}."
            )
    lines.append("")

    for table in (
        arms_table(results, limit=config.top_n, config=config),
        per_coin_table(results),
        per_family_table(results),
        per_horizon_table(results, config=config),
        per_exit_table(results),
        per_stop_table(results),
        entry_quality_table(results),
        per_regime_table(results),
    ):
        lines.append(table)
        lines.append("")

    if abandoned:
        lines.append(_title("ABANDONED EARLY"))
        reasons: dict[str, int] = {}
        for r in abandoned:
            reasons[r.abandoned or "?"] = reasons.get(r.abandoned or "?", 0) + 1
        for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1])[:12]:
            lines.append(f"  {n:>6}  {reason}")
        lines.append(
            "\nAbandonment is arithmetic, not judgement: a candidate is dropped only "
            "when it cannot reach the minimum sample however it trades."
        )
        lines.append("")

    notes = sorted({n for r in results for n in r.notes})
    if notes:
        lines.append(_title("NOTES"))
        lines.extend(f"  - {n}" for n in notes)
        lines.append("")

    return "\n".join(lines)


def to_json(results: Sequence[ArmResult], *, config: TournamentConfig,
            meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """A machine-readable view: every arm, never only the winners."""

    def stats(s: PerfStats) -> dict[str, Any]:
        return {
            "trades": s.trades, "wins": s.wins, "losses": s.losses,
            "net_pnl": s.net_pnl, "gross_pnl": s.gross_pnl, "fees": s.fees,
            "slippage": s.slippage, "profit_factor": (
                None if s.profit_factor == float("inf") else s.profit_factor
            ),
            "expectancy": s.expectancy, "max_drawdown": s.max_drawdown,
            "win_rate": s.win_rate, "avg_win": s.avg_win, "avg_loss": s.avg_loss,
            "avg_holding_seconds": s.avg_holding_seconds,
        }

    return {
        "meta": dict(meta or {}),
        "config": {
            "symbols": list(config.symbols),
            "fee_bps_per_side": config.fee_bps_per_side,
            "slippage_bps": config.slippage_bps,
            "schemes": [s.label for s in config.scheme_order()],
            "min_trades": config.min_trades,
            "max_candidates": config.max_candidates,
            "jobs": config.jobs,
        },
        "finalists": [r.key for r in qualifying(results, config)],
        "arms": [
            {
                "key": r.key,
                "family": r.candidate.family,
                "symbol": r.candidate.symbol,
                "params": {k: v for k, v in r.candidate.params.items()},
                "horizon_seconds": r.candidate.horizon_seconds,
                "exit_profile": r.candidate.exit_profile,
                "stop_profile": r.candidate.stop_profile,
                "regime": r.candidate.regime.value if r.candidate.regime else None,
                "in_sample": stats(r.in_sample),
                "out_of_sample": stats(r.out_of_sample),
                "windows_evaluated": r.windows_evaluated,
                "windows_positive": r.windows_positive,
                # Whether the gates that read individual trades had a complete
                # trade set to read.  Published so a report can never again
                # show a gate verdict without showing what it was computed on.
                "trades_retained": len(r.out_of_sample_trades),
                "trades_complete": r.trades_complete,
                "signals_available": r.signals_available,
                "exit_reasons": dict(r.exit_reasons),
                "per_regime": {k: stats(v) for k, v in r.per_regime.items()},
                "abandoned": r.abandoned,
            }
            for r in results
        ],
    }
