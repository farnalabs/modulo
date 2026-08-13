"""Lifecycle-map bundle export / import and library-primitive support.

FAR-174: lifecycle maps can be exported as a portable JSON envelope, imported
to create a new map in an organisation, and stored as ``lifecycle_map`` library
primitives so they can be listed and copied-to-adapt.

The envelope mirrors the PRD §8.31.9 primitive model (``primitive_type`` +
``content_json`` of stages/edges) and reuses ``normalize_content`` for content
validation, so an imported map is validated exactly like an editor save.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.lifecycle_map.service import create_lifecycle_map
from modulo.core.lifecycle_map.validation import normalize_content
from modulo.core.workflow_import_export import suggest_import_name
from modulo.db.crud.library_primitive import create_library_primitive
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.models.lifecycle_map import LifecycleMap

_log = logging.getLogger(__name__)

PRIMITIVE_TYPE = "lifecycle_map"
FORMAT_VERSION = "1"
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


def build_export_envelope(lifecycle_map: LifecycleMap) -> dict[str, Any]:
    """Build the portable JSON envelope for a map's active version.

    Returns the canonical PRD §8.31.9 primitive shape — the same envelope the
    import endpoint accepts, so an export can round-trip through import.
    """
    content = lifecycle_map.content_json if isinstance(lifecycle_map.content_json, dict) else {}
    return {
        "primitive_type": PRIMITIVE_TYPE,
        "format_version": FORMAT_VERSION,
        "name": lifecycle_map.name,
        "description": lifecycle_map.description,
        "content_json": normalize_content(content),
    }


def _slugify(name: str) -> str:
    """Produce a URL-safe slug from a lifecycle map name."""
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return slug or "lifecycle-map"


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
    """
    if envelope.get("primitive_type") != PRIMITIVE_TYPE:
        raise LifecycleMapBundleError(
            f"Unsupported primitive_type {envelope.get('primitive_type')!r}; expected '{PRIMITIVE_TYPE}'"
        )
    if envelope.get("format_version") != FORMAT_VERSION:
        raise LifecycleMapBundleError(
            f"Unsupported bundle format version {envelope.get('format_version')!r}; expected '{FORMAT_VERSION}'"
        )
    name = envelope.get("name")
    if not isinstance(name, str) or not name.strip():
        raise LifecycleMapBundleError("Lifecycle map export envelope is missing a non-empty 'name'")
    name = name.strip()
    description = envelope.get("description")
    if not isinstance(envelope.get("content_json"), dict):
        raise LifecycleMapBundleError("Lifecycle map export envelope is missing 'content_json'")

    existing_names = await get_existing_lifecycle_map_names(session, org_id)
    map_name = suggest_import_name(existing_names, name)

    lifecycle_map = await create_lifecycle_map(
        session,
        org_id=org_id,
        name=map_name,
        account_id=account_id,
        description=description,
        owner_team_id=owner_team_id,
        visibility=visibility,
        content_json=envelope["content_json"],
    )

    await create_library_primitive(
        session,
        org_id=org_id,
        source="local",
        primitive_type=PRIMITIVE_TYPE,
        name=map_name,
        slug=_slugify(map_name),
        description=description or "",
        author=account_id.hex[:8],
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
    if isinstance(envelope, dict):
        raw_name = envelope.get("name") or getattr(primitive, "name", None) or "Imported Lifecycle Map"
        description = envelope.get("description") or getattr(primitive, "description", None)
        raw_content = envelope.get("content_json")
        if not isinstance(raw_content, dict):
            raise LifecycleMapBundleError("Lifecycle map primitive export envelope is missing 'content_json'")
    else:
        raw_name = getattr(primitive, "name", None) or "Lifecycle Map"
        description = getattr(primitive, "description", None)
        raw_content = content

    existing_names = await get_existing_lifecycle_map_names(session, org_id)
    map_name = suggest_import_name(existing_names, raw_name)

    lifecycle_map = await create_lifecycle_map(
        session,
        org_id=org_id,
        name=map_name,
        account_id=account_id,
        description=description,
        owner_team_id=owner_team_id,
        visibility=visibility,
        content_json=raw_content,
    )
    _log.info(
        "materialize_map_from_primitive: created map '%s' (id=%s) from primitive %s",
        map_name,
        lifecycle_map.id,
        primitive.id,
    )
    return lifecycle_map
