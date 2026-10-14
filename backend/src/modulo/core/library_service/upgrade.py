"""Collection upgrade service — version-upgrade / "update available" flow (FAR-759).

An installed collection is version-pinned (ADR 032). When the author publishes
a newer version of the collection, the installed org needs a way to see the
delta and reconcile. This module reuses the library's existing version
machinery — ``version_group_id`` links version rows and
``update_available_version_id`` is honoured when the back-propagation
(``notify_importers_of_update``) has stamped it — and adds a read-time
version-group scan as the fallback so detection works even when the stamp
never landed (legacy installs, direct row seeds).

Upgrade semantics (operator-initiated only; no auto-updates):

* **Operator-modified entities are NEVER clobbered.** An entity counts as
  modified when its ``collection_install_id`` provenance stamp was cleared
  OR its content drifted from the install-time fingerprint snapshot
  (``resolved_manifest["entity_fingerprints"]``). Modified entities are
  preserved and reported.
* **Unmodified entities whose pin changed are reconciled in place** —
  schema definitions and agent content fields are rewritten from the new
  pin's content; entity ids (and therefore references) stay stable.
* **Added pins are reported, not materialised**; **removed pins are
  reported, not deleted** (deletion is the uninstall flow's job). The
  install's pipeline graph is never auto-rewritten: structural graph changes
  are reported (``action='manual'``) and require reinstalling the collection.
* After a successful upgrade the install record points at the new version
  row (``collection_id`` / ``collection_version``) and its snapshot is
  refreshed so the next upgrade compares against the new baseline.

Detection and upgrade both run inside the caller's transaction under the
installing org's RLS context; only org-owned collection rows are versionable
(shipped registry collections have no DB-persisted version group, so they
correctly report "no update available").
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.library_service.install import (
    _PIN_TYPE_ORDER,
    _schema_definition_from_content,
    compute_entity_fingerprint,
)
from modulo.db.models.agent import Agent
from modulo.db.models.collection_install import CollectionInstall
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.schema import Schema, SchemaVersion
from modulo.db.soft_delete import include_soft_deleted

__all__ = [
    "CollectionUpgradeError",
    "NoUpdateAvailableError",
    "UpgradeInstallNotFoundError",
    "detect_collection_update",
    "find_newer_version",
    "upgrade_collection",
]


class CollectionUpgradeError(Exception):
    """Base error for collection upgrade failures."""


class UpgradeInstallNotFoundError(CollectionUpgradeError):
    """Raised when the install record does not exist or belongs to another org/collection."""


class NoUpdateAvailableError(CollectionUpgradeError):
    """Raised when an upgrade is requested but no newer published version exists."""


# ---------------------------------------------------------------------------
# Pin / version resolution
# ---------------------------------------------------------------------------


def _version_key(version: str | None) -> tuple[int, ...]:
    """Parse a dotted version string into a sortable int tuple."""
    if not version:
        return (0,)
    try:
        return tuple(int(x) for x in version.split("."))
    except ValueError:
        return (0,)


def _pins_from_manifest(manifest_pins: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """Normalise raw manifest pin dicts to {slug, version} entries."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for pin in manifest_pins or []:
        slug = str(pin.get("slug", ""))
        version = str(pin.get("version", ""))
        if not slug or not version or slug in seen:
            continue
        seen.add(slug)
        out.append({"slug": slug, "version": version})
    return out


def _snapshot_pins(install: CollectionInstall, collection: LibraryPrimitive) -> list[dict[str, str]]:
    """Pins the install was created with, with their primitive types.

    Prefers the install-time snapshot (``resolved_manifest["pins"]``); falls
    back to the collection row's current manifest for legacy installs.
    """
    manifest = install.resolved_manifest or {}
    pins = manifest.get("pins")
    if isinstance(pins, list) and pins:
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        for pin in pins:
            slug = str(pin.get("slug", ""))
            if not slug or slug in seen:
                continue
            seen.add(slug)
            out.append(
                {
                    "slug": slug,
                    "version": str(pin.get("version", "")),
                    "primitive_type": str(pin.get("primitive_type", "")),
                }
            )
        return out
    return [
        {**p, "primitive_type": ""}
        for p in _pins_from_manifest(collection.manifest_pins or (collection.content_json or {}).get("manifest_pins"))
    ]


def _pin_entity_map(install: CollectionInstall) -> dict[str, dict[str, list[str]]]:
    """Install-time pin → installed-entity id map from the snapshot."""
    manifest = install.resolved_manifest or {}
    raw = manifest.get("pin_entity_ids")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, list[str]]] = {}
    for slug, per_type in raw.items():
        if not isinstance(per_type, dict):
            continue
        out[str(slug)] = {
            str(etype): [str(eid) for eid in ids] for etype, ids in per_type.items() if isinstance(ids, list)
        }
    return out


async def _resolve_pin_primitive(
    session: AsyncSession,
    org_id: uuid.UUID,
    slug: str,
    version: str,
) -> tuple[LibraryPrimitive | None, str]:
    """Resolve a manifest pin to its backing primitive.

    Routes through the same deterministic type order as install
    (``_PIN_TYPE_ORDER``). Returns ``(primitive, primitive_type)``; the
    primitive is None when the pin cannot be resolved at the pinned version
    (the type is still returned when any row with that slug was visible).
    """
    from modulo.core.library_service import get_primitive_by_slug

    for primitive_type in _PIN_TYPE_ORDER:
        prim = await get_primitive_by_slug(session, org_id, primitive_type, slug, explicit_org_filter=org_id)
        if prim is None:
            continue
        if prim.deleted_at is not None:
            continue
        if prim.version == version:
            return prim, primitive_type
        # A visible row of the right type but another version still tells us
        # the pin's type even though its content cannot be resolved.
        return None, primitive_type
    return None, ""


async def find_newer_version(
    session: AsyncSession,
    install: CollectionInstall,
    collection: LibraryPrimitive,
) -> LibraryPrimitive | None:
    """Return the newest published version row newer than the installed one.

    Honours ``collection.update_available_version_id`` first (the existing
    back-propagation stamp), then falls back to a read-time scan of the
    collection's ``version_group_id`` group. Returns None when the installed
    version is already the newest published one (or the collection has no
    DB-persisted version group, e.g. shipped registry collections).
    """
    installed_version = install.collection_version or collection.version
    installed_key = _version_key(installed_version)

    stamped_id = collection.update_available_version_id
    if stamped_id is not None:
        stamped = await session.get(LibraryPrimitive, stamped_id)
        if (
            stamped is not None
            and stamped.deleted_at is None
            and stamped.status == "published"
            and _version_key(stamped.version) > installed_key
        ):
            return stamped

    group_id = collection.version_group_id or collection.id
    stmt = (
        select(LibraryPrimitive)
        .where(
            LibraryPrimitive.organisation_id == collection.organisation_id,
            LibraryPrimitive.deleted_at.is_(None),
            LibraryPrimitive.status == "published",
            or_(
                LibraryPrimitive.version_group_id == group_id,
                LibraryPrimitive.id == group_id,
            ),
        )
        .order_by(LibraryPrimitive.created_at.desc())
    )
    candidates = (await session.execute(stmt)).scalars()
    best: LibraryPrimitive | None = None
    for row in candidates:
        # The SQL filters on status; re-check so a stray row can never win.
        if row.status != "published":
            continue
        if _version_key(row.version) <= installed_key:
            continue
        if best is None or _version_key(row.version) > _version_key(best.version):
            best = row
    return best


async def _load_install(
    session: AsyncSession,
    org_id: uuid.UUID,
    install_id: uuid.UUID,
    collection_id: uuid.UUID,
) -> CollectionInstall:
    """Load and validate the install record against org + collection."""
    install = await session.get(CollectionInstall, install_id)
    if install is None or install.organisation_id != org_id:
        raise UpgradeInstallNotFoundError(f"Install {install_id} not found for organisation {org_id}")
    # Soft-deleted (uninstalled) installs are retired history, never live.
    # ``session.get`` can resolve an already-uninstalled row from the session
    # identity map, bypassing the global ``deleted_at IS NULL`` filter, so the
    # guard is explicit — the same rule ``uninstall._load_install`` applies.
    if install.deleted_at is not None:
        raise UpgradeInstallNotFoundError(f"Install {install_id} not found for organisation {org_id}")
    if install.collection_id != collection_id:
        raise UpgradeInstallNotFoundError(f"Install {install_id} does not belong to collection {collection_id}")
    return install


# ---------------------------------------------------------------------------
# Delta computation (pure — unit-testable without a session)
# ---------------------------------------------------------------------------

_ACTION_BY_CHANGE = {
    "added": "report",
    "removed": "report",
    "unchanged": "unchanged",
}

_BUCKET_BY_TYPE = {
    "schema": "schemas",
    "agent": "agents",
    "workflow": "pipelines",
    "pipeline_template": "pipelines",
}


def _change_kind(old: dict[str, str] | None, new: dict[str, str] | None) -> str:
    """Classify a pin as added / removed / changed / unchanged."""
    if old is not None and new is None:
        return "removed"
    if old is None and new is not None:
        return "added"
    if old is not None and new is not None and old["version"] != new["version"]:
        return "changed"
    return "unchanged"


def _action_for_change(change: str, entities: list[dict[str, Any]]) -> str:
    """Resolve the operator action for a pin from its change and entities."""
    if change != "changed":
        return _ACTION_BY_CHANGE[change]
    upgradable = any(not e["modified"] and e["entity_type"] in ("schema", "agent") for e in entities)
    if upgradable:
        return "upgrade"
    only_structural = bool(entities) and any(e["entity_type"] == "pipeline" for e in entities)
    return "manual" if only_structural else "report"


def _pin_entities(
    slug: str,
    ptype: str,
    pin_entity_ids: dict[str, dict[str, list[str]]],
    entity_states: dict[str, str],
    pipeline_entity_id: str | None,
    pipeline_modified: bool,
) -> list[dict[str, Any]]:
    """Installed entities a pin maps to, prepending the embedded pipeline.

    A workflow/pipeline_template pin carries the install's pipeline entity
    plus its embedded schema/agent entities.
    """
    per_type = pin_entity_ids.get(slug) or {}
    entries = [
        {
            "entity_type": etype,
            "entity_id": eid,
            "modified": entity_states.get(eid, "unmodified") == "modified",
        }
        for etype, ids in per_type.items()
        for eid in ids
    ]
    if ptype in ("workflow", "pipeline_template") and pipeline_entity_id:
        return [
            {
                "entity_type": "pipeline",
                "entity_id": pipeline_entity_id,
                "modified": pipeline_modified,
            },
            *entries,
        ]
    return entries


def _bucket_for_pin(ptype: str, entities: list[dict[str, Any]]) -> str:
    """Resolve the delta bucket for a pin, classifying unknown types by kind."""
    bucket = _BUCKET_BY_TYPE.get(ptype)
    if bucket is not None:
        return bucket
    kinds = {e["entity_type"] for e in entities}
    if "agent" in kinds:
        return "agents"
    if "schema" in kinds:
        return "schemas"
    return "pipelines"


def _append_synthesised_pipeline_entry(
    delta: dict[str, list[dict[str, Any]]],
    pipeline_entity_id: str | None,
    pipeline_modified: bool,
) -> None:
    """Surface the install's pipeline entity when no pin produced it.

    The install's pipeline entity always surfaces in the pipelines bucket,
    even when no workflow/template pin exists (synthesised pipeline).
    """
    if not pipeline_entity_id:
        return
    covered = any(
        e["entities"] and any(ent["entity_type"] == "pipeline" for ent in e["entities"])
        for bucket in delta.values()
        for e in bucket
    )
    if covered:
        return
    # The synthesised pipeline's node set changes only when agent pins
    # are added/removed — content changes do not alter the graph.
    structural = any(
        e["change"] in ("added", "removed") and e["entity_type"] == "agent" for b in delta.values() for e in b
    )
    delta["pipelines"].append(
        {
            "entity_type": "pipeline",
            "slug": None,
            "from_version": None,
            "to_version": None,
            "change": "changed" if structural else "unchanged",
            "action": "manual" if structural else "unchanged",
            "entities": [
                {
                    "entity_type": "pipeline",
                    "entity_id": pipeline_entity_id,
                    "modified": pipeline_modified,
                }
            ],
        }
    )


def build_collection_delta(
    old_pins: list[dict[str, str]],
    new_pins: list[dict[str, str]],
    pin_entity_ids: dict[str, dict[str, list[str]]],
    entity_states: dict[str, str],
    pipeline_entity_id: str | None,
    pipeline_modified: bool,
) -> dict[str, list[dict[str, Any]]]:
    """Build the per-entity-type update delta (pure function).

    ``entity_states`` maps entity id → ``unmodified`` / ``modified`` /
    ``missing``. Buckets are schemas / agents / pipelines; a
    workflow/pipeline_template pin lands in the pipelines bucket and carries
    the pipeline entity plus its embedded schema/agent entities.
    """
    delta: dict[str, list[dict[str, Any]]] = {"schemas": [], "agents": [], "pipelines": []}

    old_by_slug = {p["slug"]: p for p in old_pins}
    new_by_slug = {p["slug"]: p for p in new_pins}
    types_by_slug: dict[str, str] = {
        p["slug"]: p.get("primitive_type", "") for p in old_pins if p.get("primitive_type")
    }
    for p in new_pins:
        if p.get("primitive_type"):
            types_by_slug.setdefault(p["slug"], p["primitive_type"])

    for slug in dict.fromkeys([*old_by_slug, *new_by_slug]):
        old = old_by_slug.get(slug)
        new = new_by_slug.get(slug)
        change = _change_kind(old, new)
        ptype = types_by_slug.get(slug, "")
        entities = _pin_entities(slug, ptype, pin_entity_ids, entity_states, pipeline_entity_id, pipeline_modified)
        entry = {
            "entity_type": ptype or ("pipeline" if pipeline_entity_id else "unknown"),
            "slug": slug,
            "from_version": old["version"] if old else None,
            "to_version": new["version"] if new else None,
            "change": change,
            "action": _action_for_change(change, entities),
            "entities": entities,
        }
        delta[_bucket_for_pin(ptype, entities)].append(entry)

    _append_synthesised_pipeline_entry(delta, pipeline_entity_id, pipeline_modified)
    return delta


# ---------------------------------------------------------------------------
# Entity classification
# ---------------------------------------------------------------------------


async def _entity_exists(
    session: AsyncSession,
    entity_type: str,
    entity_id: uuid.UUID,
) -> Any:
    """Load a tracked entity row (pipelines include soft-deleted)."""
    if entity_type == "schema":
        return await session.get(Schema, entity_id)
    if entity_type == "agent":
        return await session.get(Agent, entity_id)
    if entity_type == "pipeline":
        stmt = include_soft_deleted(select(Pipeline).where(Pipeline.id == entity_id))
        return await session.scalar(stmt)
    return None


async def _content_drifted(
    session: AsyncSession,
    org_id: uuid.UUID,
    etype: str,
    eid: str,
    fingerprints: dict[str, str],
) -> bool:
    """Whether a stamped schema/agent's content drifted from its snapshot."""
    stored = fingerprints.get(eid)
    if not stored or etype not in ("schema", "agent"):
        return False
    current = await compute_entity_fingerprint(session, org_id, etype, uuid.UUID(eid))
    return current is not None and current != stored


async def _classify_entity(
    session: AsyncSession,
    org_id: uuid.UUID,
    install: CollectionInstall,
    fingerprints: dict[str, str],
    etype: str,
    eid: str,
    states: dict[str, str],
) -> None:
    """Classify one installed entity, recording its state in ``states``.

    Modified = provenance stamp cleared OR content drifted from the
    install-time fingerprint snapshot. Missing = row no longer exists.
    """
    if eid in states:
        return
    entity = await _entity_exists(session, etype, uuid.UUID(eid))
    if entity is None:
        states[eid] = "missing"
        return
    if getattr(entity, "collection_install_id", None) != install.install_id:
        states[eid] = "modified"
        return
    if await _content_drifted(session, org_id, etype, eid, fingerprints):
        states[eid] = "modified"
        return
    states[eid] = "unmodified"


async def _classify_entities(
    session: AsyncSession,
    org_id: uuid.UUID,
    install: CollectionInstall,
    pin_entity_ids: dict[str, dict[str, list[str]]],
) -> dict[str, str]:
    """Classify each installed entity as unmodified / modified / missing.

    Modified = provenance stamp cleared OR content drifted from the
    install-time fingerprint snapshot. Missing = row no longer exists.
    """
    fingerprints = (install.resolved_manifest or {}).get("entity_fingerprints") or {}
    states: dict[str, str] = {}
    for per_type in pin_entity_ids.values():
        for etype, ids in per_type.items():
            for eid in ids:
                await _classify_entity(session, org_id, install, fingerprints, etype, eid, states)
    return states


async def _pipeline_state(
    session: AsyncSession,
    install: CollectionInstall,
    pipeline_id_str: str | None,
) -> tuple[str | None, bool]:
    """Return (pipeline entity id, modified) for the install's pipeline."""
    if not pipeline_id_str:
        return None, False
    pipeline_id = uuid.UUID(pipeline_id_str)
    entity = await _entity_exists(session, "pipeline", pipeline_id)
    if entity is None:
        return pipeline_id_str, True
    modified = entity.collection_install_id != install.install_id
    return pipeline_id_str, modified


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


async def _legacy_add_pin(
    session: AsyncSession,
    org_id: uuid.UUID,
    pin: dict[str, str],
    id_maps: dict[str, dict[str, str]],
    out: dict[str, dict[str, list[str]]],
) -> None:
    """Add one legacy pin's local entity id to ``out`` when resolvable."""
    prim, resolved_type = await _resolve_pin_primitive(session, org_id, pin["slug"], pin["version"])
    if prim is None:
        return
    use_type = pin.get("primitive_type", "") or resolved_type
    id_map = id_maps.get(use_type)
    if id_map is None:
        return
    local = id_map.get(str(prim.id))
    if not local:
        return
    out.setdefault(pin["slug"], {}).setdefault(use_type, []).append(str(local))


async def _legacy_pin_context(
    session: AsyncSession,
    org_id: uuid.UUID,
    install: CollectionInstall,
    old_pins: list[dict[str, str]],
) -> dict[str, dict[str, list[str]]]:
    """Best-effort pin→entity map for installs without the install snapshot.

    Schema and standalone agent pins resolve through the primitive id used as
    the bundle export id; workflow/template embedded entities cannot be
    mapped retroactively and are omitted.
    """
    manifest = install.resolved_manifest or {}
    id_maps: dict[str, dict[str, str]] = {
        "schema": manifest.get("schemas") or {},
        "agent": manifest.get("agents") or {},
    }
    out: dict[str, dict[str, list[str]]] = {}
    for pin in old_pins:
        await _legacy_add_pin(session, org_id, pin, id_maps, out)
    return out


async def detect_collection_update(
    session: AsyncSession,
    org_id: uuid.UUID,
    install_id: uuid.UUID,
    collection_id: uuid.UUID,
) -> dict[str, Any]:
    """Detect whether an installed collection has a newer published version.

    Returns a report dict::

        {
          "update_available": bool,
          "current_version": str | None,
          "available_version": str | None,
          "available_version_id": uuid | None,
          "delta": {"schemas": [...], "agents": [...], "pipelines": [...]} | None,
          "warnings": [str, ...],
        }

    The delta is grouped per entity type and classifies every pin as
    added / changed / unchanged / removed, with the installed entities it
    maps to and whether each was operator-modified (preserved on upgrade).
    """
    install = await _load_install(session, org_id, install_id, collection_id)
    collection = await session.get(LibraryPrimitive, install.collection_id)
    current_version = install.collection_version or (collection.version if collection else None)
    if collection is None:
        # The collection row is not visible in this org's RLS slice (shipped
        # registry collections persist only under the sentinel org). Such rows
        # have no DB-persisted version group here, so no update can exist.
        return {
            "update_available": False,
            "current_version": current_version,
            "available_version": None,
            "available_version_id": None,
            "delta": None,
            "warnings": [],
        }

    newer = await find_newer_version(session, install, collection)
    if newer is None:
        return {
            "update_available": False,
            "current_version": current_version,
            "available_version": None,
            "available_version_id": None,
            "delta": None,
            "warnings": [],
        }

    warnings: list[str] = []
    old_pins = await _load_old_pins(session, org_id, install, collection)
    new_pins, _ = await _load_new_pins(session, org_id, newer)
    pin_entity_ids = await _load_pin_entity_ids(session, org_id, install, old_pins)

    entity_states = await _classify_entities(session, org_id, install, pin_entity_ids)
    pipeline_id, pipeline_modified = await _pipeline_state(
        session, install, (install.resolved_manifest or {}).get("pipeline_id")
    )

    delta = build_collection_delta(
        old_pins,
        new_pins,
        pin_entity_ids,
        entity_states,
        pipeline_id,
        pipeline_modified,
    )
    return {
        "update_available": True,
        "current_version": current_version,
        "available_version": newer.version,
        "available_version_id": newer.id,
        "delta": delta,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def _agent_pin_content_fields(content: dict[str, Any]) -> dict[str, Any]:
    """Extract the agent content fields an upgrade reconciles from pin content.

    Mirrors the install-path agent entry shape (``_append_agent_pin`` /
    ``_append_pipeline_template_pin``) minus schema-id rewiring, which the
    caller applies from the installed schema map.
    """
    return {
        "prompt_template": content.get("prompt_template", ""),
        "description": content.get("description"),
        "connector_type_refs": content.get("connector_type_refs", []),
        "evals": content.get("evals"),
        "retry_policy": content.get("retry_policy"),
        "token_budget": content.get("token_budget"),
        "input_schema_slug": content.get("input_schema"),
        "output_schema_slug": content.get("output_schema"),
    }


def _workflow_bundle_content(content: dict[str, Any]) -> dict[str, Any]:
    """Unwrap a workflow pin's embedded bundle (same rule as install)."""
    return content.get("bundle") or content


def _embedded_agent_contents(pin: LibraryPrimitive) -> list[dict[str, Any]]:
    """Agent content dicts embedded in a workflow / pipeline_template pin."""
    content = pin.content_json or {}
    if pin.primitive_type == "workflow":
        embedded = _workflow_bundle_content(content).get("agents", [])
    elif pin.primitive_type == "pipeline_template":
        embedded = content.get("agents", [])
    else:
        embedded = []
    return [entry for entry in embedded if isinstance(entry, dict)]


def _embedded_schema_contents(pin: LibraryPrimitive) -> list[dict[str, Any]]:
    """Schema content dicts embedded in a workflow pin (templates add none)."""
    content = pin.content_json or {}
    if pin.primitive_type != "workflow":
        return []
    embedded = _workflow_bundle_content(content).get("schemas", [])
    return [entry for entry in embedded if isinstance(entry, dict)]


async def _reconcile_schema_entity(
    session: AsyncSession,
    org_id: uuid.UUID,
    entity_id: uuid.UUID,
    definition: dict[str, Any] | None,
    description: str | None,
    abstract_name: str | None,
    warnings: list[str],
) -> bool:
    """Rewrite an unmodified installed schema's definition from new pin content.

    The schema's identity (name, ids) is stable; the current SchemaVersion
    definition and the descriptive fields are reconciled in place.
    """
    schema = await session.get(Schema, entity_id)
    if schema is None or schema.organisation_id != org_id:
        warnings.append(f"Schema entity {entity_id} not found; skipped.")
        return False
    if definition is None:
        warnings.append(f"Schema entity {entity_id} has no definition in the new pin; skipped.")
        return False
    stmt = (
        select(SchemaVersion)
        .where(
            SchemaVersion.schema_id == entity_id,
            SchemaVersion.organisation_id == org_id,
        )
        .order_by(SchemaVersion.version_number.desc())
        .limit(1)
    )
    version_row = await session.scalar(stmt)
    if version_row is None:
        warnings.append(f"Schema entity {entity_id} has no schema version row; skipped.")
        return False
    version_row.definition_json = definition
    if description is not None:
        schema.description = description
    if abstract_name is not None:
        schema.abstract_name = abstract_name
    return True


async def _reconcile_agent_entity(
    session: AsyncSession,
    org_id: uuid.UUID,
    entity_id: uuid.UUID,
    content: dict[str, Any],
    schema_slug_map: dict[str, uuid.UUID],
    warnings: list[str],
) -> bool:
    """Rewrite an unmodified installed agent's content from new pin content.

    Identity (name) is stable; prompt, description, connector refs, evals,
    retry policy, token budget and schema references are reconciled. A prompt
    that fails the git-content pinning check is skipped with a warning
    (fail-loud, never silently rewritten).
    """
    from modulo.core.pipeline_engine.git_content import GitContentRefError, validate_agent_git_content_values

    agent = await session.get(Agent, entity_id)
    if agent is None or agent.organisation_id != org_id:
        warnings.append(f"Agent entity {entity_id} not found; skipped.")
        return False

    fields = _agent_pin_content_fields(content)
    prompt = str(fields["prompt_template"] or "")
    try:
        validate_agent_git_content_values(prompt_template=prompt)
    except GitContentRefError as exc:
        warnings.append(f"Agent entity {entity_id} prompt rejected by git-content check; skipped: {exc}")
        return False

    agent.prompt_template = prompt
    agent.description = fields["description"]
    agent.connector_type_refs = fields["connector_type_refs"] or []
    agent.evals = fields["evals"]
    agent.retry_policy = fields["retry_policy"] or {}
    agent.token_budget = fields["token_budget"]

    input_slug = fields["input_schema_slug"]
    if isinstance(input_slug, str) and input_slug in schema_slug_map:
        agent.input_schema_id = schema_slug_map[input_slug]
    output_slug = fields["output_schema_slug"]
    if isinstance(output_slug, str) and output_slug in schema_slug_map:
        agent.output_schema_id = schema_slug_map[output_slug]
    return True


def _schema_slug_map(pin_entity_ids: dict[str, dict[str, list[str]]]) -> dict[str, uuid.UUID]:
    """Map schema-pin slug → the installed schema entity id (first wins)."""
    out: dict[str, uuid.UUID] = {}
    for slug, per_type in pin_entity_ids.items():
        ids = per_type.get("schema") or []
        if ids:
            out[slug] = uuid.UUID(ids[0])
    return out


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------


class _CollectionReconciler:
    """Applies a collection delta in place, recording what changed and why.

    Holds the reconciliation state (warnings, reconciled / preserved /
    reported lists) and the refreshed fingerprint map for one upgrade so each
    step stays small and independently testable.
    """

    def __init__(
        self,
        session: AsyncSession,
        org_id: uuid.UUID,
        pin_entity_ids: dict[str, dict[str, list[str]]],
        new_pin_prims: dict[str, LibraryPrimitive],
        schema_map: dict[str, uuid.UUID],
        new_fingerprints: dict[str, str],
    ) -> None:
        self._session = session
        self._org_id = org_id
        self._pin_entity_ids = pin_entity_ids
        self._new_pin_prims = new_pin_prims
        self._schema_map = schema_map
        self._new_fingerprints = new_fingerprints
        self.warnings: list[str] = []
        self.reconciled: list[dict[str, str]] = []
        self.preserved: list[dict[str, str]] = []
        self.reported: list[dict[str, str]] = []

    async def reconcile_delta(self, delta: dict[str, list[dict[str, Any]]]) -> None:
        """Reconcile changed pins: schemas first (agents may reference them), then agents, then workflows."""
        for bucket in ("schemas", "agents", "pipelines"):
            for entry in delta[bucket]:
                await self._reconcile_entry(entry)

    async def _reconcile_entry(self, entry: dict[str, Any]) -> None:
        change = entry["change"]
        slug = entry["slug"]
        if change in ("added", "removed"):
            self.reported.append({"slug": slug or "", "change": change, "entity_type": entry["entity_type"]})
            return
        if change == "unchanged":
            return
        ptype = entry["entity_type"]
        new_prim = self._new_pin_prims.get(slug) if slug else None
        if ptype == "schema":
            await self._reconcile_schema_pin(entry, slug, new_prim)
        elif ptype == "agent":
            await self._reconcile_agent_pin(entry, slug, new_prim)
        elif ptype in ("workflow", "pipeline_template"):
            await self._reconcile_embedded_pin(entry, slug, new_prim)

    async def _reconcile_schema_pin(self, entry: dict[str, Any], slug: str, new_prim: LibraryPrimitive | None) -> None:
        if new_prim is None:
            self._warn_unresolved(entry, slug)
            return
        definition = _schema_definition_from_content(new_prim.content_json or {})
        for ent in entry["entities"]:
            if ent["entity_type"] != "schema":
                continue
            if ent["modified"]:
                self.preserved.append({"slug": slug, "entity_type": "schema", "entity_id": ent["entity_id"]})
                continue
            eid = uuid.UUID(ent["entity_id"])
            ok = await _reconcile_schema_entity(
                self._session,
                self._org_id,
                eid,
                definition,
                new_prim.description,
                (new_prim.content_json or {}).get("abstract_name"),
                self.warnings,
            )
            if ok:
                await self._fingerprint_and_record("schema", ent["entity_id"], slug)

    async def _reconcile_agent_pin(self, entry: dict[str, Any], slug: str, new_prim: LibraryPrimitive | None) -> None:
        if new_prim is None:
            self._warn_unresolved(entry, slug)
            return
        for ent in entry["entities"]:
            if ent["entity_type"] != "agent":
                continue
            if ent["modified"]:
                self.preserved.append({"slug": slug, "entity_type": "agent", "entity_id": ent["entity_id"]})
                continue
            eid = uuid.UUID(ent["entity_id"])
            ok = await _reconcile_agent_entity(
                self._session,
                self._org_id,
                eid,
                new_prim.content_json or {},
                self._schema_map,
                self.warnings,
            )
            if ok:
                await self._fingerprint_and_record("agent", ent["entity_id"], slug)

    async def _reconcile_embedded_pin(
        self, entry: dict[str, Any], slug: str, new_prim: LibraryPrimitive | None
    ) -> None:
        for ent in entry["entities"]:
            if ent["entity_type"] == "pipeline":
                # The pipeline graph is never auto-rewritten.
                self.preserved.append(
                    {"slug": slug, "entity_type": "pipeline", "entity_id": ent["entity_id"], "reason": "structural"}
                )
                continue
            if ent["modified"]:
                self.preserved.append(
                    {
                        "slug": slug,
                        "entity_type": ent["entity_type"],
                        "entity_id": ent["entity_id"],
                        "reason": "modified",
                    }
                )
                continue
            if new_prim is None:
                self._warn_unresolved(entry, slug)
                continue
            await self._reconcile_embedded_entity(slug, ent, new_prim)

    async def _reconcile_embedded_entity(self, slug: str, ent: dict[str, Any], new_prim: LibraryPrimitive) -> None:
        eid = uuid.UUID(ent["entity_id"])
        if ent["entity_type"] == "agent":
            ok = await self._reconcile_embedded_agent(ent, slug, eid, new_prim)
        else:
            ok = await self._reconcile_embedded_schema(ent, slug, eid, new_prim)
        if ok:
            await self._fingerprint_and_record(ent["entity_type"], ent["entity_id"], slug)

    async def _reconcile_embedded_agent(
        self, ent: dict[str, Any], slug: str, eid: uuid.UUID, new_prim: LibraryPrimitive
    ) -> bool:
        embedded = _embedded_agent_contents(new_prim)
        idx = self._embedded_index(slug, "agent", ent["entity_id"], embedded)
        if idx < 0:
            self.warnings.append(f"Embedded agent {ent['entity_id']} of pin '{slug}' not aligned; skipped.")
            return False
        return await _reconcile_agent_entity(
            self._session, self._org_id, eid, embedded[idx], self._schema_map, self.warnings
        )

    async def _reconcile_embedded_schema(
        self, ent: dict[str, Any], slug: str, eid: uuid.UUID, new_prim: LibraryPrimitive
    ) -> bool:
        embedded_schemas = _embedded_schema_contents(new_prim)
        idx = self._embedded_index(slug, "schema", ent["entity_id"], embedded_schemas)
        if idx < 0:
            self.warnings.append(f"Embedded schema {ent['entity_id']} of pin '{slug}' not aligned; skipped.")
            return False
        entry_content = embedded_schemas[idx]
        return await _reconcile_schema_entity(
            self._session,
            self._org_id,
            eid,
            entry_content.get("definition_json"),
            entry_content.get("description"),
            entry_content.get("abstract_name"),
            self.warnings,
        )

    def _embedded_index(self, slug: str, etype: str, entity_id: str, embedded: list[dict[str, Any]]) -> int:
        """Resolve an embedded entity's index in the new pin, or -1 when unaligned."""
        old_ids = (self._pin_entity_ids.get(slug) or {}).get(etype) or []
        idx = old_ids.index(entity_id) if entity_id in old_ids else -1
        if idx < 0 or idx >= len(embedded):
            return -1
        return idx

    def _warn_unresolved(self, entry: dict[str, Any], slug: str) -> None:
        self.warnings.append(f"Pin '{slug}' could not be resolved at version {entry['to_version']}; skipped.")

    async def _fingerprint_and_record(self, etype: str, entity_id: str, slug: str) -> None:
        """Refresh the reconciled entity's fingerprint and record it."""
        fp = await compute_entity_fingerprint(self._session, self._org_id, etype, uuid.UUID(entity_id))
        if fp:
            self._new_fingerprints[entity_id] = fp
        self.reconciled.append({"slug": slug, "entity_type": etype, "entity_id": entity_id})


async def _load_old_pins(
    session: AsyncSession,
    org_id: uuid.UUID,
    install: CollectionInstall,
    collection: LibraryPrimitive,
) -> list[dict[str, str]]:
    """Snapshot pins, filling missing primitive types via the pin resolver."""
    old_pins = _snapshot_pins(install, collection)
    for pin in old_pins:
        if not pin.get("primitive_type"):
            _, ptype = await _resolve_pin_primitive(session, org_id, pin["slug"], pin["version"])
            pin["primitive_type"] = ptype
    return old_pins


async def _load_new_pins(
    session: AsyncSession,
    org_id: uuid.UUID,
    newer: LibraryPrimitive,
) -> tuple[list[dict[str, str]], dict[str, LibraryPrimitive]]:
    """Resolve the newer version's manifest pins and the primitives they map to."""
    new_pin_dicts = _pins_from_manifest(newer.manifest_pins or (newer.content_json or {}).get("manifest_pins"))
    new_pins: list[dict[str, str]] = []
    new_pin_prims: dict[str, LibraryPrimitive] = {}
    for pin in new_pin_dicts:
        prim, ptype = await _resolve_pin_primitive(session, org_id, pin["slug"], pin["version"])
        new_pins.append({**pin, "primitive_type": ptype})
        if prim is not None:
            new_pin_prims[pin["slug"]] = prim
    return new_pins, new_pin_prims


async def _load_pin_entity_ids(
    session: AsyncSession,
    org_id: uuid.UUID,
    install: CollectionInstall,
    old_pins: list[dict[str, str]],
) -> dict[str, dict[str, list[str]]]:
    """Install-time pin→entity map, falling back to the legacy context."""
    pin_entity_ids = _pin_entity_map(install)
    if not pin_entity_ids:
        pin_entity_ids = await _legacy_pin_context(session, org_id, install, old_pins)
    return pin_entity_ids


def _apply_new_snapshot(
    install: CollectionInstall,
    collection: LibraryPrimitive,
    newer: LibraryPrimitive,
    new_pins: list[dict[str, str]],
    pin_entity_ids: dict[str, dict[str, list[str]]],
    new_fingerprints: dict[str, str],
) -> None:
    """Point the install at the new version row and refresh its snapshot."""
    new_snapshot_pins = [
        {"slug": p["slug"], "version": p["version"], "primitive_type": p["primitive_type"]} for p in new_pins
    ]
    new_pin_entity_ids: dict[str, dict[str, list[str]]] = {
        slug: per_type for slug, per_type in pin_entity_ids.items() if slug in {p["slug"] for p in new_pins}
    }
    manifest = dict(install.resolved_manifest or {})
    manifest["pins"] = new_snapshot_pins
    manifest["pin_entity_ids"] = new_pin_entity_ids
    manifest["entity_fingerprints"] = new_fingerprints
    install.resolved_manifest = manifest
    install.collection_id = newer.id
    install.collection_version = newer.version
    # The old row's back-propagation stamp is now stale — the install moved on.
    if collection.update_available_version_id is not None:
        collection.update_available_version_id = None


async def upgrade_collection(
    session: AsyncSession,
    org_id: uuid.UUID,
    created_by: uuid.UUID,
    install_id: uuid.UUID,
    collection_id: uuid.UUID,
) -> dict[str, Any]:
    """Upgrade an installed collection to its newest published version.

    Operator-initiated only. Unmodified entities whose pin changed are
    reconciled in place; operator-modified entities are preserved and
    reported; added/removed pins and structural pipeline changes are
    reported without action. On success the install record points at the new
    version row and its snapshot (pins, pin→entity map, fingerprints) is
    refreshed so the next upgrade compares against the new baseline.

    Raises ``UpgradeInstallNotFoundError`` when the install is absent /
    mismatched, ``NoUpdateAvailableError`` when no newer published version
    exists, and ``CollectionUpgradeError`` when the collection row is not
    visible for the install.
    """
    del created_by  # reserved for future audit of reconciling principal
    install = await _load_install(session, org_id, install_id, collection_id)
    collection = await session.get(LibraryPrimitive, install.collection_id)
    if collection is None:
        raise CollectionUpgradeError(f"Collection {install.collection_id} not visible for this organisation")

    newer = await find_newer_version(session, install, collection)
    if newer is None:
        raise NoUpdateAvailableError(
            f"Install {install_id} is already at the newest published version "
            f"'{install.collection_version or collection.version}'"
        )

    from_version = install.collection_version or collection.version
    old_pins = await _load_old_pins(session, org_id, install, collection)
    new_pins, new_pin_prims = await _load_new_pins(session, org_id, newer)
    pin_entity_ids = await _load_pin_entity_ids(session, org_id, install, old_pins)
    entity_states = await _classify_entities(session, org_id, install, pin_entity_ids)
    pipeline_id, pipeline_modified = await _pipeline_state(
        session, install, (install.resolved_manifest or {}).get("pipeline_id")
    )
    delta = build_collection_delta(
        old_pins,
        new_pins,
        pin_entity_ids,
        entity_states,
        pipeline_id,
        pipeline_modified,
    )

    new_fingerprints: dict[str, str] = dict((install.resolved_manifest or {}).get("entity_fingerprints") or {})
    reconciler = _CollectionReconciler(
        session,
        org_id,
        pin_entity_ids,
        new_pin_prims,
        _schema_slug_map(pin_entity_ids),
        new_fingerprints,
    )
    await reconciler.reconcile_delta(delta)

    _apply_new_snapshot(install, collection, newer, new_pins, pin_entity_ids, new_fingerprints)
    await session.flush()

    return {
        "install_id": str(install_id),
        "from_version": from_version,
        "to_version": newer.version,
        "reconciled": reconciler.reconciled,
        "preserved": reconciler.preserved,
        "reported": reconciler.reported,
        "warnings": reconciler.warnings,
    }
