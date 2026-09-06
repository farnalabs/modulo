"""Contract round-trip tests for the in-app notification preferences endpoints (FAR-247).

Exercises ``GET/PUT /api/v1/notifications/in-app/preferences`` against a real
SQLite-backed session so the real payload shape round-trips through the real
endpoints — the contract-round-trip review gate. Covers:

* GET returns the full ``notification_opt_outs`` map for every category,
* PUT the full GET-returned map, GET it back, and assert equality
  (dashboard_level + opt-outs),
* 422 on unknown category keys and on an invalid ``dashboard_level``,
* RLS/per-user scoping — one user's opt-outs never leak to another user, and
  the dashboard read paths agree with the preference map.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.core.notifier.event_mapper import notification_categories
from modulo.db.models.account import Account
from modulo.db.models.base import Base
from modulo.db.models.notification import Dismissal, Notification, NotificationPreference
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_A = uuid.UUID("00000000-0000-0000-0000-000000000002")
_USER_B = uuid.UUID("00000000-0000-0000-0000-000000000003")
_PREFERENCES_PATH = "/api/v1/notifications/in-app/preferences"
_DASHBOARD_PATH = "/api/v1/notifications/in-app/dashboard"

_TABLES = [Account.__table__, Notification.__table__, NotificationPreference.__table__, Dismissal.__table__]

# FAR-620: the fixture's per-test engine is captured here so tests can seed
# preferences blocks the HTTP surface cannot write (e.g. a sibling
# ``hitl_email`` block) before driving the route under test.
_CREATED_ENGINES: list[AsyncEngine] = []


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


def _make_principal(account_id: uuid.UUID, username: str) -> TenantPrincipal:
    return TenantPrincipal(
        username=username,
        organisation_id=_ORG_ID,
        account_id=account_id,
        org_role="admin",
    )


@pytest.fixture
def client(tmp_path: Path) -> Generator[TestClient, None, None]:
    # A TEMP-FILE DB (not :memory:): every pooled connection - and every event
    # loop the tests borrow - sees the same data. The in-memory variant made
    # seeded rows invisible to requests that checked out a different pooled
    # connection (FAR-620: the seeded-hitl_email + dashboard-write isolation
    # test needs deterministic cross-request visibility).
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'notif.db'}", echo=False)
    # One engine per test: clear then append so _CREATED_ENGINES[-1] is
    # always the engine backing the CURRENT test's client.
    _CREATED_ENGINES.clear()
    _CREATED_ENGINES.append(engine)
    seeded = False

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        nonlocal seeded
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
        maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
        async with maker() as session:
            if not seeded:
                async with session.begin():
                    session.add_all(
                        [
                            Account(
                                id=_USER_A,
                                email="a@example.com",
                                display_name="User A",
                                auth_provider="local",
                                preferences={},
                                active=True,
                                is_system_admin=False,
                                is_break_glass=False,
                                created_at=datetime.now(UTC),
                                updated_at=datetime.now(UTC),
                            ),
                            Account(
                                id=_USER_B,
                                email="b@example.com",
                                display_name="User B",
                                auth_provider="local",
                                preferences={},
                                active=True,
                                is_system_admin=False,
                                is_break_glass=False,
                                created_at=datetime.now(UTC),
                                updated_at=datetime.now(UTC),
                            ),
                            Notification(
                                id=uuid.uuid4(),
                                organisation_id=_ORG_ID,
                                scope="org",
                                level="warning",
                                category="run.failed",
                                title="run.failed notification",
                                body="body",
                                expires_at=datetime.now(UTC) + timedelta(days=1),
                                created_at=datetime.now(UTC),
                                updated_at=datetime.now(UTC),
                            ),
                            Notification(
                                id=uuid.uuid4(),
                                organisation_id=_ORG_ID,
                                scope="org",
                                level="warning",
                                category="run.stalled",
                                title="run.stalled notification",
                                body="body",
                                expires_at=datetime.now(UTC) + timedelta(days=1),
                                created_at=datetime.now(UTC),
                                updated_at=datetime.now(UTC),
                            ),
                        ]
                    )
                seeded = True
            yield session

    active = {"account_id": _USER_A, "username": "user-a"}

    def _current_user() -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(
            username=active["username"],
            organisation_id=_ORG_ID,
            account_id=active["account_id"],
            org_role="admin",
        )

    def _current_tenant_user() -> TenantPrincipal:
        return _make_principal(active["account_id"], active["username"])

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = _current_user
    app.dependency_overrides[get_current_tenant_user] = _current_tenant_user
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan

    with (
        patch("modulo.api.routes.in_app_notifications.set_rls_org", new=AsyncMock()),
        patch("modulo.api.routes.in_app_notifications.set_rls_user_context", new=AsyncMock()),
    ):
        yield TestClient(app)

    app.dependency_overrides.clear()


def test_get_preferences_returns_full_category_map(client: TestClient) -> None:
    resp = client.get(_PREFERENCES_PATH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dashboard_level"] == "warning"
    assert set(body["notification_opt_outs"]) == set(notification_categories())
    assert not any(body["notification_opt_outs"].values())


def test_put_preferences_round_trip_get_returns_identical_map(client: TestClient) -> None:
    first = client.get(_PREFERENCES_PATH).json()

    update = {**first["notification_opt_outs"], "run.failed": True, "eval.regression": True}
    put_resp = client.put(_PREFERENCES_PATH, json={"dashboard_level": "error", "notification_opt_outs": update})
    assert put_resp.status_code == 200, put_resp.text
    put_body = put_resp.json()
    assert put_body["dashboard_level"] == "error"
    assert put_body["notification_opt_outs"]["run.failed"] is True
    assert put_body["notification_opt_outs"]["eval.regression"] is True
    assert put_body["notification_opt_outs"]["run.stalled"] is False

    get_back = client.get(_PREFERENCES_PATH)
    assert get_back.status_code == 200
    assert get_back.json() == put_body


def test_put_preferences_partial_update_preserves_untouched_keys(client: TestClient) -> None:
    first = client.get(_PREFERENCES_PATH).json()
    assert first["notification_opt_outs"]["run.stalled"] is False

    put_resp = client.put(_PREFERENCES_PATH, json={"notification_opt_outs": {"run.stalled": True}})
    assert put_resp.status_code == 200, put_resp.text
    body = put_resp.json()
    assert body["notification_opt_outs"]["run.stalled"] is True
    assert body["notification_opt_outs"]["run.failed"] is False

    get_back = client.get(_PREFERENCES_PATH).json()
    assert get_back["notification_opt_outs"]["run.stalled"] is True
    assert get_back["notification_opt_outs"]["run.failed"] is False


def test_put_preferences_unknown_category_returns_422(client: TestClient) -> None:
    resp = client.put(_PREFERENCES_PATH, json={"notification_opt_outs": {"not.a.real.category": True}})
    assert resp.status_code == 422, resp.text


def test_put_preferences_invalid_dashboard_level_returns_422(client: TestClient) -> None:
    resp = client.put(_PREFERENCES_PATH, json={"dashboard_level": "critical"})
    assert resp.status_code == 422, resp.text


def test_opt_outs_are_scoped_to_the_user_and_read_paths_agree(client: TestClient) -> None:
    put_resp = client.put(_PREFERENCES_PATH, json={"notification_opt_outs": {"run.failed": True}})
    assert put_resp.status_code == 200, put_resp.text

    me = client.get(_PREFERENCES_PATH).json()
    assert me["notification_opt_outs"]["run.failed"] is True

    dashboard = client.get(_DASHBOARD_PATH).json()
    assert {n["category"] for n in dashboard["notifications"]} == {"run.stalled"}
    assert dashboard["total_unread"] == 1

    as_other = client.app.dependency_overrides
    as_other[get_current_user] = lambda: AuthenticatedPrincipal(
        username="user-b",
        organisation_id=_ORG_ID,
        account_id=_USER_B,
        org_role="admin",
    )
    as_other[get_current_tenant_user] = lambda: _make_principal(_USER_B, "user-b")

    other = client.get(_PREFERENCES_PATH).json()
    assert other["notification_opt_outs"]["run.failed"] is False

    other_dashboard = client.get(_DASHBOARD_PATH).json()
    assert {n["category"] for n in other_dashboard["notifications"]} == {"run.failed", "run.stalled"}
    assert other_dashboard["total_unread"] == 2


# ---------------------------------------------------------------------------
# FAR-620: the row-locked dashboard-level writer (update_account_preferences)
# ---------------------------------------------------------------------------


def _seed_preferences(account_id: uuid.UUID, preferences: dict) -> None:
    """Seed an Account.preferences block the HTTP surface cannot write."""

    async def _seed() -> None:
        engine = _CREATED_ENGINES[-1]
        maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
        async with maker() as session, session.begin():
            account = await session.get(Account, account_id)
            assert account is not None
            account.preferences = preferences

    asyncio.run(_seed())


def _read_preferences(account_id: uuid.UUID) -> dict:
    async def _read() -> dict:
        engine = _CREATED_ENGINES[-1]
        maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
        async with maker() as session, session.begin():
            account = await session.get(Account, account_id)
            assert account is not None
            return account.preferences or {}

    return asyncio.run(_read())


def test_put_preferences_missing_account_returns_404(client: TestClient) -> None:
    """FAR-620: the dashboard-level write is 404-loud for a missing account -
    the locked helper raises AccountNotFoundError and the route surfaces it as
    HTTP 404 (the pre-refactor behaviour was a silent no-op success)."""
    ghost = uuid.UUID("00000000-0000-0000-0000-0000000000ee")
    overrides = client.app.dependency_overrides
    overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="ghost",
        organisation_id=_ORG_ID,
        account_id=ghost,
        org_role="admin",
    )
    overrides[get_current_tenant_user] = lambda: _make_principal(ghost, "ghost")
    try:
        resp = client.put(_PREFERENCES_PATH, json={"dashboard_level": "error"})
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Account not found"
    finally:
        overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="user-a",
            organisation_id=_ORG_ID,
            account_id=_USER_A,
            org_role="admin",
        )
        overrides[get_current_tenant_user] = lambda: _make_principal(_USER_A, "user-a")


def test_put_preferences_dashboard_write_preserves_sibling_hitl_email_key(client: TestClient) -> None:
    """FAR-620: the dashboard-level write routes through the row-locked
    helper inside the route's own transaction (the nested-txn path) and must
    preserve every sibling top-level preferences key - here a seeded
    ``hitl_email`` block survives a dashboard_level PUT byte-for-byte."""
    override_pipeline = uuid.UUID("00000000-0000-0000-0000-00000000000f")
    seeded_block = {"default": True, "pipeline_overrides": {str(override_pipeline): True}}
    # Warm the client (engine + tables created on first request), then seed.
    assert client.get(_PREFERENCES_PATH).status_code == 200
    _seed_preferences(_USER_A, {"hitl_email": seeded_block})

    resp = client.put(_PREFERENCES_PATH, json={"dashboard_level": "error"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["dashboard_level"] == "error"

    stored = _read_preferences(_USER_A)
    assert stored["notification_dashboard_level"] == "error"
    assert stored["hitl_email"] == seeded_block


def test_put_preferences_dashboard_write_happy_path_unchanged(client: TestClient) -> None:
    """The happy path is unchanged by the locked-helper migration: the
    dashboard_level round-trips and opt-outs are untouched."""
    resp = client.put(_PREFERENCES_PATH, json={"dashboard_level": "error"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dashboard_level"] == "error"
    assert not any(body["notification_opt_outs"].values())

    get_back = client.get(_PREFERENCES_PATH).json()
    assert get_back["dashboard_level"] == "error"
    assert not any(get_back["notification_opt_outs"].values())
