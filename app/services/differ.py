"""Response-shape inference for baselines.

The pure diff() engine arrives in Phase 3. What lives here already is the
inference the runner needs: observed responses are reduced to a
JSON-Schema-shaped dict (field names, types, nullability, array item types and
small enum candidates) so raw response bodies — which may contain personal
data — are never stored. Only structure is kept.
"""

from __future__ import annotations

from typing import Any

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
        required = [str(key) for key, item in value.items() if item is not None]
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
        return dict(a)

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
