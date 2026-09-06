"""CRUD for Account records.

Accounts are global entities (not org-scoped).

FAR-620: ALL writers of ``Account.preferences`` MUST go through the row-locked
read-merge-write helpers below. The column is a single JSON document (NOT
JSONB — no type migration, see 0129's NUL-byte hazard), so the only
serialisation mechanism between concurrent writers is the ``FOR UPDATE`` row
lock: an unlocked read-merge-write can resurrect a stale blob and silently
drop a sibling preference key that was written in between (the exact race the
me.py residue docstring used to document). Never add a new preferences writer
that reads and writes the column without taking the lock through one of these
helpers.
"""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.account import Account

_CODE_ACCOUNT_PREFERENCES = "db.crud.account.preferences"

_logger = logging.getLogger(__name__)

# Key under ``Account.preferences`` holding the HITL email-alert block
# (FAR-602). The constant lives HERE, in the db layer, so the CRUD helpers can
# use it without violating the db→core import boundary; the core module
# (``core/hitl_email_alerts.py``) and the API/MCP surfaces re-use this name —
# one literal, ONE writer of the block (:func:`set_hitl_email_preference`).
PREFERENCE_KEY = "hitl_email"


class AccountNotFoundError(LookupError):
    """Raised when a preferences write targets a missing account (404-loud).

    Replaces the silent success (returning the input unchanged) the unlocked
    ``update_account_preferences`` used to exhibit for a missing account —
    callers surface this as HTTP 404 / an MCP error dict.
    """


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


async def update_last_login(session: AsyncSession, account_id: uuid.UUID) -> None:
    await session.execute(update(Account).where(Account.id == account_id).values(last_login=datetime.now(UTC)))


async def _get_account_locked(session: AsyncSession, account_id: uuid.UUID) -> Account:
    """Fetch the account row ``FOR UPDATE`` — the preferences serialisation point.

    Raises ``AccountNotFoundError`` when the row is missing (404-loud). The
    lock is held until the CALLER's transaction commits or rolls back: this
    helper is begin-AGNOSTIC (it never opens or closes a transaction).
    """
    account = await session.get(Account, account_id, with_for_update=True)
    if account is None:
        _logger.warning(
            _CODE_ACCOUNT_PREFERENCES,
            extra={"account_id": str(account_id), "detail": "account_not_found"},
        )
        raise AccountNotFoundError(f"Account {account_id} not found")
    return account


async def update_account_preferences(
    session: AsyncSession, account_id: uuid.UUID, preferences: dict[str, object]
) -> dict[str, object]:
    """Merge *preferences* into the account's preferences blob (top-level keys).

    Row-locked read-merge-write (FAR-620): concurrent writers serialise on the
    account row, so a settings write can no longer drop a sibling key (e.g.
    ``hitl_email``) written between its read and write. Per-top-level-key
    merge — the caller builds its own key dict; an absent key means
    "untouched". Raises ``AccountNotFoundError`` for a missing account (the
    previous silent success returned the input unchanged, hiding the typo).
    Operates in the caller's transaction — never calls ``begin()``.
    """
    account = await _get_account_locked(session, account_id)
    current = account.preferences if isinstance(account.preferences, dict) else {}
    merged = {**current, **preferences}
    account.preferences = merged
    await session.flush()
    return merged


async def set_hitl_email_preference(
    session: AsyncSession,
    account_id: uuid.UUID,
    *,
    default: bool,
    pipeline_overrides: dict[str, bool] | None = None,
) -> dict[str, object]:
    """Write the account's ``hitl_email`` preference block (the SINGLE writer).

    FAR-620 shared helper used by BOTH the REST ``PUT /me/hitl-email-preferences``
    endpoint and the MCP ``set_hitl_email_alerts`` tool, so the block's write
    discipline lives in exactly one place:

    - row-locked (``FOR UPDATE``) — serialises against every other preferences
      writer, closing the cross-endpoint lost-update race;
    - ``hitl_email``-only merge — every other top-level preferences key is
      preserved untouched;
    - ``pipeline_overrides=None`` (omitted) leaves the existing overrides map
      UNCHANGED (only ``default`` is written); a dict REPLACES the whole map
      atomically (the override list is a unit — per-key patching would need
      delete semantics this helper deliberately does not have);
    - 404-loud via ``AccountNotFoundError``;
    - begin-AGNOSTIC — operates inside the caller's transaction and NEVER
      calls ``begin()`` (REST DI sessions are ``autobegin=False``; the MCP
      ``_session`` wrapper opens its own ``s.begin()`` — the helper must work
      identically in both).

    Returns the merged preferences blob.
    """
    account = await _get_account_locked(session, account_id)
    current = account.preferences if isinstance(account.preferences, dict) else {}
    block: dict[str, object] = {"default": default}
    if pipeline_overrides is not None:
        block["pipeline_overrides"] = dict(pipeline_overrides)
    else:
        existing = current.get(PREFERENCE_KEY)
        block["pipeline_overrides"] = (
            dict(existing.get("pipeline_overrides", {}))
            if isinstance(existing, dict) and isinstance(existing.get("pipeline_overrides"), dict)
            else {}
        )
    merged = {**current, PREFERENCE_KEY: block}
    account.preferences = merged
    await session.flush()
    return merged
