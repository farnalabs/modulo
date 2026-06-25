"""Unit tests for /api/v1/admin/costs endpoints."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
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
    return session


@pytest.fixture()
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
        user_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
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
        user_id=_USER_ID,
        org_role="operator",
    )
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
                "modulo.db.crud.team.list_teams",
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
                "modulo.db.crud.team.list_teams",
                return_value=page_result,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get("/api/v1/admin/costs/limits")

        assert resp.status_code == 200
        assert resp.json()["org_daily_spend_limit"] is None
        assert resp.json()["team_limits"] == []

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
    ENDPOINT = "/api/v1/admin/costs/reports"
    PAYLOAD = {
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
        assert resp.json() == []


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

    @pytest.fixture()
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
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="admin",
            organisation_id=_ORG_ID,
            user_id=_USER_ID,
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
        assert resp.json() == []

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
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 204

    def test_dismiss_anomaly_not_found(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.dismiss_anomaly",
                return_value=False,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 404
