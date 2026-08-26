"""Lifecycle-map bundle export / import and library-primitive support.

FAR-174: lifecycle maps can be exported as a portable JSON envelope, imported
to create a new map in an organisation, and stored as ``lifecycle_map`` library
primitives so they can be listed and copied-to-adapt.

FAR-204: the envelope is now ``format_version: 2`` and carries an optional
``versions`` array so the version history (each version's stages/edges/notes +
metadata) is exported and re-created on import. ``format_version: 1`` payloads
(no ``versions``) still import as a single-version map, keeping the FAR-174
envelope backward compatible.

The envelope mirrors the PRD §8.31.9 primitive model (``primitive_type`` +
``content_json`` of stages/edges) and reuses ``normalize_content`` for content
validation, so an imported map is validated exactly like an editor save.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.lifecycle_map.service import create_lifecycle_map, save_map_version
from modulo.core.lifecycle_map.validation import normalize_content
from modulo.core.workflow_import_export import _sanitize_slug, suggest_import_name
from modulo.db.crud.library_primitive import create_library_primitive
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.models.lifecycle_map import LifecycleMap

_log = logging.getLogger(__name__)

PRIMITIVE_TYPE = "lifecycle_map"
FORMAT_VERSION = "2"
_SUPPORTED_FORMAT_VERSIONS = ("1", "2")
_DEFAULT_LIBRARY_VERSION = "1.0"


class LifecycleMapBundleError(ValueError):
    """Raised when an import envelope fails bundle-level validation.

    Distinct from ``LifecycleMapContentError`` (content shape) because it
    covers the envelope itself (primitive type, format version, name).
    """


async def get_existing_lifecycle_map_names(session: AsyncSession, org_id: uuid.UUID) -> set[str]:
    """Return the names of all lifecycle maps in *org_id* (active or archived)."""
    result = await session.execute(select(LifecycleMap.name).where(LifecycleMap.organisation_id == org_id))
    return {row[0] for row in result}


def build_version_entry(lifecycle_map: LifecycleMap) -> dict[str, Any]:
    """Serialize the active version as a version-history entry.

    Returns ``{version, stages, edges, notes, created_at, created_by}`` with the
    canonical stages/edges graph so a multi-version envelope can round-trip
    through import and recreate the chain. In the v1 data model only the active
    version is retained, so a map exports exactly one entry; the envelope shape
    already supports more.
    """
    content = lifecycle_map.content_json if isinstance(lifecycle_map.content_json, dict) else {}
    normalized = normalize_content(content)
    updated_at = getattr(lifecycle_map, "updated_at", None)
    return {
        "version": lifecycle_map.version,
        "stages": normalized.get("stages", []),
        "edges": normalized.get("edges", []),
        "notes": normalized.get("notes", ""),
        "created_at": updated_at.isoformat() if updated_at is not None else None,
        "created_by": None,
    }


def build_export_envelope(lifecycle_map: LifecycleMap) -> dict[str, Any]:
    """Build the portable JSON envelope for a map's version history.

    Returns the canonical PRD §8.31.9 primitive shape — the same envelope the
    import endpoint accepts, so an export can round-trip through import.
    ``format_version`` is ``2`` and the envelope carries a ``versions`` array
    (one entry per version) so a version chain is preserved across the wire.
    The legacy ``content_json`` key is retained and mirrors the newest version,
    so v1 readers still work.
    """
    content = lifecycle_map.content_json if isinstance(lifecycle_map.content_json, dict) else {}
    normalized = normalize_content(content)
    return {
        "primitive_type": PRIMITIVE_TYPE,
        "format_version": FORMAT_VERSION,
        "name": lifecycle_map.name,
        "description": lifecycle_map.description,
        "content_json": normalized,
        "versions": [build_version_entry(lifecycle_map)],
    }


def _slugify(name: str) -> str:
    """Produce a URL-safe slug from a lifecycle map name."""
    if not name:
        return "lifecycle-map"
    return _sanitize_slug(name)


async def import_lifecycle_map_envelope(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    envelope: dict[str, Any],
    owner_team_id: uuid.UUID | None = None,
    visibility: str = "org",
) -> LifecycleMap:
    """Validate an export envelope and create a new lifecycle map + library primitive.

    Content validation is delegated to ``create_lifecycle_map`` →
    ``normalize_content`` (the same validation the editor-save path uses), so a
    malformed graph raises ``LifecycleMapContentError`` and a malformed
    envelope raises ``LifecycleMapBundleError``.

    Both envelope formats are accepted: ``format_version: 2`` with a
    ``versions`` array recreates the version chain deterministically, while a
    v1 envelope (no ``versions``) imports as a single-version map.
    """
    if envelope.get("primitive_type") != PRIMITIVE_TYPE:
        raise LifecycleMapBundleError(
            f"Unsupported primitive_type {envelope.get('primitive_type')!r}; expected '{PRIMITIVE_TYPE}'"
        )
    format_version = envelope.get("format_version")
    if format_version not in _SUPPORTED_FORMAT_VERSIONS:
        raise LifecycleMapBundleError(
            "Unsupported bundle format version "
            f"{format_version!r}; expected one of {sorted(_SUPPORTED_FORMAT_VERSIONS)}"
        )
    name = envelope.get("name")
    if not isinstance(name, str) or not name.strip():
        raise LifecycleMapBundleError("Lifecycle map export envelope is missing a non-empty 'name'")
    name = name.strip()
    description = envelope.get("description")
    versions = envelope.get("versions")
    if versions is not None and not isinstance(versions, list):
        raise LifecycleMapBundleError("Lifecycle map export envelope 'versions' must be an array")
    if not versions and not isinstance(envelope.get("content_json"), dict):
        raise LifecycleMapBundleError("Lifecycle map export envelope is missing 'content_json'")

    existing_names = await get_existing_lifecycle_map_names(session, org_id)
    map_name = suggest_import_name(existing_names, name)

    lifecycle_map = await _create_map_via_envelope(
        session,
        org_id=org_id,
        account_id=account_id,
        name=map_name,
        description=description,
        content_json=envelope.get("content_json"),
        versions=versions,
        owner_team_id=owner_team_id,
        visibility=visibility,
    )

    await create_library_primitive(
        session,
        org_id=org_id,
        source="local",
        primitive_type=PRIMITIVE_TYPE,
        name=map_name,
        slug=_slugify(map_name),
        description=description or "",
        author=account_id.hex,
        version=_DEFAULT_LIBRARY_VERSION,
        tags=["imported"],
        content_json={"lifecycle_map_id": str(lifecycle_map.id), "export": build_export_envelope(lifecycle_map)},
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
        account_id=account_id,
    )

    _log.info(
        "import_lifecycle_map_envelope: imported map '%s' (id=%s) for org %s",
        map_name,
        lifecycle_map.id,
        org_id,
    )
    return lifecycle_map


def _resolve_version_number(entry: dict[str, Any], index: int) -> int:
    """Derive the integer version number for one ``versions`` entry.

    Entries without a ``version`` key are numbered by 1-based position; an
    existing ``version`` is validated to be a positive integer.
    """
    raw_number = entry.get("version")
    if raw_number is None:
        return index + 1
    if isinstance(raw_number, bool) or not isinstance(raw_number, int):
        raise LifecycleMapBundleError(
            f"Lifecycle map 'versions' entry #{index} 'version' must be an integer, got {raw_number!r}"
        )
    if raw_number < 1:
        raise LifecycleMapBundleError(f"Lifecycle map 'versions' entry #{index} 'version' must be at least 1")
    return raw_number


def _extract_version_graph(entry: dict[str, Any], index: int) -> dict[str, Any]:
    """Validate and return the ``{stages, edges, notes}`` graph of one entry."""
    stages = entry.get("stages")
    edges = entry.get("edges")
    notes = entry.get("notes", "")
    if not isinstance(stages, list):
        raise LifecycleMapBundleError(f"Lifecycle map 'versions' entry #{index} is missing 'stages' array")
    if not isinstance(edges, list):
        raise LifecycleMapBundleError(f"Lifecycle map 'versions' entry #{index} is missing 'edges' array")
    if notes is not None and not isinstance(notes, str):
        raise LifecycleMapBundleError(f"Lifecycle map 'versions' entry #{index} 'notes' must be a string")
    return {"stages": stages, "edges": edges, "notes": notes if isinstance(notes, str) else ""}


def _normalise_version_history(versions: list[Any]) -> list[tuple[int, int, dict[str, Any]]]:
    """Validate and deterministically order a ``versions`` array.

    Returns ``(version_number, original_index, {stages, edges, notes})`` tuples
    sorted by ``version`` (stable by original position). Entries without an
    integer ``version`` are re-derived from their 1-based position so import is
    deterministic regardless of the exporter's numbering.
    """
    ordered: list[tuple[int, int, dict[str, Any]]] = []
    for index, entry in enumerate(versions):
        if not isinstance(entry, dict):
            raise LifecycleMapBundleError(f"Lifecycle map 'versions' entry #{index} must be an object")
        number = _resolve_version_number(entry, index)
        graph = _extract_version_graph(entry, index)
        ordered.append((number, index, graph))
    return sorted(ordered, key=lambda item: (item[0], item[1]))


async def _create_map_via_envelope(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    name: str,
    description: str | None,
    content_json: dict[str, Any] | None,
    versions: list[Any] | None,
    owner_team_id: uuid.UUID | None,
    visibility: str,
) -> LifecycleMap:
    """Create a map from an envelope, recreating the version chain when present.

    A v2 envelope with ``versions`` replays each snapshot through
    ``save_map_version`` so the chain is recreated deterministically: the
    exported numbers are preserved when they are contiguous 1..N and otherwise
    re-derived as 1..N. A single-version payload (v1, or v2 without ``versions``)
    imports as a version-1 map from ``content_json``.
    """
    if not versions:
        return await create_lifecycle_map(
            session,
            org_id=org_id,
            name=name,
            account_id=account_id,
            description=description,
            owner_team_id=owner_team_id,
            visibility=visibility,
            content_json=content_json,
        )

    ordered = _normalise_version_history(versions)
    first = ordered[0][2]
    lifecycle_map = await create_lifecycle_map(
        session,
        org_id=org_id,
        name=name,
        account_id=account_id,
        description=description,
        owner_team_id=owner_team_id,
        visibility=visibility,
        version=1,
        content_json=first,
    )
    for _, _, snapshot in ordered[1:]:
        updated = await save_map_version(
            session,
            lifecycle_map.id,
            stages=snapshot["stages"],
            edges=snapshot["edges"],
            notes=snapshot["notes"],
        )
        if updated is None:  # pragma: no cover — the map was just created
            raise LifecycleMapBundleError("version-history import failed mid-chain")
        lifecycle_map = updated
    return lifecycle_map


async def materialize_map_from_primitive(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    primitive: LibraryPrimitive,
    owner_team_id: uuid.UUID | None = None,
    visibility: str = "org",
) -> LifecycleMap:
    """Create a real lifecycle map in the org from a ``lifecycle_map`` primitive.

    Supports both primitives produced by import (``content_json.export`` holds
    the full envelope) and primitives whose ``content_json`` IS the map graph
    (stages/edges/notes).
    """
    content = primitive.content_json if isinstance(primitive.content_json, dict) else {}
    envelope = content.get("export")
    raw_versions: list[Any] | None = None
    if isinstance(envelope, dict):
        raw_name = envelope.get("name") or getattr(primitive, "name", None) or "Imported Lifecycle Map"
        description = envelope.get("description") or getattr(primitive, "description", None)
        raw_content = envelope.get("content_json")
        raw_versions = envelope.get("versions")
        if raw_versions is not None and not isinstance(raw_versions, list):
            raise LifecycleMapBundleError("Lifecycle map primitive export envelope 'versions' must be an array")
        if not isinstance(raw_content, dict) and not raw_versions:
            raise LifecycleMapBundleError("Lifecycle map primitive export envelope is missing 'content_json'")
    else:
        raw_name = getattr(primitive, "name", None) or "Lifecycle Map"
        description = getattr(primitive, "description", None)
        raw_content = content

    existing_names = await get_existing_lifecycle_map_names(session, org_id)
    map_name = suggest_import_name(existing_names, raw_name)

    lifecycle_map = await _create_map_via_envelope(
        session,
        org_id=org_id,
        account_id=account_id,
        name=map_name,
        description=description,
        content_json=raw_content,
        versions=raw_versions,
        owner_team_id=owner_team_id,
        visibility=visibility,
    )
    _log.info(
        "materialize_map_from_primitive: created map '%s' (id=%s) from primitive %s",
        map_name,
        lifecycle_map.id,
        primitive.id,
    )
    return lifecycle_map
