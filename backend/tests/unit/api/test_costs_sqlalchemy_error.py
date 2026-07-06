"""Unit tests for /api/v1/admin/costs endpoints — SQLAlchemyError → 503."""

import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_DB_ERROR = SQLAlchemyError("mock", "", None)


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


class TestCostsSqlAlchemyError:
    ENDPOINT = "/api/v1/admin/costs"

    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.get_cost_report",
                side_effect=_DB_ERROR,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 503


class TestGetSpendLimitsSqlAlchemyError:
    ENDPOINT = "/api/v1/admin/costs/limits"

    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.get_organisation",
                side_effect=_DB_ERROR,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 503


class TestSetOrgSpendLimitSqlAlchemyError:
    ENDPOINT = "/api/v1/admin/costs/limits/org"

    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.get_organisation",
                side_effect=_DB_ERROR,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.put(self.ENDPOINT, json={"daily_spend_limit": 100.0})

        assert resp.status_code == 503


class TestSetTeamSpendLimitSqlAlchemyError:
    ENDPOINT = f"/api/v1/admin/costs/limits/teams/{uuid.uuid4()}"

    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.get_team",
                side_effect=_DB_ERROR,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.put(self.ENDPOINT, json={"daily_spend_limit": 50.0})

        assert resp.status_code == 503


class TestGetCostControlsSqlAlchemyError:
    ENDPOINT = "/api/v1/admin/costs/controls"

    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.list_teams",
                side_effect=_DB_ERROR,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 503


class TestUpdateCostControlsSqlAlchemyError:
    ENDPOINT = "/api/v1/admin/costs/controls"

    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.get_organisation",
                side_effect=_DB_ERROR,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.put(self.ENDPOINT, json={"budget": 200.0})

        assert resp.status_code == 503


class TestExportCostsSqlAlchemyError:
    ENDPOINT = "/api/v1/admin/costs/export"

    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.get_cost_report",
                side_effect=_DB_ERROR,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(f"{self.ENDPOINT}?period=this_month&group_by=team&format=csv")

        assert resp.status_code == 503


class TestCreateReportSqlAlchemyError:
    ENDPOINT = "/api/v1/admin/costs/reports"
    PAYLOAD: ClassVar[dict[str, Any]] = {
        "period": "monthly",
        "group_by": "team",
        "format": "csv",
        "recipients": ["admin@example.com"],
        "schedule_type": "one_time",
    }

    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.create_scheduled_report",
                side_effect=_DB_ERROR,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.post(self.ENDPOINT, json=self.PAYLOAD)

        assert resp.status_code == 503


class TestListReportsSqlAlchemyError:
    ENDPOINT = "/api/v1/admin/costs/reports"

    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.list_scheduled_reports",
                side_effect=_DB_ERROR,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 503


class TestDeleteReportSqlAlchemyError:
    ENDPOINT = f"/api/v1/admin/costs/reports/{uuid.uuid4()}"

    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.delete_scheduled_report",
                side_effect=_DB_ERROR,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.delete(self.ENDPOINT)

        assert resp.status_code == 503


class TestGetAnomaliesSqlAlchemyError:
    ENDPOINT = "/api/v1/admin/costs/anomalies"

    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.set_rls_org",
                side_effect=_DB_ERROR,
            ),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 503


class TestDismissAnomalySqlAlchemyError:
    ENDPOINT = f"/api/v1/admin/costs/anomalies/dismiss/{uuid.uuid4()}"

    def test_returns_503(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.costs.dismiss_anomaly",
                side_effect=_DB_ERROR,
            ),
            patch("modulo.api.routes.costs.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)

        assert resp.status_code == 503
