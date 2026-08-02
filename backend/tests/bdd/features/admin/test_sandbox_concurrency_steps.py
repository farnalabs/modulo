"""Step definitions for the admin sandbox-concurrency BDD feature.

The admin endpoint uses ``get_current_tenant_user``, so each request builds a
TestClient with an explicit tenant-principal override and a mock session whose
org row exposes a mutable ``settings_json`` (mirrors the housekeeping BDD
pattern).
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.settings import Settings, get_settings

scenarios("sandbox_concurrency.feature")

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _make_org_session(org_settings: dict) -> AsyncMock:
    org = MagicMock()
    org.id = _ORG_ID
    org.settings_json = org_settings

    result = MagicMock()
    result.scalar_one_or_none.return_value = org

    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    nested_cm = AsyncMock()
    nested_cm.__aenter__ = AsyncMock(return_value=None)
    nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.begin_nested = MagicMock(return_value=nested_cm)
    session.execute.return_value = result
    session.flush = AsyncMock()
    return session


def _role_client(role: str, session: AsyncMock) -> TestClient:
    app.dependency_overrides.pop(get_current_tenant_user, None)
    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
        modulo_license_key="test-license-key",
        modulo_csrf_enabled=False,
    )

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username=role,
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=role,
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    return TestClient(app)


def _clean_overrides() -> None:
    app.dependency_overrides.pop(get_current_tenant_user, None)
    app.dependency_overrides.pop(get_db_session, None)
    app.dependency_overrides.pop(get_settings, None)
    app.dependency_overrides.pop(get_plan_context, None)


def _request(
    request: Any,
    role: str,
    method: str,
    url: str,
    org_settings: dict,
    **kwargs: Any,
) -> Any:
    session = _make_org_session(org_settings)
    request.node._org_settings = org_settings
    request.node._session = session
    client = _role_client(role, session)
    try:
        return client.request(method, url, **kwargs)
    finally:
        _clean_overrides()


@given("I am authenticated as an admin")
def _bdd_auth_admin() -> None:
    """No-op — the ``when`` steps build an admin-principal TestClient."""


@given("the organisation sandbox concurrency limit is 3")
def _bdd_org_limit_set(request) -> None:
    request.node._org_settings = {"sandbox_concurrency_limit": 3}


@given("the organisation settings include a license key")
def _bdd_org_license(request) -> None:
    request.node._org_settings = {"license_key": "license-abc"}


@when("I request GET /api/v1/admin/org/sandbox-concurrency")
def _bdd_get_limit(request) -> None:
    org_settings = getattr(request.node, "_org_settings", {})
    if getattr(request.node, "_viewer_auth", False):
        resp = _request(request, "viewer", "GET", "/api/v1/admin/org/sandbox-concurrency", org_settings)
    else:
        resp = _request(request, "admin", "GET", "/api/v1/admin/org/sandbox-concurrency", org_settings)
    request.node._resp = resp


@when(parsers.parse("I PUT /api/v1/admin/org/sandbox-concurrency with limit {limit}"))
def _bdd_put_limit(request, limit: str) -> None:
    org_settings = getattr(request.node, "_org_settings", {})
    value = None if limit == "null" else int(limit)
    resp = _request(
        request,
        "admin",
        "PUT",
        "/api/v1/admin/org/sandbox-concurrency",
        org_settings,
        json={"sandbox_concurrency_limit": value},
    )
    request.node._resp = resp


@then("the sandbox concurrency limit is null")
def _bdd_limit_null(request) -> None:
    data = request.node._resp.json()
    assert data["sandbox_concurrency_limit"] is None


@then(parsers.parse("the sandbox concurrency limit is {limit:d}"))
def _bdd_limit_value(request, limit: int) -> None:
    data = request.node._resp.json()
    assert data["sandbox_concurrency_limit"] == limit


@then("the organisation still has its license key")
def _bdd_org_license_preserved(request) -> None:
    org = request.node._session.execute.return_value.scalar_one_or_none.return_value
    assert org.settings_json.get("license_key") == "license-abc"
