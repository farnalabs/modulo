"""Contract round-trip tests for the in-app notification preferences endpoints (FAR-247).

Exercises ``GET/PUT /api/v1/notifications/in-app/preferences`` against a real
SQLite-backed session so the real payload shape round-trips through the real
endpoints — the contract-round-trip review gate. Covers:

* GET returns the full ``notification_opt_outs`` map for every category,
* PUT the full GET-returned map, GET it back, and assert equality
  (dashboard_level + opt-outs),
* 400 on unknown category keys, 422 on unknown top-level fields and on an
  invalid ``dashboard_level``,
* RLS/per-user scoping — one user's opt-outs never leak to another user, and
  the dashboard read paths agree with the preference map,
* FAR-2024 — the ``dashboard_level`` default the response model DECLARES is
  the default the GET path actually RETURNS (both fallbacks of
  ``_load_dashboard_level``), so the published contract cannot drift from the
  behaviour again.
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
from modulo.api.routes.in_app_notifications import (
    _DASHBOARD_LEVELS,
    NotificationPreferencesResponse,
    _load_dashboard_level,
    _preference_change_is_noop,
)
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.core.notifier.event_mapper import notification_categories
from modulo.db.models.account import Account
from modulo.db.models.base import Base
from modulo.db.models.notification import Dismissal, Notification, NotificationPreference
from modulo.db.models.org_membership import OrgMembership
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_A = uuid.UUID("00000000-0000-0000-0000-000000000002")
_USER_B = uuid.UUID("00000000-0000-0000-0000-000000000003")
_PREFERENCES_PATH = "/api/v1/notifications/in-app/preferences"
_DASHBOARD_PATH = "/api/v1/notifications/in-app/dashboard"
_UNREAD_COUNT_PATH = "/api/v1/notifications/in-app/unread-count"

# OrgMembership is required because the admin-scope visibility clause reads it
# (EXISTS) on every notification read the routes perform.
_TABLES = [
    Account.__table__,
    Notification.__table__,
    NotificationPreference.__table__,
    Dismissal.__table__,
    OrgMembership.__table__,
]

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

    # Dispose the per-test engine: an undisposed aiosqlite pool leaves its
    # ``Connection`` objects to be finalised at interpreter shutdown, which
    # pytest reports as a PytestUnraisableExceptionWarning.
    asyncio.run(engine.dispose())
    app.dependency_overrides.clear()


def test_get_preferences_returns_full_category_map(client: TestClient) -> None:
    resp = client.get(_PREFERENCES_PATH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dashboard_level"] == "info"
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
    """FAR-247: an unknown category KEY is a domain validation error (400),
    distinct from the Pydantic body-shape 422.

    The function name keeps its pre-FAR-247 suffix (the status moved 422 -> 400)
    so the test function itself is preserved rather than removed/renamed.
    """
    resp = client.put(_PREFERENCES_PATH, json={"notification_opt_outs": {"not.a.real.category": True}})
    assert resp.status_code == 400, resp.text
    assert "not.a.real.category" in resp.text


def test_put_preferences_unknown_top_level_field_returns_422(client: TestClient) -> None:
    """FAR-247: an unknown TOP-LEVEL field is a 422 (``extra='forbid'``) —
    it used to be silently ignored, answering 200 and storing nothing."""
    resp = client.put(_PREFERENCES_PATH, json={"opt_outs": {"run.failed": True}})
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


def test_dashboard_returns_info_level_notifications_by_default(client: TestClient) -> None:
    """FAR-817: the default dashboard level is 'info', so info-level
    notifications must appear on the dashboard panel."""
    # Warm the client (engine + tables created on first request), then seed.
    assert client.get(_PREFERENCES_PATH).status_code == 200

    # Seed an info-level notification via direct DB write.
    async def _seed_info() -> None:
        engine = _CREATED_ENGINES[-1]
        maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
        async with maker() as session, session.begin():
            session.add(
                Notification(
                    id=uuid.uuid4(),
                    organisation_id=_ORG_ID,
                    scope="org",
                    level="info",
                    category="deploy",
                    title="Deploy info",
                    body="deploy body",
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )

    asyncio.run(_seed_info())

    dashboard = client.get(_DASHBOARD_PATH).json()
    categories = {n["category"] for n in dashboard["notifications"]}
    assert "deploy" in categories, f"Expected info-level 'deploy' notification in dashboard, got {categories}"


def test_dashboard_respects_stored_preference(client: TestClient) -> None:
    """FAR-817: when the user has set dashboard_level='warning', info-level
    notifications are excluded from the dashboard."""
    resp = client.put(_PREFERENCES_PATH, json={"dashboard_level": "warning"})
    assert resp.status_code == 200, resp.text

    # Seed an info-level notification.
    async def _seed_info() -> None:
        engine = _CREATED_ENGINES[-1]
        maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
        async with maker() as session, session.begin():
            session.add(
                Notification(
                    id=uuid.uuid4(),
                    organisation_id=_ORG_ID,
                    scope="org",
                    level="info",
                    category="deploy",
                    title="Deploy info",
                    body="deploy body",
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )

    asyncio.run(_seed_info())

    dashboard = client.get(_DASHBOARD_PATH).json()
    categories = {n["category"] for n in dashboard["notifications"]}
    assert "deploy" not in categories, (
        f"Info-level notification should be excluded with warning preference, got {categories}"
    )


# ---------------------------------------------------------------------------
# FAR-2024: the declared response default must equal the observed default
# ---------------------------------------------------------------------------


def _declared_dashboard_default() -> object:
    """The default the PUBLISHED contract declares for ``dashboard_level``."""
    return NotificationPreferencesResponse.model_fields["dashboard_level"].default


def test_declared_dashboard_default_is_info(client: TestClient) -> None:
    """The response model declares ``dashboard_level``'s default as 'info'.

    It used to declare 'warning' while ``_load_dashboard_level`` returned
    'info' for an account with no stored value. Every real GET returns the
    loader's value, so the model default was unreachable at runtime yet still
    published in the OpenAPI schema as the contract.
    """
    assert _declared_dashboard_default() == "info"

    # A fresh account has stored nothing: the real GET path must return exactly
    # the value the contract declares, so the two can never drift.
    body = client.get(_PREFERENCES_PATH).json()
    assert body["dashboard_level"] == "info"
    assert body["dashboard_level"] == _declared_dashboard_default()


def test_default_dashboard_level_is_restorable_through_the_put_vocabulary() -> None:
    """A default the PUT validator rejects would be un-settable back.

    The default is only a coherent contract if a user may explicitly write it.
    """
    assert _declared_dashboard_default() in _DASHBOARD_LEVELS


def test_load_dashboard_level_missing_account_uses_the_declared_default() -> None:
    """The loader's missing-account branch agrees with the declared default.

    ``_load_dashboard_level`` has two fallbacks (account row absent, and
    stored value absent). Both must yield the single declared default.
    """
    with patch(
        "modulo.api.routes.in_app_notifications.get_account_by_id",
        new=AsyncMock(return_value=None),
    ):
        loaded = asyncio.run(_load_dashboard_level(MagicMock(), _USER_A))

    assert loaded == "info"
    assert loaded == _declared_dashboard_default()


# ---------------------------------------------------------------------------
# FAR-247: badge/list agreement (dashboard_level threaded into get_unread_count)
# ---------------------------------------------------------------------------


def _seed_info_notification() -> None:
    async def _seed() -> None:
        engine = _CREATED_ENGINES[-1]
        maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
        async with maker() as session, session.begin():
            session.add(
                Notification(
                    id=uuid.uuid4(),
                    organisation_id=_ORG_ID,
                    scope="org",
                    level="info",
                    category="deploy",
                    title="Deploy info",
                    body="deploy body",
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )

    asyncio.run(_seed())


def test_unread_badge_agrees_with_dashboard_list_at_default_level(client: TestClient) -> None:
    """FAR-247: badge and list agree at the DEFAULT dashboard level ('info').

    The unread count used to be computed with a hardcoded ``min_level`` of
    'warning' while the list used the user's level (default 'info'), so an
    info-level row appeared in the dashboard list but was NOT counted in the
    badge. Both reads must use the same level.
    """
    assert client.get(_PREFERENCES_PATH).status_code == 200  # warm engine + tables
    _seed_info_notification()

    dashboard = client.get(_DASHBOARD_PATH).json()
    categories = {n["category"] for n in dashboard["notifications"]}
    assert "deploy" in categories, f"expected the info-level row in the list, got {categories}"
    # 2 seeded warning rows + 1 info row — the badge counts every listed row.
    assert dashboard["total_unread"] == 3

    unread = client.get(_UNREAD_COUNT_PATH).json()
    assert unread["count"] == 3, "badge endpoint must agree with the dashboard list"


def test_unread_badge_respects_stored_dashboard_level(client: TestClient) -> None:
    """A stored dashboard_level of 'warning' filters BOTH the list and badge."""
    resp = client.put(_PREFERENCES_PATH, json={"dashboard_level": "warning"})
    assert resp.status_code == 200, resp.text
    _seed_info_notification()

    dashboard = client.get(_DASHBOARD_PATH).json()
    categories = {n["category"] for n in dashboard["notifications"]}
    assert "deploy" not in categories
    assert dashboard["total_unread"] == 2

    unread = client.get(_UNREAD_COUNT_PATH).json()
    assert unread["count"] == 2


# ---------------------------------------------------------------------------
# FAR-247: AuditEvent only on an actual change
# ---------------------------------------------------------------------------


def _success_audit_calls(emit: AsyncMock) -> list:
    return [
        call
        for call in emit.await_args_list
        if call.kwargs.get("event_type") == "notification_preferences_updated"
        and call.kwargs.get("payload", {}).get("outcome") == "success"
    ]


def test_put_with_actual_change_emits_success_audit(client: TestClient) -> None:
    with patch("modulo.core.audit_coverage._emit", new=AsyncMock()) as emit:
        resp = client.put(_PREFERENCES_PATH, json={"notification_opt_outs": {"run.failed": True}})
        assert resp.status_code == 200, resp.text
    assert len(_success_audit_calls(emit)) == 1


def test_put_no_op_change_emits_no_success_audit(client: TestClient) -> None:
    """Replaying an identical PUT (nothing changes) must NOT emit the success
    AuditEvent (FAR-247: audit only on actual change)."""
    # Warm the stored state OUTSIDE the patch: run.failed opted out, level 'info'.
    warm = {"dashboard_level": "info", "notification_opt_outs": {"run.failed": True}}
    first = client.put(_PREFERENCES_PATH, json=warm)
    assert first.status_code == 200, first.text

    with patch("modulo.core.audit_coverage._emit", new=AsyncMock()) as emit:
        replay = client.put(
            _PREFERENCES_PATH, json={"dashboard_level": "info", "notification_opt_outs": {"run.failed": True}}
        )
        assert replay.status_code == 200, replay.text
    assert not _success_audit_calls(emit)


def test_put_back_to_original_state_emits_audit_again(client: TestClient) -> None:
    """Toggling off an opt-out IS a change (row removal), even though the
    final state equals the default."""
    first = client.put(_PREFERENCES_PATH, json={"notification_opt_outs": {"run.failed": True}})
    assert first.status_code == 200, first.text

    with patch("modulo.core.audit_coverage._emit", new=AsyncMock()) as emit:
        second = client.put(_PREFERENCES_PATH, json={"notification_opt_outs": {"run.failed": False}})
        assert second.status_code == 200, second.text
    assert len(_success_audit_calls(emit)) == 1


# ---------------------------------------------------------------------------
# FAR-247: the extracted no-op detector (audit-change detection)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("requested_opt_outs", "before_opted_out", "dashboard_level", "before_level", "expected"),
    [
        # Nothing requested at all -> no change.
        ({}, {"run.failed"}, None, "info", True),
        # An opt-out that already exists -> no change.
        ({"run.failed": True}, {"run.failed"}, None, "info", True),
        # A new opt-out -> change.
        ({"run.stalled": True}, set(), None, "info", False),
        # Toggling an existing opt-out OFF -> change (row removal).
        ({"run.failed": False}, {"run.failed"}, None, "info", False),
        # An explicitly unchanged dashboard level -> no change.
        ({}, set(), "info", "info", True),
        # A changed dashboard level -> change.
        ({}, set(), "warning", "info", False),
        # dashboard_level None means the request did not carry one.
        ({}, set(), None, "warning", True),
    ],
)
def test_preference_change_is_noop(
    requested_opt_outs: dict[str, bool],
    before_opted_out: set[str],
    dashboard_level: str | None,
    before_level: str,
    expected: bool,
) -> None:
    assert _preference_change_is_noop(requested_opt_outs, before_opted_out, dashboard_level, before_level) is expected


# ---------------------------------------------------------------------------
# FAR-1825: the preferences PUT's FIRST write is the notification-preference
# DELETE/INSERT, which runs BEFORE ``update_account_preferences`` takes the
# Account row lock. The lock bound must therefore be set at the top of the
# transaction, or the preference DML's own row waits stay unbounded.
# ---------------------------------------------------------------------------


def test_put_preferences_bounds_the_row_lock_before_its_first_write(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from modulo.api.routes import in_app_notifications as mod

    order: list[str] = []

    async def _record_bound(session: object) -> None:
        order.append("bound")

    async def _record_write(**kwargs: object) -> None:
        order.append("write")

    monkeypatch.setattr(mod, "set_mutation_row_lock_timeout", _record_bound)
    monkeypatch.setattr(mod, "set_notification_preferences", _record_write)

    category = min(notification_categories())
    resp = client.put(_PREFERENCES_PATH, json={"notification_opt_outs": {category: True}})

    assert resp.status_code == 200, resp.text
    assert order[0] == "bound", f"the lock bound must run before the first write, got {order}"
    assert "write" in order
