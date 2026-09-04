"""Route-level tests for the in-app notification endpoints (FAR-574).

Complements ``test_in_app_notifications_preferences.py`` (which exercises the
preferences endpoints against a real SQLite session) by covering the remaining
dashboard/unread/list/detail/dismiss/review-later surfaces with mocked CRUD â€”
happy paths, the route error convention (ProgrammingErrorâ†'501,
IntegrityErrorâ†'409, SQLAlchemyErrorâ†'500), and the 400/404 client-error paths.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_tenant_user_or_api_key, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_NOTIFICATION_ID = uuid.uuid4()
_BASE = "/api/v1/notifications/in-app"

_PATCHES = [
    "set_rls_org",
    "set_rls_user_context",
    "get_dashboard_notifications",
    "get_unread_count",
    "get_notifications_for_user",
    "count_notifications_for_user",
    "get_notification",
    "dismiss_notification",
    "review_later",
    "get_opted_out_categories",
    "get_account_by_id",
    "update_account_preferences",
    "set_notification_preferences",
]


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


def _make_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _notification_row() -> MagicMock:
    n = MagicMock()
    n.id = _NOTIFICATION_ID
    n.scope = "org"
    n.level = "warning"
    n.category = "run.failed"
    n.title = "Run failed"
    n.body = "body text"
    n.action_url = None
    n.dismiss_strategy = "user_only"
    n.dismissible_at_scope = False
    n.created_at = datetime.now(UTC)
    n.expires_at = None
    return n


class _TestHarness:
    """Session + patched CRUD helpers for one test."""

    def __init__(self) -> None:
        self.session = _make_session()
        self.event_bus = MagicMock()
        self.event_bus.publish = AsyncMock()
        self.patches = [patch(f"modulo.api.routes.in_app_notifications.{name}", new=AsyncMock()) for name in _PATCHES]
        self.patches.append(patch("modulo.api.routes.in_app_notifications.get_event_bus", return_value=self.event_bus))

    def __enter__(self) -> Self:
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *args: object) -> None:
        for p in self.patches:
            p.stop()

    def stub(self, name: str, value: object) -> None:
        import modulo.api.routes.in_app_notifications as route

        setattr(route, name, value)


@pytest.fixture
def client() -> Generator[tuple[TestClient, _TestHarness], None, None]:
    harness = _TestHarness()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield harness.session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    app.dependency_overrides[get_current_tenant_user_or_api_key] = lambda: TenantPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )

    with harness:
        yield TestClient(app), harness

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /dashboard
# ---------------------------------------------------------------------------


def test_dashboard_returns_notifications_and_unread(client: tuple[TestClient, _TestHarness]) -> None:
    http, harness = client
    harness.stub("get_dashboard_notifications", AsyncMock(return_value=[_notification_row()]))
    harness.stub("get_unread_count", AsyncMock(return_value=3))

    resp = http.get(f"{_BASE}/dashboard")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_unread"] == 3
    assert len(body["notifications"]) == 1
    item = body["notifications"][0]
    assert item["id"] == str(_NOTIFICATION_ID)
    assert item["scope_label"] == "Org-wide"
    assert item["category"] == "run.failed"


def test_dashboard_empty_returns_empty_list(client: tuple[TestClient, _TestHarness]) -> None:
    http, harness = client
    harness.stub("get_dashboard_notifications", AsyncMock(return_value=[]))
    harness.stub("get_unread_count", AsyncMock(return_value=0))

    resp = http.get(f"{_BASE}/dashboard")

    assert resp.status_code == 200
    body = resp.json()
    assert not body["notifications"]
    assert body["total_unread"] == 0


def test_dashboard_programming_error_maps_to_501(client: tuple[TestClient, _TestHarness]) -> None:
    http, harness = client
    harness.stub("get_dashboard_notifications", AsyncMock(side_effect=ProgrammingError("s", {}, Exception())))

    resp = http.get(f"{_BASE}/dashboard")

    assert resp.status_code == 501


def test_dashboard_integrity_error_maps_to_409(client: tuple[TestClient, _TestHarness]) -> None:
    http, harness = client
    harness.stub("get_dashboard_notifications", AsyncMock(side_effect=IntegrityError("s", {}, Exception())))

    resp = http.get(f"{_BASE}/dashboard")

    assert resp.status_code == 409


def test_dashboard_sqlalchemy_error_maps_to_500(client: tuple[TestClient, _TestHarness]) -> None:
    http, harness = client
    harness.stub("get_dashboard_notifications", AsyncMock(side_effect=SQLAlchemyError("boom")))

    resp = http.get(f"{_BASE}/dashboard")

    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# GET /unread-count
# ---------------------------------------------------------------------------


def test_unread_count_returns_count(client: tuple[TestClient, _TestHarness]) -> None:
    http, harness = client
    harness.stub("get_unread_count", AsyncMock(return_value=7))

    resp = http.get(f"{_BASE}/unread-count")

    assert resp.status_code == 200
    assert resp.json() == {"count": 7}


def test_unread_count_programming_error_maps_to_501(client: tuple[TestClient, _TestHarness]) -> None:
    http, harness = client
    harness.stub("get_unread_count", AsyncMock(side_effect=ProgrammingError("s", {}, Exception())))

    resp = http.get(f"{_BASE}/unread-count")

    assert resp.status_code == 501


def test_unread_count_sqlalchemy_error_maps_to_500(client: tuple[TestClient, _TestHarness]) -> None:
    http, harness = client
    harness.stub("get_unread_count", AsyncMock(side_effect=SQLAlchemyError("boom")))

    resp = http.get(f"{_BASE}/unread-count")

    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# GET "" (paginated list)
# ---------------------------------------------------------------------------


def test_list_notifications_returns_page(client: tuple[TestClient, _TestHarness]) -> None:
    http, harness = client
    harness.stub("get_notifications_for_user", AsyncMock(return_value=[_notification_row()]))
    harness.stub("count_notifications_for_user", AsyncMock(return_value=11))

    resp = http.get(_BASE, params={"page": 2, "page_size": 1, "level": "warning", "status": "unread"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 11
    assert body["page"] == 2
    assert body["page_size"] == 1
    assert len(body["items"]) == 1


def test_list_notifications_rejects_page_zero(client: tuple[TestClient, _TestHarness]) -> None:
    http, _harness = client

    resp = http.get(_BASE, params={"page": 0})

    assert resp.status_code == 422


def test_list_notifications_rejects_oversized_page_size(client: tuple[TestClient, _TestHarness]) -> None:
    http, _harness = client

    resp = http.get(_BASE, params={"page_size": 101})

    assert resp.status_code == 422


def test_list_notifications_programming_error_maps_to_501(client: tuple[TestClient, _TestHarness]) -> None:
    http, harness = client
    harness.stub("get_notifications_for_user", AsyncMock(side_effect=ProgrammingError("s", {}, Exception())))

    resp = http.get(_BASE)

    assert resp.status_code == 501


def test_list_notifications_sqlalchemy_error_maps_to_500(client: tuple[TestClient, _TestHarness]) -> None:
    http, harness = client
    harness.stub("count_notifications_for_user", AsyncMock(side_effect=SQLAlchemyError("boom")))

    resp = http.get(_BASE)

    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# GET /{notification_id}
# ---------------------------------------------------------------------------


def test_get_notification_detail_returns_row(client: tuple[TestClient, _TestHarness]) -> None:
    http, harness = client
    harness.stub("get_notification", AsyncMock(return_value=_notification_row()))

    resp = http.get(f"{_BASE}/{_NOTIFICATION_ID}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "Run failed"


def test_get_notification_detail_missing_returns_404(client: tuple[TestClient, _TestHarness]) -> None:
    http, harness = client
    harness.stub("get_notification", AsyncMock(return_value=None))

    resp = http.get(f"{_BASE}/{_NOTIFICATION_ID}")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Notification not found"


def test_get_notification_detail_sqlalchemy_error_maps_to_500(client: tuple[TestClient, _TestHarness]) -> None:
    http, harness = client
    harness.stub("get_notification", AsyncMock(side_effect=SQLAlchemyError("boom")))

    resp = http.get(f"{_BASE}/{_NOTIFICATION_ID}")

    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# POST /{notification_id}/review-later
# ---------------------------------------------------------------------------


def test_review_later_returns_status_and_publishes(client: tuple[TestClient, _TestHarness]) -> None:
    http, harness = client
    harness.stub("review_later", AsyncMock(return_value=None))

    resp = http.post(f"{_BASE}/{_NOTIFICATION_ID}/review-later")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "review_later"}
    harness.event_bus.publish.assert_awaited_once()
    assert harness.event_bus.publish.await_args.kwargs["action"] == "review_later"


def test_review_later_value_error_maps_to_400(client: tuple[TestClient, _TestHarness]) -> None:
    http, harness = client
    harness.stub("review_later", AsyncMock(side_effect=ValueError("not dismissible")))

    resp = http.post(f"{_BASE}/{_NOTIFICATION_ID}/review-later")

    assert resp.status_code == 400
    assert "not dismissible" in resp.json()["detail"]


def test_review_later_publish_failure_still_succeeds(client: tuple[TestClient, _TestHarness]) -> None:
    http, harness = client
    harness.stub("review_later", AsyncMock(return_value=None))
    harness.event_bus.publish = AsyncMock(side_effect=RuntimeError("bus down"))

    resp = http.post(f"{_BASE}/{_NOTIFICATION_ID}/review-later")

    assert resp.status_code == 200
    assert resp.json() == {"status": "review_later"}


def test_review_later_integrity_error_maps_to_409(client: tuple[TestClient, _TestHarness]) -> None:
    http, harness = client
    harness.stub("review_later", AsyncMock(side_effect=IntegrityError("s", {}, Exception())))

    resp = http.post(f"{_BASE}/{_NOTIFICATION_ID}/review-later")

    assert resp.status_code == 409


def test_review_later_sqlalchemy_error_maps_to_500(client: tuple[TestClient, _TestHarness]) -> None:
    http, harness = client
    harness.stub("review_later", AsyncMock(side_effect=SQLAlchemyError("boom")))

    resp = http.post(f"{_BASE}/{_NOTIFICATION_ID}/review-later")

    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# POST /{notification_id}/dismiss
# ---------------------------------------------------------------------------


def test_dismiss_self_returns_for_self(client: tuple[TestClient, _TestHarness]) -> None:
    http, harness = client
    harness.stub("dismiss_notification", AsyncMock(return_value=None))

    resp = http.post(f"{_BASE}/{_NOTIFICATION_ID}/dismiss", json={"dismiss_scope": "self"})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "dismissed_for_self"}
    assert harness.event_bus.publish.await_args.kwargs["action"] == "dismissed"


def test_dismiss_scope_requires_admin_role() -> None:
    harness = _TestHarness()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield harness.session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="runner",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="runner",
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="runner",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="runner",
    )
    try:
        with harness:
            # The route passes is_admin=principal.org_role == "admin" to the
            # CRUD helper; the CRUD helper rejects scope dismissal for
            # non-admins with ValueError, which the route maps to 400.
            harness.stub("dismiss_notification", AsyncMock(side_effect=ValueError("admin only")))
            http = TestClient(app)
            resp = http.post(f"{_BASE}/{_NOTIFICATION_ID}/dismiss", json={"dismiss_scope": "scope"})
        assert resp.status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_dismiss_rejects_invalid_scope(client: tuple[TestClient, _TestHarness]) -> None:
    http, _harness = client

    resp = http.post(f"{_BASE}/{_NOTIFICATION_ID}/dismiss", json={"dismiss_scope": "everyone"})

    assert resp.status_code == 422


def test_dismiss_value_error_maps_to_400(client: tuple[TestClient, _TestHarness]) -> None:
    http, harness = client
    harness.stub("dismiss_notification", AsyncMock(side_effect=ValueError("already dismissed")))

    resp = http.post(f"{_BASE}/{_NOTIFICATION_ID}/dismiss", json={"dismiss_scope": "self"})

    assert resp.status_code == 400


def test_dismiss_publish_failure_still_succeeds(client: tuple[TestClient, _TestHarness]) -> None:
    http, harness = client
    harness.stub("dismiss_notification", AsyncMock(return_value=None))
    harness.event_bus.publish = AsyncMock(side_effect=RuntimeError("bus down"))

    resp = http.post(f"{_BASE}/{_NOTIFICATION_ID}/dismiss", json={"dismiss_scope": "scope"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "dismissed_for_everyone"}


def test_dismiss_programming_error_maps_to_501(client: tuple[TestClient, _TestHarness]) -> None:
    http, harness = client
    harness.stub("dismiss_notification", AsyncMock(side_effect=ProgrammingError("s", {}, Exception())))

    resp = http.post(f"{_BASE}/{_NOTIFICATION_ID}/dismiss", json={"dismiss_scope": "self"})

    assert resp.status_code == 501


def test_dismiss_sqlalchemy_error_maps_to_500(client: tuple[TestClient, _TestHarness]) -> None:
    http, harness = client
    harness.stub("dismiss_notification", AsyncMock(side_effect=SQLAlchemyError("boom")))

    resp = http.post(f"{_BASE}/{_NOTIFICATION_ID}/dismiss", json={"dismiss_scope": "self"})

    assert resp.status_code == 500
