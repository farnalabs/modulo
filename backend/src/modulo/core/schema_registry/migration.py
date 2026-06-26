"""Schema migration — detect changes between schema versions and transform data."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class FieldChange:
    name: str
    old_type: str | None = None
    new_type: str | None = None
    old_name: str | None = None


@dataclass
class MigrationPlan:
    field_additions: dict[str, str] = field(default_factory=dict)
    field_removals: list[str] = field(default_factory=list)
    type_changes: dict[str, FieldChange] = field(default_factory=dict)
    renames: dict[str, str] = field(default_factory=dict)


def _extract_type(prop: dict[str, Any]) -> str:
    raw = prop.get("type")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        if len(raw) == 1:
            return raw[0]
        return "mixed"
    if prop.get("oneOf") or prop.get("anyOf"):
        return "union"
    if prop.get("enum") is not None:
        return "enum"
    if prop.get("items") or prop.get("prefixItems"):
        return "array"
    if prop.get("properties") is not None:
        return "object"
    if prop.get("$ref") is not None:
        return "ref"
    return "unknown"


def _extract_properties(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    props = {}
    raw = schema.get("properties", {})
    if isinstance(raw, dict):
        for name, prop in raw.items():
            if isinstance(prop, dict):
                props[name] = prop
            else:
                props[name] = {"type": str(type(prop).__name__)}
    return props


def create_migration(from_schema: dict[str, Any], to_schema: dict[str, Any]) -> MigrationPlan:
    from_props = _extract_properties(from_schema)
    to_props = _extract_properties(to_schema)

    plan = MigrationPlan()

    from_names = set(from_props.keys())
    to_names = set(to_props.keys())

    added = to_names - from_names
    removed = from_names - to_names
    common = from_names & to_names

    for name in added:
        plan.field_additions[name] = _extract_type(to_props[name])

    for name in removed:
        plan.field_removals.append(name)

    for name in common:
        old_type = _extract_type(from_props[name])
        new_type = _extract_type(to_props[name])
        if old_type != new_type:
            plan.type_changes[name] = FieldChange(
                name=name,
                old_type=old_type,
                new_type=new_type,
            )

    _detect_renames(plan, from_props, to_props, added, removed)

    return plan


def _detect_renames(
    plan: MigrationPlan,
    from_props: dict[str, dict[str, Any]],
    to_props: dict[str, dict[str, Any]],
    added: set[str],
    removed: set[str],
) -> None:
    for removed_name in list(removed):
        old_type = _extract_type(from_props[removed_name])
        for added_name in list(added):
            new_type = _extract_type(to_props[added_name])
            if old_type == new_type and old_type not in ("unknown",):
                plan.renames[removed_name] = added_name
                plan.field_removals.remove(removed_name)
                plan.field_additions.pop(added_name, None)
                added.discard(added_name)
                removed.discard(removed_name)
                break


def apply_migration(data: dict[str, Any], plan: MigrationPlan) -> dict[str, Any]:
    result = deepcopy(data)

    for old_name, new_name in plan.renames.items():
        if old_name in result:
            result[new_name] = result.pop(old_name)

    for field_name in plan.field_removals:
        result.pop(field_name, None)

    for field_name in plan.field_additions:
        if field_name not in result:
            result[field_name] = None

    return result


def transform_field(
    data: dict[str, Any],
    field_name: str,
    transform_fn: Callable[[Any], Any],
) -> dict[str, Any]:
    result = deepcopy(data)
    if field_name in result:
        result[field_name] = transform_fn(result[field_name])
    return result
