"""FastAPI application entrypoint: uvicorn app.main:app"""

from __future__ import annotations

import secrets

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api.routes import router
from app.core.config import get_settings
from app.core.errors import error_payload, register_exception_handlers

PUBLIC_PATHS = frozenset({"/health"})


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "Import an OpenAPI spec, run real requests and see exactly which "
            "response fields broke before they reach production."
        ),
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    register_exception_handlers(app)

    @app.middleware("http")
    async def require_api_key(request: Request, call_next):
        """Require the configured X-API-Key on every route except /health.

        CORS preflight requests are answered before this check so browsers can
        negotiate the actual request.
        """
        if request.method == "OPTIONS" or request.url.path in PUBLIC_PATHS:
            return await call_next(request)
        if not settings.api_key:
            return JSONResponse(
                status_code=503,
                content=error_payload(
                    "configuration_error",
                    "API_KEY is not configured; refusing to serve requests.",
                ),
            )
        provided = request.headers.get("x-api-key")
        if not provided or not secrets.compare_digest(provided, settings.api_key):
            return JSONResponse(
                status_code=401,
                content=error_payload(
                    "unauthorized",
                    "A valid X-API-Key header is required.",
                ),
            )
        return await call_next(request)

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["Content-Disposition"],
            max_age=600,
        )

    app.include_router(router)
    return app


app = create_app()
