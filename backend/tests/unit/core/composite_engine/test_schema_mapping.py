"""Tests for schema field mapping."""

from modulo.core.composite_engine.schema_mapping import apply_field_mapping


class TestApplyFieldMapping:
    def test_passthrough_when_no_field_map(self) -> None:
        source = {"a": 1, "b": 2}
        result = apply_field_mapping(source, None)
        assert result == {"a": 1, "b": 2}

    def test_simple_mapping(self) -> None:
        source = {"user": {"name": "Alice", "id": 42}}
        field_map = {"user_name": "user.name", "user_id": "user.id"}
        result = apply_field_mapping(source, field_map)
        assert result == {"user_name": "Alice", "user_id": 42}

    def test_nested_extraction(self) -> None:
        source = {"data": {"meta": {"score": 0.95}}}
        field_map = {"score": "data.meta.score"}
        result = apply_field_mapping(source, field_map)
        assert result == {"score": 0.95}

    def test_missing_jmespath_returns_none(self) -> None:
        source = {"a": 1}
        field_map = {"b": "missing.field"}
        result = apply_field_mapping(source, field_map)
        assert result == {"b": None}

    def test_expression_with_list(self) -> None:
        source = {"items": [{"name": "foo"}, {"name": "bar"}]}
        field_map = {"first": "items[0].name", "names": "items[*].name"}
        result = apply_field_mapping(source, field_map)
        assert result == {"first": "foo", "names": ["foo", "bar"]}

    def test_literal_value_in_field_map(self) -> None:
        source = {"a": 1}
        field_map = {"key": "some.expression", "literal": 42}
        result = apply_field_mapping(source, field_map)
        assert result == {"key": None, "literal": 42}

    def test_empty_field_map(self) -> None:
        source = {"a": 1}
        result = apply_field_mapping(source, {})
        assert result == {}

    def test_empty_source(self) -> None:
        result = apply_field_mapping({}, {"key": "value"})
        assert result == {"key": None}
