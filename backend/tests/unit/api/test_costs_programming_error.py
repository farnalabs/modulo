"""Unit tests: ProgrammingError on costs routes returns 501."""

import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings


_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
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


@pytest.fixture()
def anomaly_client() -> Generator[TestClient, None, None]:
    """Dedicated fixture that configures session.execute for anomaly tests."""
    mock_session = _make_mock_session()
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
        account_id=_USER_ID,
        org_role="admin",
    )
    app.dependency_overrides[get_plan_context] = lambda: _EnterprisePlan()
    yield TestClient(app)
    app.dependency_overrides.clear()


def _assert_501(resp: Any) -> None:
    assert resp.status_code == 501
    assert "migrations" in resp.json()["detail"].lower()


class TestGetCostsProgrammingError:
    def test_get_costs_returns_501(self, client: TestClient) -> None:
        with patch("modulo.api.routes.costs.set_rls_org", side_effect=ProgrammingError("table does not exist", params=None, orig=None)):
            resp = client.get("/api/v1/admin/costs")
        _assert_501(resp)

    def test_get_limits_returns_501(self, client: TestClient) -> None:
        with patch("modulo.api.routes.costs.set_rls_org", side_effect=ProgrammingError("table does not exist", params=None, orig=None)):
            resp = client.get("/api/v1/admin/costs/limits")
        _assert_501(resp)


class TestSetOrgSpendLimitProgrammingError:
    def test_returns_501(self, client: TestClient) -> None:
        with patch("modulo.api.routes.costs.set_rls_org", side_effect=ProgrammingError("table does not exist", params=None, orig=None)):
            resp = client.put("/api/v1/admin/costs/limits/org", json={"daily_spend_limit": 100.0})
        _assert_501(resp)


class TestSetTeamSpendLimitProgrammingError:
    def test_returns_501(self, client: TestClient) -> None:
        with patch("modulo.api.routes.costs.set_rls_org", side_effect=ProgrammingError("table does not exist", params=None, orig=None)):
            resp = client.put(f"/api/v1/admin/costs/limits/teams/{_TEAM_ID}", json={"daily_spend_limit": 50.0})
        _assert_501(resp)


class TestGetCostControlsProgrammingError:
    def test_returns_501(self, client: TestClient) -> None:
        with patch("modulo.api.routes.costs.set_rls_org", side_effect=ProgrammingError("table does not exist", params=None, orig=None)):
            resp = client.get("/api/v1/admin/costs/controls")
        _assert_501(resp)


class TestUpdateCostControlsProgrammingError:
    def test_returns_501(self, client: TestClient) -> None:
        with patch("modulo.api.routes.costs.set_rls_org", side_effect=ProgrammingError("table does not exist", params=None, orig=None)):
            resp = client.put("/api/v1/admin/costs/controls", json={"budget": 500.0})
        _assert_501(resp)


class TestExportCostsProgrammingError:
    def test_returns_501(self, client: TestClient) -> None:
        with patch("modulo.api.routes.costs.set_rls_org", side_effect=ProgrammingError("table does not exist", params=None, orig=None)):
            resp = client.get("/api/v1/admin/costs/export")
        _assert_501(resp)


class TestCreateReportProgrammingError:
    def test_returns_501(self, client: TestClient) -> None:
        payload = {"period": "monthly", "group_by": "team", "format": "csv", "recipients": ["admin@example.com"], "schedule_type": "one_time"}
        with patch("modulo.api.routes.costs.set_rls_org", side_effect=ProgrammingError("table does not exist", params=None, orig=None)):
            resp = client.post("/api/v1/admin/costs/reports", json=payload)
        _assert_501(resp)


class TestListReportsProgrammingError:
    def test_returns_501(self, client: TestClient) -> None:
        with patch("modulo.api.routes.costs.set_rls_org", side_effect=ProgrammingError("table does not exist", params=None, orig=None)):
            resp = client.get("/api/v1/admin/costs/reports")
        _assert_501(resp)


class TestDeleteReportProgrammingError:
    def test_returns_501(self, client: TestClient) -> None:
        with patch("modulo.api.routes.costs.set_rls_org", side_effect=ProgrammingError("table does not exist", params=None, orig=None)):
            resp = client.delete(f"/api/v1/admin/costs/reports/{uuid.uuid4()}")
        _assert_501(resp)


class TestGetAnomaliesProgrammingError:
    def test_returns_501(self, anomaly_client: TestClient) -> None:
        with patch("modulo.api.routes.costs.set_rls_org", side_effect=ProgrammingError("table does not exist", params=None, orig=None)):
            resp = anomaly_client.get("/api/v1/admin/costs/anomalies")
        _assert_501(resp)


class TestDismissAnomalyProgrammingError:
    def test_returns_501(self, client: TestClient) -> None:
        with patch("modulo.api.routes.costs.set_rls_org", side_effect=ProgrammingError("table does not exist", params=None, orig=None)):
            resp = client.get(f"/api/v1/admin/costs/anomalies/dismiss/{uuid.uuid4()}")
        _assert_501(resp)
