"""Tests for immutable snapshots created from the editable live graph."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph
from modulo.db.models.pipeline_snapshot import PipelineSnapshot


def _scalar_result(value: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    result.scalar_one.return_value = value
    return result


def _scalars_result(values: list[object]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value = values
    return result


async def test_live_graph_becomes_executable_snapshot_with_dependency_pins() -> None:
    org_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    source_id = uuid.uuid4()
    target_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    connector_id = uuid.uuid4()
    input_schema_id = uuid.uuid4()
    output_schema_id = uuid.uuid4()
    backend_id = uuid.uuid4()

    pipeline = MagicMock()
    pipeline.id = pipeline_id
    pipeline.organisation_id = org_id
    pipeline.graph_nodes_json = [
        {
            "id": str(source_id),
            "agent_id": str(agent_id),
            "connector_binding": {
                "type": "filesystem",
                "instance_id": str(connector_id),
            },
        },
        {"id": str(target_id), "agent_id": None, "connector_binding": None},
    ]
    pipeline.run_context_defaults = {"branch": "main"}

    edge = MagicMock()
    edge.id = uuid.uuid4()
    edge.source_node_id = source_id
    edge.target_node_id = target_id
    edge.edge_type = "normal"
    edge.hitl_gate_config = None

    agent = MagicMock()
    agent.id = agent_id
    agent.input_schema_id = input_schema_id
    agent.input_schema_version = "1.0"
    agent.output_schema_id = output_schema_id
    agent.output_schema_version = "2.0"
    agent.prompt_template = "Build the artifact"
    agent.updated_at = datetime(2026, 6, 20, tzinfo=UTC)
    agent.model_backend_id = backend_id

    connector = MagicMock()
    connector.id = connector_id
    connector.connector_type_id = "filesystem"
    connector.name = "Workspace"
    connector.credentials_ciphertext = b"must-not-be-copied"

    input_schema = MagicMock()
    input_schema.id = input_schema_id
    input_schema.abstract_name = "input"
    output_schema = MagicMock()
    output_schema.id = output_schema_id
    output_schema.abstract_name = "output"

    backend = MagicMock()
    backend.id = backend_id
    backend.model_id = "fixed-model-version"
    backend.credentials_ciphertext = b"must-not-be-copied"

    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = [
        _scalar_result(pipeline),
        _scalars_result([edge]),
        _scalars_result([agent]),
        _scalars_result([connector]),
        _scalars_result([input_schema, output_schema]),
        _scalars_result([backend]),
        _scalar_result(4),
    ]

    snapshot = await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)

    assert isinstance(snapshot, PipelineSnapshot)
    assert snapshot.snapshot_version == 5
    assert snapshot.graph_json["nodes"] == pipeline.graph_nodes_json
    assert snapshot.graph_json["edges"] == [
        {
            "id": str(edge.id),
            "source": str(source_id),
            "target": str(target_id),
            "type": "normal",
            "hitl_gate_config": None,
        }
    ]
    assert snapshot.connector_bindings_json[0]["instance_name"] == "Workspace"
    assert snapshot.schema_pins_json == [
        {"schema_id": str(input_schema_id), "version": "1.0", "abstract_name": "input"},
        {"schema_id": str(output_schema_id), "version": "2.0", "abstract_name": "output"},
    ]
    assert snapshot.model_backend_pins_json == [
        {
            "agent_id": str(agent_id),
            "model_backend_id": str(backend_id),
            "model_id": "fixed-model-version",
        }
    ]
    assert "credentials" not in repr(snapshot.connector_bindings_json)
    assert "credentials" not in repr(snapshot.model_backend_pins_json)
    session.add.assert_called_once_with(snapshot)
    session.flush.assert_awaited_once()
