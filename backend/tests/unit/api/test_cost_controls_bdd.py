"""Unit tests for cost controls BDD step definitions.

Tests the step implementation logic directly — verifies that the step
definitions correctly exercise the underlying cost controller and API routes.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from decimal import Decimal
from typing import Any, ClassVar
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
_TEAM_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="test-license-key",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
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
def viewer_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="viewer",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="viewer",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


# ===========================================================================
# Token budget tests (future scope — step stubs skip)
# ===========================================================================


class TestTokenBudgetSteps:
    """These steps are stubs for future implementation."""

    def test_token_budget_step_raises_skip(self) -> None:
        with pytest.raises(pytest.skip.Exception):
            pytest.skip("Per-agent token budget enforcement is not yet implemented")


# ===========================================================================
# Spend limit enforcement (implemented via check_and_record_spend)
# ===========================================================================


class TestCheckAndRecordSpendSteps:
    """Tests for the step definitions that exercise check_and_record_spend."""

    def test_spend_under_org_limit_approved(self) -> None:
        """Happy path: spend under org limit is approved."""
        mock_org_count = MagicMock()
        mock_org_count.total_spend_usd = Decimal("50.00")
        mock_org_count.run_count = 5

        mock_session = _make_mock_session()

        org_limit_result = MagicMock()
        org_limit_result.scalar_one_or_none.return_value = Decimal("100.00")

        with (
            patch(
                "modulo.core.cost_controller.get_or_create_daily_count",
                return_value=mock_org_count,
            ),
            patch.object(mock_session, "execute", return_value=org_limit_result),
        ):
            import asyncio

            from modulo.core.cost_controller import check_and_record_spend

            loop = asyncio.new_event_loop()
            try:
                approved, reason = loop.run_until_complete(
                    check_and_record_spend(
                        mock_session,
                        org_id=_ORG_ID,
                        cost_usd=Decimal("30.00"),
                        team_id=None,
                    )
                )
                assert approved is True
                assert reason is None
            finally:
                loop.close()

    def test_spend_over_org_limit_rejected(self) -> None:
        """Spend exceeding org daily limit is rejected."""
        mock_org_count = MagicMock()
        mock_org_count.total_spend_usd = Decimal("95.00")
        mock_org_count.run_count = 5

        mock_session = _make_mock_session()

        org_limit_result = MagicMock()
        org_limit_result.scalar_one_or_none.return_value = Decimal("100.00")

        with (
            patch(
                "modulo.core.cost_controller.get_or_create_daily_count",
                return_value=mock_org_count,
            ),
            patch.object(mock_session, "execute", return_value=org_limit_result),
        ):
            import asyncio

            from modulo.core.cost_controller import check_and_record_spend

            loop = asyncio.new_event_loop()
            try:
                approved, reason = loop.run_until_complete(
                    check_and_record_spend(
                        mock_session,
                        org_id=_ORG_ID,
                        cost_usd=Decimal("10.00"),
                        team_id=None,
                    )
                )
                assert approved is False
                assert reason == "Daily spend limit exceeded for organisation"
            finally:
                loop.close()

    def test_spend_over_team_limit_rejected(self) -> None:
        """Spend exceeding team daily limit is rejected."""
        mock_org_count = MagicMock()
        mock_org_count.total_spend_usd = Decimal("50.00")
        mock_org_count.run_count = 5

        mock_team_count = MagicMock()
        mock_team_count.total_spend_usd = Decimal("45.00")
        mock_team_count.run_count = 3

        mock_session = _make_mock_session()

        org_limit_result = MagicMock()
        org_limit_result.scalar_one_or_none.return_value = Decimal("500.00")
        team_limit_result = MagicMock()
        team_limit_result.scalar_one_or_none.return_value = Decimal("50.00")

        with (
            patch(
                "modulo.core.cost_controller.get_or_create_daily_count",
                side_effect=[mock_org_count, mock_team_count],
            ),
            patch.object(
                mock_session,
                "execute",
                side_effect=[org_limit_result, team_limit_result],
            ),
        ):
            import asyncio

            from modulo.core.cost_controller import check_and_record_spend

            loop = asyncio.new_event_loop()
            try:
                approved, reason = loop.run_until_complete(
                    check_and_record_spend(
                        mock_session,
                        org_id=_ORG_ID,
                        cost_usd=Decimal("10.00"),
                        team_id=_TEAM_ID,
                    )
                )
                assert approved is False
                assert reason == "Daily spend limit exceeded for team"
            finally:
                loop.close()

    def test_spend_under_both_limits_approved(self) -> None:
        """Spend under both org and team limits is approved with increments."""
        mock_org_count = MagicMock()
        mock_org_count.total_spend_usd = Decimal("100.00")
        mock_org_count.run_count = 10

        mock_team_count = MagicMock()
        mock_team_count.total_spend_usd = Decimal("20.00")
        mock_team_count.run_count = 4

        mock_session = _make_mock_session()

        org_limit_result = MagicMock()
        org_limit_result.scalar_one_or_none.return_value = Decimal("500.00")
        team_limit_result = MagicMock()
        team_limit_result.scalar_one_or_none.return_value = Decimal("100.00")

        with (
            patch(
                "modulo.core.cost_controller.get_or_create_daily_count",
                side_effect=[mock_org_count, mock_team_count],
            ),
            patch.object(
                mock_session,
                "execute",
                side_effect=[org_limit_result, team_limit_result],
            ),
        ):
            import asyncio

            from modulo.core.cost_controller import check_and_record_spend

            loop = asyncio.new_event_loop()
            try:
                approved, reason = loop.run_until_complete(
                    check_and_record_spend(
                        mock_session,
                        org_id=_ORG_ID,
                        cost_usd=Decimal("30.00"),
                        team_id=_TEAM_ID,
                    )
                )
                assert approved is True
                assert reason is None
                assert mock_org_count.total_spend_usd == Decimal("130.00")
                assert mock_org_count.run_count == 11
                assert mock_team_count.total_spend_usd == Decimal("50.00")
                assert mock_team_count.run_count == 5
            finally:
                loop.close()


# ===========================================================================
# Circuit breaker tests (future scope — step stubs skip)
# ===========================================================================


class TestCircuitBreakerSteps:
    """These steps are stubs for future implementation."""

    def test_circuit_breaker_step_raises_skip(self) -> None:
        with pytest.raises(pytest.skip.Exception):
            pytest.skip("Circuit breaker is not yet implemented")


# ===========================================================================
# Admin API — spend limits
# ===========================================================================


class TestAdminSetOrgSpendLimit:
    """BDD step: admin sets org spend limit via PUT /limits/org."""

    ENDPOINT = "/api/v1/admin/costs/limits/org"

    def test_admin_sets_org_limit(self, client: TestClient) -> None:
        org = MagicMock()
        org.id = _ORG_ID
        org.daily_spend_limit = None

        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.put(self.ENDPOINT, json={"daily_spend_limit": 250.0})

        assert resp.status_code == 200
        assert resp.json()["daily_spend_limit"] == 250.0

    def test_admin_clears_org_limit(self, client: TestClient) -> None:
        org = MagicMock()
        org.id = _ORG_ID
        org.daily_spend_limit = Decimal("100.00")

        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.put(self.ENDPOINT, json={"daily_spend_limit": None})

        assert resp.status_code == 200
        assert resp.json()["daily_spend_limit"] is None

    def test_viewer_gets_403(self, viewer_client: TestClient) -> None:
        resp = viewer_client.put(self.ENDPOINT, json={"daily_spend_limit": 100.0})
        assert resp.status_code == 403


class TestAdminSetTeamSpendLimit:
    """BDD step: admin sets team spend limit via PUT /limits/teams/{id}."""

    ENDPOINT = f"/api/v1/admin/costs/limits/teams/{_TEAM_ID}"

    def test_admin_sets_team_limit(self, client: TestClient) -> None:
        team = MagicMock()
        team.id = _TEAM_ID
        team.daily_spend_limit = None

        with (
            patch("modulo.api.routes.costs.get_team", return_value=team),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.put(self.ENDPOINT, json={"daily_spend_limit": 75.0})

        assert resp.status_code == 200
        assert resp.json()["daily_spend_limit"] == 75.0


class TestAdminGetCostsReport:
    """BDD step: GET /api/v1/admin/costs returns cost report."""

    ENDPOINT = "/api/v1/admin/costs"
    ROWS: ClassVar[list[dict[str, Any]]] = [
        {"entity_id": str(_TEAM_ID), "entity_name": "Alpha Team", "total_spend_usd": 150.0, "total_runs": 12},
    ]

    def test_returns_cost_report(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.costs.get_cost_report", return_value=self.ROWS),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 200
        data = resp.json()
        assert data["period"] == "month"
        assert data["group_by"] == "team"
        assert len(data["items"]) == 1
        assert data["items"][0]["entity_name"] == "Alpha Team"

    def test_default_params(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.costs.get_cost_report", return_value=self.ROWS),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 200
        assert resp.json()["group_by"] == "team"
        assert resp.json()["period"] == "month"

    def test_viewer_gets_403(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get(self.ENDPOINT)
        assert resp.status_code == 403

    def test_cost_report_with_custom_params(self, client: TestClient) -> None:
        org_rows = [
            {"entity_id": str(_ORG_ID), "entity_name": "Acme Corp", "total_spend_usd": 500.0, "total_runs": 25},
        ]
        with (
            patch("modulo.api.routes.costs.get_cost_report", return_value=org_rows),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get("/api/v1/admin/costs?group_by=org&period=week")

        assert resp.status_code == 200
        data = resp.json()
        assert data["group_by"] == "org"
        assert data["period"] == "week"
        assert len(data["items"]) == 1


# ===========================================================================
# View current spend (via GET /api/v1/admin/costs/limits)
# ===========================================================================


class TestAdminGetSpendLimits:
    """GET /api/v1/admin/costs/limits returns current spend limits."""

    ENDPOINT = "/api/v1/admin/costs/limits"

    def test_returns_limits(self, client: TestClient) -> None:
        org = MagicMock()
        org.id = _ORG_ID
        org.daily_spend_limit = Decimal("100.00")

        team = MagicMock()
        team.id = _TEAM_ID
        team.name = "Alpha"
        team.daily_spend_limit = Decimal("50.00")

        page_result = MagicMock(items=[team], total=1, page=1, page_size=1000)

        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.db.crud.team.list_teams", return_value=page_result),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 200
        data = resp.json()
        assert data["org_daily_spend_limit"] == 100.0
        assert len(data["team_limits"]) == 1
        assert data["team_limits"][0]["daily_spend_limit"] == 50.0
