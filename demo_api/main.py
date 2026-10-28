"""Small version-switchable API target for local API Sherlock runs."""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

app = FastAPI(title="API Sherlock Demo API", version="1.0.0")


def current_version() -> str:
    value = os.getenv("DEMO_API_VERSION", "v1").strip().lower()
    return value if value in {"v1", "v2-safe", "v2-breaking"} else "v1"


def public_user(user_id: str) -> dict[str, Any]:
    version = current_version()
    user: dict[str, Any] = {"id": user_id, "name": "Ada Lovelace", "active": True}
    if version == "v2-safe":
        user["plan"] = "free"
    if version == "v2-breaking":
        user.pop("name")
        user["active"] = "yes"
    return user


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": current_version()}


@app.get("/users/{user_id}")
def get_user(user_id: str) -> dict[str, Any]:
    return public_user(user_id)


@app.get("/users")
def list_users(limit: int = 10) -> dict[str, Any]:
    return {"items": [public_user("u-1")], "limit": limit}


@app.get("/account")
def get_account(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    if current_version() == "v2-breaking":
        api_key_name = os.getenv("DEMO_API_KEY_HEADER", "X-Demo-Key")
        expected_key = os.getenv("DEMO_API_KEY", "demo-key")
        if request.headers.get(api_key_name.lower()) != expected_key:
            raise HTTPException(
                status_code=401,
                detail=f"{api_key_name} required",
                headers={"WWW-Authenticate": "ApiKey"},
            )
    elif authorization != "Bearer demo-token":
        raise HTTPException(
            status_code=401,
            detail="Bearer token required",
            headers={"WWW-Authenticate": 'Bearer realm="demo", error="invalid_token"'},
        )
    if current_version() == "v2-breaking":
        return {"id": "acct-1", "owner": "u-1", "status": "active", "auth": "api-key"}
    return {"id": "acct-1", "owner": "u-1", "status": "active"}
