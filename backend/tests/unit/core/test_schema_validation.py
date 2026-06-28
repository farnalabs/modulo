"""Unit tests for schema union/array validation."""

from modulo.core.schema_registry.validation import (
    validate_array_schema,
    validate_union_and_array,
    validate_union_schema,
)


class TestValidateUnionSchema:
    def test_valid_one_of(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "value": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "integer"},
                    ]
                }
            },
        }
        result = validate_union_schema(schema)
        assert result.valid

    def test_valid_any_of(self) -> None:
        schema = {
            "oneOf": [
                {"type": "string"},
                {"type": "boolean"},
            ]
        }
        result = validate_union_schema(schema)
        assert result.valid

    def test_one_of_not_array(self) -> None:
        schema = {"oneOf": {"type": "string"}}
        result = validate_union_schema(schema)
        assert not result.valid
        assert "non-empty array" in result.errors[0].message

    def test_one_of_empty_array(self) -> None:
        schema = {"oneOf": []}
        result = validate_union_schema(schema)
        assert not result.valid
        assert "not be empty" in result.errors[0].message

    def test_one_of_with_type_at_same_level(self) -> None:
        schema = {"type": "string", "oneOf": [{"type": "integer"}, {"type": "boolean"}]}
        result = validate_union_schema(schema)
        assert not result.valid
        assert any("must not appear alongside 'type'" in e.message for e in result.errors)

    def test_variant_not_a_dict(self) -> None:
        schema = {"oneOf": ["string", {"type": "integer"}]}
        result = validate_union_schema(schema)
        assert not result.valid
        assert any("must be a JSON Schema object" in e.message for e in result.errors)

    def test_variant_without_type_or_composition(self) -> None:
        schema = {"oneOf": [{"description": "no type here"}]}
        result = validate_union_schema(schema)
        assert not result.valid
        assert any("no 'type' or composition" in e.message for e in result.errors)

    def test_nested_union_in_properties(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "nested": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "null"},
                    ]
                }
            },
        }
        result = validate_union_schema(schema)
        assert result.valid

    def test_deeply_nested_union_reports_paths(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "deep": {
                    "oneOf": [
                        {"type": "string"},
                        {"oneOf": [123]},
                    ]
                }
            },
        }
        result = validate_union_schema(schema)
        assert not result.valid
        errors = [e.path for e in result.errors]
        assert any("deep/oneOf/1/oneOf/0" in p for p in errors)


class TestValidateArraySchema:
    def test_valid_array_with_items_object(self) -> None:
        schema = {"type": "array", "items": {"type": "string"}}
        result = validate_array_schema(schema)
        assert result.valid

    def test_valid_array_with_tuple_items(self) -> None:
        schema = {
            "type": "array",
            "items": [{"type": "string"}, {"type": "integer"}],
        }
        result = validate_array_schema(schema)
        assert result.valid

    def test_non_array_type_passes(self) -> None:
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        result = validate_array_schema(schema)
        assert result.valid

    def test_array_missing_items_warns(self) -> None:
        schema = {"type": "array"}
        result = validate_array_schema(schema)
        assert not result.valid
        assert any("'items' is recommended" in e.message for e in result.errors)

    def test_array_items_without_type(self) -> None:
        schema = {"type": "array", "items": {"description": "bare"}}
        result = validate_array_schema(schema)
        assert not result.valid
        assert any("should specify 'type'" in e.message for e in result.errors)

    def test_tuple_item_not_dict(self) -> None:
        schema = {"type": "array", "items": [{"type": "string"}, "not-a-schema"]}
        result = validate_array_schema(schema)
        assert not result.valid
        assert any("Tuple item must be a JSON Schema object" in e.message for e in result.errors)

    def test_array_with_union_items(self) -> None:
        schema = {
            "type": "array",
            "items": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "integer"},
                ]
            },
        }
        result = validate_array_schema(schema)
        assert result.valid

    def test_array_with_contains(self) -> None:
        schema = {
            "type": "array",
            "items": {"type": "object"},
            "contains": {"type": "object", "required": ["id"]},
        }
        result = validate_array_schema(schema)
        assert result.valid

    def test_array_with_prefix_items(self) -> None:
        schema = {
            "type": "array",
            "prefixItems": [{"type": "string"}, {"type": "integer"}],
            "items": {"type": "number"},
        }
        result = validate_array_schema(schema)
        assert result.valid

    def test_nested_array_in_union(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "data": {
                    "oneOf": [
                        {"type": "array", "items": {"type": "string"}},
                        {"type": "object"},
                    ]
                }
            },
        }
        result = validate_union_and_array(schema)
        assert result.valid

    def test_no_type_but_any_of(self) -> None:
        schema = {
            "anyOf": [
                {"type": "array", "items": {"type": "string"}},
                {"type": "object"},
            ]
        }
        result = validate_array_schema(schema)
        assert result.valid


class TestValidateUnionAndArray:
    def test_both_valid_passes(self) -> None:
        schema = {
            "type": "array",
            "items": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "integer"},
                ]
            },
        }
        result = validate_union_and_array(schema)
        assert result.valid

    def test_aggregates_errors(self) -> None:
        schema = {
            "type": "array",
            "items": {
                "oneOf": [
                    {"no_type": True},
                ]
            },
        }
        result = validate_union_and_array(schema)
        assert not result.valid
