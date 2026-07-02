"""CRUD for Organisation records."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import apply_updates
from modulo.db.models.organisation import Organisation


async def get_organisation(
    session: AsyncSession,
    org_id: uuid.UUID,
) -> Organisation | None:
    result = await session.execute(select(Organisation).where(Organisation.id == org_id))
    return result.scalar_one_or_none()


async def create_organisation(
    session: AsyncSession,
    *,
    name: str,
    slug: str,
    plan_id: str | None = None,
    created_by: uuid.UUID | None = None,
) -> Organisation:
    org = Organisation(
        name=name,
        slug=slug,
        plan_id=plan_id,
        created_by=created_by,
    )
    session.add(org)
    await session.flush()
    await session.refresh(org)
    return org


async def get_organisation_by_slug(
    session: AsyncSession,
    slug: str,
) -> Organisation | None:
    result = await session.execute(select(Organisation).where(Organisation.slug == slug))
    return result.scalar_one_or_none()


async def update_organisation(
    session: AsyncSession,
    org_id: uuid.UUID,
    updates: dict[str, object],
) -> Organisation | None:
    org = await get_organisation(session, org_id)
    if org is None:
        return None
    apply_updates(org, updates)
    await session.flush()
    return org
