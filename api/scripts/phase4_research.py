#!/usr/bin/env python3
"""Run the predictive-entry research end to end and report honestly.

    BACKTEST_CACHE_DIR=/tmp/bt-cache python scripts/phase4_research.py \
        --symbols BTCUSD,ETHUSD,SOLUSD --days 120 --out-dir /tmp/phase4

The pipeline, in the only order that keeps the test period clean:

    labels (cost-aware, triple-barrier)
      -> feature matrix (leakage-safe, unscaled)
        -> chronological splits with purge + embargo
          -> model benchmark on TRAIN, selected on VALIDATE
            -> calibration fitted on VALIDATE
              -> probability threshold and EV gate chosen on VALIDATE
                -> everything frozen
                  -> TEST scored once

Nothing after the freeze may look at the test window. That is the whole
discipline of this script, and the reason the freeze is an explicit step rather
than an implied one.

Deduplication is on by default. Phase 3 pooled overlapping walk-forward schemes
and counted the same round trip more than once — 79 "trades" were 50 — which
inflates every sample-size gate and every Monte Carlo that assumes independence.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

DEFAULT_HORIZONS = (5, 10, 15, 25, 40, 60, 90)
DEFAULT_TARGETS_PCT = (0.10, 0.15, 0.20, 0.25, 0.35, 0.50, 0.75, 1.00)
DEFAULT_THRESHOLDS = (0.55, 0.575, 0.60, 0.625, 0.65, 0.675, 0.70, 0.75)


def jsonable(obj):
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def say(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(prog="phase4_research")
    ap.add_argument("--symbols", default="BTCUSD,ETHUSD,SOLUSD")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--tape-cache", default="/tmp/tape-cache")
    ap.add_argument("--out-dir", default="/tmp/phase4")
    ap.add_argument("--horizons", default=",".join(str(h) for h in DEFAULT_HORIZONS))
    ap.add_argument("--targets", default=",".join(f"{t:g}" for t in DEFAULT_TARGETS_PCT))
    ap.add_argument("--round-trip-bps", type=float, default=6.0,
                    help="fee both sides + spread + slippage, measured on this venue")
    ap.add_argument("--train-days", type=int, default=60)
    ap.add_argument("--validate-days", type=int, default=15)
    ap.add_argument("--test-days", type=int, default=15)
    ap.add_argument("--step-days", type=int, default=15)
    ap.add_argument("--embargo-minutes", type=int, default=60)
    ap.add_argument("--equity", type=float, default=500.0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--max-specs", type=int, default=24,
                    help="cap the label grid; the full product is large and most "
                         "of it is uninformative on this venue")
    args = ap.parse_args()

    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    horizons = tuple(int(h) for h in args.horizons.split(",") if h.strip())
    targets = tuple(float(t) for t in args.targets.split(",") if t.strip())
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    from app.predict import calibrate as cal
    from app.predict import ev as evmod
    from app.predict import labels as lab
    from app.predict import matrix as mx
    from app.predict import models as mdl
    from app.predict import splits as sp
    from app.research.dataset import load_dataset

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.days * 86_400_000

    report: dict = {
        "generated_ms": end_ms,
        "symbols": list(symbols),
        "days": args.days,
        "round_trip_bps": args.round_trip_bps,
        "horizons": list(horizons),
        "targets_pct": list(targets),
        "per_symbol": {},
    }

    # ---------------------------------------------------------------- #
    # Label grid. Targets that cannot clear their own round trip are not
    # targets; the labeller refuses them and we record the refusal.
    # ---------------------------------------------------------------- #
    specs = []
    for h in horizons:
        for tpct in targets:
            try:
                specs.append(lab.BarrierSpec(
                    net_target_pct=tpct, stop_kind="atr", stop_value=2.0,
                    horizon_minutes=h, round_trip_bps=args.round_trip_bps,
                ))
            except Exception as exc:
                report.setdefault("refused_specs", []).append(
                    {"horizon": h, "target_pct": tpct, "reason": str(exc)}
                )
    specs = specs[: args.max_specs]
    say(f"[specs] {len(specs)} barrier specs "
        f"({len(report.get('refused_specs', []))} refused as sub-cost)")

    best_overall = None

    for symbol in symbols:
        say(f"\n=== {symbol} ===")
        t0 = time.time()
        ds = load_dataset(symbol, start_ms, end_ms,
                          intervals=("1m", "5m", "15m"), cache_dir=args.tape_cache)
        bars = ds.bars.get("1m") or []
        say(f"[{symbol}] {len(bars):,} 1m bars, {len(ds.trades):,} prints "
            f"({time.time() - t0:.1f}s)")
        if len(bars) < 5_000:
            say(f"[{symbol}] too little history — skipped")
            continue

        sym_report: dict = {"bars": len(bars), "specs": {}}

        for spec in specs:
            key = spec.key()
            labels = lab.build_labels(bars, spec=spec, symbol=symbol)
            if not labels:
                continue
            dist: dict[str, int] = {}
            for lb in labels:
                dist[lb.outcome.value] = dist.get(lb.outcome.value, 0) + 1
            n_win = dist.get("TP_BEFORE_SL", 0)
            n_loss = dist.get("SL_BEFORE_TP", 0)
            if n_win < 100 or n_loss < 100:
                sym_report["specs"][key] = {
                    "labels": len(labels), "distribution": dist,
                    "skipped": "too few of one class to train on",
                }
                continue

            matrix = mx.build_matrix(ds, labels, cache_dir=args.tape_cache)
            windows = sp.rolling_splits(
                start_ms, end_ms,
                train_days=args.train_days, validate_days=args.validate_days,
                test_days=args.test_days, step_days=args.step_days,
                max_horizon_minutes=spec.horizon_minutes,
                embargo_minutes=args.embargo_minutes,
            )
            if not windows:
                continue

            spec_report = {
                "labels": len(labels), "distribution": dist,
                "windows": len(windows), "folds": [],
            }
            for split in windows:
                fold = run_fold(
                    matrix, labels, split, spec,
                    mdl=mdl, cal=cal, evmod=evmod, sp=sp,
                    equity=args.equity, seed=args.seed,
                    round_trip_bps=args.round_trip_bps,
                    thresholds=DEFAULT_THRESHOLDS,
                    embargo_minutes=args.embargo_minutes,
                )
                if fold:
                    spec_report["folds"].append(fold)

            oos = pool_folds(spec_report["folds"])
            spec_report["out_of_sample"] = oos
            sym_report["specs"][key] = spec_report
            if oos and oos.get("trades", 0) >= 30:
                say(f"  {key}: OOS {oos['trades']} trades  "
                    f"win {oos['win_rate']:.1%} vs needs {oos['required_win_rate']:.1%}  "
                    f"PF {oos['profit_factor']:.2f}  "
                    f"EV {oos['expectancy_bps']:+.2f} bps  "
                    f"cal-err {oos['max_abs_calibration_error']:.3f}")
                if best_overall is None or oos["expectancy_bps"] > best_overall[1]["expectancy_bps"]:
                    best_overall = (f"{symbol}|{key}", oos)

        report["per_symbol"][symbol] = sym_report

    report["best"] = (
        {"key": best_overall[0], **best_overall[1]} if best_overall else None
    )
    (out / "phase4.json").write_text(json.dumps(jsonable(report), indent=1))

    say("\n" + "=" * 70)
    if best_overall is None:
        say("NO PREDICTIVE EDGE FOUND.")
        say("No (symbol, barrier, horizon) combination produced a usable "
            "out-of-sample sample with positive expectancy. That is the "
            "search's answer, not a missing step.")
    else:
        key, o = best_overall
        say(f"BEST OUT-OF-SAMPLE: {key}")
        say(f"  trades {o['trades']}  win {o['win_rate']:.1%} "
            f"(needs {o['required_win_rate']:.1%})  PF {o['profit_factor']:.2f}  "
            f"EV {o['expectancy_bps']:+.2f} bps/trade  "
            f"(${o['expectancy_usd_at_100']:+.4f} on $100)  "
            f"brier {o['brier']:.4f}  cal-err {o['max_abs_calibration_error']:.3f}")
        say("  A result here is a candidate, not a champion: the promotion "
            "gates in robustness.py still apply.")
    say(f"[out] {out}/phase4.json")
    return 0


def run_fold(matrix, labels, split, spec, *, mdl, cal, evmod, sp,
             equity, seed, round_trip_bps, thresholds, embargo_minutes):
    """Train, calibrate and choose thresholds on train/validate; score test once.

    The ordering here is the whole point: ``benchmark`` evaluates on
    ``validate``, the calibrator is fitted on ``validate``, the threshold is
    chosen on ``validate``, and only then is ``test`` touched — once.
    """
    try:
        tr, va, te = mdl.split_matrix(matrix, split, embargo_minutes=embargo_minutes)
    except Exception as exc:
        return {"split": split.name, "skipped": f"split_matrix: {exc}"}
    if min(len(tr), len(va), len(te)) < 50:
        return {"split": split.name,
                "skipped": f"too few rows: {len(tr)}/{len(va)}/{len(te)}"}
    if len(np.unique(tr.y)) < 2 or len(np.unique(va.y)) < 2:
        return {"split": split.name, "skipped": "a slice has only one class"}

    try:
        reports = mdl.benchmark(matrix, split, seed=seed,
                                embargo_minutes=embargo_minutes)
        champion, why = mdl.select_champion(reports)
    except Exception as exc:
        return {"split": split.name, "skipped": f"benchmark: {exc}"}
    if champion is None or champion.model is None:
        return {"split": split.name, "skipped": why}

    p_va = champion.model.predict_p_win(va.X)
    # `keys=` makes the leak guard exact rather than fingerprint-based: a tree
    # ensemble gives hundreds of rows the same leaf probability, which the
    # value-based fallback cannot tell apart from genuine overlap.
    calibrator = cal.fit_calibrator(p_va, va.y, method="isotonic", keys=va.ts_ms)
    p_te = calibrator.predict(champion.model.predict_p_win(te.X))
    calibration = cal.evaluate_calibration(p_te, te.y)

    win_bps = spec.net_target_pct * 100.0
    sweep = evmod.threshold_search(
        calibrator.predict(p_va), va.y,
        net_returns=np.full(len(va.y), win_bps),
        thresholds=thresholds, cost_bps=round_trip_bps,
    )
    selection = evmod.select_threshold(sweep)
    threshold = getattr(selection, "threshold", None)
    if threshold is None:
        return {"split": split.name,
                "skipped": getattr(selection, "reason", "no threshold selected")}

    taken = p_te >= threshold
    n = int(taken.sum())
    if n == 0:
        return {"split": split.name, "trades": 0, "threshold": float(threshold),
                "skipped": "threshold admitted nothing in the test window"}

    outcomes = te.y[taken].astype(float)
    net = outcomes * win_bps - (1.0 - outcomes) * win_bps - round_trip_bps
    wins = float(net[net > 0].sum())
    losses = float(-net[net < 0].sum())
    # With a symmetric payoff the geometry alone fixes the hit rate needed to
    # break even: p > 0.5 + cost / (2 x target). Printing it beside the actual
    # win rate is the fastest way to see whether a model has skill but is being
    # eaten by costs, which is a completely different problem from having none.
    required = 0.5 + round_trip_bps / (2.0 * win_bps)
    return {
        "split": split.name,
        "threshold": float(threshold),
        "model": champion.name,
        "model_choice": why,
        "trades": n,
        "win_rate": float(outcomes.mean()),
        "net_bps": float(net.sum()),
        "expectancy_bps": float(net.mean()),
        "profit_factor": (wins / losses) if losses > 0 else float("inf"),
        "gross_win_bps": wins,
        "gross_loss_bps": losses,
        "required_win_rate": float(required),
        "brier": float(calibration.brier),
        "max_abs_calibration_error": float(calibration.max_abs_error),
        "rows": {"train": len(tr), "validate": len(va), "test": len(te)},
        "purged": split.purged,
        "embargoed": split.embargoed,
    }


def pool_folds(folds: list[dict]) -> dict | None:
    """Aggregate the test windows. Only out-of-sample numbers live here.

    Profit factor is computed from the pooled **trade** wins and losses, not
    from the per-fold nets: two losing folds would otherwise pool to a profit
    factor of 0.00 and hide the fact that each was around 0.5.
    """
    real = [f for f in folds if f.get("trades")]
    if not real:
        return None
    trades = sum(f["trades"] for f in real)
    wins = sum(f.get("gross_win_bps", 0.0) for f in real)
    losses = sum(f.get("gross_loss_bps", 0.0) for f in real)
    net_bps = sum(f["net_bps"] for f in real)
    n_wins = sum(f["trades"] * f["win_rate"] for f in real)
    return {
        "trades": trades,
        "win_rate": n_wins / trades if trades else 0.0,
        "net_bps": net_bps,
        "expectancy_bps": net_bps / trades if trades else 0.0,
        # What the same edge is worth in dollars at a representative position.
        "net_usd_at_100": net_bps / 10_000.0 * 100.0,
        "expectancy_usd_at_100": (net_bps / trades if trades else 0.0) / 10_000.0 * 100.0,
        "profit_factor": (wins / losses) if losses > 0 else float("inf"),
        "windows": len(real),
        "windows_positive": sum(1 for f in real if f["net_bps"] > 0),
        "brier": sum(f["brier"] for f in real) / len(real),
        "max_abs_calibration_error": max(f["max_abs_calibration_error"] for f in real),
        "required_win_rate": real[0].get("required_win_rate"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
