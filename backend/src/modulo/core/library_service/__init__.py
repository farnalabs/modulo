"""Library service — CRUD and community primitives for library_primitives."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.library_service._seed_data import (
    _COMMUNITY_BY_ID,
    _COMMUNITY_BY_SLUG,
    _COMMUNITY_PRIMITIVES,
    _MODULO_BY_ID,
    _MODULO_BY_SLUG,
    _MODULO_PRIMITIVES,
    CONTRIBUTION_DRAFT,
    CONTRIBUTION_PUBLISHED,
    CONTRIBUTION_REVIEW_QUEUE,
    MODULO_ORG_ID,
)
from modulo.core.library_service.community import (
    get_community_entry,
    install_community_entry,
    list_community_entries,
)
from modulo.db.crud.base import PageResult
from modulo.db.crud.library_primitive import (
    create_library_primitive,
    get_library_primitive,
    list_library_primitives,
    list_primitives_by_version_group,
    update_library_primitive,
)
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.util import sanitise_log_value as _sanitise_log_value

logger = logging.getLogger(__name__)

# Repeated log-name contract (S1192).
_LOG_COMPONENT = "core.library_service"


__all__ = [
    "CONTRIBUTION_DRAFT",
    "CONTRIBUTION_PUBLISHED",
    "CONTRIBUTION_REVIEW_QUEUE",
    "MODULO_ORG_ID",
    "CommunityPrimitiveReadOnlyError",
    "ContributionInvalidTransitionError",
    "ContributionNotFoundError",
    "contribute_fixture",
    "contribute_primitive",
    "copy_to_adapt",
    "get_community_entry",
    "get_primitive",
    "get_primitive_by_slug",
    "install_community_entry",
    "list_community_entries",
    "list_contribution_versions",
    "list_contributions",
    "list_org_contributions",
    "list_primitives",
    "notify_importers_of_update",
    "publish_contribution",
    "submit_contribution_for_review",
    "submit_contribution_version",
]


class CommunityPrimitiveReadOnlyError(Exception):
    """Raised when a modulo/community primitive is adapted via MCP — browser UI only."""


class ContributionNotFoundError(LookupError):
    """Raised when a contribution primitive is not found."""


class ContributionInvalidTransitionError(ValueError):
    """Raised when an invalid contribution status transition is attempted."""


# Guards the in-memory community cache appended to by publish_contribution.
_COMMUNITY_CACHE_LOCK: asyncio.Lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bump_version(version: str) -> str:
    """Increment the last segment of a version string."""
    if not version:
        logger.warning("_bump_version called with empty string, defaulting to 1.0")
        return "1.0"
    parts = version.split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
    except ValueError:
        logger.warning("_bump_version: non-numeric last segment in '%s', defaulting to 1.0", version)
        parts = ["1", "0"]
    return ".".join(parts)


def _parse_version_key(version: str | None) -> tuple[int, ...]:
    """Parse a dotted version string into a sortable tuple of ints."""
    if not version:
        return (0,)
    try:
        return tuple(int(x) for x in version.split("."))
    except ValueError:
        return (0,)


def _resolve_primitive_types(
    primitive_type: str | None,
    primitive_types: list[str] | None,
) -> list[str] | None:
    """Merge single and plural type filters into one set, or None when unfiltered."""
    if primitive_types:
        return primitive_types
    if primitive_type is not None:
        return [primitive_type]
    return None


def _filter_primitives(
    primitives: list[LibraryPrimitive],
    *,
    primitive_type: str | None = None,
    primitive_types: list[str] | None = None,
    search: str | None = None,
) -> list[LibraryPrimitive]:
    if not primitives:
        return []
    allowed_types = _resolve_primitive_types(primitive_type, primitive_types)
    if allowed_types is not None:
        allowed = set(allowed_types)
        primitives = [p for p in primitives if p.primitive_type in allowed]
    if search:
        term = search.strip().lower()
        if term:
            primitives = [
                p for p in primitives if term in (p.name or "").lower() or term in (p.description or "").lower()
            ]
    return primitives


def _filter_modulo(
    *,
    primitive_type: str | None = None,
    primitive_types: list[str] | None = None,
    search: str | None = None,
) -> list[LibraryPrimitive]:
    return _filter_primitives(
        _MODULO_PRIMITIVES,
        primitive_type=primitive_type,
        primitive_types=primitive_types,
        search=search,
    )


def _filter_community(
    *,
    primitive_type: str | None = None,
    primitive_types: list[str] | None = None,
    search: str | None = None,
) -> list[LibraryPrimitive]:
    return _filter_primitives(
        _COMMUNITY_PRIMITIVES,
        primitive_type=primitive_type,
        primitive_types=primitive_types,
        search=search,
    )


async def _fetch_published_community_from_db(
    session: AsyncSession,
    _org_id: uuid.UUID,
    *,
    primitive_type: str | None = None,
    primitive_types: list[str] | None = None,
    search: str | None = None,
) -> list[LibraryPrimitive]:
    """Fetch published community items from the database.

    Queries for items with contribution_status='published' and visibility='community'.
    Returns an empty list on DB errors (missing tables, RLS restrictions).
    Published items are also cached in the in-memory community list at publish time,
    so this query is a best-effort supplement that handles warm-start scenarios.
    """
    try:
        saved_tenant = session.info.pop("org_id", None)
        try:
            stmt = (
                select(LibraryPrimitive)
                .where(LibraryPrimitive.contribution_status == CONTRIBUTION_PUBLISHED)
                .where(LibraryPrimitive.visibility == "community")
                .order_by(LibraryPrimitive.created_at.desc())
            )
            allowed_types = _resolve_primitive_types(primitive_type, primitive_types)
            if allowed_types is not None:
                stmt = stmt.where(LibraryPrimitive.primitive_type.in_(allowed_types))
            if search:
                term = f"%{search.strip()}%"
                stmt = stmt.where(LibraryPrimitive.name.ilike(term))
            result = await session.execute(stmt)
            return list(result.scalars())
        finally:
            if saved_tenant is not None:
                session.info["org_id"] = saved_tenant
    except ProgrammingError:
        logger.warning("_fetch_published_community_from_db: ProgrammingError — missing DB table or migration")
        return []
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("_fetch_published_community_from_db: unexpected error")
        return []


async def _with_org_txn[T](
    session: AsyncSession,
    org_id: uuid.UUID,
    fn: Callable[[AsyncSession], Awaitable[T]],
) -> T:
    """Run a write body inside a single RLS-scoped transaction.

    Reproduces the shared error contract of the contribution functions: a
    ProgrammingError (missing table/migration) is logged and re-raised.
    """
    try:
        async with session.begin():
            await set_rls_org(session, org_id)
            return await fn(session)
    except ProgrammingError:
        logger.exception(_LOG_COMPONENT)
        raise


# ---------------------------------------------------------------------------
# Public API — lookup
# ---------------------------------------------------------------------------


async def _scoped_execute[T](
    session: AsyncSession,
    org_id: uuid.UUID,
    query: Callable[[AsyncSession], Awaitable[T]],
) -> T:
    """Run a read query inside an RLS-scoped transaction.

    Reuses the caller's transaction when already active (``set_rls_org`` sets a
    ``SET LOCAL`` context bound to the current transaction), otherwise opens one.
    """
    if session.in_transaction():
        await set_rls_org(session, org_id)
        return await query(session)
    async with session.begin():
        await set_rls_org(session, org_id)
        return await query(session)


async def get_primitive(
    session: AsyncSession,
    org_id: uuid.UUID,
    primitive_id: uuid.UUID,
) -> LibraryPrimitive | None:
    """Return a primitive visible to org_id, or None.

    Checks the org-scoped DB first, then falls back to in-memory modulo primitives.
    Supports being called within an existing transaction or starting its own.
    """
    try:
        item = await _scoped_execute(session, org_id, lambda s: get_library_primitive(s, primitive_id))
    except ProgrammingError:
        logger.warning("get_primitive — DB not migrated or table missing for %s", primitive_id)
        return None
    except SQLAlchemyError:
        logger.exception("get_primitive — DB error for %s", primitive_id)
        raise
    if item is not None:
        return item
    return _MODULO_BY_ID.get(primitive_id) or _COMMUNITY_BY_ID.get(primitive_id)


async def get_primitive_by_slug(
    session: AsyncSession,
    org_id: uuid.UUID,
    primitive_type: str,
    slug: str,
) -> LibraryPrimitive | None:
    """Return a primitive visible to org_id by type and slug, or None.

    Checks the org-scoped DB first, then falls back to in-memory modulo primitives.
    Supports being called within an existing transaction or starting its own.
    """

    async def _lookup(s: AsyncSession) -> LibraryPrimitive | None:
        stmt = select(LibraryPrimitive).where(
            LibraryPrimitive.primitive_type == primitive_type,
            LibraryPrimitive.slug == slug,
        )
        result = await s.execute(stmt)
        return result.scalar_one_or_none()

    try:
        item = await _scoped_execute(session, org_id, _lookup)
    except ProgrammingError:
        logger.warning(
            "get_primitive_by_slug — DB not migrated for %s/%s",
            _sanitise_log_value(primitive_type),
            _sanitise_log_value(slug),
        )
        return None
    except SQLAlchemyError:
        logger.exception(
            "get_primitive_by_slug — DB error for %s/%s",
            _sanitise_log_value(primitive_type),
            _sanitise_log_value(slug),
        )
        raise
    if item is not None:
        return item
    return _MODULO_BY_SLUG.get((primitive_type, slug)) or _COMMUNITY_BY_SLUG.get((primitive_type, slug))


# ---------------------------------------------------------------------------
# Public API — copy to adapt
# ---------------------------------------------------------------------------


async def _re_read_primitive(
    session: AsyncSession,
    org_id: uuid.UUID,
    primitive_id: uuid.UUID,
) -> LibraryPrimitive:
    """Re-read the source inside the copy transaction to avoid TOCTOU.

    Falls back to the in-memory cache for modulo/community primitives.
    """
    refreshed = await get_library_primitive(session, primitive_id)
    if refreshed is None:
        refreshed = _MODULO_BY_ID.get(primitive_id) or _COMMUNITY_BY_ID.get(primitive_id)
    if refreshed is None:
        raise LookupError(f"Primitive {primitive_id} not found for org {org_id} during copy")
    return refreshed


async def _increment_registry_downloads(session: AsyncSession, refreshed: LibraryPrimitive) -> None:
    """Atomically increment the download count on registry primitives."""
    if refreshed.source == "registry":
        await session.execute(
            sa_update(LibraryPrimitive)
            .where(LibraryPrimitive.id == refreshed.id)
            .values(download_count=func.coalesce(LibraryPrimitive.download_count, 0) + 1)
        )


def _build_copy_args(
    refreshed: LibraryPrimitive,
    org_id: uuid.UUID,
    new_version: str,
    target_team_id: uuid.UUID | None,
    created_by: uuid.UUID | None,
) -> dict[str, Any]:
    """Build the create_library_primitive kwargs for an adapted org copy."""
    return {
        "org_id": org_id,
        "source": "local",
        "primitive_type": refreshed.primitive_type,
        "name": refreshed.name,
        "slug": f"{refreshed.slug}-copy",
        "description": refreshed.description,
        "author": refreshed.author,
        "version": new_version,
        "tags": list(refreshed.tags or []),
        "content_json": dict(refreshed.content_json) if refreshed.content_json is not None else {},
        "source_url": None,
        "forked_from": refreshed.id,
        "checksum": None,
        "ed25519_signature": None,
        "verified": None,
        "download_count": None,
        "average_rating": None,
        "review_count": None,
        "owner_team_id": target_team_id,
        "visibility": "org",
        "account_id": created_by,
        "auto_update": True,
        "tier": refreshed.tier,
    }


async def copy_to_adapt(
    session: AsyncSession,
    org_id: uuid.UUID,
    primitive_id: uuid.UUID,
    *,
    target_team_id: uuid.UUID | None = None,
    created_by: uuid.UUID | None = None,
    org_role: str = "admin",
    via_mcp: bool = False,
) -> LibraryPrimitive:
    """Clone a primitive into the org workspace.

    Raises CommunityPrimitiveReadOnlyError if via_mcp=True and the source is community.
    Raises LookupError if the primitive does not exist.
    """
    source = await get_primitive(session, org_id, primitive_id)
    if source is None:
        raise LookupError(f"Primitive {primitive_id} not found for org {org_id}")

    if via_mcp and source.visibility == "community":
        raise CommunityPrimitiveReadOnlyError(
            "Community primitives may only be adapted via the browser UI, not via MCP."
        )

    async def _do(s: AsyncSession) -> LibraryPrimitive:
        if created_by is not None:
            await set_rls_user_context(s, created_by, org_role)
        refreshed = await _re_read_primitive(s, org_id, primitive_id)
        new_version = _bump_version(refreshed.version)
        await _increment_registry_downloads(s, refreshed)
        return await create_library_primitive(
            s, **_build_copy_args(refreshed, org_id, new_version, target_team_id, created_by)
        )

    return await _with_org_txn(session, org_id, _do)


# ---------------------------------------------------------------------------
# Contribution flow
# ---------------------------------------------------------------------------


async def _create_draft_contribution(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    created_by: uuid.UUID,
    primitive_type: str,
    name: str,
    slug: str,
    description: str | None,
    tags: list[str],
    content_json: dict[str, Any],
    source_url: str | None,
    owner_team_id: uuid.UUID | None,
) -> LibraryPrimitive:
    """Create a primitive and immediately set it to draft contribution status."""

    async def _do(s: AsyncSession) -> LibraryPrimitive:
        prim = await create_library_primitive(
            s,
            org_id=org_id,
            source="local",
            primitive_type=primitive_type,
            name=name,
            slug=slug,
            description=description,
            author=created_by.hex,
            version="1.0",
            tags=tags,
            content_json=content_json,
            source_url=source_url,
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
        updated = await update_library_primitive(s, prim.id, {"contribution_status": CONTRIBUTION_DRAFT})
        if updated is None:
            raise ContributionNotFoundError(f"Contribution {prim.id} not found after creation")
        return updated

    return await _with_org_txn(session, org_id, _do)


async def contribute_fixture(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    created_by: uuid.UUID,
    name: str,
    slug: str,
    description: str | None,
    tags: list[str],
    fixture_map: dict[str, str],
    source_run_id: uuid.UUID | None = None,
    source_pipeline_id: uuid.UUID | None = None,
    owner_team_id: uuid.UUID | None = None,
) -> LibraryPrimitive:
    """Create a draft fixture contribution in the org's library.

    The fixture is stored as a test_fixture primitive with contribution_status='draft'.
    It is visible only to the submitting org until published to the community library.
    """
    content: dict[str, Any] = {
        "fixture_map": fixture_map,
        "source_run_id": str(source_run_id) if source_run_id else None,
        "source_pipeline_id": str(source_pipeline_id) if source_pipeline_id else None,
    }

    return await _create_draft_contribution(
        session,
        org_id=org_id,
        created_by=created_by,
        primitive_type="test_fixture",
        name=name,
        slug=slug,
        description=description,
        tags=tags,
        content_json=content,
        source_url=None,
        owner_team_id=owner_team_id,
    )


async def contribute_primitive(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    created_by: uuid.UUID,
    primitive_type: str,
    name: str,
    slug: str,
    description: str | None,
    tags: list[str],
    content_json: dict[str, Any],
    source_url: str | None = None,
    owner_team_id: uuid.UUID | None = None,
) -> LibraryPrimitive:
    """Create a contribution for the community library.

    Stores the primitive with source='local' and contribution_status='draft'.
    An admin can review and publish it to the community library.
    """
    return await _create_draft_contribution(
        session,
        org_id=org_id,
        created_by=created_by,
        primitive_type=primitive_type,
        name=name,
        slug=slug,
        description=description,
        tags=tags,
        content_json=content_json,
        source_url=source_url,
        owner_team_id=owner_team_id,
    )


async def submit_contribution_for_review(
    session: AsyncSession,
    org_id: uuid.UUID,
    primitive_id: uuid.UUID,
    *,
    _created_by: uuid.UUID,
) -> LibraryPrimitive:
    """Move a draft fixture contribution to the review queue.

    Raises ContributionNotFoundError if the primitive does not exist.
    Raises ContributionInvalidTransitionError if the primitive is not in draft status.
    """

    async def _do(s: AsyncSession) -> LibraryPrimitive:
        prim = await get_library_primitive(s, primitive_id)

        if prim is None:
            raise ContributionNotFoundError(f"Contribution {primitive_id} not found")

        if prim.contribution_status != CONTRIBUTION_DRAFT:
            raise ContributionInvalidTransitionError(
                f"Cannot submit contribution {primitive_id} for review: "
                f"expected status '{CONTRIBUTION_DRAFT}', got '{prim.contribution_status}'"
            )

        updated = await update_library_primitive(
            s,
            primitive_id,
            {"contribution_status": CONTRIBUTION_REVIEW_QUEUE},
        )
        if updated is None:
            raise ContributionNotFoundError(f"Contribution {primitive_id} not found")
        return updated

    return await _with_org_txn(session, org_id, _do)


async def publish_contribution(
    session: AsyncSession,
    org_id: uuid.UUID,
    primitive_id: uuid.UUID,
) -> LibraryPrimitive:
    """Publish a reviewed fixture contribution to the community library.

    Changes visibility to 'community' and sets contribution_status to 'published'.
    The primitive is reassigned to the community sentinel org so it appears
    for all users. Accepts contributions in either 'draft' or 'review_queue' status.
    """

    async def _do(s: AsyncSession) -> LibraryPrimitive:
        prim = await get_library_primitive(s, primitive_id)

        if prim is None:
            raise ContributionNotFoundError(f"Contribution {primitive_id} not found")

        if prim.contribution_status not in (CONTRIBUTION_DRAFT, CONTRIBUTION_REVIEW_QUEUE):
            raise ContributionInvalidTransitionError(
                f"Cannot publish contribution {primitive_id}: "
                f"expected status '{CONTRIBUTION_DRAFT}' or '{CONTRIBUTION_REVIEW_QUEUE}', "
                f"got '{prim.contribution_status}'"
            )

        updated = await update_library_primitive(
            s,
            primitive_id,
            {
                "contribution_status": CONTRIBUTION_PUBLISHED,
                "visibility": "community",
                "organisation_id": MODULO_ORG_ID,
            },
        )
        if updated is None:
            raise ContributionNotFoundError(f"Contribution {primitive_id} not found")
        return updated

    updated = await _with_org_txn(session, org_id, _do)

    # Add to in-memory community cache so it appears in community listings immediately.
    async with _COMMUNITY_CACHE_LOCK:
        if updated.id not in _COMMUNITY_BY_ID:
            _COMMUNITY_PRIMITIVES.append(updated)
            _COMMUNITY_BY_ID[updated.id] = updated
            _COMMUNITY_BY_SLUG[(updated.primitive_type, updated.slug)] = updated

    # Notify importers using MODULO_ORG_ID since the primitive now belongs to the sentinel org
    await notify_importers_of_update(session, MODULO_ORG_ID, primitive_id)

    return updated


def _apply_contribution_status_filter(
    result: PageResult[LibraryPrimitive],
    contribution_status: str | None,
) -> PageResult[LibraryPrimitive]:
    if contribution_status is not None:
        result.items = [p for p in result.items if p.contribution_status == contribution_status]
        result.total = len(result.items)
    return result


async def list_contributions(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    contribution_status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> PageResult[LibraryPrimitive]:
    """List fixture contributions scoped to the org."""
    result = await _with_org_txn(
        session,
        org_id,
        lambda s: list_library_primitives(s, page=page, page_size=page_size, primitive_type="test_fixture"),
    )
    return _apply_contribution_status_filter(result, contribution_status)


async def list_org_contributions(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    contribution_status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> PageResult[LibraryPrimitive]:
    """List contributions submitted by the org, optionally filtered by status."""
    result = await _with_org_txn(
        session,
        org_id,
        lambda s: list_library_primitives(s, page=page, page_size=page_size),
    )
    return _apply_contribution_status_filter(result, contribution_status)


# ---------------------------------------------------------------------------
# Contribution versioning
# ---------------------------------------------------------------------------


async def _resolve_version_group(
    s: AsyncSession,
    existing: LibraryPrimitive,
    primitive_id: uuid.UUID,
) -> uuid.UUID:
    """Return the primitive's version group, seeding it on the seed row if absent."""
    group_id = existing.version_group_id or existing.id
    if existing.version_group_id is None:
        seed_update = await update_library_primitive(
            s,
            primitive_id,
            {"version_group_id": group_id},
        )
        if seed_update is None:
            raise ContributionNotFoundError(f"Contribution {primitive_id} not found for version group seeding")
    return group_id


async def submit_contribution_version(
    session: AsyncSession,
    org_id: uuid.UUID,
    primitive_id: uuid.UUID,
    *,
    created_by: uuid.UUID,
    name: str,
    slug: str,
    description: str | None,
    tags: list[str],
    fixture_map: dict[str, str],
    source_run_id: uuid.UUID | None = None,
    source_pipeline_id: uuid.UUID | None = None,
    owner_team_id: uuid.UUID | None = None,
) -> LibraryPrimitive:
    """Submit a new version of an existing published fixture contribution.

    Auto-increments the minor version and creates a new draft row linked
    via version_group_id.  The new version must go through
    review_queue -> published independently.
    """
    content: dict[str, Any] = {
        "fixture_map": fixture_map,
        "source_run_id": str(source_run_id) if source_run_id else None,
        "source_pipeline_id": str(source_pipeline_id) if source_pipeline_id else None,
    }

    async def _do(s: AsyncSession) -> LibraryPrimitive:
        existing = await get_library_primitive(s, primitive_id)

        if existing is None:
            raise ContributionNotFoundError(f"Contribution {primitive_id} not found")

        if existing.contribution_status != CONTRIBUTION_PUBLISHED:
            raise ContributionInvalidTransitionError(
                f"Cannot version contribution {primitive_id}: "
                f"expected status '{CONTRIBUTION_PUBLISHED}', got '{existing.contribution_status}'"
            )

        new_version = _bump_version(existing.version)
        group_id = await _resolve_version_group(s, existing, primitive_id)

        prim = await create_library_primitive(
            s,
            org_id=org_id,
            source="local",
            primitive_type="test_fixture",
            name=name,
            slug=slug,
            description=description,
            author=created_by.hex,
            version=new_version,
            tags=tags,
            content_json=content,
            source_url=None,
            forked_from=primitive_id,
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
        updated = await update_library_primitive(
            s,
            prim.id,
            {
                "contribution_status": CONTRIBUTION_DRAFT,
                "version_group_id": group_id,
            },
        )
        if updated is None:
            raise ContributionNotFoundError(f"Contribution version {prim.id} not found after creation")
        return updated

    return await _with_org_txn(session, org_id, _do)


async def list_contribution_versions(
    session: AsyncSession,
    org_id: uuid.UUID,
    primitive_id: uuid.UUID,
) -> list[LibraryPrimitive]:
    """Return all versions for a contribution primitive, newest first."""

    async def _do(s: AsyncSession) -> tuple[list[LibraryPrimitive], LibraryPrimitive]:
        prim = await get_library_primitive(s, primitive_id)

        if prim is None:
            raise ContributionNotFoundError(f"Contribution {primitive_id} not found")

        if prim.version_group_id is None:
            return [prim], prim

        results = await list_primitives_by_version_group(s, prim.version_group_id)
        # Include the seed primitive (the one whose version_group_id was set to
        # its own id) — it won't appear in the version-group query because it
        # may not yet have the version_group_id set if it predates the feature.
        return results, prim

    results, prim = await _with_org_txn(session, org_id, _do)

    if not any(r.id == prim.id for r in results):
        results.append(prim)

    return sorted(results, key=lambda p: _parse_version_key(p.version), reverse=True)


async def _mark_fork_copies(s: AsyncSession, prim: LibraryPrimitive) -> None:
    """Back-propagate the new version id to every auto-update fork copy."""
    group_id = prim.version_group_id
    if group_id is None:
        return

    stmt = select(LibraryPrimitive).where(
        LibraryPrimitive.forked_from.in_(
            select(LibraryPrimitive.id).where(LibraryPrimitive.version_group_id == group_id)
        )
    )
    result = await s.execute(stmt)

    for copy in list(result.scalars()):
        if not copy.auto_update:
            continue
        try:
            await update_library_primitive(
                s,
                copy.id,
                {"update_available_version_id": prim.id},
            )
        except SQLAlchemyError:
            logger.exception("notify_importers_of_update: failed to update copy %s", copy.id)


async def notify_importers_of_update(
    session: AsyncSession,
    org_id: uuid.UUID,
    primitive_id: uuid.UUID,
) -> None:
    """Mark library entries that forked from this primitive as having an update.

    Finds all primitives that were copied (``forked_from``) from any version
    in the same version group and sets their ``update_available_version_id``
    to the newly published version.
    """
    try:
        async with session.begin():
            await set_rls_org(session, org_id)
            prim = await get_library_primitive(session, primitive_id)

            if prim is None:
                return

            await _mark_fork_copies(session, prim)
    except ProgrammingError:
        logger.warning("notify_importers_of_update failed (DB not migrated): %s", primitive_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("notify_importers_of_update: unexpected error for primitive %s", primitive_id)


# ---------------------------------------------------------------------------
# Public API — list primitives
# ---------------------------------------------------------------------------


async def _load_org_page(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    primitive_type: str | None,
    primitive_types: list[str] | None,
    search: str | None,
    page: int,
    page_size: int,
    include_community: bool,
    source: str | None,
    cursor: str | None,
    excluded_tiers: list[str] | None,
) -> tuple[PageResult[LibraryPrimitive], list[LibraryPrimitive]]:
    """Fetch the org page and published DB community rows, degrading gracefully."""
    org_page: PageResult[LibraryPrimitive] = PageResult(items=[], total=0, page=page, page_size=page_size)
    db_community: list[LibraryPrimitive] = []
    try:
        async with session.begin():
            await set_rls_org(session, org_id)
            org_page = await list_library_primitives(
                session,
                org_id=org_id,
                page=page,
                page_size=page_size,
                primitive_type=primitive_type,
                primitive_types=primitive_types,
                search=search,
                cursor=cursor,
                excluded_tiers=excluded_tiers,
            )
            if include_community and (source is None or source == "community"):
                db_community = await _fetch_published_community_from_db(
                    session,
                    org_id,
                    primitive_type=primitive_type,
                    primitive_types=primitive_types,
                    search=search,
                )
    except ProgrammingError:
        logger.warning("list_primitives — DB not migrated for org %s", org_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("list_primitives — DB query failed for org %s", org_id)
    return org_page, db_community


def _filter_org_by_source(
    org_page: PageResult[LibraryPrimitive],
    source: str | None,
) -> tuple[list[LibraryPrimitive], int]:
    """Narrow the org items (and total) to the requested source when given."""
    org_items = list(org_page.items)
    org_total = org_page.total
    if source is not None:
        org_items = [p for p in org_items if p.source == source]
        org_total = len(org_items)
    return org_items, org_total


def _gather_in_memory_sources(
    *,
    primitive_type: str | None,
    primitive_types: list[str] | None,
    search: str | None,
    source: str | None,
    include_community: bool,
    db_community: list[LibraryPrimitive],
) -> tuple[list[LibraryPrimitive], list[LibraryPrimitive]]:
    """Merge in-memory modulo/community items with DB community rows (deduped by id)."""
    modulo: list[LibraryPrimitive] = []
    community: list[LibraryPrimitive] = []
    if include_community:
        if source is None or source == "modulo":
            modulo = _filter_modulo(
                primitive_type=primitive_type,
                primitive_types=primitive_types,
                search=search,
            )
        if source is None or source == "community":
            community = _filter_community(
                primitive_type=primitive_type,
                primitive_types=primitive_types,
                search=search,
            )
            seen_ids = {p.id for p in community}
            for p in db_community:
                if p.id not in seen_ids:
                    community.append(p)
                    seen_ids.add(p.id)
    return modulo, community


def _exclude_tiers(
    org_items: list[LibraryPrimitive],
    org_total: int,
    modulo: list[LibraryPrimitive],
    community: list[LibraryPrimitive],
    excluded_tiers: list[str] | None,
) -> tuple[list[LibraryPrimitive], int, list[LibraryPrimitive], list[LibraryPrimitive]]:
    if excluded_tiers:
        org_items = [p for p in org_items if p.tier not in excluded_tiers]
        org_total = len(org_items)
        modulo = [p for p in modulo if p.tier not in excluded_tiers]
        community = [p for p in community if p.tier not in excluded_tiers]
    return org_items, org_total, modulo, community


async def list_primitives(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    primitive_type: str | None = None,
    primitive_types: list[str] | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 20,
    include_community: bool = True,
    source: str | None = None,
    cursor: str | None = None,
    excluded_tiers: list[str] | None = None,
) -> PageResult[LibraryPrimitive]:
    """Return org-scoped, Native library, and community-database primitives merged into a single page.

    ``source`` (when given) restricts the result to exactly that source
    value — e.g. ``source="community"`` returns only community-database
    example pipelines, ``source="modulo"`` returns only Native library
    built-ins, ``source="local"`` returns only the org's own saved
    primitives. When omitted, all sources are merged (existing default
    behaviour, unchanged for backwards compatibility).

    ``primitive_types`` (when given) restricts the result to any of the
    listed primitive types, e.g. ``["workflow", "agent"]``. It takes
    precedence over the single-value ``primitive_type`` filter.
    """
    if excluded_tiers is None:
        excluded_tiers = ["in_dev"]
    org_page, db_community = await _load_org_page(
        session,
        org_id,
        primitive_type=primitive_type,
        primitive_types=primitive_types,
        search=search,
        page=page,
        page_size=page_size,
        include_community=include_community,
        source=source,
        cursor=cursor,
        excluded_tiers=excluded_tiers,
    )

    org_items, org_total = _filter_org_by_source(org_page, source)
    modulo, community = _gather_in_memory_sources(
        primitive_type=primitive_type,
        primitive_types=primitive_types,
        search=search,
        source=source,
        include_community=include_community,
        db_community=db_community,
    )
    org_items, org_total, modulo, community = _exclude_tiers(org_items, org_total, modulo, community, excluded_tiers)

    all_items: list[LibraryPrimitive] = org_items + modulo + community
    return PageResult(
        items=all_items,
        total=org_total + len(modulo) + len(community),
        page=page,
        page_size=page_size,
        next_cursor=org_page.next_cursor,
        has_more=org_page.has_more,
    )
