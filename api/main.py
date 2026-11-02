"""FastAPI app factory. ``uvicorn api.main:app`` (OpenAPI at /docs).

Every ``/v1`` route requires ``X-API-Key``. ``/health`` and ``/ready`` are open.
The API never runs the pipeline itself: uploads enqueue a job and
``python -m worker`` processes it (ADR-0002).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker

from api.deps import require_api_key
from api.errors import install_error_handlers
from api.routers import health, jobs, runs, statements, transactions
from config.settings import Settings, get_settings


def create_app(
    *,
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> FastAPI:
    app = FastAPI(
        title="BSAA API",
        version="0.2.0",
        description="Bank statement analysis: upload, job status, results. Synthetic-data demo.",
    )
    app.state.settings = settings or get_settings()
    app.state.session_factory = session_factory  # None → built lazily from DATABASE_URL
    install_error_handlers(app)

    # Reject oversized uploads from the Content-Length header, before multipart
    # parsing spools the body. The route re-checks the real size (chunked
    # requests have no Content-Length). 64 KiB headroom for multipart framing.
    limit = app.state.settings.max_upload_bytes + 64 * 1024

    @app.middleware("http")
    async def _upload_size_guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        length = request.headers.get("content-length")
        if request.method == "POST" and length and length.isdigit() and int(length) > limit:
            return JSONResponse(
                {"error": {"code": "too_large", "message": "File exceeds the upload limit",
                           "details": {"max_bytes": app.state.settings.max_upload_bytes}}},
                status_code=413,
            )
        return await call_next(request)

    auth = [Depends(require_api_key)]
    app.include_router(health.router)
    for r in (statements.router, jobs.router, runs.router, transactions.router):
        app.include_router(r, dependencies=auth)
    return app


app = create_app()
