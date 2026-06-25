"""Create immutable execution snapshots from the editable pipeline graph."""

import copy
import hashlib
import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.agent import Agent
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_edge import PipelineEdge
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.schema import Schema


def _ids(values: Iterable[Any]) -> set[uuid.UUID]:
    return {uuid.UUID(str(value)) for value in values if value is not None}


async def create_snapshot_from_live_graph(
    session: AsyncSession,
    *,
    pipeline_id: uuid.UUID,
    created_by: uuid.UUID | None = None,
) -> PipelineSnapshot | None:
    """Lock and copy the authoritative live graph into an immutable snapshot.

    The caller must already be inside a transaction with the organisation RLS
    context set. Locking the pipeline makes snapshot version allocation and the
    graph copy atomic with respect to graph replacement.
    """
    pipeline_result = await session.execute(select(Pipeline).where(Pipeline.id == pipeline_id).with_for_update())
    pipeline = pipeline_result.scalar_one_or_none()
    if pipeline is None:
        return None

    edge_result = await session.execute(
        select(PipelineEdge)
        .where(PipelineEdge.pipeline_id == pipeline_id)
        .order_by(PipelineEdge.created_at, PipelineEdge.id)
    )
    edges = list(edge_result.scalars())
    nodes = copy.deepcopy(list(pipeline.graph_nodes_json))

    agent_ids = _ids(node.get("agent_id") for node in nodes)
    agents: list[Agent] = []
    if agent_ids:
        agents = list((await session.execute(select(Agent).where(Agent.id.in_(agent_ids)))).scalars())

    connector_ids = _ids(
        binding.get("instance_id") for node in nodes if (binding := node.get("connector_binding")) is not None
    )
    connectors: list[ConnectorInstance] = []
    if connector_ids:
        connectors = list(
            (await session.execute(select(ConnectorInstance).where(ConnectorInstance.id.in_(connector_ids)))).scalars()
        )
    connectors_by_id = {connector.id: connector for connector in connectors}

    schema_ids = {schema_id for agent in agents for schema_id in (agent.input_schema_id, agent.output_schema_id)}
    schemas: list[Schema] = []
    if schema_ids:
        schemas = list((await session.execute(select(Schema).where(Schema.id.in_(schema_ids)))).scalars())
    schemas_by_id = {schema.id: schema for schema in schemas}

    backend_ids = {agent.model_backend_id for agent in agents}
    backends: list[ModelBackend] = []
    if backend_ids:
        backends = list((await session.execute(select(ModelBackend).where(ModelBackend.id.in_(backend_ids)))).scalars())
    backends_by_id = {backend.id: backend for backend in backends}

    version_result = await session.execute(
        select(func.coalesce(func.max(PipelineSnapshot.snapshot_version), 0)).where(
            PipelineSnapshot.pipeline_id == pipeline_id
        )
    )
    snapshot_version = int(version_result.scalar_one()) + 1

    connector_bindings: list[dict[str, Any]] = []
    for node in nodes:
        binding = node.get("connector_binding")
        if binding is None:
            continue
        connector_id = uuid.UUID(str(binding["instance_id"]))
        connector = connectors_by_id.get(connector_id)
        connector_bindings.append(
            {
                "node_id": str(node["id"]),
                "connector_instance_id": str(connector_id),
                "connector_type": (connector.connector_type_id if connector is not None else binding.get("type")),
                "instance_name": connector.name if connector is not None else None,
            }
        )

    schema_pins: list[dict[str, Any]] = []
    seen_schema_pins: set[tuple[uuid.UUID, str]] = set()
    for agent in agents:
        for schema_id, version in (
            (agent.input_schema_id, agent.input_schema_version),
            (agent.output_schema_id, agent.output_schema_version),
        ):
            key = (schema_id, version)
            if key in seen_schema_pins:
                continue
            seen_schema_pins.add(key)
            schema = schemas_by_id.get(schema_id)
            schema_pins.append(
                {
                    "schema_id": str(schema_id),
                    "version": version,
                    "abstract_name": schema.abstract_name if schema is not None else None,
                }
            )

    prompt_pins = [
        {
            "agent_id": str(agent.id),
            "prompt_version_hash": hashlib.sha256(agent.prompt_template.encode()).hexdigest(),
            "prompt_version_at": agent.updated_at.isoformat(),
        }
        for agent in agents
    ]
    model_backend_pins = [
        {
            "agent_id": str(agent.id),
            "model_backend_id": str(agent.model_backend_id),
            "model_id": backend.model_id,
        }
        for agent in agents
        if (backend := backends_by_id.get(agent.model_backend_id)) is not None
    ]

    graph_json = {
        "nodes": nodes,
        "edges": [
            {
                "id": str(edge.id),
                "source": str(edge.source_node_id),
                "target": str(edge.target_node_id),
                "type": edge.edge_type,
                "hitl_gate_config": copy.deepcopy(edge.hitl_gate_config),
            }
            for edge in edges
        ],
    }
    snapshot = PipelineSnapshot(
        organisation_id=pipeline.organisation_id,
        pipeline_id=pipeline.id,
        snapshot_version=snapshot_version,
        created_by=created_by,
        graph_json=graph_json,
        connector_bindings_json=connector_bindings,
        schema_pins_json=schema_pins,
        prompt_pins_json=prompt_pins,
        model_backend_pins_json=model_backend_pins,
        run_context_defaults=copy.deepcopy(pipeline.run_context_defaults),
    )
    session.add(snapshot)
    await session.flush()
    return snapshot
