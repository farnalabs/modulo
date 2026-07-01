"""Org-scoped CRUD for Publisher.

All functions require RLS org context to be set by the caller.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult, apply_updates
from modulo.db.models.publisher import Publisher

TRUST_TIER_GREEN = "green"
TRUST_TIER_AMBER = "amber"
VALID_TIERS = frozenset({TRUST_TIER_GREEN, TRUST_TIER_AMBER})


async def get_publisher(session: AsyncSession, publisher_id: uuid.UUID) -> Publisher | None:
    stmt = select(Publisher).where(Publisher.id == publisher_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_publisher_by_key(session: AsyncSession, org_id: uuid.UUID, public_key_hex: str) -> Publisher | None:
    stmt = select(Publisher).where(
        Publisher.organisation_id == org_id,
        Publisher.public_key_hex == public_key_hex,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_publisher_by_name(session: AsyncSession, org_id: uuid.UUID, name: str) -> Publisher | None:
    stmt = select(Publisher).where(
        Publisher.organisation_id == org_id,
        Publisher.name == name,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_publishers(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    page: int = 1,
    page_size: int = 20,
    trust_tier: str | None = None,
    search: str | None = None,
) -> PageResult[Publisher]:
    conditions = [Publisher.organisation_id == org_id]

    if trust_tier is not None:
        conditions.append(Publisher.trust_tier == trust_tier)

    if search is not None and search.strip():
        term = f"%{search.strip()}%"
        conditions.append(Publisher.name.ilike(term))

    count_stmt = select(Publisher).where(*conditions)
    total = len((await session.execute(count_stmt)).scalars().all())

    items_stmt = (
        select(Publisher)
        .where(*conditions)
        .order_by(Publisher.trust_tier, Publisher.name)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = list((await session.execute(items_stmt)).scalars())

    return PageResult(items=items, total=total, page=page, page_size=page_size)


async def create_publisher(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    contact_email: str | None,
    public_key_hex: str,
    trust_tier: str = TRUST_TIER_AMBER,
    website_url: str | None = None,
) -> Publisher:
    if trust_tier not in VALID_TIERS:
        raise ValueError(f"Invalid trust_tier: {trust_tier}. Must be one of {sorted(VALID_TIERS)}")

    publisher = Publisher(
        organisation_id=org_id,
        name=name,
        contact_email=contact_email,
        public_key_hex=public_key_hex,
        trust_tier=trust_tier,
        verified_since=datetime.now(UTC) if trust_tier == TRUST_TIER_GREEN else None,
        website_url=website_url,
    )
    session.add(publisher)
    await session.flush()
    return publisher


async def update_publisher(
    session: AsyncSession,
    publisher_id: uuid.UUID,
    updates: dict[str, Any],
) -> Publisher | None:
    publisher = await get_publisher(session, publisher_id)
    if publisher is None:
        return None

    if "trust_tier" in updates:
        tier = updates["trust_tier"]
        if tier not in VALID_TIERS:
            raise ValueError(f"Invalid trust_tier: {tier}. Must be one of {sorted(VALID_TIERS)}")
        if tier == TRUST_TIER_GREEN and publisher.trust_tier != TRUST_TIER_GREEN:
            updates["verified_since"] = datetime.now(UTC)
        elif tier != TRUST_TIER_GREEN:
            updates["verified_since"] = None

    apply_updates(publisher, updates)
    await session.flush()
    return publisher


async def delete_publisher(session: AsyncSession, publisher_id: uuid.UUID) -> bool:
    publisher = await get_publisher(session, publisher_id)
    if publisher is None:
        return False
    await session.delete(publisher)
    await session.flush()
    return True
