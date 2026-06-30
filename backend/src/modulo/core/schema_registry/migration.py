"""Schema migration — detect changes between schema versions and transform data."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


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
            val = raw[0]
            assert isinstance(val, str)
            return val
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


# ---------------------------------------------------------------------------
# Schema migration registry — functions between schema versions
# ---------------------------------------------------------------------------


@dataclass
class SchemaMigration:
    """A registered migration from one schema version to another."""

    source_version: str
    target_version: str
    func: Callable[[dict[str, Any]], dict[str, Any]]
    description: str = ""


class MissingMigrationError(Exception):
    """Raised when no migration path exists between schema versions."""


class MigrationRegistry:
    """Registry of migration functions between schema versions.

    Migrations form a directed acyclic graph. The registry resolves
    multi-step chains (e.g. v1->v2->v3) via BFS, validates that a
    chain has no gaps, and applies a full chain to data.
    """

    def __init__(self) -> None:
        self._migrations: dict[tuple[str, str], SchemaMigration] = {}

    def register(
        self,
        source_version: str,
        target_version: str,
        func: Callable[[dict[str, Any]], dict[str, Any]],
        description: str = "",
    ) -> SchemaMigration:
        key = (source_version, target_version)
        if key in self._migrations:
            raise ValueError(
                f"Migration from {source_version} to {target_version} already registered"
            )
        m = SchemaMigration(
            source_version=source_version,
            target_version=target_version,
            func=func,
            description=description,
        )
        self._migrations[key] = m
        return m

    def get_migration(
        self, source_version: str, target_version: str
    ) -> SchemaMigration | None:
        return self._migrations.get((source_version, target_version))

    def get_migration_chain(
        self, source_version: str, target_version: str
    ) -> list[SchemaMigration]:
        """Return ordered list of migrations from source to target.

        Raises MissingMigrationError if no chain exists.
        """
        if source_version == target_version:
            return []

        adj: dict[str, list[SchemaMigration]] = {}
        for mf in self._migrations.values():
            adj.setdefault(mf.source_version, []).append(mf)

        visited: set[str] = set()
        queue: list[tuple[str, list[SchemaMigration]]] = [(source_version, [])]

        while queue:
            current_version, path = queue.pop(0)
            if current_version == target_version:
                return path
            if current_version in visited:
                continue
            visited.add(current_version)

            for mf in adj.get(current_version, []):
                if mf.target_version not in visited:
                    queue.append((mf.target_version, [*path, mf]))

        raise MissingMigrationError(
            f"No migration path from {source_version} to {target_version}"
        )

    def validate_chain(
        self, source_version: str, target_version: str
    ) -> list[str]:
        """Return list of gap descriptions, empty if chain is complete."""
        try:
            self.get_migration_chain(source_version, target_version)
            return []
        except MissingMigrationError:
            pass

        reachable: set[str] = set()
        q: list[str] = [source_version]
        while q:
            cur = q.pop(0)
            if cur in reachable:
                continue
            reachable.add(cur)
            for src, tgt in self._migrations:
                if src == cur:
                    q.append(tgt)

        if len(reachable) == 1:
            return [f"No outgoing migration from {source_version}"]

        def _longest_path(ver: str, visited: set[str]) -> list[str]:
            best = [ver]
            for src, tgt in self._migrations:
                if src == ver and tgt not in visited:
                    visited.add(tgt)
                    sub = _longest_path(tgt, visited)
                    if len(sub) + 1 > len(best):
                        best = [ver, *sub]
                    visited.discard(tgt)
            return best

        longest = _longest_path(source_version, {source_version})
        last = longest[-1]

        if last == source_version:
            return [f"No outgoing migration from {source_version}"]

        return [
            f"Chain reaches {last}",
            f"Missing migration from {last} towards {target_version}",
        ]

    def apply(
        self,
        data: dict[str, Any],
        source_version: str,
        target_version: str,
    ) -> dict[str, Any]:
        """Apply the full migration chain, transforming data in order."""
        chain = self.get_migration_chain(source_version, target_version)
        result = deepcopy(data)
        for mf in chain:
            result = mf.func(result)
        return result

    def clear(self) -> None:
        self._migrations.clear()

    def list_migrations(self) -> list[SchemaMigration]:
        return list(self._migrations.values())

    def __len__(self) -> int:
        return len(self._migrations)


# ---------------------------------------------------------------------------
# Helper factories — build common migration functions
# ---------------------------------------------------------------------------


def rename_field(
    old_name: str, new_name: str
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Create a migration function that renames a field."""
    def _rename(data: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(data)
        if old_name in result:
            result[new_name] = result.pop(old_name)
        return result
    _rename.__name__ = f"rename_{old_name}_to_{new_name}"
    return _rename


def convert_field(
    field_name: str,
    converter: Callable[[Any], Any],
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Create a migration function that converts a field's value type."""
    def _convert(data: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(data)
        if field_name in result:
            result[field_name] = converter(result[field_name])
        return result
    _convert.__name__ = f"convert_{field_name}"
    return _convert


def set_default(
    field_name: str,
    default: Any,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Create a migration function that adds a field with a default value."""
    def _set_default(data: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(data)
        if field_name not in result:
            result[field_name] = deepcopy(default)
        return result
    _set_default.__name__ = f"default_{field_name}"
    return _set_default


def add_field(
    field_name: str,
    value_fn: Callable[[dict[str, Any]], Any],
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Create a migration function that adds a computed field."""
    def _add_field(data: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(data)
        result[field_name] = value_fn(result)
        return result
    _add_field.__name__ = f"add_{field_name}"
    return _add_field


def remove_field(
    field_name: str,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Create a migration function that removes a field."""
    def _remove(data: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(data)
        result.pop(field_name, None)
        return result
    _remove.__name__ = f"remove_{field_name}"
    return _remove
