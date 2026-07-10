"""Org-scoped CRUD for LifecycleMap.

All functions assume the caller has set the RLS org context via set_rls_org()
before calling. The session must be within an active transaction.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult, apply_updates
from modulo.db.models.lifecycle_map import LifecycleMap


async def create_lifecycle_map(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    account_id: uuid.UUID,
    description: str | None = None,
    owner_team_id: uuid.UUID | None = None,
    visibility: str = "org",
    version: int = 1,
    content_json: dict[str, Any] | None = None,
) -> LifecycleMap:
    lifecycle_map = LifecycleMap(
        organisation_id=org_id,
        name=name,
        account_id=account_id,
        description=description,
        owner_team_id=owner_team_id,
        visibility=visibility,
        version=version,
        content_json=content_json or {},
    )
    session.add(lifecycle_map)
    await session.flush()
    return lifecycle_map


async def get_lifecycle_map(session: AsyncSession, lifecycle_map_id: uuid.UUID) -> LifecycleMap | None:
    result = await session.execute(
        select(LifecycleMap).where(LifecycleMap.id == lifecycle_map_id, LifecycleMap.archived_at.is_(None))
    )
    return result.scalar_one_or_none()


async def list_lifecycle_maps(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
    owner_team_id: uuid.UUID | None = None,
    include_archived: bool = False,
) -> PageResult[LifecycleMap]:
    offset = (page - 1) * page_size
    query = select(LifecycleMap)
    count_query = select(func.count()).select_from(LifecycleMap)
    if not include_archived:
        query = query.where(LifecycleMap.archived_at.is_(None))
        count_query = count_query.where(LifecycleMap.archived_at.is_(None))
    if owner_team_id is not None:
        query = query.where(LifecycleMap.owner_team_id == owner_team_id)
        count_query = count_query.where(LifecycleMap.owner_team_id == owner_team_id)
    try:
        total = (await session.execute(count_query)).scalar_one()
    except ProgrammingError:
        return PageResult(items=[], total=0, page=page, page_size=page_size)
    items = list(
        (
            await session.execute(query.order_by(LifecycleMap.updated_at.desc()).offset(offset).limit(page_size))
        ).scalars()
    )
    return PageResult(items=items, total=total, page=page, page_size=page_size)


async def update_lifecycle_map(
    session: AsyncSession,
    lifecycle_map_id: uuid.UUID,
    updates: dict[str, Any],
) -> LifecycleMap | None:
    lifecycle_map = await get_lifecycle_map(session, lifecycle_map_id)
    if lifecycle_map is None:
        return None
    apply_updates(lifecycle_map, updates)
    await session.flush()
    return lifecycle_map


async def delete_lifecycle_map(session: AsyncSession, lifecycle_map_id: uuid.UUID) -> bool:
    lifecycle_map = await get_lifecycle_map(session, lifecycle_map_id)
    if lifecycle_map is None:
        return False
    lifecycle_map.archived_at = datetime.now(UTC)
    await session.flush()
    return True
