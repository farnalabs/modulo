"""Unit tests for Notification Endpoint CRUD API routes."""

import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER = uuid.UUID("00000000-0000-0000-0000-000000000002")
_VALID_32 = "a" * 32
_FERNET_KEY = Fernet.generate_key().decode()


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_FERNET_KEY,
        modulo_admin_password="testpass",
    )


def _make_session() -> AsyncMock:
    session = AsyncMock()
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.in_transaction = MagicMock(return_value=True)
    session.get_bind = MagicMock(return_value=bind)
    session.info = {}
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.delete = MagicMock()
    return session


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    session = _make_session()

    # Wire up execute() to return a result with scalars
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.__iter__ = lambda self: iter([])
    scalar_result.scalar_one_or_none.return_value = None
    scalar_result.first.return_value = None
    session.execute = AsyncMock(return_value=scalar_result)

    # Simulate session.get() returning None by default (not found)
    async def _get(model: Any, pk: Any) -> Any:
        return None

    session.get = _get

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG,
        account_id=_USER,
        org_role="operator",
    )
    test_client = TestClient(app)
    test_client.cookies.set("XSRF-TOKEN", "test-csrf-token")
    test_client.headers["X-CSRF-Token"] = "test-csrf-token"
    yield test_client
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /api/v1/notifications
# ---------------------------------------------------------------------------


def test_list_endpoints_empty(client: TestClient) -> None:
    resp = client.get("/api/v1/notifications")
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# POST /api/v1/notifications
# ---------------------------------------------------------------------------


def test_create_endpoint_success(client: TestClient) -> None:
    payload = {
        "url": "https://hooks.example.com/notify",
        "secret": "my-hmac-secret",
        "events": ["hitl_awaiting", "run_failed"],
        "description": "My webhook",
    }
    resp = client.post("/api/v1/notifications", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["url"] == payload["url"]
    assert data["events"] == payload["events"]
    assert data["description"] == "My webhook"


def test_create_endpoint_minimal(client: TestClient) -> None:
    payload = {"url": "https://hooks.example.com/notify"}
    resp = client.post("/api/v1/notifications", json=payload)
    assert resp.status_code == 201


def test_create_endpoint_rejects_relative_url(client: TestClient) -> None:
    payload = {"url": "hooks.example.com/notify"}
    resp = client.post("/api/v1/notifications", json=payload)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/v1/notifications/{id}
# ---------------------------------------------------------------------------


def test_get_endpoint_not_found_returns_404(client: TestClient) -> None:
    resp = client.get(f"/api/v1/notifications/{uuid.uuid4()}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/v1/notifications/{id}
# ---------------------------------------------------------------------------


def test_delete_endpoint_not_found_returns_404(client: TestClient) -> None:
    resp = client.delete(f"/api/v1/notifications/{uuid.uuid4()}")
    assert resp.status_code == 404
