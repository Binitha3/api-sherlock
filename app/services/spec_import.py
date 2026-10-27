"""Spec import: parse, validate, resolve $refs and extract operations.

Supports OpenAPI 3.0 and 3.1 documents given as JSON or YAML, either uploaded
as a file or fetched from a URL. $ref resolution covers local references
("#/..."); external file/URL references are rejected with a clear error
(assumption: inline them or import from a single self-contained document).
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml
from jsonschema.exceptions import ValidationError as JSONSchemaValidationError
from openapi_spec_validator import validate as validate_openapi

from app.core.config import get_settings
from app.core.errors import SpecInvalidError, UpstreamFetchError

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
SUPPORTED_OPENAPI_MAJOR = ("3.0", "3.1")
MAX_REF_EXPANSIONS = 5000
MAX_REF_DEPTH = 32

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ---------------------------------------------------------------------------
# JSON path helpers
# ---------------------------------------------------------------------------


def format_json_path(parts: Any) -> str:
    """Render an iterable of path parts as a readable JSON path: $.a.b[0]["c-d"]"""
    out = "$"
    for part in parts:
        if isinstance(part, int):
            out += f"[{part}]"
        elif _IDENTIFIER_RE.match(str(part)):
            out += f".{part}"
        else:
            out += f"[{json.dumps(str(part))}]"
    return out


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_document(
    raw: bytes | str,
    *,
    filename: str | None = None,
    content_type: str | None = None,
) -> dict[str, Any]:
    """Parse a JSON or YAML OpenAPI document into a dict."""
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SpecInvalidError("Document is not valid UTF-8 text.") from exc
    else:
        text = raw

    if not text.strip():
        raise SpecInvalidError("Document is empty.")

    prefer_json = bool(content_type and "json" in content_type) or bool(
        filename and filename.lower().endswith(".json")
    )
    looks_json = text.lstrip().startswith("{")

    doc: Any = None
    json_error: str | None = None
    yaml_error: str | None = None

    if prefer_json or looks_json:
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            json_error = f"invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"

    if doc is None:
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            where = f" at line {mark.line + 1} column {mark.column + 1}" if mark else ""
            yaml_error = f"invalid YAML{where}"

    if doc is None:
        message = json_error or yaml_error or "Document could not be parsed."
        raise SpecInvalidError(message)

    if not isinstance(doc, dict):
        raise SpecInvalidError(
            f"OpenAPI document must be a JSON/YAML object, got {type(doc).__name__}."
        )
    return doc


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_document(doc: dict[str, Any]) -> None:
    """Validate an OpenAPI 3.0/3.1 document, reporting JSON paths of problems."""
    version = str(doc.get("openapi", ""))
    if not version:
        raise SpecInvalidError(
            "Missing required field 'openapi'.",
            details=[{"path": "$.openapi", "message": "field 'openapi' is required"}],
        )
    if not version.startswith(SUPPORTED_OPENAPI_MAJOR):
        raise SpecInvalidError(
            f"Unsupported OpenAPI version '{version}'. API Sherlock supports "
            f"OpenAPI {' and '.join(SUPPORTED_OPENAPI_MAJOR)}.",
            details=[{"path": "$.openapi", "message": f"unsupported version '{version}'"}],
        )

    try:
        validate_openapi(doc)
    except JSONSchemaValidationError as exc:
        raise SpecInvalidError(
            "OpenAPI document failed validation.",
            details=_validation_details(exc),
        ) from exc
    except SpecInvalidError:
        raise
    except Exception as exc:  # noqa: BLE001 — validator raises assorted internal errors
        raise SpecInvalidError(f"OpenAPI document failed validation: {exc}") from exc


def _validation_details(exc: JSONSchemaValidationError, limit: int = 20) -> list[dict[str, str]]:
    """Flatten a jsonschema error tree (with sub-errors) into path/message details."""
    details: list[dict[str, str]] = []

    def walk(error: JSONSchemaValidationError) -> None:
        if len(details) >= limit:
            return
        contexts = getattr(error, "context", None)
        if contexts:
            for sub in contexts:
                walk(sub)
            return
        details.append({"path": format_json_path(error.absolute_path), "message": error.message})

    walk(exc)
    if not details:
        details.append({"path": "$", "message": exc.message})
    return details[:limit]


# ---------------------------------------------------------------------------
# $ref resolution (local references only)
# ---------------------------------------------------------------------------


def collect_external_refs(doc: dict[str, Any]) -> list[str]:
    """Return unique $ref targets that are not local ('#...') pointers."""
    found: list[str] = []
    seen: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and not ref.startswith("#") and ref not in seen:
                seen.add(ref)
                found.append(ref)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(doc)
    return found


def _resolve_pointer(root: Any, pointer: str) -> tuple[bool, Any]:
    """Resolve a JSON pointer ('' or '/a/~1b/0'). Returns (found, value)."""
    if pointer in ("", "/"):
        return True, root
    node = root
    for raw in pointer.lstrip("/").split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            if part not in node:
                return False, None
            node = node[part]
        elif isinstance(node, list):
            if not (part.isdigit() and int(part) < len(node)):
                return False, None
            node = node[int(part)]
        else:
            return False, None
    return True, node


def reject_external_refs(doc: dict[str, Any]) -> None:
    """Raise a clear error when the document points outside itself."""
    external = collect_external_refs(doc)
    if external:
        raise SpecInvalidError(
            f"External $ref '{external[0]}' is not supported. Inline the reference "
            "or import a self-contained document.",
            details=[{"path": "$.$ref", "message": f"external reference '{external[0]}'"}],
        )


def resolve_refs(doc: dict[str, Any]) -> dict[str, Any]:
    """Inline local $refs. Cycles keep their $ref (still valid for jsonschema
    when the containing document is passed as a resource)."""
    reject_external_refs(doc)

    root = copy.deepcopy(doc)
    expansions = [MAX_REF_EXPANSIONS]

    def walk(node: Any, stack: tuple[str, ...], depth: int) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and depth <= MAX_REF_DEPTH:
                keep = {"$ref": ref}
                keep.update({k: copy.deepcopy(v) for k, v in node.items() if k != "$ref"})
                if ref in stack or expansions[0] <= 0:
                    return keep  # cycle or budget exhausted: leave the reference as-is
                expansions[0] -= 1
                found, target = _resolve_pointer(root, ref[1:])
                if not found:
                    raise SpecInvalidError(
                        f"$ref '{ref}' does not resolve to a document position.",
                        details=[{"path": "$.$ref", "message": f"unresolvable reference '{ref}'"}],
                    )
                resolved = walk(copy.deepcopy(target), stack + (ref,), depth + 1)
                if isinstance(resolved, dict):
                    merged = dict(resolved)
                    for key, value in node.items():
                        if key != "$ref":
                            merged[key] = value  # 3.1 sibling keywords win
                    return merged
                return resolved
            return {key: walk(value, stack, depth + 1) for key, value in node.items()}
        if isinstance(node, list):
            return [walk(item, stack, depth + 1) for item in node]
        return node

    return walk(root, (), 0)


# ---------------------------------------------------------------------------
# Operations extraction
# ---------------------------------------------------------------------------


def _normalize_parameter(param: Any) -> dict[str, Any] | None:
    if not isinstance(param, dict) or "name" not in param or "in" not in param:
        return None
    example = param.get("example")
    if example is None and isinstance(param.get("examples"), dict):
        for item in param["examples"].values():
            if isinstance(item, dict) and "value" in item:
                example = item["value"]
                break
    return {
        "name": str(param["name"]),
        "in": str(param["in"]),
        "required": bool(param.get("required", param.get("in") == "path")),
        "schema": param.get("schema"),
        "example": example,
        "description": param.get("description"),
    }


def _merge_parameters(shared: list[Any], own: list[Any]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in list(shared) + list(own):
        normalized = _normalize_parameter(raw)
        if normalized is None:
            continue
        merged[(normalized["name"], normalized["in"])] = normalized  # operation wins
    return list(merged.values())


def extract_operations(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract method/path/parameters/response schemas from a resolved document."""
    operations: list[dict[str, Any]] = []
    global_security = doc.get("security") or []
    paths = doc.get("paths")
    if not isinstance(paths, dict):
        return operations

    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        shared_params = path_item.get("parameters")
        if not isinstance(shared_params, list):
            shared_params = []
        for method in HTTP_METHODS:
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue

            responses: dict[str, Any] = {}
            raw_responses = op.get("responses")
            if isinstance(raw_responses, dict):
                for status, response in raw_responses.items():
                    if not isinstance(response, dict):
                        continue
                    content: dict[str, Any] = {}
                    raw_content = response.get("content")
                    if isinstance(raw_content, dict):
                        for media, media_object in raw_content.items():
                            schema = (
                                media_object.get("schema")
                                if isinstance(media_object, dict)
                                else None
                            )
                            content[str(media)] = {"schema": schema}
                    headers = response.get("headers")
                    responses[str(status)] = {
                        "description": response.get("description"),
                        "content": content,
                        "headers": sorted(headers) if isinstance(headers, dict) else [],
                    }

            request_body = op.get("requestBody")
            body_info: dict[str, Any] | None = None
            if isinstance(request_body, dict):
                raw_content = request_body.get("content")
                content_types = sorted(raw_content) if isinstance(raw_content, dict) else []
                body_info = {
                    "required": bool(request_body.get("required", False)),
                    "content_types": content_types,
                }

            op_params = op.get("parameters")
            if not isinstance(op_params, list):
                op_params = []

            operations.append(
                {
                    "operation_id": op.get("operationId") or f"{method.upper()} {path}",
                    "method": method.upper(),
                    "path": str(path),
                    "summary": op.get("summary"),
                    "parameters": _merge_parameters(shared_params, op_params),
                    "request_body": body_info,
                    "responses": responses,
                    "security": op.get("security", global_security),
                    "tags": op.get("tags") or [],
                }
            )
    return operations


# ---------------------------------------------------------------------------
# URL fetching
# ---------------------------------------------------------------------------


def _check_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SpecInvalidError(
            f"Spec URL must use http or https, got '{parsed.scheme or 'none'}'.",
            details=[{"path": "$.url", "message": "scheme must be http or https"}],
        )


async def fetch_document(url: str) -> tuple[bytes, str | None]:
    """Fetch a document over HTTP with a timeout and size cap."""
    _check_url(url)
    settings = get_settings()
    limit = settings.max_spec_bytes
    try:
        async with (
            httpx.AsyncClient(
                timeout=settings.request_timeout_seconds,
                follow_redirects=True,
                headers={"User-Agent": "api-sherlock/0.1"},
            ) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code >= 400:
                raise UpstreamFetchError(f"Spec URL returned HTTP {response.status_code}.")
            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                buffer += chunk
                if len(buffer) > limit:
                    raise SpecInvalidError(
                        f"Spec document exceeds the {limit // (1024 * 1024)} MiB limit."
                    )
            content_type = response.headers.get("content-type")
    except SpecInvalidError:
        raise
    except UpstreamFetchError:
        raise
    except httpx.HTTPError as exc:
        raise UpstreamFetchError(f"Could not fetch spec URL: {exc.__class__.__name__}.") from exc
    return bytes(buffer), content_type


# ---------------------------------------------------------------------------
# Full import pipeline
# ---------------------------------------------------------------------------


@dataclass
class ImportResult:
    openapi_version: str
    content_hash: str
    raw_document: dict[str, Any]
    resolved_document: dict[str, Any]
    operations: list[dict[str, Any]]
    title: str | None = None
    spec_version: str | None = None


def content_hash(doc: dict[str, Any]) -> str:
    canonical = json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def import_document(
    raw: bytes, *, filename: str | None = None, content_type: str | None = None
) -> ImportResult:
    """Parse -> validate -> resolve -> extract, the full Phase 1 pipeline."""
    doc = parse_document(raw, filename=filename, content_type=content_type)
    reject_external_refs(doc)  # before validation: the validator would fail with a worse message
    validate_document(doc)
    resolved = resolve_refs(doc)
    operations = extract_operations(resolved)
    info = doc.get("info") if isinstance(doc.get("info"), dict) else {}
    return ImportResult(
        openapi_version=str(doc.get("openapi", "")),
        content_hash=content_hash(doc),
        raw_document=doc,
        resolved_document=resolved,
        operations=operations,
        title=info.get("title"),
        spec_version=info.get("version"),
    )
