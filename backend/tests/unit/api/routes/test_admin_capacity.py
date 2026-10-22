"""Unit tests for GET /api/v1/admin/db-capacity (FAR-425/426, source FAR-1882).

Exercises the real route through the app so the authz guard and the response
model are both covered - in particular that the response carries ``source``,
the field an operator reads to tell a configured capacity from an
auto-detected one.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from tests.unit.api.mock_session import configure_rls_preamble

from modulo.api.dependencies import get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import TenantPrincipal
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

_STATUS: dict[str, Any] = {
    "capacity_percent": 87.5,
    "mode": "fixed",
    "alert_level": "warn",
    "used_bytes": 87_500_000,
    "capacity_bytes": 100_000_000,
    "source": "filesystem",
}


def _make_principal(role: str = "admin", *, is_system_admin: bool = False) -> TenantPrincipal:
    return TenantPrincipal(
        username="admin" if role == "admin" else "viewer",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=role,
        is_system_admin=is_system_admin,
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    configure_rls_preamble(session)
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=True)
    session.execute = AsyncMock(return_value=result)
    return session


def _client(principal: TenantPrincipal | None) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield _make_mock_session()

    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )
    app.dependency_overrides[get_db_session] = override_session
    if principal is not None:
        app.dependency_overrides[get_current_tenant_user] = lambda: principal
        app.dependency_overrides[get_current_user] = lambda: principal
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    yield from _client(_make_principal("admin"))


@pytest.fixture
def viewer_client() -> Generator[TestClient, None, None]:
    yield from _client(_make_principal("viewer"))


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    yield from _client(None)


def _patch_status(status: dict[str, Any]) -> Any:
    """Patch the route's status source and the shared engine it is handed."""
    return (
        patch("modulo.db.session.get_shared_engine", new=MagicMock(return_value="engine")),
        patch("modulo.api.routes.admin_capacity.db_capacity_status", new=AsyncMock(return_value=status)),
    )


def test_admin_gets_the_capacity_status(client: TestClient) -> None:
    engine_patch, status_patch = _patch_status(_STATUS)
    with engine_patch, status_patch as status_mock:
        response = client.get("/api/v1/admin/db-capacity")

    assert response.status_code == 200
    assert response.json() == _STATUS
    # The measurement runs on the process-wide shared engine, never a request session.
    assert status_mock.call_args.args == ("engine",)


@pytest.mark.parametrize("source", ["env", "filesystem", "none"])
def test_source_is_serialised(client: TestClient, source: str) -> None:
    engine_patch, status_patch = _patch_status({**_STATUS, "source": source})
    with engine_patch, status_patch:
        response = client.get("/api/v1/admin/db-capacity")

    assert response.status_code == 200
    assert response.json()["source"] == source


def test_fail_open_when_the_measurement_raises(client: TestClient) -> None:
    # A broken measurement degrades the banner to quiet - never a 5xx.
    engine_patch = patch("modulo.db.session.get_shared_engine", new=MagicMock(return_value="engine"))
    status_patch = patch(
        "modulo.api.routes.admin_capacity.db_capacity_status",
        new=AsyncMock(side_effect=RuntimeError("measurement blew up")),
    )
    with engine_patch, status_patch:
        response = client.get("/api/v1/admin/db-capacity")

    assert response.status_code == 200
    body = response.json()
    assert body["capacity_percent"] is None
    assert body["alert_level"] == "ok"
    assert body["source"] == "none"


def test_viewer_is_forbidden(viewer_client: TestClient) -> None:
    # A non-admin (even authenticated) must not read instance-wide capacity.
    response = viewer_client.get("/api/v1/admin/db-capacity")
    assert response.status_code == 403


def test_unauthenticated_is_rejected(unauth_client: TestClient) -> None:
    response = unauth_client.get("/api/v1/admin/db-capacity")
    assert response.status_code == 401
