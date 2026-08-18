"""Unit tests for notification read-time opt-outs (FAR-247).

Covers ``apply_prefs_filter`` (the shared read-path helper) and the
``get_opted_out_categories`` / ``set_notification_preferences`` CRUD
round-trip against an in-memory SQLite database.
"""

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import sqlite
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.notifier.event_mapper import _EVENT_CONFIG, notification_categories
from modulo.db.crud.notifications import (
    apply_prefs_filter,
    get_opted_out_categories,
    set_notification_preferences,
)
from modulo.db.models.base import Base
from modulo.db.models.notification import Notification, NotificationPreference

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER = uuid.UUID("00000000-0000-0000-0000-000000000002")

# The 13 categories named in the FAR-247 ticket — must be a subset of what is
# derived at runtime from _EVENT_CONFIG.
_TICKET_CATEGORIES = {
    "hitl.awaiting",
    "hitl.claim_expired",
    "hitl.overdue",
    "hitl.gate_removed",
    "hitl.gate_removal_denied",
    "run.failed",
    "run.stalled",
    "run.budget_exceeded",
    "eval.regression",
    "eval.blocked",
    "feedback.pending",
    "system.announcement",
    "triggers.auto_deactivated",
}


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn, tables=[Notification.__table__, NotificationPreference.__table__]
            )
        )
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


def test_notification_categories_derived_from_event_config() -> None:
    expected = {cfg["category"] for cfg in _EVENT_CONFIG.values()}
    assert notification_categories() == expected
    assert notification_categories() >= _TICKET_CATEGORIES


def test_apply_prefs_filter_adds_opt_out_subquery() -> None:
    user_id = uuid.uuid4()
    q = select(Notification).where(Notification.organisation_id == _ORG)
    filtered = apply_prefs_filter(q, account_id=user_id)

    sql = str(filtered.compile(dialect=sqlite.dialect()))
    assert "NOT IN (SELECT notification_preferences.category" in sql.replace("\n", " ")
    assert "notification_preferences.account_id = ?" in sql
    assert "notification_preferences.organisation_id = notifications.organisation_id" in sql.replace("\n", " ")


async def test_empty_opt_outs_returns_empty_set(session: AsyncSession) -> None:
    assert not await get_opted_out_categories(session, org_id=_ORG, account_id=_USER)


async def test_set_then_get_round_trip(session: AsyncSession) -> None:
    await set_notification_preferences(
        session,
        org_id=_ORG,
        account_id=_USER,
        opt_outs={"run.failed": True, "eval.regression": True},
    )
    await session.commit()
    assert await get_opted_out_categories(session, org_id=_ORG, account_id=_USER) == {"run.failed", "eval.regression"}


async def test_opt_in_false_removes_row(session: AsyncSession) -> None:
    await set_notification_preferences(session, org_id=_ORG, account_id=_USER, opt_outs={"run.failed": True})
    await set_notification_preferences(session, org_id=_ORG, account_id=_USER, opt_outs={"run.failed": False})
    await session.commit()
    assert not await get_opted_out_categories(session, org_id=_ORG, account_id=_USER)


async def test_partial_update_leaves_untouched_keys(session: AsyncSession) -> None:
    await set_notification_preferences(
        session, org_id=_ORG, account_id=_USER, opt_outs={"run.failed": True, "run.stalled": True}
    )
    await set_notification_preferences(session, org_id=_ORG, account_id=_USER, opt_outs={"run.failed": False})
    await session.commit()
    assert await get_opted_out_categories(session, org_id=_ORG, account_id=_USER) == {"run.stalled"}


async def test_opt_outs_are_scoped_per_org(session: AsyncSession) -> None:
    other_org = uuid.UUID("00000000-0000-0000-0000-00000000ffff")
    await set_notification_preferences(session, org_id=_ORG, account_id=_USER, opt_outs={"run.failed": True})
    await session.commit()
    assert not await get_opted_out_categories(session, org_id=other_org, account_id=_USER)
