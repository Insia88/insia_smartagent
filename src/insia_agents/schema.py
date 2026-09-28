"""Strict JSON schemas for Claude structured outputs (``output_config.format``).

The structured-outputs schema subset requires ``additionalProperties: false``
on every object and does not support numeric/string/array constraints,
``default`` values or recursive schemas. ``output_schema`` turns a pydantic
model into that subset:

- inlines every ``$ref`` (the models are not recursive; recursion raises),
- sets ``additionalProperties: false`` and marks every property required,
- strips unsupported keywords (``default``, ``title``, ``minimum`` …),
- rejects free-form dict fields (``additionalProperties`` with a schema).
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel

UNSUPPORTED_KEYS = frozenset({
    "default", "title", "examples", "example", "readOnly", "writeOnly", "deprecated",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern",
    "minItems", "maxItems", "uniqueItems", "contains", "minContains", "maxContains",
    "minProperties", "maxProperties", "patternProperties", "propertyNames", "dependentRequired",
    "discriminator",
})
SUPPORTED_FORMATS = frozenset({"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"})


class SchemaError(ValueError):
    pass


def _resolve_ref(ref: str, defs: dict[str, Any]) -> dict[str, Any]:
    prefix = "#/$defs/"
    if not ref.startswith(prefix) or ref[len(prefix):] not in defs:
        raise SchemaError(f"unsupported $ref {ref!r}")
    return defs[ref[len(prefix):]]


def _clean(node: Any, defs: dict[str, Any], stack: tuple[str, ...]) -> Any:
    if isinstance(node, list):
        return [_clean(item, defs, stack) for item in node]
    if not isinstance(node, dict):
        return node

    if "$ref" in node:
        ref = node["$ref"]
        if ref in stack:
            raise SchemaError(f"recursive schema via {ref} is not supported by structured outputs")
        target = copy.deepcopy(_resolve_ref(ref, defs))
        siblings = {k: v for k, v in node.items() if k != "$ref"}
        target.update(siblings)  # e.g. a field-level description next to the $ref
        return _clean(target, defs, stack + (ref,))

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in UNSUPPORTED_KEYS or key == "$defs":
            continue
        if key == "format" and value not in SUPPORTED_FORMATS:
            continue
        if key == "properties":
            out[key] = {name: _clean(sub, defs, stack) for name, sub in value.items()}
        elif key in ("items", "anyOf", "allOf", "oneOf", "prefixItems"):
            out["anyOf" if key == "oneOf" else key] = _clean(value, defs, stack)
        elif key == "additionalProperties":
            if value not in (False, None):
                raise SchemaError("free-form dict fields are not supported; use a list of objects instead")
        else:
            out[key] = _clean(value, defs, stack)

    is_object = out.get("type") == "object" or "properties" in out
    if is_object:
        props = out.get("properties", {})
        out["type"] = "object"
        out["properties"] = props
        out["required"] = list(props)
        out["additionalProperties"] = False
    return out


def output_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Strict JSON schema for ``model`` usable as ``output_config.format.schema``."""
    raw = model.model_json_schema(mode="validation")
    defs = raw.get("$defs", {})
    return _clean(raw, defs, ())


def json_format(model: type[BaseModel]) -> dict[str, Any]:
    """The full ``output_config.format`` value for ``model``."""
    return {"type": "json_schema", "schema": output_schema(model)}


def iter_objects(schema: Any):
    """Yield every object-typed sub-schema (used by tests and sanity checks)."""
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            yield schema
        for value in schema.values():
            yield from iter_objects(value)
    elif isinstance(schema, list):
        for item in schema:
            yield from iter_objects(item)
