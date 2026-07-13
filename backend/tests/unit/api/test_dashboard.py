"""Unit tests for /api/v1/dashboard/summary and /api/v1/dashboard/trends."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


class _MockRow:
    """Simulates a SQLAlchemy result row with named attribute access."""

    def __init__(self, **kwargs: object) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _MockResult:
    """Simulates a SQLAlchemy result proxy for chain calls."""

    def __init__(self, scalar_one_val: object = 42, rows: object | None = None) -> None:
        self._scalar_one = scalar_one_val
        self._rows = rows if rows is not None else []

    def scalar_one(self) -> object:
        return self._scalar_one

    def scalar_one_or_none(self) -> object:
        return self._scalar_one

    def one(self) -> "_MockRow":
        """Return the first row, or a default row if none exist."""
        if hasattr(self._rows, "__iter__"):
            rows_list = list(self._rows)
            if rows_list:
                return rows_list[0]
        return _MockRow(total=100, passed=75)

    def scalars(self) -> "_MockResult":
        return self

    def all(self) -> list[object]:
        return list(self._rows) if hasattr(self._rows, "__iter__") else []

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._rows if hasattr(self._rows, "__iter__") else [])


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    def _execute_side_effect(*_args: object, **_kwargs: object) -> _MockResult:
        return _MockResult()

    session.execute = AsyncMock(side_effect=_execute_side_effect)
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
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestDashboardSummary:
    """GET /api/v1/dashboard/summary"""

    def test_returns_summary(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/summary")
        assert response.status_code == 200
        body = response.json()
        assert "total_runs" in body
        assert "active_pipelines" in body
        assert "run_counts_by_status" in body
        counts = body["run_counts_by_status"]
        for status in ("running", "awaiting_human", "failed", "idle"):
            assert status in counts
            assert isinstance(counts[status], int)

    def test_includes_team_metrics(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/summary")
        assert response.status_code == 200
        body = response.json()
        assert "teams" in body
        assert isinstance(body["teams"], list)

    def test_includes_eval_pass_rate(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/summary")
        assert response.status_code == 200
        body = response.json()
        assert "eval_pass_rate" in body
        if body["eval_pass_rate"] is not None:
            er = body["eval_pass_rate"]
            assert "overall_pass_rate" in er
            assert "total_evals" in er
            assert "passed_evals" in er
            assert "per_pipeline" in er

    def test_includes_trend(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/summary")
        assert response.status_code == 200
        body = response.json()
        assert "trend" in body
        trend = body["trend"]
        assert isinstance(trend, list)
        for point in trend:
            assert "date" in point
            assert "run_count" in point
            assert "eval_pass_rate" in point
            assert "token_spend_usd" in point

    def test_trend_is_7_days(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/summary")
        assert response.status_code == 200
        body = response.json()
        assert len(body["trend"]) == 7

    def test_all_new_fields_present(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/summary")
        assert response.status_code == 200
        body = response.json()
        expected = {
            "total_runs",
            "active_pipelines",
            "run_counts_by_status",
            "teams",
            "eval_pass_rate",
            "trend",
            "recent_runs",
            "config_warnings",
        }
        assert set(body.keys()) == expected

    def test_config_warnings_is_list(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/summary")
        assert response.status_code == 200
        body = response.json()
        assert "config_warnings" in body
        assert isinstance(body["config_warnings"], list)

    def test_requires_auth(self, unauth_client: TestClient) -> None:
        response = unauth_client.get("/api/v1/dashboard/summary")
        assert response.status_code in (401, 403)


class TestDashboardTrends:
    """GET /api/v1/dashboard/trends"""

    def test_returns_trend_data(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends?days=7")
        assert response.status_code == 200
        body = response.json()
        assert "days" in body
        assert "run_counts" in body
        assert "eval_pass_rates" in body
        assert "token_spend" in body
        assert body["days"] == 7

    def test_defaults_to_7_days(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends")
        assert response.status_code == 200
        body = response.json()
        assert body["days"] == 7

    def test_accepts_30_days(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends?days=30")
        assert response.status_code == 200
        assert response.json()["days"] == 30

    def test_accepts_90_days(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends?days=90")
        assert response.status_code == 200
        assert response.json()["days"] == 90

    def test_rejects_invalid_days(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends?days=0")
        assert response.status_code == 422

    def test_rejects_too_many_days(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends?days=91")
        assert response.status_code == 422

    def test_requires_auth(self, unauth_client: TestClient) -> None:
        response = unauth_client.get("/api/v1/dashboard/trends")
        assert response.status_code in (401, 403)

    def test_run_counts_structure(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends?days=7")
        assert response.status_code == 200
        body = response.json()
        for entry in body["run_counts"]:
            assert "date" in entry
            assert "run_count" in entry

    def test_eval_pass_rates_structure(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends?days=7")
        assert response.status_code == 200
        body = response.json()
        for entry in body["eval_pass_rates"]:
            assert "date" in entry
            assert "total_evals" in entry
            assert "passed_evals" in entry
            assert "pass_rate" in entry

    def test_token_spend_structure(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends?days=7")
        assert response.status_code == 200
        body = response.json()
        for entry in body["token_spend"]:
            assert "date" in entry
            assert "total_spend_usd" in entry

    def test_hitl_volume_present(self, client: TestClient) -> None:
        """HITL metrics are present in trends response (§8.20)."""
        response = client.get("/api/v1/dashboard/trends?days=7")
        assert response.status_code == 200
        body = response.json()
        assert "hitl_volume" in body
        assert "rejection_trend" in body
        assert "correlation" in body
        assert "feedback_volume" in body

    def test_hitl_volume_structure(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends?days=7")
        assert response.status_code == 200
        body = response.json()
        for entry in body["hitl_volume"]:
            assert "date" in entry
            assert "total_decisions" in entry
            assert "approved_count" in entry
            assert "rejected_count" in entry
            assert "rejection_rate" in entry
            assert "avg_time_to_approve_ms" in entry
            assert entry["avg_time_to_approve_ms"] is None or isinstance(entry["avg_time_to_approve_ms"], (int, float))

    def test_rejection_trend_structure(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends?days=7")
        assert response.status_code == 200
        body = response.json()
        for entry in body["rejection_trend"]:
            assert "date" in entry
            assert "rolling_rejection_rate" in entry
            assert "raw_rejection_rate" in entry

    def test_correlation_structure(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends?days=7")
        assert response.status_code == 200
        body = response.json()
        for entry in body["correlation"]:
            assert "date" in entry
            assert "rejection_rate" in entry
            assert "eval_pass_rate" in entry

    def test_feedback_volume_structure(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends?days=7")
        assert response.status_code == 200
        body = response.json()
        for entry in body["feedback_volume"]:
            assert "date" in entry
            assert "feedback_count" in entry
            assert "resolved_count" in entry
            assert "correcting_count" in entry

    def test_all_trends_align_by_day_count(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends?days=7")
        assert response.status_code == 200
        body = response.json()
        expected_len = 7
        assert len(body["hitl_volume"]) == expected_len
        assert len(body["rejection_trend"]) == expected_len
        assert len(body["correlation"]) == expected_len
        assert len(body["feedback_volume"]) == expected_len
