"""CRUD for Account records.

Accounts are global entities (not org-scoped).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult
from modulo.db.models.account import Account


async def get_account_by_email(session: AsyncSession, email: str) -> Account | None:
    result = await session.execute(select(Account).where(Account.email == email))
    return result.scalar_one_or_none()


async def get_account_by_id(session: AsyncSession, account_id: uuid.UUID) -> Account | None:
    result = await session.execute(select(Account).where(Account.id == account_id))
    return result.scalar_one_or_none()


async def create_account(
    session: AsyncSession,
    *,
    email: str,
    display_name: str,
    password_hash: str | None = None,
    auth_provider: str = "local",
) -> Account:
    account = Account(
        email=email,
        display_name=display_name,
        password_hash=password_hash,
        auth_provider=auth_provider,
    )
    session.add(account)
    await session.flush()
    return account


async def update_account(
    session: AsyncSession,
    account_id: uuid.UUID,
    updates: dict[str, object],
) -> Account | None:
    account = await get_account_by_id(session, account_id)
    if account is None:
        return None
    from modulo.db.crud.base import apply_updates

    apply_updates(account, updates)
    await session.flush()
    return account


async def update_last_login(session: AsyncSession, account_id: uuid.UUID) -> None:
    await session.execute(update(Account).where(Account.id == account_id).values(last_login=datetime.now(UTC)))


async def update_account_preferences(
    session: AsyncSession, account_id: uuid.UUID, preferences: dict[str, object]
) -> dict[str, object]:
    account = await get_account_by_id(session, account_id)
    if account is None:
        return preferences
    merged = {**account.preferences, **preferences}
    await session.execute(update(Account).where(Account.id == account_id).values(preferences=merged))
    return merged


async def list_accounts(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
    search: str | None = None,
) -> PageResult[Account]:
    conditions: list[Any] = []
    if search:
        conditions.append(Account.email.ilike(f"%{search}%"))

    count_q = select(func.count()).select_from(Account).where(*conditions)
    total = (await session.execute(count_q)).scalar() or 0

    query = (
        select(Account).where(*conditions).order_by(Account.created_at)
        .offset((page - 1) * page_size).limit(page_size)
    )
    result = await session.execute(query)
    items = list(result.scalars().all())

    return PageResult(items=items, total=total, page=page, page_size=page_size)
