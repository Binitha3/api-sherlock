from __future__ import annotations

from app.services.differ import diff, diff_observed, diff_operations


def test_response_schema_rules_and_nested_paths() -> None:
    changes = diff(
        {
            "type": "object",
            "required": ["id", "name", "nullable"],
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "nullable": {"type": "string"},
                "nested": {"type": "object", "properties": {"zip": {"type": "string"}}},
            },
        },
        {
            "type": "object",
            "required": ["id", "nullable"],
            "properties": {
                "id": {"type": "integer"},
                "nullable": {"type": ["string", "null"]},
                "nested": {"type": "object", "properties": {"zip": {"type": "integer"}}},
            },
        },
        path="GET /users/{id} -> 200 -> body",
    )
    by_kind_path = {(change.kind, change.path): change.severity for change in changes}
    assert by_kind_path[("field_removed", "GET /users/{id} -> 200 -> body.name")] == "breaking"
    assert by_kind_path[("type_changed", "GET /users/{id} -> 200 -> body.id")] == "breaking"
    assert by_kind_path[("nullable_added", "GET /users/{id} -> 200 -> body.nullable")] == "breaking"
    assert by_kind_path[("type_changed", "GET /users/{id} -> 200 -> body.nested.zip")] == "breaking"


def test_required_removed_optional_added_and_array_item_change() -> None:
    changes = diff(
        {
            "type": "object",
            "required": ["nested"],
            "properties": {
                "nested": {
                    "type": "object",
                    "required": ["zip"],
                    "properties": {"zip": {"type": "string"}},
                },
                "items": {"type": "array", "items": {"type": "string"}},
            },
        },
        {
            "type": "object",
            "required": ["nested"],
            "properties": {
                "nested": {"type": "object", "properties": {"zip": {"type": "string"}}},
                "items": {"type": "array", "items": {"type": "integer"}},
                "extra": {"type": "boolean"},
            },
        },
    )
    assert {(change.kind, change.path, change.severity) for change in changes} == {
        ("required_removed", "body.nested.zip", "breaking"),
        ("array_item_type_changed", "body.items[]", "breaking"),
        ("field_added", "body.extra", "non_breaking"),
    }


def test_enum_description_format_and_numeric_range_rules() -> None:
    changes = diff(
        {
            "type": "string",
            "enum": ["active", "deleted", "new"],
            "format": "date",
            "minimum": 1,
            "description": "Old",
        },
        {
            "type": "string",
            "enum": ["active", "deleted", "paused"],
            "format": "date-time",
            "minimum": 2,
            "description": "New",
        },
    )
    assert {(change.kind, change.severity) for change in changes} == {
        ("format_changed", "warning"),
        ("range_changed", "warning"),
        ("enum_value_removed", "breaking"),
        ("enum_value_added", "warning"),
        ("description_changed", "non_breaking"),
    }


def test_request_context_marks_new_required_fields_and_params_breaking() -> None:
    changes = diff(
        {"type": "object", "properties": {"mode": {"type": "string", "enum": ["fast"]}}},
        {
            "type": "object",
            "required": ["mode", "token"],
            "properties": {
                "mode": {"type": "string", "enum": ["fast", "safe"]},
                "token": {"type": "string"},
            },
        },
        context="request",
    )
    assert {(change.kind, change.severity) for change in changes} == {
        ("enum_value_added", "non_breaking"),
        ("required_added", "breaking"),
    }
    assert any(
        change.kind == "required_added" and change.path == "body.token" for change in changes
    )


def test_spec_diff_handles_parameters_status_content_and_auth() -> None:
    old = [
        {
            "method": "GET",
            "path": "/items",
            "parameters": [
                {"name": "old", "in": "query", "required": False, "schema": {"type": "string"}}
            ],
            "request_body": None,
            "responses": {
                "200": {"content": {"application/json": {"schema": {"type": "string"}}}},
                "404": {"content": {}},
            },
            "security": [{"bearer": []}],
        }
    ]
    new = [
        {
            "method": "GET",
            "path": "/items",
            "parameters": [
                {"name": "new", "in": "query", "required": True, "schema": {"type": "string"}}
            ],
            "request_body": None,
            "responses": {
                "200": {"content": {"text/plain": {"schema": {"type": "string"}}}},
                "201": {"content": {"application/json": {"schema": {"type": "string"}}}},
            },
            "security": [{"key": []}],
        }
    ]
    changes = diff_operations(
        old,
        new,
        old_document={
            "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}}
        },
        new_document={
            "components": {
                "securitySchemes": {"key": {"type": "apiKey", "in": "header", "name": "X-Key"}}
            }
        },
    )
    assert {(change.kind, change.severity) for change in changes} == {
        ("parameter_removed", "warning"),
        ("parameter_added", "breaking"),
        ("status_removed", "breaking"),
        ("status_added", "non_breaking"),
        ("content_type_changed", "breaking"),
        ("security_changed", "breaking"),
    }


def test_schema_diff_resolves_local_refs() -> None:
    old_op = [
        {
            "method": "GET",
            "path": "/users",
            "parameters": [],
            "responses": {
                "200": {
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/User"}}
                    }
                }
            },
        }
    ]
    new_op = [
        {
            "method": "GET",
            "path": "/users",
            "parameters": [],
            "responses": {
                "200": {
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/User"}}
                    }
                }
            },
        }
    ]
    changes = diff_operations(
        old_op,
        new_op,
        old_document={
            "components": {
                "schemas": {"User": {"type": "object", "properties": {"id": {"type": "string"}}}}
            }
        },
        new_document={
            "components": {
                "schemas": {"User": {"type": "object", "properties": {"id": {"type": "integer"}}}}
            }
        },
    )
    assert len(changes) == 1
    assert changes[0].path == "GET /users -> 200 -> body.id"
    assert changes[0].severity == "breaking"


def test_live_diff_exact_path_for_nested_type_change() -> None:
    old = {
        "GET /users/{id}": {
            "200": {
                "content_type": "application/json",
                "schema": {
                    "type": "object",
                    "properties": {
                        "address": {"type": "object", "properties": {"zip": {"type": "string"}}}
                    },
                },
            }
        }
    }
    new = {
        "GET /users/{id}": {
            "200": {
                "content_type": "application/json",
                "schema": {
                    "type": "object",
                    "properties": {
                        "address": {"type": "object", "properties": {"zip": {"type": "integer"}}}
                    },
                },
            }
        }
    }
    changes = diff_observed(old, new)
    assert len(changes) == 1
    assert changes[0].path == "GET /users/{id} -> 200 -> body.address.zip"
    assert changes[0].severity == "breaking"
