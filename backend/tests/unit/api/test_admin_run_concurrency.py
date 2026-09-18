"""Tests for the admin org run-concurrency endpoint contract.

Mirrors ``test_admin_sandbox_concurrency.py`` for the ``run_concurrency_limit``
org setting. The two caps are independent org settings that both produce the
``org_capacity_limited`` marker on deferred runs, but they are configured via
distinct endpoints.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal

ORG_ID = uuid4()
USER_ID = uuid4()


def _admin(role: str = "admin") -> TenantPrincipal:
    return TenantPrincipal(
        username="admin@test",
        organisation_id=ORG_ID,
        account_id=USER_ID,
        org_role=role,
    )


@pytest.fixture
def org_settings():
    return {}


@pytest.fixture
def mock_session(org_settings):
    """Mock session whose org row exposes a mutable settings_json."""
    org = MagicMock()
    org.id = ORG_ID
    org.settings_json = org_settings

    result = MagicMock()
    result.scalar_one_or_none.return_value = org

    session = AsyncMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    nested_cm = MagicMock()
    nested_cm.__aenter__ = AsyncMock(return_value=None)
    nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.begin_nested = MagicMock(return_value=nested_cm)
    session.execute.return_value = result
    session.flush = AsyncMock()
    return session


def _make_client(mock_session, role: str):
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True

    async def _override_tenant() -> TenantPrincipal:
        return _admin(role)

    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_db_session] = lambda: mock_session
    app.dependency_overrides[get_current_tenant_user] = _override_tenant
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def client_admin(mock_session):
    client = _make_client(mock_session, role="admin")
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def client_viewer(mock_session):
    client = _make_client(mock_session, role="viewer")
    yield client
    app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_get_defaults_to_null(client_admin):
    resp = await client_admin.get("/api/v1/admin/org/run-concurrency")
    assert resp.status_code == 200
    assert resp.json() == {"run_concurrency_limit": None}


@pytest.mark.anyio
async def test_put_sets_limit(client_admin, mock_session):
    resp = await client_admin.put(
        "/api/v1/admin/org/run-concurrency",
        json={"run_concurrency_limit": 5},
    )
    assert resp.status_code == 200
    assert resp.json() == {"run_concurrency_limit": 5}
    org = mock_session.execute.return_value.scalar_one_or_none.return_value
    assert org.settings_json["run_concurrency_limit"] == 5


@pytest.mark.anyio
@pytest.mark.parametrize("bad", [0, -1, 101])
async def test_put_rejects_out_of_range(client_admin, bad):
    resp = await client_admin.put(
        "/api/v1/admin/org/run-concurrency",
        json={"run_concurrency_limit": bad},
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_put_null_clears_limit(client_admin, org_settings):
    org_settings["run_concurrency_limit"] = 3
    resp = await client_admin.put(
        "/api/v1/admin/org/run-concurrency",
        json={"run_concurrency_limit": None},
    )
    assert resp.status_code == 200
    assert resp.json() == {"run_concurrency_limit": None}


@pytest.mark.anyio
async def test_put_merge_preserves_other_settings(client_admin, mock_session):
    org = mock_session.execute.return_value.scalar_one_or_none.return_value
    org.settings_json = {"license_key": "license-abc", "sandbox_concurrency_limit": 4}
    resp = await client_admin.put(
        "/api/v1/admin/org/run-concurrency",
        json={"run_concurrency_limit": 7},
    )
    assert resp.status_code == 200
    assert org.settings_json["license_key"] == "license-abc"
    assert org.settings_json["sandbox_concurrency_limit"] == 4
    assert org.settings_json["run_concurrency_limit"] == 7


@pytest.mark.anyio
async def test_viewer_forbidden_on_get(client_viewer):
    resp = await client_viewer.get("/api/v1/admin/org/run-concurrency")
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_viewer_forbidden_on_put(client_viewer):
    resp = await client_viewer.put(
        "/api/v1/admin/org/run-concurrency",
        json={"run_concurrency_limit": 3},
    )
    assert resp.status_code == 403


def _raise(exc: Exception):
    async def _raises(*_args, **_kwargs):
        raise exc

    return _raises


@pytest.mark.anyio
async def test_get_programming_error_returns_501(client_admin):
    """Migration-required errors on GET surface as 501."""
    with patch(
        "modulo.api.routes.admin.get_org_run_concurrency_limit",
        new=_raise(ProgrammingError("stmt", {}, Exception("missing table"))),
    ):
        resp = await client_admin.get("/api/v1/admin/org/run-concurrency")
    assert resp.status_code == 501


@pytest.mark.anyio
async def test_get_sqlalchemy_error_returns_503(client_admin):
    """Generic SQLAlchemy errors on GET surface as 503."""
    with patch(
        "modulo.api.routes.admin.get_org_run_concurrency_limit",
        new=_raise(SQLAlchemyError("mock", {}, "")),
    ):
        resp = await client_admin.get("/api/v1/admin/org/run-concurrency")
    assert resp.status_code == 503


@pytest.mark.anyio
async def test_get_integrity_error_returns_503_not_409(client_admin):
    """The read-only GET has no IntegrityError→409 'already exists' mapping."""
    with patch(
        "modulo.api.routes.admin.get_org_run_concurrency_limit",
        new=_raise(IntegrityError("stmt", {}, Exception("fk violation"))),
    ):
        resp = await client_admin.get("/api/v1/admin/org/run-concurrency")
    assert resp.status_code == 503
    assert "already exists" not in resp.text
