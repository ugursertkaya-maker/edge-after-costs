"""Monospace text rendering of backtest results.

This output is pasted straight into the final report, so it is written to be
*honest first, pretty second*: a losing strategy is labelled LOSS, fees and
slippage get their own lines instead of hiding inside "net", and a
walk-forward render always shows in-sample and out-of-sample next to each
other.  There is deliberately no code path that prints in-sample numbers
alone.
"""

from __future__ import annotations

import datetime as _dt
import math

from app.types import PerfStats

from .engine import BacktestResult
from .walkforward import WalkForwardResult

__all__ = ["format_result", "format_walkforward", "format_stats_row", "verdict_for"]

WIDTH = 78


def _ts(ms: int) -> str:
    if not ms:
        return "-"
    return _dt.datetime.fromtimestamp(ms / 1000, _dt.UTC).strftime("%Y-%m-%d %H:%M")


def _money(v: float) -> str:
    return f"{v:>10,.2f}"


def _pf(v: float) -> str:
    if v == float("inf"):
        return "  inf"
    return f"{v:5.2f}"


def _rule(char: str = "-") -> str:
    return char * WIDTH


def _title(text: str) -> str:
    return f"{_rule('=')}\n{text}\n{_rule('=')}"


def verdict_for(stats: PerfStats) -> str:
    """One blunt word.  No euphemisms for a losing curve."""
    if stats.trades == 0:
        return "NO TRADES"
    if stats.net_pnl > 0:
        return "PROFIT"
    if stats.net_pnl < 0:
        return "LOSS"
    return "FLAT"


# --------------------------------------------------------------------------- #
# Single backtest
# --------------------------------------------------------------------------- #

_LEADER_HEAD = (
    f"{'strategy':<20}{'trades':>7}{'win%':>7}{'net $':>11}"
    f"{'fees $':>10}{'slip $':>9}{'PF':>7}{'verdict':>10}"
)


def format_stats_row(name: str, s: PerfStats) -> str:
    return (
        f"{name[:20]:<20}{s.trades:>7}{s.win_rate * 100:>6.1f}%"
        f"{s.net_pnl:>11,.2f}{s.fees:>10,.2f}{s.slippage:>9,.2f}"
        f"{_pf(s.profit_factor):>7}{verdict_for(s):>10}"
    )


def _headline(result: BacktestResult) -> list[str]:
    s = result.stats
    ret_pct = (
        (result.ending_equity - result.starting_equity) / result.starting_equity * 100.0
        if result.starting_equity
        else 0.0
    )
    gross = s.gross_pnl
    return [
        f"  period            {_ts(result.start_ms)}  ->  {_ts(result.end_ms)}",
        f"  symbols           {', '.join(result.symbols) or '-'}",
        f"  bars processed    {result.bars_processed:,}",
        f"  starting equity  {_money(result.starting_equity)}",
        f"  ending equity    {_money(result.ending_equity)}   ({ret_pct:+.2f}%)",
        "",
        f"  gross P&L        {_money(gross)}   (price move, fees not yet charged)",
        f"  fees             {_money(-abs(s.fees))}",
        f"  NET P&L          {_money(s.net_pnl)}   <= {verdict_for(s)}",
        f"  slippage (memo)  {_money(-abs(s.slippage))}   (paid away from mid, "
        f"already inside gross)",
        "",
        f"  trades {s.trades}   wins {s.wins}   losses {s.losses}   "
        f"win rate {s.win_rate * 100:.1f}%",
        f"  avg win {s.avg_win:+.2f}   avg loss {s.avg_loss:+.2f}   "
        f"expectancy {s.expectancy:+.3f}/trade",
        f"  profit factor {_pf(s.profit_factor).strip()}   "
        f"max drawdown {s.max_drawdown:,.2f}   "
        f"avg hold {s.avg_holding_seconds / 60:.1f}m",
    ]


def _funnel(result: BacktestResult) -> list[str]:
    lines = [
        f"  signals generated  {result.signals_generated:,}",
        f"  entries approved   {result.entries_approved:,}",
        f"  entries filled     {result.entries_filled:,}",
        f"  open at end        {result.open_at_end}",
    ]
    if result.rejections:
        lines.append("  rejections:")
        for reason, count in sorted(
            result.rejections.items(), key=lambda kv: (-kv[1], kv[0])
        ):
            lines.append(f"    {reason:<28}{count:>8,}")
    return lines


def format_result(result: BacktestResult, *, include_trades: int = 0) -> str:
    """Render one backtest.  ``include_trades`` prints the last N round trips."""
    out: list[str] = [_title(f"BACKTEST — {result.label}"), ""]
    out += _headline(result)
    out += ["", _rule(), "PER-STRATEGY LEADERBOARD", _rule(), _LEADER_HEAD, _rule()]

    if result.per_strategy:
        ordered = sorted(
            result.per_strategy.items(), key=lambda kv: kv[1].net_pnl, reverse=True
        )
        for name, stats in ordered:
            out.append(format_stats_row(name, stats))
        losers = [n for n, st in ordered if st.net_pnl < 0]
        if losers:
            out.append("")
            out.append(f"  net negative: {', '.join(losers)}")
    else:
        out.append("  (no strategy produced a closed trade)")

    out += ["", _rule(), "DECISION FUNNEL", _rule()]
    out += _funnel(result)

    if include_trades and result.trades:
        out += ["", _rule(), f"LAST {min(include_trades, len(result.trades))} TRADES", _rule()]
        out.append(
            f"{'time':<17}{'symbol':<10}{'strategy':<18}{'reason':<16}{'net $':>9}"
        )
        for t in result.trades[-include_trades:]:
            out.append(
                f"{_ts(t.exit_ms):<17}{t.symbol:<10}{t.strategy[:18]:<18}"
                f"{t.exit_reason.value[:16]:<16}{t.net_pnl_usd:>9,.2f}"
            )

    if result.notes:
        out += ["", _rule(), "NOTES", _rule()]
        out += [f"  - {n}" for n in dict.fromkeys(result.notes)]

    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Walk-forward
# --------------------------------------------------------------------------- #

_SIDE_BY_SIDE_ROWS: tuple[tuple[str, str], ...] = (
    ("trades", "trades"),
    ("wins", "wins"),
    ("losses", "losses"),
    ("win rate %", "win_rate_pct"),
    ("gross P&L", "gross_pnl"),
    ("fees", "fees"),
    ("slippage", "slippage"),
    ("NET P&L", "net_pnl"),
    ("expectancy", "expectancy"),
    ("profit factor", "profit_factor"),
    ("max drawdown", "max_drawdown"),
)


def _value(stats: PerfStats, key: str) -> str:
    if key == "win_rate_pct":
        return f"{stats.win_rate * 100:.1f}"
    if key == "profit_factor":
        return _pf(stats.profit_factor).strip()
    v = getattr(stats, key)
    if isinstance(v, int):
        return f"{v:,}"
    return f"{v:,.2f}"


def _in_vs_out(wf: WalkForwardResult) -> list[str]:
    head = f"{'metric':<20}{'IN-SAMPLE':>16}{'OUT-OF-SAMPLE':>18}{'delta':>14}"
    lines = [head, _rule()]
    for label, key in _SIDE_BY_SIDE_ROWS:
        a = _value(wf.in_sample, key)
        b = _value(wf.out_of_sample, key)
        try:
            gap = float(b.replace(",", "")) - float(a.replace(",", ""))
            # inf - inf is nan, which is not a delta anyone can act on.
            delta = f"{gap:+,.2f}" if math.isfinite(gap) else "-"
        except ValueError:
            delta = "-"
        lines.append(f"{label:<20}{a:>16}{b:>18}{delta:>14}")
    lines.append(_rule())
    lines.append(
        f"{'verdict':<20}{verdict_for(wf.in_sample):>16}"
        f"{verdict_for(wf.out_of_sample):>18}{'':>14}"
    )
    return lines


def _honesty_note(wf: WalkForwardResult) -> list[str]:
    ins, oos = wf.in_sample, wf.out_of_sample
    lines = [
        "  IN-SAMPLE is the training half. The strategy subset was chosen BY",
        "  looking at it, so it is a fit, not a result. Only OUT-OF-SAMPLE is",
        "  evidence.",
    ]
    if oos.trades == 0:
        lines.append("  OUT-OF-SAMPLE produced no trades — nothing has been validated.")
    elif oos.net_pnl <= 0 < ins.net_pnl:
        lines.append(
            "  The edge did NOT survive out of sample. This system is not "
            "validated."
        )
    elif oos.net_pnl <= 0:
        lines.append("  Out-of-sample is net negative. This system loses money.")
    else:
        decay = (
            (ins.expectancy - oos.expectancy) / abs(ins.expectancy) * 100.0
            if ins.expectancy
            else 0.0
        )
        moved = "decayed" if decay >= 0 else "improved"
        lines.append(
            f"  Out-of-sample is net positive; expectancy {moved} "
            f"{abs(decay):.0f}% versus in-sample."
        )
        if oos.trades < 30:
            lines.append(
                f"  Only {oos.trades} out-of-sample trades — too few to call this "
                "an edge rather than noise."
            )
    return lines


def format_walkforward(wf: WalkForwardResult, *, include_periods: bool = True) -> str:
    out: list[str] = [
        _title(
            f"WALK-FORWARD VALIDATION — {wf.train_days}d train / {wf.test_days}d test"
        ),
        "",
        f"  windows run       {len(wf.windows)}",
        f"  windows planned   {len(wf.splits)}",
        "",
        _rule(),
        "IN-SAMPLE vs OUT-OF-SAMPLE",
        _rule(),
    ]
    out += _in_vs_out(wf)
    out += ["", _rule(), "HOW TO READ THIS", _rule()]
    out += _honesty_note(wf)

    if include_periods:
        out += ["", _rule(), "PER-PERIOD BREAKDOWN", _rule()]
        out.append(
            f"{'window':<8}{'test period':<24}{'IS net $':>11}{'OOS net $':>12}"
            f"{'OOS trades':>12}{'kept strategies':>18}"
        )
        out.append(_rule())
        for i, (ins, oos) in enumerate(wf.windows):
            kept = wf.selected[i] if i < len(wf.selected) else ()
            kept_txt = f"{len(kept)} of {len(ins.per_strategy) or '?'}" if kept else "none"
            period = f"{_ts(oos.start_ms)}+{wf.test_days}d"
            out.append(
                f"{i:<8}{period:<24}{ins.stats.net_pnl:>11,.2f}"
                f"{oos.stats.net_pnl:>12,.2f}{oos.stats.trades:>12}{kept_txt:>18}"
            )
        if wf.selected:
            out += ["", "  strategies kept per window:"]
            for i, kept in enumerate(wf.selected):
                out.append(f"    W{i}: {', '.join(kept) if kept else '(none)'}")

    if wf.notes:
        out += ["", _rule(), "NOTES", _rule()]
        out += [f"  - {n}" for n in dict.fromkeys(wf.notes)]

    return "\n".join(out)
