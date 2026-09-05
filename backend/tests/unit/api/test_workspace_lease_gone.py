"""Unit tests for the deliberate-410 GET /runs/{id}/workspace-lease route (FAR-587).

The WorkspaceLease scaffolding was removed; the route is kept out of the
OpenAPI schema but answers a typed 410 so existing clients see an intentional
contract change, not an accidental 404.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, _get_session_factory, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_RUN_ID = uuid.uuid4()

_EXPECTED_410_DETAIL = (
    "The workspace lease API was removed; workspace state is tracked via the run's sandbox dispatch state."
)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=begin_cm)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[_get_session_factory] = lambda: MagicMock()
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
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)

    app.dependency_overrides.clear()


def test_workspace_lease_route_answers_typed_410(client: TestClient) -> None:
    response = client.get(f"/api/v1/runs/{_RUN_ID}/workspace-lease")

    assert response.status_code == 410
    body: dict[str, Any] = response.json()
    assert body["detail"] == _EXPECTED_410_DETAIL


def test_workspace_lease_route_is_absent_from_openapi_schema(client: TestClient) -> None:
    schema = app.openapi()
    lease_paths = [path for path in schema["paths"] if "workspace-lease" in path]

    assert lease_paths == []
