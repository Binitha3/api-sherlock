"""Execute spec operations against a live API and collect per-operation results.

Safety limits: 10s timeout, 2 MiB response cap, no automatic redirect to a
different host, and an SSRF check on every hop (see app.core.security).
Auth secrets live only in the AuthContext passed for the duration of a run —
they are never stored or logged.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx

from app.core.config import get_settings
from app.core.security import BlockedTargetError, DnsLookupError, assert_safe_url
from app.services.differ import infer_observed_responses
from app.services.validator import (
    diagnose_auth,
    is_json_media,
    parse_media_type,
    validate_response,
)

logger = logging.getLogger("api_sherlock.runner")

MAX_REDIRECTS = 5
USER_AGENT = "api-sherlock/0.1"
DIAGNOSIS_BODY_BYTES = 2000

__all__ = ["AuthContext", "Prepared", "build_request", "run_operations"]


# ---------------------------------------------------------------------------
# Auth (in-memory only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthContext:
    """Credentials for one run: built from the environment plus per-run secrets."""

    auth_type: str = "none"
    header_name: str | None = None
    token: str | None = None
    api_key: str | None = None
    username: str | None = None
    password: str | None = None

    @property
    def scheme(self) -> str | None:
        """Kind of credential actually available to send, or None."""
        if self.auth_type == "bearer" and self.token:
            return "bearer"
        if self.auth_type == "api_key_header" and self.api_key:
            return "api_key"
        if self.auth_type == "basic" and self.username is not None:
            return "basic"
        return None

    @property
    def sent_header_name(self) -> str | None:
        scheme = self.scheme
        if scheme in ("bearer", "basic"):
            return "Authorization"
        if scheme == "api_key":
            return self.header_name or "X-API-Key"
        return None

    @property
    def headers(self) -> dict[str, str]:
        scheme = self.scheme
        if scheme == "bearer":
            return {"Authorization": f"Bearer {self.token}"}
        if scheme == "api_key":
            return {self.header_name or "X-API-Key": str(self.api_key)}
        if scheme == "basic":
            raw = f"{self.username}:{self.password or ''}".encode()
            return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
        return {}


# ---------------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Prepared:
    method: str
    url: str
    params: dict[str, Any]
    headers: dict[str, str]
    cookies: dict[str, str]


@dataclass(frozen=True)
class Skip:
    code: str
    reason: str


def _param_value(param: dict[str, Any], overrides: dict[str, Any]) -> tuple[bool, Any]:
    """Value for a parameter: run override, then example, then schema default,
    then the first enum value."""
    name = param.get("name")
    if isinstance(name, str) and name in overrides:
        return True, overrides[name]
    if param.get("example") is not None:
        return True, param["example"]
    schema = param.get("schema")
    if isinstance(schema, dict):
        if "default" in schema:
            return True, schema["default"]
        enum = schema.get("enum")
        if isinstance(enum, list) and enum:
            return True, enum[0]
    return False, None


def build_request(
    operation: dict[str, Any],
    base_url: str,
    parameter_values: dict[str, dict[str, Any]] | None,
    auth: AuthContext,
) -> tuple[Prepared | None, Skip | None]:
    """Build one request from the spec, or explain why the operation is skipped."""
    request_body = operation.get("request_body")
    if isinstance(request_body, dict) and request_body.get("required"):
        return None, Skip(
            "request_body",
            "The operation requires a request body, which is not inferred; send it manually.",
        )

    overrides: dict[str, Any] = {}
    if parameter_values:
        operation_id = operation.get("operation_id")
        key = f"{operation.get('method')} {operation.get('path')}"
        raw = parameter_values.get(operation_id) if operation_id else None
        if raw is None:
            raw = parameter_values.get(key)
        if isinstance(raw, dict):
            overrides = raw

    path = str(operation.get("path") or "")
    params: dict[str, Any] = {}
    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}

    for param in operation.get("parameters") or []:
        name = param.get("name")
        location = param.get("in")
        if not isinstance(name, str) or location not in ("path", "query", "header", "cookie"):
            return None, Skip(
                "parameter_unsupported",
                f"Parameter '{name}' uses unsupported location '{location}'.",
            )
        found, value = _param_value(param, overrides)
        if not found or value is None:
            if param.get("required"):
                return None, Skip(
                    "parameter_uninferable",
                    f"Required {location} parameter '{name}' has no example or default to send.",
                )
            continue
        if location == "path":
            token = "{" + name + "}"
            if token not in path:
                return None, Skip(
                    "parameter_uninferable",
                    f"Path parameter '{name}' is missing from the path template '{path}'.",
                )
            path = path.replace(token, quote(str(value), safe=""))
        elif location == "query":
            params[name] = value
        elif location == "header":
            headers[name] = str(value)
        else:
            cookies[name] = str(value)

    if "{" in path or "}" in path:
        return None, Skip(
            "parameter_uninferable",
            f"Path template '{operation.get('path')}' has parameters that were not filled in.",
        )

    url = base_url.rstrip("/") + "/" + path.lstrip("/")
    headers.update(auth.headers)  # credentials win over example header params
    return (
        Prepared(
            method=str(operation.get("method") or "GET"),
            url=url,
            params=params,
            headers=headers,
            cookies=cookies,
        ),
        None,
    )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    status: int | None = None
    content_type: str | None = None
    body: bytes = b""
    www_authenticate: str | None = None
    latency_ms: float = 0.0
    error_code: str | None = None
    error_message: str | None = None


async def _execute(
    client: httpx.AsyncClient,
    prepared: Prepared,
    *,
    base_url: str,
    max_bytes: int,
    timeout_seconds: float,
) -> Outcome:
    original_origin = _origin(base_url)
    url = prepared.url
    current_params = prepared.params

    for _hop in range(MAX_REDIRECTS + 1):
        try:
            assert_safe_url(url)
        except (BlockedTargetError, DnsLookupError) as exc:
            return Outcome(error_code=exc.code, error_message=exc.message)

        started = time.perf_counter()
        try:
            async with client.stream(
                prepared.method,
                url,
                params=current_params or None,
                headers=prepared.headers or None,
                cookies=prepared.cookies or None,
            ) as response:
                buffer = bytearray()
                too_large = False
                async for chunk in response.aiter_bytes():
                    buffer += chunk
                    if len(buffer) > max_bytes:
                        too_large = True
                        break
                latency_ms = (time.perf_counter() - started) * 1000

                if too_large:
                    return Outcome(
                        latency_ms=latency_ms,
                        error_code="response_too_large",
                        error_message=(
                            f"Response exceeded the {max_bytes // (1024 * 1024)} MiB limit; "
                            "the body was not validated."
                        ),
                    )

                location = response.headers.get("location")
                if 300 <= response.status_code < 400 and location:
                    target = urljoin(url, location)
                    target_origin = _origin(target)
                    if target_origin != original_origin:
                        target_host = target_origin[1]
                        return Outcome(
                            status=response.status_code,
                            latency_ms=latency_ms,
                            error_code="redirect_blocked",
                            error_message=(
                                "Refused to follow a redirect to a different host "
                                f"('{target_host or 'unknown'}')."
                            ),
                        )
                    url = target
                    current_params = {}  # the Location header carries its own query
                    continue

                return Outcome(
                    status=response.status_code,
                    content_type=response.headers.get("content-type"),
                    body=bytes(buffer),
                    www_authenticate=response.headers.get("www-authenticate"),
                    latency_ms=latency_ms,
                )
        except httpx.TimeoutException:
            return Outcome(
                error_code="timeout",
                error_message=f"Request timed out after {timeout_seconds:g}s.",
            )
        except httpx.ConnectError:
            return Outcome(
                error_code="connection_failed",
                error_message="Could not connect to the host.",
            )
        except httpx.HTTPError as exc:
            return Outcome(
                error_code="request_failed",
                error_message=f"Request failed ({exc.__class__.__name__}).",
            )
    return Outcome(
        error_code="too_many_redirects",
        error_message=f"More than {MAX_REDIRECTS} redirects.",
    )


# ---------------------------------------------------------------------------
# Per-operation orchestration
# ---------------------------------------------------------------------------


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlparse(url)
    default_port = 443 if parsed.scheme == "https" else 80
    try:
        port = parsed.port or default_port
    except ValueError:
        port = None
    return (parsed.scheme.lower(), (parsed.hostname or "").lower(), port)


def _result_base(operation: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation_id": operation.get("operation_id"),
        "method": operation.get("method"),
        "path": operation.get("path"),
    }


def _observed(outcome: Outcome) -> dict[str, Any] | None:
    media = parse_media_type(outcome.content_type)
    if not is_json_media(media) or not outcome.body:
        return None
    try:
        payload = json.loads(outcome.body.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    if outcome.status is None:
        return None
    return infer_observed_responses(outcome.status, media, payload)


def _filter_operations(
    operations: list[dict[str, Any]], include: list[str] | None
) -> list[dict[str, Any]]:
    if not include:
        return list(operations)
    wanted = set(include)
    return [
        operation
        for operation in operations
        if operation.get("operation_id") in wanted
        or f"{operation.get('method')} {operation.get('path')}" in wanted
    ]


async def _run_one(
    client: httpx.AsyncClient,
    operation: dict[str, Any],
    *,
    base_url: str,
    parameter_values: dict[str, dict[str, Any]] | None,
    auth: AuthContext,
    resolved_document: dict[str, Any],
    max_bytes: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    base = _result_base(operation)
    prepared, skip = build_request(operation, base_url, parameter_values, auth)

    if skip is not None or prepared is None:
        reason = skip or Skip("request_invalid", "The request could not be built.")
        return base | {
            "status": "skipped",
            "http_status": None,
            "latency_ms": None,
            "content_type": None,
            "violations": [],
            "auth_hint": None,
            "error_code": reason.code,
            "error_message": reason.reason,
            "observed_schema": None,
        }

    outcome = await _execute(
        client,
        prepared,
        base_url=base_url,
        max_bytes=max_bytes,
        timeout_seconds=timeout_seconds,
    )
    if outcome.error_code:
        return base | {
            "status": "failed",
            "http_status": outcome.status,
            "latency_ms": round(outcome.latency_ms, 1),
            "content_type": None,
            "violations": [],
            "auth_hint": None,
            "error_code": outcome.error_code,
            "error_message": outcome.error_message,
            "observed_schema": None,
        }

    violations = validate_response(
        operation,
        status=outcome.status or 0,
        content_type=outcome.content_type,
        body=outcome.body,
        resolved_document=resolved_document,
    )

    auth_hint = None
    if outcome.status in (401, 403):
        auth_hint = diagnose_auth(
            status=outcome.status,
            operation=operation,
            resolved_document=resolved_document,
            auth_type=auth.auth_type,
            sent_scheme=auth.scheme,
            sent_header_name=auth.sent_header_name,
            www_authenticate=outcome.www_authenticate,
            body_text=outcome.body[:DIAGNOSIS_BODY_BYTES].decode("utf-8", errors="replace"),
        )

    logger.info(
        "%s %s -> %s in %.1fms, %d violations",
        base["method"],
        base["path"],
        outcome.status,
        outcome.latency_ms,
        len(violations),
    )
    return base | {
        "status": "failed" if violations else "passed",
        "http_status": outcome.status,
        "latency_ms": round(outcome.latency_ms, 1),
        "content_type": outcome.content_type,
        "violations": violations,
        "auth_hint": auth_hint,
        "error_code": None,
        "error_message": None,
        "observed_schema": _observed(outcome),
    }


async def run_operations(
    operations: list[dict[str, Any]],
    *,
    base_url: str,
    resolved_document: dict[str, Any],
    include: list[str] | None = None,
    parameter_values: dict[str, dict[str, Any]] | None = None,
    auth: AuthContext | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """Run every selected operation sequentially and return result dicts shaped
    like the run_results table. `client` allows tests to inject a transport."""
    settings = get_settings()
    auth = auth or AuthContext()
    selected = _filter_operations(operations, include)
    assert_safe_url(base_url)

    owns_client = client is None
    active = client or httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": USER_AGENT},
    )
    results: list[dict[str, Any]] = []
    try:
        for operation in selected:
            results.append(
                await _run_one(
                    active,
                    operation,
                    base_url=base_url,
                    parameter_values=parameter_values,
                    auth=auth,
                    resolved_document=resolved_document,
                    max_bytes=settings.max_response_bytes,
                    timeout_seconds=settings.request_timeout_seconds,
                )
            )
    finally:
        if owns_client:
            await active.aclose()
    return results
