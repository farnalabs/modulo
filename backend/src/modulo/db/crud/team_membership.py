"""CRUD for TeamMembership records."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult
from modulo.db.models.team_membership import TeamMembership


async def add_team_member(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str = "viewer",
) -> TeamMembership:
    membership = TeamMembership(
        organisation_id=org_id,
        team_id=team_id,
        user_id=user_id,
        role=role,
    )
    session.add(membership)
    await session.flush()
    return membership


async def get_membership(session: AsyncSession, membership_id: uuid.UUID) -> TeamMembership | None:
    result = await session.execute(select(TeamMembership).where(TeamMembership.id == membership_id))
    return result.scalar_one_or_none()


async def get_membership_by_team_and_user(
    session: AsyncSession, team_id: uuid.UUID, user_id: uuid.UUID
) -> TeamMembership | None:
    result = await session.execute(
        select(TeamMembership).where(
            TeamMembership.team_id == team_id,
            TeamMembership.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def list_team_members(
    session: AsyncSession,
    team_id: uuid.UUID,
    *,
    page: int = 1,
    page_size: int = 20,
) -> PageResult[TeamMembership]:
    count_q = select(func.count()).select_from(TeamMembership).where(TeamMembership.team_id == team_id)
    total_result = await session.execute(count_q)
    total = total_result.scalar() or 0

    query = (
        select(TeamMembership)
        .where(TeamMembership.team_id == team_id)
        .order_by(TeamMembership.created_at)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await session.execute(query)
    items = list(result.scalars().all())

    return PageResult(items=items, total=total, page=page, page_size=page_size)


async def update_member_role(
    session: AsyncSession,
    membership_id: uuid.UUID,
    role: str,
) -> TeamMembership | None:
    membership = await get_membership(session, membership_id)
    if membership is None:
        return None
    membership.role = role
    await session.flush()
    return membership


async def list_memberships_for_user(session: AsyncSession, user_id: uuid.UUID) -> list[TeamMembership]:
    result = await session.execute(select(TeamMembership).where(TeamMembership.user_id == user_id))
    return list(result.scalars().all())


async def remove_team_member(session: AsyncSession, membership_id: uuid.UUID) -> bool:
    membership = await get_membership(session, membership_id)
    if membership is None:
        return False
    await session.delete(membership)
    await session.flush()
    return True
