"""Org-scoped CRUD for LibraryPrimitive.

All functions require RLS org context to be set by the caller.
"""

import logging
import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from modulo.db.crud.base import PageResult, apply_updates
from modulo.db.crud.pagination import CursorPaginator
from modulo.db.models.library_primitive import LibraryPrimitive

logger = logging.getLogger(__name__)


async def copy_to_adapt(
    session: AsyncSession,
    *,
    primitive_id: uuid.UUID,
    target_org_id: uuid.UUID,
    target_team_id: uuid.UUID | None,
    account_id: uuid.UUID | None,
) -> LibraryPrimitive | None:
    source = await get_library_primitive(session, primitive_id)
    if source is None:
        return None

    version_parts = source.version.split(".")
    try:
        minor = int(version_parts[-1]) + 1
    except (ValueError, IndexError):
        minor = 1
    version_parts[-1] = str(minor)
    new_version = ".".join(version_parts) if len(version_parts) > 1 else f"{minor}.0"

    copied = LibraryPrimitive(
        organisation_id=target_org_id,
        source="local",
        primitive_type=source.primitive_type,
        name=source.name,
        slug=f"{source.slug}-copy",
        description=source.description,
        author=source.author,
        version=new_version,
        tags=list(source.tags) if source.tags else [],
        content_json=dict(source.content_json),
        source_url=None,
        forked_from=primitive_id,
        checksum=None,
        ed25519_signature=None,
        verified=None,
        download_count=None,
        average_rating=None,
        review_count=None,
        owner_team_id=target_team_id,
        visibility="org",
        account_id=account_id,
        tier=source.tier,
    )
    session.add(copied)
    await session.flush()

    result = await session.execute(select(LibraryPrimitive).where(LibraryPrimitive.id == copied.id))
    return result.scalar_one_or_none()


async def get_library_primitive(session: AsyncSession, primitive_id: uuid.UUID) -> LibraryPrimitive | None:
    stmt = select(LibraryPrimitive).where(LibraryPrimitive.id == primitive_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def _build_list_conditions(
    *,
    org_id: uuid.UUID | None,
    primitive_type: str | None,
    primitive_types: list[str] | None,
    search: str | None,
    source: str | None,
    excluded_tiers: list[str] | None,
) -> list[ColumnElement[bool]]:
    """Build the WHERE predicates shared by the cursor and offset list paths.

    Both branches narrow with exactly this chain, so the COUNT, the offset row
    page and the keyset page all describe the same universe.

    The returned list always holds at least the soft-delete predicate, so
    callers may apply ``.where(*conditions)`` unconditionally.
    """
    tiers = ["in_dev"] if excluded_tiers is None else excluded_tiers

    conditions: list[ColumnElement[bool]] = [LibraryPrimitive.deleted_at.is_(None)]

    if org_id is not None:
        conditions.append(LibraryPrimitive.organisation_id == org_id)

    if primitive_types:
        conditions.append(LibraryPrimitive.primitive_type.in_(primitive_types))
    elif primitive_type is not None:
        conditions.append(LibraryPrimitive.primitive_type == primitive_type)

    if search is not None and search.strip():
        term = f"%{search.strip()}%"
        conditions.append(LibraryPrimitive.name.ilike(term))

    if source is not None:
        # Pushed into the shared ``conditions`` so the COUNT, the row page and
        # the cursor path all narrow to the same source-filtered universe — a
        # Python-side post-filter cannot make ``total`` exact.
        conditions.append(LibraryPrimitive.source == source)

    if tiers:
        conditions.append(~LibraryPrimitive.tier.in_(tiers))

    return conditions


async def _page_by_cursor(
    session: AsyncSession,
    *,
    conditions: list[ColumnElement[bool]],
    cursor: str,
    page: int,
    page_size: int,
) -> PageResult[LibraryPrimitive]:
    paginator = CursorPaginator()
    stmt = select(LibraryPrimitive).where(*conditions)
    cp = await paginator.paginate(
        session,
        stmt,
        cursor=cursor,
        limit=page_size,
        model=LibraryPrimitive,
        compute_total=True,
    )
    return PageResult(
        items=cp.items,
        total=cp.total or 0,
        page=page,
        page_size=page_size,
        next_cursor=cp.next_cursor,
        has_more=cp.has_more,
    )


async def _page_by_offset(
    session: AsyncSession,
    *,
    conditions: list[ColumnElement[bool]],
    page: int,
    page_size: int,
) -> PageResult[LibraryPrimitive]:
    try:
        count_stmt = select(func.count()).select_from(LibraryPrimitive).where(*conditions)
        total = (await session.execute(count_stmt)).scalar_one()
    except SQLAlchemyError:
        logger.exception("count query failed")
        raise

    offset = (page - 1) * page_size
    try:
        items_stmt = (
            select(LibraryPrimitive)
            .where(*conditions)
            .order_by(LibraryPrimitive.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        items = list((await session.execute(items_stmt)).scalars())
    except SQLAlchemyError:
        logger.exception("items query failed")
        raise

    has_more = (offset + len(items)) < total
    return PageResult(items=items, total=total, page=page, page_size=page_size, has_more=has_more)


async def list_library_primitives(
    session: AsyncSession,
    *,
    org_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 20,
    primitive_type: str | None = None,
    primitive_types: list[str] | None = None,
    search: str | None = None,
    source: str | None = None,
    cursor: str | None = None,
    excluded_tiers: list[str] | None = None,
) -> PageResult[LibraryPrimitive]:

    conditions = _build_list_conditions(
        org_id=org_id,
        primitive_type=primitive_type,
        primitive_types=primitive_types,
        search=search,
        source=source,
        excluded_tiers=excluded_tiers,
    )

    if cursor is not None:
        return await _page_by_cursor(
            session,
            conditions=conditions,
            cursor=cursor,
            page=page,
            page_size=page_size,
        )

    return await _page_by_offset(session, conditions=conditions, page=page, page_size=page_size)


async def create_library_primitive(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    source: str,
    primitive_type: str,
    name: str,
    slug: str,
    description: str | None,
    author: str,
    version: str,
    tags: list[str],
    content_json: dict[str, Any],
    account_id: uuid.UUID | None,
    **options: Any,
) -> LibraryPrimitive:
    """Create a library primitive while grouping optional metadata."""
    source_url = options.pop("source_url", None)
    forked_from = options.pop("forked_from", None)
    checksum = options.pop("checksum", None)
    ed25519_signature = options.pop("ed25519_signature", None)
    verified = options.pop("verified", None)
    download_count = options.pop("download_count", None)
    average_rating = options.pop("average_rating", None)
    review_count = options.pop("review_count", None)
    owner_team_id = options.pop("owner_team_id", None)
    visibility = options.pop("visibility", "org")
    auto_update = options.pop("auto_update", True)
    tier = options.pop("tier", "native")
    if options:
        raise TypeError(f"unexpected create_library_primitive options: {sorted(options)}")

    primitive = LibraryPrimitive(
        organisation_id=org_id,
        source=source,
        primitive_type=primitive_type,
        name=name,
        slug=slug,
        description=description,
        author=author,
        version=version,
        tags=tags,
        content_json=content_json,
        source_url=source_url,
        forked_from=forked_from,
        checksum=checksum,
        ed25519_signature=ed25519_signature,
        verified=verified,
        download_count=download_count,
        average_rating=average_rating,
        review_count=review_count,
        owner_team_id=owner_team_id,
        visibility=visibility,
        account_id=account_id,
        auto_update=auto_update,
        tier=tier,
    )
    session.add(primitive)
    await session.flush()
    return primitive


async def update_library_primitive(
    session: AsyncSession,
    primitive_id: uuid.UUID,
    updates: dict[str, Any],
) -> LibraryPrimitive | None:
    primitive = await get_library_primitive(session, primitive_id)
    if primitive is None:
        return None
    apply_updates(primitive, updates)
    await session.flush()
    return primitive


async def list_primitives_by_version_group(
    session: AsyncSession,
    version_group_id: uuid.UUID,
) -> list[LibraryPrimitive]:
    stmt = (
        select(LibraryPrimitive)
        .where(LibraryPrimitive.version_group_id == version_group_id)
        .order_by(LibraryPrimitive.created_at.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars())


async def soft_delete_library_primitive(session: AsyncSession, primitive_id: uuid.UUID) -> LibraryPrimitive | None:
    result = await session.execute(
        update(LibraryPrimitive)
        .where(LibraryPrimitive.id == primitive_id, LibraryPrimitive.deleted_at.is_(None))
        .values(deleted_at=func.now())
        .returning(LibraryPrimitive)
    )
    await session.flush()
    return result.scalar_one_or_none()


async def restore_library_primitive(session: AsyncSession, primitive_id: uuid.UUID) -> LibraryPrimitive | None:
    result = await session.execute(
        update(LibraryPrimitive)
        .where(LibraryPrimitive.id == primitive_id, LibraryPrimitive.deleted_at.is_not(None))
        .values(deleted_at=None)
        .returning(LibraryPrimitive)
    )
    await session.flush()
    return result.scalar_one_or_none()
