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

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.library_service import get_primitive, get_primitive_by_slug
from modulo.core.library_service._seed_data import MODULO_ORG_ID
from modulo.core.workflow_import_export import BUNDLE_FORMAT_VERSION, materialize_import
from modulo.db.models.agent import Agent
from modulo.db.models.collection_install import CollectionInstall, CollectionInstallEntity
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.models.organisation import Organisation
from modulo.db.models.schema import Schema
from modulo.db.rls import set_rls_org

__all__ = ["install_collection"]

logger = logging.getLogger(__name__)

# Deterministic order in which a slug-only manifest pin is tried against the
# library_service resolver's primitive-type namespaces (ADR 032 §4). Manifest
# pins reference primitives by slug alone, but slugs are unique only WITHIN a
# primitive type — the same slug may exist as a schema AND an agent. A pin is
# resolved by trying each namespace in this fixed order and taking the FIRST
# type whose namespace contains the slug; a version mismatch on a hit fails
# loudly rather than falling through to a later type.
_PIN_TYPE_ORDER: tuple[str, ...] = ("schema", "agent", "workflow", "pipeline_template")

# Sources whose primitives are visible to EVERY organisation via the
# library_service resolver (ADR 032 §4): modulo built-ins come from the
# in-code registry and community contributions from the published
# community cache — neither lives inside the installing org's RLS slice.
_VISIBLE_EVERYWHERE_SOURCES = frozenset({"modulo", "community"})

# Library-primitive ``content_json["fields"]`` → JSON Schema type conversion.
_FIELD_TYPE_MAP: dict[str, str] = {
    "string": "string",
    "text": "string",
    "integer": "integer",
    "number": "number",
    "float": "number",
    "boolean": "boolean",
    "array": "array",
    "object": "object",
}


# Sub-field spec map → JSON Schema object properties (recursive via
# ``_definition_from_field_spec``), excluding a sibling ``required`` key.
def _subfield_properties(spec: dict[Any, Any]) -> tuple[dict[str, Any], list[str]]:
    """Convert a named-sub-field map to ``(properties, required)``.

    ``required`` collects sub-specs that declare ``"required": true`` so the
    nested object schema carries the same required list as the seed.
    """
    properties: dict[str, Any] = {
        str(name): _definition_from_field_spec(sub) for name, sub in spec.items() if name != "required"
    }
    required = [
        str(name)
        for name, sub in spec.items()
        if name != "required" and isinstance(sub, dict) and sub.get("required", False)
    ]
    return properties, required


def _items_definition(items: Any) -> dict[str, Any] | None:
    """Convert a field spec's ``items`` value into a JSON Schema items schema.

    - string shorthand (``"items": "string"``) → scalar item schema
    - dict (a named sub-field spec map) → object schema carrying the nested
      properties and any nested ``required`` entries
    - anything else (including absent) → ``None`` (unconstrained items)
    """
    if isinstance(items, str):
        return {"type": _FIELD_TYPE_MAP.get(items, "string")}
    if isinstance(items, dict):
        properties, required = _subfield_properties(items)
        obj: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            obj["required"] = required
        return obj
    return None


def _definition_from_field_spec(spec: Any) -> dict[str, Any]:
    """Convert a single library-primitive field spec to a JSON Schema property."""
    if isinstance(spec, str):
        return {"type": _FIELD_TYPE_MAP.get(spec, "string")}
    if not isinstance(spec, dict):
        return {"type": "string"}
    if "type" in spec:
        mapped = _FIELD_TYPE_MAP.get(str(spec["type"]), "string")
        prop: dict[str, Any] = {"type": mapped}
        if mapped == "array":
            items = _items_definition(spec.get("items"))
            if items is not None:
                prop["items"] = items
        if "enum" in spec:
            prop["enum"] = spec["enum"]
        return prop
    # Named sub-field map (e.g. findings: {severity: {...}}) → object with properties.
    properties, required = _subfield_properties(spec)
    obj: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        obj["required"] = required
    return obj


def _definition_from_fields(fields: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert a library-primitive ``fields`` list into a JSON Schema definition.

    Builtin modulo/community schema primitives carry a simplified
    ``content_json={"fields": [...]}`` shape; ``materialize_import`` requires a
    ``definition_json`` JSON Schema (it refuses to materialise a schema
    without one), so collection install translates eagerly.
    """
    required: list[str] = []
    properties: dict[str, Any] = {}
    for field in fields:
        name = str(field.get("name", ""))
        if not name:
            continue
        properties[name] = _definition_from_field_spec(field)
        if field.get("required", False):
            required.append(name)
    definition: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        definition["required"] = required
    return definition


def _schema_definition_from_content(content: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the installable JSON Schema definition from a schema pin."""
    definition = content.get("definition_json")
    if isinstance(definition, dict) and definition:
        return definition
    fields = content.get("fields")
    if isinstance(fields, list) and fields:
        return _definition_from_fields(fields)
    return None


class CollectionInstallError(Exception):
    """Base error for collection install failures."""


class CollectionNotPublishedError(CollectionInstallError):
    """Raised when the collection primitive is not in ``published`` status."""


class PinResolutionError(CollectionInstallError):
    """Raised when a manifest pin cannot be resolved to a visible primitive."""


async def _resolve_collection(
    session: AsyncSession,
    org_id: uuid.UUID,
    collection_id: uuid.UUID,
) -> LibraryPrimitive | None:
    """Return the collection primitive visible to ``org_id``, or None.

    The org-scoped DB row wins (unchanged behaviour for genuinely local
    collections). Otherwise fall back to the library_service resolver, which
    sees the in-code modulo registry and the published community cache —
    primitives every org may install (ADR 032 §4). A resolver fallback is
    only accepted when the row itself is org-owned or comes from a
    visible-everywhere source; cross-org local rows stay invisible.
    """
    collection = await session.get(LibraryPrimitive, collection_id)
    if collection is not None and collection.organisation_id == org_id:
        return collection
    resolved = await get_primitive(session, org_id, collection_id, explicit_org_filter=org_id)
    if resolved is None:
        return None
    if resolved.organisation_id == org_id or resolved.source in _VISIBLE_EVERYWHERE_SOURCES:
        return resolved
    return None


async def _resolve_pin(
    session: AsyncSession,
    org_id: uuid.UUID,
    slug: str,
    version: str,
) -> LibraryPrimitive:
    """Resolve a single manifest pin to its backing primitive.

    Routes through the library_service resolver (ADR 032 §4): the org-scoped
    DB first, then the visible-everywhere modulo/community registries. Raises
    ``PinResolutionError`` when the pin references an unknown, invisible, or
    version-mismatched primitive.
    """
    for primitive_type in _PIN_TYPE_ORDER:
        prim = await get_primitive_by_slug(session, org_id, primitive_type, slug, explicit_org_filter=org_id)
        if prim is None:
            continue
        if prim.deleted_at is not None:
            continue
        if prim.version != version:
            raise PinResolutionError(
                f"Pin '{slug}@{version}' does not match the visible primitive version '{prim.version}'"
            )
        return prim
    raise PinResolutionError(f"Pin '{slug}@{version}' does not resolve to a visible primitive in this organisation")


def _transient_primitives(prims: list[LibraryPrimitive]) -> list[LibraryPrimitive]:
    """Return the registry objects with NO backing DB row yet (transient).

    Builtin modulo/community primitives are module-level ``LibraryPrimitive``
    instances outside any session; ``sa_inspect(...).transient`` identifies
    exactly those (DB-loaded rows are persistent/detached with a row on disk).
    """
    return [p for p in prims if sa_inspect(p).transient]


def _clone_builtin(prim: LibraryPrimitive) -> LibraryPrimitive:
    """Build a session-owned copy of a registry primitive.

    The registry instances are module-global singletons; adding them to an
    application session would leave every later consumer holding an expired
    detached instance. Cloning (shallow copy of the JSON payload — install
    treats pin content as read-only) keeps registry state pristine.
    """
    return LibraryPrimitive(
        id=prim.id,
        organisation_id=prim.organisation_id,
        source=prim.source,
        primitive_type=prim.primitive_type,
        name=prim.name,
        slug=prim.slug,
        description=prim.description,
        author=prim.author,
        version=prim.version,
        tags=prim.tags,
        content_json=prim.content_json,
        source_url=prim.source_url,
        forked_from=prim.forked_from,
        checksum=prim.checksum,
        ed25519_signature=prim.ed25519_signature,
        verified=prim.verified,
        download_count=prim.download_count,
        average_rating=prim.average_rating,
        review_count=prim.review_count,
        owner_team_id=prim.owner_team_id,
        visibility=prim.visibility,
        tier=prim.tier,
        account_id=prim.account_id,
        auto_update=prim.auto_update,
        contribution_status=prim.contribution_status,
        status=prim.status,
        manifest_pins=prim.manifest_pins,
    )


def _collection_content_differs(row: LibraryPrimitive, prim: LibraryPrimitive) -> bool:
    """Compare the content-bearing columns of a persisted row against the registry."""
    return (
        row.content_json != prim.content_json
        or row.manifest_pins != prim.manifest_pins
        or row.checksum != prim.checksum
    )


def _resync_collection_row(row: LibraryPrimitive, prim: LibraryPrimitive) -> None:
    """Copy the registry's content onto a persisted collection row (registry wins)."""
    row.name = prim.name
    row.description = prim.description
    row.content_json = prim.content_json
    row.manifest_pins = prim.manifest_pins
    row.checksum = prim.checksum


async def _persist_collection_row(
    session: AsyncSession,
    org_id: uuid.UUID,
    collection: LibraryPrimitive,
) -> uuid.UUID:
    """Ensure the collection primitive has a ``library_primitives`` row for the install FK.

    ``collection_install.collection_id`` carries an FK to ``library_primitives``,
    so installing a collection that only exists in the in-code registry must
    persist its row first. ONLY the collection row is persisted — pin content is
    read in-memory by ``_build_bundle_from_pins`` and is never written to disk,
    so the DB can never drift from the registry for pins (ADR 032 §4: the
    in-code registry is the single source of truth for modulo/community
    primitives).

    The upsert is idempotent on the unique tuple ``(organisation_id, source,
    slug, version)`` (``uq_library_primitive_version``, WHERE deleted_at IS
    NULL) — NOT on ``id``: a row created by a previous install, the marketplace
    sync, or migration seeding may carry a different id for the same tuple, and
    an id-keyed INSERT would collide with that constraint. When the existing
    row's content differs from the registry (checksum/content compare) it is
    re-synced — the registry always wins. A soft-deleted row holding the
    registry id satisfies the FK on its own, so no insert is attempted (an
    id-keyed INSERT there would violate the PK).

    Everything happens under the sentinel org's RLS context (the installing
    org's RLS session may neither read nor write sentinel rows), then the RLS
    context is restored to the installing org. The sentinel ``Organisation``
    row itself is created by migration 0228 — this function only reads it and
    raises a clear, actionable error when it is absent.

    Returns the id of the backing DB row; the caller MUST use it as
    ``collection_install.collection_id`` so the FK targets the row that
    actually exists on disk.
    """
    if not _transient_primitives([collection]):
        # Org-owned DB row — already persisted, nothing to do.
        return collection.id

    await set_rls_org(session, MODULO_ORG_ID)
    try:
        org_row = await session.get(Organisation, MODULO_ORG_ID)
        if org_row is None:
            raise CollectionInstallError(
                "The Modulo sentinel organisation is missing from the database. "
                "It is created by migration 0230_seed_modulo_sentinel_organisation — "
                "run `alembic upgrade head` and retry the install."
            )
        existing = (
            await session.execute(
                select(LibraryPrimitive).where(
                    LibraryPrimitive.organisation_id == MODULO_ORG_ID,
                    LibraryPrimitive.source == collection.source,
                    LibraryPrimitive.slug == collection.slug,
                    LibraryPrimitive.version == collection.version,
                    LibraryPrimitive.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if _collection_content_differs(existing, collection):
                _resync_collection_row(existing, collection)
                await session.flush()
            return existing.id
        # Tuple miss: a soft-deleted row may still hold the registry id (the
        # partial-unique index ignores it). It satisfies the FK, so reuse it
        # instead of colliding with the PK.
        by_id = await session.get(LibraryPrimitive, collection.id)
        if by_id is not None:
            if by_id.deleted_at is None and _collection_content_differs(by_id, collection):
                _resync_collection_row(by_id, collection)
                await session.flush()
            return by_id.id
        session.add(_clone_builtin(collection))
        await session.flush()
        return collection.id
    finally:
        await set_rls_org(session, org_id)


def _build_bundle_from_pins(
    resolved_pins: list[LibraryPrimitive],
) -> dict[str, Any]:
    """Build a ``materialize_import``-compatible bundle from resolved primitives.

    Each pin contributes its content to the appropriate bundle section:
    - ``schema`` pins → ``schemas`` list
    - ``agent`` pins → ``agents`` list (+ one agent graph node)
    - ``workflow`` pins → agents/schemas/nodes from the embedded bundle
    - ``pipeline_template`` pins → embedded template agents/nodes/edges
      (template agents are materialised as real agents and the template's
      ``agent_index``-referenced graph is emitted as real pipeline nodes)

    Unrecognised pin types are NOT silently dropped — they raise loudly so a
    shipped collection can never install a silently-empty pipeline (ADR 032
    §4 "fail loudly").
    """
    schemas: list[dict[str, Any]] = []
    agents: list[dict[str, Any]] = []
    graph_nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    schema_ids_by_slug = {p.slug: str(p.id) for p in resolved_pins if p.primitive_type == "schema"}

    for pin in resolved_pins:
        ptype = pin.primitive_type
        content = pin.content_json or {}
        if ptype == "schema":
            schemas.append(
                {
                    "id": str(pin.id),
                    "name": pin.name,
                    "description": pin.description,
                    "abstract_name": content.get("abstract_name"),
                    "latest_version": content.get("latest_version", "1.0"),
                    "definition_json": _schema_definition_from_content(content),
                }
            )
        elif ptype == "workflow":
            workflow_content = content.get("bundle") or content
            pipeline_info = workflow_content.get("pipeline", {})
            graph_nodes.extend(pipeline_info.get("graph_nodes_json", []))
            edges.extend(workflow_content.get("edges", []))
            agents.extend(workflow_content.get("agents", []))
            schemas.extend(workflow_content.get("schemas", []))
        elif ptype == "pipeline_template":
            _append_pipeline_template_pin(pin, content, agents, graph_nodes, edges, schema_ids_by_slug)
        elif ptype == "agent":
            _append_agent_pin(pin, agents, graph_nodes, schema_ids_by_slug)
        else:
            raise CollectionInstallError(
                f"Collection pin '{pin.name}' (slug '{pin.slug}') has unsupported primitive "
                f"type '{ptype}' for installation"
            )

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
        "edges": edges,
    }


def _append_agent_pin(
    pin: LibraryPrimitive,
    agents: list[dict[str, Any]],
    graph_nodes: list[dict[str, Any]],
    schema_ids_by_slug: dict[str, str],
) -> None:
    """Contribute a standalone ``agent`` pin's definition and graph node.

    Slug-shaped schema references (``input_schema`` / ``output_schema``) are
    resolved against the collection's own schema pins so the installed agent
    points at the freshly materialised schema entities.
    """
    content = pin.content_json or {}
    entry: dict[str, Any] = {
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
    for slug_key, id_key in (("input_schema", "input_schema_id"), ("output_schema", "output_schema_id")):
        slug_ref = content.get(slug_key)
        if isinstance(slug_ref, str) and slug_ref in schema_ids_by_slug:
            entry[id_key] = schema_ids_by_slug[slug_ref]
    agents.append(entry)

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


def _append_pipeline_template_pin(
    pin: LibraryPrimitive,
    content: dict[str, Any],
    agents: list[dict[str, Any]],
    graph_nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    schema_ids_by_slug: dict[str, str],
) -> None:
    """Contribute a ``pipeline_template`` pin to the bundle.

    The template's embedded agents become bundle agent entries (with fresh
    export ids), the template's graph nodes reference them via
    ``agent_index`` → the export id (which ``materialize_import`` rewires),
    and the template's edges are emitted as real pipeline edges with their
    HITL gate configs preserved.
    """
    template_agents = content.get("agents", [])
    template_nodes = content.get("graph_nodes", [])
    template_edges = content.get("edges", [])

    export_agent_ids = [str(uuid.uuid4()) for _ in template_agents]
    for tmpl_agent, export_id in zip(template_agents, export_agent_ids, strict=True):
        entry: dict[str, Any] = {
            "id": export_id,
            "name": tmpl_agent.get("name", ""),
            "description": tmpl_agent.get("description"),
            "prompt_template": tmpl_agent.get("prompt_template", ""),
            "connector_type_refs": tmpl_agent.get("connector_type_refs", []),
            "evals": tmpl_agent.get("evals"),
            "retry_policy": tmpl_agent.get("retry_policy"),
            "token_budget": tmpl_agent.get("token_budget"),
        }
        for slug_key, id_key in (("input_schema", "input_schema_id"), ("output_schema", "output_schema_id")):
            slug_ref = tmpl_agent.get(slug_key)
            if isinstance(slug_ref, str) and slug_ref in schema_ids_by_slug:
                entry[id_key] = schema_ids_by_slug[slug_ref]
        agents.append(entry)

    node_id_map = {node.get("id", ""): str(uuid.uuid4()) for node in template_nodes}
    for node in template_nodes:
        new_node: dict[str, Any] = {
            "id": node_id_map.get(node.get("id", ""), node.get("id", "")) or str(uuid.uuid4()),
            "node_type": node.get("node_type", "agent"),
            "position": node.get("position", {"x": 0, "y": 0}),
            "label": node.get("label", ""),
        }
        agent_index = node.get("agent_index")
        if agent_index is not None and 0 <= agent_index < len(export_agent_ids):
            new_node["agent_id"] = export_agent_ids[agent_index]
        if node.get("output_schema_id") and node["output_schema_id"] in schema_ids_by_slug:
            new_node["output_schema_id"] = schema_ids_by_slug[node["output_schema_id"]]
        graph_nodes.append(new_node)

    for edge in template_edges:
        source = edge.get("source_node_id", edge.get("source", ""))
        target = edge.get("target_node_id", edge.get("target", ""))
        new_edge: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "source_node_id": node_id_map.get(source, source),
            "target_node_id": node_id_map.get(target, target),
            "edge_type": edge.get("edge_type", "normal"),
        }
        if edge.get("hitl_gate_config"):
            new_edge["hitl_gate_config"] = edge["hitl_gate_config"]
        edges.append(new_edge)


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

    # Defer the autoflush triggered by the ``session.get`` lookups below until
    # the ``CollectionInstall`` provenance row has been added to the session
    # (install_collection step 10). Flushing the stamped ``collection_install_id``
    # values before that row exists would violate ``fk_agents_collection_install_id``
    # (added in migration 0223), since the FK target is not yet present.
    with session.no_autoflush:
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


def _connector_ref_type_id(ref: Any) -> str:
    """Extract the connector type id from a single connector_type_refs entry.

    Entries are either plain strings (Native library built-ins like
    ``["github"]``) or dicts (``{"connector_type_id": ...}`` /
    ``{"connector_type": ...}``).
    """
    if isinstance(ref, str):
        return ref
    if isinstance(ref, dict):
        value = ref.get("connector_type_id") or ref.get("connector_type") or ref.get("type", "")
        return str(value) if value is not None else ""
    return ""


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
            ctid = _connector_ref_type_id(ref)
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
    # 1. Load the collection primitive — org-scoped DB first, then the
    # library_service resolver so shipped modulo/community collections
    # (in-code registry + published community cache, no DB row in the
    # installing org's RLS slice) are installable per ADR 032 §4.
    collection = await _resolve_collection(session, org_id, collection_id)
    if collection is None:
        raise CollectionInstallError(f"Collection {collection_id} not found")
    if collection.primitive_type != "library_collection":
        raise CollectionInstallError(f"Primitive {collection_id} is not a collection")
    if collection.status != "published":
        raise CollectionNotPublishedError(
            f"Collection '{collection.name}' must be published before installing (current status: {collection.status})"
        )

    # 2. Resolve manifest pins
    # Local org collections carry the pins on the ``manifest_pins`` column;
    # built-in modulo/community collections carry them in ``content_json``.
    manifest_pins = collection.manifest_pins or (collection.content_json or {}).get("manifest_pins") or []
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

    # 2b. Persist ONLY the collection row when it comes from the in-code
    # registry (built-in modulo/community items). ``collection_install.collection_id``
    # references ``library_primitives``, so that one row must exist on disk;
    # pin rows are NEVER persisted — pin content is consumed in-memory by
    # ``_build_bundle_from_pins`` and the registry stays the single source of
    # truth. The returned id is the backing DB row's id and is used for the
    # install FK below (it may differ from the registry id when a same-tuple
    # row already existed under a different id).
    collection_row_id = await _persist_collection_row(session, org_id, collection)

    # 3. Build the materialize_import bundle
    bundle = _build_bundle_from_pins(resolved_pins)

    # 4. Refuse if the collection is already installed in this organisation
    existing_stmt = select(CollectionInstall).where(
        CollectionInstall.organisation_id == org_id,
        CollectionInstall.collection_id == collection_row_id,
    )
    existing = (await session.execute(existing_stmt)).scalar_one_or_none()
    if existing is not None:
        raise CollectionInstallError(f"Collection '{collection.name}' is already installed in this organisation")
    install_id = uuid.uuid4()

    # Create the provenance row up front and flush it so the
    # ``collection_install_id`` FK (migration 0223) is satisfied when
    # ``_stamp_install_id`` autoflushes the agent/pipeline UPDATEs below. The
    # transaction rolls back the whole install on any later failure.
    install = CollectionInstall(
        install_id=install_id,
        collection_id=collection_row_id,
        collection_version=collection.version,
        organisation_id=org_id,
        status="installed",
        community_sourced=collection.source in ("community", "registry"),
    )
    session.add(install)
    await session.flush()

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
    install.community_sourced = community_sourced

    # 10. Populate the provenance record (row already created + flushed above).
    install.resolved_manifest = {
        "schemas": result.get("schemas", {}),
        "agents": result.get("agents", {}),
        "pipeline_id": result.get("pipeline_id"),
        "warnings": warnings,
    }
    install.connector_checklist = connector_checklist
    install.installed_entities = entities

    await session.flush()

    # ``install`` is the ORM instance already added to the session identity map;
    # the field mutations above were applied in place, so return it directly.
    return install
