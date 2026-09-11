"""Read-only access to stored backtest and walk-forward runs."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import require_user

router = APIRouter(prefix="/backtests", tags=["backtests"])


@router.get("")
def list_runs(limit: int = 25, _user: dict = Depends(require_user)) -> dict:
    try:
        from app.db import get_session
        from app.models import BacktestRun

        with get_session() as session:
            rows = (
                session.query(BacktestRun)
                .order_by(BacktestRun.ts_ms.desc())
                .limit(min(limit, 100))
                .all()
            )
            return {
                "runs": [
                    {
                        "id": r.id, "ts_ms": r.ts_ms, "label": r.label, "kind": r.kind,
                        "symbols": r.symbols, "start_ms": r.start_ms, "end_ms": r.end_ms,
                        "config": r.config, "stats": r.stats,
                        "per_strategy": r.per_strategy,
                        "in_sample": r.in_sample, "out_of_sample": r.out_of_sample,
                    }
                    for r in rows
                ]
            }
    except Exception:
        return {"runs": []}
