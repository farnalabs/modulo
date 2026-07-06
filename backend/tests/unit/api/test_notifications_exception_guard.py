"""Unit tests for admin_notifications exception guards: Exception→500 on all DB routes."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_WEBHOOK_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_DELIVERY_ID = uuid.UUID("00000000-0000-0000-0000-000000000020")
_FERNET_KEY = Fernet.generate_key().decode()


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key=_FERNET_KEY,
        modulo_admin_password="testpass",
    )


def _make_mock_endpoint(**overrides: object) -> MagicMock:
    ep = MagicMock()
    ep.id = overrides.get("id", _WEBHOOK_ID)
    ep.organisation_id = _ORG_ID
    ep.url = overrides.get("url", "https://hooks.example.com/notify")
    ep.secret_ciphertext = overrides.get("secret_ciphertext")
    ep.events = overrides.get("events", '["hitl_awaiting"]')
    ep.description = overrides.get("description")
    ep.auto_disabled = overrides.get("auto_disabled", False)
    ep.consecutive_dead_letter_count = overrides.get("consecutive_dead_letter_count", 0)
    ep.created_by = _USER_ID
    return ep


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()
    mock_session.get = AsyncMock(return_value=_make_mock_endpoint())

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


_RAISE_EXC = Exception("boom")


@pytest.mark.parametrize(
    "route,method",
    [
        ("/api/v1/admin/notifications", "GET"),
        ("/api/v1/admin/notifications/deliveries", "GET"),
        ("/api/v1/admin/notifications/deliveries/retry-all-failed", "POST"),
    ],
)
def test_list_routes_exception_returns_500(route: str, method: str, client: TestClient) -> None:
    with patch("modulo.api.routes.admin_notifications.set_rls_org", side_effect=_RAISE_EXC):
        resp = client.request(method, route)
    assert resp.status_code == 500, f"{method} {route}: expected 500, got {resp.status_code}"


@pytest.mark.parametrize(
    "route,method",
    [
        ("/api/v1/admin/notifications", "POST"),
        ("/api/v1/admin/notifications/00000000-0000-0000-0000-000000000010", "GET"),
        ("/api/v1/admin/notifications/00000000-0000-0000-0000-000000000010", "PUT"),
        ("/api/v1/admin/notifications/00000000-0000-0000-0000-000000000010", "DELETE"),
        ("/api/v1/admin/notifications/00000000-0000-0000-0000-000000000010/test", "POST"),
        ("/api/v1/admin/notifications/00000000-0000-0000-0000-000000000010/re-enable", "POST"),
        ("/api/v1/admin/notifications/00000000-0000-0000-0000-000000000010/deliveries", "GET"),
    ],
)
def test_webhook_routes_exception_returns_500(route: str, method: str, client: TestClient) -> None:
    body = None
    if method == "PUT":
        body = {}
    elif method == "POST" and route == "/api/v1/admin/notifications":
        body = {"url": "https://example.com/hook", "events": ["hitl_awaiting"]}
    with patch("modulo.api.routes.admin_notifications.set_rls_org", side_effect=_RAISE_EXC):
        resp = client.request(method, route, json=body)
    assert resp.status_code == 500, f"{method} {route}: expected 500, got {resp.status_code}"


def test_retry_delivery_exception_returns_500(client: TestClient) -> None:
    with patch("modulo.api.routes.admin_notifications.set_rls_org", side_effect=_RAISE_EXC):
        resp = client.post(
            f"/api/v1/admin/notifications/{_WEBHOOK_ID}/deliveries/{_DELIVERY_ID}/retry",
        )
    assert resp.status_code == 500


def test_create_webhook_returns_201(client: TestClient) -> None:
    with patch("modulo.api.routes.admin_notifications.set_rls_org"):
        resp = client.post(
            "/api/v1/admin/notifications",
            json={"url": "https://example.com/hook", "events": ["hitl_awaiting"]},
        )
    assert resp.status_code == 201
