"""Unit tests for GET /api/v1/admin/evals/dashboard."""

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
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_PIPELINE_ID_2 = uuid.UUID("00000000-0000-0000-0000-000000000011")
_NODE_1 = uuid.UUID("00000000-0000-0000-0000-000000000020")
_NODE_2 = uuid.UUID("00000000-0000-0000-0000-000000000021")
_EVAL_DEF_ID = uuid.UUID("00000000-0000-0000-0000-000000000030")
_EVAL_RESULT_1 = uuid.UUID("00000000-0000-0000-0000-000000000040")
_EVAL_RESULT_2 = uuid.UUID("00000000-0000-0000-0000-000000000041")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    configure_mock_session(session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock()
    bind_mock = MagicMock()
    bind_mock.dialect.name = "postgresql"
    session.get_bind = MagicMock(return_value=bind_mock)
    return session


def _make_row(**attrs) -> MagicMock:
    """Create a MagicMock with explicit attributes (avoiding kwargs pitfall)."""
    m = MagicMock()
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


def _make_result(one_value=None, scalar_value=None, all_value=None) -> MagicMock:
    """Create a mock result from session.execute()."""
    m = MagicMock()
    if one_value is not None:
        m.one = MagicMock(return_value=one_value)
    if scalar_value is not None:
        m.scalar = MagicMock(return_value=scalar_value)
    if all_value is not None:
        m.all = MagicMock(return_value=all_value)
    return m


def _configure_execute(
    session: AsyncMock,
    *,
    total_results: int = 10,
    passed: int = 7,
    failed: int = 3,
    total_defs: int = 5,
    trend_rows: list | None = None,
    type_rows: list | None = None,
    pipeline_rows: list | None = None,
    eval_def_rows: list | None = None,
    recent_rows: list | None = None,
) -> None:
    if trend_rows is None:
        trend_rows = [_make_row(bucket="2025-06-01", total=5, passed=3, failed=2)]

    if type_rows is None:
        type_rows = [_make_row(eval_type="llm_judge", total=6, passed=4, failed=2)]

    if pipeline_rows is None:
        pipeline_rows = [
            _make_row(
                id=_PIPELINE_ID,
                name="Data Pipeline",
                graph_nodes_json=[{"id": str(_NODE_1)}, {"id": str(_NODE_2)}],
            )
        ]

    if eval_def_rows is None:
        eval_def_rows = [_make_row(pipeline_id=_PIPELINE_ID, node_id=_NODE_1)]

    if recent_rows is None:
        recent_rows = [
            _make_row(
                id=_EVAL_RESULT_1,
                eval_id=_EVAL_DEF_ID,
                eval_name="Test Eval",
                eval_type="llm_judge",
                passed=True,
                score=0.95,
                detail="All good",
                evaluated_at="2025-06-01T12:00:00+00:00",
            )
        ]

    summary_row = _make_row(total_results=total_results, passed=passed, failed=failed)

    # First three calls come from set_rls_org() + set_rls_user_context()
    # (org id, user id, org role), then the 7 dashboard queries
    session.execute.side_effect = [
        _make_result(scalar_value=None),  # set_rls_org SELECT set_config
        _make_result(scalar_value=None),  # set_rls_user_context app.user_id
        _make_result(scalar_value=None),  # set_rls_user_context app.org_role
        _make_result(one_value=summary_row),
        _make_result(scalar_value=total_defs),
        _make_result(all_value=trend_rows),
        _make_result(all_value=type_rows),
        _make_result(all_value=pipeline_rows),
        _make_result(all_value=eval_def_rows),
        _make_result(all_value=recent_rows),
    ]


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()
    _configure_execute(mock_session)

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
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def operator_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()
    _configure_execute(mock_session)

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
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def runner_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()
    _configure_execute(mock_session)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="runner",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="runner",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestEvalDashboardAuth:
    URL = "/api/v1/admin/evals/dashboard"

    def test_unauthorized_returns_401(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(self.URL)
        assert resp.status_code in (401, 403)

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.get(self.URL)
        assert resp.status_code == 403

    def test_runner_returns_403(self, runner_client: TestClient) -> None:
        resp = runner_client.get(self.URL)
        assert resp.status_code == 403

    def test_admin_returns_200(self, client: TestClient) -> None:
        resp = client.get(self.URL)
        assert resp.status_code == 200


class TestEvalDashboardSections:
    URL = "/api/v1/admin/evals/dashboard"

    def test_summary_section(self, client: TestClient) -> None:
        resp = client.get(self.URL)
        data = resp.json()
        s = data["summary"]
        assert s["total_results"] == 10
        assert s["passed"] == 7
        assert s["failed"] == 3
        assert s["pass_rate"] == pytest.approx(0.7)
        assert s["total_definitions"] == 5

    def test_trend_section(self, client: TestClient) -> None:
        resp = client.get(self.URL)
        data = resp.json()
        assert len(data["trend"]) == 1
        t = data["trend"][0]
        assert t["bucket"] == "2025-06-01"
        assert t["total"] == 5
        assert t["passed"] == 3
        assert t["failed"] == 2

    def test_by_type_section(self, client: TestClient) -> None:
        resp = client.get(self.URL)
        data = resp.json()
        assert len(data["by_type"]) == 1
        bt = data["by_type"][0]
        assert bt["eval_type"] == "llm_judge"
        assert bt["total"] == 6
        assert bt["passed"] == 4
        assert bt["failed"] == 2

    def test_coverage_gaps_found(self, client: TestClient) -> None:
        resp = client.get(self.URL)
        data = resp.json()
        assert len(data["coverage_gaps"]) == 1
        gap = data["coverage_gaps"][0]
        assert gap["pipeline_id"] == str(_PIPELINE_ID)
        assert gap["pipeline_name"] == "Data Pipeline"
        assert gap["node_id"] == str(_NODE_2)

    def test_coverage_gaps_none(self, client: TestClient) -> None:
        mock_session = _make_mock_session()
        _configure_execute(
            mock_session,
            eval_def_rows=[
                _make_row(pipeline_id=_PIPELINE_ID, node_id=_NODE_1),
                _make_row(pipeline_id=_PIPELINE_ID, node_id=_NODE_2),
            ],
        )

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = client.get(self.URL)
        data = resp.json()
        assert not data["coverage_gaps"]

    def test_recent_results_section(self, client: TestClient) -> None:
        resp = client.get(self.URL)
        data = resp.json()
        assert len(data["recent_results"]) == 1
        rr = data["recent_results"][0]
        assert rr["eval_name"] == "Test Eval"
        assert rr["eval_type"] == "llm_judge"
        assert rr["passed"] is True
        assert rr["score"] == pytest.approx(0.95)

    def test_all_five_keys_present(self, client: TestClient) -> None:
        resp = client.get(self.URL)
        data = resp.json()
        assert set(data.keys()) == {
            "summary",
            "trend",
            "by_type",
            "coverage_gaps",
            "recent_results",
        }


class TestEvalDashboardEmptyState:
    URL = "/api/v1/admin/evals/dashboard"

    def test_empty_database(self, client: TestClient) -> None:
        mock_session = _make_mock_session()

        summary_row = _make_row(total_results=0, passed=0, failed=0)

        mock_session.execute.side_effect = [
            _make_result(scalar_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context app.user_id
            _make_result(scalar_value=None),  # set_rls_user_context app.org_role
            _make_result(one_value=summary_row),
            _make_result(scalar_value=0),
            _make_result(all_value=[]),
            _make_result(all_value=[]),
            _make_result(all_value=[]),
            _make_result(all_value=[]),
            _make_result(all_value=[]),
        ]

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = client.get(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"]["total_results"] == 0
        assert data["summary"]["pass_rate"] == 0.0
        assert not data["trend"]
        assert not data["by_type"]
        assert not data["coverage_gaps"]
        assert not data["recent_results"]


class TestEvalDashboardMultiType:
    URL = "/api/v1/admin/evals/dashboard"

    def test_multiple_type_breakdown(self, client: TestClient) -> None:
        mock_session = _make_mock_session()

        summary_row = _make_row(total_results=10, passed=6, failed=4)
        t1 = _make_row(eval_type="llm_judge", total=5, passed=4, failed=1)
        t2 = _make_row(eval_type="regex", total=3, passed=1, failed=2)
        t3 = _make_row(eval_type="json_schema", total=2, passed=1, failed=1)

        mock_session.execute.side_effect = [
            _make_result(scalar_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context app.user_id
            _make_result(scalar_value=None),  # set_rls_user_context app.org_role
            _make_result(one_value=summary_row),
            _make_result(scalar_value=3),
            _make_result(all_value=[]),
            _make_result(all_value=[t1, t2, t3]),
            _make_result(all_value=[]),
            _make_result(all_value=[]),
            _make_result(all_value=[]),
        ]

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = client.get(self.URL)
        assert resp.status_code == 200
        by_type = resp.json()["by_type"]
        assert len(by_type) == 3
        types = {bt["eval_type"]: bt for bt in by_type}
        assert types["llm_judge"]["passed"] == 4
        assert types["regex"]["failed"] == 2
        assert types["json_schema"]["total"] == 2
