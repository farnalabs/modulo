"""Org-scoped CRUD for EnvironmentProfile."""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult, apply_updates
from modulo.db.models.environment_profile import EnvironmentProfile


async def create_environment_profile(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    image_ref: str,
    account_id: uuid.UUID,
    description: str | None = None,
    capabilities: list[str] | None = None,
    egress_policy: str | None = None,
    timeout_seconds: int = 3600,
    persistence_policy: dict[str, Any] | None = None,
    resource_limits: dict[str, Any] | None = None,
) -> EnvironmentProfile:
    profile = EnvironmentProfile(
        organisation_id=org_id,
        name=name,
        description=description,
        image_ref=image_ref,
        capabilities=capabilities or [],
        egress_policy=egress_policy,
        timeout_seconds=timeout_seconds,
        persistence_policy=persistence_policy or {},
        resource_limits_json=resource_limits or {},
        account_id=account_id,
        is_active=True,
    )
    session.add(profile)
    await session.flush()
    return profile


async def get_environment_profile(session: AsyncSession, profile_id: uuid.UUID) -> EnvironmentProfile | None:
    result = await session.execute(select(EnvironmentProfile).where(EnvironmentProfile.id == profile_id))
    return result.scalar_one_or_none()


async def list_environment_profiles(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
) -> PageResult[EnvironmentProfile]:
    offset = (page - 1) * page_size
    count_q = select(func.count()).select_from(EnvironmentProfile)
    total = (await session.execute(count_q)).scalar_one()

    q = select(EnvironmentProfile).order_by(EnvironmentProfile.created_at.desc()).offset(offset).limit(page_size)
    rows = (await session.execute(q)).scalars().all()
    return PageResult(items=list(rows), total=total, page=page, page_size=page_size)


async def update_environment_profile(
    session: AsyncSession,
    profile_id: uuid.UUID,
    updates: dict[str, Any],
) -> EnvironmentProfile | None:
    profile = await get_environment_profile(session, profile_id)
    if profile is None:
        return None
    apply_updates(profile, updates)
    await session.flush()
    return profile


async def delete_environment_profile(session: AsyncSession, profile_id: uuid.UUID) -> bool:
    profile = await get_environment_profile(session, profile_id)
    if profile is None:
        return False
    await session.delete(profile)
    await session.flush()
    return True
