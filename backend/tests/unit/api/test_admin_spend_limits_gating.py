"""Unit tests for admin_spend_limits feature gating on /api/v1/admin/costs/limits endpoints."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.feature_flags import DbPlanContext, FeatureFlagRegistry
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.begin_nested = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock(
        return_value=MagicMock(
            scalar=MagicMock(return_value=0),
            scalar_one_or_none=MagicMock(return_value=None),
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))),
        )
    )
    return session


def _team_plan_context() -> DbPlanContext:
    registry = FeatureFlagRegistry(current_tier="team", has_license_key=True)
    return DbPlanContext(registry)


class _FeatureSubsetPlan:
    """Plan-context stub enabling ONLY the named features (all others disabled).

    Mirrors the PlanContext protocol (feature_enabled / list_enabled_features /
    tier / has_license_key) so a single feature can be turned on while
    ``admin_spend_limits`` stays off — proving each non-limit cost surface is
    gated by its own governing feature, not the spend-limits feature.
    """

    def __init__(self, *features: str) -> None:
        self._features = set(features)

    def feature_enabled(self, name: str) -> bool:
        return name in self._features

    def list_enabled_features(self) -> list:
        return []

    def tier(self) -> str:
        return "team"

    def has_license_key(self) -> bool:
        return True


def _build_feature_client(*features: str) -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()
    plan_ctx = _FeatureSubsetPlan(*features)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _settings_with_license
    app.dependency_overrides[get_plan_context] = lambda: plan_ctx
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def _settings_no_license() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _settings_with_license() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="valid-license-key",
    )


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _settings_no_license
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def licensed_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()
    plan_ctx = _team_plan_context()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _settings_with_license
    app.dependency_overrides[get_plan_context] = lambda: plan_ctx
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def controls_only_client() -> Generator[TestClient, None, None]:
    """Client with admin_cost_controls enabled but admin_spend_limits disabled."""
    yield from _build_feature_client("admin_cost_controls")


@pytest.fixture
def breakdown_only_client() -> Generator[TestClient, None, None]:
    """Client with admin_cost_breakdown enabled but admin_spend_limits disabled."""
    yield from _build_feature_client("admin_cost_breakdown")


# ── Feature disabled: expect 402 ──────────────────────────────────────────────


class TestSpendLimitsGatingFeatureDisabled:
    """When admin_spend_limits is disabled, limit endpoints return 402 Payment Required."""

    def test_get_spend_limits_returns_402(self, client: TestClient) -> None:
        resp = client.get("/api/v1/admin/costs/limits")
        assert resp.status_code == 402
        assert "admin_spend_limits" in resp.text

    def test_set_org_spend_limit_returns_402(self, client: TestClient) -> None:
        resp = client.put("/api/v1/admin/costs/limits/org", json={"daily_spend_limit": 100.0})
        assert resp.status_code == 402
        assert "admin_spend_limits" in resp.text

    def test_set_team_spend_limit_returns_402(self, client: TestClient) -> None:
        resp = client.put(
            f"/api/v1/admin/costs/limits/teams/{_TEAM_ID}",
            json={"daily_spend_limit": 50.0},
        )
        assert resp.status_code == 402
        assert "admin_spend_limits" in resp.text


# ── Feature enabled: requests should succeed ───────────────────────────────────


class TestSpendLimitsGatingFeatureEnabled:
    """When admin_spend_limits is enabled, limit endpoints behave normally."""

    def test_get_spend_limits_succeeds(self, licensed_client: TestClient) -> None:
        org = MagicMock()
        org.id = _ORG_ID
        org.daily_spend_limit = Decimal("100.00")

        page_result = MagicMock(items=[], total=0, page=1, page_size=1000)

        with (
            patch(
                "modulo.api.routes.costs.get_organisation",
                return_value=org,
            ),
            patch(
                "modulo.api.routes.costs.list_teams",
                return_value=page_result,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = licensed_client.get("/api/v1/admin/costs/limits")

        assert resp.status_code == 200
        assert resp.json()["org_daily_spend_limit"] == 100.0

    def test_set_org_spend_limit_succeeds(self, licensed_client: TestClient) -> None:
        org = MagicMock()
        org.id = _ORG_ID
        org.daily_spend_limit = None

        with (
            patch(
                "modulo.api.routes.costs.get_organisation",
                return_value=org,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = licensed_client.put(
                "/api/v1/admin/costs/limits/org",
                json={"daily_spend_limit": 250.0},
            )

        assert resp.status_code == 200
        assert resp.json()["daily_spend_limit"] == 250.0

    def test_set_team_spend_limit_succeeds(self, licensed_client: TestClient) -> None:
        team = MagicMock()
        team.id = _TEAM_ID
        team.organisation_id = _ORG_ID
        team.daily_spend_limit = None

        with (
            patch(
                "modulo.api.routes.costs.get_team",
                return_value=team,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = licensed_client.put(
                f"/api/v1/admin/costs/limits/teams/{_TEAM_ID}",
                json={"daily_spend_limit": 75.0},
            )

        assert resp.status_code == 200
        assert resp.json()["daily_spend_limit"] == 75.0

    def test_set_team_spend_limit_cross_org_returns_404(self, licensed_client: TestClient) -> None:
        # Principal org is _ORG_ID; get_team returns a team in a DIFFERENT org.
        other_org = uuid.UUID("00000000-0000-0000-0000-000000000099")
        team = MagicMock()
        team.id = _TEAM_ID
        team.organisation_id = other_org
        team.daily_spend_limit = Decimal("50.00")

        with (
            patch(
                "modulo.api.routes.costs.get_team",
                return_value=team,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = licensed_client.put(
                f"/api/v1/admin/costs/limits/teams/{_TEAM_ID}",
                json={"daily_spend_limit": 75.0},
            )

        assert resp.status_code == 404
        assert team.daily_spend_limit == Decimal("50.00")


# ── Non-gated endpoints should be unaffected ──────────────────────────────────


class TestNonGatedEndpoints:
    """Endpoints that are NOT spend-limit configuration should work regardless."""

    def test_get_costs_unaffected(self, licensed_client: TestClient) -> None:
        rows = [{"entity_id": str(_TEAM_ID), "entity_name": "Team A", "total_spend_usd": 100.0, "total_runs": 5}]
        with (
            patch("modulo.api.routes.costs.get_cost_report", return_value=rows),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = licensed_client.get("/api/v1/admin/costs?group_by=team&period=month")
        assert resp.status_code == 200

    def test_export_unaffected(self, licensed_client: TestClient) -> None:
        rows = [{"entity_id": str(_TEAM_ID), "entity_name": "Team A", "total_spend_usd": 100.0, "total_runs": 5}]
        with (
            patch("modulo.api.routes.costs.get_cost_report", return_value=rows),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = licensed_client.get("/api/v1/admin/costs/export?period=this_month&group_by=team&format=csv")
        assert resp.status_code == 200

    def test_create_report_unaffected(self, licensed_client: TestClient) -> None:
        mock_report = MagicMock()
        mock_report.id = uuid.uuid4()
        mock_report.period = "monthly"
        mock_report.group_by = "team"
        mock_report.format = "csv"
        mock_report.recipients = ["admin@example.com"]
        mock_report.schedule_type = "one_time"
        mock_report.created_at = datetime(2025, 1, 1, tzinfo=UTC)

        with (
            patch("modulo.api.routes.costs.create_scheduled_report", return_value=mock_report),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = licensed_client.post(
                "/api/v1/admin/costs/reports",
                json={
                    "period": "monthly",
                    "group_by": "team",
                    "format": "csv",
                    "recipients": ["admin@example.com"],
                    "schedule_type": "one_time",
                },
            )
        assert resp.status_code == 201

    def test_anomalies_unaffected(self, licensed_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.costs.list_anomalies", return_value=[]),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = licensed_client.get("/api/v1/admin/costs/anomalies")
        assert resp.status_code == 200


# ── Non-limit cost surfaces are gated by their governing feature ─────────────


class TestNonLimitCostSurfacesGatedByGoverningFeature:
    """Non-limit cost surfaces are gated by THEIR feature, not admin_spend_limits.

    Regression coverage for the sweep-added ``admin_spend_limits`` double-gate:
    a deployment with ``admin_cost_controls`` (or ``admin_cost_breakdown``)
    enabled but ``admin_spend_limits`` disabled must NOT get 402 on those
    surfaces. The /limits endpoints keep their ``admin_spend_limits`` gate.
    """

    def test_get_controls_succeeds_with_controls_but_no_spend_limits(self, controls_only_client: TestClient) -> None:
        org = MagicMock()
        org.id = _ORG_ID
        org.daily_spend_limit = None
        page_result = MagicMock(items=[], total=0, page=1, page_size=1000)

        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.list_teams", return_value=page_result),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = controls_only_client.get("/api/v1/admin/costs/controls")

        assert resp.status_code == 200, (
            f"Expected 200 with admin_cost_controls enabled (no spend limits), got {resp.status_code}"
        )

    def test_get_limits_still_402_without_spend_limits(self, controls_only_client: TestClient) -> None:
        resp = controls_only_client.get("/api/v1/admin/costs/limits")
        assert resp.status_code == 402
        assert "admin_spend_limits" in resp.text

    def test_get_anomalies_succeeds_with_breakdown_but_no_spend_limits(self, breakdown_only_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.costs.list_anomalies", return_value=[]),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = breakdown_only_client.get("/api/v1/admin/costs/anomalies")

        assert resp.status_code == 200, (
            f"Expected 200 with admin_cost_breakdown enabled (no spend limits), got {resp.status_code}"
        )

    def test_get_cost_report_succeeds_with_no_features(self, client: TestClient) -> None:
        rows = [{"entity_id": str(_TEAM_ID), "entity_name": "Team A", "total_spend_usd": 100.0, "total_runs": 5}]
        with (
            patch("modulo.api.routes.costs.get_cost_report", return_value=rows),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get("/api/v1/admin/costs?group_by=team&period=month")
        assert resp.status_code == 200, f"Expected free-tier access to the cost report, got {resp.status_code}"
