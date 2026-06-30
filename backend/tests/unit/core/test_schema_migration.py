"""Unit tests for schema migration."""

from copy import deepcopy
from typing import Any

import pytest

from modulo.core.schema_registry.migration import (
    MigrationPlan,
    MigrationRegistry,
    MissingMigrationError,
    SchemaMigration,
    add_field,
    apply_migration,
    convert_field,
    create_migration,
    remove_field,
    rename_field,
    set_default,
    transform_field,
)


class TestCreateMigration:
    def test_detect_added_fields(self) -> None:
        old = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
        }
        new = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "email": {"type": "string"},
                "age": {"type": "integer"},
            },
        }
        plan = create_migration(old, new)
        assert "email" in plan.field_additions
        assert "age" in plan.field_additions
        assert plan.field_additions["email"] == "string"
        assert plan.field_additions["age"] == "integer"
        assert plan.field_removals == []
        assert plan.type_changes == {}
        assert plan.renames == {}

    def test_detect_removed_fields(self) -> None:
        old = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "legacy": {"type": "string"},
            },
        }
        new = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
        }
        plan = create_migration(old, new)
        assert plan.field_removals == ["legacy"]

    def test_detect_type_changes(self) -> None:
        old = {
            "type": "object",
            "properties": {
                "count": {"type": "string"},
            },
        }
        new = {
            "type": "object",
            "properties": {
                "count": {"type": "integer"},
            },
        }
        plan = create_migration(old, new)
        assert "count" in plan.type_changes
        assert plan.type_changes["count"].old_type == "string"
        assert plan.type_changes["count"].new_type == "integer"

    def test_detect_renames(self) -> None:
        old = {
            "type": "object",
            "properties": {
                "full_name": {"type": "string"},
            },
        }
        new = {
            "type": "object",
            "properties": {
                "display_name": {"type": "string"},
            },
        }
        plan = create_migration(old, new)
        assert "full_name" in plan.renames
        assert plan.renames["full_name"] == "display_name"
        assert "full_name" not in plan.field_removals
        assert "display_name" not in plan.field_additions

    def test_rename_does_not_match_different_types(self) -> None:
        old = {
            "type": "object",
            "properties": {
                "count": {"type": "integer"},
            },
        }
        new = {
            "type": "object",
            "properties": {
                "count_str": {"type": "string"},
            },
        }
        plan = create_migration(old, new)
        assert plan.renames == {}
        assert "count_str" in plan.field_additions
        assert "count" in plan.field_removals

    def test_no_changes(self) -> None:
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        plan = create_migration(schema, schema)
        assert plan.field_additions == {}
        assert plan.field_removals == []
        assert plan.type_changes == {}
        assert plan.renames == {}

    def test_handles_missing_properties(self) -> None:
        plan = create_migration({"type": "object"}, {"type": "object"})
        assert plan.field_additions == {}
        assert plan.field_removals == []
        assert plan.type_changes == {}
        assert plan.renames == {}

    def test_detects_union_type(self) -> None:
        old = {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
            },
        }
        new = {
            "type": "object",
            "properties": {
                "value": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "integer"},
                    ]
                },
            },
        }
        plan = create_migration(old, new)
        assert "value" in plan.type_changes
        assert plan.type_changes["value"].old_type == "string"
        assert plan.type_changes["value"].new_type == "union"

    def test_detects_array_type(self) -> None:
        old = {
            "type": "object",
            "properties": {
                "tags": {"type": "string"},
            },
        }
        new = {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        }
        plan = create_migration(old, new)
        assert "tags" in plan.type_changes
        assert plan.type_changes["tags"].old_type == "string"
        assert plan.type_changes["tags"].new_type == "array"


class TestApplyMigration:
    def test_apply_additions(self) -> None:
        plan = MigrationPlan(field_additions={"email": "string", "age": "integer"})
        data = {"name": "Alice"}
        result = apply_migration(data, plan)
        assert result == {"name": "Alice", "email": None, "age": None}

    def test_apply_removals(self) -> None:
        plan = MigrationPlan(field_removals=["legacy", "old_field"])
        data = {"name": "Alice", "legacy": "old", "old_field": "val", "keep": "stay"}
        result = apply_migration(data, plan)
        assert result == {"name": "Alice", "keep": "stay"}

    def test_apply_renames(self) -> None:
        plan = MigrationPlan(renames={"full_name": "display_name"})
        data = {"full_name": "Alice", "age": 30}
        result = apply_migration(data, plan)
        assert "full_name" not in result
        assert result["display_name"] == "Alice"
        assert result["age"] == 30

    def test_apply_idempotent(self) -> None:
        plan = MigrationPlan(
            field_additions={"email": "string"},
            field_removals=["legacy"],
            renames={"old": "new"},
        )
        data = {"name": "Alice", "legacy": "x", "old": "y"}
        first = apply_migration(data, plan)
        second = apply_migration(first, plan)
        assert first == second

    def test_apply_does_not_mutate_original(self) -> None:
        plan = MigrationPlan(field_removals=["secret"])
        data = {"name": "Alice", "secret": "s3cret"}
        result = apply_migration(data, plan)
        assert "secret" not in result
        assert "secret" in data

    def test_apply_empty_plan(self) -> None:
        plan = MigrationPlan()
        data = {"name": "Alice"}
        result = apply_migration(data, plan)
        assert result == data
        assert result is not data


class TestTransformField:
    def test_transform_existing_field(self) -> None:
        data = {"count": "42"}
        result = transform_field(data, "count", int)
        assert result == {"count": 42}
        assert data == {"count": "42"}

    def test_transform_missing_field_noop(self) -> None:
        data = {"name": "Alice"}
        result = transform_field(data, "missing", int)
        assert result == data
        assert result is not data


# ---------------------------------------------------------------------------
# Migration registry
# ---------------------------------------------------------------------------


class TestMigrationRegistry:
    def test_register_and_retrieve(self) -> None:
        registry = MigrationRegistry()

        def _migrate(data: dict[str, Any]) -> dict[str, Any]:
            return {**data, "version": "2.0.0"}

        m = registry.register("1.0.0", "2.0.0", _migrate, "Upgrade to v2")
        assert isinstance(m, SchemaMigration)
        assert m.source_version == "1.0.0"
        assert m.target_version == "2.0.0"
        assert m.description == "Upgrade to v2"

        retrieved = registry.get_migration("1.0.0", "2.0.0")
        assert retrieved is not None
        assert retrieved.func is _migrate

    def test_register_duplicate_raises(self) -> None:
        registry = MigrationRegistry()
        registry.register("1.0.0", "2.0.0", lambda d: d)
        with pytest.raises(ValueError, match="already registered"):
            registry.register("1.0.0", "2.0.0", lambda d: d)

    def test_register_multiple_versions(self) -> None:
        registry = MigrationRegistry()
        registry.register("1.0.0", "2.0.0", lambda d: d)
        registry.register("2.0.0", "3.0.0", lambda d: d)
        assert len(registry) == 2

    def test_get_migration_nonexistent(self) -> None:
        registry = MigrationRegistry()
        assert registry.get_migration("1.0.0", "9.9.9") is None

    def test_clear(self) -> None:
        registry = MigrationRegistry()
        registry.register("1.0.0", "2.0.0", lambda d: d)
        registry.clear()
        assert len(registry) == 0

    def test_list_migrations(self) -> None:
        registry = MigrationRegistry()
        registry.register("1.0.0", "2.0.0", lambda d: d, "first")
        registry.register("2.0.0", "3.0.0", lambda d: d, "second")
        migrations = registry.list_migrations()
        assert len(migrations) == 2
        descriptions = {m.description for m in migrations}
        assert descriptions == {"first", "second"}


class TestMigrationChain:
    """Field rename migration — v1 uses 'full_name', v2 uses 'display_name'."""

    def test_single_step_v1_to_v2(self) -> None:
        registry = MigrationRegistry()
        registry.register("1.0.0", "2.0.0", rename_field("full_name", "display_name"))

        data = {"full_name": "Alice", "age": 30}
        result = registry.apply(data, "1.0.0", "2.0.0")
        assert result == {"display_name": "Alice", "age": 30}
        assert "full_name" not in result

    def test_rename_does_not_mutate_original(self) -> None:
        registry = MigrationRegistry()
        registry.register("1.0.0", "2.0.0", rename_field("full_name", "display_name"))

        data = {"full_name": "Alice"}
        result = registry.apply(data, "1.0.0", "2.0.0")
        assert result == {"display_name": "Alice"}
        assert data == {"full_name": "Alice"}

    def test_rename_missing_field_noop(self) -> None:
        registry = MigrationRegistry()
        registry.register("1.0.0", "2.0.0", rename_field("full_name", "display_name"))

        data = {"age": 30}
        result = registry.apply(data, "1.0.0", "2.0.0")
        assert result == data


class TestTypeConversionMigration:
    """Type conversion — v1 stores 'count' as string, v2 stores as int."""

    def test_convert_string_to_int(self) -> None:
        registry = MigrationRegistry()
        registry.register("1.0.0", "2.0.0", convert_field("count", int))

        data = {"count": "42", "name": "Alice"}
        result = registry.apply(data, "1.0.0", "2.0.0")
        assert result == {"count": 42, "name": "Alice"}
        assert isinstance(result["count"], int)

    def test_convert_with_custom_function(self) -> None:
        registry = MigrationRegistry()

        def _to_bool(val: Any) -> bool:
            return val.lower() in ("true", "yes", "1")

        registry.register("1.0.0", "2.0.0", convert_field("active", _to_bool))

        data = {"active": "true", "name": "Alice"}
        result = registry.apply(data, "1.0.0", "2.0.0")
        assert result["active"] is True

        data2 = {"active": "false", "name": "Bob"}
        result2 = registry.apply(data2, "1.0.0", "2.0.0")
        assert result2["active"] is False

    def test_convert_missing_field_noop(self) -> None:
        registry = MigrationRegistry()
        registry.register("1.0.0", "2.0.0", convert_field("missing", int))

        data = {"name": "Alice"}
        result = registry.apply(data, "1.0.0", "2.0.0")
        assert result == data


class TestChainedMigrations:
    """Chained migrations v1->v2->v3."""

    def test_two_step_chain(self) -> None:
        registry = MigrationRegistry()
        registry.register("1.0.0", "2.0.0", rename_field("full_name", "display_name"))
        registry.register("2.0.0", "3.0.0", convert_field("count", int))

        data = {"full_name": "Alice", "count": "42"}
        result = registry.apply(data, "1.0.0", "3.0.0")
        assert result == {"display_name": "Alice", "count": 42}
        assert isinstance(result["count"], int)

    def test_three_step_chain(self) -> None:
        registry = MigrationRegistry()
        registry.register("1.0.0", "2.0.0", rename_field("full_name", "display_name"))
        registry.register("2.0.0", "2.5.0", set_default("middle_name", ""))
        registry.register("2.5.0", "3.0.0", convert_field("count", int))

        data = {"full_name": "Alice", "count": "42"}
        result = registry.apply(data, "1.0.0", "3.0.0")
        assert result == {"display_name": "Alice", "count": 42, "middle_name": ""}
        assert isinstance(result["count"], int)

    def test_chain_from_intermediate_version(self) -> None:
        registry = MigrationRegistry()
        registry.register("1.0.0", "2.0.0", rename_field("full_name", "display_name"))
        registry.register("2.0.0", "3.0.0", convert_field("count", int))

        data = {"display_name": "Alice", "count": "42"}
        result = registry.apply(data, "2.0.0", "3.0.0")
        assert result == {"display_name": "Alice", "count": 42}

    def test_same_version_returns_data(self) -> None:
        registry = MigrationRegistry()
        data = {"name": "Alice"}
        result = registry.apply(data, "1.0.0", "1.0.0")
        assert result == data
        assert result is not data


class TestMissingMigration:
    """Missing migration detection."""

    def test_missing_intermediate_raises(self) -> None:
        registry = MigrationRegistry()
        registry.register("1.0.0", "2.0.0", rename_field("full_name", "display_name"))
        # No migration from 2.0.0 to 3.0.0

        data = {"full_name": "Alice"}
        with pytest.raises(MissingMigrationError):
            registry.apply(data, "1.0.0", "3.0.0")

    def test_missing_source(self) -> None:
        registry = MigrationRegistry()
        registry.register("2.0.0", "3.0.0", lambda d: d)

        data = {"name": "Alice"}
        with pytest.raises(MissingMigrationError):
            registry.apply(data, "1.0.0", "3.0.0")

    def test_missing_target(self) -> None:
        registry = MigrationRegistry()
        registry.register("1.0.0", "2.0.0", lambda d: d)

        data = {"name": "Alice"}
        with pytest.raises(MissingMigrationError):
            registry.apply(data, "1.0.0", "3.0.0")


class TestValidateChain:
    """Chain validation."""

    def test_complete_chain_returns_empty(self) -> None:
        registry = MigrationRegistry()
        registry.register("1.0.0", "2.0.0", lambda d: d)
        registry.register("2.0.0", "3.0.0", lambda d: d)
        assert registry.validate_chain("1.0.0", "3.0.0") == []

    def test_same_version_returns_empty(self) -> None:
        registry = MigrationRegistry()
        assert registry.validate_chain("1.0.0", "1.0.0") == []

    def test_gap_detected(self) -> None:
        registry = MigrationRegistry()
        registry.register("1.0.0", "2.0.0", lambda d: d)
        # Missing 2.0.0 -> 3.0.0
        registry.register("3.0.0", "4.0.0", lambda d: d)

        gaps = registry.validate_chain("1.0.0", "4.0.0")
        assert len(gaps) > 0
        assert any("Chain reaches" in g for g in gaps)
        assert any("Missing migration" in g for g in gaps)

    def test_no_registrations_returns_gap(self) -> None:
        registry = MigrationRegistry()
        gaps = registry.validate_chain("1.0.0", "2.0.0")
        assert len(gaps) > 0

    def test_partial_chain_from_source_only(self) -> None:
        registry = MigrationRegistry()
        registry.register("2.0.0", "3.0.0", lambda d: d)

        gaps = registry.validate_chain("1.0.0", "3.0.0")
        assert any("No outgoing migration from 1.0.0" in g for g in gaps)

    def test_single_migration_completes_chain(self) -> None:
        registry = MigrationRegistry()
        registry.register("1.0.0", "3.0.0", lambda d: d)
        assert registry.validate_chain("1.0.0", "3.0.0") == []


class TestRoundTrip:
    """Round-trip migration — forward then reverse yields original data."""

    def test_forward_reverse_round_trip(self) -> None:
        registry = MigrationRegistry()

        def _forward(data: dict[str, Any]) -> dict[str, Any]:
            result = deepcopy(data)
            if "full_name" in result:
                result["display_name"] = result.pop("full_name")
            if "count" in result:
                result["count"] = int(result["count"])
            return result

        def _reverse(data: dict[str, Any]) -> dict[str, Any]:
            result = deepcopy(data)
            if "display_name" in result:
                result["full_name"] = result.pop("display_name")
            if "count" in result:
                result["count"] = str(result["count"])
            return result

        registry.register("1.0.0", "2.0.0", _forward)
        registry.register("2.0.0", "1.0.0", _reverse)

        original = {"full_name": "Alice", "count": "42"}
        migrated = registry.apply(original, "1.0.0", "2.0.0")
        assert migrated == {"display_name": "Alice", "count": 42}

        restored = registry.apply(migrated, "2.0.0", "1.0.0")
        assert restored == original

    def test_round_trip_three_versions(self) -> None:
        registry = MigrationRegistry()

        def _v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
            result = deepcopy(data)
            if "full_name" in result:
                result["name"] = result.pop("full_name")
            return result

        def _v2_to_v3(data: dict[str, Any]) -> dict[str, Any]:
            result = deepcopy(data)
            if "count" in result:
                result["count"] = int(result["count"])
            return result

        def _v3_to_v2(data: dict[str, Any]) -> dict[str, Any]:
            result = deepcopy(data)
            if "count" in result:
                result["count"] = str(result["count"])
            return result

        def _v2_to_v1(data: dict[str, Any]) -> dict[str, Any]:
            result = deepcopy(data)
            if "name" in result:
                result["full_name"] = result.pop("name")
            return result

        registry.register("1.0.0", "2.0.0", _v1_to_v2)
        registry.register("2.0.0", "3.0.0", _v2_to_v3)
        registry.register("3.0.0", "2.0.0", _v3_to_v2)
        registry.register("2.0.0", "1.0.0", _v2_to_v1)

        original = {"full_name": "Alice", "count": "42"}
        result = registry.apply(original, "1.0.0", "3.0.0")
        assert result == {"name": "Alice", "count": 42}

        restored = registry.apply(result, "3.0.0", "1.0.0")
        assert restored == original


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


class TestRenameField:
    def test_rename_field(self) -> None:
        fn = rename_field("old", "new")
        result = fn({"old": "val", "keep": "stay"})
        assert result == {"new": "val", "keep": "stay"}

    def test_rename_nonexistent_field_noop(self) -> None:
        fn = rename_field("old", "new")
        result = fn({"keep": "stay"})
        assert result == {"keep": "stay"}

    def test_rename_does_not_mutate_original(self) -> None:
        fn = rename_field("old", "new")
        data = {"old": "val"}
        result = fn(data)
        assert result == {"new": "val"}
        assert data == {"old": "val"}

    def test_rename_has_descriptive_name(self) -> None:
        fn = rename_field("full_name", "display_name")
        assert "rename_full_name_to_display_name" in fn.__name__


class TestConvertField:
    def test_convert_with_int(self) -> None:
        fn = convert_field("count", int)
        result = fn({"count": "42"})
        assert result == {"count": 42}

    def test_convert_with_custom_callable(self) -> None:
        fn = convert_field("active", lambda v: v.lower() == "true")
        result = fn({"active": "True"})
        assert result["active"] is True

    def test_convert_missing_field_noop(self) -> None:
        fn = convert_field("missing", int)
        data = {"name": "Alice"}
        result = fn(data)
        assert result == data

    def test_convert_does_not_mutate_original(self) -> None:
        fn = convert_field("count", int)
        data = {"count": "42"}
        result = fn(data)
        assert result == {"count": 42}
        assert data == {"count": "42"}


class TestSetDefault:
    def test_set_default_on_missing_field(self) -> None:
        fn = set_default("nickname", "unknown")
        result = fn({"name": "Alice"})
        assert result == {"name": "Alice", "nickname": "unknown"}

    def test_set_default_preserves_existing(self) -> None:
        fn = set_default("nickname", "unknown")
        result = fn({"name": "Alice", "nickname": "Ace"})
        assert result == {"name": "Alice", "nickname": "Ace"}

    def test_set_default_does_not_mutate_original(self) -> None:
        fn = set_default("nickname", "unknown")
        data = {"name": "Alice"}
        result = fn(data)
        assert "nickname" in result
        assert "nickname" not in data

    def test_set_default_with_mutable_default(self) -> None:
        fn = set_default("tags", [])
        result = fn({"name": "Alice"})
        assert result == {"name": "Alice", "tags": []}
        result["tags"].append("admin")
        # Subsequent calls should not share the same list
        result2 = fn({"name": "Bob"})
        assert result2["tags"] == []


class TestAddField:
    def test_add_computed_field(self) -> None:
        fn = add_field("full_name", lambda d: f"{d['first']} {d['last']}")
        result = fn({"first": "Alice", "last": "Smith"})
        assert result["full_name"] == "Alice Smith"

    def test_add_field_with_constant(self) -> None:
        fn = add_field("version", lambda d: "2.0.0")
        result = fn({"name": "Alice"})
        assert result["version"] == "2.0.0"

    def test_add_field_overwrites_existing(self) -> None:
        fn = add_field("version", lambda d: "2.0.0")
        result = fn({"name": "Alice", "version": "1.0.0"})
        assert result["version"] == "2.0.0"

    def test_add_field_does_not_mutate_original(self) -> None:
        fn = add_field("version", lambda d: "2.0.0")
        data = {"name": "Alice"}
        result = fn(data)
        assert result == {"name": "Alice", "version": "2.0.0"}
        assert data == {"name": "Alice"}


class TestRemoveField:
    def test_remove_existing_field(self) -> None:
        fn = remove_field("legacy")
        result = fn({"name": "Alice", "legacy": "old"})
        assert result == {"name": "Alice"}

    def test_remove_nonexistent_field_noop(self) -> None:
        fn = remove_field("missing")
        data = {"name": "Alice"}
        result = fn(data)
        assert result == data

    def test_remove_does_not_mutate_original(self) -> None:
        fn = remove_field("legacy")
        data = {"name": "Alice", "legacy": "old"}
        result = fn(data)
        assert result == {"name": "Alice"}
        assert data == {"legacy": "old", "name": "Alice"}


class TestIntegrationWithExistingMigration:
    """Test that new migration registry works with existing create_migration/apply_migration."""

    def test_create_migration_plan_and_apply_via_registry(self) -> None:
        v1_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "full_name": {"type": "string"},
                "count": {"type": "integer"},
            },
        }
        v2_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "display_name": {"type": "string"},
                "count": {"type": "integer"},
                "email": {"type": "string"},
            },
        }

        plan = create_migration(v1_schema, v2_schema)

        def _auto_migrate(data: dict[str, Any]) -> dict[str, Any]:
            return apply_migration(data, plan)

        registry = MigrationRegistry()
        registry.register("1.0.0", "2.0.0", _auto_migrate)

        data = {"full_name": "Alice", "count": 42}
        result = registry.apply(data, "1.0.0", "2.0.0")

        # The heuristic rename may pair "full_name" with "display_name" or "email";
        # we only assert invariants that hold regardless
        assert "full_name" not in result
        assert "count" in result
        assert result["count"] == 42


# ---------------------------------------------------------------------------
# Dry-run / describe-chain
# ---------------------------------------------------------------------------


class TestDryRun:
    """Dry-run mode on MigrationRegistry."""

    def test_describe_chain_returns_step_descriptions(self) -> None:
        registry = MigrationRegistry()
        registry.register("1.0.0", "2.0.0", rename_field("full_name", "display_name"), "Rename full_name")
        registry.register("2.0.0", "3.0.0", convert_field("count", int), "Convert count to int")

        steps = registry.describe_chain("1.0.0", "3.0.0")
        assert len(steps) == 2
        assert steps[0]["source_version"] == "1.0.0"
        assert steps[0]["target_version"] == "2.0.0"
        assert steps[0]["description"] == "Rename full_name"
        assert steps[1]["source_version"] == "2.0.0"
        assert steps[1]["target_version"] == "3.0.0"
        assert steps[1]["description"] == "Convert count to int"

    def test_describe_chain_same_version_empty(self) -> None:
        registry = MigrationRegistry()
        assert registry.describe_chain("1.0.0", "1.0.0") == []

    def test_describe_chain_missing_raises(self) -> None:
        registry = MigrationRegistry()
        registry.register("1.0.0", "2.0.0", lambda d: d)
        with pytest.raises(MissingMigrationError):
            registry.describe_chain("1.0.0", "3.0.0")

    def test_dry_run_returns_per_step_diff(self) -> None:
        registry = MigrationRegistry()
        registry.register("1.0.0", "2.0.0", rename_field("full_name", "display_name"))
        registry.register("2.0.0", "3.0.0", convert_field("count", int))

        data = {"full_name": "Alice", "count": "42", "email": "a@b.com"}
        steps = registry.dry_run(data, "1.0.0", "3.0.0")

        assert len(steps) == 2

        # Step 1: rename full_name -> display_name
        assert steps[0]["source_version"] == "1.0.0"
        assert steps[0]["target_version"] == "2.0.0"
        assert "display_name" in steps[0]["added_fields"]
        assert "full_name" in steps[0]["removed_fields"]
        assert "count" not in steps[0]["added_fields"]
        assert steps[0]["changed_fields"] == {}

        # Step 2: convert count to int
        assert steps[1]["source_version"] == "2.0.0"
        assert steps[1]["target_version"] == "3.0.0"
        assert "count" in steps[1]["changed_fields"]
        assert steps[1]["changed_fields"]["count"]["old"] == "42"
        assert steps[1]["changed_fields"]["count"]["new"] == 42
        assert steps[1]["added_fields"] == []
        assert steps[1]["removed_fields"] == []

    def test_dry_run_does_not_mutate_original(self) -> None:
        registry = MigrationRegistry()
        registry.register("1.0.0", "2.0.0", rename_field("full_name", "display_name"))

        data = {"full_name": "Alice"}
        original = deepcopy(data)
        steps = registry.dry_run(data, "1.0.0", "2.0.0")
        assert data == original
        assert len(steps) == 1

    def test_dry_run_same_version_returns_empty(self) -> None:
        registry = MigrationRegistry()
        data = {"name": "Alice"}
        steps = registry.dry_run(data, "1.0.0", "1.0.0")
        assert steps == []

    def test_dry_run_missing_chain_raises(self) -> None:
        registry = MigrationRegistry()
        data = {"name": "Alice"}
        with pytest.raises(MissingMigrationError):
            registry.dry_run(data, "1.0.0", "3.0.0")

    def test_dry_run_set_default_appears_as_addition(self) -> None:
        registry = MigrationRegistry()
        registry.register("1.0.0", "2.0.0", set_default("middle_name", ""))

        data = {"first_name": "Alice"}
        steps = registry.dry_run(data, "1.0.0", "2.0.0")
        assert len(steps) == 1
        assert "middle_name" in steps[0]["added_fields"]
        assert "first_name" not in steps[0]["added_fields"]
        assert steps[0]["removed_fields"] == []

    def test_dry_run_remove_field_appears_as_removal(self) -> None:
        registry = MigrationRegistry()
        registry.register("1.0.0", "2.0.0", remove_field("legacy"))

        data = {"name": "Alice", "legacy": "old"}
        steps = registry.dry_run(data, "1.0.0", "2.0.0")
        assert len(steps) == 1
        assert "legacy" in steps[0]["removed_fields"]
        assert "name" not in steps[0]["removed_fields"]

    def test_dry_run_uses_description_from_registration(self) -> None:
        registry = MigrationRegistry()
        registry.register("1.0.0", "2.0.0", rename_field("a", "b"), "Rename field a to b")

        steps = registry.dry_run({"a": 1}, "1.0.0", "2.0.0")
        assert steps[0]["description"] == "Rename field a to b"

    def test_dry_run_falls_back_to_function_name(self) -> None:
        registry = MigrationRegistry()
        registry.register("1.0.0", "2.0.0", rename_field("a", "b"))

        steps = registry.dry_run({"a": 1}, "1.0.0", "2.0.0")
        assert "rename_a_to_b" in steps[0]["description"]
