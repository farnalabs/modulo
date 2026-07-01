"""CRUD for token family management (refresh token rotation + family invalidation)."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.token_family import TokenFamily


async def get_or_create_family(
    session: AsyncSession, family_id: uuid.UUID, account_id: uuid.UUID, org_id: uuid.UUID | None
) -> TokenFamily:
    result = await session.execute(select(TokenFamily).where(TokenFamily.family_id == family_id).with_for_update())
    family = result.scalar_one_or_none()
    if family is None:
        family = TokenFamily(
            family_id=family_id,
            account_id=account_id,
            organisation_id=org_id,
            max_sequence=0,
        )
        session.add(family)
        await session.flush()
    return family


async def create_family(session: AsyncSession, account_id: uuid.UUID, org_id: uuid.UUID | None) -> TokenFamily:
    family = TokenFamily(
        family_id=uuid.uuid4(),
        account_id=account_id,
        organisation_id=org_id,
        max_sequence=0,
    )
    session.add(family)
    await session.flush()
    return family


async def advance_sequence(session: AsyncSession, family_id: uuid.UUID, expected_sequence: int) -> tuple[int, bool]:
    """Advance the token family sequence.

    Uses SELECT FOR UPDATE to prevent concurrent advancement races.
    Returns (new_sequence, theft_detected).
    theft_detected=True if the expected_sequence does not match max_sequence
    (meaning a different token in the family was already used).
    """
    result = await session.execute(select(TokenFamily).where(TokenFamily.family_id == family_id).with_for_update())
    family = result.scalar_one_or_none()
    if family is None:
        return 0, False

    if family.is_blacklisted:
        return 0, True

    if family.max_sequence != expected_sequence:
        family.is_blacklisted = True
        family.blacklisted_at = datetime.now(UTC)
        await session.flush()
        return family.max_sequence, True

    family.max_sequence += 1
    await session.flush()
    return family.max_sequence, False


async def blacklist_family(session: AsyncSession, family_id: uuid.UUID) -> bool:
    result = await session.execute(select(TokenFamily).where(TokenFamily.family_id == family_id).with_for_update())
    family = result.scalar_one_or_none()
    if family is None:
        return False
    family.is_blacklisted = True
    family.blacklisted_at = datetime.now(UTC)
    await session.flush()
    return True


async def list_families_for_account(session: AsyncSession, account_id: uuid.UUID) -> list[TokenFamily]:
    result = await session.execute(select(TokenFamily).where(TokenFamily.account_id == account_id))
    return list(result.scalars().all())


async def is_family_blacklisted(session: AsyncSession, family_id: uuid.UUID) -> bool:
    result = await session.execute(select(TokenFamily).where(TokenFamily.family_id == family_id))
    family = result.scalar_one_or_none()
    if family is None:
        return False
    return family.is_blacklisted
