#!/usr/bin/env python3
"""Why does calibration work on synthetic data and fail on real data?

Phase 4's best cell reached a 67.5% hit rate against a 70.0% requirement and
still could not be promoted, because its probabilities were wrong by 27 points.
Isotonic calibration fitted on validation gets a max reliability error of 0.018
on synthetic data and 0.203-1.000 on real data. Something differs between the
validation window and the test window that does not differ in the synthetic
case, and the obvious candidate is that the market moved.

This measures that rather than assuming it. For each fold it reports:

* the **base rate** in train / validate / test — if the thing being predicted is
  itself rarer or commoner in test, no calibrator fitted on validation can be
  right, and this is a property of the market, not of the model;
* the **predicted-probability distribution** on validate vs test — if the model's
  scores shift, the isotonic mapping learned on one is being applied to a
  different support;
* calibration error **before and after** the calibrator, so we can see whether
  it helps, does nothing, or actively hurts;
* the same with the calibrator fitted only on the **most recent slice** of
  validation, which is the cheapest available hypothesis: if recency helps, the
  problem is drift and the fix is a shorter, closer calibration window.

    BACKTEST_CACHE_DIR=/tmp/bt-cache python scripts/calibration_diagnosis.py \\
        --symbol BTCUSD --days 120 --horizon 60 --target 0.15
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402


def describe(p: np.ndarray) -> dict:
    if p.size == 0:
        return {}
    return {
        "n": int(p.size),
        "mean": round(float(p.mean()), 4),
        "p10": round(float(np.percentile(p, 10)), 4),
        "p50": round(float(np.percentile(p, 50)), 4),
        "p90": round(float(np.percentile(p, 90)), 4),
    }


def main() -> int:
    ap = argparse.ArgumentParser(prog="calibration_diagnosis")
    ap.add_argument("--symbol", default="BTCUSD")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--horizon", type=int, default=60)
    ap.add_argument("--target", type=float, default=0.15)
    ap.add_argument("--round-trip-bps", type=float, default=6.0)
    ap.add_argument("--tape-cache", default="/tmp/tape-cache")
    ap.add_argument("--train-days", type=int, default=60)
    ap.add_argument("--validate-days", type=int, default=15)
    ap.add_argument("--test-days", type=int, default=15)
    ap.add_argument("--step-days", type=int, default=15)
    ap.add_argument("--embargo-minutes", type=int, default=60)
    ap.add_argument("--recent-frac", type=float, default=0.33,
                    help="share of the validation window used by the recency arm")
    ap.add_argument("--out", default="/tmp/phase4/calibration_diagnosis.json")
    args = ap.parse_args()

    from app.predict import calibrate as cal
    from app.predict import labels as lab
    from app.predict import matrix as mx
    from app.predict import models as mdl
    from app.predict import splits as sp
    from app.research.dataset import load_dataset

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.days * 86_400_000

    print(f"[load] {args.symbol} {args.days}d", flush=True)
    ds = load_dataset(args.symbol, start_ms, end_ms,
                      intervals=("1m", "5m", "15m"), cache_dir=args.tape_cache)
    bars = ds.bars.get("1m") or []
    print(f"[load] {len(bars):,} bars", flush=True)

    spec = lab.BarrierSpec(
        net_target_pct=args.target, stop_kind="atr", stop_value=2.0,
        horizon_minutes=args.horizon, round_trip_bps=args.round_trip_bps,
    )
    labels = lab.build_labels(bars, spec=spec, symbol=args.symbol)
    matrix = mx.build_matrix(ds, labels, cache_dir=args.tape_cache)
    windows = sp.rolling_splits(
        start_ms, end_ms,
        train_days=args.train_days, validate_days=args.validate_days,
        test_days=args.test_days, step_days=args.step_days,
        max_horizon_minutes=spec.horizon_minutes,
        embargo_minutes=args.embargo_minutes,
    )
    print(f"[splits] {len(windows)} folds", flush=True)

    folds = []
    for split in windows:
        tr, va, te = mdl.split_matrix(matrix, split,
                                      embargo_minutes=args.embargo_minutes)
        if min(len(tr), len(va), len(te)) < 200:
            continue
        reports = mdl.benchmark(matrix, split, seed=7,
                                embargo_minutes=args.embargo_minutes)
        champ, _why = mdl.select_champion(reports)
        if champ is None or champ.model is None:
            continue

        p_va = champ.model.predict_p_win(va.X)
        p_te_raw = champ.model.predict_p_win(te.X)

        raw = cal.evaluate_calibration(p_te_raw, te.y)
        full = cal.fit_calibrator(p_va, va.y, method="isotonic", keys=va.ts_ms)
        after_full = cal.evaluate_calibration(full.predict(p_te_raw), te.y)

        # Recency arm: fit only on the tail of validation, i.e. the rows closest
        # in time to the test window.
        order = np.argsort(va.ts_ms)
        cut = int(len(order) * (1.0 - args.recent_frac))
        recent = order[cut:]
        after_recent = None
        if len(recent) >= 200 and len(np.unique(va.y[recent])) == 2:
            rc = cal.fit_calibrator(p_va[recent], va.y[recent],
                                    method="isotonic", keys=va.ts_ms[recent])
            after_recent = cal.evaluate_calibration(rc.predict(p_te_raw), te.y)

        fold = {
            "split": split.name,
            "model": champ.name,
            "base_rate": {
                "train": round(float(tr.y.mean()), 4),
                "validate": round(float(va.y.mean()), 4),
                "test": round(float(te.y.mean()), 4),
            },
            "base_rate_shift_va_to_te": round(
                float(te.y.mean() - va.y.mean()), 4
            ),
            "p_validate": describe(p_va),
            "p_test": describe(p_te_raw),
            "calibration": {
                "raw_max_err": round(float(raw.max_abs_error), 4),
                "raw_brier": round(float(raw.brier), 4),
                "full_val_max_err": round(float(after_full.max_abs_error), 4),
                "full_val_brier": round(float(after_full.brier), 4),
            },
        }
        if after_recent is not None:
            fold["calibration"]["recent_val_max_err"] = round(
                float(after_recent.max_abs_error), 4)
            fold["calibration"]["recent_val_brier"] = round(
                float(after_recent.brier), 4)
        folds.append(fold)

        c = fold["calibration"]
        print(
            f"  {split.name}: base va {fold['base_rate']['validate']:.3f} "
            f"-> te {fold['base_rate']['test']:.3f} "
            f"(shift {fold['base_rate_shift_va_to_te']:+.3f})  "
            f"p̄ va {fold['p_validate']['mean']:.3f} te {fold['p_test']['mean']:.3f}  "
            f"err raw {c['raw_max_err']:.3f} "
            f"-> full {c['full_val_max_err']:.3f}"
            + (f" -> recent {c['recent_val_max_err']:.3f}"
               if "recent_val_max_err" in c else ""),
            flush=True,
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "symbol": args.symbol, "horizon": args.horizon, "target_pct": args.target,
        "folds": folds,
    }, indent=1))

    if folds:
        shifts = [abs(f["base_rate_shift_va_to_te"]) for f in folds]
        full_err = [f["calibration"]["full_val_max_err"] for f in folds]
        recent_err = [f["calibration"]["recent_val_max_err"] for f in folds
                      if "recent_val_max_err" in f["calibration"]]
        print("\n" + "=" * 66)
        print(f"mean |base-rate shift| validate -> test : {np.mean(shifts):.4f}")
        print(f"mean calibration error, full validation : {np.mean(full_err):.4f}")
        if recent_err:
            print(f"mean calibration error, recent slice    : {np.mean(recent_err):.4f}")
            better = sum(1 for f in folds
                         if f["calibration"].get("recent_val_max_err", 9)
                         < f["calibration"]["full_val_max_err"])
            print(f"folds where recency helped              : {better}/{len(folds)}")
        print("A large base-rate shift means the thing being predicted is itself")
        print("rarer or commoner in test than in validation. No calibrator fitted")
        print("on validation can be right about that, and it is a property of the")
        print("market rather than of the model.")
    print(f"[out] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
