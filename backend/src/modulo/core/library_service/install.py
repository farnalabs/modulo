"""Collection install service — adapter over materialize_import (FAR-762).

Resolves version-pinned collection manifests into runnable org entities by
driving the existing ``materialize_import`` engine.  Each pin resolves to a
``LibraryPrimitive`` row visible to the org, then the multi-entity definition
dict is built and handed to ``materialize_import`` which handles dependency
ordering, entity creation, and all-or-nothing rollback.

After materialization succeeds: stamps ``collection_install_id`` on every
created entity (schemas, agents, pipelines), populates
``collection_install_entity`` child rows, and creates the ``CollectionInstall``
record with ``status=installed``.

Runnability is computed on read (pure function, not stored) — see
``runnability.py``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.workflow_import_export import BUNDLE_FORMAT_VERSION, materialize_import
from modulo.db.models.agent import Agent
from modulo.db.models.collection_install import CollectionInstall, CollectionInstallEntity
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.models.schema import Schema

__all__ = ["install_collection"]

logger = logging.getLogger(__name__)


class CollectionInstallError(Exception):
    """Base error for collection install failures."""


class CollectionNotPublishedError(CollectionInstallError):
    """Raised when the collection primitive is not in ``published`` status."""


class PinResolutionError(CollectionInstallError):
    """Raised when a manifest pin cannot be resolved to a visible primitive."""


async def _resolve_pin(
    session: AsyncSession,
    org_id: uuid.UUID,
    slug: str,
    version: str,
) -> LibraryPrimitive:
    """Resolve a single manifest pin to its backing primitive.

    Raises ``PinResolutionError`` when the pin references an unknown,
    invisible, or version-mismatched primitive.
    """
    stmt = select(LibraryPrimitive).where(
        LibraryPrimitive.organisation_id == org_id,
        LibraryPrimitive.slug == slug,
        LibraryPrimitive.version == version,
        LibraryPrimitive.deleted_at.is_(None),
    )
    result = await session.execute(stmt)
    prim = result.scalar_one_or_none()
    if prim is None:
        raise PinResolutionError(f"Pin '{slug}@{version}' does not resolve to a visible primitive in this organisation")
    return prim


async def _build_bundle_from_pins(
    resolved_pins: list[LibraryPrimitive],
) -> dict[str, Any]:
    """Build a ``materialize_import``-compatible bundle from resolved primitives.

    Each pin contributes its content to the appropriate bundle section:
    - ``schema`` pins → ``schemas`` list
    - ``agent`` pins → ``agents`` list
    - ``workflow`` pins → ``pipeline`` (single workflow bundle)
    """
    schemas: list[dict[str, Any]] = []
    agents: list[dict[str, Any]] = []
    graph_nodes: list[dict[str, Any]] = []

    for pin in resolved_pins:
        content = pin.content_json or {}
        if pin.primitive_type == "schema":
            schemas.append(
                {
                    "id": str(pin.id),
                    "name": pin.name,
                    "description": pin.description,
                    "abstract_name": content.get("abstract_name"),
                    "latest_version": content.get("latest_version", "1.0"),
                    "definition_json": content.get("definition_json"),
                }
            )
        elif pin.primitive_type == "agent":
            agents.append(
                {
                    "id": str(pin.id),
                    "name": pin.name,
                    "description": pin.description,
                    "input_schema_id": content.get("input_schema_id", ""),
                    "output_schema_id": content.get("output_schema_id", ""),
                    "prompt_template": content.get("prompt_template", ""),
                    "connector_type_refs": content.get("connector_type_refs", []),
                    "evals": content.get("evals"),
                    "retry_policy": content.get("retry_policy"),
                    "token_budget": content.get("token_budget"),
                }
            )
            node_id = str(uuid.uuid4())
            graph_nodes.append(
                {
                    "id": node_id,
                    "node_type": "agent",
                    "agent_id": str(pin.id),
                    "position": {"x": 0, "y": 0},
                    "label": pin.name,
                }
            )
        elif pin.primitive_type == "workflow":
            workflow_content = content.get("bundle") or content
            pipeline_info = workflow_content.get("pipeline", {})
            pipeline_graph_nodes = pipeline_info.get("graph_nodes_json", [])
            graph_nodes.extend(pipeline_graph_nodes)
            agents.extend(workflow_content.get("agents", []))
            schemas.extend(workflow_content.get("schemas", []))

    return {
        "format_version": BUNDLE_FORMAT_VERSION,
        "pipeline": {
            "name": "Collection Install",
            "description": "Entities installed from a library collection",
            "graph_nodes_json": graph_nodes,
            "run_context_defaults": {},
            "node_timeout_seconds": 300,
            "retry_policy": {},
        },
        "agents": agents,
        "schemas": schemas,
        "edges": [],
    }


async def _stamp_install_id(
    session: AsyncSession,
    org_id: uuid.UUID,
    install_id: uuid.UUID,
    result: dict[str, Any],
) -> list[dict[str, str]]:
    """Stamp ``collection_install_id`` on every entity created by
    ``materialize_import`` and return the entity tracking list.
    """
    entities: list[dict[str, str]] = []

    # Stamp schemas
    schema_id_map = result.get("schemas", {})
    for local_id_str in schema_id_map.values():
        local_id = uuid.UUID(local_id_str)
        schema = await session.get(Schema, local_id)
        if schema is not None and schema.organisation_id == org_id:
            schema.collection_install_id = install_id
            entities.append({"entity_type": "schema", "entity_id": local_id_str})

    # Stamp agents
    agent_id_map = result.get("agents", {})
    for local_id_str in agent_id_map.values():
        local_id = uuid.UUID(local_id_str)
        agent = await session.get(Agent, local_id)
        if agent is not None and agent.organisation_id == org_id:
            agent.collection_install_id = install_id
            entities.append({"entity_type": "agent", "entity_id": local_id_str})

    # Stamp pipeline (created by materialize_import)
    pipeline_id_str = result.get("pipeline_id")
    if pipeline_id_str:
        from modulo.db.models.pipeline import Pipeline

        pipeline = await session.get(Pipeline, uuid.UUID(pipeline_id_str))
        if pipeline is not None and pipeline.organisation_id == org_id:
            pipeline.collection_install_id = install_id
            entities.append({"entity_type": "pipeline", "entity_id": pipeline_id_str})

    return entities


async def _record_entities(
    session: AsyncSession,
    install_id: uuid.UUID,
    entities: list[dict[str, str]],
) -> None:
    """Create ``CollectionInstallEntity`` child rows for each stamped entity."""
    for ent in entities:
        session.add(
            CollectionInstallEntity(
                install_id=install_id,
                entity_type=ent["entity_type"],
                entity_id=uuid.UUID(ent["entity_id"]),
            )
        )


def _build_connector_checklist(
    resolved_pins: list[LibraryPrimitive],
) -> list[dict[str, Any]]:
    """Build the connector requirement checklist from resolved agents' refs.

    Each unique connector_type_id referenced by any agent in the collection
    gets an entry with ``status='pending'``.  The runnability check on read
    verifies all entries are ``'configured+bound'``.
    """
    seen: dict[str, dict[str, Any]] = {}
    for pin in resolved_pins:
        if pin.primitive_type != "agent":
            continue
        content = pin.content_json or {}
        for ref in content.get("connector_type_refs", []):
            ctid = ref.get("connector_type_id") or ref.get("type", "")
            if ctid and ctid not in seen:
                seen[ctid] = {
                    "connector_type_id": ctid,
                    "status": "pending",
                }
    return list(seen.values())


async def install_collection(
    session: AsyncSession,
    org_id: uuid.UUID,
    created_by: uuid.UUID,
    collection_id: uuid.UUID,
) -> CollectionInstall:
    """Install a published collection into the organisation.

    Resolves manifest pins to visible primitives, materialises entities via
    ``materialize_import``, stamps ``collection_install_id`` on all created
    entities, and creates the ``CollectionInstall`` provenance record.

    Raises ``CollectionNotPublishedError`` if the collection is not published,
    ``PinResolutionError`` if a pin cannot be resolved, and propagates any
    ``materialize_import`` errors (which rollback all-or-nothing).
    """
    # 1. Load the collection primitive
    collection = await session.get(LibraryPrimitive, collection_id)
    if collection is None or collection.organisation_id != org_id:
        raise CollectionInstallError(f"Collection {collection_id} not found")
    if collection.primitive_type != "library_collection":
        raise CollectionInstallError(f"Primitive {collection_id} is not a collection")
    if collection.status != "published":
        raise CollectionNotPublishedError(
            f"Collection '{collection.name}' must be published before installing (current status: {collection.status})"
        )

    # 2. Resolve manifest pins
    manifest_pins = collection.manifest_pins or []
    resolved_pins: list[LibraryPrimitive] = []
    for pin in manifest_pins:
        slug = pin.get("slug", "")
        version = pin.get("version", "")
        if not slug or not version:
            raise PinResolutionError(f"Pin missing slug or version: {pin}")
        prim = await _resolve_pin(session, org_id, slug, version)
        resolved_pins.append(prim)

    if not resolved_pins:
        raise CollectionInstallError("Collection has no manifest pins to install")

    # 3. Build the materialize_import bundle
    bundle = await _build_bundle_from_pins(resolved_pins)

    # 4. Refuse if the collection is already installed in this organisation
    existing_stmt = select(CollectionInstall).where(
        CollectionInstall.organisation_id == org_id,
        CollectionInstall.collection_id == collection_id,
    )
    existing = (await session.execute(existing_stmt)).scalar_one_or_none()
    if existing is not None:
        raise CollectionInstallError(f"Collection '{collection.name}' is already installed in this organisation")
    install_id = uuid.uuid4()

    # 5. Call materialize_import (all-or-nothing transaction)
    warnings: list[str] = []
    try:
        result = await materialize_import(
            session,
            org_id=org_id,
            created_by=created_by,
            bundle=bundle,
            pipeline_name_override=f"Collection: {collection.name}",
            warnings=warnings,
        )
    except Exception:
        logger.exception(
            "install_collection: materialize_import failed for collection %s",
            collection_id,
        )
        raise

    # 6. Stamp collection_install_id on created entities
    entities = await _stamp_install_id(session, org_id, install_id, result)

    # 7. Record entities in collection_install_entity
    await _record_entities(session, install_id, entities)

    # 8. Build connector checklist
    connector_checklist = _build_connector_checklist(resolved_pins)

    # 9. Determine community provenance (ADR 032 D2).
    # Community-sourced or registry-sourced collections restrict agent tool/
    # connector access until an operator explicitly grants access.
    community_sourced = collection.source in ("community", "registry")

    # 10. Create the CollectionInstall provenance record
    session.add(
        CollectionInstall(
            install_id=install_id,
            collection_id=collection_id,
            collection_version=collection.version,
            organisation_id=org_id,
            status="installed",
            community_sourced=community_sourced,
            resolved_manifest={
                "schemas": result.get("schemas", {}),
                "agents": result.get("agents", {}),
                "pipeline_id": result.get("pipeline_id"),
                "warnings": warnings,
            },
            connector_checklist=connector_checklist,
            installed_entities=entities,
        )
    )

    await session.flush()

    # Re-fetch to return the complete record
    install = await session.get(CollectionInstall, install_id)
    assert install is not None
    return install
