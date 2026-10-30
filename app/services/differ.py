"""Infer privacy-safe response shapes and compare OpenAPI/observed schemas."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# Keep at most this many array elements / object samples per node.
MAX_SAMPLES = 50
# An enum candidate needs this many observations before it is trusted...
MIN_ENUM_SAMPLES = 4
# ...and must be small and show repetition (a unique id is not an enum).
MAX_ENUM_VALUES = 8

_COUNT_KEY = "_sample_count"


def json_type(value: Any) -> str:
    """JSON type name of a decoded JSON value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def _scalar(value: Any) -> dict[str, Any]:
    kind = json_type(value)
    if kind == "null":
        return {"type": "null", _COUNT_KEY: 1}
    schema: dict[str, Any] = {"type": kind, _COUNT_KEY: 1}
    if kind in ("string", "integer", "boolean"):
        # Candidate only; _finalize decides whether it survives.
        schema["enum"] = [value]
    return schema


def _count(schema: dict[str, Any]) -> int:
    value = schema.get(_COUNT_KEY, 1)
    return value if isinstance(value, int) and value > 0 else 1


def _infer(value: Any, depth: int = 0) -> dict[str, Any]:
    if depth > 32:
        return {"type": json_type(value), _COUNT_KEY: 1}
    if isinstance(value, dict):
        properties = {str(key): _infer(item, depth + 1) for key, item in value.items()}
        required = [str(key) for key in value]
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            _COUNT_KEY: 1,
        }
    if isinstance(value, list):
        items = [_infer(item, depth + 1) for item in value[:MAX_SAMPLES]]
        merged = _merge_many(items)
        return {"type": "array", "items": merged if merged is not None else {}, _COUNT_KEY: 1}
    return _scalar(value)


def _merge_many(schemas: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not schemas:
        return None
    out = schemas[0]
    for schema in schemas[1:]:
        out = merge_schemas(out, schema)
    return out


def merge_schemas(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Union of two inferred schemas (used for array items and object keys).

    Severity-relevant details: object keys seen in every sample stay required,
    types are unioned (so a field observed as null once becomes nullable), and
    enum candidates are unioned only when both sides carry one.
    """
    if a == b:
        identical = dict(a)
        identical[_COUNT_KEY] = _count(a) + _count(b)
        return identical

    type_a, type_b = a.get("type"), b.get("type")

    if type_a == "object" and type_b == "object":
        props_a = a.get("properties") or {}
        props_b = b.get("properties") or {}
        keys = list(dict.fromkeys([*props_a, *props_b]))
        properties: dict[str, Any] = {}
        for key in keys:
            if key in props_a and key in props_b:
                properties[key] = merge_schemas(props_a[key], props_b[key])
            else:
                # Seen in only one sample: keep its shape, it is optional now.
                single = props_a[key] if key in props_a else props_b[key]
                properties[key] = _finalize(single)
        required = sorted(set(a.get("required") or []) & set(b.get("required") or []))
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            _COUNT_KEY: _count(a) + _count(b),
        }

    if type_a == "array" and type_b == "array":
        items_a = a.get("items") or {}
        items_b = b.get("items") or {}
        items = merge_schemas(items_a, items_b) if items_a and items_b else items_a or items_b
        return {"type": "array", "items": items, _COUNT_KEY: _count(a) + _count(b)}

    types = sorted({str(t) for t in (type_a, type_b) if t is not None})
    merged: dict[str, Any] = {"type": types[0] if len(types) == 1 else types}
    merged[_COUNT_KEY] = _count(a) + _count(b)

    enum_a = a.get("enum")
    enum_b = b.get("enum")
    if isinstance(enum_a, list) and isinstance(enum_b, list):
        union: list[Any] = []
        for item in [*enum_a, *enum_b]:
            if item not in union:
                union.append(item)
        merged["enum"] = union
    # One side without an enum candidate: not enough signal, drop it.

    # Carry nullability of arrays nested under an optional-only merge.
    for key in ("items", "properties", "required"):
        if key in a and key not in merged:
            merged[key] = a[key]
        if key in b and key not in merged:
            merged[key] = b[key]
    return merged


def _finalize(schema: dict[str, Any]) -> dict[str, Any]:
    """Strip bookkeeping and keep only enum candidates that qualified."""
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in (_COUNT_KEY, "enum"):
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: _finalize(prop) for name, prop in value.items()}
        elif key == "items" and isinstance(value, dict):
            out[key] = _finalize(value)
        else:
            out[key] = value

    enum = schema.get("enum")
    if isinstance(enum, list):
        kind = schema.get("type")
        scalarish = isinstance(kind, str) and kind in ("string", "integer", "boolean")
        observed = _count(schema)
        if (
            scalarish
            and len(enum) > 1
            and len(enum) <= MAX_ENUM_VALUES
            and observed >= MIN_ENUM_SAMPLES
            and len(enum) < observed  # repeated values: ids and timestamps drop out
        ):
            out["enum"] = list(enum)
    return out


def infer_schema(value: Any) -> dict[str, Any]:
    """Infer a JSON-Schema-shaped description of one observed JSON value."""
    return _finalize(_infer(value))


def infer_observed_responses(status: int, content_type: str | None, payload: Any) -> dict[str, Any]:
    """Observed shape of one response, keyed by status code."""
    return {
        str(status): {
            "content_type": content_type,
            "schema": infer_schema(payload),
        }
    }


Severity = Literal["breaking", "warning", "non_breaking"]


@dataclass(frozen=True)
class Change:
    """One contract change, with a stable machine-readable classification."""

    path: str
    kind: str
    severity: Severity
    old: Any
    new: Any
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "severity": self.severity,
            "old": self.old,
            "new": self.new,
            "message": self.message,
        }


def _change(
    path: str,
    kind: str,
    severity: Severity,
    old: Any,
    new: Any,
    message: str,
) -> Change:
    return Change(path, kind, severity, old, new, message)


def _schema_type(schema: dict[str, Any]) -> set[str]:
    value = schema.get("type")
    types = (
        {value}
        if isinstance(value, str)
        else {str(item) for item in value}
        if isinstance(value, list)
        else set()
    )
    if schema.get("nullable") is True:
        types.add("null")
    return types


def _display(value: Any) -> str:
    if isinstance(value, list):
        return " | ".join(str(item) for item in value)
    return str(value)


def diff(
    old_schema: dict[str, Any],
    new_schema: dict[str, Any],
    *,
    path: str = "body",
    context: Literal["response", "request"] = "response",
) -> list[Change]:
    """Compare two JSON/OpenAPI schemas and classify consumer-impacting changes.

    `context="request"` treats new required request fields as breaking and new
    enum values as non-breaking; response enum additions are warnings because a
    client may not handle an unfamiliar value.
    """
    changes: list[Change] = []
    old_schema = old_schema if isinstance(old_schema, dict) else {}
    new_schema = new_schema if isinstance(new_schema, dict) else {}
    old_types, new_types = _schema_type(old_schema), _schema_type(new_schema)

    if old_types != new_types and (old_types or new_types):
        added_types = new_types - old_types
        removed_types = old_types - new_types
        nullable_added = "null" in added_types and "null" not in old_types
        nullable_removed = "null" in removed_types
        if nullable_added:
            changes.append(
                _change(
                    path,
                    "nullable_added",
                    "breaking",
                    sorted(old_types),
                    sorted(new_types),
                    f"{path}: field made nullable.",
                )
            )
        elif nullable_removed:
            changes.append(
                _change(
                    path,
                    "nullable_removed",
                    "non_breaking",
                    sorted(old_types),
                    sorted(new_types),
                    f"{path}: field is no longer nullable.",
                )
            )
        else:
            changes.append(
                _change(
                    path,
                    "type_changed",
                    "breaking",
                    sorted(old_types),
                    sorted(new_types),
                    f"{path}: type changed from {_display(sorted(old_types))} to "
                    f"{_display(sorted(new_types))}.",
                )
            )
        # Recurse when a schema union retains a compatible object/array branch.
        common = old_types & new_types
        if common:
            old_branch = dict(old_schema, type=next(iter(common)))
            new_branch = dict(new_schema, type=next(iter(common)))
            changes.extend(diff(old_branch, new_branch, path=path, context=context))
        return changes

    if old_schema.get("format") != new_schema.get("format") and (
        "format" in old_schema or "format" in new_schema
    ):
        changes.append(
            _change(
                path,
                "format_changed",
                "warning",
                old_schema.get("format"),
                new_schema.get("format"),
                f"{path}: format changed from {old_schema.get('format')} to "
                f"{new_schema.get('format')}.",
            )
        )

    for bound in (
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
    ):
        if old_schema.get(bound) != new_schema.get(bound) and (
            bound in old_schema or bound in new_schema
        ):
            changes.append(
                _change(
                    path,
                    "range_changed",
                    "warning",
                    old_schema.get(bound),
                    new_schema.get(bound),
                    f"{path}: numeric/string/array constraint '{bound}' changed.",
                )
            )

    old_enum = old_schema.get("enum")
    new_enum = new_schema.get("enum")
    if isinstance(old_enum, list) and isinstance(new_enum, list):
        removed = [item for item in old_enum if item not in new_enum]
        added = [item for item in new_enum if item not in old_enum]
        if removed:
            changes.append(
                _change(
                    path,
                    "enum_value_removed",
                    "breaking",
                    removed,
                    new_enum,
                    f"{path}: enum value(s) removed: {', '.join(map(str, removed))}.",
                )
            )
        if added:
            severity: Severity = "non_breaking" if context == "request" else "warning"
            changes.append(
                _change(
                    path,
                    "enum_value_added",
                    severity,
                    old_enum,
                    added,
                    f"{path}: new enum value(s) added: {', '.join(map(str, added))}.",
                )
            )
    elif isinstance(old_enum, list) and "enum" not in new_schema:
        changes.append(
            _change(
                path,
                "enum_removed",
                "non_breaking",
                old_enum,
                None,
                f"{path}: enum constraint removed.",
            )
        )

    old_props = old_schema.get("properties") or {}
    new_props = new_schema.get("properties") or {}
    old_required = set(old_schema.get("required") or [])
    new_required = set(new_schema.get("required") or [])
    if isinstance(old_props, dict) and isinstance(new_props, dict):
        for name in sorted(old_props.keys() - new_props.keys()):
            severity: Severity = "breaking" if context == "response" else "warning"
            changes.append(
                _change(
                    f"{path}.{name}",
                    "field_removed",
                    severity,
                    old_props[name],
                    None,
                    f"{path}.{name}: field removed.",
                )
            )
        for name in sorted(new_props.keys() - old_props.keys()):
            required = name in new_required
            if context == "request" and required:
                changes.append(
                    _change(
                        f"{path}.{name}",
                        "required_added",
                        "breaking",
                        None,
                        new_props[name],
                        f"{path}.{name}: required request field added.",
                    )
                )
            else:
                changes.append(
                    _change(
                        f"{path}.{name}",
                        "field_added",
                        "non_breaking",
                        None,
                        new_props[name],
                        f"{path}.{name}: optional field added.",
                    )
                )
        for name in sorted(old_props.keys() & new_props.keys()):
            changes.extend(
                diff(old_props[name], new_props[name], path=f"{path}.{name}", context=context)
            )
            if name in old_required and name not in new_required:
                severity = "breaking" if context == "response" else "non_breaking"
                changes.append(
                    _change(
                        f"{path}.{name}",
                        "required_removed",
                        severity,
                        True,
                        False,
                        f"{path}.{name}: required property removed.",
                    )
                )
            elif name not in old_required and name in new_required and context == "request":
                changes.append(
                    _change(
                        f"{path}.{name}",
                        "required_added",
                        "breaking",
                        False,
                        True,
                        f"{path}.{name}: request field is now required.",
                    )
                )

    old_items, new_items = old_schema.get("items"), new_schema.get("items")
    if isinstance(old_items, dict) and isinstance(new_items, dict):
        old_item_types, new_item_types = _schema_type(old_items), _schema_type(new_items)
        if old_item_types != new_item_types and (old_item_types or new_item_types):
            changes.append(
                _change(
                    f"{path}[]",
                    "array_item_type_changed",
                    "breaking",
                    sorted(old_item_types),
                    sorted(new_item_types),
                    f"{path}[]: array item type changed from "
                    f"{_display(sorted(old_item_types))} to {_display(sorted(new_item_types))}.",
                )
            )
        else:
            changes.extend(diff(old_items, new_items, path=f"{path}[]", context=context))
    elif old_items != new_items and (old_items is not None or new_items is not None):
        changes.append(
            _change(
                f"{path}[]",
                "array_items_changed",
                "breaking",
                old_items,
                new_items,
                f"{path}[]: array item schema changed.",
            )
        )

    if old_schema.get("description") != new_schema.get("description") and (
        "description" in old_schema or "description" in new_schema
    ):
        changes.append(
            _change(
                path,
                "description_changed",
                "non_breaking",
                old_schema.get("description"),
                new_schema.get("description"),
                f"{path}: description changed.",
            )
        )
    return changes


def _resolve_schema_refs(
    schema: Any,
    document: dict[str, Any] | None,
    stack: tuple[str, ...] = (),
) -> Any:
    """Resolve local schema references for defensive direct service callers.

    Imported specs are already expanded; this also handles references left in
    recursive schemas without looping forever.
    """
    if isinstance(schema, list):
        return [_resolve_schema_refs(item, document, stack) for item in schema]
    if not isinstance(schema, dict):
        return schema
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/") and document is not None and ref not in stack:
        target: Any = document
        try:
            for part in ref[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
        except (KeyError, IndexError, TypeError):
            target = None
        if isinstance(target, dict):
            merged = _resolve_schema_refs(target, document, stack + (ref,))
            if isinstance(merged, dict):
                merged = dict(merged)
                merged.update(
                    {
                        key: _resolve_schema_refs(value, document, stack)
                        for key, value in schema.items()
                        if key != "$ref"
                    }
                )
                return merged
    return {
        key: _resolve_schema_refs(value, document, stack)
        for key, value in schema.items()
        if key != "$ref"
    }


def _operation_key(operation: dict[str, Any]) -> tuple[str, str]:
    return (str(operation.get("method", "GET")).upper(), str(operation.get("path", "/")))


def _operation_label(operation: dict[str, Any]) -> str:
    method, path = _operation_key(operation)
    return f"{method} {path}"


def diff_operations(
    old_operations: list[dict[str, Any]],
    new_operations: list[dict[str, Any]],
    *,
    old_document: dict[str, Any] | None = None,
    new_document: dict[str, Any] | None = None,
) -> list[Change]:
    """Compare extracted operations from two resolved OpenAPI documents."""
    old_by_key = {_operation_key(op): op for op in old_operations}
    new_by_key = {_operation_key(op): op for op in new_operations}
    changes: list[Change] = []
    for key in sorted(old_by_key.keys() - new_by_key.keys()):
        operation = old_by_key[key]
        label = _operation_label(operation)
        changes.append(
            _change(
                label,
                "operation_removed",
                "breaking",
                operation.get("operation_id"),
                None,
                f"{label}: operation removed.",
            )
        )
    for key in sorted(new_by_key.keys() - old_by_key.keys()):
        operation = new_by_key[key]
        label = _operation_label(operation)
        changes.append(
            _change(
                label,
                "operation_added",
                "non_breaking",
                None,
                operation.get("operation_id"),
                f"{label}: operation added.",
            )
        )

    for key in sorted(old_by_key.keys() & new_by_key.keys()):
        old, new = old_by_key[key], new_by_key[key]
        label = _operation_label(old)
        old_schemes = (old_document or {}).get("components", {}).get("securitySchemes", {})
        new_schemes = (new_document or {}).get("components", {}).get("securitySchemes", {})
        old_security = old.get("security") or []
        new_security = new.get("security") or []
        used_old = {name for requirement in old_security for name in requirement}
        used_new = {name for requirement in new_security for name in requirement}
        old_security_contract = {name: old_schemes.get(name) for name in sorted(used_old)}
        new_security_contract = {name: new_schemes.get(name) for name in sorted(used_new)}
        if old_security != new_security or old_security_contract != new_security_contract:
            changes.append(
                _change(
                    f"{label} -> security",
                    "security_changed",
                    "breaking",
                    {"requirements": old_security, "schemes": old_security_contract},
                    {"requirements": new_security, "schemes": new_security_contract},
                    f"{label}: authentication requirements or schemes changed.",
                )
            )

        old_params = {
            (str(p.get("in", "")), str(p.get("name", ""))): p for p in old.get("parameters") or []
        }
        new_params = {
            (str(p.get("in", "")), str(p.get("name", ""))): p for p in new.get("parameters") or []
        }
        for parameter in sorted(old_params.keys() - new_params.keys()):
            item = old_params[parameter]
            changes.append(
                _change(
                    f"{label} -> {item['in']} {item['name']}",
                    "parameter_removed",
                    "warning",
                    item,
                    None,
                    f"{label}: parameter '{item['name']}' removed.",
                )
            )
        for parameter in sorted(new_params.keys() - old_params.keys()):
            item = new_params[parameter]
            required = bool(item.get("required"))
            severity: Severity = "breaking" if required else "non_breaking"
            changes.append(
                _change(
                    f"{label} -> {item['in']} {item['name']}",
                    "parameter_added",
                    severity,
                    None,
                    item,
                    f"{label}: {'required' if required else 'optional'} parameter "
                    f"'{item['name']}' added.",
                )
            )
        for parameter in sorted(old_params.keys() & new_params.keys()):
            old_param, new_param = old_params[parameter], new_params[parameter]
            param_path = f"{label} -> {old_param['in']} {old_param['name']}"
            if not old_param.get("required") and new_param.get("required"):
                changes.append(
                    _change(
                        param_path,
                        "parameter_required",
                        "breaking",
                        False,
                        True,
                        f"{label}: parameter '{old_param['name']}' is now required.",
                    )
                )
            changes.extend(
                diff(
                    old_param.get("schema") or {},
                    new_param.get("schema") or {},
                    path=param_path,
                    context="request",
                )
            )

        old_body, new_body = old.get("request_body") or {}, new.get("request_body") or {}
        if not old_body and new_body:
            severity = "breaking" if new_body.get("required") else "non_breaking"
            changes.append(
                _change(
                    f"{label} -> request body",
                    "request_body_added",
                    severity,
                    None,
                    new_body,
                    f"{label}: {'required' if new_body.get('required') else 'optional'} "
                    "request body added.",
                )
            )
        elif old_body and not new_body:
            changes.append(
                _change(
                    f"{label} -> request body",
                    "request_body_removed",
                    "warning",
                    old_body,
                    None,
                    f"{label}: request body removed.",
                )
            )
        else:
            if not old_body.get("required") and new_body.get("required"):
                changes.append(
                    _change(
                        f"{label} -> request body",
                        "request_body_required",
                        "breaking",
                        False,
                        True,
                        f"{label}: request body is now required.",
                    )
                )
            old_content_types = set(old_body.get("content_types") or [])
            new_content_types = set(new_body.get("content_types") or [])
            if old_content_types != new_content_types:
                changes.append(
                    _change(
                        f"{label} -> request body content_type",
                        "request_content_type_changed",
                        "breaking",
                        sorted(old_content_types),
                        sorted(new_content_types),
                        f"{label}: request body content type changed.",
                    )
                )
            old_schemas = old_body.get("schemas") or {}
            new_schemas = new_body.get("schemas") or {}
            for media in sorted(old_schemas.keys() | new_schemas.keys()):
                changes.extend(
                    diff(
                        _resolve_schema_refs(old_schemas.get(media) or {}, old_document),
                        _resolve_schema_refs(new_schemas.get(media) or {}, new_document),
                        path=f"{label} -> request body ({media})",
                        context="request",
                    )
                )

        old_responses = old.get("responses") or {}
        new_responses = new.get("responses") or {}
        for status in sorted(old_responses.keys() - new_responses.keys()):
            changes.append(
                _change(
                    f"{label} -> {status}",
                    "status_removed",
                    "breaking",
                    old_responses[status],
                    None,
                    f"{label} -> {status}: response status removed.",
                )
            )
        for status in sorted(new_responses.keys() - old_responses.keys()):
            changes.append(
                _change(
                    f"{label} -> {status}",
                    "status_added",
                    "non_breaking",
                    None,
                    new_responses[status],
                    f"{label} -> {status}: response status added.",
                )
            )
        for status in sorted(old_responses.keys() & new_responses.keys()):
            old_content = old_responses[status].get("content") or {}
            new_content = new_responses[status].get("content") or {}
            response_path = f"{label} -> {status} -> body"
            if set(old_content) != set(new_content):
                changes.append(
                    _change(
                        f"{label} -> {status} -> content_type",
                        "content_type_changed",
                        "breaking",
                        sorted(old_content),
                        sorted(new_content),
                        f"{label} -> {status}: content type changed from "
                        f"{', '.join(sorted(old_content)) or 'none'} to "
                        f"{', '.join(sorted(new_content)) or 'none'}.",
                    )
                )
            if set(old_content) == set(new_content):
                for media in sorted(old_content.keys() & new_content.keys()):
                    changes.extend(
                        diff(
                            _resolve_schema_refs(
                                old_content[media].get("schema") or {}, old_document
                            ),
                            _resolve_schema_refs(
                                new_content[media].get("schema") or {}, new_document
                            ),
                            path=response_path,
                            context="response",
                        )
                    )
    return changes


def diff_observed(
    old_observations: dict[str, Any], new_observations: dict[str, Any]
) -> list[Change]:
    """Compare baseline/current maps keyed by `METHOD path` and status code."""
    changes: list[Change] = []
    for operation in sorted(old_observations.keys() - new_observations.keys()):
        changes.append(
            _change(
                operation,
                "operation_missing",
                "breaking",
                old_observations[operation],
                None,
                f"{operation}: no observed response in the current run.",
            )
        )
    for operation in sorted(new_observations.keys() - old_observations.keys()):
        changes.append(
            _change(
                operation,
                "operation_observed",
                "non_breaking",
                None,
                new_observations[operation],
                f"{operation}: response observed for the first time.",
            )
        )
    for operation in sorted(old_observations.keys() & new_observations.keys()):
        old_responses = old_observations[operation]
        new_responses = new_observations[operation]
        label = operation
        for status in sorted(old_responses.keys() - new_responses.keys()):
            changes.append(
                _change(
                    f"{label} -> {status}",
                    "status_removed",
                    "breaking",
                    old_responses[status],
                    None,
                    f"{label} -> {status}: response status removed.",
                )
            )
        for status in sorted(new_responses.keys() - old_responses.keys()):
            changes.append(
                _change(
                    f"{label} -> {status}",
                    "status_added",
                    "non_breaking",
                    None,
                    new_responses[status],
                    f"{label} -> {status}: response status added.",
                )
            )
        for status in sorted(old_responses.keys() & new_responses.keys()):
            old_response, new_response = old_responses[status], new_responses[status]
            path = f"{label} -> {status} -> body"
            if old_response.get("content_type") != new_response.get("content_type"):
                changes.append(
                    _change(
                        f"{label} -> {status} -> content_type",
                        "content_type_changed",
                        "breaking",
                        old_response.get("content_type"),
                        new_response.get("content_type"),
                        f"{label} -> {status}: content type changed from "
                        f"{old_response.get('content_type')} to "
                        f"{new_response.get('content_type')}.",
                    )
                )
            if old_response.get("content_type") == new_response.get("content_type"):
                changes.extend(
                    diff(
                        old_response.get("schema") or {},
                        new_response.get("schema") or {},
                        path=path,
                    )
                )
    return changes
