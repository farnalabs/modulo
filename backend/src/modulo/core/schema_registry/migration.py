"""Schema migration — detect changes between schema versions and transform data."""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger(__name__)


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
        if len(raw) == 1 and isinstance(raw[0], str):
            return raw[0]
        return "mixed"
    if prop.get("oneOf") or prop.get("anyOf"):
        return "union"
    if prop.get("enum") is not None:
        return "enum"
    if "items" in prop or "prefixItems" in prop or "contains" in prop:
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
                _log.warning("Property %s is not a schema object (got %s)", name, type(prop).__name__)
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

    plan.field_additions = {name: _extract_type(to_props[name]) for name in added}

    plan.field_removals.extend(removed)

    for name in common:
        old_type = _extract_type(from_props[name])
        new_type = _extract_type(to_props[name])
        if old_type != new_type:
            plan.type_changes[name] = FieldChange(
                name=name,
                old_type=old_type,
                new_type=new_type,
            )

    # Sort candidate names so the same-type heuristic is deterministic across
    # runs (sets/py dicts have hash-randomised iteration order).
    renames = _detect_renames(from_props, to_props, sorted(removed), sorted(added))
    for old, new in renames:
        plan.renames[old] = new
        plan.field_removals.remove(old)
        plan.field_additions.pop(new, None)

    return plan


def _detect_renames(
    from_props: dict[str, dict[str, Any]],
    to_props: dict[str, dict[str, Any]],
    removed_names: list[str],
    added_names: list[str],
) -> list[tuple[str, str]]:
    renames: list[tuple[str, str]] = []
    consumed: set[str] = set()
    for removed_name in removed_names:
        old_type = _extract_type(from_props[removed_name])
        if old_type == "unknown":
            continue
        for added_name in added_names:
            if added_name in consumed:
                continue
            new_type = _extract_type(to_props[added_name])
            if old_type == new_type:
                renames.append((removed_name, added_name))
                consumed.add(added_name)
                break
    return renames


def _detect_rename_cycles(renames: dict[str, str]) -> list[list[str]]:
    """Return rename cycles as ordered lists of old-name keys.

    A cycle ``A -> B -> A`` is returned as ``[A, B]`` (each node renames
    to the next). Keys that participate in no cycle are not returned.
    """
    cycles: list[list[str]] = []
    visited: set[str] = set()

    for start in renames:
        if start in visited:
            continue
        path: list[str] = []
        index: dict[str, int] = {}
        node = start
        while node in renames and node not in index:
            if node in visited:
                break
            index[node] = len(path)
            path.append(node)
            node = renames[node]
        if node in index:
            cycle = path[index[node] :]
            cycles.append(cycle)
            visited.update(cycle)
        else:
            visited.update(path)

    return cycles


def _apply_renames(result: dict[str, Any], renames: dict[str, str]) -> dict[str, Any]:
    """Apply a rename mapping without losing data or hanging on cycles.

    Renames are treated as a simultaneous value rotation:

    - *Cycles* (``A -> B -> A``) rotate values: each field receives the
      value of its predecessor, so ``{A: 1, B: 2}`` with ``A<->B`` becomes
      ``{A: 2, B: 1}`` instead of losing one field.
    - *Chains* are applied tail-first (reverse topological order) so a
      value that moves into a field that is itself being renamed is
      forwarded before the field is overwritten.
    """
    cycles = _detect_rename_cycles(renames)
    _apply_cycle_rotations(result, cycles)

    cyclic_nodes = {name for cycle in cycles for name in cycle}
    pending = {old: new for old, new in renames.items() if old not in cyclic_nodes}
    _apply_pending_renames(result, pending, sources=set(renames))

    return result


def _apply_cycle_rotations(result: dict[str, Any], cycles: list[list[str]]) -> None:
    """Rotate values along each rename cycle so no field is lost (see module doc)."""
    for cycle in cycles:
        _apply_single_cycle(result, cycle)


def _apply_single_cycle(result: dict[str, Any], cycle: list[str]) -> None:
    saved = {name: result.get(name) for name in cycle}
    for i, name in enumerate(cycle):
        predecessor = cycle[i - 1]
        if predecessor in result:
            result[name] = saved[predecessor]
    _log.warning(
        "Circular rename chain detected: %s -> %s; applied as value rotation",
        " -> ".join(cycle),
        cycle[0],
    )


def _apply_pending_renames(
    result: dict[str, Any],
    pending: dict[str, str],
    sources: set[str],
) -> None:
    """Apply non-cyclic renames tail-first, warning on irreducible cycles."""
    while pending:
        if not _advance_pending_renames(result, pending, sources):
            _log.warning(
                "Rename plan has an irreducible cycle: %s",
                ", ".join(f"{old} -> {new}" for old, new in pending.items()),
            )
            break


def _advance_pending_renames(
    result: dict[str, Any],
    pending: dict[str, str],
    sources: set[str],
) -> bool:
    """Apply one pass of resolvable renames. Return True if any progressed."""
    progressed = False
    for old_name, new_name in list(pending.items()):
        if new_name in pending:
            continue
        if old_name in result:
            if new_name in result and new_name not in sources:
                _log.warning(
                    "Rename %s -> %s: target field already exists, overwriting",
                    old_name,
                    new_name,
                )
            result[new_name] = result.pop(old_name)
        del pending[old_name]
        progressed = True
    return progressed


def apply_migration(data: dict[str, Any], plan: MigrationPlan) -> dict[str, Any]:
    result = deepcopy(data)

    if plan.renames:
        result = _apply_renames(result, plan.renames)

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


def _longest_path(start: str, migrations: dict[tuple[str, str], SchemaMigration]) -> list[str]:
    """Return the longest simple version path starting at *start*.

    Depth-first enumeration over the migration graph, never revisiting a
    version within a path (cycles are skipped). Used by ``validate_chain``
    for gap descriptions and by ``get_partial_chain`` to build the
    best-effort partial chain when a full chain to the target has a gap.
    """
    best: list[str] = [start]
    stack: list[tuple[str, list[str], int]] = [(start, [start], 0)]
    while stack:
        ver, path, idx = stack.pop()
        items = [tgt for (src, tgt) in migrations if src == ver]
        if idx < len(items):
            stack.append((ver, path, idx + 1))
            tgt = items[idx]
            if tgt not in path:
                new_path = [*path, tgt]
                if len(new_path) > len(best):
                    best = new_path
                stack.append((tgt, new_path, 0))
    return best


def _build_step_reports(
    chain: list[SchemaMigration],
    data: dict[str, Any],
) -> list[dict[str, Any]]:
    """Simulate a migration chain and return per-step field diffs.

    The original *data* is never modified.
    """
    steps: list[dict[str, Any]] = []
    current = deepcopy(data)
    for mf in chain:
        before = deepcopy(current)
        current = mf.func(current)
        added = sorted(set(current) - set(before))
        removed = sorted(set(before) - set(current))
        changed = {}
        for key in set(current) & set(before):
            if current[key] != before[key]:
                changed[key] = {"old": before[key], "new": current[key]}
        steps.append(
            {
                "source_version": mf.source_version,
                "target_version": mf.target_version,
                "description": mf.description or mf.func.__name__,
                "added_fields": added,
                "removed_fields": removed,
                "changed_fields": changed,
            }
        )
    return steps


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
        self._lock = asyncio.Lock()

    async def register(
        self,
        source_version: str,
        target_version: str,
        func: Callable[[dict[str, Any]], dict[str, Any]],
        description: str = "",
    ) -> SchemaMigration:
        async with self._lock:
            key = (source_version, target_version)
            if key in self._migrations:
                raise ValueError(f"Migration from {source_version} to {target_version} already registered")
            m = SchemaMigration(
                source_version=source_version,
                target_version=target_version,
                func=func,
                description=description,
            )
            self._migrations[key] = m
            return m

    def get_migration(self, source_version: str, target_version: str) -> SchemaMigration | None:
        return self._migrations.get((source_version, target_version))

    async def get_migration_chain(self, source_version: str, target_version: str) -> list[SchemaMigration]:
        if source_version == target_version:
            return []

        async with self._lock:
            adj: dict[str, list[SchemaMigration]] = {}
            for mf in self._migrations.values():
                adj.setdefault(mf.source_version, []).append(mf)

        visited: set[str] = set()
        queue: deque[tuple[str, list[SchemaMigration]]] = deque([(source_version, [])])

        while queue:
            current_version, path = queue.popleft()
            if current_version == target_version:
                return path
            if current_version in visited:
                continue
            visited.add(current_version)

            for mf in adj.get(current_version, []):
                if mf.target_version not in visited:
                    queue.append((mf.target_version, [*path, mf]))

        raise MissingMigrationError(f"No migration path from {source_version} to {target_version}")

    async def validate_chain(self, source_version: str, target_version: str) -> list[str]:
        """Return list of gap descriptions, empty if chain is complete."""
        try:
            await self.get_migration_chain(source_version, target_version)
            return []
        except MissingMigrationError:
            pass

        async with self._lock:
            migrations_copy = dict(self._migrations)

        reachable: set[str] = set()
        q: deque[str] = deque([source_version])
        while q:
            cur = q.popleft()
            if cur in reachable:
                continue
            reachable.add(cur)
            for src, tgt in migrations_copy:
                if src == cur:
                    q.append(tgt)

        if len(reachable) == 1:
            return [f"No outgoing migration from {source_version}"]

        longest = _longest_path(source_version, migrations_copy)
        last = longest[-1]

        if last == source_version:
            return [f"No outgoing migration from {source_version}"]

        return [
            f"Chain reaches {last}",
            f"Missing migration from {last} towards {target_version}",
        ]

    async def get_partial_chain(
        self,
        source_version: str,
        target_version: str,
    ) -> tuple[list[SchemaMigration], list[str]]:
        """Return ``(partial_chain, gaps)`` — best-effort migration fallback.

        When a full chain from *source_version* to *target_version* exists,
        returns ``(full_chain, [])``. When a migration step is missing, the
        longest reachable prefix of the chain is returned alongside the
        human-readable gap descriptions from :meth:`validate_chain`, so
        callers can degrade gracefully instead of failing hard on a partial
        chain. Never raises :class:`MissingMigrationError`.
        """
        try:
            return await self.get_migration_chain(source_version, target_version), []
        except MissingMigrationError:
            pass

        gaps = await self.validate_chain(source_version, target_version)

        async with self._lock:
            migrations_copy = dict(self._migrations)

        path = _longest_path(source_version, migrations_copy)
        chain: list[SchemaMigration] = []
        for src, tgt in itertools.pairwise(path):
            mf = migrations_copy.get((src, tgt))
            if mf is None:
                break
            chain.append(mf)
        return chain, gaps

    async def apply_partial(
        self,
        data: dict[str, Any],
        source_version: str,
        target_version: str,
    ) -> tuple[dict[str, Any], list[str]]:
        """Apply the best-effort partial chain without raising.

        Returns ``(migrated_data, gaps)``. ``gaps`` is empty when the chain
        is complete. When a migration step is missing, the reachable prefix
        is still applied and the missing steps are reported instead of
        failing the whole migration; when the source is unreachable the data
        passes through unchanged.
        """
        chain, gaps = await self.get_partial_chain(source_version, target_version)
        result = deepcopy(data)
        for mf in chain:
            result = mf.func(result)
        return result, gaps

    async def describe_partial_chain(
        self,
        source_version: str,
        target_version: str,
    ) -> tuple[list[dict[str, str]], list[str]]:
        """Describe the best-effort partial chain without running it.

        Returns ``(step_descriptions, gaps)`` — the reachable prefix is
        described and the missing steps reported instead of raising.
        """
        chain, gaps = await self.get_partial_chain(source_version, target_version)
        return [
            {
                "source_version": m.source_version,
                "target_version": m.target_version,
                "description": m.description or m.func.__name__,
            }
            for m in chain
        ], gaps

    async def dry_run_partial(
        self,
        data: dict[str, Any],
        source_version: str,
        target_version: str,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Best-effort dry-run: per-step diff of the reachable prefix + gaps.

        Returns ``(steps, gaps)``. ``steps`` contains the per-step field
        diffs for the applied prefix; the original data is never modified.
        Missing steps are reported in ``gaps`` instead of raising.
        """
        chain, gaps = await self.get_partial_chain(source_version, target_version)
        return _build_step_reports(chain, data), gaps

    async def apply(
        self,
        data: dict[str, Any],
        source_version: str,
        target_version: str,
    ) -> dict[str, Any]:
        """Apply the full migration chain, transforming data in order."""
        chain = await self.get_migration_chain(source_version, target_version)
        result = deepcopy(data)
        for mf in chain:
            result = mf.func(result)
        return result

    async def describe_chain(self, source_version: str, target_version: str) -> list[dict[str, str]]:
        """Return descriptions of each step in a migration chain without running it."""
        chain = await self.get_migration_chain(source_version, target_version)
        return [
            {
                "source_version": m.source_version,
                "target_version": m.target_version,
                "description": m.description or m.func.__name__,
            }
            for m in chain
        ]

    async def dry_run(
        self,
        data: dict[str, Any],
        source_version: str,
        target_version: str,
    ) -> list[dict[str, Any]]:
        """Simulate the full migration chain and return per-step diff.

        Each step report shows:
        - source/target version
        - description
        - which fields were added, removed, or changed
        - the before/after values for changed fields

        The original data is never modified.
        """
        chain = await self.get_migration_chain(source_version, target_version)
        return _build_step_reports(chain, data)

    def clear(self) -> None:
        self._migrations.clear()

    def list_migrations(self) -> list[SchemaMigration]:
        return list(self._migrations.values())

    def __len__(self) -> int:
        return len(self._migrations)


# ---------------------------------------------------------------------------
# Helper factories — build common migration functions
# ---------------------------------------------------------------------------


def rename_field(old_name: str, new_name: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
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
