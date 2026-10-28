from __future__ import annotations

import json

from app.services.validator import diagnose_auth, validate_response

SPEC = {"components": {"securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}}}
OPERATION = {
    "security": [{"bearerAuth": []}],
    "responses": {
        "200": {
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["id", "profile"],
                        "properties": {
                            "id": {"type": "integer"},
                            "profile": {
                                "type": "object",
                                "required": ["zip"],
                                "properties": {"zip": {"type": "string"}},
                            },
                        },
                    }
                }
            }
        },
        "401": {"description": "Unauthorized"},
    },
}


def test_valid_response_passes_nested_schema() -> None:
    violations = validate_response(
        OPERATION,
        status=200,
        content_type="application/json; charset=utf-8",
        body=json.dumps({"id": 1, "profile": {"zip": "02139"}}).encode(),
        resolved_document=SPEC,
    )
    assert violations == []


def test_invalid_response_reports_nested_exact_paths() -> None:
    violations = validate_response(
        OPERATION,
        status=200,
        content_type="application/json",
        body=b'{"id":"one","profile":{}}',
        resolved_document=SPEC,
    )
    by_path = {violation["path"]: violation for violation in violations}
    assert by_path["body.id"]["expected"] == "integer"
    assert by_path["body.profile.zip"]["actual"] == "missing"


def test_status_and_content_type_are_checked() -> None:
    status = validate_response(
        OPERATION,
        status=202,
        content_type="application/json",
        body=b"{}",
        resolved_document=SPEC,
    )
    content = validate_response(
        OPERATION,
        status=200,
        content_type="text/plain",
        body=b"{}",
        resolved_document=SPEC,
    )
    assert status[0]["kind"] == "status_code"
    assert content[0]["kind"] == "content_type"


def test_auth_hints_cover_absent_wrong_expired_and_scope() -> None:
    common = {
        "operation": OPERATION,
        "resolved_document": SPEC,
        "auth_type": "bearer",
        "sent_header_name": "Authorization",
    }
    missing = diagnose_auth(status=401, sent_scheme=None, **common)
    wrong = diagnose_auth(status=401, sent_scheme="api_key", **common)
    expired = diagnose_auth(
        status=401,
        sent_scheme="bearer",
        www_authenticate='Bearer error="invalid_token"',
        **common,
    )
    scope = diagnose_auth(
        status=403,
        sent_scheme="bearer",
        www_authenticate='Bearer error="insufficient_scope"',
        **common,
    )
    assert missing and "no credentials" in missing
    assert wrong and "Bearer" in wrong
    assert expired and "expired or invalid" in expired
    assert scope and "scope or permission" in scope
