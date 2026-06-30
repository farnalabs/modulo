"""Workflow bundle export and import service.

Produces portable .zip bundles that carry pipeline + agent + schema definitions
but strip org-private details (owner_team_id, connector credentials, api keys).
Import resolves local equivalents via a binding wizard.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from typing import Any

from sqlalchemy import select
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

BUNDLE_FORMAT_VERSION = "1"
MANIFEST_FILENAME = "bundle.json"

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
    stmt = select(Pipeline).where(Pipeline.id == pipeline_id)
    pipeline = (await session.execute(stmt)).scalar_one_or_none()
    if pipeline is None:
        raise LookupError(f"Pipeline {pipeline_id} not found")

    agent_ids: set[uuid.UUID] = set()
    schema_ids: set[uuid.UUID] = set()
    model_backend_ids: set[uuid.UUID] = set()

    if pipeline.graph_nodes_json:
        for node in pipeline.graph_nodes_json:
            agent_id = node.get("agent_id")
            if agent_id:
                agent_ids.add(uuid.UUID(agent_id) if isinstance(agent_id, str) else agent_id)

    agents_list: list[dict[str, Any]] = []
    if agent_ids:
        agent_result = await session.execute(select(Agent).where(Agent.id.in_(agent_ids)))
        agents = list(agent_result.scalars())
        agents_list = [
            {
                "id": str(a.id),
                "name": a.name,
                "description": a.description,
                "input_schema_id": str(a.input_schema_id),
                "input_schema_version": a.input_schema_version,
                "output_schema_id": str(a.output_schema_id),
                "output_schema_version": a.output_schema_version,
                "prompt_template": a.prompt_template,
                "model_backend_id": str(a.model_backend_id),
                "connector_type_refs": list(a.connector_type_refs or []),
                "evals": list(a.evals or []) if a.evals else None,
                "retry_policy": dict(a.retry_policy or {}),
                "token_budget": a.token_budget,
            }
            for a in agents
        ]
        for a in agents:
            schema_ids.add(a.input_schema_id)
            schema_ids.add(a.output_schema_id)
            model_backend_ids.add(a.model_backend_id)

    # Collect manual node output schemas
    if pipeline.graph_nodes_json:
        for node in pipeline.graph_nodes_json:
            schema_id = node.get("output_schema_id")
            if schema_id:
                schema_ids.add(uuid.UUID(schema_id) if isinstance(schema_id, str) else schema_id)

    schemas_list: list[dict[str, Any]] = []
    if schema_ids:
        schema_result = await session.execute(select(Schema).where(Schema.id.in_(schema_ids)))
        schemas = list(schema_result.scalars())
        for s in schemas:
            # Get latest published version
            sv_result = await session.execute(
                select(SchemaVersion)
                .where(
                    SchemaVersion.schema_id == s.id,
                    SchemaVersion.published.is_(True),
                )
                .order_by(SchemaVersion.version_number.desc())
                .limit(1)
            )
            latest_version = sv_result.scalar_one_or_none()
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
    edge_result = await session.execute(select(PipelineEdge).where(PipelineEdge.pipeline_id == pipeline_id))
    edges = list(edge_result.scalars())
    edges_list = [
        {
            "id": str(e.id),
            "source_node_id": str(e.source_node_id),
            "target_node_id": str(e.target_node_id),
            "edge_type": e.edge_type,
        }
        for e in edges
    ]

    bundle: dict[str, Any] = {
        "format_version": BUNDLE_FORMAT_VERSION,
        "pipeline": {
            "name": pipeline.name,
            "description": pipeline.description,
            "graph_nodes_json": pipeline.graph_nodes_json if pipeline.graph_nodes_json else [],
            "run_context_defaults": dict(pipeline.run_context_defaults or {}),
            "node_timeout_seconds": pipeline.node_timeout_seconds,
        },
        "agents": agents_list,
        "schemas": schemas_list,
        "model_backends": model_backends_list,
        "edges": edges_list,
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST_FILENAME, json.dumps(bundle, indent=2, default=str))
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
    name = export_schema["name"]

    # First try abstract_name match
    if abstract_name:
        stmt = select(Schema).where(
            Schema.organisation_id == org_id,
            Schema.abstract_name == abstract_name,
        )
        result = await session.execute(stmt)
        schema = result.scalar_one_or_none()
        if schema is not None:
            sv_result = await session.execute(
                select(SchemaVersion)
                .where(
                    SchemaVersion.schema_id == schema.id,
                    SchemaVersion.published.is_(True),
                )
                .order_by(SchemaVersion.version_number.desc())
                .limit(1)
            )
            sv = sv_result.scalar_one_or_none()
            return {
                "schema_id": str(schema.id),
                "version": sv.version if sv else "1.0",
                "warning": None,
            }

    # Try matching by same definition structure
    if definition:
        all_schemas = (await session.execute(select(Schema).where(Schema.organisation_id == org_id))).scalars()
        for s in all_schemas:
            sv_result = await session.execute(
                select(SchemaVersion)
                .where(
                    SchemaVersion.schema_id == s.id,
                    SchemaVersion.published.is_(True),
                )
                .order_by(SchemaVersion.version_number.desc())
                .limit(1)
            )
            sv = sv_result.scalar_one_or_none()
            if sv and sv.definition_json == definition:
                return {
                    "schema_id": str(s.id),
                    "version": sv.version,
                    "warning": None,
                }

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
    stmt = select(ConnectorInstance).where(
        ConnectorInstance.organisation_id == org_id,
        ConnectorInstance.connector_type_id == connector_type_id,
        ConnectorInstance.status == "active",
    )
    result = await session.execute(stmt)
    instances = list(result.scalars())
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
    name = export_backend["name"]
    provider = export_backend["provider"]
    model_id = export_backend["model_id"]

    # Try by name first
    stmt = select(ModelBackend).where(
        ModelBackend.organisation_id == org_id,
        ModelBackend.name == name,
        ModelBackend.status == "active",
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
    result = await session.execute(select(Pipeline.name).where(Pipeline.organisation_id == org_id))
    return {row[0] for row in result}


async def get_existing_agent_names(
    session: AsyncSession,
    org_id: uuid.UUID,
) -> set[str]:
    result = await session.execute(select(Agent.name).where(Agent.organisation_id == org_id))
    return {row[0] for row in result}


# ---------------------------------------------------------------------------
# ZIP extraction and analysis (server-side)
# ---------------------------------------------------------------------------


def extract_bundle_json_from_zip(zip_bytes: bytes) -> dict[str, Any]:
    """Extract bundle.json from a .modulo.zip archive."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        if MANIFEST_FILENAME not in names:
            raise LookupError(f"{MANIFEST_FILENAME} not found in archive (found: {names})")
        result: dict[str, Any] = json.loads(zf.read(MANIFEST_FILENAME))
        return result


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
    mb_overrides: dict[str, str] = model_backend_overrides or {}
    warnings = warnings or []
    schema_overrides: dict[str, str] = schema_id_overrides or {}
    sv_overrides: dict[str, str] = schema_version_overrides or {}
    conn_overrides: dict[str, str] = connector_instance_overrides or {}
    pipeline_info = bundle.get("pipeline", {})
    agents_data = bundle.get("agents", [])
    schemas_data = bundle.get("schemas", [])
    edges_data = bundle.get("edges", [])
    name = pipeline_name_override or pipeline_info.get("name", "Imported Pipeline")

    existing_agent_names = await get_existing_agent_names(session, org_id)
    existing_pipeline_names = await get_existing_pipeline_names(session, org_id)

    pname = suggest_import_name(existing_pipeline_names, name) if name in existing_pipeline_names else name

    # --- Step 1: Create any schemas that don't exist locally ---
    schema_id_map: dict[str, str] = {}
    schema_version_map: dict[str, str] = {}
    for sd in schemas_data:
        export_schema_id = sd.get("id", "")
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
            sv_result = await session.execute(
                select(SchemaVersion)
                .where(
                    SchemaVersion.schema_id == existing_schema.id,
                    SchemaVersion.published.is_(True),
                )
                .order_by(SchemaVersion.version_number.desc())
                .limit(1)
            )
            existing_sv = sv_result.scalar_one_or_none()
            if existing_sv and existing_sv.definition_json != definition:
                existing_schemas = list(
                    (await session.execute(select(Schema).where(Schema.organisation_id == org_id))).scalars()
                )
                existing_names = {s.name for s in existing_schemas}
                sname = suggest_import_name(
                    existing_names,
                    sname,
                    suffix="(imported)",
                )
                warnings.append(
                    f"Schema '{existing_schema.name}' exists with different structure. Created as '{sname}' instead."
                )

        new_schema = await create_schema(
            session,
            org_id=org_id,
            name=sname,
            account_id=created_by,
            description=sd.get("description"),
            abstract_name=sd.get("abstract_name"),
        )
        schema_id_map[export_schema_id] = str(new_schema.id)

        new_sv = await create_schema_version(
            session,
            org_id=org_id,
            schema_id=new_schema.id,
            version=sd.get("latest_version", "1.0"),
            version_number=1,
            definition_json=definition,
            account_id=created_by,
            published=True,
        )
        schema_version_map[export_schema_id] = new_sv.version

    # --- Step 2: Create agents ---
    agent_id_map: dict[str, str] = {}
    for ad in agents_data:
        export_agent_id = ad.get("id", "")
        aname = suggest_import_name(existing_agent_names, ad.get("name", "Imported Agent"))
        existing_agent_names.add(aname)

        input_schema_id_str = ad.get("input_schema_id", "")
        output_schema_id_str = ad.get("output_schema_id", "")
        resolved_input_id = schema_id_map.get(input_schema_id_str, input_schema_id_str)
        resolved_output_id = schema_id_map.get(output_schema_id_str, output_schema_id_str)
        resolved_input_version = schema_version_map.get(input_schema_id_str, ad.get("input_schema_version", "1.0"))
        resolved_output_version = schema_version_map.get(output_schema_id_str, ad.get("output_schema_version", "1.0"))

        export_mb_id = ad.get("model_backend_id", "")
        resolved_mb_id = ad.get("_resolved_model_backend_id")
        resolved_mb_id_str = resolved_mb_id if resolved_mb_id else mb_overrides.get(export_mb_id, export_mb_id)

        agent = await create_agent(
            session,
            org_id=org_id,
            name=aname,
            account_id=created_by,
            input_schema_id=uuid.UUID(resolved_input_id),
            input_schema_version=resolved_input_version,
            output_schema_id=uuid.UUID(resolved_output_id),
            output_schema_version=resolved_output_version,
            prompt_template=ad.get("prompt_template", ""),
            model_backend_id=uuid.UUID(resolved_mb_id_str),
            description=ad.get("description"),
            connector_type_refs=ad.get("connector_type_refs"),
            evals=ad.get("evals"),
            retry_policy=ad.get("retry_policy"),
            token_budget=ad.get("token_budget"),
        )
        agent_id_map[export_agent_id] = str(agent.id)

    # --- Step 3: Create pipeline ---
    graph_nodes = list(pipeline_info.get("graph_nodes_json", []))
    # Rewire agent_id references in graph nodes
    for node in graph_nodes:
        node_export_id = node.get("agent_id")
        if node_export_id and node_export_id in agent_id_map:
            node["agent_id"] = agent_id_map[node_export_id]
        # Rewire connector bindings (user override wins over analysis)
        connector_binding = node.get("connector_binding")
        if connector_binding:
            existing_id = connector_binding.get("instance_id", "")
            if existing_id and existing_id in conn_overrides:
                connector_binding["instance_id"] = conn_overrides[existing_id]

    pipeline = await create_pipeline(
        session,
        org_id=org_id,
        name=pname,
        account_id=created_by,
        description=pipeline_info.get("description"),
        visibility="org",
        owner_team_id=owner_team_id,
        node_timeout_seconds=pipeline_info.get("node_timeout_seconds", 300),
        run_context_defaults=pipeline_info.get("run_context_defaults"),
    )

    # Set graph nodes
    pipeline.graph_nodes_json = graph_nodes
    await session.flush()

    # --- Step 4: Create edges ---
    pipeline_edges_added: list[PipelineEdge] = []
    for ed in edges_data:
        edge = PipelineEdge(
            id=uuid.UUID(ed["id"]) if ed.get("id") else uuid.uuid4(),
            organisation_id=org_id,
            pipeline_id=pipeline.id,
            source_node_id=uuid.UUID(ed["source_node_id"]),
            target_node_id=uuid.UUID(ed["target_node_id"]),
            edge_type=ed.get("edge_type", "normal"),
            hitl_gate_config=ed.get("hitl_gate_config"),
        )
        session.add(edge)
        pipeline_edges_added.append(edge)
    await session.flush()

    # --- Step 5: Create library primitive for the workflow ---
    prim = await create_library_primitive(
        session,
        org_id=org_id,
        source="local",
        primitive_type="workflow",
        name=pname,
        slug=pname.lower().replace(" ", "-").replace("_", "-"),
        description=pipeline_info.get("description", ""),
        author=created_by.hex[:8],
        version="1.0",
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
