"""CRUD for User records.

Login flow queries users without RLS (no org context yet).
All other operations are org-scoped.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult
from modulo.db.models.user import User


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    """Find a user by email across all organisations (no RLS — used at login)."""
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def get_user_by_id_org(
    session: AsyncSession, user_id: uuid.UUID, org_id: uuid.UUID
) -> User | None:
    result = await session.execute(
        select(User).where(User.id == user_id, User.organisation_id == org_id)
    )
    return result.scalar_one_or_none()


async def create_user(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    email: str,
    display_name: str,
    password_hash: str,
    org_role: str = "runner",
    auth_provider: str = "local",
) -> User:
    user = User(
        organisation_id=org_id,
        email=email,
        display_name=display_name,
        password_hash=password_hash,
        org_role=org_role,
        auth_provider=auth_provider,
    )
    session.add(user)
    await session.flush()
    return user


async def update_user_preferences(
    session: AsyncSession, user_id: uuid.UUID, preferences: dict[str, object]
) -> dict[str, object]:
    await session.execute(
        update(User).where(User.id == user_id).values(preferences=preferences)
    )
    return preferences


async def update_last_login(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(
        update(User).where(User.id == user_id).values(last_login=datetime.now(UTC))
    )


async def list_users_for_org(
    session: AsyncSession, org_id: uuid.UUID
) -> list[User]:
    result = await session.execute(
        select(User).where(User.organisation_id == org_id).order_by(User.created_at)
    )
    return list(result.scalars().all())


async def list_users_paginated(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    page: int = 1,
    page_size: int = 20,
    search: str | None = None,
    role_filter: str | None = None,
) -> PageResult[User]:
    conditions = [User.organisation_id == org_id]
    if search:
        conditions.append(User.email.ilike(f"%{search}%"))
    if role_filter:
        conditions.append(User.org_role == role_filter)

    count_q = select(func.count()).select_from(User).where(*conditions)
    total = (await session.execute(count_q)).scalar() or 0

    query = (
        select(User)
        .where(*conditions)
        .order_by(User.created_at)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await session.execute(query)
    items = list(result.scalars().all())

    return PageResult(items=items, total=total, page=page, page_size=page_size)


async def update_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    updates: dict[str, object],
) -> User | None:
    user = await get_user_by_id(session, user_id)
    if user is None:
        return None
    for key, value in updates.items():
        setattr(user, key, value)
    await session.flush()
    return user
