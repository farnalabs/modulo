"""Workflow bundle export and import service.

Produces portable .zip bundles that carry pipeline + agent + schema definitions
but strip org-private details (owner_team_id, connector credentials, api keys).
Import resolves local equivalents via a binding wizard.
"""

from __future__ import annotations

import asyncio
import copy
import io
import json
import logging
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from typing import Any, NamedTuple, TypeGuard

import yaml
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.graph_validator import GraphValidator
from modulo.core.graph_validator._types import ValidationResult
from modulo.db.crud.agent import create_agent
from modulo.db.crud.library_primitive import create_library_primitive
from modulo.db.crud.pipeline import create_pipeline
from modulo.db.crud.schema import create_schema, create_schema_version
from modulo.db.models.account import Account
from modulo.db.models.agent import Agent
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_edge import PipelineEdge
from modulo.db.models.schema import Schema, SchemaVersion
from modulo.db.models.team import Team
from modulo.db.models.trigger import Trigger
from modulo.util import sanitise_log_value as _sanitise_log_value

logger = logging.getLogger(__name__)

BUNDLE_FORMAT_VERSION = "1"
MANIFEST_FILENAME = "bundle.json"
DEFAULT_SCHEMA_VERSION = "1.0"

# Suffix appended to imported names that collide with existing entities.
_IMPORTED_SUFFIX = "(imported)"
DEFAULT_NODE_TIMEOUT = 300
_MAX_NAME_RETRIES = 5
VALID_EDGE_TYPES: frozenset[str] = frozenset({"normal", "reject", "conditional", "loop"})

# ---------------------------------------------------------------------------
# Shared value objects
# ---------------------------------------------------------------------------


class _V2ExportParts(NamedTuple):
    """Pieces of a v2 YAML bundle gathered from the database."""

    agents: list[dict[str, Any]]
    schemas: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    triggers: list[dict[str, Any]]
    owner_team_name: str | None
    author: str


@dataclass(frozen=True)
class _ImportOverrides:
    """Override maps applied while materialising an import bundle."""

    model_backends: dict[str, str] = field(default_factory=dict)
    schema_ids: dict[str, str] = field(default_factory=dict)
    schema_versions: dict[str, str] = field(default_factory=dict)
    connector_instances: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _ImportSections:
    """Parsed top-level sections of an import bundle."""

    pipeline_info: dict[str, Any]
    agents_data: list[dict[str, Any]]
    schemas_data: list[dict[str, Any]]
    edges_data: list[dict[str, Any]]
    name: str


@dataclass(frozen=True)
class _ImportContext:
    """Ambient state shared by every import-materialisation helper."""

    session: AsyncSession
    org_id: uuid.UUID
    created_by: uuid.UUID
    warnings: list[str]
    owner_team_id: uuid.UUID | None = None
    overrides: _ImportOverrides = field(default_factory=_ImportOverrides)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_latest_published_version(
    session: AsyncSession,
    schema_id: uuid.UUID,
) -> SchemaVersion | None:
    try:
        sv_result = await session.execute(
            select(SchemaVersion)
            .where(
                SchemaVersion.schema_id == schema_id,
                SchemaVersion.published.is_(True),
            )
            .order_by(SchemaVersion.version_number.desc())
            .limit(1)
        )
        return sv_result.scalar_one_or_none()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Failed to fetch latest published version for schema %s", schema_id)
        raise


async def _get_existing_names(
    session: AsyncSession,
    org_id: uuid.UUID,
    model_cls: type[Any],
    *,
    for_update: bool = False,
) -> set[str]:
    try:
        stmt = select(model_cls.name).where(model_cls.organisation_id == org_id)
        if for_update:
            stmt = stmt.with_for_update()
        result = await session.execute(stmt)
        return {row[0] for row in result}
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("_get_existing_names: failed to fetch names for %s", model_cls.__name__)
        raise


def _safe_uuid(value: Any, label: str = "field") -> uuid.UUID:
    """Convert a value to UUID, raising ValueError with a descriptive message."""
    try:
        return uuid.UUID(value) if not isinstance(value, uuid.UUID) else value
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"Invalid UUID for {label}: {value!r}") from exc


def _sanitize_slug(name: str) -> str:
    """Produce a URL-safe slug from a pipeline name."""
    slug = name.lower().replace(" ", "-").replace("_", "-")
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-") or "imported-pipeline"


def _retries_exhausted(attempt: int) -> bool:
    """True when the name-collision retry budget is exhausted."""
    return attempt == _MAX_NAME_RETRIES - 1


def _has_matching_definition(existing_sv: SchemaVersion | None, definition: Any) -> TypeGuard[SchemaVersion]:
    """True when an existing published schema version equals the exported definition."""
    return existing_sv is not None and existing_sv.definition_json == definition


def _is_unresolved_ref(ref_id: str, resolved_id: str | None) -> bool:
    """True when a referenced id has no resolved local equivalent."""
    return bool(ref_id) and not resolved_id


def _is_valid_edge_type(edge_type: str) -> bool:
    """True when the edge type is one of the supported graph edge kinds."""
    return edge_type in VALID_EDGE_TYPES


# ---------------------------------------------------------------------------
# Export — pipeline_id → ZIP bytes
# ---------------------------------------------------------------------------


async def _load_pipeline(session: AsyncSession, pipeline_id: uuid.UUID) -> Pipeline:
    """Fetch a pipeline by id, raising ValueError when it does not exist."""
    stmt = select(Pipeline).where(Pipeline.id == pipeline_id)
    pipeline = (await session.execute(stmt)).scalar_one_or_none()
    if pipeline is None:
        raise ValueError(f"Pipeline {pipeline_id} not found")
    return pipeline


async def _fetch_and_project_edges(session: AsyncSession, pipeline_id: uuid.UUID) -> list[dict[str, Any]]:
    edge_result = await session.execute(
        select(PipelineEdge).where(PipelineEdge.pipeline_id == pipeline_id).order_by(PipelineEdge.created_at)
    )
    edges = list(edge_result.scalars())
    return [
        {
            "id": str(e.id),
            "source_node_id": str(e.source_node_id),
            "target_node_id": str(e.target_node_id),
            "edge_type": e.edge_type,
            "hitl_gate_config": e.hitl_gate_config,
        }
        for e in edges
    ]


async def export_pipeline_bundle(
    session: AsyncSession,
    pipeline_id: uuid.UUID,
) -> bytes:
    """Build a portable ZIP bundle from a pipeline.

    Strips owner_team_id and other org-private fields.
    """
    try:
        pipeline = await _load_pipeline(session, pipeline_id)

        agent_ids, schema_ids, model_backend_ids = _collect_referenced_ids(pipeline.graph_nodes_json)

        agents_list = await _build_agents_list(session, agent_ids, schema_ids, model_backend_ids)
        schemas_list = await _build_schemas_list(session, schema_ids)
        model_backends_list = await _build_model_backends_list(session, model_backend_ids)

        edges_list = await _fetch_and_project_edges(session, pipeline_id)

        bundle: dict[str, Any] = {
            "format_version": BUNDLE_FORMAT_VERSION,
            "pipeline": {
                "name": pipeline.name,
                "description": pipeline.description,
                "graph_nodes_json": pipeline.graph_nodes_json or [],
                "run_context_defaults": dict(pipeline.run_context_defaults or {}),
                "node_timeout_seconds": pipeline.node_timeout_seconds,
                "retry_policy": dict(pipeline.retry_policy or {}),
                "visibility": "org",  # Always strip team scoping
            },
            "agents": agents_list,
            "schemas": schemas_list,
            "model_backends": model_backends_list,
            "edges": edges_list,
        }
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("export_pipeline_bundle: failed while building bundle for pipeline %s", pipeline_id)
        raise

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST_FILENAME, json.dumps(bundle, indent=2, default=str))

    logger.info("Exported pipeline %s with %d agents, %d edges", pipeline_id, len(agents_list), len(edges_list))
    return buf.getvalue()


def _collect_referenced_ids(
    graph_nodes_json: list[dict[str, Any]] | None,
) -> tuple[set[uuid.UUID], set[uuid.UUID], set[uuid.UUID]]:
    """Collect agent/schema/model-backend ids referenced by the graph nodes."""
    agent_ids: set[uuid.UUID] = set()
    schema_ids: set[uuid.UUID] = set()
    model_backend_ids: set[uuid.UUID] = set()

    if not graph_nodes_json:
        return agent_ids, schema_ids, model_backend_ids

    for node in graph_nodes_json:
        agent_id_str = node.get("agent_id")
        if agent_id_str:
            try:
                agent_ids.add(_safe_uuid(agent_id_str, "node.agent_id"))
            except ValueError:
                logger.warning("Skipping node with invalid agent_id: %s", agent_id_str)
                continue

        schema_id_str = node.get("output_schema_id")
        if schema_id_str:
            try:
                schema_ids.add(_safe_uuid(schema_id_str, "node.output_schema_id"))
            except ValueError:
                logger.warning("Skipping node with invalid output_schema_id: %s", schema_id_str)

    return agent_ids, schema_ids, model_backend_ids


async def _build_agents_list(
    session: AsyncSession,
    agent_ids: set[uuid.UUID],
    schema_ids: set[uuid.UUID],
    model_backend_ids: set[uuid.UUID],
) -> list[dict[str, Any]]:
    """Build the bundle's agents section, extending the referenced-id sets."""
    agents_list: list[dict[str, Any]] = []
    if not agent_ids:
        return agents_list
    agent_result = await session.execute(select(Agent).where(Agent.id.in_(agent_ids)))
    agents = list(agent_result.scalars())
    for a in agents:
        agents_list.append(
            {
                "id": str(a.id),
                "name": a.name,
                "description": a.description,
                "input_schema_id": str(a.input_schema_id) if a.input_schema_id else None,
                "input_schema_version": a.input_schema_version or DEFAULT_SCHEMA_VERSION,
                "output_schema_id": str(a.output_schema_id) if a.output_schema_id else None,
                "output_schema_version": a.output_schema_version or DEFAULT_SCHEMA_VERSION,
                "prompt_template": a.prompt_template,
                "model_backend_id": str(a.model_backend_id) if a.model_backend_id else None,
                "connector_type_refs": list(a.connector_type_refs or []),
                "evals": list(a.evals or []),
                "retry_policy": dict(a.retry_policy or {}),
                "token_budget": a.token_budget,
            }
        )
        if a.input_schema_id:
            schema_ids.add(a.input_schema_id)
        if a.output_schema_id:
            schema_ids.add(a.output_schema_id)
        if a.model_backend_id:
            model_backend_ids.add(a.model_backend_id)
    return agents_list


async def _build_schemas_list(session: AsyncSession, schema_ids: set[uuid.UUID]) -> list[dict[str, Any]]:
    """Build the bundle's schemas section from the referenced schema ids."""
    schemas_list: list[dict[str, Any]] = []
    if not schema_ids:
        return schemas_list
    schema_result = await session.execute(select(Schema).where(Schema.id.in_(schema_ids)))
    schemas = list(schema_result.scalars())
    for s in schemas:
        latest_version = await _get_latest_published_version(session, s.id)
        schemas_list.append(
            {
                "id": str(s.id),
                "name": s.name,
                "description": s.description,
                "abstract_name": s.abstract_name,
                "latest_version": latest_version.version if latest_version else None,
                "definition_json": latest_version.definition_json if latest_version else None,
            }
        )
    return schemas_list


async def _build_model_backends_list(session: AsyncSession, model_backend_ids: set[uuid.UUID]) -> list[dict[str, Any]]:
    """Build the bundle's model-backends section from the referenced ids."""
    if not model_backend_ids:
        return []
    mb_result = await session.execute(select(ModelBackend).where(ModelBackend.id.in_(model_backend_ids)))
    backends = list(mb_result.scalars())
    return [
        {
            "id": str(b.id),
            "name": b.name,
            "provider": b.provider,
            "model_id": b.model_id,
        }
        for b in backends
    ]


# ---------------------------------------------------------------------------
# Export v2 — pipeline_id → YAML string
# ---------------------------------------------------------------------------


async def export_pipeline_bundle_v2(
    session: AsyncSession,
    pipeline_id: uuid.UUID,
) -> str:
    """Build a v2 YAML bundle from a pipeline per ADR 015.

    Returns a YAML string with the v2 bundle format including triggers,
    owner_team, visibility, lifecyle_map_ref, composite_template_refs,
    and partial bundle support.
    """
    try:
        pipeline = await _load_pipeline(session, pipeline_id)
        agent_ids, schema_ids = _collect_agent_and_schema_ids(pipeline.graph_nodes_json)
        parts = await _gather_v2_export_parts(session, pipeline, agent_ids, schema_ids)
        bundle = _build_v2_bundle(pipeline, parts)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("export_pipeline_bundle_v2: failed while building bundle for pipeline %s", pipeline_id)
        raise

    yaml_str = yaml.safe_dump(bundle, default_flow_style=False, sort_keys=False, allow_unicode=True)
    logger.info(
        "Exported v2 bundle for pipeline %s with %d agents, %d edges",
        pipeline_id,
        len(parts.agents),
        len(parts.edges),
    )
    return yaml_str


async def _gather_v2_export_parts(
    session: AsyncSession,
    pipeline: Pipeline,
    agent_ids: set[uuid.UUID],
    schema_ids: set[uuid.UUID],
) -> _V2ExportParts:
    """Fetch and project every piece of a v2 bundle for a pipeline."""
    agents_list = await _build_v2_agents_list(session, agent_ids, schema_ids)
    schemas_list = await _build_schemas_list(session, schema_ids)
    edges_list = await _fetch_and_project_edges_v2(session, pipeline.id)
    triggers_list = await _fetch_v2_triggers(session, pipeline.id)
    owner_team_name = await _resolve_owner_team_name(session, pipeline.owner_team_id)
    author = await _resolve_author_email(session, pipeline)
    return _V2ExportParts(
        agents=agents_list,
        schemas=schemas_list,
        edges=edges_list,
        triggers=triggers_list,
        owner_team_name=owner_team_name,
        author=author,
    )


def _build_v2_bundle(pipeline: Pipeline, parts: _V2ExportParts) -> dict[str, Any]:
    """Assemble the v2 ``modulo_workflow`` envelope from gathered parts."""
    requires = _build_v2_requires(parts.agents, parts.schemas)
    return {
        "modulo_workflow": {
            "id": str(pipeline.id),
            "name": pipeline.name,
            "version": "1.0.0",
            "author": parts.author,
            "owner_team": parts.owner_team_name,
            "visibility": pipeline.visibility,
            "lifecycle_map_ref": None,
            "composite_template_refs": [],
            "partial": False,
            "requires": requires,
            "triggers": parts.triggers,
            "agents": parts.agents,
            "edges": parts.edges,
            "schemas": parts.schemas,
        }
    }


def _collect_agent_and_schema_ids(
    graph_nodes_json: list[dict[str, Any]] | None,
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """Collect agent + schema ids referenced by the v2 bundle graph nodes."""
    agent_ids: set[uuid.UUID] = set()
    schema_ids: set[uuid.UUID] = set()
    if not graph_nodes_json:
        return agent_ids, schema_ids
    for node in graph_nodes_json:
        agent_id_str = node.get("agent_id")
        if agent_id_str:
            try:
                agent_ids.add(_safe_uuid(agent_id_str, "node.agent_id"))
            except ValueError:
                logger.warning("Skipping node with invalid agent_id: %s", agent_id_str)
                continue
        schema_id_str = node.get("output_schema_id")
        if schema_id_str:
            try:
                schema_ids.add(_safe_uuid(schema_id_str, "node.output_schema_id"))
            except ValueError:
                logger.warning("Skipping node with invalid output_schema_id: %s", schema_id_str)
    return agent_ids, schema_ids


async def _build_v2_agents_list(
    session: AsyncSession,
    agent_ids: set[uuid.UUID],
    schema_ids: set[uuid.UUID],
) -> list[dict[str, Any]]:
    """Build the v2 bundle's agents section, extending the schema-id set."""
    agents_list: list[dict[str, Any]] = []
    if not agent_ids:
        return agents_list
    agent_result = await session.execute(select(Agent).where(Agent.id.in_(agent_ids)))
    agents = list(agent_result.scalars())
    for a in agents:
        agent_entry: dict[str, Any] = {
            "id": str(a.id),
            "name": a.name,
            "description": a.description,
            "input_schema": str(a.input_schema_id) if a.input_schema_id else None,
            "output_schema": str(a.output_schema_id) if a.output_schema_id else None,
            "prompt_template": a.prompt_template,
            "template_id": a.template_id,
            "agent_command": a.agent_command,
            "connector_type_refs": list(a.connector_type_refs or []),
            "evals": list(a.evals or []),
            "retry_policy": dict(a.retry_policy or {}),
            "token_budget": a.token_budget,
        }
        agents_list.append(agent_entry)
        if a.input_schema_id:
            schema_ids.add(a.input_schema_id)
        if a.output_schema_id:
            schema_ids.add(a.output_schema_id)
    return agents_list


async def _fetch_and_project_edges_v2(session: AsyncSession, pipeline_id: uuid.UUID) -> list[dict[str, Any]]:
    """Project a pipeline's edges into the v2 bundle shape (source/target)."""
    edge_result = await session.execute(
        select(PipelineEdge).where(PipelineEdge.pipeline_id == pipeline_id).order_by(PipelineEdge.created_at)
    )
    edges = list(edge_result.scalars())
    return [
        {
            "source": str(e.source_node_id),
            "target": str(e.target_node_id),
            "edge_type": e.edge_type,
            "hitl_gate_config": e.hitl_gate_config,
        }
        for e in edges
    ]


def _build_v2_requires(
    agents_list: list[dict[str, Any]],
    schemas_list: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """Collect the v2 bundle's required connector types and abstract schemas."""
    connector_type_refs_set: set[str] = set()
    for a_entry in agents_list:
        for ref in a_entry.get("connector_type_refs", []):
            ctid = ref.get("connector_type_id", ref.get("type", ""))
            if ctid:
                connector_type_refs_set.add(ctid)

    abstract_schema_names_set: set[str] = set()
    for s_entry in schemas_list:
        aname = s_entry.get("abstract_name")
        if aname:
            abstract_schema_names_set.add(aname)

    return {
        "connector_types": sorted(connector_type_refs_set),
        "abstract_schemas": sorted(abstract_schema_names_set),
    }


async def _fetch_v2_triggers(session: AsyncSession, pipeline_id: uuid.UUID) -> list[dict[str, Any]]:
    """Fetch the pipeline's triggers for the v2 bundle (best-effort)."""
    try:
        trig_stmt = select(Trigger).where(Trigger.pipeline_id == pipeline_id)
        triggers = (await session.execute(trig_stmt)).scalars().all()
        return [
            {
                "trigger_type": t.trigger_type,
                "config": dict(t.config_json or {}),
                "active": t.active,
            }
            for t in triggers
        ]
    except Exception:
        logger.warning(
            "Could not fetch triggers for v2 bundle export (table may not exist yet)",
            exc_info=True,
        )
        return []


async def _resolve_owner_team_name(session: AsyncSession, owner_team_id: uuid.UUID | None) -> str | None:
    """Resolve the pipeline's owner-team name (best-effort)."""
    if not owner_team_id:
        return None
    try:
        team_result = await session.execute(select(Team).where(Team.id == owner_team_id))
        team = team_result.scalar_one_or_none()
        return team.name if team else None
    except Exception:
        logger.warning("Could not resolve owner_team for v2 bundle export", exc_info=True)
        return None


async def _resolve_author_email(session: AsyncSession, pipeline: Pipeline) -> str:
    """Resolve the pipeline's creator email for the v2 bundle author field."""
    author = str(pipeline.account_id)
    try:
        acct_result = await session.execute(select(Account).where(Account.id == pipeline.account_id))
        creator = acct_result.scalar_one_or_none()
        if creator:
            author = creator.email
    except Exception:
        logger.warning("Could not resolve creator email for v2 bundle export", exc_info=True)
    return author


# ---------------------------------------------------------------------------
# Import helpers — resolve references from a bundle to local equivalents
# ---------------------------------------------------------------------------


async def resolve_schema(
    session: AsyncSession,
    org_id: uuid.UUID,
    export_schema: dict[str, Any],
) -> dict[str, Any]:
    """Find a local schema matching the exported one.

    Returns mapping with schema_id, version, and a warning string.
    """
    definition = export_schema.get("definition_json")
    abstract_name = export_schema.get("abstract_name")
    name = export_schema.get("name")
    if not name:
        return {
            "schema_id": None,
            "version": None,
            "warning": "Schema entry missing 'name' field.",
        }

    try:
        # First try abstract_name match
        if abstract_name:
            matched = await _resolve_schema_by_abstract_name(session, org_id, abstract_name)
            if matched is not None:
                return matched

        # Try matching by same definition structure — batch load all schema versions
        if definition:
            matched = await _resolve_schema_by_definition(session, org_id, definition)
            if matched is not None:
                return matched
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("resolve_schema: DB query failed for schema '%s'", name)
        raise

    return {
        "schema_id": None,
        "version": None,
        "warning": f"Schema '{name}' not found locally. It will need to be created.",
    }


async def _resolve_schema_by_abstract_name(
    session: AsyncSession,
    org_id: uuid.UUID,
    abstract_name: str,
) -> dict[str, Any] | None:
    """Match an exported schema by its abstract_name (most specific)."""
    stmt = (
        select(Schema)
        .where(
            Schema.organisation_id == org_id,
            Schema.abstract_name == abstract_name,
        )
        .order_by(Schema.created_at.desc())
    )
    result = await session.execute(stmt)
    schema = result.scalar_one_or_none()
    if schema is None:
        return None
    sv = await _get_latest_published_version(session, schema.id)
    return {
        "schema_id": str(schema.id),
        "version": sv.version if sv else DEFAULT_SCHEMA_VERSION,
        "warning": None,
    }


async def _resolve_schema_by_definition(
    session: AsyncSession,
    org_id: uuid.UUID,
    definition: Any,
) -> dict[str, Any] | None:
    """Match an exported schema by its published definition JSON structure."""
    all_schemas = (await session.execute(select(Schema).where(Schema.organisation_id == org_id))).scalars().all()
    schema_ids = [s.id for s in all_schemas]
    if not schema_ids:
        return None
    all_svs = (
        (
            await session.execute(
                select(SchemaVersion)
                .where(
                    SchemaVersion.schema_id.in_(schema_ids),
                    SchemaVersion.published.is_(True),
                )
                .order_by(SchemaVersion.schema_id, SchemaVersion.version_number.desc())
            )
        )
        .scalars()
        .all()
    )

    published: dict[uuid.UUID, SchemaVersion] = {}
    for sv in all_svs:
        if sv.schema_id not in published:
            published[sv.schema_id] = sv

    for s in all_schemas:
        candidate_sv = published.get(s.id)
        if candidate_sv and candidate_sv.definition_json == definition:
            return {
                "schema_id": str(s.id),
                "version": candidate_sv.version,
                "warning": None,
            }
    return None


async def resolve_connector_type(
    session: AsyncSession,
    org_id: uuid.UUID,
    connector_type_id: str,
) -> dict[str, Any]:
    """Find a local connector instance matching the given type."""
    try:
        stmt = (
            select(ConnectorInstance)
            .where(
                ConnectorInstance.organisation_id == org_id,
                ConnectorInstance.connector_type_id == connector_type_id,
                ConnectorInstance.status == "active",
            )
            .order_by(ConnectorInstance.created_at.desc())
        )
        result = await session.execute(stmt)
        instances = list(result.scalars())
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("resolve_connector_type: DB query failed for type '%s'", connector_type_id)
        raise

    if instances:
        return {
            "instance_id": str(instances[0].id),
            "instance_name": instances[0].name,
            "warning": None,
        }
    return {
        "instance_id": None,
        "instance_name": None,
        "warning": (f"Connector type '{connector_type_id}' not found locally. A matching instance must be created."),
    }


async def resolve_model_backend(
    session: AsyncSession,
    org_id: uuid.UUID,
    export_backend: dict[str, Any],
) -> dict[str, Any]:
    """Find a local model backend matching the exported one by name or provider+model_id."""
    name = export_backend.get("name")
    provider = export_backend.get("provider")
    model_id = export_backend.get("model_id")
    if not name or not provider or not model_id:
        return {
            "model_backend_id": None,
            "warning": "Model backend entry is missing required fields (name, provider, model_id).",
        }

    try:
        # Try by name first
        stmt = (
            select(ModelBackend)
            .where(
                ModelBackend.organisation_id == org_id,
                ModelBackend.name == name,
                ModelBackend.status == "active",
            )
            .order_by(ModelBackend.created_at.desc())
        )
        result = await session.execute(stmt)
        backend = result.scalar_one_or_none()
        if backend is not None:
            return {
                "model_backend_id": str(backend.id),
                "warning": None,
            }

        # Try by provider+model_id
        stmt2 = select(ModelBackend).where(
            ModelBackend.organisation_id == org_id,
            ModelBackend.provider == provider,
            ModelBackend.model_id == model_id,
            ModelBackend.status == "active",
        )
        result2 = await session.execute(stmt2)
        backend2 = result2.scalar_one_or_none()
        if backend2 is not None:
            return {
                "model_backend_id": str(backend2.id),
                "warning": None,
            }
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("resolve_model_backend: DB query failed for '%s' (%s/%s)", name, provider, model_id)
        raise

    return {
        "model_backend_id": None,
        "warning": f"Model backend '{name}' ({provider}/{model_id}) not found locally.",
    }


def suggest_import_name(
    existing_names: set[str],
    proposed_name: str,
    *,
    suffix: str = _IMPORTED_SUFFIX,
    max_length: int = 255,
) -> str:
    """Suggest a non-colliding name by appending a suffix.

    When a suffix must be added, the base name is clamped so the candidate
    always fits *max_length* (the name-column width, e.g. ``String(255)``) —
    otherwise a max-width imported name would overflow the column after
    suffixing. Reserved counter room keeps the numbered form within bounds too.
    """
    if proposed_name not in existing_names:
        return proposed_name
    # Reserve room for " <suffix>" and the dedupe counter so every numbered
    # candidate stays within max_length. With max_length=255 and
    # suffix="(imported)" that is 4 counter digits, so the loop stops at 9999 —
    # allowing idx=10000 (5 digits) would overflow the name column.
    reserved = len(suffix) + 2 + 4
    base = proposed_name[: max_length - reserved]
    candidate = f"{base} {suffix}"
    if candidate not in existing_names:
        return candidate
    max_idx = 10 ** (reserved - len(suffix) - 2) - 1
    idx = 2
    while f"{base} {suffix} {idx}" in existing_names and idx < max_idx:
        idx += 1
    return f"{base} {suffix} {idx}"


def _sanitize_retry_policy(imported: Any) -> dict[str, Any]:
    """Validate an imported pipeline ``retry_policy``; coerce malformed to {}.

    A malformed policy is a hard pre-run failure at execute time
    (``GraphValidator.check_retry_policy`` → ``GraphValidationError``), which
    would break EVERY run of an imported pipeline. Import is best-effort copy:
    never let a malformed bundled policy permanently break the imported
    pipeline's runs, so invalid policies are dropped to the no-policy default
    ({}). A valid policy is returned unchanged (as a shallow copy).
    """
    if not isinstance(imported, dict):
        return {}
    check = ValidationResult()
    GraphValidator.check_retry_policy(imported, check)
    if check.is_valid:
        return dict(imported)
    return {}


async def get_existing_pipeline_names(
    session: AsyncSession,
    org_id: uuid.UUID,
) -> set[str]:
    return await _get_existing_names(session, org_id, Pipeline)


async def get_existing_agent_names(
    session: AsyncSession,
    org_id: uuid.UUID,
) -> set[str]:
    return await _get_existing_names(session, org_id, Agent)


# ---------------------------------------------------------------------------
# ZIP extraction and analysis (server-side)
# ---------------------------------------------------------------------------


def extract_bundle_json_from_zip(zip_bytes: bytes) -> dict[str, Any]:
    """Extract bundle.json from a .modulo.zip archive."""
    if len(zip_bytes) > 100 * 1024 * 1024:
        raise ValueError(f"Bundle too large: {len(zip_bytes)} bytes (max 100 MB)")

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            if MANIFEST_FILENAME not in names:
                raise LookupError(f"{MANIFEST_FILENAME} not found in archive (found: {names})")
            result: dict[str, Any] = json.loads(zf.read(MANIFEST_FILENAME))
            return result
    except zipfile.BadZipFile as exc:
        raise ValueError("Invalid bundle: not a valid ZIP archive") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid bundle: {MANIFEST_FILENAME} contains malformed JSON") from exc


# ---------------------------------------------------------------------------
# Materialize — create real database entities from a bundle
# ---------------------------------------------------------------------------


async def materialize_import(
    session: AsyncSession,
    org_id: uuid.UUID,
    created_by: uuid.UUID,
    bundle: dict[str, Any],
    *,
    owner_team_id: uuid.UUID | None = None,
    pipeline_name_override: str | None = None,
    model_backend_overrides: dict[str, str] | None = None,
    schema_id_overrides: dict[str, str] | None = None,
    schema_version_overrides: dict[str, str] | None = None,
    connector_instance_overrides: dict[str, str] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Create pipeline, agents, schemas, and edges from an import bundle.

    Returns a dict with created entity IDs and any warnings.
    """
    _validate_bundle_format(bundle)
    await _validate_owner_team(session, org_id, owner_team_id)

    ctx = _ImportContext(
        session=session,
        org_id=org_id,
        created_by=created_by,
        warnings=warnings or [],
        owner_team_id=owner_team_id,
        overrides=_ImportOverrides(
            model_backends=model_backend_overrides or {},
            schema_ids=schema_id_overrides or {},
            schema_versions=schema_version_overrides or {},
            connector_instances=connector_instance_overrides or {},
        ),
    )
    sections = _resolve_bundle_sections(bundle, pipeline_name_override)

    existing_agent_names = await get_existing_agent_names(session, org_id)
    existing_pipeline_names = await get_existing_pipeline_names(session, org_id)

    pname = suggest_import_name(existing_pipeline_names, sections.name)
    _log_import_start(pname, sections)

    schema_id_map, schema_version_map = await _materialize_schemas(ctx, sections.schemas_data)

    agent_id_map = await _materialize_agents(
        ctx,
        sections.agents_data,
        existing_agent_names,
        schema_id_map,
        schema_version_map,
    )

    pipeline, pipeline_edges_added, prim = await _materialize_pipeline_and_edges(
        ctx,
        sections,
        pname,
        existing_pipeline_names,
        agent_id_map,
        schema_id_map,
        bundle,
    )

    return _build_import_result(
        pname,
        pipeline,
        prim,
        sections,
        pipeline_edges_added,
        agent_id_map,
        schema_id_map,
        ctx.warnings,
    )


def _log_import_start(pname: str, sections: _ImportSections) -> None:
    """Log the materialisation start with section counts."""
    logger.info(
        "Materializing import: pipeline='%s' (%d agents, %d schemas, %d edges)",
        _sanitise_log_value(pname),
        len(sections.agents_data),
        len(sections.schemas_data),
        len(sections.edges_data),
    )


async def _materialize_pipeline_and_edges(
    ctx: _ImportContext,
    sections: _ImportSections,
    pname: str,
    existing_pipeline_names: set[str],
    agent_id_map: dict[str, str],
    schema_id_map: dict[str, str],
    bundle: dict[str, Any],
) -> tuple[Pipeline, list[PipelineEdge], Any]:
    """Create the imported pipeline, its edges, and the library primitive."""
    graph_nodes = _rewire_graph_nodes(
        _normalize_graph_nodes(sections.pipeline_info, ctx.warnings),
        agent_id_map,
        schema_id_map,
        ctx.overrides.connector_instances,
    )

    pipeline = await _create_imported_pipeline(
        ctx,
        pname,
        sections.pipeline_info,
        existing_pipeline_names,
        sections.name,
    )

    pipeline.graph_nodes_json = list(graph_nodes)
    _apply_imported_retry_policy(pipeline, sections.pipeline_info, ctx.warnings)
    await ctx.session.flush()

    pipeline_edges_added = await _materialize_edges(ctx, pipeline, sections.edges_data)
    await ctx.session.flush()

    prim = await _create_import_primitive(ctx, pipeline, bundle, pname, sections.pipeline_info)

    return pipeline, pipeline_edges_added, prim


def _build_import_result(
    pname: str,
    pipeline: Pipeline,
    prim: Any,
    sections: _ImportSections,
    pipeline_edges_added: list[PipelineEdge],
    agent_id_map: dict[str, str],
    schema_id_map: dict[str, str],
    warnings: list[str],
) -> dict[str, Any]:
    """Build the import result dict and log the completion summary."""
    logger.info(
        "Imported pipeline '%s' (id=%s) with %d agents, %d edges, %d schemas",
        _sanitise_log_value(pname),
        pipeline.id,
        len(sections.agents_data),
        len(pipeline_edges_added),
        len(sections.schemas_data),
    )

    return {
        "pipeline_id": str(pipeline.id),
        "pipeline_name": pname,
        "primitive_id": str(prim.id),
        "agent_count": len(sections.agents_data),
        "edge_count": len(pipeline_edges_added),
        "schema_count": len(sections.schemas_data),
        "agents": agent_id_map,
        "schemas": schema_id_map,
        "warnings": warnings,
    }


def _validate_bundle_format(bundle: dict[str, Any]) -> None:
    """Reject bundles whose format version is unsupported."""
    fmt_version = bundle.get("format_version")
    if fmt_version == BUNDLE_FORMAT_VERSION:
        return
    msg = (
        f"Unsupported bundle format version '{fmt_version}'. "
        f"Expected '{BUNDLE_FORMAT_VERSION}'. "
        "This bundle may have been created by a different version of Modulo."
    )
    raise ValueError(msg)


async def _validate_owner_team(session: AsyncSession, org_id: uuid.UUID, owner_team_id: uuid.UUID | None) -> None:
    """Ensure an owner team, if provided, belongs to the organisation."""
    if owner_team_id is None:
        return
    team_exists = await session.execute(
        select(Team).where(
            Team.id == owner_team_id,
            Team.organisation_id == org_id,
            Team.deleted_at.is_(None),
        )
    )
    if team_exists.scalar_one_or_none() is None:
        raise ValueError(f"Team {owner_team_id} not found in this organisation.")


def _resolve_bundle_sections(
    bundle: dict[str, Any],
    pipeline_name_override: str | None,
) -> _ImportSections:
    """Pull the bundle's top-level sections and resolve the pipeline name."""
    pipeline_info = bundle.get("pipeline") or {}
    return _ImportSections(
        pipeline_info=pipeline_info,
        agents_data=bundle.get("agents") or [],
        schemas_data=bundle.get("schemas") or [],
        edges_data=bundle.get("edges") or [],
        name=pipeline_name_override or pipeline_info.get("name", "Imported Pipeline"),
    )


def _normalize_graph_nodes(pipeline_info: dict[str, Any], warnings: list[str]) -> list[dict[str, Any]]:
    """Return the graph nodes list, warning and defaulting to empty on non-list input."""
    raw_graph_nodes = pipeline_info.get("graph_nodes_json")
    if isinstance(raw_graph_nodes, list):
        return raw_graph_nodes
    warnings.append("Pipeline 'graph_nodes_json' is not a list; nodes will be empty.")
    return []


def _apply_imported_retry_policy(pipeline: Pipeline, pipeline_info: dict[str, Any], warnings: list[str]) -> None:
    """Sanitise the imported retry_policy so a malformed policy never breaks runs."""
    imported_retry_policy = pipeline_info.get("retry_policy")
    sanitized_retry_policy = _sanitize_retry_policy(imported_retry_policy)
    if imported_retry_policy is not None and sanitized_retry_policy != imported_retry_policy:
        warnings.append("Imported pipeline 'retry_policy' was malformed; dropped to the no-policy default ({}).")
    pipeline.retry_policy = sanitized_retry_policy


async def _create_import_primitive(
    ctx: _ImportContext,
    pipeline: Pipeline,
    bundle: dict[str, Any],
    pname: str,
    pipeline_info: dict[str, Any],
) -> Any:
    """Create the library primitive wrapping the imported workflow."""
    try:
        return await create_library_primitive(
            ctx.session,
            org_id=ctx.org_id,
            source="local",
            primitive_type="workflow",
            name=pname,
            slug=_sanitize_slug(pname),
            description=pipeline_info.get("description", ""),
            author=ctx.created_by.hex[:8],
            version=DEFAULT_SCHEMA_VERSION,
            tags=["imported"],
            content_json={
                "pipeline_id": str(pipeline.id),
                "bundle": bundle,
            },
            source_url=None,
            forked_from=None,
            checksum=None,
            ed25519_signature=None,
            verified=None,
            download_count=None,
            average_rating=None,
            review_count=None,
            owner_team_id=ctx.owner_team_id,
            visibility="org",
            account_id=ctx.created_by,
        )
    except Exception:
        logger.exception("Failed to create library primitive for pipeline '%s'", _sanitise_log_value(pname))
        raise


async def _reconcile_existing_schema(
    ctx: _ImportContext,
    sname: str,
    definition: Any,
    export_schema_id: str,
    existing_schema_names: set[str] | None,
) -> tuple[set[str] | None, str, dict[str, dict[str, str]] | None]:
    """Resolve a schema name collision against an existing local schema.

    Returns ``(existing_schema_names, resolved_sname, reuse)``. When a local
    schema with a matching definition exists, ``reuse`` holds the id/version
    mapping fragments keyed by ``export_schema_id`` and the schema should be
    reused rather than created. Otherwise ``reuse`` is ``None`` and
    ``resolved_sname`` is the (possibly suffixed) name to create.
    """
    existing_stmt = select(Schema).where(Schema.organisation_id == ctx.org_id, Schema.name == sname)
    existing_result = await ctx.session.execute(existing_stmt)
    existing_schema = existing_result.scalar_one_or_none()
    if existing_schema is None:
        return existing_schema_names, sname, None

    existing_sv = await _get_latest_published_version(ctx.session, existing_schema.id)
    if existing_sv is not None and existing_sv.definition_json != definition:
        if existing_schema_names is None:
            existing_schema_names = await _load_schema_names(ctx)
        sname = suggest_import_name(existing_schema_names, sname, suffix="(imported)")
        existing_schema_names.add(sname)
        ctx.warnings.append(
            f"Schema '{existing_schema.name}' exists with different structure. Created as '{sname}' instead."
        )
        return existing_schema_names, sname, None

    if _has_matching_definition(existing_sv, definition):
        return (
            existing_schema_names,
            sname,
            {
                "schema_id_map": {export_schema_id: str(existing_schema.id)},
                "schema_version_map": {export_schema_id: existing_sv.version},
            },
        )

    return existing_schema_names, sname, None


async def _materialize_schemas(
    ctx: _ImportContext,
    schemas_data: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, str]]:
    """Create any schemas that don't exist locally; return the id/version maps."""
    schema_id_map: dict[str, str] = {}
    schema_version_map: dict[str, str] = {}
    existing_schema_names: set[str] | None = None

    for sd in schemas_data:
        export_schema_id = sd.get("id", "")
        if not export_schema_id:
            ctx.warnings.append("Skipping schema with no 'id' field in bundle.")
            continue

        if export_schema_id in ctx.overrides.schema_ids:
            schema_id_map[export_schema_id] = ctx.overrides.schema_ids[export_schema_id]
            if export_schema_id in ctx.overrides.schema_versions:
                schema_version_map[export_schema_id] = ctx.overrides.schema_versions[export_schema_id]
            continue

        existing_schema_id = sd.get("_resolved_id")
        if existing_schema_id:
            schema_id_map[export_schema_id] = existing_schema_id
            existing_version = sd.get("_resolved_version")
            if existing_version:
                schema_version_map[export_schema_id] = existing_version
            continue

        definition = sd.get("definition_json")
        if not definition:
            ctx.warnings.append(
                f"Schema '{sd.get('name', 'unknown')}' has no definition JSON and will be skipped. "
                "Agents referencing this schema may fail."
            )
            continue

        sname: str = sd.get("name", "Imported Schema")

        # Check for existing schema with same name but different definition
        existing_schema_names, sname, reuse = await _reconcile_existing_schema(
            ctx,
            sname,
            definition,
            export_schema_id,
            existing_schema_names,
        )
        if reuse is not None:
            schema_id_map.update(reuse["schema_id_map"])
            schema_version_map.update(reuse["schema_version_map"])
            continue

        new_schema = await _create_schema_with_retry(ctx, sname, sd, existing_schema_names)

        schema_id_map[export_schema_id] = str(new_schema.id)

        try:
            new_sv = await create_schema_version(
                ctx.session,
                org_id=ctx.org_id,
                schema_id=new_schema.id,
                version=sd.get("latest_version") or DEFAULT_SCHEMA_VERSION,
                version_number=1,
                definition_json=definition,
                account_id=ctx.created_by,
                published=True,
            )
        except Exception:
            logger.exception("Failed to create schema version for '%s'", sname)
            raise

        schema_version_map[export_schema_id] = new_sv.version

    return schema_id_map, schema_version_map


async def _load_schema_names(ctx: _ImportContext) -> set[str]:
    """Load all local schema names for collision-avoiding suggestions."""
    all_existing = (
        (await ctx.session.execute(select(Schema).where(Schema.organisation_id == ctx.org_id))).scalars().all()
    )
    return {s.name for s in all_existing}


async def _create_schema_with_retry(
    ctx: _ImportContext,
    sname: str,
    sd: dict[str, Any],
    existing_schema_names: set[str] | None,
) -> Schema:
    """Create a schema, retrying with a suffixed name on IntegrityError collision."""
    attempt_sc = 0
    while True:
        try:
            async with ctx.session.begin_nested():
                return await create_schema(
                    ctx.session,
                    org_id=ctx.org_id,
                    name=sname,
                    account_id=ctx.created_by,
                    description=sd.get("description"),
                    abstract_name=sd.get("abstract_name"),
                )
        except IntegrityError:
            if _retries_exhausted(attempt_sc):
                raise
            attempt_sc += 1
            if existing_schema_names is None:
                existing_schema_names = await _load_schema_names(ctx)
            existing_schema_names.add(sname)
            new_sname = suggest_import_name(existing_schema_names, sname, suffix=_IMPORTED_SUFFIX)
            ctx.warnings.append(f"Schema name '{sname}' collided; retrying as '{new_sname}'.")
            sname = new_sname
            existing_schema_names.add(sname)
        except Exception:
            logger.exception("Failed to create schema '%s'", sname)
            raise


async def _materialize_agents(
    ctx: _ImportContext,
    agents_data: list[dict[str, Any]],
    existing_agent_names: set[str],
    schema_id_map: dict[str, str],
    schema_version_map: dict[str, str],
) -> dict[str, str]:
    """Create the bundle's agents; return the export→local agent id map."""
    agent_id_map: dict[str, str] = {}
    for ad in agents_data:
        export_agent_id = ad.get("id", "")
        aname = suggest_import_name(existing_agent_names, ad.get("name", "Imported Agent"))
        existing_agent_names.add(aname)

        agent_args = _base_agent_args(ctx, aname, ad)
        _apply_agent_references(
            agent_args,
            ad,
            schema_id_map,
            schema_version_map,
            ctx.overrides.model_backends,
            ctx.warnings,
        )

        agent = await _create_agent_with_retry(ctx, agent_args, existing_agent_names, ad.get("name", "Imported Agent"))

        agent_id_map[export_agent_id] = str(agent.id)

    return agent_id_map


def _base_agent_args(
    ctx: _ImportContext,
    aname: str,
    ad: dict[str, Any],
) -> dict[str, Any]:
    """Build the base create_agent keyword arguments for an imported agent."""
    return {
        "session": ctx.session,
        "org_id": ctx.org_id,
        "name": aname,
        "account_id": ctx.created_by,
        "prompt_template": ad.get("prompt_template", ""),
        "description": ad.get("description"),
        "connector_type_refs": ad.get("connector_type_refs"),
        "evals": ad.get("evals"),
        "retry_policy": ad.get("retry_policy"),
        "token_budget": ad.get("token_budget"),
    }


def _apply_agent_references(
    agent_args: dict[str, Any],
    ad: dict[str, Any],
    schema_id_map: dict[str, str],
    schema_version_map: dict[str, str],
    mb_overrides: dict[str, str],
    warnings: list[str],
) -> None:
    """Resolve an agent's schema/model-backend references onto agent_args.

    Appends a warning for every unresolved reference; the reference is then
    omitted from the created agent.
    """
    aname = agent_args["name"]
    input_schema_id_str = ad.get("input_schema_id", "")
    output_schema_id_str = ad.get("output_schema_id", "")
    resolved_input_id = schema_id_map.get(input_schema_id_str)
    resolved_output_id = schema_id_map.get(output_schema_id_str)
    resolved_input_version = schema_version_map.get(input_schema_id_str) or ad.get(
        "input_schema_version", DEFAULT_SCHEMA_VERSION
    )
    resolved_output_version = schema_version_map.get(output_schema_id_str) or ad.get(
        "output_schema_version", DEFAULT_SCHEMA_VERSION
    )

    export_mb_id = ad.get("model_backend_id", "")
    resolved_mb_id = ad.get("_resolved_model_backend_id") or mb_overrides.get(export_mb_id)

    if _is_unresolved_ref(input_schema_id_str, resolved_input_id):
        warnings.append(
            f"Agent '{aname}' references unresolved input schema '{input_schema_id_str}'. "
            "The schema reference will be omitted."
        )
    if _is_unresolved_ref(output_schema_id_str, resolved_output_id):
        warnings.append(
            f"Agent '{aname}' references unresolved output schema '{output_schema_id_str}'. "
            "The schema reference will be omitted."
        )
    if _is_unresolved_ref(export_mb_id, resolved_mb_id):
        warnings.append(
            f"Agent '{aname}' references unresolved model backend '{export_mb_id}'. "
            "The model backend reference will be omitted."
        )
    if resolved_input_id:
        agent_args["input_schema_id"] = _safe_uuid(resolved_input_id, "agent.input_schema_id")
        agent_args["input_schema_version"] = resolved_input_version
    if resolved_output_id:
        agent_args["output_schema_id"] = _safe_uuid(resolved_output_id, "agent.output_schema_id")
        agent_args["output_schema_version"] = resolved_output_version
    if resolved_mb_id:
        agent_args["model_backend_id"] = _safe_uuid(resolved_mb_id, "agent.model_backend_id")


async def _create_agent_with_retry(
    ctx: _ImportContext,
    agent_args: dict[str, Any],
    existing_agent_names: set[str],
    base_name: str,
) -> Agent:
    """Create an agent, retrying with a suffixed name on IntegrityError collision."""
    aname = agent_args["name"]
    attempt_a = 0
    while True:
        try:
            async with ctx.session.begin_nested():
                return await create_agent(**agent_args)
        except IntegrityError:
            if _retries_exhausted(attempt_a):
                raise
            attempt_a += 1
            existing_agent_names.add(aname)
            aname = suggest_import_name(existing_agent_names, base_name)
            existing_agent_names.add(aname)
            agent_args["name"] = aname
            ctx.warnings.append(f"Agent name collided; retrying as '{aname}'.")
        except (ValueError, SQLAlchemyError):
            logger.exception("Failed to create agent '%s'", _sanitise_log_value(aname))
            raise


def _rewire_graph_nodes(
    graph_nodes: list[dict[str, Any]],
    agent_id_map: dict[str, str],
    schema_id_map: dict[str, str],
    conn_overrides: dict[str, str],
) -> list[dict[str, Any]]:
    """Rewire agent/schema/connector references in the imported graph nodes."""
    graph_nodes = copy.deepcopy(graph_nodes)
    for node in graph_nodes:
        node_export_id = node.get("agent_id")
        if node_export_id and node_export_id in agent_id_map:
            node["agent_id"] = agent_id_map[node_export_id]
        node_output_schema = node.get("output_schema_id")
        if node_output_schema and node_output_schema in schema_id_map:
            node["output_schema_id"] = schema_id_map[node_output_schema]
        _rewire_connector_binding(node, conn_overrides)
    return graph_nodes


def _rewire_connector_binding(node: dict[str, Any], conn_overrides: dict[str, str]) -> None:
    """Rewrite a node's connector binding instance id from an override map."""
    connector_binding = node.get("connector_binding")
    if not isinstance(connector_binding, dict):
        return
    existing_id = connector_binding.get("instance_id", "")
    if existing_id and existing_id in conn_overrides:
        connector_binding["instance_id"] = conn_overrides[existing_id]


async def _create_imported_pipeline(
    ctx: _ImportContext,
    pname: str,
    pipeline_info: dict[str, Any],
    existing_pipeline_names: set[str],
    base_name: str,
) -> Pipeline:
    """Create the imported pipeline, retrying with a suffixed name on collision."""
    attempt_p = 0
    while True:
        try:
            async with ctx.session.begin_nested():
                return await create_pipeline(
                    ctx.session,
                    org_id=ctx.org_id,
                    name=pname,
                    account_id=ctx.created_by,
                    description=pipeline_info.get("description"),
                    visibility="org",
                    owner_team_id=ctx.owner_team_id,
                    node_timeout_seconds=pipeline_info.get("node_timeout_seconds") or DEFAULT_NODE_TIMEOUT,
                    run_context_defaults=pipeline_info.get("run_context_defaults"),
                )
        except IntegrityError:
            if _retries_exhausted(attempt_p):
                raise
            attempt_p += 1
            existing_pipeline_names.add(pname)
            pname = suggest_import_name(existing_pipeline_names, base_name)
            ctx.warnings.append(f"Pipeline name '{base_name}' conflicted; retrying as '{pname}'.")
        except Exception:
            logger.exception("Failed to create pipeline '%s'", _sanitise_log_value(pname))
            raise


async def _materialize_edges(
    ctx: _ImportContext,
    pipeline: Pipeline,
    edges_data: list[dict[str, Any]],
) -> list[PipelineEdge]:
    """Create the imported edges; returns the edges added."""
    pipeline_edges_added: list[PipelineEdge] = []
    for ed in edges_data:
        try:
            source_id = _safe_uuid(ed.get("source_node_id", ""), "edge.source_node_id")
            target_id = _safe_uuid(ed.get("target_node_id", ""), "edge.target_node_id")
            edge_id = _safe_uuid(ed["id"]) if ed.get("id") else uuid.uuid4()
        except ValueError as exc:
            ctx.warnings.append(f"Skipping edge with invalid UUID: {exc}")
            continue
        edge_type = ed.get("edge_type", "normal")
        if not _is_valid_edge_type(edge_type):
            ctx.warnings.append(f"Unknown edge type '{edge_type}', defaulting to 'normal'.")
            edge_type = "normal"
        edge = PipelineEdge(
            id=edge_id,
            organisation_id=ctx.org_id,
            pipeline_id=pipeline.id,
            source_node_id=source_id,
            target_node_id=target_id,
            edge_type=edge_type,
            condition_expression=ed.get("condition_expression"),
            hitl_gate_config=ed.get("hitl_gate_config"),
            source_port=ed.get("source_port", "out"),
            target_port=ed.get("target_port", "in"),
        )
        ctx.session.add(edge)
        pipeline_edges_added.append(edge)
    return pipeline_edges_added
