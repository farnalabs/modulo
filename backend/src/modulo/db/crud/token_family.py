"""CRUD for token family management (refresh token rotation + family invalidation)."""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.break_glass_deny import live_predicate, render_sql

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult
from modulo.db.models.token_family import TokenFamily


async def get_or_create_family(
    session: AsyncSession, family_id: uuid.UUID, account_id: uuid.UUID, org_id: uuid.UUID | None
) -> TokenFamily:
    result = await session.execute(
        select(TokenFamily)
        .where(TokenFamily.family_id == family_id, TokenFamily.account_id == account_id)
        .with_for_update()
    )
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


async def advance_sequence(
    session: AsyncSession, family_id: uuid.UUID, expected_sequence: int, account_id: uuid.UUID
) -> tuple[int, bool]:
    """Advance the token family sequence.

    Uses SELECT FOR UPDATE to prevent concurrent advancement races.
    Only advances families owned by *account_id*.
    Returns (new_sequence, theft_detected).
    theft_detected=True if the expected_sequence does not match max_sequence
    (meaning a different token in the family was already used).
    """
    result = await session.execute(
        select(TokenFamily)
        .where(TokenFamily.family_id == family_id, TokenFamily.account_id == account_id)
        .with_for_update()
    )
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


async def blacklist_family(session: AsyncSession, family_id: uuid.UUID, account_id: uuid.UUID) -> bool:
    result = await session.execute(
        select(TokenFamily)
        .where(TokenFamily.family_id == family_id, TokenFamily.account_id == account_id)
        .with_for_update()
    )
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


async def is_family_blacklisted(session: AsyncSession, family_id: uuid.UUID, account_id: uuid.UUID) -> bool:
    result = await session.execute(
        select(TokenFamily).where(TokenFamily.family_id == family_id, TokenFamily.account_id == account_id)
    )
    family = result.scalar_one_or_none()
    if family is None:
        return False
    return family.is_blacklisted


async def consume_break_glass_credential(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    current_password_hash: str,
    new_password_hash: str,
) -> int:
    """Compare-and-swap a break-glass one-shot credential (login-hook CAS).

    Atomically UPDATEs ``accounts.password_hash`` to *new_password_hash* only
    when the row still holds *current_password_hash* AND the credential is
    still live — the CAS WHERE is emitted from the shared break-glass builder
    (``live_predicate``), so an already-consumed / expired / deactivated /
    inactive credential matches nothing and the UPDATE changes zero rows.

    Returns the number of rows changed: 1 when this caller consumed the
    credential, 0 when it was already spent. Raw ``text()`` (never the ORM) so
    the UPDATE does not inherit ``TimestampMixin.updated_at``'s onupdate —
    credential consumption is deliberately invisible to ``updated_at``.
    """
    cas_where = render_sql(live_predicate())
    statement = " ".join(
        (
            "UPDATE public.accounts SET password_hash = :bg_new_hash",
            "WHERE accounts.id = :bg_account_id",
            "AND accounts.password_hash = :bg_old_hash AND",
            cas_where,
        )
    )
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            text(statement).bindparams(
                bg_new_hash=new_password_hash,
                bg_account_id=account_id,
                bg_old_hash=current_password_hash,
            )
        ),
    )
    return result.rowcount or 0
