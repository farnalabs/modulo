"""SCIM 2.0 provisioning CRUD — maps SCIM resources to internal Account/OrgMembership models.

Users  → Account + OrgMembership
Groups → internal Team, with members → TeamMembership
"""

import uuid

from sqlalchemy import func, or_, select, text
from sqlalchemy import update as sa_update
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.account import get_account_by_email, get_account_by_id
from modulo.db.crud.base import apply_updates
from modulo.db.crud.org_membership import create_membership, get_membership_by_account_and_org
from modulo.db.crud.team import create_team, delete_team, get_team, update_team
from modulo.db.crud.team_membership import (
    add_team_member,
    get_membership_by_team_and_account,
    remove_team_member,
)
from modulo.db.models.account import Account
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.team import Team
from modulo.db.models.team_membership import TeamMembership

# ── User provisioning ─────────────────────────────────────────────────


async def scim_create_user(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    email: str,
    display_name: str,
    active: bool = True,
    org_role: str = "runner",
) -> Account:
    existing = await get_account_by_email(session, email)
    if existing is not None:
        membership = await get_membership_by_account_and_org(session, existing.id, org_id)
        if membership is None:
            await create_membership(
                session,
                account_id=existing.id,
                org_id=org_id,
                role=org_role,
            )
        elif membership.deactivated_at is not None:
            # Re-create reversibility: an IdP delete-then-recreate must not 409
            # forever. The membership is tombstoned (deactivated_at set), so
            # clear the tombstone, re-activate the account, and reset the hash
            # (SCIM-managed accounts authenticate via the IdP/SSO flow — a
            # password login on a NULL-hash SCIM account is 401 by design).
            membership.deactivated_at = None
            existing.password_hash = None
        existing.active = active
        await session.flush()
        return existing

    account = Account(
        email=email,
        display_name=display_name,
        active=active,
        auth_provider="scim",
        password_hash=None,
    )
    session.add(account)
    await session.flush()

    await create_membership(
        session,
        account_id=account.id,
        org_id=org_id,
        role=org_role,
    )
    return account


async def scim_get_user(session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> Account | None:
    account = await get_account_by_id(session, user_id)
    if account is None:
        return None
    membership = await get_membership_by_account_and_org(session, user_id, org_id)
    if membership is None:
        return None
    return account


async def scim_update_user(
    session: AsyncSession,
    account: Account,
    *,
    org_id: uuid.UUID,
    email: str | None = None,
    display_name: str | None = None,
    active: bool | None = None,
    org_role: str | None = None,
) -> Account:
    updates: dict[str, object] = {}
    if email is not None:
        updates["email"] = email
    if display_name is not None:
        updates["display_name"] = display_name
    if active is not None:
        updates["active"] = active
    apply_updates(account, updates)
    if org_role is not None:
        await session.execute(
            sa_update(OrgMembership)
            .where(
                OrgMembership.account_id == account.id,
                OrgMembership.organisation_id == org_id,
            )
            .values(role=org_role)
        )
    await session.flush()
    return account


async def scim_deactivate_user(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    caller_account_id: uuid.UUID,
) -> Account | None:
    """Tombstone a SCIM-managed user via the caller-bound deactivate_break_glass.

    The SECURITY DEFINER revokes families/keys scoped to the caller's shared
    orgs, tombstones the membership(s) (``deactivated_at`` — the per-org
    deactivation signal, gh-1794/FAR-533; the global ``accounts.active`` flip
    is operator/break-glass only), and applies the destructive tombstone for
    break-glass targets. Atomic single statement; raises M2010/M2020/M2040 via
    custom ERRCODE that the route maps to SCIM status codes.
    """
    account = await scim_get_user(session, org_id, user_id)
    if account is None:
        return None
    await session.execute(
        text("SELECT public.deactivate_break_glass(:caller, :target, false)"),
        {"caller": caller_account_id, "target": user_id},
    )
    await session.refresh(account)
    return account


async def scim_delete_user_by_id(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    caller_account_id: uuid.UUID,
) -> bool:
    """SCIM DELETE parity — tombstone via deactivate_break_glass, not hard-delete.

    The membership row is soft-deactivated (deactivated_at set) so an IdP
    delete-then-recreate is reversible (see scim_create_user). Returns False
    when the user does not exist in the org.
    """
    account = await scim_get_user(session, org_id, user_id)
    if account is None:
        return False
    await session.execute(
        text("SELECT public.deactivate_break_glass(:caller, :target, false)"),
        {"caller": caller_account_id, "target": user_id},
    )
    return True


async def scim_list_users(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    filter_str: str | None = None,
    start_index: int = 1,
    count: int = 20,
) -> tuple[list[Account], int]:
    conditions = [OrgMembership.organisation_id == org_id]
    if filter_str:
        like = f"%{filter_str}%"
        conditions.append(or_(Account.email.ilike(like), Account.display_name.ilike(like)))

    try:
        count_q = (
            select(func.count())
            .select_from(OrgMembership)
            .join(Account, Account.id == OrgMembership.account_id)
            .where(*conditions)
        )
        total = (await session.execute(count_q)).scalar() or 0

        query = (
            select(Account)
            .join(OrgMembership, Account.id == OrgMembership.account_id)
            .where(*conditions)
            .order_by(Account.created_at)
            .offset(max(0, start_index - 1))
            .limit(count)
        )
        result = await session.execute(query)
        items = list(result.scalars().all())
    except ProgrammingError:
        return [], 0
    return items, total


# ── Group (Team) provisioning ────────────────────────────────────────


_NIL_UUID = uuid.UUID(int=0)


async def scim_create_group(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    display_name: str,
    account_id: uuid.UUID | None = None,
    description: str | None = None,
) -> Team:
    if account_id is None:
        account_id = _NIL_UUID
    return await create_team(
        session,
        org_id=org_id,
        name=display_name,
        account_id=account_id,
        description=description,
    )


async def scim_get_group(session: AsyncSession, team_id: uuid.UUID) -> Team | None:
    return await get_team(session, team_id)


async def scim_update_group(
    session: AsyncSession,
    team: Team,
    *,
    name: str | None = None,
) -> Team | None:
    updates: dict[str, object] = {}
    if name is not None:
        updates["name"] = name
    if not updates:
        return team
    return await update_team(session, team.id, updates)


async def scim_delete_group_by_id(session: AsyncSession, team_id: uuid.UUID) -> bool:
    return await delete_team(session, team_id)


async def scim_list_groups(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    filter_str: str | None = None,
    start_index: int = 1,
    count: int = 20,
) -> tuple[list[Team], int]:
    conditions = [Team.organisation_id == org_id, Team.deleted_at.is_(None)]
    if filter_str:
        like = f"%{filter_str}%"
        conditions.append(Team.name.ilike(like))

    try:
        count_q = select(func.count()).select_from(Team).where(*conditions)
        total = (await session.execute(count_q)).scalar() or 0

        query = select(Team).where(*conditions).order_by(Team.created_at).offset(max(0, start_index - 1)).limit(count)
        result = await session.execute(query)
        items = list(result.scalars().all())
    except ProgrammingError:
        return [], 0
    return items, total


# ── Group membership mapping ─────────────────────────────────────────


async def scim_add_group_member(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str = "member",
) -> TeamMembership:
    existing = await get_membership_by_team_and_account(session, team_id, user_id)
    if existing is not None:
        return existing
    return await add_team_member(session, org_id=org_id, team_id=team_id, account_id=user_id, role=role)


async def scim_remove_group_member(session: AsyncSession, team_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    membership = await get_membership_by_team_and_account(session, team_id, user_id)
    if membership is None:
        return False
    await remove_team_member(session, membership.id)
    return True


async def scim_list_group_members(session: AsyncSession, team_id: uuid.UUID) -> list[TeamMembership]:
    result = await session.execute(select(TeamMembership).where(TeamMembership.team_id == team_id))
    return list(result.scalars().all())
