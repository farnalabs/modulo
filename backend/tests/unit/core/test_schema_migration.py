"""Unit tests for schema migration."""


from modulo.core.schema_registry.migration import (
    MigrationPlan,
    apply_migration,
    create_migration,
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
