"""SCIM 2.0 provisioning CRUD — maps SCIM resources to internal User/Team models.

Users  → internal User
Groups → internal Team, with members → TeamMembership
"""

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.team import create_team, delete_team, get_team, update_team
from modulo.db.crud.team_membership import (
    add_team_member,
    get_membership_by_team_and_user,
    remove_team_member,
)
from modulo.db.crud.user import get_user_by_id_org
from modulo.db.models.team import Team
from modulo.db.models.team_membership import TeamMembership
from modulo.db.models.user import User

# ── User provisioning ─────────────────────────────────────────────────


async def scim_create_user(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    email: str,
    display_name: str,
    active: bool = True,
    org_role: str = "runner",
) -> User:
    user = User(
        organisation_id=org_id,
        email=email,
        display_name=display_name,
        active=active,
        org_role=org_role,
        auth_provider="scim",
        password_hash=None,
    )
    session.add(user)
    await session.flush()
    return user


async def scim_get_user(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
) -> User | None:
    return await get_user_by_id_org(session, user_id, org_id)


async def scim_update_user(
    session: AsyncSession,
    user: User,
    *,
    email: str | None = None,
    display_name: str | None = None,
    active: bool | None = None,
    org_role: str | None = None,
) -> User:
    if email is not None:
        user.email = email
    if display_name is not None:
        user.display_name = display_name
    if active is not None:
        user.active = active
    if org_role is not None:
        user.org_role = org_role
    await session.flush()
    return user


async def scim_delete_user_by_id(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    user = await get_user_by_id_org(session, user_id, org_id)
    if user is None:
        return False
    await session.delete(user)
    await session.flush()
    return True


async def scim_list_users(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    filter_str: str | None = None,
    start_index: int = 1,
    count: int = 20,
) -> tuple[list[User], int]:
    conditions = [User.organisation_id == org_id]
    if filter_str:
        like = f"%{filter_str}%"
        conditions.append(or_(User.email.ilike(like), User.display_name.ilike(like)))

    count_q = select(func.count()).select_from(User).where(*conditions)
    total = (await session.execute(count_q)).scalar() or 0

    query = (
        select(User)
        .where(*conditions)
        .order_by(User.created_at)
        .offset(start_index - 1)
        .limit(count)
    )
    result = await session.execute(query)
    items = list(result.scalars().all())
    return items, total


# ── Group (Team) provisioning ────────────────────────────────────────


async def scim_create_group(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    display_name: str,
    created_by: uuid.UUID,
    description: str | None = None,
) -> Team:
    return await create_team(
        session,
        org_id=org_id,
        name=display_name,
        created_by=created_by,
        description=description,
    )


async def scim_get_group(
    session: AsyncSession, team_id: uuid.UUID
) -> Team | None:
    return await get_team(session, team_id)


async def scim_update_group(
    session: AsyncSession,
    team: Team,
    *,
    name: str | None = None,
) -> Team | None:
    updates: dict[str, object] = {}
    if name is not None:
        updates["name"] = name
    if not updates:
        return team
    return await update_team(session, team.id, updates)


async def scim_delete_group_by_id(
    session: AsyncSession, team_id: uuid.UUID
) -> bool:
    return await delete_team(session, team_id)


async def scim_list_groups(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    filter_str: str | None = None,
    start_index: int = 1,
    count: int = 20,
) -> tuple[list[Team], int]:
    conditions = [Team.organisation_id == org_id]
    if filter_str:
        like = f"%{filter_str}%"
        conditions.append(Team.name.ilike(like))

    count_q = select(func.count()).select_from(Team).where(*conditions)
    total = (await session.execute(count_q)).scalar() or 0

    query = (
        select(Team)
        .where(*conditions)
        .order_by(Team.created_at)
        .offset(start_index - 1)
        .limit(count)
    )
    result = await session.execute(query)
    items = list(result.scalars().all())
    return items, total


# ── Group membership mapping ─────────────────────────────────────────


async def scim_add_group_member(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str = "member",
) -> TeamMembership:
    existing = await get_membership_by_team_and_user(session, team_id, user_id)
    if existing is not None:
        return existing
    return await add_team_member(
        session, org_id=org_id, team_id=team_id, user_id=user_id, role=role
    )


async def scim_remove_group_member(
    session: AsyncSession, team_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    membership = await get_membership_by_team_and_user(session, team_id, user_id)
    if membership is None:
        return False
    await remove_team_member(session, membership.id)
    return True


async def scim_list_group_members(
    session: AsyncSession, team_id: uuid.UUID
) -> list[TeamMembership]:
    result = await session.execute(
        select(TeamMembership).where(TeamMembership.team_id == team_id)
    )
    return list(result.scalars().all())
