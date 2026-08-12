"""Tests for composite graph expansion and parameter injection."""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.composite_engine.expander import (
    _inject_parameters,
    expand_composite_node,
    expand_composites_in_graph,
)
from modulo.db.models.composite_template import CompositeTemplate


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

    def test_missing_node_id_raises(self) -> None:
        sub_node = {"id": str(uuid.uuid4()), "agent_id": str(uuid.uuid4())}
        template = _template(nodes=[sub_node])
        node_def = {"node_type": "composite", "composite_ref": str(uuid.uuid4())}
        with pytest.raises(ValueError, match="missing required 'id'"):
            expand_composite_node(node_def, template)

    def test_input_and_output_mappings_propagated(self) -> None:
        sub_node = {"id": str(uuid.uuid4()), "agent_id": str(uuid.uuid4())}
        template = _template(nodes=[sub_node])
        input_mapping = {"a": "x.y"}
        output_mapping = {"b": "z.w"}
        node_def = _node_def(
            {
                "composite_input_mapping": input_mapping,
                "composite_output_mapping": output_mapping,
            }
        )
        expanded = expand_composite_node(node_def, template)
        assert expanded[0]["_input_mapping"] == input_mapping
        assert expanded[0]["_output_mapping"] == output_mapping

    def test_edge_with_unknown_sub_node_id_warns_and_passes_through(self) -> None:
        sub_node = {"id": "known", "agent_id": str(uuid.uuid4())}
        template = _template(
            nodes=[sub_node],
            edges=[{"id": "e1", "source": "known", "target": "ghost", "type": "normal"}],
        )
        node_def = _node_def()
        expanded = expand_composite_node(node_def, template)
        assert expanded[0]["_composite_edges"] == [{"id": "e1", "source": "known", "target": "ghost", "type": "normal"}]

    def test_edges_validated_against_sub_node_ids(self) -> None:
        sub_nodes = [
            {"id": "a", "agent_id": str(uuid.uuid4())},
            {"id": "b", "agent_id": str(uuid.uuid4())},
        ]
        template = _template(
            nodes=sub_nodes,
            edges=[{"id": "e1", "source": "a", "target": "b", "type": "normal"}],
        )
        node_def = _node_def()
        expanded = expand_composite_node(node_def, template)
        assert expanded[0]["_composite_edges"] == [{"id": "e1", "source": "a", "target": "b", "type": "normal"}]


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


class _FakeResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value

    def scalar_one(self) -> object:
        return self._value


def _session_returns(*values: object) -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=[_FakeResult(v) for v in values])
    return session


def _orm_template(
    template_id: uuid.UUID,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]] | None = None,
    version: str = "1.0.0",
) -> MagicMock:
    template = MagicMock(spec=CompositeTemplate)
    template.id = template_id
    template.version = version
    template.sub_pipeline_graph_json = {"nodes": nodes, "edges": edges or []}
    return template


def _composite_node(composite_id: uuid.UUID, template_id: uuid.UUID, **extra: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": str(composite_id),
        "node_type": "composite",
        "composite_ref": str(template_id),
    }
    node.update(extra)
    return node


class TestExpandCompositesInGraph:
    async def test_no_composite_nodes_returns_unchanged(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        node_id = str(uuid.uuid4())
        nodes = [{"id": node_id, "node_type": "agent"}]
        edges = [{"source": node_id, "target": node_id, "type": "normal"}]
        out_nodes, out_edges, bindings = await expand_composites_in_graph(session, None, nodes, edges)
        assert out_nodes == nodes
        assert out_edges == edges
        assert bindings == []
        session.execute.assert_not_called()

    async def test_single_composite_expands_to_flat_node_with_uuid(self) -> None:
        template_id = uuid.uuid4()
        template = _orm_template(template_id, [{"id": "advocate", "node_type": "agent", "prompt": "Hello"}])
        session = _session_returns(template)
        composite_id = uuid.uuid4()
        nodes = [_composite_node(composite_id, template_id)]
        out_nodes, out_edges, bindings = await expand_composites_in_graph(session, None, nodes, [])

        assert len(out_nodes) == 1
        node = out_nodes[0]
        assert node["id"] != str(composite_id)
        uuid.UUID(node["id"])
        assert node["_composite_parent_id"] == str(composite_id)
        assert node["_composite_index"] == 0
        assert node["prompt"] == "Hello"
        assert out_edges == []
        assert bindings == [
            {
                "composite_template_id": str(template_id),
                "composite_version": "1.0.0",
                "parameter_values": {},
                "input_mapping": None,
                "output_mapping": None,
            }
        ]

    async def test_parent_edges_rewired_through_single_sub_node(self) -> None:
        template_id = uuid.uuid4()
        template = _orm_template(template_id, [{"id": "only", "node_type": "agent"}])
        session = _session_returns(template)
        composite_id = uuid.uuid4()
        upstream = uuid.uuid4()
        downstream = uuid.uuid4()
        nodes = [
            {"id": str(upstream), "node_type": "agent"},
            _composite_node(composite_id, template_id),
            {"id": str(downstream), "node_type": "agent"},
        ]
        edges = [
            {"id": "e1", "source": str(upstream), "target": str(composite_id), "type": "normal"},
            {"id": "e2", "source": str(composite_id), "target": str(downstream), "type": "normal"},
        ]
        out_nodes, out_edges, _ = await expand_composites_in_graph(session, None, nodes, edges)

        assert len(out_nodes) == 3
        sub_id = next(n["id"] for n in out_nodes if n.get("_composite_parent_id") == str(composite_id))
        e1 = next(e for e in out_edges if e["id"] == "e1")
        e2 = next(e for e in out_edges if e["id"] == "e2")
        assert e1["source"] == str(upstream)
        assert e1["target"] == sub_id
        assert e1["type"] == "normal"
        assert e2["source"] == sub_id
        assert e2["target"] == str(downstream)

    async def test_multiple_entry_sub_nodes_fan_in_parent_edge(self) -> None:
        template_id = uuid.uuid4()
        template = _orm_template(
            template_id,
            [
                {"id": "for", "node_type": "agent"},
                {"id": "against", "node_type": "agent"},
                {"id": "mediator", "node_type": "agent"},
            ],
            edges=[
                {"id": "s1", "source": "for", "target": "mediator"},
                {"id": "s2", "source": "against", "target": "mediator"},
            ],
        )
        session = _session_returns(template)
        composite_id = uuid.uuid4()
        upstream = uuid.uuid4()
        downstream = uuid.uuid4()
        nodes = [
            {"id": str(upstream), "node_type": "agent"},
            _composite_node(composite_id, template_id),
            {"id": str(downstream), "node_type": "agent"},
        ]
        edges = [
            {"id": "p1", "source": str(upstream), "target": str(composite_id), "type": "normal"},
            {"id": "p2", "source": str(composite_id), "target": str(downstream), "type": "normal"},
        ]
        out_nodes, out_edges, _ = await expand_composites_in_graph(session, None, nodes, edges)

        sub = {n["_composite_index"]: n["id"] for n in out_nodes if n.get("_composite_parent_id") == str(composite_id)}
        for_id = sub[0]
        against_id = sub[1]
        mediator_id = sub[2]

        p1_edges = [e for e in out_edges if e["id"] == "p1"]
        assert sorted(e["target"] for e in p1_edges) == sorted([for_id, against_id])

        p2_edges = [e for e in out_edges if e["id"] == "p2"]
        assert [e["source"] for e in p2_edges] == [mediator_id]

        assert any(e["id"] == "s1" and e["source"] == for_id and e["target"] == mediator_id for e in out_edges)
        assert any(e["id"] == "s2" and e["source"] == against_id and e["target"] == mediator_id for e in out_edges)

    async def test_parameter_injection_in_prompt_template(self) -> None:
        template_id = uuid.uuid4()
        template = _orm_template(
            template_id,
            [{"id": "a", "node_type": "agent", "prompt_template": "You are {{parameter.role}}"}],
        )
        session = _session_returns(template)
        composite_id = uuid.uuid4()
        nodes = [_composite_node(composite_id, template_id, composite_parameter_values={"role": "Python"})]
        out_nodes, _, bindings = await expand_composites_in_graph(session, None, nodes, [])
        assert out_nodes[0]["prompt_template"] == "You are Python"
        assert bindings[0]["parameter_values"] == {"role": "Python"}

    async def test_template_not_found_raises(self) -> None:
        template_id = uuid.uuid4()
        session = _session_returns(None)
        nodes = [_composite_node(uuid.uuid4(), template_id)]
        with pytest.raises(ValueError, match=str(template_id)):
            await expand_composites_in_graph(session, None, nodes, [])

    async def test_empty_subgraph_raises(self) -> None:
        template_id = uuid.uuid4()
        template = _orm_template(template_id, [])
        session = _session_returns(template)
        nodes = [_composite_node(uuid.uuid4(), template_id)]
        with pytest.raises(ValueError, match="no sub-pipeline nodes"):
            await expand_composites_in_graph(session, None, nodes, [])

    async def test_nested_composite_expands_recursively(self) -> None:
        inner_tid = uuid.uuid4()
        inner = _orm_template(inner_tid, [{"id": "leaf", "node_type": "agent", "prompt": "inner"}])
        outer_tid = uuid.uuid4()
        outer = _orm_template(outer_tid, [{"id": "nested", "node_type": "composite", "composite_ref": str(inner_tid)}])
        session = _session_returns(outer, inner)
        nodes = [_composite_node(uuid.uuid4(), outer_tid)]
        out_nodes, _, bindings = await expand_composites_in_graph(session, None, nodes, [])

        assert len(out_nodes) == 1
        node = out_nodes[0]
        assert node["prompt"] == "inner"
        assert node["_composite_parent_id"] == "nested"
        assert len(bindings) == 2
        assert {b["composite_template_id"] for b in bindings} == {str(outer_tid), str(inner_tid)}

    async def test_depth_limit_guards_self_referential_composite(self) -> None:
        template_id = uuid.uuid4()
        template = _orm_template(
            template_id,
            [{"id": "self", "node_type": "composite", "composite_ref": str(template_id)}],
        )
        session = _session_returns(template)
        nodes = [_composite_node(uuid.uuid4(), template_id)]
        with pytest.raises(ValueError, match="depth limit"):
            await expand_composites_in_graph(session, None, nodes, [])

    async def test_output_schema_propagated_to_exit_sub_node(self) -> None:
        template_id = uuid.uuid4()
        template = _orm_template(
            template_id,
            [{"id": "a", "node_type": "agent"}, {"id": "b", "node_type": "agent"}],
            edges=[{"source": "a", "target": "b"}],
        )
        session = _session_returns(template)
        schema: dict[str, Any] = {"type": "object"}
        nodes = [_composite_node(uuid.uuid4(), template_id, output_schema_json=schema)]
        out_nodes, _, _ = await expand_composites_in_graph(session, None, nodes, [])
        b_node = next(n for n in out_nodes if n["_composite_index"] == 1)
        assert b_node.get("output_schema_json") == schema

    async def test_multiple_composite_nodes_get_distinct_uuids(self) -> None:
        template_id = uuid.uuid4()
        template = _orm_template(template_id, [{"id": "a", "node_type": "agent"}])
        session = _session_returns(template)
        nodes = [
            _composite_node(uuid.uuid4(), template_id),
            _composite_node(uuid.uuid4(), template_id),
        ]
        out_nodes, _, bindings = await expand_composites_in_graph(session, None, nodes, [])
        assert len(out_nodes) == 2
        assert out_nodes[0]["id"] != out_nodes[1]["id"]
        assert len(bindings) == 2

    async def test_loop_edge_default_target_remapped(self) -> None:
        template_id = uuid.uuid4()
        template = _orm_template(template_id, [{"id": "only", "node_type": "agent"}])
        session = _session_returns(template)
        composite_id = uuid.uuid4()
        downstream = uuid.uuid4()
        nodes = [
            _composite_node(composite_id, template_id),
            {"id": str(downstream), "node_type": "agent"},
        ]
        edges = [
            {
                "id": "loop1",
                "source": str(composite_id),
                "target": str(composite_id),
                "type": "loop",
                "max_iterations": 3,
                "default_target": str(composite_id),
            },
            {"id": "n1", "source": str(composite_id), "target": str(downstream), "type": "normal"},
        ]
        out_nodes, out_edges, _ = await expand_composites_in_graph(session, None, nodes, edges)

        sub_id = next(n["id"] for n in out_nodes if n.get("_composite_parent_id") == str(composite_id))
        loop = next(e for e in out_edges if e["id"] == "loop1")
        assert loop["source"] == sub_id
        assert loop["target"] == sub_id
        assert loop["max_iterations"] == 3
        assert loop["default_target"] == sub_id


class TestCompositeExpanderErrorPaths:
    async def test_composite_node_missing_composite_ref_raises(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        nodes = [{"id": str(uuid.uuid4()), "node_type": "composite"}]
        with pytest.raises(ValueError, match="missing required 'composite_ref'"):
            await expand_composites_in_graph(session, None, nodes, [])

    async def test_invalid_composite_ref_raises(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        nodes = [_composite_node(uuid.uuid4(), uuid.uuid4(), composite_ref="not-a-uuid")]
        with pytest.raises(ValueError, match="invalid composite_ref"):
            await expand_composites_in_graph(session, None, nodes, [])

    async def test_template_graph_not_a_dict_raises(self) -> None:
        template_id = uuid.uuid4()
        template = MagicMock(spec=CompositeTemplate)
        template.id = template_id
        template.version = "1.0.0"
        template.sub_pipeline_graph_json = "not-a-dict"
        session = _session_returns(template)
        nodes = [_composite_node(uuid.uuid4(), template_id)]
        with pytest.raises(ValueError, match="no sub-pipeline graph"):
            await expand_composites_in_graph(session, None, nodes, [])

    async def test_template_edges_non_list_coerced_to_empty(self) -> None:
        template_id = uuid.uuid4()
        template = _orm_template(template_id, [{"id": "a", "node_type": "agent"}])
        template.sub_pipeline_graph_json = {"nodes": [{"id": "a", "node_type": "agent"}], "edges": "nope"}
        session = _session_returns(template)
        nodes = [_composite_node(uuid.uuid4(), template_id)]
        out_nodes, out_edges, _ = await expand_composites_in_graph(session, None, nodes, [])
        assert len(out_nodes) == 1
        assert out_edges == []

    async def test_sub_node_without_id_raises(self) -> None:
        template_id = uuid.uuid4()
        template = _orm_template(template_id, [{"node_type": "agent"}])
        session = _session_returns(template)
        nodes = [_composite_node(uuid.uuid4(), template_id)]
        with pytest.raises(ValueError, match="sub-node without an id"):
            await expand_composites_in_graph(session, None, nodes, [])

    async def test_duplicate_sub_node_id_raises(self) -> None:
        template_id = uuid.uuid4()
        template = _orm_template(
            template_id,
            [
                {"id": "dup", "node_type": "agent"},
                {"id": "dup", "node_type": "agent"},
            ],
        )
        session = _session_returns(template)
        nodes = [_composite_node(uuid.uuid4(), template_id)]
        with pytest.raises(ValueError, match="duplicate sub-node id"):
            await expand_composites_in_graph(session, None, nodes, [])

    async def test_nested_composite_missing_composite_ref_raises(self) -> None:
        inner_tid = uuid.uuid4()
        inner = _orm_template(inner_tid, [{"id": "leaf", "node_type": "agent"}])
        outer_tid = uuid.uuid4()
        outer = _orm_template(outer_tid, [{"id": "nested", "node_type": "composite"}])
        session = _session_returns(outer, inner)
        nodes = [_composite_node(uuid.uuid4(), outer_tid)]
        with pytest.raises(ValueError, match="missing required 'composite_ref'"):
            await expand_composites_in_graph(session, None, nodes, [])

    async def test_nested_composite_template_missing_raises(self) -> None:
        missing_inner_tid = uuid.uuid4()
        outer_tid = uuid.uuid4()
        outer = _orm_template(
            outer_tid,
            [{"id": "nested", "node_type": "composite", "composite_ref": str(missing_inner_tid)}],
        )
        session = _session_returns(outer, None)
        nodes = [_composite_node(uuid.uuid4(), outer_tid)]
        with pytest.raises(ValueError, match="references missing CompositeTemplate"):
            await expand_composites_in_graph(session, None, nodes, [])

    async def test_org_scoped_template_lookup_adds_org_filter(self) -> None:
        org_id = uuid.uuid4()
        template_id = uuid.uuid4()
        template = _orm_template(template_id, [{"id": "a", "node_type": "agent"}])
        session = _session_returns(template)
        nodes = [_composite_node(uuid.uuid4(), template_id)]
        out_nodes, _, _ = await expand_composites_in_graph(session, org_id, nodes, [])
        assert len(out_nodes) == 1
        where = session.execute.call_args.args[0].whereclause
        assert where is not None

    async def test_edge_metadata_source_and_target_node_ids_remapped(self) -> None:
        template_id = uuid.uuid4()
        template = _orm_template(template_id, [{"id": "only", "node_type": "agent"}])
        session = _session_returns(template)
        composite_id = uuid.uuid4()
        downstream = uuid.uuid4()
        nodes = [
            _composite_node(composite_id, template_id),
            {"id": str(downstream), "node_type": "agent"},
        ]
        edges = [
            {
                "id": "e1",
                "source": str(composite_id),
                "target": str(downstream),
                "type": "normal",
                "source_node_id": str(composite_id),
                "target_node_id": str(downstream),
            },
        ]
        out_nodes, out_edges, _ = await expand_composites_in_graph(session, None, nodes, edges)
        sub_id = next(n["id"] for n in out_nodes if n.get("_composite_parent_id") == str(composite_id))
        e1 = out_edges[0]
        assert e1["source"] == sub_id
        assert e1["source_node_id"] == sub_id
        assert e1["target_node_id"] == str(downstream)

    async def test_edge_to_unknown_node_id_passes_through(self) -> None:
        template_id = uuid.uuid4()
        template = _orm_template(template_id, [{"id": "only", "node_type": "agent"}])
        session = _session_returns(template)
        composite_id = uuid.uuid4()
        ghost = uuid.uuid4()
        nodes = [_composite_node(composite_id, template_id)]
        edges = [
            {"id": "e1", "source": str(ghost), "target": str(composite_id), "type": "normal"},
        ]
        out_nodes, out_edges, _ = await expand_composites_in_graph(session, None, nodes, edges)
        sub_id = next(n["id"] for n in out_nodes if n.get("_composite_parent_id") == str(composite_id))
        assert out_edges == [{"id": "e1", "source": str(ghost), "target": sub_id, "type": "normal"}]

    async def test_nested_composite_with_edges_in_cycle(self) -> None:
        inner_tid = uuid.uuid4()
        inner = _orm_template(
            inner_tid,
            [
                {"id": "in1", "node_type": "agent"},
                {"id": "in2", "node_type": "agent"},
            ],
        )
        outer_tid = uuid.uuid4()
        outer = _orm_template(
            outer_tid,
            [
                {"id": "a", "node_type": "agent"},
                {"id": "b", "node_type": "agent"},
                {
                    "id": "nested",
                    "node_type": "composite",
                    "composite_ref": str(inner_tid),
                    "composite_parameter_values": {"x": 1},
                },
            ],
            edges=[
                {"id": "s1", "source": "a", "target": "b", "type": "normal"},
                {"id": "s2", "source": "b", "target": "nested", "type": "normal"},
                {"id": "s3", "source": "nested", "target": "a", "type": "normal"},
            ],
        )
        session = _session_returns(outer, inner)
        composite_id = uuid.uuid4()
        nodes = [_composite_node(composite_id, outer_tid)]
        edges = [
            {
                "id": "loop1",
                "source": str(composite_id),
                "target": str(composite_id),
                "type": "loop",
                "default_target": str(composite_id),
            },
        ]
        out_nodes, out_edges, bindings = await expand_composites_in_graph(session, None, nodes, edges)

        assert len(bindings) == 2
        assert {b["composite_template_id"] for b in bindings} == {str(outer_tid), str(inner_tid)}
        assert any(n.get("_composite_parent_id") == "nested" for n in out_nodes)
        loop = next((e for e in out_edges if e["id"] == "loop1"), None)
        assert loop is None
        assert any(e["id"] == "s1" for e in out_edges)
        assert any(e["id"] == "s2" for e in out_edges)
        assert any(e["id"] == "s3" for e in out_edges)

    async def test_default_target_to_composite_with_no_entry_nodes_unchanged(self) -> None:
        template_id = uuid.uuid4()
        template = _orm_template(
            template_id,
            [
                {"id": "n1", "node_type": "agent"},
                {"id": "n2", "node_type": "agent"},
            ],
            edges=[
                {"id": "c1", "source": "n1", "target": "n2", "type": "normal"},
                {"id": "c2", "source": "n2", "target": "n1", "type": "normal"},
            ],
        )
        session = _session_returns(template)
        composite_id = uuid.uuid4()
        leaf = uuid.uuid4()
        nodes = [
            {"id": str(leaf), "node_type": "agent"},
            _composite_node(composite_id, template_id),
        ]
        edges = [
            {
                "id": "loop1",
                "source": str(leaf),
                "target": str(leaf),
                "type": "loop",
                "default_target": str(composite_id),
            },
        ]
        out_nodes, out_edges, _ = await expand_composites_in_graph(session, None, nodes, edges)

        assert any(n.get("_composite_parent_id") == str(composite_id) for n in out_nodes)
        loop = next(e for e in out_edges if e["id"] == "loop1")
        assert loop["source"] == str(leaf)
        assert loop["target"] == str(leaf)
        assert loop["default_target"] == str(composite_id)
