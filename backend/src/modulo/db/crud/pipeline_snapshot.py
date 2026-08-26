"""Create immutable execution snapshots from the editable pipeline graph."""

import copy
import hashlib
import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.composite_engine.expander import expand_composites_in_graph
from modulo.core.exceptions import SnapshotLockNotAvailableError
from modulo.db.models.agent import Agent
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.parameter_schema import ParameterSchema
from modulo.db.models.parameter_set import ParameterSet
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_edge import PipelineEdge
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.schema import Schema
from modulo.db.models.snapshot_schema_pin import SnapshotSchemaPin


def _ids(values: Iterable[Any]) -> set[uuid.UUID]:
    return {uuid.UUID(str(value)) for value in values if value is not None}


def _pipeline_lock_keys(pipeline_id: uuid.UUID) -> tuple[int, int]:
    """Derive two int4 advisory lock keys from a pipeline UUID using MD5."""
    digest = hashlib.md5(str(pipeline_id).encode("ascii"), usedforsecurity=False).digest()
    key1 = int.from_bytes(digest[:4], "big", signed=True)
    key2 = int.from_bytes(digest[4:8], "big", signed=True)
    return (key1, key2)


async def _load_pipeline_and_edges(
    session: AsyncSession, pipeline_id: uuid.UUID
) -> tuple[Pipeline | None, list[dict[str, Any]], list[dict[str, Any]]]:
    pipeline_result = await session.execute(select(Pipeline).where(Pipeline.id == pipeline_id))
    pipeline = pipeline_result.scalar_one_or_none()
    if pipeline is None:
        return (None, [], [])
    edge_result = await session.execute(
        select(PipelineEdge)
        .where(PipelineEdge.pipeline_id == pipeline_id)
        .order_by(PipelineEdge.created_at, PipelineEdge.id)
    )
    edges = list(edge_result.scalars())
    nodes = copy.deepcopy(list(pipeline.graph_nodes_json or []))
    edge_dicts = [
        {
            "id": str(edge.id),
            "source": str(edge.source_node_id),
            "target": str(edge.target_node_id),
            "type": edge.edge_type,
            "hitl_gate_config": copy.deepcopy(edge.hitl_gate_config),
        }
        for edge in edges
    ]
    return (pipeline, nodes, edge_dicts)


def _apply_agent_fields(node: dict[str, Any], agent: Agent) -> uuid.UUID | None:
    if agent.token_budget is not None:
        node["token_budget"] = agent.token_budget
    if agent.prompt_template is not None:
        node["prompt_template"] = agent.prompt_template
    if agent.model_backend_id is not None:
        node["model_backend_id"] = str(agent.model_backend_id)
    if agent.agent_command is not None:
        node["agent_command"] = agent.agent_command
    if agent.agent_commands is not None:
        node["agent_commands"] = agent.agent_commands
    if agent.parameter_schema_id is not None:
        node["parameter_schema_id"] = str(agent.parameter_schema_id)
        return agent.parameter_schema_id
    return None


async def _materialize_agent_fields(
    session: AsyncSession, nodes: list[dict[str, Any]]
) -> tuple[list[Agent], dict[uuid.UUID, Agent], set[uuid.UUID]]:
    agent_ids = _ids(node.get("agent_id") for node in nodes)
    agents: list[Agent] = []
    agents_by_id: dict[uuid.UUID, Agent] = {}
    if agent_ids:
        agents = list((await session.execute(select(Agent).where(Agent.id.in_(agent_ids)))).scalars())
        agents_by_id = {a.id: a for a in agents}

    parameter_schema_ids: set[uuid.UUID] = set()
    for node in nodes:
        agent_id = node.get("agent_id")
        if agent_id is None:
            continue
        agent = agents_by_id.get(uuid.UUID(str(agent_id)))
        if agent is None:
            continue
        schema_id = _apply_agent_fields(node, agent)
        if schema_id is not None:
            parameter_schema_ids.add(schema_id)
    return (agents, agents_by_id, parameter_schema_ids)


async def _load_parameter_schemas(
    session: AsyncSession, parameter_schema_ids: set[uuid.UUID]
) -> dict[uuid.UUID, ParameterSchema]:
    schema_rows = (
        (await session.execute(select(ParameterSchema).where(ParameterSchema.id.in_(parameter_schema_ids))))
        .scalars()
        .all()
    )
    return {s.id: s for s in schema_rows}


def _collect_parameter_set_ids(nodes: list[dict[str, Any]]) -> set[uuid.UUID]:
    set_ids: set[uuid.UUID] = set()
    for node in nodes:
        raw_set_id = node.get("parameter_set_id")
        if raw_set_id is not None:
            set_ids.add(uuid.UUID(str(raw_set_id)))
    return set_ids


async def _load_parameter_sets(session: AsyncSession, set_ids: set[uuid.UUID]) -> dict[uuid.UUID, ParameterSet]:
    sets_by_id: dict[uuid.UUID, ParameterSet] = {}
    if set_ids:
        set_rows = (await session.execute(select(ParameterSet).where(ParameterSet.id.in_(set_ids)))).scalars().all()
        sets_by_id = {s.id: s for s in set_rows}
    return sets_by_id


def _resolve_node_parameters(
    schema: ParameterSchema, sets_by_id: dict[uuid.UUID, ParameterSet], node: dict[str, Any]
) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    for param in schema.parameters or []:
        if isinstance(param, dict) and "name" in param:
            resolved[param["name"]] = param.get("default")

    raw_set_id = node.get("parameter_set_id")
    if raw_set_id is not None:
        ps = sets_by_id.get(uuid.UUID(str(raw_set_id)))
        if ps is not None and isinstance(ps.values, dict):
            resolved.update(ps.values)

    overrides = node.get("parameter_overrides")
    if isinstance(overrides, dict):
        resolved.update(overrides)
    return resolved


def _build_parameter_binding(
    node: dict[str, Any], schema_id: uuid.UUID, resolved: dict[str, Any], raw_set_id: Any
) -> dict[str, Any]:
    return {
        "agent_id": str(node.get("agent_id")) if node.get("agent_id") else None,
        "parameter_schema_id": str(schema_id),
        "parameter_set_id": str(raw_set_id) if raw_set_id is not None else None,
        "resolved_values": resolved,
    }


async def _resolve_parameter_bindings(
    session: AsyncSession,
    nodes: list[dict[str, Any]],
    parameter_schema_ids: set[uuid.UUID],
) -> dict[str, Any]:
    parameter_bindings: dict[str, Any] = {}
    if parameter_schema_ids:
        schemas_by_id = await _load_parameter_schemas(session, parameter_schema_ids)
        set_ids = _collect_parameter_set_ids(nodes)
        sets_by_id = await _load_parameter_sets(session, set_ids)

        for node in nodes:
            raw_schema_id = node.get("parameter_schema_id")
            if raw_schema_id is None:
                continue
            schema_id = uuid.UUID(str(raw_schema_id))
            schema = schemas_by_id.get(schema_id)
            if schema is None:
                continue
            resolved = _resolve_node_parameters(schema, sets_by_id, node)
            node["_resolved_parameters"] = resolved
            raw_set_id = node.get("parameter_set_id")
            parameter_bindings[str(node["id"])] = _build_parameter_binding(node, schema_id, resolved, raw_set_id)
    return parameter_bindings


async def _load_reference_models(
    session: AsyncSession, nodes: list[dict[str, Any]], agents: list[Agent]
) -> tuple[dict[uuid.UUID, ConnectorInstance], dict[uuid.UUID, Schema], dict[uuid.UUID, ModelBackend]]:
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
    schema_models_by_id: dict[uuid.UUID, Schema] = {schema.id: schema for schema in schemas}

    backend_ids = {agent.model_backend_id for agent in agents if agent.model_backend_id is not None}
    backends: list[ModelBackend] = []
    if backend_ids:
        backends = list((await session.execute(select(ModelBackend).where(ModelBackend.id.in_(backend_ids)))).scalars())
    backends_by_id = {backend.id: backend for backend in backends}
    return (connectors_by_id, schema_models_by_id, backends_by_id)


def _build_connector_bindings(
    nodes: list[dict[str, Any]], connectors_by_id: dict[uuid.UUID, ConnectorInstance]
) -> list[dict[str, Any]]:
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
    return connector_bindings


def _build_schema_pins(agents: list[Agent], schema_models_by_id: dict[uuid.UUID, Schema]) -> list[dict[str, Any]]:
    schema_pins: list[dict[str, Any]] = []
    seen_schema_pins: set[tuple[uuid.UUID, str]] = set()
    for agent in agents:
        for schema_pin_id, schema_pin_version in (
            (agent.input_schema_id, agent.input_schema_version),
            (agent.output_schema_id, agent.output_schema_version),
        ):
            if schema_pin_id is None or schema_pin_version is None:
                continue
            key = (schema_pin_id, schema_pin_version)
            if key in seen_schema_pins:
                continue
            seen_schema_pins.add(key)
            schema_model = schema_models_by_id.get(schema_pin_id)
            schema_pins.append(
                {
                    "schema_id": str(schema_pin_id),
                    "version": schema_pin_version,
                    "abstract_name": schema_model.abstract_name if schema_model is not None else None,
                }
            )
    return schema_pins


def _build_prompt_and_backend_pins(
    agents: list[Agent], backends_by_id: dict[uuid.UUID, ModelBackend]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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
        if agent.model_backend_id is not None and (backend := backends_by_id.get(agent.model_backend_id)) is not None
    ]
    return (prompt_pins, model_backend_pins)


async def _load_guardrail_pins(session: AsyncSession, pipeline: Pipeline) -> list[dict[str, Any]] | None:
    from modulo.core.guardrails import serialize_guardrail_pin
    from modulo.db.crud.guardrail_config import load_pipeline_guardrail_rows

    guardrail_rows = await load_pipeline_guardrail_rows(
        session,
        pipeline_id=pipeline.id,
        organisation_id=pipeline.organisation_id,
    )
    return [serialize_guardrail_pin(row) for row in guardrail_rows] or None


def _fingerprint_guardrail_pins(pins: list[dict[str, Any]] | None) -> str | None:
    """Canonical SHA-256 over the serialized guardrail pin set (FAR-309 PR B).

    Localized wrapper so the snapshot CRUD layer never reaches into the
    engine's internals — the fingerprint is computed by the shared guardrails
    module helper and only the digest is stored on the snapshot.
    """
    from modulo.core.guardrails import fingerprint_guardrail_pins

    return fingerprint_guardrail_pins(pins)


def _add_snapshot_schema_pins(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    nodes: list[dict[str, Any]],
) -> None:
    for node in nodes:
        raw_input_pin = node.get("input_schema_pin")
        raw_output_pin = node.get("output_schema_pin")
        if raw_input_pin is not None:
            session.add(
                SnapshotSchemaPin(
                    organisation_id=organisation_id,
                    snapshot_id=snapshot_id,
                    node_id=uuid.UUID(str(node["id"])),
                    direction="input",
                    schema_id=uuid.UUID(str(raw_input_pin["schema_id"])),
                    schema_version=str(raw_input_pin.get("schema_version", "")),
                )
            )
        if raw_output_pin is not None:
            session.add(
                SnapshotSchemaPin(
                    organisation_id=organisation_id,
                    snapshot_id=snapshot_id,
                    node_id=uuid.UUID(str(node["id"])),
                    direction="output",
                    schema_id=uuid.UUID(str(raw_output_pin["schema_id"])),
                    schema_version=str(raw_output_pin.get("schema_version", "")),
                )
            )


async def create_snapshot_from_live_graph(
    session: AsyncSession,
    *,
    pipeline_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
    version_kind: str = "run",
    created_kind: str = "run",
    draft: bool = False,
    channel: str = "none",
) -> PipelineSnapshot | None:
    """Lock and copy the authoritative live graph into an immutable snapshot.

    The caller must already be inside a transaction with the organisation RLS
    context set. Uses a Postgres advisory lock (session-scoped) to serialise
    snapshot creation for a given pipeline, avoiding transaction-scoped FOR
    UPDATE so the caller's transaction is not blocked during graph loading.

    FAR-402 P6: the run-start callers (webhook/replay/trigger/manual/slack)
    keep the defaults and produce a ``version_kind='run'`` snapshot; live-edit
    saves go through ``create_snapshot_edit`` which passes ``version_kind='edit'``
    so the live-edit chain stays distinguishable from run-frozen snapshots.
    """
    # Acquire session-scoped advisory lock to serialise snapshot creation.
    key1, key2 = _pipeline_lock_keys(pipeline_id)
    lock_result = await session.execute(
        text("SELECT pg_try_advisory_lock(:key1, :key2)"),
        {"key1": key1, "key2": key2},
    )
    if not lock_result.scalar_one():
        raise SnapshotLockNotAvailableError(f"Cannot acquire snapshot lock for pipeline {pipeline_id}")

    try:
        pipeline, nodes, edge_dicts = await _load_pipeline_and_edges(session, pipeline_id)
        if pipeline is None:
            return None

        # Expand composite nodes into flat sub-pipeline nodes BEFORE agent
        # materialization so sub-node agents get their prompt/model_backend
        # embedded like top-level nodes. After this the snapshot graph contains
        # only flat node types and the compiled runtime needs no changes.
        nodes, edge_dicts, composite_bindings = await expand_composites_in_graph(
            session,
            org_id=pipeline.organisation_id,
            nodes=nodes,
            edges=edge_dicts,
        )

        agents, _, parameter_schema_ids = await _materialize_agent_fields(session, nodes)
        parameter_bindings = await _resolve_parameter_bindings(session, nodes, parameter_schema_ids)
        connectors_by_id, schema_models_by_id, backends_by_id = await _load_reference_models(session, nodes, agents)

        try:
            version_result = await session.execute(
                select(func.coalesce(func.max(PipelineSnapshot.snapshot_version), 0)).where(
                    PipelineSnapshot.pipeline_id == pipeline_id
                )
            )
            snapshot_version = int(version_result.scalar_one()) + 1
        except ProgrammingError:
            return None

        connector_bindings = _build_connector_bindings(nodes, connectors_by_id)
        schema_pins = _build_schema_pins(agents, schema_models_by_id)
        prompt_pins, model_backend_pins = _build_prompt_and_backend_pins(agents, backends_by_id)

        graph_json = {
            "nodes": nodes,
            "edges": edge_dicts,
        }

        # Guardrail snapshot pin (FAR-223 item 10): serialize the pipeline's
        # bound guardrail rows so a replay evaluates the ORIGINAL conditions
        # (the pinned set), never the live rows. Loaded here — not inside
        # create_run — so the pin is immutable like the graph itself.
        guardrail_pins = await _load_guardrail_pins(session, pipeline)
        # Run-start snapshot-integrity fingerprint (FAR-309 PR B): the digest
        # of the serialized pin set is saved alongside it so the replay seam
        # can detect a tampered/drifted pin set and fail closed.
        guardrail_pins_fingerprint = _fingerprint_guardrail_pins(guardrail_pins)

        snapshot = PipelineSnapshot(
            organisation_id=pipeline.organisation_id,
            pipeline_id=pipeline.id,
            snapshot_version=snapshot_version,
            account_id=account_id,
            graph_json=graph_json,
            connector_bindings_json=connector_bindings,
            schema_pins_json=schema_pins,
            prompt_pins_json=prompt_pins,
            model_backend_pins_json=model_backend_pins,
            composite_bindings_json=composite_bindings or None,
            parameter_bindings_json=parameter_bindings or None,
            guardrail_pins_json=guardrail_pins,
            guardrail_pins_fingerprint=guardrail_pins_fingerprint,
            run_context_defaults=copy.deepcopy(pipeline.run_context_defaults),
            version_kind=version_kind,
            created_kind=created_kind,
            draft=draft,
            channel=channel,
        )
        session.add(snapshot)
        await session.flush()

        _add_snapshot_schema_pins(session, pipeline.organisation_id, snapshot.id, nodes)

        return snapshot
    finally:
        await session.execute(
            text("SELECT pg_advisory_unlock(:key1, :key2)"),
            {"key1": key1, "key2": key2},
        )


async def create_snapshot_edit(
    session: AsyncSession,
    *,
    pipeline_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
    draft: bool = False,
    channel: str = "none",
) -> PipelineSnapshot | None:
    """Snapshot the live graph as a LIVE-EDIT version (FAR-402 P6).

    A live-edit save reuses the snapshot machinery but tags the row as
    ``version_kind='edit'`` / ``created_kind='edit'``, so the editor's save
    history (the live-edit chain) is distinguishable from run-frozen snapshots.
    Each save leaves the prior snapshot row immutable, so rollback remains a
    pointer swap to a prior snapshot (``rollback_to_snapshot``).
    """
    return await create_snapshot_from_live_graph(
        session,
        pipeline_id=pipeline_id,
        account_id=account_id,
        version_kind="edit",
        created_kind="edit",
        draft=draft,
        channel=channel,
    )
