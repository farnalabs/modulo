"""Unit tests for /api/v1/admin/costs endpoints."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
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
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    # require_permission -> resolve_authz_enforce reads the org authz kill-switch
    # via `await session.execute(...)` then calls the un-awaited
    # `result.scalar_one_or_none()`. With a bare AsyncMock execute result that
    # chained call leaks an unawaited AsyncMockMixin._execute_mock_call
    # coroutine (PytestUnraisableExceptionWarning) on EVERY request. Returning
    # a plain MagicMock result keeps the sync `.scalar_one_or_none()` chain off
    # the async-mock path; None mirrors the "row absent" default (enforce).
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=execute_result)
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
    app.dependency_overrides[get_plan_context] = lambda: _TeamPlan()
    yield TestClient(app)
    app.dependency_overrides.clear()


class _TeamPlan:
    """Stub plan context that enables all team features for tests."""

    def feature_enabled(self, name: str) -> bool:
        return True

    def list_enabled_features(self) -> list:
        return []


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_plan_context] = lambda: _TeamPlan()
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def operator_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="operator",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="operator",
    )
    app.dependency_overrides[get_plan_context] = lambda: _TeamPlan()
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestGetCostsReport:
    ROWS: ClassVar[list[dict[str, Any]]] = [
        {
            "entity_id": str(_TEAM_ID),
            "entity_name": "Alpha Team",
            "total_spend_usd": 150.0,
            "total_runs": 12,
        },
    ]

    def test_returns_cost_report(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.get_cost_report",
                return_value=self.ROWS,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get("/api/v1/admin/costs?group_by=team&period=month")

        assert resp.status_code == 200
        data = resp.json()
        assert data["period"] == "month"
        assert data["group_by"] == "team"
        assert len(data["items"]) == 1
        assert data["items"][0]["entity_name"] == "Alpha Team"

    def test_default_params(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.get_cost_report",
                return_value=self.ROWS,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get("/api/v1/admin/costs")

        assert resp.status_code == 200
        assert resp.json()["group_by"] == "team"
        assert resp.json()["period"] == "month"

    def test_invalid_group_by_returns_422(self, client: TestClient) -> None:
        resp = client.get("/api/v1/admin/costs?group_by=invalid")
        assert resp.status_code == 422

    def test_invalid_period_returns_422(self, client: TestClient) -> None:
        resp = client.get("/api/v1/admin/costs?period=decade")
        assert resp.status_code == 422

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get("/api/v1/admin/costs")
        assert resp.status_code in (401, 403)

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.get("/api/v1/admin/costs")
        assert resp.status_code == 403


class TestGetSpendLimits:
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
            resp = client.get("/api/v1/admin/costs/limits")

        assert resp.status_code == 200
        data = resp.json()
        assert data["organisation_id"] == str(_ORG_ID)
        assert data["org_daily_spend_limit"] == 100.0
        assert len(data["team_limits"]) == 1
        assert data["team_limits"][0]["team_name"] == "Alpha"
        assert data["team_limits"][0]["daily_spend_limit"] == 50.0

    def test_returns_none_limits_when_not_set(self, client: TestClient) -> None:
        org = MagicMock()
        org.id = _ORG_ID
        org.daily_spend_limit = None

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
            resp = client.get("/api/v1/admin/costs/limits")

        assert resp.status_code == 200
        assert resp.json()["org_daily_spend_limit"] is None
        assert not resp.json()["team_limits"]

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get("/api/v1/admin/costs/limits")
        assert resp.status_code in (401, 403)

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.get("/api/v1/admin/costs/limits")
        assert resp.status_code == 403


class TestSetOrgSpendLimit:
    ENDPOINT = "/api/v1/admin/costs/limits/org"

    def test_sets_limit(self, client: TestClient) -> None:
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
            resp = client.put(self.ENDPOINT, json={"daily_spend_limit": 250.0})

        assert resp.status_code == 200
        assert resp.json()["daily_spend_limit"] == 250.0
        assert org.daily_spend_limit == Decimal("250.00")

    def test_clears_limit(self, client: TestClient) -> None:
        org = MagicMock()
        org.id = _ORG_ID
        org.daily_spend_limit = Decimal("100.00")

        with (
            patch(
                "modulo.api.routes.costs.get_organisation",
                return_value=org,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.put(self.ENDPOINT, json={"daily_spend_limit": None})

        assert resp.status_code == 200
        assert resp.json()["daily_spend_limit"] is None
        assert org.daily_spend_limit is None

    def test_org_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.get_organisation",
                return_value=None,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.put(self.ENDPOINT, json={"daily_spend_limit": 100.0})

        assert resp.status_code == 404

    def test_negative_limit_returns_422(self, client: TestClient) -> None:
        resp = client.put(self.ENDPOINT, json={"daily_spend_limit": -1})
        assert resp.status_code == 422

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.put(self.ENDPOINT, json={"daily_spend_limit": 100})
        assert resp.status_code == 403


class TestSetTeamSpendLimit:
    ENDPOINT = f"/api/v1/admin/costs/limits/teams/{_TEAM_ID}"

    def test_sets_team_limit(self, client: TestClient) -> None:
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
            resp = client.put(self.ENDPOINT, json={"daily_spend_limit": 75.0})

        assert resp.status_code == 200
        assert resp.json()["daily_spend_limit"] == 75.0
        assert team.daily_spend_limit == Decimal("75.00")

    def test_clears_team_limit(self, client: TestClient) -> None:
        team = MagicMock()
        team.id = _TEAM_ID
        team.organisation_id = _ORG_ID
        team.daily_spend_limit = Decimal("50.00")

        with (
            patch(
                "modulo.api.routes.costs.get_team",
                return_value=team,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.put(self.ENDPOINT, json={"daily_spend_limit": None})

        assert resp.status_code == 200
        assert resp.json()["daily_spend_limit"] is None
        assert team.daily_spend_limit is None

    def test_team_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.get_team",
                return_value=None,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.put(self.ENDPOINT, json={"daily_spend_limit": 50.0})

        assert resp.status_code == 404

    def test_cross_org_team_returns_404_and_is_not_mutated(self, client: TestClient) -> None:
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
            resp = client.put(self.ENDPOINT, json={"daily_spend_limit": 75.0})

        assert resp.status_code == 404
        # The org-scoping guard must short-circuit before any mutation.
        assert team.daily_spend_limit == Decimal("50.00")

    def test_invalid_team_id_returns_422(self, client: TestClient) -> None:
        resp = client.put(
            "/api/v1/admin/costs/limits/teams/not-a-uuid",
            json={"daily_spend_limit": 50.0},
        )
        assert resp.status_code == 422

    def test_negative_limit_returns_422(self, client: TestClient) -> None:
        resp = client.put(self.ENDPOINT, json={"daily_spend_limit": -5})
        assert resp.status_code == 422

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.put(self.ENDPOINT, json={"daily_spend_limit": 50})
        assert resp.status_code == 403


class TestExportCosts:
    ROWS: ClassVar[list[dict[str, Any]]] = [
        {
            "entity_id": str(_TEAM_ID),
            "entity_name": "Alpha Team",
            "total_spend_usd": 150.0,
            "total_runs": 12,
        },
    ]

    def test_export_csv(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.get_cost_report",
                return_value=self.ROWS,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get("/api/v1/admin/costs/export?period=this_month&group_by=team&format=csv")

        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        assert "costs-export-this_month.csv" in resp.headers["content-disposition"]
        body = resp.text
        assert "entity_id" in body
        assert "Alpha Team" in body
        assert "150.0" in body

    def test_export_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get("/api/v1/admin/costs/export")
        assert resp.status_code in (401, 403)

    def test_export_invalid_period_returns_422(self, client: TestClient) -> None:
        resp = client.get("/api/v1/admin/costs/export?period=invalid")
        assert resp.status_code == 422


class TestCreateReport:
    ENDPOINT: ClassVar[str] = "/api/v1/admin/costs/reports"
    PAYLOAD: ClassVar[dict] = {
        "period": "monthly",
        "group_by": "team",
        "format": "csv",
        "recipients": ["admin@example.com"],
        "schedule_type": "one_time",
    }

    def test_creates_report(self, client: TestClient) -> None:
        mock_report = MagicMock()
        mock_report.id = uuid.uuid4()
        mock_report.period = "monthly"
        mock_report.group_by = "team"
        mock_report.format = "csv"
        mock_report.recipients = ["admin@example.com"]
        mock_report.schedule_type = "one_time"
        mock_report.created_at = datetime(2025, 1, 1, tzinfo=UTC)

        with (
            patch(
                "modulo.api.routes.costs.create_scheduled_report",
                return_value=mock_report,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.post(self.ENDPOINT, json=self.PAYLOAD)

        assert resp.status_code == 201
        data = resp.json()
        assert data["period"] == "monthly"
        assert data["group_by"] == "team"
        assert data["format"] == "csv"
        assert data["recipients"] == ["admin@example.com"]

    def test_create_report_missing_recipients_returns_422(self, client: TestClient) -> None:
        resp = client.post(self.ENDPOINT, json={**self.PAYLOAD, "recipients": []})
        assert resp.status_code == 422

    def test_create_report_rejects_unsupported_grouping(self, client: TestClient) -> None:
        resp = client.post(self.ENDPOINT, json={**self.PAYLOAD, "group_by": "pipeline"})
        assert resp.status_code == 422

    def test_create_report_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(self.ENDPOINT, json=self.PAYLOAD)
        assert resp.status_code in (401, 403)


class TestListReports:
    ENDPOINT = "/api/v1/admin/costs/reports"

    def test_list_reports(self, client: TestClient) -> None:
        mock_report = MagicMock()
        mock_report.id = uuid.uuid4()
        mock_report.period = "weekly"
        mock_report.group_by = "pipeline"
        mock_report.format = "json"
        mock_report.recipients = ["a@b.com", "c@d.com"]
        mock_report.schedule_type = "recurring"
        mock_report.created_at = datetime(2025, 1, 1, tzinfo=UTC)

        with (
            patch(
                "modulo.api.routes.costs.list_scheduled_reports",
                return_value=[mock_report],
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["period"] == "weekly"
        assert data[0]["group_by"] == "pipeline"

    def test_list_reports_empty(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.list_scheduled_reports",
                return_value=[],
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 200
        assert not resp.json()


class TestDeleteReport:
    ENDPOINT = f"/api/v1/admin/costs/reports/{uuid.uuid4()}"

    def test_deletes_report(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.delete_scheduled_report",
                return_value=True,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.delete(self.ENDPOINT)

        assert resp.status_code == 204

    def test_delete_report_not_found(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.delete_scheduled_report",
                return_value=False,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.delete(self.ENDPOINT)

        assert resp.status_code == 404


class TestGetAnomalies:
    ENDPOINT = "/api/v1/admin/costs/anomalies"

    @pytest.fixture
    def anomaly_client(self) -> Generator[TestClient, None, None]:
        """Dedicated fixture that configures session.execute for anomaly tests."""
        mock_session = _make_mock_session()
        # Configure execute to return a result with .all() returning empty list
        # so the anomaly detection loop doesn't crash with AsyncMock
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

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

    def test_returns_stored_anomalies(self, anomaly_client: TestClient) -> None:
        mock_anomaly = MagicMock()
        mock_anomaly.id = uuid.uuid4()
        mock_anomaly.anomaly_date = "2025-01-01"
        mock_anomaly.pipeline_id = None
        mock_anomaly.amount = Decimal("500.00")
        mock_anomaly.baseline = Decimal("200.00")
        mock_anomaly.percent_above = Decimal("150.00")
        mock_anomaly.dismissed = False

        with (
            patch(
                "modulo.api.routes.costs.list_anomalies",
                return_value=[mock_anomaly],
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = anomaly_client.get(self.ENDPOINT)

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1

    def test_returns_empty_when_no_anomalies(self, anomaly_client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.list_anomalies",
                return_value=[],
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = anomaly_client.get(self.ENDPOINT)

        assert resp.status_code == 200
        assert not resp.json()

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(self.ENDPOINT)
        assert resp.status_code in (401, 403)


class TestDismissAnomaly:
    ENDPOINT = f"/api/v1/admin/costs/anomalies/dismiss/{uuid.uuid4()}"

    def test_dismisses_anomaly(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.dismiss_anomaly",
                return_value=True,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.post(self.ENDPOINT)

        assert resp.status_code == 204

    def test_dismiss_anomaly_not_found(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.dismiss_anomaly",
                return_value=False,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.post(self.ENDPOINT)

        assert resp.status_code == 404

    def test_dismiss_is_post_not_get(self, client: TestClient) -> None:
        """The dismiss action mutates state, so GET must not be allowed (REST 405)."""
        resp = client.get(self.ENDPOINT)

        assert resp.status_code == 405


class TestCostControlsCurrency:
    """Cost-control currency/billing settings persist via org.settings_json and round-trip."""

    ENDPOINT = "/api/v1/admin/costs/controls"

    def _org(self, settings_json: dict | None = None) -> MagicMock:
        org = MagicMock()
        org.id = _ORG_ID
        org.daily_spend_limit = None
        org.settings_json = settings_json if settings_json is not None else {}
        return org

    def test_controls_defaults_when_unset(self, client: TestClient) -> None:
        org = self._org()
        page_result = MagicMock(items=[], total=0, page=1, page_size=1000)
        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.list_teams", return_value=page_result),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 200
        data = resp.json()
        assert data["currency"] == "USD"
        assert data["billing_period"] == "monthly"
        assert data["circuit_breaker_enabled"] is False
        assert data["alert_thresholds"] == [50, 75, 90]

    def test_update_persists_currency_and_settings(self, client: TestClient) -> None:
        org = self._org()
        page_result = MagicMock(items=[], total=0, page=1, page_size=1000)
        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.list_teams", return_value=page_result),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.put(
                self.ENDPOINT,
                json={"currency": "EUR", "billing_period": "quarterly", "circuit_breaker_enabled": True},
            )

        assert resp.status_code == 200
        assert org.settings_json == {
            "cost_controls": {
                "currency": "EUR",
                "billing_period": "quarterly",
                "circuit_breaker_enabled": True,
            }
        }

    def test_update_persists_alert_thresholds(self, client: TestClient) -> None:
        org = self._org()
        page_result = MagicMock(items=[], total=0, page=1, page_size=1000)
        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.list_teams", return_value=page_result),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.put(self.ENDPOINT, json={"alert_thresholds": [50, 75, 90]})

        assert resp.status_code == 200
        assert org.settings_json == {"cost_controls": {"alert_thresholds": [50.0, 75.0, 90.0]}}
        assert resp.json()["alert_thresholds"] == [50.0, 75.0, 90.0]

    def test_get_returns_persisted_alert_thresholds(self, client: TestClient) -> None:
        org = self._org({"cost_controls": {"alert_thresholds": [50, 90]}})
        page_result = MagicMock(items=[], total=0, page=1, page_size=1000)
        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.list_teams", return_value=page_result),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 200
        assert resp.json()["alert_thresholds"] == [50.0, 90.0]

    def test_update_rejects_unknown_currency(self, client: TestClient) -> None:
        org = self._org()
        page_result = MagicMock(items=[], total=0, page=1, page_size=1000)
        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.list_teams", return_value=page_result),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.put(self.ENDPOINT, json={"currency": "ABC"})

        assert resp.status_code == 422

    def test_update_rejects_unknown_billing_period(self, client: TestClient) -> None:
        org = self._org()
        page_result = MagicMock(items=[], total=0, page=1, page_size=1000)
        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.list_teams", return_value=page_result),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.put(self.ENDPOINT, json={"billing_period": "yearly"})

        assert resp.status_code == 422

    def test_currency_round_trips_through_update_get(self, client: TestClient) -> None:
        org = self._org()
        page_result = MagicMock(items=[], total=0, page=1, page_size=1000)
        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.list_teams", return_value=page_result),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            put_resp = client.put(self.ENDPOINT, json={"currency": "GBP"})
            assert put_resp.status_code == 200
            get_resp = client.get(self.ENDPOINT)

        assert get_resp.status_code == 200
        data = get_resp.json()
        assert data["currency"] == "GBP"
        assert data["billing_period"] == "monthly"
        assert data["circuit_breaker_enabled"] is False

    def test_update_preserves_existing_settings_and_budget(self, client: TestClient) -> None:
        org = self._org({"cost_controls": {"currency": "USD", "billing_period": "monthly"}})
        page_result = MagicMock(items=[], total=0, page=1, page_size=1000)
        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.list_teams", return_value=page_result),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.put(self.ENDPOINT, json={"currency": "EUR", "budget": 250.0})

        assert resp.status_code == 200
        assert org.settings_json["cost_controls"] == {
            "currency": "EUR",
            "billing_period": "monthly",
        }

    def test_update_org_not_found_returns_404(self, client: TestClient) -> None:
        page_result = MagicMock(items=[], total=0, page=1, page_size=1000)
        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=None),
            patch("modulo.api.routes.costs.list_teams", return_value=page_result),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.put(self.ENDPOINT, json={"currency": "EUR"})

        assert resp.status_code == 404


class TestCostControlsAlertThresholds:
    """alert_thresholds persist via org.settings_json and round-trip through update/get."""

    ENDPOINT = "/api/v1/admin/costs/controls"

    def _org(self, settings_json: dict | None = None) -> MagicMock:
        org = MagicMock()
        org.id = _ORG_ID
        org.daily_spend_limit = None
        org.settings_json = settings_json if settings_json is not None else {}
        return org

    def test_defaults_when_unset(self, client: TestClient) -> None:
        org = self._org()
        page_result = MagicMock(items=[], total=0, page=1, page_size=1000)
        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.list_teams", return_value=page_result),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 200
        assert resp.json()["alert_thresholds"] == [50, 75, 90]

    def test_defaults_ignores_corrupted_persisted_value(self, client: TestClient) -> None:
        org = self._org({"cost_controls": {"alert_thresholds": "not-a-list"}})
        page_result = MagicMock(items=[], total=0, page=1, page_size=1000)
        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.list_teams", return_value=page_result),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 200
        assert resp.json()["alert_thresholds"] == [50, 75, 90]

    def test_defaults_ignores_non_numeric_list_item(self, client: TestClient) -> None:
        org = self._org({"cost_controls": {"alert_thresholds": [50, "high"]}})
        page_result = MagicMock(items=[], total=0, page=1, page_size=1000)
        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.list_teams", return_value=page_result),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 200
        assert resp.json()["alert_thresholds"] == [50, 75, 90]

    def test_defaults_ignores_out_of_range_persisted_value(self, client: TestClient) -> None:
        org = self._org({"cost_controls": {"alert_thresholds": [50, 1000]}})
        page_result = MagicMock(items=[], total=0, page=1, page_size=1000)
        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.list_teams", return_value=page_result),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 200
        assert resp.json()["alert_thresholds"] == [50, 75, 90]

    def test_defaults_ignores_overflow_huge_int_persisted_value(self, client: TestClient) -> None:
        org = self._org({"cost_controls": {"alert_thresholds": [50, int("1" + "0" * 400)]}})
        page_result = MagicMock(items=[], total=0, page=1, page_size=1000)
        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.list_teams", return_value=page_result),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 200
        assert resp.json()["alert_thresholds"] == [50, 75, 90]

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_defaults_ignores_non_finite_persisted_value(self, client: TestClient, bad: float) -> None:
        org = self._org({"cost_controls": {"alert_thresholds": [50, bad]}})
        page_result = MagicMock(items=[], total=0, page=1, page_size=1000)
        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.list_teams", return_value=page_result),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 200
        assert resp.json()["alert_thresholds"] == [50, 75, 90]

    def test_update_persists_alert_thresholds(self, client: TestClient) -> None:
        org = self._org()
        page_result = MagicMock(items=[], total=0, page=1, page_size=1000)
        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.list_teams", return_value=page_result),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.put(self.ENDPOINT, json={"alert_thresholds": [50, 75, 90, 100]})

        assert resp.status_code == 200
        assert org.settings_json == {"cost_controls": {"alert_thresholds": [50.0, 75.0, 90.0, 100.0]}}

    def test_alert_thresholds_round_trip_through_update_get(self, client: TestClient) -> None:
        org = self._org()
        page_result = MagicMock(items=[], total=0, page=1, page_size=1000)
        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.list_teams", return_value=page_result),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            put_resp = client.put(self.ENDPOINT, json={"alert_thresholds": [50, 100]})
            assert put_resp.status_code == 200
            get_resp = client.get(self.ENDPOINT)

        assert get_resp.status_code == 200
        assert get_resp.json()["alert_thresholds"] == [50.0, 100.0]

    def test_update_preserves_existing_thresholds_and_currency(self, client: TestClient) -> None:
        org = self._org({"cost_controls": {"currency": "EUR", "alert_thresholds": [75]}})
        page_result = MagicMock(items=[], total=0, page=1, page_size=1000)
        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.list_teams", return_value=page_result),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.put(self.ENDPOINT, json={"currency": "GBP"})

        assert resp.status_code == 200
        assert org.settings_json["cost_controls"] == {"currency": "GBP", "alert_thresholds": [75]}

    def test_empty_thresholds_returns_422(self, client: TestClient) -> None:
        resp = client.put(self.ENDPOINT, json={"alert_thresholds": []})
        assert resp.status_code == 422

    def test_out_of_range_threshold_returns_422(self, client: TestClient) -> None:
        resp = client.put(self.ENDPOINT, json={"alert_thresholds": [50, 150]})
        assert resp.status_code == 422

    def test_non_integer_threshold_returns_422(self, client: TestClient) -> None:
        resp = client.put(self.ENDPOINT, json={"alert_thresholds": [50.5]})
        assert resp.status_code == 422

    def test_non_numeric_threshold_returns_422(self, client: TestClient) -> None:
        resp = client.put(self.ENDPOINT, json={"alert_thresholds": ["high"]})
        assert resp.status_code == 422

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.put(self.ENDPOINT, json={"alert_thresholds": [50]})
        assert resp.status_code == 403


class TestOrgSettingsCurrency:
    """GET /api/v1/org/settings exposes the org currency to any tenant member."""

    ENDPOINT = "/api/v1/org/settings"

    def _org(self, settings_json: dict | None = None) -> MagicMock:
        org = MagicMock()
        org.id = _ORG_ID
        org.settings_json = settings_json if settings_json is not None else {}
        return org

    def test_returns_default_when_unset(self, client: TestClient) -> None:
        org = self._org()
        with (
            patch("modulo.api.routes.org_settings.get_organisation", return_value=org),
            patch("modulo.api.routes.org_settings.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 200
        assert resp.json() == {"currency": "USD"}

    def test_returns_persisted_currency(self, client: TestClient) -> None:
        org = self._org({"cost_controls": {"currency": "EUR", "billing_period": "quarterly"}})
        with (
            patch("modulo.api.routes.org_settings.get_organisation", return_value=org),
            patch("modulo.api.routes.org_settings.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 200
        assert resp.json() == {"currency": "EUR"}

    def test_non_admin_tenant_member_can_read(self, operator_client: TestClient) -> None:
        org = self._org({"cost_controls": {"currency": "GBP"}})
        with (
            patch("modulo.api.routes.org_settings.get_organisation", return_value=org),
            patch("modulo.api.routes.org_settings.set_rls_org"),
        ):
            resp = operator_client.get(self.ENDPOINT)

        assert resp.status_code == 200
        assert resp.json() == {"currency": "GBP"}

    def test_unauthenticated_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(self.ENDPOINT)
        assert resp.status_code in (401, 403)

    def test_missing_org_returns_default(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.org_settings.get_organisation", return_value=None),
            patch("modulo.api.routes.org_settings.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 200
        assert resp.json() == {"currency": "USD"}


class TestSpendCeilingEndpoints:
    """FAR-391 — GET/PUT /api/v1/admin/costs/ceiling."""

    def test_get_returns_ceilings_and_remaining(self, client: TestClient) -> None:
        org = MagicMock()
        org.id = _ORG_ID
        org.max_run_cost_cents = 1234  # $12.34
        org.spend_ceiling_cents = 10000  # $100.00
        org.org_cumulative_spend_cents = 4000  # $40.00 consumed

        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get("/api/v1/admin/costs/ceiling")

        assert resp.status_code == 200
        data = resp.json()
        assert data["max_run_cost"] == pytest.approx(12.34)
        assert data["spend_ceiling"] == 100.0
        assert data["org_cumulative_spend_usd"] == 40.0
        assert data["remaining_budget_usd"] == 60.0

    def test_get_null_ceilings_serialise_as_none(self, client: TestClient) -> None:
        org = MagicMock()
        org.id = _ORG_ID
        org.max_run_cost_cents = None
        org.spend_ceiling_cents = None
        org.org_cumulative_spend_cents = 0

        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get("/api/v1/admin/costs/ceiling")

        assert resp.status_code == 200
        data = resp.json()
        assert data["max_run_cost"] is None
        assert data["spend_ceiling"] is None
        assert data["remaining_budget_usd"] is None

    def test_put_persists_cents(self, client: TestClient) -> None:
        org = MagicMock()
        org.id = _ORG_ID
        org.max_run_cost_cents = None
        org.spend_ceiling_cents = None
        org.org_cumulative_spend_cents = 0

        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.put(
                "/api/v1/admin/costs/ceiling",
                json={"max_run_cost": 12.34, "spend_ceiling": 100.0},
            )

        assert resp.status_code == 200
        assert org.max_run_cost_cents == 1234
        assert org.spend_ceiling_cents == 10000
        assert resp.json()["remaining_budget_usd"] == 100.0

    def test_put_clears_ceiling_with_explicit_null(self, client: TestClient) -> None:
        """FAR-391 Major 2 — an explicit null must CLEAR a set ceiling.

        The frontend maps an empty input to ``null``; "Empty = no limit" must
        round-trip. A non-null sibling field must be left untouched (the handler
        uses ``exclude_unset`` so omitted fields are never clobbered).
        """
        org = MagicMock()
        org.id = _ORG_ID
        org.max_run_cost_cents = 1234  # pre-existing per-run ceiling
        org.spend_ceiling_cents = 10000  # pre-existing org ceiling
        org.org_cumulative_spend_cents = 0

        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.put(
                "/api/v1/admin/costs/ceiling",
                json={"max_run_cost": None, "spend_ceiling": 100.0},
            )

        assert resp.status_code == 200
        # per-run ceiling cleared to unlimited
        assert org.max_run_cost_cents is None
        # org ceiling preserved (explicit value)
        assert org.spend_ceiling_cents == 10000

    def test_put_omitted_field_is_left_unchanged(self, client: TestClient) -> None:
        """A field absent from the body must NOT be reset to unlimited."""
        org = MagicMock()
        org.id = _ORG_ID
        org.max_run_cost_cents = 1234
        org.spend_ceiling_cents = 10000
        org.org_cumulative_spend_cents = 0

        with (
            patch("modulo.api.routes.costs.get_organisation", return_value=org),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.put(
                "/api/v1/admin/costs/ceiling",
                json={"spend_ceiling": 200.0},
            )

        assert resp.status_code == 200
        assert org.max_run_cost_cents == 1234  # untouched
        assert org.spend_ceiling_cents == 20000
