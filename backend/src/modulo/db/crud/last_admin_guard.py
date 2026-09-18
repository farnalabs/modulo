"""Last-admin prevention guard shared by the REST admin + SCIM mutation surfaces.

Deliverable (A) of the break-glass admin recovery plan (docs/break-glass-admin-
recovery-plan.md v17): reject any mutation that would leave an org with zero
ACTIVE, non-break-glass admins.

Serialization uses the same TWO-INT4 MD5 advisory-lock key derivation as
``modulo.db.repositories.locks`` (key = ``str(org_id)`` verbatim — a prefixed
key silently re-creates the disjoint-keyspace bug). One lock mode is pinned:
``pg_try_advisory_xact_lock`` in a polling loop, auto-released when the
surrounding transaction ends (the count and the mutation share the same
transaction). On polling EXHAUSTION the guard raises an error that surfaces as
503/500 — it NEVER proceeds unlocked.
"""

import asyncio
import uuid
from collections.abc import Awaitable
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.account import Account
from modulo.db.models.org_membership import OrgMembership
from modulo.db.repositories.locks import _POLL_INTERVAL, _str_to_lock_keys

_LOCK_TIMEOUT_SECONDS = 300.0


class LastAdminLockoutError(Exception):
    """The mutation would leave the org with zero active non-break-glass admins."""

    def __init__(self, *, org_id: uuid.UUID, reason: str) -> None:
        super().__init__(reason)
        self.org_id = org_id
        self.reason = reason


class LastAdminLockoutUnavailableError(RuntimeError):
    """The advisory lock guarding the last-admin count could not be acquired."""

    def __init__(self, *, org_id: uuid.UUID) -> None:
        super().__init__(f"Could not acquire last-admin guard lock for org {org_id}")
        self.org_id = org_id


async def _dialect_name(session: AsyncSession) -> str:
    """Return the SQLAlchemy dialect name, awaiting the AsyncSession's bind."""
    bind = session.get_bind()
    if isinstance(bind, Awaitable):
        bind = await bind
    return str(getattr(getattr(bind, "dialect", None), "name", "") or "")


async def _acquire_org_lock(session: AsyncSession, org_id: uuid.UUID) -> None:
    """Acquire the org's two-int4 MD5 advisory xact lock via polling.

    Uses ``pg_try_advisory_xact_lock`` so the lock is released automatically
    when the surrounding transaction commits or rolls back — never explicitly
    unlocked, never mixed with ``pg_advisory_unlock`` (which cannot release a
    xact lock). On exhaustion raises ``LastAdminLockoutUnavailableError``.
    """
    k1, k2 = _str_to_lock_keys(str(org_id))
    deadline = asyncio.get_running_loop().time() + _LOCK_TIMEOUT_SECONDS
    while True:
        result = await session.execute(
            text("SELECT pg_try_advisory_xact_lock(:key1, :key2)"),
            {"key1": k1, "key2": k2},
        )
        if bool(result.scalar_one()):
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise LastAdminLockoutUnavailableError(org_id=org_id)
        await asyncio.sleep(_POLL_INTERVAL)


async def assert_not_last_admin(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    target_account_id: uuid.UUID,
    target_role_after: str | None,
    target_active_after: bool | None,
) -> None:
    """Reject a mutation that would leave the org with zero ACTIVE, non-break-glass admins.

    ``target_role_after`` / ``target_active_after`` describe the POST-mutation
    state. Remaining admins = (other active non-break-glass admins) + (1 if the
    target itself remains an active non-break-glass admin). Raises
    ``LastAdminLockoutError`` (mapped to 422 REST / 409 SCIM) when the result
    would be zero.
    """
    dialect = await _dialect_name(session)
    if dialect == "postgresql":
        await _acquire_org_lock(session, org_id)

    target_row: Any = await session.execute(
        select(Account.active, Account.is_break_glass).where(Account.id == target_account_id)
    )
    target = target_row.first()
    target_active_now = bool(target[0]) if target is not None else False
    target_is_break_glass = bool(target[1]) if target is not None else False

    target_active = target_active_after if target_active_after is not None else target_active_now
    target_remains_admin = target_role_after == "admin" and target_active is True and not target_is_break_glass

    count_result = await session.execute(
        select(func.count())
        .select_from(OrgMembership)
        .join(Account, Account.id == OrgMembership.account_id)
        .where(
            OrgMembership.organisation_id == org_id,
            OrgMembership.account_id != target_account_id,
            OrgMembership.role == "admin",
            OrgMembership.deactivated_at.is_(None),
            Account.active.is_(True),
            Account.is_break_glass.is_(False),
        )
    )
    other_admins = int(count_result.scalar_one() or 0)

    remaining = other_admins + (1 if target_remains_admin else 0)
    if remaining == 0:
        raise LastAdminLockoutError(
            org_id=org_id,
            reason="Cannot remove the last admin. Promote another user to admin first.",
        )
