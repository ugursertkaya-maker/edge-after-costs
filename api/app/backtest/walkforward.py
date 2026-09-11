"""Rolling walk-forward validation.

A single backtest over the whole history is a *fit*, not evidence.  This module
splits the series into rolling ``train_days`` / ``test_days`` windows, runs
in-sample over the train slice, keeps only the strategies that were net
profitable there, and re-runs out-of-sample over the following, never-before-
seen test slice with just that subset.

The strategy subset is **the only thing optimised**.  No threshold, fee,
sizing or exit parameter is tuned per window — those would be far easier to
overfit and much harder to detect.

``WalkForwardResult`` carries both aggregates because the in-sample number is
the one that flatters and the out-of-sample number is the one that counts;
``report.format_walkforward`` always prints them side by side so the good one
can never be quoted alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import Settings
from app.types import Bar, PerfStats

from .engine import (
    BacktestConfig,
    BacktestResult,
    Components,
    bars_span_ms,
    combine_stats,
    run_backtest,
    slice_bars,
)

__all__ = ["WalkForwardResult", "WalkForwardWindow", "plan_windows", "run_walk_forward"]

MS_PER_DAY = 86_400_000


@dataclass(frozen=True)
class WalkForwardWindow:
    """One train/test split, as half-open ``[start, end)`` millisecond ranges."""

    index: int
    train_start_ms: int
    train_end_ms: int
    test_start_ms: int
    test_end_ms: int

    def overlaps_training(self, ts_ms: int) -> bool:
        return self.train_start_ms <= ts_ms < self.train_end_ms


@dataclass
class WalkForwardResult:
    windows: list[tuple[BacktestResult, BacktestResult]]   # (in-sample, out-of-sample)
    in_sample: PerfStats
    out_of_sample: PerfStats
    # Defaulted extras: the frozen constructor order above is untouched.
    splits: list[WalkForwardWindow] = field(default_factory=list)
    selected: list[tuple[str, ...]] = field(default_factory=list)
    train_days: int = 0
    test_days: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def in_sample_curve(self) -> list[tuple[int, float]]:
        return [pt for is_, _ in self.windows for pt in is_.equity_curve]

    @property
    def out_of_sample_curve(self) -> list[tuple[int, float]]:
        return [pt for _, oos in self.windows for pt in oos.equity_curve]


def plan_windows(
    start_ms: int, end_ms: int, *, train_days: int, test_days: int
) -> list[WalkForwardWindow]:
    """Tile ``[start_ms, end_ms)`` with rolling train/test splits.

    Test windows are contiguous and never overlap each other; a window's test
    slice always begins exactly where its train slice ends, so no bar is ever
    both trained on and validated on within the same window.
    """
    if train_days <= 0 or test_days <= 0:
        raise ValueError("train_days and test_days must both be positive")
    train_ms = train_days * MS_PER_DAY
    test_ms = test_days * MS_PER_DAY

    windows: list[WalkForwardWindow] = []
    train_start = start_ms
    index = 0
    while True:
        train_end = train_start + train_ms
        test_end = train_end + test_ms
        if test_end > end_ms:
            # A truncated final test window would be scored on less data than
            # the others; drop it rather than publish an uneven average.
            break
        windows.append(
            WalkForwardWindow(
                index=index,
                train_start_ms=train_start,
                train_end_ms=train_end,
                test_start_ms=train_end,
                test_end_ms=test_end,
            )
        )
        index += 1
        train_start += test_ms
    return windows


def _profitable_strategies(result: BacktestResult) -> tuple[str, ...]:
    """Strategies that were net-positive in-sample.  Net, never gross."""
    return tuple(
        sorted(name for name, stats in result.per_strategy.items() if stats.net_pnl > 0)
    )


def run_walk_forward(
    bars_by_symbol: dict[str, list[Bar]],
    cfg: BacktestConfig,
    *,
    train_days: int,
    test_days: int,
    components: Components | None = None,
    settings: Settings | None = None,
    component_factory=None,
) -> WalkForwardResult:
    """Run the full rolling validation.

    ``component_factory(cfg) -> Components`` lets each window start from clean
    component state (a ``FeatureEngine`` carries per-symbol history, so reusing
    one across windows would leak the train slice into the test slice).  When
    only ``components`` is given it is reused and the feature engine is reset
    between windows if it supports it.
    """
    start_ms, end_ms = bars_span_ms(bars_by_symbol)
    # ``bars_span_ms`` returns the last bar's *inclusive* close, while
    # ``plan_windows`` works in half-open ranges; without the +1 a history of
    # exactly train+test days produces zero windows.
    splits = plan_windows(
        start_ms, end_ms + 1 if end_ms else end_ms, train_days=train_days, test_days=test_days
    )

    notes: list[str] = []
    if not splits:
        notes.append(
            f"history spans {(end_ms - start_ms) / MS_PER_DAY:.1f}d — too short for a "
            f"{train_days}d/{test_days}d walk-forward; no windows were run"
        )

    def fresh(window_cfg: BacktestConfig) -> Components | None:
        if component_factory is not None:
            return component_factory(window_cfg)
        if components is not None:
            reset = getattr(components.features, "reset", None)
            if callable(reset):
                reset()
            return components
        return None

    windows: list[tuple[BacktestResult, BacktestResult]] = []
    selected: list[tuple[str, ...]] = []

    for w in splits:
        train_bars = slice_bars(bars_by_symbol, w.train_start_ms, w.train_end_ms)
        test_bars = slice_bars(bars_by_symbol, w.test_start_ms, w.test_end_ms)
        if not train_bars or not test_bars:
            notes.append(f"window {w.index}: empty slice, skipped")
            continue

        in_cfg = cfg
        in_result = run_backtest(
            train_bars,
            in_cfg,
            label=f"W{w.index}-IN-SAMPLE",
            components=fresh(in_cfg),
            settings=settings,
        )

        keep = _profitable_strategies(in_result)
        selected.append(keep)
        if not keep:
            notes.append(
                f"window {w.index}: no strategy was net-profitable in-sample — "
                "out-of-sample traded nothing, which is the correct answer"
            )

        from dataclasses import replace

        out_cfg = replace(cfg, strategies=keep)
        out_result = run_backtest(
            test_bars,
            out_cfg,
            label=f"W{w.index}-OUT-OF-SAMPLE",
            components=fresh(out_cfg),
            settings=settings,
        )
        windows.append((in_result, out_result))

    return WalkForwardResult(
        windows=windows,
        in_sample=combine_stats([a.stats for a, _ in windows]),
        out_of_sample=combine_stats([b.stats for _, b in windows]),
        splits=splits,
        selected=selected,
        train_days=train_days,
        test_days=test_days,
        notes=notes,
    )
