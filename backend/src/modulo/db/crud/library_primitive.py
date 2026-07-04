"""Org-scoped CRUD for LibraryPrimitive.

All functions require RLS org context to be set by the caller.
"""

import logging
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

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
    )
    session.add(copied)
    await session.flush()

    result = await session.execute(select(LibraryPrimitive).where(LibraryPrimitive.id == copied.id))
    return result.scalar_one_or_none()


async def get_library_primitive(session: AsyncSession, primitive_id: uuid.UUID) -> LibraryPrimitive | None:
    stmt = select(LibraryPrimitive).where(LibraryPrimitive.id == primitive_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_library_primitives(
    session: AsyncSession,
    *,
    org_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 20,
    primitive_type: str | None = None,
    search: str | None = None,
    cursor: str | None = None,
    excluded_tiers: list[str] | None = None,
) -> PageResult[LibraryPrimitive]:

    if excluded_tiers is None:
        excluded_tiers = ["in_dev"]

    conditions = []

    if org_id is not None:
        conditions.append(LibraryPrimitive.organisation_id == org_id)

    if primitive_type is not None:
        conditions.append(LibraryPrimitive.primitive_type == primitive_type)

    if search is not None and search.strip():
        term = f"%{search.strip()}%"
        conditions.append(LibraryPrimitive.name.ilike(term))

    if excluded_tiers:
        conditions.append(~LibraryPrimitive.tier.in_(excluded_tiers))

    if cursor is not None:
        paginator = CursorPaginator()
        stmt = select(LibraryPrimitive)
        if conditions:
            stmt = stmt.where(*conditions)
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

    try:
        count_stmt = select(func.count()).select_from(LibraryPrimitive)
        if conditions:
            count_stmt = count_stmt.where(*conditions)
        total = (await session.execute(count_stmt)).scalar_one()
    except SQLAlchemyError:
        logger.exception("count query failed")
        raise

    try:
        items_stmt = (
            select(LibraryPrimitive)
            .order_by(LibraryPrimitive.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        if conditions:
            items_stmt = items_stmt.where(*conditions)

        items = list((await session.execute(items_stmt)).scalars())
    except SQLAlchemyError:
        logger.exception("items query failed")
        raise

    return PageResult(items=items, total=total, page=page, page_size=page_size)


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
    source_url: str | None,
    forked_from: uuid.UUID | None,
    checksum: str | None,
    ed25519_signature: str | None,
    verified: bool | None,
    download_count: int | None,
    average_rating: float | None,
    review_count: int | None,
    owner_team_id: uuid.UUID | None,
    visibility: str,
    account_id: uuid.UUID | None,
    auto_update: bool = True,
    tier: str = "native",
) -> LibraryPrimitive:
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


async def delete_library_primitive(session: AsyncSession, primitive_id: uuid.UUID) -> bool:
    primitive = await get_library_primitive(session, primitive_id)
    if primitive is None:
        return False
    await session.delete(primitive)
    await session.flush()
    return True
