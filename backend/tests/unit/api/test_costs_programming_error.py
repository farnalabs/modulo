"""Unit tests for /api/v1/admin/costs endpoints — ProgrammingError → 501."""

import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PROG_ERROR = ProgrammingError("", {}, None)


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


class _EnterprisePlan:
    def feature_enabled(self, name: str) -> bool:
        return True

    def list_enabled_features(self) -> list:
        return []


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
        account_id=_USER_ID,
        org_role="admin",
    )
    app.dependency_overrides[get_plan_context] = lambda: _EnterprisePlan()
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestCostsProgrammingError:
    """GET /api/v1/admin/costs → 501 when ProgrammingError raised."""

    ENDPOINT = "/api/v1/admin/costs"

    def test_returns_501(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.get_cost_report",
                side_effect=_PROG_ERROR,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestGetSpendLimitsProgrammingError:
    """GET /api/v1/admin/costs/limits → 501 when ProgrammingError raised."""

    ENDPOINT = "/api/v1/admin/costs/limits"

    def test_returns_501(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.get_organisation",
                side_effect=_PROG_ERROR,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestSetOrgSpendLimitProgrammingError:
    """PUT /api/v1/admin/costs/limits/org → 501 when ProgrammingError raised."""

    ENDPOINT = "/api/v1/admin/costs/limits/org"

    def test_returns_501(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.get_organisation",
                side_effect=_PROG_ERROR,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.put(self.ENDPOINT, json={"daily_spend_limit": 100.0})

        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestSetTeamSpendLimitProgrammingError:
    """PUT /api/v1/admin/costs/limits/teams/{id} → 501 when ProgrammingError raised."""

    ENDPOINT = f"/api/v1/admin/costs/limits/teams/{uuid.uuid4()}"

    def test_returns_501(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.get_team",
                side_effect=_PROG_ERROR,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.put(self.ENDPOINT, json={"daily_spend_limit": 50.0})

        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestGetCostControlsProgrammingError:
    """GET /api/v1/admin/costs/controls → 501 when ProgrammingError raised."""

    ENDPOINT = "/api/v1/admin/costs/controls"

    def test_returns_501(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.list_teams",
                side_effect=_PROG_ERROR,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestUpdateCostControlsProgrammingError:
    """PUT /api/v1/admin/costs/controls → 501 when ProgrammingError raised."""

    ENDPOINT = "/api/v1/admin/costs/controls"

    def test_returns_501(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.get_organisation",
                side_effect=_PROG_ERROR,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.put(self.ENDPOINT, json={"budget": 200.0})

        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestExportCostsProgrammingError:
    """GET /api/v1/admin/costs/export → 501 when ProgrammingError raised."""

    ENDPOINT = "/api/v1/admin/costs/export"

    def test_returns_501(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.get_cost_report",
                side_effect=_PROG_ERROR,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(f"{self.ENDPOINT}?period=this_month&group_by=team&format=csv")

        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestCreateReportProgrammingError:
    """POST /api/v1/admin/costs/reports → 501 when ProgrammingError raised."""

    ENDPOINT = "/api/v1/admin/costs/reports"
    PAYLOAD: ClassVar[dict[str, Any]] = {
        "period": "monthly",
        "group_by": "team",
        "format": "csv",
        "recipients": ["admin@example.com"],
        "schedule_type": "one_time",
    }

    def test_returns_501(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.create_scheduled_report",
                side_effect=_PROG_ERROR,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.post(self.ENDPOINT, json=self.PAYLOAD)

        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestListReportsProgrammingError:
    """GET /api/v1/admin/costs/reports → 501 when ProgrammingError raised."""

    ENDPOINT = "/api/v1/admin/costs/reports"

    def test_returns_501(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.list_scheduled_reports",
                side_effect=_PROG_ERROR,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestDeleteReportProgrammingError:
    """DELETE /api/v1/admin/costs/reports/{id} → 501 when ProgrammingError raised."""

    ENDPOINT = f"/api/v1/admin/costs/reports/{uuid.uuid4()}"

    def test_returns_501(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.delete_scheduled_report",
                side_effect=_PROG_ERROR,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.delete(self.ENDPOINT)

        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestGetAnomaliesProgrammingError:
    """GET /api/v1/admin/costs/anomalies → 501 when ProgrammingError raised."""

    ENDPOINT = "/api/v1/admin/costs/anomalies"

    def test_returns_501(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.set_rls_org",
                side_effect=_PROG_ERROR,
            ),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestDismissAnomalyProgrammingError:
    """GET /api/v1/admin/costs/anomalies/dismiss/{id} → 501 when ProgrammingError raised."""

    ENDPOINT = f"/api/v1/admin/costs/anomalies/dismiss/{uuid.uuid4()}"

    def test_returns_501(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.dismiss_anomaly",
                side_effect=_PROG_ERROR,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()
