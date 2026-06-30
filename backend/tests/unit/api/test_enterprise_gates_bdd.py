"""BDD-derived unit tests for Team gate enforcement across all 4 gated features.

Tests mirror the Gherkin scenarios in enterprise_gates.feature but run as plain
pytest unit tests (no pytest-bdd dependency), verifying that require_feature()
returns 402 for each gated feature and 200 for Community-tier and licensed requests.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_PROVIDER_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")


# ── Settings helpers ──────────────────────────────────────────────────────


def _make_settings(*, license_key: str = "") -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key=license_key,
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


# ── Client fixtures ───────────────────────────────────────────────────────


@pytest.fixture()
def free_client() -> Generator[TestClient, None, None]:
    """Client with no license key — all team features disabled."""
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = lambda: _make_settings(license_key="")
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


@pytest.fixture()
def licensed_client() -> Generator[TestClient, None, None]:
    """Client with a valid license key — all team features enabled."""
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = lambda: _make_settings(license_key="valid-license-key")
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


# ── Individual feature gating tests (402 when disabled) ───────────────────


class TestSsoGating:
    """SSO endpoints return 402 when no team license is present."""

    def test_list_providers_returns_402_on_free(self, free_client: TestClient) -> None:
        resp = free_client.get("/api/v1/admin/sso/providers")
        assert resp.status_code == 402
        assert "sso" in resp.text.lower()

    def test_list_providers_succeeds_on_licensed(self, licensed_client: TestClient) -> None:
        mock_provider = MagicMock()
        mock_provider.id = _PROVIDER_ID
        mock_provider.provider_type = "oidc"
        mock_provider.name = "Test"
        mock_provider.client_id = "cid"
        mock_provider.client_secret = "secret"
        mock_provider.discovery_url = "https://example.com"
        mock_provider.metadata_url = None
        mock_provider.metadata_xml = None
        mock_provider.entity_id = None
        mock_provider.scopes = '["openid"]'
        mock_provider.enabled = True
        mock_provider.auto_provision = True
        mock_provider.default_role = "runner"
        mock_provider.group_mappings = []
        mock_provider.created_at = None
        mock_provider.updated_at = None

        with patch("modulo.api.routes.admin_sso.list_providers", new=AsyncMock(return_value=[mock_provider])):
            resp = licensed_client.get("/api/v1/admin/sso/providers")
            assert resp.status_code == 200


class TestTeamRbacGating:
    """Team RBAC endpoints return 402 when no team license is present."""

    def test_list_teams_returns_402_on_free(self, free_client: TestClient) -> None:
        resp = free_client.get("/api/v1/teams")
        assert resp.status_code == 402
        assert "team_rbac" in resp.text.lower()

    def test_list_teams_succeeds_on_licensed(self, licensed_client: TestClient) -> None:
        mock_team = MagicMock()
        mock_team.id = _TEAM_ID
        mock_team.organisation_id = _ORG_ID
        mock_team.name = "Test Team"
        mock_team.description = None
        mock_team.created_by = _USER_ID
        mock_team.created_at = datetime(2025, 1, 1, tzinfo=UTC)

        page_result = MagicMock(items=[mock_team], total=1, page=1, page_size=20)
        with (
            patch("modulo.api.routes.teams.list_teams", return_value=page_result),
            patch("modulo.api.routes.teams.set_rls_org"),
            patch("modulo.api.routes.teams.set_rls_user_context"),
        ):
            resp = licensed_client.get("/api/v1/teams")
            assert resp.status_code == 200


class TestAuditGating:
    """Audit viewer endpoints return 402 when no team license is present."""

    def test_list_audit_returns_402_on_free(self, free_client: TestClient) -> None:
        resp = free_client.get("/api/v1/admin/audit")
        assert resp.status_code == 402
        assert "audit_viewer" in resp.text.lower()

    def test_list_audit_succeeds_on_licensed(self, licensed_client: TestClient) -> None:
        with (
            patch("modulo.api.routes.audit.list_audit_events", return_value={"items": [], "total": 0}),
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            resp = licensed_client.get("/api/v1/admin/audit")
            assert resp.status_code == 200


class TestSpendLimitsGating:
    """Spend limit endpoints return 402 when no team license is present."""

    def test_get_limits_returns_402_on_free(self, free_client: TestClient) -> None:
        resp = free_client.get("/api/v1/admin/costs/limits")
        assert resp.status_code == 402
        assert "admin_spend_limits" in resp.text.lower()

    def test_get_limits_succeeds_on_licensed(self, licensed_client: TestClient) -> None:
        org = MagicMock()
        org.id = _ORG_ID
        org.daily_spend_limit = Decimal("100.00")

        page_result = MagicMock(items=[], total=0, page=1, page_size=1000)

        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.db.crud.team.list_teams", return_value=page_result),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = licensed_client.get("/api/v1/admin/costs/limits")
            assert resp.status_code == 200


# ── Community tier features are accessible without a license ─────────────


class TestCommunityTierAccess:
    """Community tier features remain accessible without any license."""

    def test_list_pipelines_succeeds_on_free(self, free_client: TestClient) -> None:
        page_result = MagicMock(items=[], total=0, page=1, page_size=20, next_cursor=None, has_more=False)
        with (
            patch("modulo.api.routes.pipelines.list_pipelines", return_value=page_result),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
            resp = free_client.get("/api/v1/pipelines")
        assert resp.status_code == 200

    def test_changelog_succeeds_on_free(self, free_client: TestClient) -> None:
        resp = free_client.get("/api/v1/changelog")
        assert resp.status_code == 200


# ── Mixed gating: some cost endpoints gated, some free ────────────────────


class TestMixedGating:
    """Within admin/costs, the /limits endpoints are gated but /costs is not."""

    def test_costs_limits_gated_on_free(self, free_client: TestClient) -> None:
        resp = free_client.get("/api/v1/admin/costs/limits")
        assert resp.status_code == 402

    def test_costs_report_not_gated_on_free(self, free_client: TestClient) -> None:
        rows = [{"entity_id": str(_TEAM_ID), "entity_name": "Team A", "total_spend_usd": 100.0, "total_runs": 5}]
        with (
            patch("modulo.api.routes.costs.get_cost_report", return_value=rows),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = free_client.get("/api/v1/admin/costs")
        assert resp.status_code == 200
