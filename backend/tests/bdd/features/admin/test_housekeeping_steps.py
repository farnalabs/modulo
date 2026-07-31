"""Step definitions for admin housekeeping BDD scenarios."""

import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.housekeeping import Candidate, CategoryResult
from modulo.settings import Settings, get_settings

scenarios("housekeeping.feature")

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _sample_candidate() -> Candidate:
    return Candidate(
        id=str(uuid.uuid4()),
        name="stale-key",
        detail="Orphan secret — no connector or agent references this key",
        created_at="2026-01-01T00:00:00+00:00",
        entity_type="secret",
    )


def _sample_results() -> list[CategoryResult]:
    return [
        CategoryResult(category="orphan_secrets", candidates=[_sample_candidate()]),
        CategoryResult(category="empty_teams", candidates=[]),
    ]


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _role_client(role: str | None, session: AsyncMock | None = None) -> TestClient:
    """Build a TestClient with an explicitly managed tenant principal.

    ``role`` is the org role for ``get_current_tenant_user``; ``None`` removes
    the override so the real HTTPBearer dependency rejects the request.
    """
    app.dependency_overrides.pop(get_current_tenant_user, None)
    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
        modulo_license_key="test-license-key",
        modulo_csrf_enabled=False,
    )

    if session is None:
        session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_db_session] = override_session
    if role is not None:
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


def _request(role: str | None, method: str, url: str, session: AsyncMock | None = None, **kwargs: Any) -> Any:
    client = _role_client(role, session)
    try:
        return client.request(method, url, **kwargs)
    finally:
        _clean_overrides()


@given("I am authenticated as an admin")
def _bdd_auth_admin() -> None:
    """No-op — the ``when`` steps build an admin-principal TestClient."""


@given("I am authenticated as a non-admin user")
def _bdd_auth_non_admin(request) -> None:
    """Flag scenario for the viewer client."""
    request.node._viewer_auth = True


@given("I am not authenticated")
def _bdd_not_authenticated(request) -> None:
    """Flag scenario for the unauthenticated client."""
    request.node._unauth = True


@when("I request GET /api/v1/admin/housekeeping")
def _bdd_get_housekeeping(request) -> None:
    node = request.node
    if getattr(node, "_unauth", False):
        resp = _request(None, "GET", "/api/v1/admin/housekeeping")
        request.node._resp = resp
        return
    if getattr(node, "_viewer_auth", False):
        resp = _request("viewer", "GET", "/api/v1/admin/housekeeping")
        request.node._resp = resp
        return
    with (
        patch("modulo.api.routes.admin_housekeeping.scan_all", return_value=_sample_results()) as mock_scan,
        patch("modulo.api.routes.admin_housekeeping.set_rls_org") as mock_rls,
    ):
        resp = _request("admin", "GET", "/api/v1/admin/housekeeping")
    request.node._resp = resp
    request.node._mock_scan = mock_scan
    request.node._mock_rls = mock_rls


@then("the housekeeping response contains categories and total_count")
def _bdd_check_housekeeping_fields(request) -> None:
    data = request.node._resp.json()
    assert "categories" in data
    assert "total_count" in data
    assert data["total_count"] == 1
    assert len(data["categories"]) == 2


@then("every candidate includes an entity_type")
def _bdd_check_candidate_entity_type(request) -> None:
    data = request.node._resp.json()
    for cat in data["categories"]:
        for candidate in cat["candidates"]:
            assert "entity_type" in candidate
            assert candidate["entity_type"] == "secret"


@then("the housekeeping scan applies the organisation RLS context")
def _bdd_check_rls_applied(request) -> None:
    assert request.node._mock_rls.called


@when(parsers.parse("I cleanup housekeeping items with entity types {entity_types}"))
def _bdd_cleanup(request, entity_types: str) -> None:
    types = [t.strip() for t in entity_types.split(",") if t.strip()]
    items = [{"id": str(uuid.uuid4()), "entity_type": t} for t in types]

    if getattr(request.node, "_unauth", False):
        resp = _request(None, "POST", "/api/v1/admin/housekeeping/cleanup", json={"items": items})
        request.node._resp = resp
        return
    if getattr(request.node, "_viewer_auth", False):
        resp = _request("viewer", "POST", "/api/v1/admin/housekeeping/cleanup", json={"items": items})
        request.node._resp = resp
        return

    if types and types[0] == "does_not_exist":
        with patch("modulo.api.routes.admin_housekeeping.set_rls_org") as mock_rls:
            resp = _request("admin", "POST", "/api/v1/admin/housekeeping/cleanup", json={"items": items})
        request.node._resp = resp
        request.node._mock_rls = mock_rls
        return

    with patch("modulo.api.routes.admin_housekeeping.set_rls_org") as mock_rls:
        session = _make_mock_session()
        session.begin_nested = MagicMock(return_value=_make_begin_nested())
        execute_result = MagicMock()
        execute_result.scalar_one_or_none = MagicMock(return_value=MagicMock())
        session.execute = AsyncMock(return_value=execute_result)
        session.delete = AsyncMock(return_value=None)
        resp = _request("admin", "POST", "/api/v1/admin/housekeeping/cleanup", session=session, json={"items": items})
    request.node._resp = resp
    request.node._mock_rls = mock_rls


def _make_begin_nested() -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@then("the cleanup response reports the deleted count")
def _bdd_check_cleanup_deleted(request) -> None:
    data = request.node._resp.json()
    assert data["deleted_count"] == 2
    assert data["errors"] == []


@then("the cleanup deletes items scoped to the organisation")
def _bdd_check_cleanup_rls(request) -> None:
    assert request.node._mock_rls.called


@then("the cleanup response reports an unknown entity type error")
def _bdd_check_cleanup_unknown_type(request) -> None:
    data = request.node._resp.json()
    assert data["deleted_count"] == 0
    assert len(data["errors"]) == 1
    assert "Unknown entity type" in data["errors"][0]["error"]
