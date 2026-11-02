"""One error shape for every failure: ``{"error": {"code", "message", "details"}}``."""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class APIError(Exception):
    def __init__(self, status_code: int, code: str, message: str,
                 details: dict[str, Any] | list[Any] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def not_found(what: str, ident: object) -> APIError:
    return APIError(404, "not_found", f"{what} not found", {"id": str(ident)})


def _body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def _api_error(_req: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(_body(exc.code, exc.message, exc.details), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation(_req: Request, exc: RequestValidationError) -> JSONResponse:
        # Drop the echoed "input" so uploaded values never come back in errors.
        details = [{k: v for k, v in e.items() if k in ("loc", "msg", "type")} for e in exc.errors()]
        return JSONResponse(_body("invalid_request", "Request validation failed", details),
                            status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_req: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return JSONResponse(_body(code, str(exc.detail)), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled(_req: Request, _exc: Exception) -> JSONResponse:
        return JSONResponse(_body("internal_error", "Internal server error"), status_code=500)
