"""Org-scoped CRUD for Stage.

All functions assume the caller has set the RLS org context via set_rls_org()
before calling. The session must be within an active transaction.
"""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult, apply_updates
from modulo.db.models.stage import Stage


async def create_stage(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    account_id: uuid.UUID,
    description: str | None = None,
    position: int = 0,
    owner_team_id: uuid.UUID | None = None,
    visibility: str = "org",
) -> Stage:
    stage = Stage(
        organisation_id=org_id,
        name=name,
        created_by=account_id,
        description=description,
        position=position,
        owner_team_id=owner_team_id,
        visibility=visibility,
    )
    session.add(stage)
    await session.flush()
    return stage


async def get_stage(session: AsyncSession, stage_id: uuid.UUID) -> Stage | None:
    result = await session.execute(select(Stage).where(Stage.id == stage_id))
    return result.scalar_one_or_none()


async def list_stages(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
    owner_team_id: uuid.UUID | None = None,
) -> PageResult[Stage]:
    offset = (page - 1) * page_size
    query = select(Stage)
    count_query = select(func.count()).select_from(Stage)
    if owner_team_id is not None:
        query = query.where(Stage.owner_team_id == owner_team_id)
        count_query = count_query.where(Stage.owner_team_id == owner_team_id)
    total = (await session.execute(count_query)).scalar_one()
    items = list(
        (
            await session.execute(query.order_by(Stage.position, Stage.name).offset(offset).limit(page_size))
        ).scalars()
    )
    return PageResult(items=items, total=total, page=page, page_size=page_size)


async def update_stage(
    session: AsyncSession,
    stage_id: uuid.UUID,
    updates: dict[str, Any],
) -> Stage | None:
    stage = await get_stage(session, stage_id)
    if stage is None:
        return None
    apply_updates(stage, updates)
    await session.flush()
    return stage


async def delete_stage(session: AsyncSession, stage_id: uuid.UUID) -> bool:
    stage = await get_stage(session, stage_id)
    if stage is None:
        return False
    await session.delete(stage)
    await session.flush()
    return True
