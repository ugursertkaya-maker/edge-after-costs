#!/usr/bin/env python
"""Does resting beat crossing on Binance.US?  Ask the tape, not a bar.

    python scripts/run_execution_study.py \
        --tape-dir /data/tape --symbols BTCUSD,ETHUSD,SOLUSD \
        --timeout-seconds 5,15,30,60 --offset-ticks 0,1,2 --out text

If the tape was captured inside the worker container, copy it out first
(read-only; the running worker is not modified)::

    docker compose exec -T worker tar cf - /data/tape | tar xf - -C /tmp/

The output has three parts, in this order on purpose:

1. **Coverage** — how much tape there actually is.  If there is not enough, the
   script says so, says exactly how much more is needed, and refuses to present
   a conclusion.  The recorder is new; the honest answer today is "insufficient
   data, here is the machinery".  Twenty minutes of tape can produce a fill
   rate; it cannot produce a finding, and a number printed without its sample
   size is how a research tool starts lying.
2. **Quote grid** — fill rate, wait and markout for every (timeout, offset)
   pair.  This needs no strategy: it is purely about whether a passive quote on
   this venue gets hit, and what the price does to us immediately after.
3. **Execution comparison** — TAKER vs MAKER vs HYBRID over the same probe
   signals, with the opportunity cost of the signals the maker never filled
   charged against the maker arm.

Read the markout column before the fill-rate column.  A high fill rate with
negative markout is a *worse* outcome than a low fill rate with flat markout:
it means the 0% maker fee is being paid back through the price.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
_API_ROOT = _SCRIPTS_DIR.parent
for _p in (_API_ROOT, _SCRIPTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from app.research.maker_replay import (  # noqa: E402
    MARKOUT_HORIZONS_MS,
    MIN_PRINTS_PER_SYMBOL,
    MIN_TAPE_HOURS,
    MakerReplay,
    TapeCoverage,
    load_tape,
    tape_coverage,
)

_HORIZONS = tuple(MARKOUT_HORIZONS_MS)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_execution_study",
        description="Queue-aware maker/taker/hybrid execution study over the recorded tape.",
    )
    p.add_argument("--tape-dir", default="/data/tape", help="directory of tape-*.ndjson.gz")
    p.add_argument("--symbols", default="BTCUSD,ETHUSD,SOLUSD", help="comma separated")
    p.add_argument("--timeout-seconds", default="5,15,30,60", help="quote lifetimes to test")
    p.add_argument("--offset-ticks", default="0,1,2", help="ticks behind the touch")
    p.add_argument("--out", choices=("text", "json"), default="text")
    p.add_argument(
        "--cancel-ahead-ratio",
        type=float,
        default=0.5,
        help="share of unexplained level shrinkage assumed to be ahead of us "
        "(1.0 optimistic, 0.0 pessimistic); the pessimistic floor is always reported too",
    )
    p.add_argument("--notional", type=float, default=50.0, help="quote size in USD")
    p.add_argument("--cadence-seconds", type=int, default=30, help="seconds between probe quotes")
    p.add_argument("--hold-seconds", type=int, default=300, help="probe holding period")
    p.add_argument("--max-quotes", type=int, default=400, help="cap probes per symbol")
    p.add_argument("--warmup-seconds", type=int, default=30,
                   help="tape ignored at the start, so the replay book is warm")
    p.add_argument("--min-hours", type=float, default=MIN_TAPE_HOURS,
                   help="tape required before a conclusion is allowed")
    p.add_argument("--min-prints", type=int, default=MIN_PRINTS_PER_SYMBOL,
                   help="prints per symbol required before a conclusion is allowed")
    p.add_argument("--start-ms", type=int, default=0)
    p.add_argument("--end-ms", type=int, default=0)
    return p


def _ints(text: str) -> list[int]:
    out: list[int] = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out


# --------------------------------------------------------------------------- #
# The study
# --------------------------------------------------------------------------- #


def run(args: argparse.Namespace) -> dict[str, Any]:
    symbols = [s.strip().upper() for s in (args.symbols or "").split(",") if s.strip()]
    timeouts_ms = [s * 1000 for s in _ints(args.timeout_seconds) if s > 0]
    offsets = [o for o in _ints(args.offset_ticks) if o >= 0]
    if not timeouts_ms:
        timeouts_ms = [20_000]
    if not offsets:
        offsets = [1]

    slices = load_tape(
        args.tape_dir, symbols=symbols or None, start_ms=args.start_ms, end_ms=args.end_ms
    )
    coverage = tape_coverage(
        slices, min_hours=args.min_hours, min_prints=args.min_prints
    )
    coverage.files = _count_files(args.tape_dir)

    payload: dict[str, Any] = {
        "tape_dir": str(args.tape_dir),
        "requested_symbols": symbols,
        "coverage": _coverage_dict(coverage),
        "sufficient": coverage.sufficient,
        "insufficient_reasons": coverage.reasons(),
        "params": {
            "timeouts_ms": timeouts_ms,
            "offset_ticks": offsets,
            "cancel_ahead_ratio": args.cancel_ahead_ratio,
            "notional_usd": args.notional,
            "cadence_seconds": args.cadence_seconds,
            "hold_seconds": args.hold_seconds,
        },
        "grid": [],
        "grid_pessimistic": [],
        "comparison": {},
        "notes": [],
        "conclusion": "",
    }

    if not slices:
        payload["conclusion"] = (
            "INSUFFICIENT DATA — no tape at all. " + "; ".join(coverage.reasons())
        )
        return payload

    replay = MakerReplay(tape_dir=args.tape_dir, cancel_ahead_ratio=args.cancel_ahead_ratio)
    floor = MakerReplay(tape_dir=args.tape_dir, cancel_ahead_ratio=0.0)

    for sym in sorted(slices):
        sl = slices[sym]
        payload["grid"].extend(
            replay.quote_grid(
                sl,
                timeouts_ms=timeouts_ms,
                offsets=offsets,
                cadence_ms=args.cadence_seconds * 1000,
                notional_usd=args.notional,
                warmup_ms=args.warmup_seconds * 1000,
                limit=args.max_quotes,
            )
        )
        payload["grid_pessimistic"].extend(
            floor.quote_grid(
                sl,
                timeouts_ms=timeouts_ms,
                offsets=offsets,
                cadence_ms=args.cadence_seconds * 1000,
                notional_usd=args.notional,
                warmup_ms=args.warmup_seconds * 1000,
                limit=args.max_quotes,
            )
        )

    # One comparison per symbol at the middle timeout / first offset, using
    # probe signals.  These are a machinery check, not a strategy: see
    # MakerReplay.probe_signals.
    timeout = timeouts_ms[len(timeouts_ms) // 2]
    offset = offsets[0]
    payload["params"]["comparison_timeout_ms"] = timeout
    payload["params"]["comparison_offset_ticks"] = offset
    for sym in sorted(slices):
        sl = slices[sym]
        # A probe whose exit horizon falls past the end of the tape is dropped,
        # so on a very short tape every probe would vanish and the comparison
        # would print three empty arms.  Shorten the hold instead, and say so
        # rather than letting the reader assume the configured one was used.
        hold_ms = args.hold_seconds * 1000
        span_ms = max(0, sl.end_ms - sl.start_ms)
        if hold_ms * 4 > span_ms and span_ms > 0:
            hold_ms = max(1_000, span_ms // 4)
            payload.setdefault("notes", []).append(
                f"{sym}: probe hold shortened from {args.hold_seconds}s to "
                f"{hold_ms // 1000}s — the tape spans only {span_ms / 1000:.0f}s"
            )
        signals = replay.probe_signals(
            sl,
            cadence_ms=args.cadence_seconds * 1000,
            hold_ms=hold_ms,
            notional_usd=args.notional,
            warmup_ms=args.warmup_seconds * 1000,
            limit=args.max_quotes,
        )
        cmp = replay.compare(
            None,
            None,
            tape={sym: sl},
            signals=signals,
            timeout_ms=timeout,
            offset_ticks=offset,
        )
        payload["comparison"][sym] = cmp.as_dict()

    payload["conclusion"] = _conclusion(coverage, payload)
    return payload


def _count_files(directory: str | Path) -> int:
    d = Path(directory)
    return len(list(d.glob("tape-*.ndjson.gz"))) if d.is_dir() else 0


def _coverage_dict(cov: TapeCoverage) -> dict[str, Any]:
    return {
        "hours": round(cov.hours, 4),
        "files": cov.files,
        "symbols": cov.symbols,
        "truncated_files": list(cov.truncated_files),
        "min_hours": cov.min_hours,
        "min_prints": cov.min_prints,
        "sufficient": cov.sufficient,
        "shortfall_hours": round(cov.shortfall_hours, 4),
    }


def _conclusion(cov: TapeCoverage, payload: dict[str, Any]) -> str:
    if not cov.sufficient:
        return (
            "INSUFFICIENT DATA — this run cannot say whether resting beats "
            "crossing. " + " ".join(cov.reasons()) + " The machinery above is "
            "exercised and correct; re-run once the recorder has the tape."
        )
        # (deliberately no number extrapolated from a partial window)
    lines = []
    for sym, c in sorted(payload["comparison"].items()):
        lines.append(f"{sym}: {c['verdict']}")
    return " | ".join(lines) if lines else "no comparable signals in the tape window"


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def render(payload: dict[str, Any], log=print) -> None:
    cov = payload["coverage"]
    log("")
    log("=" * 78)
    log("EXECUTION STUDY — maker (rest) vs taker (cross) vs hybrid")
    log("=" * 78)

    # -- 1. how much tape ------------------------------------------------ #
    log("")
    log(f"TAPE  {payload['tape_dir']}")
    log(
        f"  files={cov['files']}  span={cov['hours']:.2f}h  "
        f"required={cov['min_hours']:.0f}h  sufficient={cov['sufficient']}"
    )
    if cov["truncated_files"]:
        log(f"  still being written (read to last complete block): "
            f"{', '.join(cov['truncated_files'])}")
    if cov["symbols"]:
        log("")
        log(f"  {'symbol':<10}{'events':>10}{'prints':>9}{'depth':>9}"
            f"{'quotes':>9}{'hours':>9}{'tick':>10}")
        for sym, v in sorted(cov["symbols"].items()):
            log(
                f"  {sym:<10}{v['events']:>10}{v['trades']:>9}{v['depth']:>9}"
                f"{v['ticker']:>9}{v['hours']:>9.3f}{v['tick_size']:>10}"
            )
    else:
        log("  (no symbols found in this tape)")

    if not payload["sufficient"]:
        log("")
        log("-" * 78)
        log("INSUFFICIENT DATA — no conclusion is drawn from this run.")
        for reason in payload["insufficient_reasons"]:
            log(f"  * {reason}")
        log("")
        log("  What a full day of tape will tell us, from exactly this machinery:")
        log("    - the fill rate of a post-only quote at 0/1/2 ticks behind the touch")
        log("    - how long it waits, and how much size sat in front of it")
        log("    - the markout at 1s/5s/30s after each fill, i.e. whether the 0%")
        log("      maker fee is being handed back through adverse selection")
        log("    - TAKER vs MAKER vs HYBRID NET, with the missed moves charged")
        log("      against the maker arm rather than quietly dropped")
        log("-" * 78)

    # -- 2. the quote grid ----------------------------------------------- #
    grid = payload.get("grid") or []
    if grid:
        floor = {
            (r["symbol"], r["timeout_ms"], r["offset_ticks"]): r
            for r in payload.get("grid_pessimistic") or []
        }
        log("")
        log("QUOTE GRID — post-only, never crossing")
        log("  fill% is of RESOLVED quotes; 'unres' were still open when the tape ended.")
        log("  floor% is the same quote with cancel_ahead_ratio=0 (no cancellation ever")
        log("  helps our queue position) — the pessimistic bound on the same data.")
        header = (
            f"  {'symbol':<9}{'t/out':>7}{'off':>5}{'quotes':>8}{'unres':>7}"
            f"{'touch%':>8}{'fill%':>8}{'floor%':>8}{'wait ms':>9}{'queue$':>10}"
        )
        for h in _HORIZONS:
            header += f"{'mk ' + h:>10}"
        log(header)
        for r in grid:
            f = floor.get((r["symbol"], r["timeout_ms"], r["offset_ticks"]))
            line = (
                f"  {r['symbol']:<9}{r['timeout_ms'] // 1000:>6}s{r['offset_ticks']:>5}"
                f"{r['quotes']:>8}{r['unresolved']:>7}{r['touch_rate'] * 100:>8.1f}"
                f"{r['fill_rate'] * 100:>8.1f}"
                f"{(f['fill_rate'] * 100 if f else 0.0):>8.1f}"
                f"{r['mean_wait_ms']:>9.0f}{r['mean_queue_ahead_usd']:>10.0f}"
            )
            for h in _HORIZONS:
                mk = r["markout_bps"].get(h)
                line += f"{(f'{mk:+.2f}' if mk is not None else 'n/a'):>10}"
            log(line)
        _log_touch_note(grid, log)

    # -- 3. the comparison ----------------------------------------------- #
    comparison = payload.get("comparison") or {}
    if comparison:
        log("")
        log("EXECUTION COMPARISON — same probe signals, three execution styles")
        log(
            f"  (timeout {payload['params'].get('comparison_timeout_ms', 0) // 1000}s, "
            f"offset {payload['params'].get('comparison_offset_ticks', 0)} ticks, "
            f"probe signals — no edge is claimed by them)"
        )
        for sym, c in sorted(comparison.items()):
            log("")
            log(f"  {sym}  ({c['signals']} signals)")
            log(
                f"    {'arm':<8}{'trades':>8}{'net $':>11}{'fees $':>10}"
                f"{'slip $':>10}{'expect $':>11}{'PF':>8}{'maxDD $':>10}"
            )
            for name in ("taker", "maker", "hybrid"):
                s = c[name]
                pf = s["profit_factor"]
                log(
                    f"    {name.upper():<8}{s['trades']:>8}{s['net_pnl']:>11.4f}"
                    f"{s['fees']:>10.4f}{s['slippage']:>10.4f}{s['expectancy']:>11.5f}"
                    f"{(f'{pf:.2f}' if pf != float('inf') else 'inf'):>8}"
                    f"{s['max_drawdown']:>10.4f}"
                )
            log(
                f"    maker fill rate {c['maker_fill_rate']:.1%} "
                f"({c['maker_fills']} fills, {c['maker_partials']} partial, "
                f"{c['maker_unresolved']} unresolved)"
            )
            log(
                f"    hybrid cross rate {c['hybrid_cross_rate']:.1%} "
                f"({c['hybrid_crosses']} crossed after a maker miss, "
                f"{c['hybrid_maker_fills']} filled as maker)"
            )
            log(
                f"    opportunity cost of unfilled maker signals: "
                f"${c['maker_opportunity_cost_usd']:.4f}  -> maker NET after it: "
                f"${c['maker_net_after_opportunity_cost']:.4f}"
            )
            log(f"    mean queue ahead ${c['mean_queue_ahead_usd']:.0f}, "
                f"mean wait {c['mean_wait_ms']:.0f} ms")
            marks = c.get("markout_bps") or {}
            samples = c.get("markout_samples") or {}
            cells = " ".join(
                f"{h}={marks[h]:+.2f}bps(n={samples.get(h, 0)})" if h in marks else f"{h}=n/a"
                for h in _HORIZONS
            )
            log(f"    markout (+ = in our favour): {cells}")
            if c.get("miss_reasons"):
                log(f"    misses: {c['miss_reasons']}")
            log(f"    >> {c['verdict']}")
            for note in c.get("notes") or []:
                log(f"       note: {note}")

    for note in payload.get("notes") or []:
        log(f"  note: {note}")

    log("")
    log("=" * 78)
    log(payload["conclusion"])
    log("=" * 78)
    log("")


def _log_touch_note(grid: list[dict[str, Any]], log) -> None:
    """Say out loud how many touches were not fills — the central lie.

    Counted over grid *cells*, not quotes: the same probe appears once per
    (timeout, offset) combination, and saying "touches" without that
    qualification would inflate the denominator.
    """
    touched = sum(r["touched"] for r in grid)
    fills = sum(r["fills"] for r in grid)
    if touched <= 0:
        log(
            "  nothing touched our quote level anywhere in the grid — with this "
            "little tape that is a statement about the sample, not the venue."
        )
        return
    log(
        f"  touched-but-not-filled: {touched - fills} of {touched} touched "
        f"quote-cells ({(1 - fills / touched) * 100:.1f}%) — a naive maker "
        "backtest books every touch as a fill and would report the other "
        f"{fills} as {touched}."
    )


# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run(args)
    if args.out == "json":
        print(json.dumps(payload, indent=2, default=str))
    else:
        render(payload)
    # Always 0: "there is not enough tape" is a result, not a failure.
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
