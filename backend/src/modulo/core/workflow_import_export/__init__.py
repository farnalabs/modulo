"""Workflow bundle export and import service.

Produces portable .zip bundles that carry pipeline + agent + schema definitions
but strip org-private details (owner_team_id, connector credentials, api keys).
Import resolves local equivalents via a binding wizard.
"""

from __future__ import annotations

import copy
import io
import json
import logging
import re
import uuid
import zipfile
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.agent import create_agent
from modulo.db.crud.library_primitive import create_library_primitive
from modulo.db.crud.pipeline import create_pipeline
from modulo.db.crud.schema import create_schema, create_schema_version
from modulo.db.models.agent import Agent
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_edge import PipelineEdge
from modulo.db.models.schema import Schema, SchemaVersion
from modulo.db.models.team import Team

logger = logging.getLogger(__name__)

BUNDLE_FORMAT_VERSION = "1"
MANIFEST_FILENAME = "bundle.json"
DEFAULT_SCHEMA_VERSION = "1.0"
DEFAULT_NODE_TIMEOUT = 300
VALID_EDGE_TYPES: frozenset[str] = frozenset({"normal", "conditional", "error", "always", "success"})

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
    except Exception:
        logger.error("Failed to fetch latest published version for schema %s", schema_id)
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
    except Exception:
        logger.error("_get_existing_names: failed to fetch names for %s", model_cls.__name__)
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


# ---------------------------------------------------------------------------
# Export — pipeline_id → ZIP bytes
# ---------------------------------------------------------------------------


async def export_pipeline_bundle(
    session: AsyncSession,
    pipeline_id: uuid.UUID,
) -> bytes:
    """Build a portable ZIP bundle from a pipeline.

    Strips owner_team_id and other org-private fields.
    """
    try:
        stmt = select(Pipeline).where(Pipeline.id == pipeline_id)
        pipeline = (await session.execute(stmt)).scalar_one_or_none()
        if pipeline is None:
            raise ValueError(f"Pipeline {pipeline_id} not found")

        agent_ids: set[uuid.UUID] = set()
        schema_ids: set[uuid.UUID] = set()
        model_backend_ids: set[uuid.UUID] = set()

        if pipeline.graph_nodes_json:
            for node in pipeline.graph_nodes_json:
                agent_id_str = node.get("agent_id")
                if agent_id_str:
                    try:
                        agent_id = _safe_uuid(agent_id_str, "node.agent_id")
                    except ValueError:
                        logger.warning("Skipping node with invalid agent_id: %s", agent_id_str)
                        continue
                    agent_ids.add(agent_id)

                schema_id_str = node.get("output_schema_id")
                if schema_id_str:
                    try:
                        schema_ids.add(_safe_uuid(schema_id_str, "node.output_schema_id"))
                    except ValueError:
                        logger.warning("Skipping node with invalid output_schema_id: %s", schema_id_str)

        agents_list: list[dict[str, Any]] = []
        if agent_ids:
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

        schemas_list: list[dict[str, Any]] = []
        if schema_ids:
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

        model_backends_list: list[dict[str, Any]] = []
        if model_backend_ids:
            mb_result = await session.execute(select(ModelBackend).where(ModelBackend.id.in_(model_backend_ids)))
            backends = list(mb_result.scalars())
            model_backends_list = [
                {
                    "id": str(b.id),
                    "name": b.name,
                    "provider": b.provider,
                    "model_id": b.model_id,
                }
                for b in backends
            ]

        # Edges
        edge_result = await session.execute(
            select(PipelineEdge)
            .where(PipelineEdge.pipeline_id == pipeline_id)
            .order_by(PipelineEdge.created_at)
        )
        edges = list(edge_result.scalars())
        edges_list = [
            {
                "id": str(e.id),
                "source_node_id": str(e.source_node_id),
                "target_node_id": str(e.target_node_id),
                "edge_type": e.edge_type,
                "hitl_gate_config": e.hitl_gate_config,
            }
            for e in edges
        ]

        bundle: dict[str, Any] = {
            "format_version": BUNDLE_FORMAT_VERSION,
            "pipeline": {
                "name": pipeline.name,
                "description": pipeline.description,
                "graph_nodes_json": pipeline.graph_nodes_json or [],
                "run_context_defaults": dict(pipeline.run_context_defaults or {}),
                "node_timeout_seconds": pipeline.node_timeout_seconds,
            },
            "agents": agents_list,
            "schemas": schemas_list,
            "model_backends": model_backends_list,
            "edges": edges_list,
        }
    except Exception:
        logger.error("export_pipeline_bundle: failed while building bundle for pipeline %s", pipeline_id)
        raise

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST_FILENAME, json.dumps(bundle, indent=2, default=str))

    logger.info("Exported pipeline %s with %d agents, %d edges", pipeline_id, len(agents_list), len(edges_list))
    return buf.getvalue()


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
            if schema is not None:
                sv = await _get_latest_published_version(session, schema.id)
                return {
                    "schema_id": str(schema.id),
                    "version": sv.version if sv else DEFAULT_SCHEMA_VERSION,
                    "warning": None,
                }

        # Try matching by same definition structure — batch load all schema versions
        if definition:
            all_schemas = (
                await session.execute(select(Schema).where(Schema.organisation_id == org_id))
            ).scalars().all()
            schema_ids = [s.id for s in all_schemas]
            if schema_ids:
                all_svs = (
                    await session.execute(
                        select(SchemaVersion)
                        .where(
                            SchemaVersion.schema_id.in_(schema_ids),
                            SchemaVersion.published.is_(True),
                        )
                        .order_by(SchemaVersion.schema_id, SchemaVersion.version_number.desc())
                    )
                ).scalars().all()

                published: dict[uuid.UUID, SchemaVersion] = {}
                for sv in all_svs:
                    if sv.schema_id not in published:
                        published[sv.schema_id] = sv

                for s in all_schemas:
                    sv = published.get(s.id)
                    if sv and sv.definition_json == definition:
                        return {
                            "schema_id": str(s.id),
                            "version": sv.version,
                            "warning": None,
                        }
    except Exception:
        logger.error("resolve_schema: DB query failed for schema '%s'", name)
        raise

    return {
        "schema_id": None,
        "version": None,
        "warning": f"Schema '{name}' not found locally. It will need to be created.",
    }


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
    except Exception:
        logger.error("resolve_connector_type: DB query failed for type '%s'", connector_type_id)
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
    except Exception:
        logger.error("resolve_model_backend: DB query failed for '%s' (%s/%s)", name, provider, model_id)
        raise

    return {
        "model_backend_id": None,
        "warning": f"Model backend '{name}' ({provider}/{model_id}) not found locally.",
    }


def suggest_import_name(
    existing_names: set[str],
    proposed_name: str,
    *,
    suffix: str = "(imported)",
) -> str:
    """Suggest a non-colliding name by appending a suffix."""
    if proposed_name not in existing_names:
        return proposed_name
    candidate = f"{proposed_name} {suffix}"
    if candidate not in existing_names:
        return candidate
    idx = 2
    while f"{proposed_name} {suffix} {idx}" in existing_names:
        idx += 1
    return f"{proposed_name} {suffix} {idx}"


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
    # Validate bundle format version
    fmt_version = bundle.get("format_version")
    if fmt_version != BUNDLE_FORMAT_VERSION:
        msg = (
            f"Unsupported bundle format version '{fmt_version}'. "
            f"Expected '{BUNDLE_FORMAT_VERSION}'. "
            "This bundle may have been created by a different version of Modulo."
        )
        raise ValueError(msg)

    if owner_team_id is not None:
        team_exists = await session.execute(
            select(Team).where(Team.id == owner_team_id, Team.organisation_id == org_id)
        )
        if team_exists.scalar_one_or_none() is None:
            raise ValueError(f"Team {owner_team_id} not found in this organisation.")

    mb_overrides: dict[str, str] = model_backend_overrides or {}
    warnings = warnings or []
    schema_overrides: dict[str, str] = schema_id_overrides or {}
    sv_overrides: dict[str, str] = schema_version_overrides or {}
    conn_overrides: dict[str, str] = connector_instance_overrides or {}
    pipeline_info = bundle.get("pipeline") or {}
    agents_data = bundle.get("agents") or []
    schemas_data = bundle.get("schemas") or []
    edges_data = bundle.get("edges") or []
    name = pipeline_name_override or pipeline_info.get("name", "Imported Pipeline")

    existing_agent_names = await get_existing_agent_names(session, org_id)
    existing_pipeline_names = await get_existing_pipeline_names(session, org_id)

    pname = suggest_import_name(existing_pipeline_names, name)

    logger.info(
        "Materializing import: pipeline='%s' (%d agents, %d schemas, %d edges)",
        pname, len(agents_data), len(schemas_data), len(edges_data),
    )

    # --- Step 1: Create any schemas that don't exist locally ---
    schema_id_map: dict[str, str] = {}
    schema_version_map: dict[str, str] = {}
    existing_schema_names: set[str] | None = None

    for sd in schemas_data:
        export_schema_id = sd.get("id", "")
        if not export_schema_id:
            warnings.append("Skipping schema with no 'id' field in bundle.")
            continue

        if export_schema_id in schema_overrides:
            schema_id_map[export_schema_id] = schema_overrides[export_schema_id]
            if export_schema_id in sv_overrides:
                schema_version_map[export_schema_id] = sv_overrides[export_schema_id]
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
            warnings.append(
                f"Schema '{sd.get('name', 'unknown')}' has no definition JSON and will be skipped. "
                "Agents referencing this schema may fail."
            )
            continue

        sname: str = sd.get("name", "Imported Schema")

        # Check for existing schema with same name but different definition
        existing_stmt = select(Schema).where(
            Schema.organisation_id == org_id,
            Schema.name == sname,
        )
        existing_result = await session.execute(existing_stmt)
        existing_schema = existing_result.scalar_one_or_none()
        if existing_schema is not None:
            existing_sv = await _get_latest_published_version(session, existing_schema.id)
            if existing_sv and existing_sv.definition_json != definition:
                if existing_schema_names is None:
                    all_existing = (
                        await session.execute(select(Schema).where(Schema.organisation_id == org_id))
                    ).scalars().all()
                    existing_schema_names = {s.name for s in all_existing}
                sname = suggest_import_name(existing_schema_names, sname, suffix="(imported)")
                existing_schema_names.add(sname)
                warnings.append(
                    f"Schema '{existing_schema.name}' exists with different structure. Created as '{sname}' instead."
                )
            elif existing_sv and existing_sv.definition_json == definition:
                schema_id_map[export_schema_id] = str(existing_schema.id)
                schema_version_map[export_schema_id] = existing_sv.version
                continue

        try:
            new_schema = await create_schema(
                session,
                org_id=org_id,
                name=sname,
                account_id=created_by,
                description=sd.get("description"),
                abstract_name=sd.get("abstract_name"),
            )
        except Exception as exc:
            logger.error("Failed to create schema '%s': %s", sname, exc)
            raise

        schema_id_map[export_schema_id] = str(new_schema.id)

        try:
            new_sv = await create_schema_version(
                session,
                org_id=org_id,
                schema_id=new_schema.id,
                version=sd.get("latest_version") or DEFAULT_SCHEMA_VERSION,
                version_number=1,
                definition_json=definition,
                account_id=created_by,
                published=True,
            )
        except Exception as exc:
            logger.error("Failed to create schema version for '%s': %s", sname, exc)
            raise

        schema_version_map[export_schema_id] = new_sv.version

    # --- Step 2: Create agents ---
    agent_id_map: dict[str, str] = {}
    for ad in agents_data:
        export_agent_id = ad.get("id", "")
        aname = suggest_import_name(existing_agent_names, ad.get("name", "Imported Agent"))
        existing_agent_names.add(aname)

        input_schema_id_str = ad.get("input_schema_id", "")
        output_schema_id_str = ad.get("output_schema_id", "")
        resolved_input_id = schema_id_map.get(input_schema_id_str)
        resolved_output_id = schema_id_map.get(output_schema_id_str)
        resolved_input_version = (
            schema_version_map.get(input_schema_id_str)
            or ad.get("input_schema_version", DEFAULT_SCHEMA_VERSION)
        )
        resolved_output_version = (
            schema_version_map.get(output_schema_id_str)
            or ad.get("output_schema_version", DEFAULT_SCHEMA_VERSION)
        )

        export_mb_id = ad.get("model_backend_id", "")
        resolved_mb_id = ad.get("_resolved_model_backend_id") or mb_overrides.get(export_mb_id)

        agent_args = {
            "session": session,
            "org_id": org_id,
            "name": aname,
            "account_id": created_by,
            "prompt_template": ad.get("prompt_template", ""),
            "description": ad.get("description"),
            "connector_type_refs": ad.get("connector_type_refs"),
            "evals": ad.get("evals"),
            "retry_policy": ad.get("retry_policy"),
            "token_budget": ad.get("token_budget"),
        }

        if input_schema_id_str and not resolved_input_id:
            warnings.append(
                f"Agent '{aname}' references unresolved input schema '{input_schema_id_str}'. "
                "The schema reference will be omitted."
            )
        if output_schema_id_str and not resolved_output_id:
            warnings.append(
                f"Agent '{aname}' references unresolved output schema '{output_schema_id_str}'. "
                "The schema reference will be omitted."
            )
        if export_mb_id and not resolved_mb_id:
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

        try:
            agent = await create_agent(**agent_args)
        except (ValueError, SQLAlchemyError) as exc:
            logger.error("Failed to create agent '%s': %s", aname, exc)
            raise

        agent_id_map[export_agent_id] = str(agent.id)

    # --- Step 3: Create pipeline ---
    raw_graph_nodes = pipeline_info.get("graph_nodes_json")
    if isinstance(raw_graph_nodes, list):
        graph_nodes = raw_graph_nodes
    else:
        graph_nodes = []
        warnings.append("Pipeline 'graph_nodes_json' is not a list; nodes will be empty.")

    # Rewire agent_id, output_schema_id, and connector binding references in graph nodes
    graph_nodes = copy.deepcopy(graph_nodes)
    for node in graph_nodes:
        node_export_id = node.get("agent_id")
        if node_export_id and node_export_id in agent_id_map:
            node["agent_id"] = agent_id_map[node_export_id]
        node_output_schema = node.get("output_schema_id")
        if node_output_schema and node_output_schema in schema_id_map:
            node["output_schema_id"] = schema_id_map[node_output_schema]
        connector_binding = node.get("connector_binding")
        if connector_binding:
            existing_id = connector_binding.get("instance_id", "")
            if existing_id and existing_id in conn_overrides:
                connector_binding["instance_id"] = conn_overrides[existing_id]

    try:
        pipeline = await create_pipeline(
            session,
            org_id=org_id,
            name=pname,
            account_id=created_by,
            description=pipeline_info.get("description"),
            visibility="org",
            owner_team_id=owner_team_id,
            node_timeout_seconds=pipeline_info.get("node_timeout_seconds") or DEFAULT_NODE_TIMEOUT,
            run_context_defaults=pipeline_info.get("run_context_defaults"),
        )
    except Exception as exc:
        logger.error("Failed to create pipeline '%s': %s", pname, exc)
        raise

    pipeline.graph_nodes_json = list(graph_nodes)
    await session.flush()

    # --- Step 4: Create edges ---
    pipeline_edges_added: list[PipelineEdge] = []
    for ed in edges_data:
        try:
            source_id = _safe_uuid(ed.get("source_node_id", ""), "edge.source_node_id")
            target_id = _safe_uuid(ed.get("target_node_id", ""), "edge.target_node_id")
            edge_id = _safe_uuid(ed["id"]) if ed.get("id") else uuid.uuid4()
        except ValueError as exc:
            warnings.append(f"Skipping edge with invalid UUID: {exc}")
            continue
        edge_type = ed.get("edge_type", "normal")
        if edge_type not in VALID_EDGE_TYPES:
            warnings.append(f"Unknown edge type '{edge_type}', defaulting to 'normal'.")
            edge_type = "normal"
        edge = PipelineEdge(
            id=edge_id,
            organisation_id=org_id,
            pipeline_id=pipeline.id,
            source_node_id=source_id,
            target_node_id=target_id,
            edge_type=edge_type,
            hitl_gate_config=ed.get("hitl_gate_config"),
        )
        session.add(edge)
        pipeline_edges_added.append(edge)
    await session.flush()

    # --- Step 5: Create library primitive for the workflow ---
    try:
        prim = await create_library_primitive(
            session,
            org_id=org_id,
            source="local",
            primitive_type="workflow",
            name=pname,
            slug=_sanitize_slug(pname),
            description=pipeline_info.get("description", ""),
            author=created_by.hex[:8],
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
            owner_team_id=owner_team_id,
            visibility="org",
            account_id=created_by,
        )
    except Exception as exc:
        logger.error("Failed to create library primitive for pipeline '%s': %s", pname, exc)
        raise

    logger.info(
        "Imported pipeline '%s' (id=%s) with %d agents, %d edges, %d schemas",
        pname, pipeline.id, len(agents_data), len(edges_data), len(schemas_data),
    )

    return {
        "pipeline_id": str(pipeline.id),
        "pipeline_name": pname,
        "primitive_id": str(prim.id),
        "agent_count": len(agents_data),
        "edge_count": len(pipeline_edges_added),
        "schema_count": len(schemas_data),
        "agents": agent_id_map,
        "schemas": schema_id_map,
        "warnings": warnings,
    }
