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
    session: AsyncSession,
    family_id: uuid.UUID,
    expected_sequence: int,
    account_id: uuid.UUID,
    *,
    reuse_grace_seconds: int,
) -> tuple[int, bool, bool]:
    """Advance the token family sequence.

    Uses SELECT FOR UPDATE to prevent concurrent advancement races. Only
    advances families owned by *account_id*.

    FAR-819 reuse-interval semantics:

    * ``expected_sequence == max_sequence`` → advance + mint (normal).
    * ``expected_sequence > max_sequence`` (forged/ahead) → blacklist (theft).
    * ``expected_sequence < max_sequence``:
      - if ``reuse_grace_seconds > 0`` AND ``rotated_at`` is set AND
        ``now - rotated_at <= reuse_grace_seconds`` → benign reuse: advance
        normally (``max_sequence += 1``), reset ``rotated_at`` to now, mint.
        Set ``reuse_replay=True`` for logging ONLY — never blacklist.
      - otherwise → blacklist (theft).
    * family already blacklisted → theft.

    Returns ``(new_sequence, theft_detected, reuse_replay)``:
    ``reuse_replay`` flags a benign within-window reuse that was accepted and
    minted normally. The caller always mints on ``reuse_replay=False`` AND
    ``theft_detected=False``.
    """
    result = await session.execute(
        select(TokenFamily)
        .where(TokenFamily.family_id == family_id, TokenFamily.account_id == account_id)
        .with_for_update()
    )
    family = result.scalar_one_or_none()
    if family is None:
        return 0, False, False

    if family.is_blacklisted:
        return 0, True, False

    now = datetime.now(UTC)
    if expected_sequence > family.max_sequence:
        family.is_blacklisted = True
        family.blacklisted_at = now
        await session.flush()
        return family.max_sequence, True, False

    if expected_sequence < family.max_sequence:
        last_rotation = _as_utc_aware(family.rotated_at)
        if (
            reuse_grace_seconds > 0
            and last_rotation is not None
            and (now - last_rotation).total_seconds() <= reuse_grace_seconds
        ):
            family.max_sequence += 1
            family.rotated_at = now
            await session.flush()
            return family.max_sequence, False, True

        family.is_blacklisted = True
        family.blacklisted_at = now
        await session.flush()
        return family.max_sequence, True, False

    family.max_sequence += 1
    family.rotated_at = now
    await session.flush()
    return family.max_sequence, False, False


def _as_utc_aware(value: datetime | None) -> datetime | None:
    """Normalise a stored timestamp to tz-aware UTC for interval arithmetic.

    PostgreSQL round-trips timezone-aware timestamps as aware datetimes, but
    the SQLite dialect does not honour ``DateTime(timezone=True)`` and returns
    naive datetimes — subtract them from a tz-aware ``now`` without this guard
    and the comparison raises ``TypeError``.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


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
