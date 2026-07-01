"""Tests for composite graph expansion and parameter injection."""

import uuid
from typing import Any

import pytest

from modulo.core.composite_engine.expander import (
    _inject_parameters,
    expand_composite_node,
)


def _template(
    nodes: list[dict[str, Any]] | None = None,
    edges: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "nodes": nodes or [],
        "edges": edges or [],
    }


def _node_def(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    defn: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "node_type": "composite",
        "composite_ref": str(uuid.uuid4()),
    }
    if overrides:
        defn.update(overrides)
    return defn


class TestExpandCompositeNode:
    def test_expand_single_node(self) -> None:
        sub_node = {"id": str(uuid.uuid4()), "agent_id": str(uuid.uuid4()), "prompt": "Hello"}
        template = _template(nodes=[sub_node])
        node_def = _node_def()
        expanded = expand_composite_node(node_def, template)

        assert len(expanded) == 1
        assert expanded[0]["_composite_parent_id"] == node_def["id"]
        assert expanded[0]["_composite_index"] == 0
        assert expanded[0]["prompt"] == "Hello"

    def test_expand_multiple_nodes(self) -> None:
        sub_nodes = [
            {"id": str(uuid.uuid4()), "agent_id": str(uuid.uuid4()), "prompt": "Node A"},
            {"id": str(uuid.uuid4()), "agent_id": str(uuid.uuid4()), "prompt": "Node B"},
        ]
        template = _template(nodes=sub_nodes)
        node_def = _node_def()
        expanded = expand_composite_node(node_def, template)

        assert len(expanded) == 2
        assert expanded[0]["_composite_index"] == 0
        assert expanded[1]["_composite_index"] == 1
        assert expanded[0]["prompt"] == "Node A"
        assert expanded[1]["prompt"] == "Node B"

    def test_empty_template_raises(self) -> None:
        template = _template(nodes=[])
        node_def = _node_def()
        with pytest.raises(ValueError, match="no sub-pipeline nodes"):
            expand_composite_node(node_def, template)

    def test_parameter_injection(self) -> None:
        sub_node = {
            "id": str(uuid.uuid4()),
            "agent_id": str(uuid.uuid4()),
            "prompt": "You are a {{parameter.role}} expert. Use {{parameter.model}}.",
        }
        template = _template(nodes=[sub_node])
        node_def = _node_def()
        expanded = expand_composite_node(
            node_def,
            template,
            parameter_values={"role": "Python", "model": "gpt-4"},
        )
        assert expanded[0]["prompt"] == "You are a Python expert. Use gpt-4."

    def test_unrecognized_placeholder_left_as_is(self) -> None:
        sub_node = {
            "id": str(uuid.uuid4()),
            "agent_id": str(uuid.uuid4()),
            "prompt": "Hello {{parameter.missing}} world",
        }
        template = _template(nodes=[sub_node])
        node_def = _node_def()
        expanded = expand_composite_node(
            node_def,
            template,
            parameter_values={"other": "val"},
        )
        assert expanded[0]["prompt"] == "Hello {{parameter.missing}} world"

    def test_empty_prompt_not_affected(self) -> None:
        sub_node = {
            "id": str(uuid.uuid4()),
            "agent_id": str(uuid.uuid4()),
            "prompt": "",
        }
        template = _template(nodes=[sub_node])
        node_def = _node_def()
        expanded = expand_composite_node(node_def, template)
        assert expanded[0]["prompt"] == ""

    def test_none_prompt_unchanged(self) -> None:
        sub_node = {
            "id": str(uuid.uuid4()),
            "agent_id": str(uuid.uuid4()),
        }
        template = _template(nodes=[sub_node])
        node_def = _node_def()
        expanded = expand_composite_node(node_def, template)
        assert "prompt" not in expanded[0] or expanded[0].get("prompt") is None


class TestInjectParameters:
    def test_basic_replacement(self) -> None:
        result = _inject_parameters(
            "Use {{parameter.model}} and {{parameter.temp}}",
            {"model": "gpt-4", "temp": 0.5},
        )
        assert result == "Use gpt-4 and 0.5"

    def test_no_placeholders(self) -> None:
        result = _inject_parameters("Hello world", {"x": "y"})
        assert result == "Hello world"

    def test_unknown_placeholder(self) -> None:
        result = _inject_parameters("{{parameter.unknown}}", {"known": "val"})
        assert result == "{{parameter.unknown}}"

    def test_non_string_value(self) -> None:
        result = _inject_parameters("Count: {{parameter.n}}", {"n": 42})
        assert result == "Count: 42"
