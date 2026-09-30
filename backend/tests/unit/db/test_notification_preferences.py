"""Unit tests for notification read-time opt-outs (FAR-247).

Covers ``apply_prefs_filter`` (the shared read-path helper) and the
``get_opted_out_categories`` / ``set_notification_preferences`` CRUD
round-trip against an in-memory SQLite database.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import sqlite
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.notifier.event_mapper import _EVENT_CONFIG, notification_categories
from modulo.db.crud.notifications import (
    apply_prefs_filter,
    count_notifications_for_user,
    create_notification,
    get_dashboard_notifications,
    get_notification,
    get_notifications_for_user,
    get_opted_out_categories,
    get_unread_count,
    set_notification_preferences,
)
from modulo.db.models.account import Account
from modulo.db.models.base import Base
from modulo.db.models.notification import Dismissal, Notification, NotificationPreference
from modulo.db.models.org_membership import OrgMembership

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER = uuid.UUID("00000000-0000-0000-0000-000000000002")
#: A second member of the same org with the VIEWER role.
_VIEWER = uuid.UUID("00000000-0000-0000-0000-000000000005")

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
                sync_conn,
                tables=[
                    Notification.__table__,
                    NotificationPreference.__table__,
                    Dismissal.__table__,
                    # The admin-scope visibility clause (FAR-1274 QA) reads
                    # org_memberships via an EXISTS subquery on every
                    # visibility read — the table must exist here.
                    OrgMembership.__table__,
                    Account.__table__,
                ],
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


async def test_opt_out_is_per_user_other_user_unaffected(session: AsyncSession) -> None:
    other_user = uuid.UUID("00000000-0000-0000-0000-000000000004")
    await create_notification(
        session,
        org_id=_ORG,
        scope="org",
        level="warning",
        category="run.failed",
        title="run.failed notification",
        body="body",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    await session.commit()

    await set_notification_preferences(session, org_id=_ORG, account_id=_USER, opt_outs={"run.failed": True})
    await session.commit()

    assert not await get_dashboard_notifications(session, org_id=_ORG, user_id=_USER, min_level="debug", limit=50)
    others = await get_notifications_for_user(session, org_id=_ORG, user_id=other_user, limit=50)
    assert {n.category for n in others} == {"run.failed"}


async def test_opted_out_category_excluded_from_all_read_paths(session: AsyncSession) -> None:
    """Prove-the-fix: an opted-out category is excluded by EVERY read path.

    Inserts notifications across categories, opts out of one, and asserts the
    dashboard list, full list, count, and unread count all agree — the core
    FAR-247 claim that the read-time filter is wired into each read path.
    """
    expires_at = datetime.now(UTC) + timedelta(days=1)
    for category in ("run.failed", "run.stalled", "eval.regression"):
        await create_notification(
            session,
            org_id=_ORG,
            scope="org",
            level="warning",
            category=category,
            title=f"{category} notification",
            body="body",
            expires_at=expires_at,
        )
    await session.commit()

    await set_notification_preferences(session, org_id=_ORG, account_id=_USER, opt_outs={"run.failed": True})
    await session.commit()

    dashboard = await get_dashboard_notifications(session, org_id=_ORG, user_id=_USER, min_level="debug", limit=50)
    assert {n.category for n in dashboard} == {"run.stalled", "eval.regression"}

    listed = await get_notifications_for_user(session, org_id=_ORG, user_id=_USER, limit=50)
    assert {n.category for n in listed} == {"run.stalled", "eval.regression"}

    assert await count_notifications_for_user(session, org_id=_ORG, user_id=_USER) == 2

    assert await get_unread_count(session, org_id=_ORG, user_id=_USER, min_level="debug") == 2


async def test_opt_outs_are_scoped_per_org(session: AsyncSession) -> None:
    other_org = uuid.UUID("00000000-0000-0000-0000-00000000ffff")
    await set_notification_preferences(session, org_id=_ORG, account_id=_USER, opt_outs={"run.failed": True})
    await session.commit()
    assert not await get_opted_out_categories(session, org_id=other_org, account_id=_USER)


# ---------------------------------------------------------------------------
# Admin-scope visibility (FAR-1274 QA): scope='admin' requires an admin role
# ---------------------------------------------------------------------------


def _seed_account(session: AsyncSession, account_id: uuid.UUID, *, email: str) -> None:
    session.add(
        Account(
            id=account_id,
            email=email,
            display_name=email.split("@", maxsplit=1)[0],
            auth_provider="local",
            preferences={},
            active=True,
            is_system_admin=False,
            is_break_glass=False,
        )
    )


@pytest.fixture
async def visibility_session(session: AsyncSession) -> AsyncSession:
    """One org with an ADMIN member and a VIEWER member, plus one admin-scoped
    and one org-scoped notification (both live)."""
    _seed_account(session, _USER, email="admin@example.com")
    _seed_account(session, _VIEWER, email="viewer@example.com")
    session.add(OrgMembership(organisation_id=_ORG, account_id=_USER, role="admin"))
    session.add(OrgMembership(organisation_id=_ORG, account_id=_VIEWER, role="viewer"))
    expires_at = datetime.now(UTC) + timedelta(days=1)
    await create_notification(
        session,
        org_id=_ORG,
        scope="admin",
        level="warning",
        category="hitl.overdue",
        title="admin alert",
        body="body",
        expires_at=expires_at,
    )
    await create_notification(
        session,
        org_id=_ORG,
        scope="org",
        level="warning",
        category="run.failed",
        title="org alert",
        body="body",
        expires_at=expires_at,
    )
    await session.commit()
    return session


class TestAdminScopeVisibility:
    """``scope='admin'`` rows are readable by org ADMINS only.

    The visibility clause used to OR ``scope == 'admin'`` UNCONDITIONALLY, so a
    viewer-role member could read admin-scoped alerts (run numbers, deep links,
    PR URLs) although ``run.output`` / ``run.list`` require a higher role. The
    admin arm now requires a LIVE admin membership in the notification's org —
    the role check ``get_notification``'s docstring always claimed.
    """

    async def test_admin_member_sees_both_rows_on_the_list(self, visibility_session: AsyncSession) -> None:
        listed = await get_notifications_for_user(visibility_session, org_id=_ORG, user_id=_USER, limit=50)
        assert {n.scope for n in listed} == {"admin", "org"}

    async def test_viewer_member_does_not_see_admin_rows_on_the_list(self, visibility_session: AsyncSession) -> None:
        listed = await get_notifications_for_user(visibility_session, org_id=_ORG, user_id=_VIEWER, limit=50)
        assert {n.scope for n in listed} == {"org"}

    async def test_viewer_dashboard_count_and_unread_exclude_admin_rows(self, visibility_session: AsyncSession) -> None:
        dashboard = await get_dashboard_notifications(
            visibility_session, org_id=_ORG, user_id=_VIEWER, min_level="debug", limit=50
        )
        assert {n.scope for n in dashboard} == {"org"}
        count = await count_notifications_for_user(visibility_session, org_id=_ORG, user_id=_VIEWER)
        assert count == 1
        unread = await get_unread_count(visibility_session, org_id=_ORG, user_id=_VIEWER, min_level="debug")
        assert unread == 1

    async def test_admin_count_and_unread_include_admin_rows(self, visibility_session: AsyncSession) -> None:
        count = await count_notifications_for_user(visibility_session, org_id=_ORG, user_id=_USER)
        assert count == 2
        unread = await get_unread_count(visibility_session, org_id=_ORG, user_id=_USER, min_level="debug")
        assert unread == 2

    async def test_detail_read_applies_the_same_role_gate(self, visibility_session: AsyncSession) -> None:
        row = (
            await visibility_session.execute(
                select(Notification.id).where(
                    Notification.organisation_id == _ORG,
                    Notification.scope == "admin",
                )
            )
        ).scalar_one()
        hidden = await get_notification(visibility_session, org_id=_ORG, notification_id=row, user_id=_VIEWER)
        assert hidden is None
        visible = await get_notification(visibility_session, org_id=_ORG, notification_id=row, user_id=_USER)
        assert visible is not None
        assert visible.scope == "admin"
