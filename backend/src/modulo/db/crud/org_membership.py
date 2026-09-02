"""CRUD for OrgMembership records.

OrgMemberships are org-scoped: they link an Account to an Organisation.
"""

import uuid

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.break_glass_deny import denied_predicate
from modulo.db.models.account import Account
from modulo.db.models.org_membership import OrgMembership


async def get_membership_by_account_and_org(
    session: AsyncSession, account_id: uuid.UUID, org_id: uuid.UUID
) -> OrgMembership | None:
    result = await session.execute(
        select(OrgMembership).where(
            OrgMembership.account_id == account_id,
            OrgMembership.organisation_id == org_id,
        )
    )
    return result.scalar_one_or_none()


async def list_memberships_for_account(
    session: AsyncSession, account_id: uuid.UUID, *, active_only: bool = False
) -> list[OrgMembership]:
    """List the account's org memberships ordered by join date.

    With ``active_only=True`` the tombstoned memberships (``deactivated_at``
    set — the per-org deactivation signal, gh-1794/FAR-533) are excluded, so
    callers that resolve an org CONTEXT (login) never pick a deactivated org.
    """
    stmt = select(OrgMembership).where(OrgMembership.account_id == account_id)
    if active_only:
        stmt = stmt.where(OrgMembership.deactivated_at.is_(None))
    result = await session.execute(stmt.order_by(OrgMembership.joined_at))
    return list(result.scalars().all())


async def list_memberships_for_org(session: AsyncSession, org_id: uuid.UUID) -> list[OrgMembership]:
    result = await session.execute(
        select(OrgMembership).where(OrgMembership.organisation_id == org_id).order_by(OrgMembership.joined_at)
    )
    return list(result.scalars().all())


async def create_membership(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    org_id: uuid.UUID,
    role: str = "runner",
) -> OrgMembership:
    membership = OrgMembership(
        account_id=account_id,
        organisation_id=org_id,
        role=role,
    )
    session.add(membership)
    await session.flush()
    return membership


async def update_membership_role(
    session: AsyncSession,
    membership_id: uuid.UUID,
    role: str,
    *,
    org_id: uuid.UUID,
) -> OrgMembership | None:
    result = await session.execute(
        select(OrgMembership).where(
            OrgMembership.id == membership_id,
            OrgMembership.organisation_id == org_id,
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        return None
    membership.role = role
    await session.flush()
    return membership


async def resolve_role_from_membership(session: AsyncSession, account_id: str, organisation_id: str) -> str | None:
    """Return the LIVE org role for the account in the org, or None if no active membership.

    Filters ``deactivated_at IS NULL`` — a soft-deactivated membership must not
    resolve a role (ADR 017). The INNER JOIN on ``accounts`` requires
    ``accounts.active IS TRUE`` (deliverable A of the break-glass plan): an
    account deactivated globally resolves to None in every org, closing the
    account-global-deactivation latent bug. It ALSO excludes deny-eligible
    break-glass accounts via ``NOT is_break_glass_denied`` (deliverable B,
    chunk 1): an expired / NULL-expiry / deactivated / inactive break-glass
    account resolves to None, and every existing caller's None-check denies.
    INNER-JOIN semantics: a membership whose account row is missing resolves
    to None. Lives in the db layer (pure ORM query) so the service-layer
    backstop (db.crud.hitl_gate_guard) can reuse it without importing
    ``auth.dependencies`` (which would transitively reach the api layer,
    violating the import-linter contracts).
    """
    result = await session.execute(
        select(OrgMembership.role)
        .join(
            Account,
            and_(
                Account.id == OrgMembership.account_id,
                Account.active.is_(True),
                ~denied_predicate(),
            ),
        )
        .where(
            OrgMembership.account_id == account_id,
            OrgMembership.organisation_id == organisation_id,
            OrgMembership.deactivated_at.is_(None),
        )
    )
    return result.scalar_one_or_none()
