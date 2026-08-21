"""CRUD for UserMembership records.

UserMemberships link Accounts to CustomerAccounts with a role.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.customer_account import CustomerAccount
from modulo.db.models.user_membership import UserMembership


async def get_membership_by_account_and_ca(
    session: AsyncSession, account_id: uuid.UUID, ca_id: uuid.UUID
) -> UserMembership | None:
    result = await session.execute(
        select(UserMembership).where(
            UserMembership.account_id == account_id,
            UserMembership.account_id == ca_id,
        )
    )
    return result.scalar_one_or_none()


async def list_memberships_for_account(session: AsyncSession, account_id: uuid.UUID) -> list[UserMembership]:
    result = await session.execute(
        select(UserMembership).where(UserMembership.account_id == account_id).order_by(UserMembership.created_at)
    )
    return list(result.scalars().all())


async def list_memberships_for_ca(session: AsyncSession, ca_id: uuid.UUID) -> list[UserMembership]:
    result = await session.execute(
        select(UserMembership).where(UserMembership.account_id == ca_id).order_by(UserMembership.created_at)
    )
    return list(result.scalars().all())


async def create_membership(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    ca_id: uuid.UUID,
    role: str = "member",
) -> UserMembership:
    membership = UserMembership(
        account_id=ca_id,
        user_id=account_id,
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
    ca_id: uuid.UUID,
) -> UserMembership | None:
    result = await session.execute(
        select(UserMembership).where(
            UserMembership.id == membership_id,
            UserMembership.account_id == ca_id,
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        return None
    membership.role = role
    await session.flush()
    return membership


async def resolve_account_for_user(session: AsyncSession, user_id: uuid.UUID) -> CustomerAccount | None:
    """Return the CustomerAccount for the given user via their membership, or None.

    Returns the first active account membership (ordered by created_at).
    A user with no active membership returns None.
    """
    result = await session.execute(
        select(CustomerAccount)
        .join(UserMembership, UserMembership.account_id == CustomerAccount.id)
        .where(
            UserMembership.user_id == user_id,
            CustomerAccount.status == "active",
        )
        .order_by(UserMembership.created_at)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def resolve_role_for_user_in_ca(session: AsyncSession, user_id: uuid.UUID, ca_id: uuid.UUID) -> str | None:
    """Return the role for the user in the given CustomerAccount, or None."""
    result = await session.execute(
        select(UserMembership.role).where(
            UserMembership.user_id == user_id,
            UserMembership.account_id == ca_id,
        )
    )
    return result.scalar_one_or_none()
