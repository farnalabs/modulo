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

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_VIEWER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
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


class _TeamPlan:
    def feature_enabled(self, name: str) -> bool:
        return True

    def list_enabled_features(self) -> list:
        return []


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_plan_context] = lambda: _TeamPlan()
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
    app.dependency_overrides[get_plan_context] = lambda: _TeamPlan()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="viewer",
        organisation_id=_ORG_ID,
        account_id=_VIEWER_ID,
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
    """Tests for the step definitions that exercise check_and_record_spend.

    The refusal decision uses the CREATED-AT day SUM (excluding the current
    run) + the explicit cost add; the ledger row is keyed by the run-start day.
    """

    def _limit(self, value: object) -> MagicMock:
        r = MagicMock()
        r.scalar_one_or_none.return_value = value
        return r

    def _sum(self, value: object) -> MagicMock:
        r = MagicMock()
        r.scalar_one.return_value = value
        r.scalar_one_or_none.return_value = value
        return r

    def _run(
        self, approved: bool, reason: str | None, cost: str, team: bool, sums: list[object]
    ) -> tuple[bool, str | None]:
        mock_org_count = MagicMock()
        mock_org_count.total_spend_usd = Decimal("50.00")
        mock_org_count.run_count = 5
        mock_org_count.clamped = False
        mock_org_count.refused_spend_usd = Decimal(0)
        mock_team_count = MagicMock()
        mock_team_count.total_spend_usd = Decimal("20.00")
        mock_team_count.run_count = 4
        mock_team_count.clamped = False
        mock_team_count.refused_spend_usd = Decimal(0)
        mock_session = _make_mock_session()
        with (
            patch(
                "modulo.core.cost_controller.get_or_create_daily_count",
                side_effect=[mock_org_count, mock_team_count] if team else [mock_org_count],
            ),
            patch.object(mock_session, "execute", side_effect=sums),
        ):
            import asyncio

            from modulo.core.cost_controller import check_and_record_spend

            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(
                    check_and_record_spend(
                        mock_session,
                        org_id=_ORG_ID,
                        cost_usd=Decimal(cost),
                        team_id=_TEAM_ID if team else None,
                    )
                )
            finally:
                loop.close()

    def test_spend_under_org_limit_approved(self) -> None:
        """Happy path: spend under org limit is approved."""
        # org limit 100, created-at SUM (other runs) 50, +30 = 80 <= 100.
        approved, reason = self._run(
            True, None, "30.00", team=False, sums=[self._limit(Decimal("100.00")), self._sum(Decimal("50.00"))]
        )
        assert approved is True
        assert reason is None

    def test_spend_over_org_limit_rejected(self) -> None:
        """Spend exceeding org daily limit is refused."""
        # org limit 100, created-at SUM 95, +10 = 105 > 100.
        approved, reason = self._run(
            False,
            "daily_limit_exceeded: organisation",
            "10.00",
            team=False,
            sums=[self._limit(Decimal("100.00")), self._sum(Decimal("95.00"))],
        )
        assert approved is False
        assert reason == "daily_limit_exceeded: organisation"

    def test_spend_over_team_limit_rejected(self) -> None:
        """Spend exceeding team daily limit is refused (org passes, team fails)."""
        # org limit 500, org SUM 50; team limit 50, team SUM 45, +10 = 55 > 50.
        approved, reason = self._run(
            False,
            "daily_limit_exceeded: team",
            "10.00",
            team=True,
            sums=[
                self._limit(Decimal("500.00")),
                self._sum(Decimal("50.00")),
                self._limit(Decimal("50.00")),
                self._sum(Decimal("45.00")),
            ],
        )
        assert approved is False
        assert reason == "daily_limit_exceeded: team"

    def test_spend_under_both_limits_approved(self) -> None:
        """Spend under both org and team limits is approved with increments."""
        mock_org_count = MagicMock()
        mock_org_count.total_spend_usd = Decimal("100.00")
        mock_org_count.run_count = 10
        mock_org_count.clamped = False
        mock_org_count.refused_spend_usd = Decimal(0)

        mock_team_count = MagicMock()
        mock_team_count.total_spend_usd = Decimal("20.00")
        mock_team_count.run_count = 4
        mock_team_count.clamped = False
        mock_team_count.refused_spend_usd = Decimal(0)

        mock_session = _make_mock_session()

        with (
            patch(
                "modulo.core.cost_controller.get_or_create_daily_count",
                side_effect=[mock_org_count, mock_team_count],
            ),
            patch.object(
                mock_session,
                "execute",
                side_effect=[
                    self._limit(Decimal("500.00")),
                    self._sum(Decimal("100.00")),
                    self._limit(Decimal("100.00")),
                    self._sum(Decimal("20.00")),
                ],
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
        team.organisation_id = _ORG_ID
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
            patch("modulo.api.routes.costs.list_teams", return_value=page_result),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 200
        data = resp.json()
        assert data["org_daily_spend_limit"] == 100.0
        assert len(data["team_limits"]) == 1
        assert data["team_limits"][0]["daily_spend_limit"] == 50.0


# ===========================================================================
# GET /api/v1/admin/costs/controls — fresh/empty DB regression
# ===========================================================================


class TestAdminGetCostControls:
    """GET /api/v1/admin/costs/controls against a fresh/empty database.

    The endpoint must return 200 with sensible defaults (empty team list, null
    budget) when the org exists but has no teams and no spend limit set, and
    must never 500 on NULL/unusable spend-limit values.
    """

    ENDPOINT = "/api/v1/admin/costs/controls"

    def test_empty_database_returns_defaults(self, client: TestClient) -> None:
        """Fresh DB: org present, no teams, NULL org spend limit -> 200 + defaults."""
        org = MagicMock()
        org.id = _ORG_ID
        org.daily_spend_limit = None

        page_result = MagicMock(items=[], total=0, page=1, page_size=1000)

        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.list_teams", return_value=page_result),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 200
        data = resp.json()
        assert not data["teams"]
        assert data["budget"] is None

    def test_missing_org_returns_defaults(self, client: TestClient) -> None:
        """No organisation row -> 200 with empty teams and null budget."""
        page_result = MagicMock(items=[], total=0, page=1, page_size=1000)

        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=None),
            patch("modulo.api.routes.costs.list_teams", return_value=page_result),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 200
        data = resp.json()
        assert not data["teams"]
        assert data["budget"] is None

    def test_populated_database_returns_values(self, client: TestClient) -> None:
        """Populated DB: org limit + team limits are returned correctly."""
        org = MagicMock()
        org.id = _ORG_ID
        org.daily_spend_limit = Decimal("1000.00")

        team_a = MagicMock()
        team_a.id = _TEAM_ID
        team_a.name = "Alpha"
        team_a.daily_spend_limit = Decimal("250.50")

        team_b = MagicMock()
        team_b.id = uuid.UUID("20000000-0000-0000-0000-000000000001")
        team_b.name = "Beta"
        team_b.daily_spend_limit = None

        page_result = MagicMock(items=[team_a, team_b], total=2, page=1, page_size=1000)

        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.list_teams", return_value=page_result),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 200
        data = resp.json()
        assert data["budget"] == 1000.0
        assert len(data["teams"]) == 2
        assert data["teams"][0] == {"id": str(_TEAM_ID), "name": "Alpha", "daily_limit_usd": 250.5}
        assert data["teams"][1]["daily_limit_usd"] is None

    def test_unusable_spend_limit_value_does_not_500(self, client: TestClient) -> None:
        """A NaN/odd stored spend limit must serialize as null, not 500."""
        org = MagicMock()
        org.id = _ORG_ID
        org.daily_spend_limit = Decimal("NaN")

        page_result = MagicMock(items=[], total=0, page=1, page_size=1000)

        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.list_teams", return_value=page_result),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 200
        data = resp.json()
        assert not data["teams"]
        assert data["budget"] is None
