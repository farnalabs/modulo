"""CRUD for Invitation records (FAR-461).

Invitations live OUTSIDE the ``rls_org_isolation`` regime (pre-authenticated
consumption), so every helper scopes by organisation explicitly. Token
plaintexts are 256-bit urlsafe values minted by
``modulo.util.one_time_token.generate_token`` (shared with the MCP setup
handoff); only their SHA-256 hex is persisted.
"""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.invitation import Invitation
from modulo.util.one_time_token import generate_token, hash_token

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult


def hash_invitation_token(token: str) -> str:
    """SHA-256 hex of a token plaintext — the value stored in invitations.token_hash."""
    return hash_token(token)


def _now() -> datetime:
    return datetime.now(UTC)


def _live_conditions(
    invitation: type[Invitation],
) -> tuple[ColumnElement[bool], ColumnElement[bool], ColumnElement[bool]]:
    """Liveness predicate shared by every lookup/mutation: an invitation is
    live when it is un-consumed, un-revoked, and not yet expired.

    Kept in ONE place so ``get_valid_by_token_hash`` (the pre-CAS read) and
    ``consume_invitation``'s CAS WHERE clause stay semantically identical —
    that invariant is what makes double consumption impossible.
    """
    return (
        invitation.consumed_at.is_(None),
        invitation.revoked_at.is_(None),
        invitation.expires_at > _now(),
    )


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
    plaintext = generate_token()
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
            *_live_conditions(Invitation),
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
            *_live_conditions(Invitation),
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
                *_live_conditions(Invitation),
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
                *_live_conditions(Invitation),
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
        *_live_conditions(Invitation),
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
