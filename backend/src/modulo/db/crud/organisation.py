"""CRUD for Organisation records."""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import apply_updates
from modulo.db.models.organisation import Organisation
from modulo.db.seed import seed_system_schemas

_log = logging.getLogger(__name__)


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

    # Seed system schemas for the new organisation.
    if created_by is not None:
        try:
            await seed_system_schemas(session, org.id, created_by)
        except Exception:
            _log.warning("seed.system_schemas_failed_for_new_org", exc_info=True)

    return org


async def get_organisation_by_slug(
    session: AsyncSession,
    slug: str,
) -> Organisation | None:
    # ``organisations.slug`` is a partial UNIQUE index (``WHERE deleted_at IS
    # NULL``) so a soft-deleted org's slug may be reused. A live lookup must
    # ignore soft-deleted rows; otherwise a create would 409 against a slug that
    # is free to reuse, and a duplicate slug materialising would make
    # ``scalar_one_or_none`` raise ``MultipleResultsFound`` -> 500.
    stmt = select(Organisation).where(Organisation.slug == slug).where(Organisation.deleted_at.is_(None)).limit(1)
    result = await session.execute(stmt)
    return result.scalars().first()


async def list_organisations(
    session: AsyncSession,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[Organisation]:
    try:
        result = await session.execute(
            select(Organisation).order_by(Organisation.created_at.desc()).offset(offset).limit(limit)
        )
        return list(result.scalars().all())
    except ProgrammingError:
        return []


async def delete_organisation(
    session: AsyncSession,
    org_id: uuid.UUID,
) -> bool:
    org = await get_organisation(session, org_id)
    if org is None:
        return False
    await session.delete(org)
    await session.flush()
    return True


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
