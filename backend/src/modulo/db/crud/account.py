"""CRUD for Account records.

Accounts are global entities (not org-scoped).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.account import Account
from modulo.util.emails import normalize_email


async def get_account_by_email(session: AsyncSession, email: str) -> Account | None:
    """Look an account up by email, case-insensitively (FAR-584).

    The comparison canonicalises the submitted email and lowercases the stored
    column, so a case-variant of an existing address resolves to the same
    account — including pre-0177 rows that still hold mixed-case spellings.
    Postgres satisfies this via the ``uq_accounts_email_lower`` functional
    unique index.
    """
    result = await session.execute(select(Account).where(func.lower(Account.email) == normalize_email(email)))
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
        email=normalize_email(email),
        display_name=display_name,
        password_hash=password_hash,
        auth_provider=auth_provider,
    )
    session.add(account)
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
    current = account.preferences if isinstance(account.preferences, dict) else {}
    merged = {**current, **preferences}
    await session.execute(update(Account).where(Account.id == account_id).values(preferences=merged))
    return merged
