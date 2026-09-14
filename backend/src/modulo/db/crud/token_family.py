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
    reuse_grace_max_steps: int,
    reuse_grace_max_per_window: int,
) -> tuple[int, bool, bool]:
    """Advance the token family sequence.

    Uses SELECT FOR UPDATE to prevent concurrent advancement races. Only
    advances families owned by *account_id*.

    FAR-819 reuse-interval semantics: presenting a stale (lower) family
    sequence is normally a theft signal that blacklists the family. When the
    reuse grace window is enabled (``reuse_grace_seconds > 0``) a replay is
    treated as a BENIGN retry/race instead of theft when ALL of these hold:

    * it is within ``reuse_grace_max_steps`` of the current sequence
      (0 < max_sequence - expected_sequence <= reuse_grace_max_steps; an
      expected_sequence ahead of max is never benign);
    * it arrives inside the live reuse window — ``rotated_at`` is set and
      ``now - rotated_at <= reuse_grace_seconds`` (the interval since the last
      rotation in which the superseded token stays acceptable);
    * it is within the per-window replay budget: the current window (marked by
      ``reuse_window_started_at``) has not already admitted
      ``reuse_grace_max_per_window`` replays.

    A benign replay advances the sequence normally, stamps a fresh
    ``rotated_at``, records the replay against the reuse window (starting a new
    window and resetting the counter when the prior window is NULL or expired),
    and returns ``(new_sequence, False, True)``.

    Every other mismatch — grace disabled, steps-behind beyond tolerance,
    expected_sequence ahead of max, an expired or never-started reuse window,
    an exhausted window budget, or an already-blacklisted family — blacklists
    the family (theft) and returns ``(max_sequence, True, False)``.

    Returns (new_sequence, theft_detected, grace_replay): ``grace_replay``
    marks a reuse tolerated as benign and implies ``theft_detected is False``.
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
    if family.max_sequence != expected_sequence:
        if _is_benign_reuse(
            family,
            expected_sequence,
            now,
            grace_seconds=reuse_grace_seconds,
            grace_max_steps=reuse_grace_max_steps,
        ):
            last_window = _as_utc_aware(family.reuse_window_started_at)
            if last_window is None or (now - last_window).total_seconds() > reuse_grace_seconds:
                family.reuse_window_started_at = now
                family.reuse_replay_count = 0
            if family.reuse_replay_count + 1 > reuse_grace_max_per_window:
                family.is_blacklisted = True
                family.blacklisted_at = now
                await session.flush()
                return family.max_sequence, True, False
            family.reuse_replay_count += 1
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


def _is_benign_reuse(
    family: TokenFamily,
    expected_sequence: int,
    now: datetime,
    *,
    grace_seconds: int,
    grace_max_steps: int,
) -> bool:
    """Decide whether a sequence mismatch is a tolerable reuse-interval replay.

    Benign only when grace is enabled, the presented sequence trails the
    current one within the steps tolerance (never when it is ahead), and the
    reuse window since the last rotation is still live. The per-window replay
    budget is enforced by the caller after this passes.
    """
    if grace_seconds <= 0:
        return False
    if expected_sequence > family.max_sequence:
        return False
    if family.max_sequence - expected_sequence > grace_max_steps:
        return False
    last_rotation = _as_utc_aware(family.rotated_at)
    return last_rotation is not None and (now - last_rotation).total_seconds() <= grace_seconds


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
