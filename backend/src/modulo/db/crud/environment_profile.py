"""Org-scoped CRUD for EnvironmentProfile."""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult, apply_updates
from modulo.db.models.environment_profile import EnvironmentProfile


async def create_environment_profile(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    account_id: uuid.UUID,
    description: str | None = None,
    provider_type: str = "local_docker",
    image_ref: str | None = None,
    capabilities: list[str] | None = None,
    config_json: dict[str, Any] | None = None,
    network_policy: str = "outbound",
    initialisation_strategy: str = "git_clone",
    secret_refs: list[str] | None = None,
    persistence_policy: str = "ephemeral",
    owner_team_id: uuid.UUID | None = None,
    visibility: str = "org",
) -> EnvironmentProfile:
    profile = EnvironmentProfile(
        organisation_id=org_id,
        name=name,
        description=description,
        provider_type=provider_type,
        image_ref=image_ref,
        capabilities_json=capabilities or [],
        config_json=config_json or {},
        network_policy=network_policy,
        initialisation_strategy=initialisation_strategy,
        secret_refs_json=secret_refs or [],
        persistence_policy=persistence_policy,
        account_id=account_id,
        owner_team_id=owner_team_id,
        visibility=visibility,
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
    try:
        count_q = select(func.count()).select_from(EnvironmentProfile)
        total = (await session.execute(count_q)).scalar_one()
    except ProgrammingError:
        return PageResult(items=[], total=0, page=page, page_size=page_size)

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
    """Soft-delete: set status to 'deleted' instead of hard-deleting."""
    profile = await get_environment_profile(session, profile_id)
    if profile is None:
        return False
    profile.status = "deleted"
    await session.flush()
    return True
