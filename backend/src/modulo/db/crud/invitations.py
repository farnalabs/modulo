"""CRUD for Invitation records (FAR-461).

Invitations live OUTSIDE the ``rls_org_isolation`` regime (pre-authenticated
consumption), so every helper scopes by organisation explicitly. Token
plaintexts are ``secrets.token_urlsafe(32)``; only their SHA-256 hex is
persisted.
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.invitation import Invitation

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult


def hash_invitation_token(token: str) -> str:
    """SHA-256 hex of a token plaintext — the value stored in invitations.token_hash."""
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


async def create_invitation(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    email: str,
    display_name: str,
    org_role: str,
    invited_by: uuid.UUID,
    expires_at: datetime,
) -> tuple[Invitation, str]:
    """Create an invitation. Returns ``(invitation, plaintext)`` — the plaintext
    is shown to the inviting admin exactly once and never persisted."""
    plaintext = secrets.token_urlsafe(32)
    invitation = Invitation(
        organisation_id=organisation_id,
        email=email,
        display_name=display_name,
        org_role=org_role,
        token_hash=hash_invitation_token(plaintext),
        invited_by=invited_by,
        expires_at=expires_at,
    )
    session.add(invitation)
    await session.flush()
    return invitation, plaintext


async def has_live_for_email(session: AsyncSession, *, org_id: uuid.UUID, email: str) -> bool:
    """True when an un-consumed / un-revoked / un-expired invitation for this
    email already exists in the org (duplicate-invite guard)."""
    result = await session.execute(
        select(Invitation.id)
        .where(
            Invitation.organisation_id == org_id,
            Invitation.email == email,
            Invitation.consumed_at.is_(None),
            Invitation.revoked_at.is_(None),
            Invitation.expires_at > _now(),
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def get_valid_by_token_hash(session: AsyncSession, token_hash: str) -> Invitation | None:
    """Look up a consumable invitation by its pre-computed hash.

    Rejects consumed, revoked, and expired rows. Row-locks so two concurrent
    accepts cannot both read the same still-valid row before the CAS.
    """
    result = await session.execute(
        select(Invitation)
        .where(
            Invitation.token_hash == token_hash,
            Invitation.consumed_at.is_(None),
            Invitation.revoked_at.is_(None),
            Invitation.expires_at > _now(),
        )
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def consume_invitation(session: AsyncSession, invitation: Invitation) -> bool:
    """Compare-and-swap consumption of a still-live invitation.

    Atomically sets ``consumed_at`` only when the row is un-consumed,
    un-revoked, and not yet expired; an already-spent row matches nothing and
    zero rows change. Returns True when THIS caller consumed it.
    """
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(Invitation)
            .where(
                Invitation.id == invitation.id,
                Invitation.consumed_at.is_(None),
                Invitation.revoked_at.is_(None),
                Invitation.expires_at > _now(),
            )
            .values(consumed_at=_now())
        ),
    )
    return bool(result.rowcount)


async def revoke_invitation(session: AsyncSession, *, invitation_id: uuid.UUID, org_id: uuid.UUID) -> bool:
    """Revoke a pending invitation owned by *org_id*.

    Returns False (no rows changed) when the invitation does not exist, lives
    in another org, was already consumed, was already revoked, or has expired.
    """
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(Invitation)
            .where(
                Invitation.id == invitation_id,
                Invitation.organisation_id == org_id,
                Invitation.consumed_at.is_(None),
                Invitation.revoked_at.is_(None),
                Invitation.expires_at > _now(),
            )
            .values(revoked_at=_now())
        ),
    )
    return bool(result.rowcount)


async def list_pending_for_org(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[Invitation], int]:
    """List pending (not consumed / not revoked / not expired) org invitations."""
    conditions = (
        Invitation.organisation_id == org_id,
        Invitation.consumed_at.is_(None),
        Invitation.revoked_at.is_(None),
        Invitation.expires_at > _now(),
    )

    count_q = select(func.count()).select_from(Invitation).where(*conditions)
    total = (await session.execute(count_q)).scalar() or 0

    query = (
        select(Invitation)
        .where(*conditions)
        .order_by(Invitation.created_at.desc(), Invitation.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await session.execute(query)
    return list(result.scalars().all()), total
