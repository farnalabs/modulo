"""CRUD for OrgMembership records.

OrgMemberships are org-scoped: they link an Account to an Organisation.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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


async def list_memberships_for_account(session: AsyncSession, account_id: uuid.UUID) -> list[OrgMembership]:
    result = await session.execute(
        select(OrgMembership).where(OrgMembership.account_id == account_id).order_by(OrgMembership.joined_at)
    )
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
) -> OrgMembership | None:
    result = await session.execute(select(OrgMembership).where(OrgMembership.id == membership_id))
    membership = result.scalar_one_or_none()
    if membership is None:
        return None
    membership.role = role
    await session.flush()
    return membership


async def get_primary_membership(session: AsyncSession, account_id: uuid.UUID) -> OrgMembership | None:
    result = await session.execute(
        select(OrgMembership)
        .where(OrgMembership.account_id == account_id, OrgMembership.deactivated_at.is_(None))
        .order_by(OrgMembership.joined_at)
        .limit(1)
    )
    return result.scalar_one_or_none()
