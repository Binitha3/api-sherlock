"""FastAPI application entrypoint: uvicorn app.main:app"""

from __future__ import annotations

from fastapi import FastAPI

from app import __version__
from app.api.routes import router
from app.core.config import get_settings
from app.core.errors import register_exception_handlers


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
    app.include_router(router)
    return app


app = create_app()
