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


_MAX_RECURSION_DEPTH = 50
_VALID_ITEM_KEYWORDS = {"oneOf", "anyOf", "allOf", "not", "if", "then", "else", "$ref"}
_VALID_ITEM_KEYWORDS_TUPLE = ("type", *tuple(_VALID_ITEM_KEYWORDS))


def _normalize_type(raw: Any) -> str | None:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], str):
        return raw[0]
    return None


def _validate_both(schema: dict[str, Any], path: str, _depth: int) -> SchemaValidationResult:
    result = validate_union_schema(schema, path, _depth)
    array_result = validate_array_schema(schema, path, _depth)
    result.errors.extend(array_result.errors)
    result.valid = len(result.errors) == 0
    return result


def validate_union_schema(
    schema: dict[str, Any],
    path: str = "#",
    _depth: int = 0,
) -> SchemaValidationResult:
    if _depth > _MAX_RECURSION_DEPTH:
        return SchemaValidationResult(
            valid=False,
            errors=[SchemaValidationError(path=path, message="Maximum recursion depth exceeded")],
        )

    result = SchemaValidationResult()

    has_union = False
    for kw in ("oneOf", "anyOf"):
        variants = schema.get(kw)
        if variants is None:
            continue
        has_union = True
        current = f"{path}/{kw}"
        if not isinstance(variants, list):
            result.errors.append(SchemaValidationError(path=current, message=f"'{kw}' must be a non-empty array"))
            continue
        if len(variants) == 0:
            result.errors.append(SchemaValidationError(path=current, message=f"'{kw}' must not be empty"))
            continue
        for i, variant in enumerate(variants):
            if not isinstance(variant, dict):
                result.errors.append(
                    SchemaValidationError(
                        path=f"{current}/{i}",
                        message=f"Each variant in '{kw}' must be a JSON Schema object, got {type(variant).__name__}",
                    )
                )
                continue
            if all(k not in variant for k in _VALID_ITEM_KEYWORDS_TUPLE):
                result.errors.append(
                    SchemaValidationError(
                        path=f"{current}/{i}",
                        message="Variant has no 'type', composition keyword, or $ref",
                    )
                )
            nested = _validate_both(variant, f"{current}/{i}", _depth + 1)
            result.errors.extend(nested.errors)

    if has_union and schema.get("type") is not None:
        result.errors.append(
            SchemaValidationError(
                path=path,
                message="'oneOf'/'anyOf' must not appear alongside 'type' at the same level"
                " — use a wrapping object or allOf instead",
            )
        )

    properties = schema.get("properties", {})
    if isinstance(properties, dict):
        for prop_name, prop_schema in properties.items():
            if isinstance(prop_schema, dict):
                nested = _validate_both(prop_schema, f"{path}/properties/{prop_name}", _depth + 1)
                result.errors.extend(nested.errors)

    result.valid = len(result.errors) == 0
    return result


def validate_array_schema(
    schema: dict[str, Any],
    path: str = "#",
    _depth: int = 0,
) -> SchemaValidationResult:
    if _depth > _MAX_RECURSION_DEPTH:
        return SchemaValidationResult(
            valid=False,
            errors=[SchemaValidationError(path=path, message="Maximum recursion depth exceeded")],
        )

    result = SchemaValidationResult()

    schema_type = _normalize_type(schema.get("type"))

    if schema_type is None:
        for kw in ("anyOf", "oneOf"):
            variants = schema.get(kw)
            if isinstance(variants, list):
                for i, v in enumerate(variants):
                    if not isinstance(v, dict):
                        result.errors.append(
                            SchemaValidationError(
                                path=f"{path}/{kw}/{i}",
                                message=f"Each variant in '{kw}' must be a JSON Schema object, got {type(v).__name__}",
                            )
                        )
                        continue
                    nested = validate_array_schema(v, f"{path}/{kw}/{i}", _depth + 1)
                    result.errors.extend(nested.errors)
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for prop_name, prop_schema in properties.items():
                if isinstance(prop_schema, dict):
                    nested = validate_array_schema(prop_schema, f"{path}/properties/{prop_name}", _depth + 1)
                    result.errors.extend(nested.errors)
        result.valid = len(result.errors) == 0
        return result

    if schema_type != "array":
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for prop_name, prop_schema in properties.items():
                if isinstance(prop_schema, dict):
                    nested = validate_array_schema(prop_schema, f"{path}/properties/{prop_name}", _depth + 1)
                    result.errors.extend(nested.errors)
        result.valid = len(result.errors) == 0
        return result

    current = f"{path}/items"
    items = schema.get("items")
    contains = schema.get("contains")
    prefix_items = schema.get("prefixItems")

    if items is None and contains is None and prefix_items is None:
        result.errors.append(
            SchemaValidationError(
                path=current,
                message="'items' is recommended for array schemas — add an items schema or use contains/prefixItems",
            )
        )
        return result

    if isinstance(items, dict):
        t = _normalize_type(items.get("type"))
        if t is None and not any(k in items for k in _VALID_ITEM_KEYWORDS):
            result.errors.append(
                SchemaValidationError(
                    path=f"{current}",
                    message="Array items schema should specify 'type', oneOf/anyOf/allOf, or $ref",
                )
            )
        nested = _validate_both(items, current, _depth + 1)
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
            nested = _validate_both(item_schema, f"{current}/{i}", _depth + 1)
            result.errors.extend(nested.errors)

    if isinstance(contains, dict):
        nested = _validate_both(contains, f"{path}/contains", _depth + 1)
        result.errors.extend(nested.errors)

    if isinstance(prefix_items, list):
        for i, ps in enumerate(prefix_items):
            if isinstance(ps, dict):
                nested = _validate_both(ps, f"{path}/prefixItems/{i}", _depth + 1)
                result.errors.extend(nested.errors)

    result.valid = len(result.errors) == 0
    return result


def validate_union_and_array(schema: dict[str, Any]) -> SchemaValidationResult:
    result = validate_union_schema(schema)
    array_result = validate_array_schema(schema)
    result.errors.extend(array_result.errors)
    result.valid = len(result.errors) == 0
    return result
