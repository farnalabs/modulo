"""Schema validation — union types (oneOf/anyOf) and array schemas."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SchemaValidationError:
    path: str
    message: str


@dataclass
class SchemaValidationResult:
    valid: bool = True
    errors: list[SchemaValidationError] = field(default_factory=list)


_VALID_ITEM_KEYWORDS = {"oneOf", "anyOf", "allOf", "not", "if", "then", "else"}
_SIMPLE_TYPES = {"string", "number", "integer", "boolean", "null", "object", "array"}


def _normalize_type(raw: Any) -> str | None:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list) and len(raw) == 1:
        val = raw[0]
        assert isinstance(val, str)
        return val
    return None


def validate_union_schema(schema: dict[str, Any], path: str = "#") -> SchemaValidationResult:
    result = SchemaValidationResult()

    for kw in ("oneOf", "anyOf"):
        variants = schema.get(kw)
        if variants is None:
            continue
        current = f"{path}/{kw}"
        if not isinstance(variants, list):
            result.errors.append(
                SchemaValidationError(path=current, message=f"'{kw}' must be a non-empty array")
            )
            continue
        if len(variants) == 0:
            result.errors.append(
                SchemaValidationError(path=current, message=f"'{kw}' must not be empty")
            )
            continue
        if schema.get("type") is not None:
            result.errors.append(
                SchemaValidationError(
                    path=current,
                    message=f"'{kw}' must not appear alongside 'type' at the same level"
                    " — use a wrapping object or allOf instead",
                )
            )
        for i, variant in enumerate(variants):
            if not isinstance(variant, dict):
                result.errors.append(
                    SchemaValidationError(
                        path=f"{current}/{i}",
                        message=f"Each variant in '{kw}' must be a JSON Schema object, got {type(variant).__name__}",
                    )
                )
                continue
            if all(k not in variant for k in ("type", *list(_VALID_ITEM_KEYWORDS))):
                result.errors.append(
                    SchemaValidationError(
                        path=f"{current}/{i}",
                        message="Variant has no 'type' or composition keyword",
                    )
                )
            nested = validate_union_schema(variant, f"{current}/{i}")
            result.errors.extend(nested.errors)

    properties = schema.get("properties", {})
    if isinstance(properties, dict):
        for prop_name, prop_schema in properties.items():
            if isinstance(prop_schema, dict):
                nested = validate_union_schema(prop_schema, f"{path}/properties/{prop_name}")
                result.errors.extend(nested.errors)

    result.valid = len(result.errors) == 0
    return result


def _merge_results(results: list[SchemaValidationResult]) -> SchemaValidationResult:
    combined = SchemaValidationResult()
    for r in results:
        combined.errors.extend(r.errors)
    combined.valid = len(combined.errors) == 0
    return combined


def validate_array_schema(schema: dict[str, Any], path: str = "#") -> SchemaValidationResult:
    result = SchemaValidationResult()

    schema_type = _normalize_type(schema.get("type"))

    if schema_type is None:
        any_of = schema.get("anyOf") or schema.get("oneOf")
        if any_of:
            for i, v in enumerate(any_of):
                nested = validate_array_schema(v, f"{path}/anyOf/{i}")
                result.errors.extend(nested.errors)
        return result

    if schema_type != "array":
        return result

    current = f"{path}/items"
    items = schema.get("items")

    if items is None and not schema.get("contains") and not schema.get("prefixItems"):
        result.errors.append(
            SchemaValidationError(
                path=current,
                message="'items' is recommended for array schemas — "
                "add an items schema or use contains/prefixItems",
            )
        )
        result.valid = False
        return result

    if isinstance(items, dict):
        t = _normalize_type(items.get("type"))
        if t is None and not items.get("oneOf") and not items.get("anyOf") and not items.get("$ref"):
            result.errors.append(
                SchemaValidationError(
                    path=f"{current}",
                    message="Array items schema should specify 'type', oneOf/anyOf, or $ref",
                )
            )
        nested = validate_union_schema(items, str(current))
        result.errors.extend(nested.errors)
        nested = validate_array_schema(items, str(current))
        result.errors.extend(nested.errors)

    elif isinstance(items, list):
        for i, item_schema in enumerate(items):
            if not isinstance(item_schema, dict):
                result.errors.append(
                    SchemaValidationError(
                        path=f"{current}/{i}",
                        message=f"Tuple item must be a JSON Schema object, got {type(item_schema).__name__}",
                    )
                )
                continue
            nested = validate_union_schema(item_schema, f"{current}/{i}")
            result.errors.extend(nested.errors)
            nested = validate_array_schema(item_schema, f"{current}/{i}")
            result.errors.extend(nested.errors)

    contains = schema.get("contains")
    if isinstance(contains, dict):
        nested = validate_union_schema(contains, f"{path}/contains")
        result.errors.extend(nested.errors)
        nested = validate_array_schema(contains, f"{path}/contains")
        result.errors.extend(nested.errors)

    prefix_items = schema.get("prefixItems", [])
    if isinstance(prefix_items, list):
        for i, ps in enumerate(prefix_items):
            if isinstance(ps, dict):
                nested = validate_union_schema(ps, f"{path}/prefixItems/{i}")
                result.errors.extend(nested.errors)
                nested = validate_array_schema(ps, f"{path}/prefixItems/{i}")
                result.errors.extend(nested.errors)

    result.valid = len(result.errors) == 0
    return result


def validate_union_and_array(schema: dict[str, Any]) -> SchemaValidationResult:
    result = validate_union_schema(schema)
    array_result = validate_array_schema(schema)
    result.errors.extend(array_result.errors)
    result.valid = len(result.errors) == 0
    return result
