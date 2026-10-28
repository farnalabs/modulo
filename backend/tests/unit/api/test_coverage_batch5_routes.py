"""Additional real-route coverage for API coverage batch five."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.auth.scim_auth import ScimPrincipal, get_scim_plan_context, get_scim_principal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_rls_preamble

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_VALID_32 = "a" * 32


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_public_url="https://modulo.example",
        modulo_scim_token="scim-token",
        redis_url="redis://localhost:6379/0",
    )


def _result(*, rows: list[object] | None = None, scalar: object = None) -> MagicMock:
    result = MagicMock()
    result.all.return_value = rows or []
    result.scalars.return_value.all.return_value = rows or []
    result.scalar_one.return_value = scalar
    result.scalar_one_or_none.return_value = scalar
    result.first.return_value = rows[0] if rows else None
    result.__iter__.return_value = iter(rows or [])
    return result


def _session(*results: MagicMock) -> AsyncMock:
    session = configure_rls_preamble(AsyncMock())
    begin = MagicMock()
    begin.__aenter__ = AsyncMock(return_value=None)
    begin.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin)
    values = list(results) or [_result()]
    session.execute = AsyncMock(side_effect=values + [values[-1]] * 30)
    return session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    session = _session(_result(scalar=True))

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="user", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="user", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    plan = MagicMock()
    plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: plan
    app.dependency_overrides[get_scim_principal] = lambda: ScimPrincipal(organisation_id=_ORG_ID)
    app.dependency_overrides[get_scim_plan_context] = lambda: plan
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestCommunityLibraryRoutes:
    def test_list_includes_manifest_timestamp(self) -> None:
        from fastapi import FastAPI

        from modulo.api.routes.community_library import router

        route_app = FastAPI()
        route_app.include_router(router)
        session = _session(_result(scalar=True))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        route_app.dependency_overrides[get_settings] = _settings
        route_app.dependency_overrides[get_db_session] = override_session
        route_app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
            username="user", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
        )
        with (
            patch(
                "modulo.api.routes.community_library._community_objects_enabled",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "modulo.api.routes.community_library.list_community_entries",
                new=AsyncMock(return_value=[{"slug": "demo/item"}]),
            ),
            patch(
                "modulo.api.routes.community_library.get_cached_manifest",
                new=AsyncMock(return_value={"generated_at": "2026-01-01T00:00:00Z"}),
            ),
        ):
            response = TestClient(route_app).get("/api/v1/libraries/community")
        assert response.status_code == 200
        assert response.json()["synced_at"] == "2026-01-01T00:00:00Z"

    def test_get_entry_parses_content(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.community_library._community_objects_enabled",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "modulo.api.routes.community_library.get_community_entry",
                new=AsyncMock(return_value={"content_sha256": "sha"}),
            ),
            patch(
                "modulo.api.routes.community_library._fetch_entry_content",
                new=AsyncMock(return_value={"ok": True}),
            ),
        ):
            response = client.get("/api/v1/libraries/community/entry")
        assert response.status_code == 200
        assert response.json()["content"] == {"ok": True}

    def test_install_maps_fetch_failure(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.community_library._community_objects_enabled",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "modulo.api.routes.community_library.get_community_entry",
                new=AsyncMock(return_value={"id": "entry", "type": "agent"}),
            ),
            patch(
                "modulo.api.routes.community_library.install_community_entry",
                new=AsyncMock(side_effect=ValueError("blob fetch failed")),
            ),
        ):
            response = client.post("/api/v1/libraries/community/entry/install", json={})
        assert response.status_code == 502

    def test_install_unknown_entry_returns_404(self, client: TestClient) -> None:
        """An unresolvable community entry is a 404, not a 500."""
        with (
            patch(
                "modulo.api.routes.community_library._community_objects_enabled",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "modulo.api.routes.community_library.get_community_entry",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "modulo.api.routes.community_library.install_community_entry",
                new=AsyncMock(),
            ) as mock_install,
        ):
            response = client.post("/api/v1/libraries/community/entry/install", json={})
        assert response.status_code == 404
        mock_install.assert_not_called()


class TestMetricsRoutes:
    def test_empty_batch_is_no_content(self, client: TestClient) -> None:
        assert client.post("/api/v1/metrics/web-vitals", json={"events": []}).status_code == 204

    def test_invalid_metric_value_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/metrics/web-vitals", json={"events": [{"metric_name": "LCP", "metric_value": -1}]}
        )
        assert response.status_code == 422

    def test_summary_window_is_validated(self, client: TestClient) -> None:
        assert client.get("/api/v1/metrics/web-vitals/summary?days=91").status_code == 422


class TestAdminTiersRoute:
    def test_cached_response_is_returned(self, client: TestClient) -> None:
        redis = MagicMock()
        redis.get = AsyncMock(return_value='{"tiers":[{"name":"cached"}]}')
        redis.aclose = AsyncMock()
        with patch("modulo.api.routes.admin_tiers.Redis.from_url", return_value=redis):
            response = client.get("/api/v1/admin/tiers")
        assert response.status_code == 200
        assert response.json()["tiers"][0]["name"] == "cached"

    def test_programming_error_returns_501(self, client: TestClient) -> None:
        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        redis.aclose = AsyncMock()
        with (
            patch("modulo.api.routes.admin_tiers.Redis.from_url", return_value=redis),
            patch(
                "modulo.api.routes.admin_tiers.list_tiers",
                new=AsyncMock(side_effect=ProgrammingError("migration", {}, None)),
            ),
        ):
            response = client.get("/api/v1/admin/tiers")
        assert response.status_code == 501


class TestDashboardRoutes:
    def test_daily_counts_are_grouped(self, client: TestClient) -> None:
        row = MagicMock(day=date(2026, 1, 2), status="complete", cnt=3)
        session = _session(_result(rows=[row]))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        app.dependency_overrides[get_db_session] = override_session
        response = client.get("/api/v1/dashboard/daily-run-counts?days=7")
        assert response.status_code == 200
        assert response.json()["daily_counts"] == {"2026-01-02": {"complete": 3}}

    def test_trends_reject_invalid_days(self, client: TestClient) -> None:
        assert client.get("/api/v1/dashboard/trends?days=0").status_code == 422


class TestOnboardingRoutes:
    def test_invalid_action_is_rejected(self, client: TestClient) -> None:
        response = client.post("/api/v1/onboarding/actions/not-real/skip")
        assert response.status_code == 422

    def test_seed_requires_model_backend(self, client: TestClient) -> None:
        session = _session(_result(scalar=None))

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        app.dependency_overrides[get_db_session] = override_session
        response = client.post("/api/v1/onboarding/seed-examples")
        assert response.status_code == 409
        assert "no AI model backend" in response.json()["detail"]


class TestScimRoutes:
    def test_service_provider_config_is_available(self, client: TestClient) -> None:
        response = client.get("/scim/v2/ServiceProviderConfig")
        assert response.status_code == 200
        assert response.json()["patch"]["supported"] is True

    def test_list_users_preserves_pagination(self, client: TestClient) -> None:
        account = MagicMock(
            id=_USER_ID,
            email="alice@example.com",
            display_name="Alice Smith",
            active=True,
            created_at=None,
            updated_at=None,
        )
        with patch("modulo.api.routes.scim.scim_list_users", new=AsyncMock(return_value=([account], 5))):
            response = client.get("/scim/v2/Users?startIndex=3&count=1")
        assert response.status_code == 200
        assert response.json()["totalResults"] == 5
        assert response.json()["startIndex"] == 3

    def test_invalid_user_id_is_rejected(self, client: TestClient) -> None:
        assert client.get("/scim/v2/Users/not-a-uuid").status_code == 422

    def test_database_error_returns_503(self, client: TestClient) -> None:
        with patch("modulo.api.routes.scim.scim_list_users", new=AsyncMock(side_effect=SQLAlchemyError("down"))):
            response = client.get("/scim/v2/Users")
        assert response.status_code == 503


@pytest.mark.asyncio
async def test_route_error_paths_and_helpers() -> None:
    from modulo.api.routes.dashboard import _period_metric
    from modulo.api.routes.metrics import (
        WebVitalBatchItem,
        WebVitalBatchRequest,
        get_web_vitals_summary,
        get_web_vitals_timeseries,
        ingest_web_vitals,
    )

    assert _period_metric(None, 1)["delta_pct"] is None
    principal = TenantPrincipal(username="u", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin")
    session = AsyncMock()
    begin = MagicMock()
    begin.__aenter__ = AsyncMock(side_effect=SQLAlchemyError("down"))
    begin.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin)
    request = WebVitalBatchRequest(events=[WebVitalBatchItem(metric_name="LCP", metric_value=1)])
    with pytest.raises(HTTPException):
        await ingest_web_vitals(request, principal, session)
    with pytest.raises(HTTPException):
        await get_web_vitals_summary(7, principal, session)
    with pytest.raises(HTTPException):
        await get_web_vitals_timeseries("LCP", 7, principal, session)
