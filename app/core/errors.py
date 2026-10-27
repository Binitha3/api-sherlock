"""Error handling: every failure returns JSON with a code and a plain message."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("api_sherlock.errors")


class AppError(Exception):
    """Base class for errors that map directly to a JSON response."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "app_error",
        status_code: int = 400,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details = details

    def to_payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"error": error}


class NotFoundError(AppError):
    def __init__(self, message: str, *, resource: str = "resource") -> None:
        super().__init__(message, code="not_found", status_code=404)
        self.resource = resource


class SpecInvalidError(AppError):
    """An OpenAPI document failed validation; details carry JSON paths."""

    def __init__(
        self,
        message: str,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message, code="spec_invalid", status_code=422, details=details)


class UpstreamFetchError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="upstream_fetch_failed", status_code=422)


def error_payload(code: str, message: str, details: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return {"error": error}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {
                "path": ".".join(str(part) for part in err["loc"] if part != "body"),
                "message": err["msg"],
            }
            for err in exc.errors()
        ]
        message = details[0]["message"] if details else "Request body is invalid."
        return JSONResponse(
            status_code=400,
            content=error_payload("request_invalid", message, details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        message = str(exc.detail) if exc.detail else "Request failed."
        return JSONResponse(status_code=exc.status_code, content=error_payload(code, message))

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content=error_payload("internal_error", "Internal server error."),
        )
