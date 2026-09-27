"""Notification lifecycle read-path defects — TTL (3) and scope dismissal (4).

Both defects live in the WHERE clause of the read path, so both are reproduced
against the REAL endpoints on a real SQLite session (no CRUD mocking):

3. **``expires_at`` was decorative.** Rows are written with a 72h/168h TTL but
   no read path filtered on it, so a notification days past its expiry still
   counted as active on the dashboard, in the inbox and in the unread badge.

4. **``dismiss_scope='scope'`` only hid the row for the dismissing user.** The
   active filter keyed on ``dismissed_by_user_id`` alone and ignored
   ``dismiss_scope``, so an org-wide dismissal never applied to anyone else.

PROVE-THE-FIX: every assertion below is an assertion the pre-fix code fails —
the expired row IS served, and the other user DOES still see a scope-dismissed
row, until the clauses are added.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.db.models.account import Account
from modulo.db.models.base import Base
from modulo.db.models.notification import Dismissal, Notification, NotificationPreference
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
#: Admin — performs the dismissals.
_USER_A = uuid.UUID("00000000-0000-0000-0000-000000000002")
#: Runner — a SECOND user in the same org, never dismisses anything.
_USER_B = uuid.UUID("00000000-0000-0000-0000-000000000003")

_BASE = "/api/v1/notifications/in-app"
_DASHBOARD_PATH = f"{_BASE}/dashboard"
_ACTIVE = f"{_BASE}?page=1&page_size=20&status=active"
_UNREAD = f"{_BASE}/unread-count"

_TABLES = [
    Account.__table__,
    Notification.__table__,
    NotificationPreference.__table__,
    Dismissal.__table__,
]

_TTL_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_LIVE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a2")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


def _account(account_id: uuid.UUID, *, email: str) -> Account:
    now = datetime.now(UTC)
    return Account(
        id=account_id,
        email=email,
        display_name=email.split("@", maxsplit=1)[0],
        auth_provider="local",
        preferences={},
        active=True,
        is_system_admin=False,
        is_break_glass=False,
        created_at=now,
        updated_at=now,
    )


def _notification(
    *,
    notification_id: uuid.UUID,
    expires_at: datetime,
    title: str,
    dismiss_strategy: str = "any_scope",
    category: str = "hitl.awaiting",
) -> Notification:
    now = datetime.now(UTC)
    return Notification(
        id=notification_id,
        organisation_id=_ORG_ID,
        scope="org",
        level="warning",
        category=category,
        title=title,
        body='Pipeline "Improve Security" is waiting for human review.',
        action_url=None,
        dismiss_strategy=dismiss_strategy,
        dismissible_at_scope=True,
        expires_at=expires_at,
        created_at=now - timedelta(hours=1),
        updated_at=now - timedelta(hours=1),
    )


@contextmanager
def _notification_client(
    tmp_path: Path,
    *,
    notifications: Sequence[Notification],
) -> Generator[tuple[TestClient, SimpleNamespace], None, None]:
    """Real-SQLite TestClient over the in-app notification routes.

    Yields ``(client, actor)``; mutate ``actor.account_id`` / ``actor.org_role``
    to switch which user is acting (defect 4 needs two users in one test).
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'notif_lifecycle.db'}", echo=False)
    seeded = False
    rows = list(notifications)

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        nonlocal seeded
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
        maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
        async with maker() as session:
            if not seeded:
                async with session.begin():
                    session.add(_account(_USER_A, email="a@example.com"))
                    session.add(_account(_USER_B, email="b@example.com"))
                    for row in rows:
                        session.add(row)
                seeded = True
            yield session

    actor = SimpleNamespace(account_id=_USER_A, username="user-a", org_role="admin")

    def _current_user() -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(
            username=actor.username,
            organisation_id=_ORG_ID,
            account_id=actor.account_id,
            org_role=actor.org_role,
        )

    def _current_tenant_user() -> TenantPrincipal:
        return TenantPrincipal(
            username=actor.username,
            organisation_id=_ORG_ID,
            account_id=actor.account_id,
            org_role=actor.org_role,
        )

    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = _current_user
    app.dependency_overrides[get_current_tenant_user] = _current_tenant_user
    app.dependency_overrides[get_plan_context] = lambda: mock_plan

    try:
        with (
            patch("modulo.api.routes.in_app_notifications.set_rls_org", new=AsyncMock()),
            patch("modulo.api.routes.in_app_notifications.set_rls_user_context", new=AsyncMock()),
        ):
            yield TestClient(app), actor
    finally:
        app.dependency_overrides.clear()


def _ttl_client(tmp_path: Path) -> AbstractContextManager[tuple[TestClient, SimpleNamespace]]:
    """One EXPIRED ``hitl.awaiting`` row and one still-live sibling."""
    now = datetime.now(UTC)
    return _notification_client(
        tmp_path,
        notifications=[
            _notification(
                notification_id=_TTL_ID,
                expires_at=now - timedelta(hours=1),
                title="HITL review needed — Improve Security (expired)",
            ),
            _notification(
                notification_id=_LIVE_ID,
                expires_at=now + timedelta(hours=72),
                title="HITL review needed — Improve Security (live)",
            ),
        ],
    )


def _items(resp: Response) -> list[dict[str, object]]:
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    return list(payload.get("notifications") or payload.get("items") or [])


def _ids(resp) -> list[str]:
    return [str(row["id"]) for row in _items(resp)]


# ---------------------------------------------------------------------------
# (3) expires_at TTL is enforced in the read paths
# ---------------------------------------------------------------------------


def test_expired_notification_is_not_served_on_the_dashboard(tmp_path: Path) -> None:
    with _ttl_client(tmp_path) as (client, _actor):
        resp = client.get(_DASHBOARD_PATH)

    assert _ids(resp) == [str(_LIVE_ID)]
    assert resp.json()["total_unread"] == 1


def test_expired_notification_is_not_served_in_the_active_inbox(tmp_path: Path) -> None:
    with _ttl_client(tmp_path) as (client, _actor):
        resp = client.get(_ACTIVE)

    assert _ids(resp) == [str(_LIVE_ID)]
    # The page total must agree with the page items — the count path carries
    # the same TTL clause as the list path.
    assert resp.json()["total"] == 1


def test_expired_notification_is_not_served_in_the_default_inbox(tmp_path: Path) -> None:
    """The API default (no ``status``) is a live view too."""
    with _ttl_client(tmp_path) as (client, _actor):
        resp = client.get(f"{_BASE}?page=1&page_size=20")

    assert _ids(resp) == [str(_LIVE_ID)]
    assert resp.json()["total"] == 1


def test_unread_count_excludes_expired(tmp_path: Path) -> None:
    with _ttl_client(tmp_path) as (client, _actor):
        resp = client.get(_UNREAD)

    assert resp.status_code == 200, resp.text
    assert resp.json()["count"] == 1


def test_live_notification_is_still_served(tmp_path: Path) -> None:
    """Control: the TTL clause must not hide a row that has NOT expired."""
    with _ttl_client(tmp_path) as (client, _actor):
        dashboard = _ids(client.get(_DASHBOARD_PATH))
        active = _ids(client.get(_ACTIVE))

    assert dashboard == [str(_LIVE_ID)]
    assert active == [str(_LIVE_ID)]


def test_expired_notification_is_still_retrievable_via_explicit_filter(tmp_path: Path) -> None:
    """Semantics: TTL drops expired rows from the LIVE views only — an explicit
    historical filter (here ``dismissed_self``) still retrieves them."""
    with _ttl_client(tmp_path) as (client, _actor):
        dismiss = client.post(f"{_BASE}/{_TTL_ID}/dismiss", json={"dismiss_scope": "self"})
        assert dismiss.status_code == 200, dismiss.text

        dismissed = _ids(client.get(f"{_BASE}?page=1&page_size=20&status=dismissed_self"))

    assert dismissed == [str(_TTL_ID)]


# ---------------------------------------------------------------------------
# (4) dismiss_scope='scope' hides for everyone; 'self' stays per-user
# ---------------------------------------------------------------------------


def test_scope_dismissal_hides_the_notification_for_every_user(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    with _notification_client(
        tmp_path,
        notifications=[
            _notification(notification_id=_LIVE_ID, expires_at=now + timedelta(hours=72), title="any_scope row"),
        ],
    ) as (client, actor):
        # User A sees it, then dismisses it for EVERYONE.
        assert _ids(client.get(_ACTIVE)) == [str(_LIVE_ID)]

        dismiss = client.post(f"{_BASE}/{_LIVE_ID}/dismiss", json={"dismiss_scope": "scope"})
        assert dismiss.status_code == 200, dismiss.text
        assert dismiss.json() == {"status": "dismissed_for_everyone"}

        # Hidden for the dismissing user…
        assert not _ids(client.get(_ACTIVE))
        assert not _items(client.get(_DASHBOARD_PATH))
        assert client.get(_UNREAD).json()["count"] == 0

        # …and for the OTHER user in scope (the defect: this used to stay visible).
        actor.account_id = _USER_B
        actor.username = "user-b"
        actor.org_role = "runner"

        assert not _ids(client.get(_ACTIVE))
        assert not _items(client.get(_DASHBOARD_PATH))
        assert client.get(_UNREAD).json()["count"] == 0


def test_self_dismissal_stays_per_user(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    with _notification_client(
        tmp_path,
        notifications=[
            _notification(notification_id=_LIVE_ID, expires_at=now + timedelta(hours=72), title="self row"),
        ],
    ) as (client, actor):
        dismiss = client.post(f"{_BASE}/{_LIVE_ID}/dismiss", json={"dismiss_scope": "self"})
        assert dismiss.status_code == 200, dismiss.text
        assert dismiss.json() == {"status": "dismissed_for_self"}

        # Hidden for the dismissing user only.
        assert not _ids(client.get(_ACTIVE))
        assert not _items(client.get(_DASHBOARD_PATH))
        assert client.get(_UNREAD).json()["count"] == 0

        # The other user still sees it — a per-user dismissal must not leak.
        actor.account_id = _USER_B
        actor.username = "user-b"
        actor.org_role = "runner"

        assert _ids(client.get(_ACTIVE)) == [str(_LIVE_ID)]
        assert _ids(client.get(_DASHBOARD_PATH)) == [str(_LIVE_ID)]
        assert client.get(_UNREAD).json()["count"] == 1
