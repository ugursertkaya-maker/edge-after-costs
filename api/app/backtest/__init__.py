"""Backtesting and walk-forward validation.

The harness drives the *real* decision stack (features, regime, strategies,
ranker, risk, exits, simulator, ledger) over historical 1m bars.  Nothing in
this package reimplements a strategy, a sizing rule or an exit rule; if it
did, a green backtest would prove nothing about the live worker.
"""

from __future__ import annotations

from .data import cache_path, cache_root, fetch_klines, load_cached, save_cache
from .engine import (
    BacktestConfig,
    BacktestResult,
    Components,
    bars_span_ms,
    build_components,
    combine_stats,
    real_components_available,
    run_backtest,
    slice_bars,
    synth_book,
    synth_symbol_info,
    synth_trades,
)
from .report import format_result, format_walkforward
from .walkforward import WalkForwardResult, WalkForwardWindow, plan_windows, run_walk_forward

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "Components",
    "WalkForwardResult",
    "WalkForwardWindow",
    "bars_span_ms",
    "build_components",
    "cache_path",
    "cache_root",
    "combine_stats",
    "fetch_klines",
    "format_result",
    "format_walkforward",
    "load_cached",
    "plan_windows",
    "real_components_available",
    "run_backtest",
    "run_walk_forward",
    "save_cache",
    "slice_bars",
    "synth_book",
    "synth_symbol_info",
    "synth_trades",
]
