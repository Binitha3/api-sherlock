"""Application settings, read from environment variables only."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from the environment (optionally a local .env)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "API Sherlock"
    environment: str = "development"
    log_level: str = "info"

    # Database — PostgreSQL in Docker/dev, SQLite only for local tests.
    database_url: str = "postgresql+psycopg://sherlock:sherlock@localhost:5432/api_sherlock"

    # API protection (Phase 5): X-API-Key required on every route except /health.
    api_key: str = "change-me"

    # CORS: comma-separated origins allowed to call the API from the landing page.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # SSRF guard (Phase 2): true ONLY for local development against private hosts.
    allow_private_hosts: bool = False

    # Outgoing request limits.
    request_timeout_seconds: float = 10.0
    max_response_bytes: int = 2 * 1024 * 1024  # 2 MiB
    max_spec_bytes: int = 5 * 1024 * 1024  # 5 MiB for uploaded/fetched specs

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
