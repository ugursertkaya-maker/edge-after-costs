"""FastAPI application.

Mounted at ``/cryptobot/api`` behind the shared nginx.  The API is deliberately
thin: the worker publishes a pre-rendered dashboard payload to Redis and this
process mostly hands it over, so the API container costs almost nothing on a
2-vCPU host.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.routes import auth as auth_routes
from app.routes import backtests, dashboard, health, system
from app.safety import PAPER_ONLY_BANNER, assert_paper_mode, source_tree_is_paper_only

log = logging.getLogger("cryptobot.api")


def create_app() -> FastAPI:
    s = get_settings()
    assert_paper_mode(s)

    app = FastAPI(
        title="Kripto Micro-Profit Trader",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    prefix = f"{s.base_path}/api"
    app.include_router(health.router, prefix=prefix)
    app.include_router(auth_routes.router, prefix=prefix)
    app.include_router(dashboard.router, prefix=prefix)
    app.include_router(system.router, prefix=prefix)
    app.include_router(backtests.router, prefix=prefix)

    @app.on_event("startup")
    def _startup() -> None:
        logging.basicConfig(level=logging.INFO)
        log.info(PAPER_ONLY_BANNER)
        from pathlib import Path

        violations = source_tree_is_paper_only(Path(__file__).resolve().parent)
        if violations:
            # Refusing to start is the correct response: something in the tree
            # gained the ability to talk to a trading endpoint.
            raise RuntimeError("paper-only guard failed: " + "; ".join(violations))
        try:
            from app.db import init_db

            init_db()
        except Exception as exc:
            log.warning("database not ready at startup: %s", exc)

    @app.exception_handler(Exception)
    async def _unhandled(_request, exc: Exception) -> JSONResponse:  # pragma: no cover
        log.exception("unhandled error", exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    return app


app = create_app()
