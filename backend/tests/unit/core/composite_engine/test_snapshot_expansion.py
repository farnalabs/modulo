"""Unit tests for composite expansion during snapshot creation.

Verifies that ``create_snapshot_from_live_graph`` expands composite nodes into
flat sub-pipeline nodes at snapshot time, records composite bindings, and that
the resulting ``graph_json`` compiles via ``build_graph_from_json`` — the
"compilable at runtime" proof.
"""

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.pipeline_engine.graph_cache import build_graph_from_json
from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph
from modulo.db.models.pipeline_snapshot import PipelineSnapshot


def _scalar_result(value: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    result.scalar_one.return_value = value
    return result


def _scalars_result(values: list[object]) -> MagicMock:
    result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = values
    scalars_mock.__iter__.return_value = iter(values)
    result.scalars.return_value = scalars_mock
    return result


def _template_mock(template_id: uuid.UUID, sub_graph: dict[str, Any]) -> MagicMock:
    template = MagicMock()
    template.id = template_id
    template.version = "1.0.0"
    template.sub_pipeline_graph_json = sub_graph
    return template


def _agent_mock(agent_id: uuid.UUID) -> MagicMock:
    agent = MagicMock()
    agent.id = agent_id
    agent.input_schema_id = None
    agent.output_schema_id = None
    agent.prompt_template = "You are a debate advocate."
    agent.updated_at = datetime(2026, 6, 20, tzinfo=UTC)
    agent.model_backend_id = None
    agent.token_budget = None
    agent.max_input_length = None
    agent.parameter_schema_id = None
    agent.agent_command = None
    agent.agent_commands = None
    return agent


async def test_snapshot_with_composite_node_is_expanded_and_compiles() -> None:
    org_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    template_id = uuid.uuid4()
    composite_id = uuid.uuid4()
    target_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    template = _template_mock(
        template_id,
        {
            "nodes": [
                {
                    "id": "advocate-for",
                    "node_type": "agent",
                    "agent_id": str(agent_id),
                    "prompt_template": "argue {{parameter.position}}",
                }
            ],
            "edges": [],
        },
    )

    pipeline = MagicMock()
    pipeline.id = pipeline_id
    pipeline.organisation_id = org_id
    pipeline.graph_nodes_json = [
        {
            "id": str(composite_id),
            "node_type": "composite",
            "composite_ref": str(template_id),
            "composite_parameter_values": {"position": "flying cars"},
        },
        {"id": str(target_id), "agent_id": None, "connector_binding": None},
    ]
    pipeline.run_context_defaults = {}

    edge = MagicMock()
    edge.id = uuid.uuid4()
    edge.source_node_id = composite_id
    edge.target_node_id = target_id
    edge.edge_type = "normal"
    edge.hitl_gate_config = None

    session = AsyncMock(spec=AsyncSession)
    lock_result = MagicMock()
    lock_result.scalar_one.return_value = True
    unlock_result = MagicMock()
    session.execute.side_effect = [
        lock_result,  # 1 pg_try_advisory_lock
        _scalar_result(pipeline),  # 2 pipeline
        _scalars_result([edge]),  # 3 edges
        _scalar_result(template),  # 4 composite template (expander)
        _scalars_result([_agent_mock(agent_id)]),  # 5 agents
        _scalars_result([]),  # 6 schemas (schema_ids contains None)
        _scalar_result(0),  # 7 snapshot version max
        _scalars_result([]),  # 8 guardrail rows (EvalDefinition)
        unlock_result,  # 9 pg_advisory_unlock
    ]

    snapshot = await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)

    assert isinstance(snapshot, PipelineSnapshot)
    nodes = snapshot.graph_json["nodes"]
    assert all(n.get("node_type") != "composite" for n in nodes)
    assert len(nodes) == 2  # expanded sub-node + target node

    sub = next(n for n in nodes if n.get("_composite_parent_id") == str(composite_id))
    assert sub["_composite_index"] == 0
    uuid.UUID(sub["id"])
    assert sub["agent_id"] == str(agent_id)
    # agent materialization ran for the sub-node (agent prompt is authoritative).
    assert sub["prompt_template"] == "You are a debate advocate."

    edges = snapshot.graph_json["edges"]
    assert len(edges) == 1
    assert edges[0]["source"] == sub["id"]
    assert edges[0]["target"] == str(target_id)
    assert edges[0]["type"] == "normal"

    assert snapshot.composite_bindings_json == [
        {
            "composite_template_id": str(template_id),
            "composite_version": "1.0.0",
            "parameter_values": {"position": "flying cars"},
            "input_mapping": None,
            "output_mapping": None,
        }
    ]

    # Compilable proof: build_graph_from_json accepts the expanded graph.
    compiled = build_graph_from_json(snapshot.graph_json, pipeline_node_timeout_seconds=300)
    assert compiled is not None


async def test_snapshot_sub_node_prompt_injection_survives_without_agent() -> None:
    org_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    template_id = uuid.uuid4()
    composite_id = uuid.uuid4()

    template = _template_mock(
        template_id,
        {
            "nodes": [
                {"id": "only", "node_type": "agent", "prompt_template": "argue {{parameter.position}}"},
            ],
            "edges": [],
        },
    )

    pipeline = MagicMock()
    pipeline.id = pipeline_id
    pipeline.organisation_id = org_id
    pipeline.graph_nodes_json = [
        {
            "id": str(composite_id),
            "node_type": "composite",
            "composite_ref": str(template_id),
            "composite_parameter_values": {"position": "flying cars"},
        }
    ]
    pipeline.run_context_defaults = {}

    session = AsyncMock(spec=AsyncSession)
    lock_result = MagicMock()
    lock_result.scalar_one.return_value = True
    unlock_result = MagicMock()
    session.execute.side_effect = [
        lock_result,  # 1 pg_try_advisory_lock
        _scalar_result(pipeline),  # 2 pipeline
        _scalars_result([]),  # 3 edges
        _scalar_result(template),  # 4 composite template (expander)
        _scalar_result(0),  # 5 snapshot version max
        _scalars_result([]),  # 6 guardrail rows (EvalDefinition)
        unlock_result,  # 7 pg_advisory_unlock
    ]

    snapshot = await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)

    nodes = snapshot.graph_json["nodes"]
    assert len(nodes) == 1
    assert nodes[0]["prompt_template"] == "argue flying cars"
    assert not snapshot.graph_json["edges"]

    compiled = build_graph_from_json(snapshot.graph_json, pipeline_node_timeout_seconds=300)
    assert compiled is not None


async def test_snapshot_composite_template_missing_raises() -> None:
    org_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    template_id = uuid.uuid4()
    composite_id = uuid.uuid4()

    pipeline = MagicMock()
    pipeline.id = pipeline_id
    pipeline.organisation_id = org_id
    pipeline.graph_nodes_json = [
        {
            "id": str(composite_id),
            "node_type": "composite",
            "composite_ref": str(template_id),
        }
    ]
    pipeline.run_context_defaults = {}

    session = AsyncMock(spec=AsyncSession)
    lock_result = MagicMock()
    lock_result.scalar_one.return_value = True
    unlock_result = MagicMock()
    session.execute.side_effect = [
        lock_result,  # 1 pg_try_advisory_lock
        _scalar_result(pipeline),  # 2 pipeline
        _scalars_result([]),  # 3 edges
        _scalar_result(None),  # 4 composite template missing
        unlock_result,  # 5 pg_advisory_unlock
    ]

    with pytest.raises(ValueError, match=str(template_id)):
        await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)
