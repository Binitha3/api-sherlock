"""Contract validation: compare one observed response against the spec, plus
plain-language auth diagnosis for 401/403 responses.

Violations are plain dicts — {path, kind, expected, actual, message} — with
paths like `body.address.zip`, so a report can name the exact field that broke.
"""

from __future__ import annotations

import json
import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from app.services.differ import json_type

BASE_URI = "urn:api-sherlock:spec"
MAX_VIOLATIONS = 25
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

__all__ = [
    "diagnose_auth",
    "format_body_path",
    "is_json_media",
    "match_status",
    "parse_media_type",
    "validate_response",
]


# ---------------------------------------------------------------------------
# Paths and media types
# ---------------------------------------------------------------------------


def format_body_path(parts: Any) -> str:
    """Render path parts as `body.address[0].zip` (body is the schema root)."""
    out = "body"
    for part in parts:
        if isinstance(part, int):
            out += f"[{part}]"
        elif IDENTIFIER_RE.match(str(part)):
            out += f".{part}"
        else:
            out += f"[{json.dumps(str(part))}]"
    return out


def parse_media_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    media = content_type.split(";")[0].strip().lower()
    return media or None


def is_json_media(media: str | None) -> bool:
    return bool(media) and "json" in media


def match_status(responses: dict[str, Any], status: int) -> str | None:
    """Declared response for a status: exact key, `2XX` range, then `default`."""
    key = str(status)
    if key in responses:
        return key
    wildcard = f"{status // 100}XX"
    if wildcard in responses:
        return wildcard
    if wildcard.lower() in responses:
        return wildcard.lower()
    if "default" in responses:
        return "default"
    return None


def _media_matches(declared: str, actual: str | None) -> bool:
    if actual is None:
        return False
    if declared in (actual, "*/*"):
        return True
    if declared.endswith("/*"):
        return actual.startswith(declared[:-1])
    return False


# ---------------------------------------------------------------------------
# Schema normalization (OpenAPI 3.0 constructs -> JSON Schema 2020-12)
# ---------------------------------------------------------------------------


def normalize_openapi_schema(schema: Any) -> Any:
    """Copy a schema, adapting OpenAPI 3.0 constructs for a 2020-12 validator."""
    if isinstance(schema, list):
        return [normalize_openapi_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {key: normalize_openapi_schema(value) for key, value in schema.items()}
    if out.pop("nullable", False):
        kind = out.get("type")
        if isinstance(kind, str):
            out["type"] = sorted({kind, "null"})
        elif isinstance(kind, list):
            out["type"] = sorted({*kind, "null"})
    # OpenAPI 3.0 allows `minimum: 5, exclusiveMinimum: true` (draft-4 style).
    for bound, flag in (("minimum", "exclusiveMinimum"), ("maximum", "exclusiveMaximum")):
        value = out.get(flag)
        if isinstance(value, bool):
            out.pop(flag)
            if value and bound in out:
                out[flag] = out.pop(bound)
    return out


def rewrite_refs(node: Any, base: str) -> Any:
    """Make local `#/...` pointers absolute so they resolve against the full
    document registered under `base` (cyclic $refs survive inlining)."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and value.startswith("#"):
                out[key] = base + value
            else:
                out[key] = rewrite_refs(value, base)
        return out
    if isinstance(node, list):
        return [rewrite_refs(item, base) for item in node]
    return node


# ---------------------------------------------------------------------------
# Response validation
# ---------------------------------------------------------------------------


def validate_response(
    operation: dict[str, Any],
    *,
    status: int,
    content_type: str | None,
    body: bytes,
    resolved_document: dict[str, Any],
) -> list[dict[str, Any]]:
    """Validate one observed response against the operation's contract."""
    responses = operation.get("responses") or {}
    violations: list[dict[str, Any]] = []

    matched = match_status(responses, status)
    if matched is None:
        declared = ", ".join(sorted(str(key) for key in responses)) or "nothing"
        violations.append(
            {
                "path": "status",
                "kind": "status_code",
                "expected": sorted(str(key) for key in responses),
                "actual": status,
                "message": (
                    f"Status {status} is not declared in the contract (declared: {declared})."
                ),
            }
        )
        return violations

    content = (responses[matched] or {}).get("content") or {}
    if not content:
        return violations

    media = parse_media_type(content_type)
    declared_media = [str(name) for name in content]
    if not any(_media_matches(name, media) for name in declared_media):
        violations.append(
            {
                "path": "content_type",
                "kind": "content_type",
                "expected": declared_media,
                "actual": media,
                "message": (
                    f"Response content type '{media or 'none'}' does not match any declared "
                    f"type ({', '.join(declared_media)})."
                ),
            }
        )
        return violations

    matched_media = next(name for name in declared_media if _media_matches(name, media))
    schema = (content[matched_media] or {}).get("schema")
    if not isinstance(schema, dict) or not schema:
        return violations
    if not is_json_media(media):
        return violations  # non-JSON bodies are only checked for content type

    text = body.decode("utf-8", errors="replace") if body else ""
    if not text.strip():
        violations.append(
            {
                "path": "body",
                "kind": "empty_body",
                "expected": "a JSON body",
                "actual": "empty",
                "message": "Response body is empty but the contract declares a JSON schema.",
            }
        )
        return violations
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        violations.append(
            {
                "path": "body",
                "kind": "invalid_json",
                "expected": "valid JSON",
                "actual": f"unparseable (line {exc.lineno}, column {exc.colno})",
                "message": (
                    f"Response body is not valid JSON (line {exc.lineno}, column {exc.colno})."
                ),
            }
        )
        return violations

    violations.extend(_validate_json(schema, payload, resolved_document))
    if len(violations) > MAX_VIOLATIONS:
        total = len(violations)
        violations = violations[:MAX_VIOLATIONS]
        violations.append(
            {
                "path": "body",
                "kind": "truncated",
                "expected": f"at most {MAX_VIOLATIONS} violations",
                "actual": total,
                "message": (
                    f"{total} violations in total; only the first {MAX_VIOLATIONS} are listed."
                ),
            }
        )
    return violations


def _validate_json(
    schema: dict[str, Any], payload: Any, resolved_document: dict[str, Any]
) -> list[dict[str, Any]]:
    normalized = normalize_openapi_schema(schema)
    rewritten = rewrite_refs(normalized, BASE_URI)
    registry = Registry().with_resource(
        BASE_URI,
        Resource.from_contents(resolved_document or {}, default_specification=DRAFT202012),
    )
    validator = Draft202012Validator(rewritten, registry=registry)
    try:
        errors = sorted(
            validator.iter_errors(payload),
            key=lambda error: [str(part) for part in error.absolute_path],
        )
    except Exception as exc:  # noqa: BLE001 — unresolvable refs, unsupported constructs
        return [
            {
                "path": "body",
                "kind": "schema_error",
                "expected": "a schema the validator can evaluate",
                "actual": exc.__class__.__name__,
                "message": f"Could not evaluate the response schema: {exc.__class__.__name__}.",
            }
        ]
    violations: list[dict[str, Any]] = []
    for error in errors:
        violations.extend(_error_to_violations(error))
    return violations


def _error_to_violations(error: SchemaValidationError) -> list[dict[str, Any]]:
    parts = list(error.absolute_path)
    validator_name = str(error.validator)
    expected = error.validator_value
    actual = error.instance

    if validator_name == "required":
        missing_names = (
            [str(name) for name in expected if isinstance(actual, dict) and name not in actual]
            if isinstance(expected, list)
            else [str(expected)]
        )
        return [
            {
                "path": format_body_path([*parts, missing]),
                "kind": "required",
                "expected": missing,
                "actual": "missing",
                "message": (
                    f"{format_body_path([*parts, missing])}: "
                    f"required property '{missing}' is missing."
                ),
            }
            for missing in missing_names
        ]

    path = format_body_path(parts)
    if validator_name == "type":
        want = (
            "/".join(str(item) for item in expected)
            if isinstance(expected, list)
            else str(expected)
        )
        got = json_type(actual)
        return [
            {
                "path": path,
                "kind": "type",
                "expected": want,
                "actual": got,
                "message": f"{path}: expected type '{want}', got '{got}'.",
            }
        ]
    if validator_name == "enum":
        return [
            {
                "path": path,
                "kind": "enum",
                "expected": expected,
                "actual": json_type(actual),
                "message": f"{path}: observed value of type '{json_type(actual)}' is not allowed.",
            }
        ]
    return [
        {
            "path": path,
            "kind": validator_name,
            "expected": expected,
            "actual": json_type(actual),
            "message": f"{path}: response value did not satisfy '{validator_name}'.",
        }
    ]


# ---------------------------------------------------------------------------
# Auth diagnosis (401 / 403)
# ---------------------------------------------------------------------------


def _expected_auth(
    operation: dict[str, Any], resolved_document: dict[str, Any]
) -> list[dict[str, Any]]:
    components = (
        resolved_document.get("components") if isinstance(resolved_document, dict) else None
    )
    schemes = (components or {}).get("securitySchemes") or {}
    requirements = operation.get("security")
    if not isinstance(requirements, list):
        return []

    expected: list[dict[str, Any]] = []
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        for name in requirement:
            scheme = schemes.get(name) if isinstance(schemes, dict) else None
            entry: dict[str, Any] = {
                "name": name,
                "type": None,
                "scheme": None,
                "header": None,
                "in": None,
            }
            if isinstance(scheme, dict):
                entry["type"] = scheme.get("type")
                entry["scheme"] = scheme.get("scheme")
                entry["header"] = scheme.get("name")
                entry["in"] = scheme.get("in")
            expected.append(entry)
    return expected


def _describe_expected(expected: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for entry in expected:
        if entry["type"] == "apiKey":
            where = entry["in"] or "header"
            parts.append(f"API key in {where} '{entry['header'] or entry['name']}'")
        elif entry["type"] == "http" and str(entry["scheme"] or "").lower() in ("bearer", "basic"):
            parts.append(f"{str(entry['scheme']).capitalize()} credentials")
        elif entry["type"] == "oauth2":
            parts.append(f"OAuth 2.0 token ('{entry['name']}')")
        else:
            parts.append(f"security scheme '{entry['name']}'")
    unique = list(dict.fromkeys(parts))
    return "; ".join(unique) if unique else "authentication"


def _scheme_mismatch(
    expected: list[dict[str, Any]], sent_scheme: str | None, sent_header_name: str | None
) -> str | None:
    """Structural mismatches between the credentials sent and what the spec asks for."""
    if not expected or sent_scheme is None:
        return None
    api_key = next((entry for entry in expected if entry["type"] == "apiKey"), None)
    http_entries = [entry for entry in expected if entry["type"] == "http"]
    if sent_scheme == "bearer" and api_key and http_entries:
        return (
            "The environment sent a Bearer token. The spec lists either Bearer or an API key "
            f"in header '{api_key['header'] or api_key['name']}'; verify which scheme this "
            "environment requires."
        )
    http_matches = any(
        str(entry["scheme"] or "bearer").lower() == sent_scheme for entry in http_entries
    )

    if sent_scheme == "bearer" and api_key and not http_matches:
        header = api_key["header"] or "the API-key header"
        return (
            f"A Bearer token was sent, but the spec expects an API key in header '{header}'. "
            f"Set the environment auth type to api_key_header and name the header '{header}'."
        )
    if sent_scheme == "api_key" and api_key:
        wanted = api_key["header"]
        if wanted and sent_header_name and wanted.lower() != sent_header_name.lower():
            return (
                f"The API key was sent in header '{sent_header_name}' but the spec expects "
                f"'{wanted}'. Fix auth_header_name on the environment."
            )
        return None
    if sent_scheme == "api_key" and http_entries and not api_key:
        scheme_name = str(http_entries[0]["scheme"] or "bearer").capitalize()
        return (
            f"An API key was sent in header '{sent_header_name or ''}' but the spec expects "
            f"{scheme_name} credentials in the Authorization header."
        )
    if sent_scheme in ("bearer", "basic") and http_entries and not http_matches:
        wanted = str(http_entries[0]["scheme"] or "bearer").lower()
        return (
            f"{sent_scheme.capitalize()} credentials were sent but the spec expects the "
            f"'{wanted}' scheme in the Authorization header."
        )
    return None


def diagnose_auth(
    *,
    status: int,
    operation: dict[str, Any],
    resolved_document: dict[str, Any],
    auth_type: str,
    sent_scheme: str | None,
    sent_header_name: str | None,
    www_authenticate: str | None = None,
    body_text: str = "",
) -> str | None:
    """Plain-language hint explaining a 401/403, or None for other statuses."""
    if status not in (401, 403):
        return None

    expected = _expected_auth(operation, resolved_document)
    www_raw = www_authenticate or ""
    www = www_raw.lower()
    body = body_text[:2000].lower()

    if sent_scheme is None:
        if auth_type != "none":
            return (
                f"The environment is configured for auth type '{auth_type}' but no credentials "
                "were supplied with this run, so the request went out unauthenticated."
            )
        if expected:
            return (
                f"The spec requires authentication ({_describe_expected(expected)}) but no "
                "credentials were sent with this run. Add a token or API key to the run's "
                "auth field."
            )
        return (
            f"The server returned {status} but the spec declares no authentication for this "
            "operation. Check the base URL and the operation path."
        )

    mismatch = _scheme_mismatch(expected, sent_scheme, sent_header_name)
    if mismatch:
        return mismatch

    if "invalid_token" in www or "expired" in www or "expired" in body:
        return "The server reports the token is expired or invalid. Refresh or re-issue the token."
    if status == 403 and (
        "insufficient_scope" in www
        or "scope" in body
        or "permission" in body
        or "forbidden" in body
    ):
        return (
            "The credentials were accepted but do not grant the required access. "
            "Grant the scope or permission the server requires."
        )
    if not expected:
        return (
            f"Credentials were sent but the spec declares no authentication for this operation, "
            f"and the server still returned {status}. The base URL or path may be wrong."
        )
    if status == 401:
        return (
            "The server rejected the credentials (401). Check the credential value, the scheme "
            "and the header name."
        )
    return (
        "The server accepted the credentials but refused access (403). The account may lack "
        "permission for this operation."
    )
