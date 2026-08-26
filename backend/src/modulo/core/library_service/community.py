"""Community library browse + install helpers (FAR-363).

Reads the verified, cached manifest from ``LibrarySyncState`` (via the
``library_sync`` package) and installs registry primitives into an
organisation's ``library_primitives`` table.

Browse helpers are fail-open: no cached manifest yields an empty list / None.
Install raises ``ValueError`` for unknown, revoked, unsupported, or
undeliverable entries.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.library_sync import LibraryClient, get_cached_manifest, is_revoked
from modulo.core.library_sync.manifest import parse_manifest
from modulo.db.crud.library_primitive import create_library_primitive
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.rls import set_rls_org
from modulo.settings import get_settings

__all__ = [
    "get_community_entry",
    "install_community_entry",
    "list_community_entries",
]

logger = logging.getLogger(__name__)

# Values accepted by the ``ck_library_primitives_type`` CHECK constraint on
# ``library_primitives.primitive_type``.
_VALID_PRIMITIVE_TYPES = {
    "schema",
    "workflow",
    "agent",
    "integration",
    "test_fixture",
    "pipeline_template",
    "composite",
    "lifecycle_map",
}


async def _enter_org_context(session: AsyncSession, org_id: uuid.UUID) -> bool:
    """Set RLS org context on *session*, beginning a transaction when none is active.

    ``set_rls_org`` requires an active transaction (sessions use
    ``autobegin=False``), so when the caller has not opened one we begin it
    here. Returns True when this call began the transaction (the caller owns
    rollback on failure); False when reusing the caller's transaction.
    """
    if session.in_transaction():
        await set_rls_org(session, org_id)
        return False
    await session.begin()
    try:
        await set_rls_org(session, org_id)
    except BaseException:
        # Rollback undoes only the transaction this function opened via
        # session.begin() above; the caller-owned path returns False at L61 and
        # is never clobbered here.
        await session.rollback()  # nosemgrep: session-rollback-abuse
        raise
    return True


async def _installed_registry_keys(session: AsyncSession, org_id: uuid.UUID) -> set[tuple[str, str]]:
    """Return the set of (slug, version) registry rows already installed for *org_id*."""
    began = await _enter_org_context(session, org_id)
    try:
        result = await session.execute(
            select(LibraryPrimitive.slug, LibraryPrimitive.version).where(
                LibraryPrimitive.organisation_id == org_id,
                LibraryPrimitive.source == "registry",
                LibraryPrimitive.deleted_at.is_(None),
            )
        )
        keys: set[tuple[str, str]] = set()
        for row in result.all():
            slug = row[0]
            version = row[1]
            if isinstance(slug, str) and isinstance(version, str):
                keys.add((slug, version))
        return keys
    finally:
        if began:
            # Read-only query that opened the txn itself (began is True only for
            # a txn this function started); committing closes our own txn without
            # clobbering a concurrent caller's pending writes. session.rollback()
            # here would violate the no-bare-rollback architecture rule.
            await session.commit()


def _find_entry(entries: list[dict[str, Any]], entry_id: str) -> dict[str, Any] | None:
    for entry in entries:
        if str(entry.get("id")) == entry_id:
            return entry
    return None


async def _fetch_blob(sha256: str) -> dict[str, Any] | None:
    """Fetch and parse a manifest entry blob, verifying its content hash.

    Returns the parsed JSON object, or None when the fetch fails, the hash does
    not match, or the blob is not a valid JSON object.
    """
    settings = get_settings()
    client = LibraryClient(
        endpoint=settings.modulo_library_endpoint,
        root_public_key_pem=settings.modulo_library_root_public_key,
        timeout_seconds=settings.modulo_library_sync_timeout_seconds,
    )
    try:
        blob = await client.fetch_blob(sha256)
        if blob is None:
            return None
        if hashlib.sha256(blob).hexdigest() != sha256.lower():
            logger.warning("community.install.blob_hash_mismatch")
            return None
        content: Any = json.loads(blob.decode("utf-8"))
    except ValueError:
        logger.warning("community.install.blob_not_json")
        return None
    finally:
        await client.close()
    if not isinstance(content, dict):
        logger.warning("community.install.blob_not_object")
        return None
    return content


async def list_community_entries(session: AsyncSession, org_id: uuid.UUID) -> list[dict[str, Any]]:
    """List synced community entries, each augmented with ``installed``.

    ``installed`` is True when the org already has a ``source="registry"`` row
    with the same slug+version. Fail-open: returns ``[]`` when no cached
    manifest is available.
    """
    manifest = await get_cached_manifest(session)
    if not manifest:
        return []
    data = parse_manifest(manifest)
    installed = await _installed_registry_keys(session, org_id)
    revoked_ids = {str(item.get("id")) for item in data.revoked if isinstance(item.get("id"), str)}
    entries: list[dict[str, Any]] = []
    for entry in data.entries:
        if str(entry.get("id")) in revoked_ids:
            continue
        item = dict(entry)
        item["installed"] = (entry.get("slug"), entry.get("version")) in installed
        entries.append(item)
    return entries


async def get_community_entry(session: AsyncSession, entry_id: str) -> dict[str, Any] | None:
    """Return a single community entry by id, or None.

    Returns None for a revoked entry and when no cached manifest exists.
    """
    manifest = await get_cached_manifest(session)
    if not manifest:
        return None
    if await is_revoked(session, entry_id):
        return None
    data = parse_manifest(manifest)
    return _find_entry(data.entries, entry_id)


async def install_community_entry(
    session: AsyncSession,
    org_id: uuid.UUID,
    entry_id: str,
    *,
    target_team_id: uuid.UUID | None = None,
    created_by: uuid.UUID | None = None,
) -> LibraryPrimitive:
    """Install a community entry as a ``source="registry"`` library primitive.

    Raises ValueError("entry not found") for unknown or revoked entries,
    ValueError("blob fetch failed") when the blob cannot be fetched, fails its
    content hash, or is not valid JSON, ValueError("already installed")
    when the org already has a row for this slug+version, and
    ValueError("registry entries are org-owned") when ``target_team_id`` is
    provided (registry rows must keep ``owner_team_id`` NULL). Flushes but
    does not commit — the caller owns the commit.
    """
    manifest = await get_cached_manifest(session)
    if not manifest:
        raise ValueError("entry not found")
    data = parse_manifest(manifest)
    entry = _find_entry(data.entries, entry_id)
    if entry is None or await is_revoked(session, entry_id):
        raise ValueError("entry not found")

    if target_team_id is not None:
        raise ValueError("registry entries are org-owned")

    primitive_type = entry.get("type")
    if primitive_type not in _VALID_PRIMITIVE_TYPES:
        raise ValueError(f"unsupported primitive type: {primitive_type}")

    content_sha256 = entry.get("content_sha256")
    slug = entry.get("slug")
    version = entry.get("version")
    author = entry.get("author")
    if (
        not isinstance(content_sha256, str)
        or not content_sha256
        or not isinstance(slug, str)
        or not isinstance(version, str)
        or not isinstance(author, str)
    ):
        raise ValueError("entry not found")

    began = await _enter_org_context(session, org_id)
    try:
        existing = await session.execute(
            select(LibraryPrimitive.id).where(
                LibraryPrimitive.organisation_id == org_id,
                LibraryPrimitive.source == "registry",
                LibraryPrimitive.slug == slug,
                LibraryPrimitive.version == version,
                LibraryPrimitive.deleted_at.is_(None),
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise ValueError("already installed")

        content = await _fetch_blob(content_sha256)
        if content is None:
            raise ValueError("blob fetch failed")

        name = slug
        raw_description = content.get("description")
        description = raw_description if isinstance(raw_description, str) else None
        settings = get_settings()
        source_url = f"{settings.modulo_library_endpoint}/v1/entries/{entry_id}"

        primitive = await create_library_primitive(
            session,
            org_id=org_id,
            source="registry",
            primitive_type=primitive_type,
            name=name,
            slug=slug,
            description=description,
            author=author,
            version=version,
            tags=[],
            content_json=content,
            source_url=source_url,
            forked_from=None,
            checksum=content_sha256,
            ed25519_signature=None,
            verified=True,
            download_count=0,
            average_rating=None,
            review_count=None,
            owner_team_id=target_team_id,
            visibility="org",
            account_id=created_by,
            auto_update=False,
            tier="native",
        )
        await session.flush()
    except BaseException:
        if began:
            # Error path for a txn this function opened (began True only when
            # _enter_org_context started it); the rollback undoes our own
            # uncommitted primitive, never a concurrent caller's pending writes.
            await session.rollback()  # nosemgrep: session-rollback-abuse
        raise
    return primitive
