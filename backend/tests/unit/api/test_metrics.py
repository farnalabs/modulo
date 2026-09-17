"""Unit tests for POST /api/v1/metrics/web-vitals.

ADR 017: the ingest route is swept with ``metrics.ingest`` (``viewer``
minimum) so telemetry keeps working for every tenant role.

FAR-880: the summary/timeseries read endpoints now carry the same
``metrics.ingest`` dependency (viewer minimum — tenant read role).
"""

import inspect
import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

# Shared mock session reference for tests that need per-test execute results.
_mock_session: AsyncMock | None = None


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    global _mock_session
    _mock_session = configure_mock_session(AsyncMock())
    mock_session = _mock_session
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=begin_cm)
    result = MagicMock()
    result.scalar_one_or_none.return_value = True
    mock_session.execute = AsyncMock(return_value=result)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_plan_context] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()
    _mock_session = None


def _set_principal(role: str) -> None:
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="viewer", organisation_id=_ORG_ID, account_id=_USER_ID, org_role=role
    )


def test_web_vitals_viewer_allowed(client: TestClient) -> None:
    _set_principal("viewer")
    with (
        patch("modulo.api.routes.metrics.set_rls_org"),
        patch("modulo.api.routes.metrics.set_rls_user_context"),
    ):
        resp = client.post(
            "/api/v1/metrics/web-vitals",
            json={"events": [{"metric_name": "LCP", "metric_value": 1200.0}]},
        )
    assert resp.status_code == 204


def test_web_vitals_admin_allowed(client: TestClient) -> None:
    _set_principal("admin")
    with (
        patch("modulo.api.routes.metrics.set_rls_org"),
        patch("modulo.api.routes.metrics.set_rls_user_context"),
    ):
        resp = client.post(
            "/api/v1/metrics/web-vitals",
            json={"events": [{"metric_name": "CLS", "metric_value": 0.1}]},
        )
    assert resp.status_code == 204


def test_web_vitals_unauthenticated_returns_4xx(client: TestClient) -> None:
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_current_tenant_user, None)
    try:
        resp = client.post(
            "/api/v1/metrics/web-vitals",
            json={"events": [{"metric_name": "LCP", "metric_value": 1200.0}]},
        )
    finally:
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
        )
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# FAR-880: read endpoints (summary / timeseries) carry metrics.ingest
# ---------------------------------------------------------------------------


def _empty_summary_result() -> MagicMock:
    result = MagicMock()
    result.all.return_value = []
    result.scalar_one_or_none.return_value = True
    return result


def test_read_endpoints_carry_metrics_ingest_permission() -> None:
    from modulo.api.routes import metrics

    for name in ("get_web_vitals_summary", "get_web_vitals_timeseries"):
        params = inspect.signature(getattr(metrics, name)).parameters
        dep = params["current_user"].default
        assert getattr(dep, "permission", None) == "metrics.ingest"
        assert getattr(dep, "permission_kind", None) == "tenant"


def test_web_vitals_summary_viewer_allowed(client: TestClient) -> None:
    _set_principal("viewer")
    assert _mock_session is not None
    _mock_session.execute = AsyncMock(return_value=_empty_summary_result())
    with (
        patch("modulo.api.routes.metrics.set_rls_org"),
        patch("modulo.api.routes.metrics.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/metrics/web-vitals/summary?days=7")
    assert resp.status_code == 200
    assert not resp.json()


def test_web_vitals_summary_unauthenticated_returns_4xx(client: TestClient) -> None:
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_current_tenant_user, None)
    try:
        resp = client.get("/api/v1/metrics/web-vitals/summary?days=7")
    finally:
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
        )
    assert resp.status_code in (401, 403)


def test_web_vitals_timeseries_viewer_allowed(client: TestClient) -> None:
    _set_principal("viewer")
    assert _mock_session is not None
    _mock_session.execute = AsyncMock(return_value=_empty_summary_result())
    with (
        patch("modulo.api.routes.metrics.set_rls_org"),
        patch("modulo.api.routes.metrics.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/metrics/web-vitals/timeseries?metric_name=LCP&days=7")
    assert resp.status_code == 200
    assert not resp.json()


def test_web_vitals_timeseries_unauthenticated_returns_4xx(client: TestClient) -> None:
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_current_tenant_user, None)
    try:
        resp = client.get("/api/v1/metrics/web-vitals/timeseries?metric_name=LCP&days=7")
    finally:
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
        )
    assert resp.status_code in (401, 403)
