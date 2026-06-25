"""CRUD for Team records."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult, apply_updates
from modulo.db.models.team import Team


async def create_team(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    created_by: uuid.UUID,
    description: str | None = None,
) -> Team:
    team = Team(
        organisation_id=org_id,
        name=name,
        created_by=created_by,
        description=description,
    )
    session.add(team)
    await session.flush()
    return team


async def get_team(session: AsyncSession, team_id: uuid.UUID) -> Team | None:
    result = await session.execute(select(Team).where(Team.id == team_id))
    return result.scalar_one_or_none()


async def get_team_by_name(
    session: AsyncSession, org_id: uuid.UUID, name: str
) -> Team | None:
    result = await session.execute(
        select(Team).where(Team.organisation_id == org_id, Team.name == name)
    )
    return result.scalar_one_or_none()


async def list_teams(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    page: int = 1,
    page_size: int = 20,
) -> PageResult[Team]:
    count_q = select(func.count()).select_from(Team).where(Team.organisation_id == org_id)
    total_result = await session.execute(count_q)
    total = total_result.scalar() or 0

    query = (
        select(Team)
        .where(Team.organisation_id == org_id)
        .order_by(Team.created_at)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await session.execute(query)
    items = list(result.scalars().all())

    return PageResult(items=items, total=total, page=page, page_size=page_size)


async def update_team(
    session: AsyncSession,
    team_id: uuid.UUID,
    updates: dict[str, object],
) -> Team | None:
    team = await get_team(session, team_id)
    if team is None:
        return None
    apply_updates(team, updates)
    await session.flush()
    return team


async def delete_team(session: AsyncSession, team_id: uuid.UUID) -> bool:
    team = await get_team(session, team_id)
    if team is None:
        return False
    await session.delete(team)
    await session.flush()
    return True
