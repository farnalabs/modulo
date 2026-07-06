"""Unit tests for POST /api/v1/evals/compare, GET /api/v1/evals/coverage,
POST /api/v1/evals/from-run."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

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
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_NODE_1 = uuid.UUID("00000000-0000-0000-0000-000000000020")
_NODE_2 = uuid.UUID("00000000-0000-0000-0000-000000000021")
_EVAL_DEF_1 = uuid.UUID("00000000-0000-0000-0000-000000000030")
_EVAL_DEF_2 = uuid.UUID("00000000-0000-0000-0000-000000000031")
_RUN_A = uuid.UUID("00000000-0000-0000-0000-000000000040")
_RUN_B = uuid.UUID("00000000-0000-0000-0000-000000000041")
_RESULT_A1 = uuid.UUID("00000000-0000-0000-0000-000000000050")
_RESULT_B1 = uuid.UUID("00000000-0000-0000-0000-000000000051")

_DT = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)


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
    session.execute = AsyncMock()
    bind_mock = MagicMock()
    bind_mock.dialect.name = "postgresql"
    session.get_bind = AsyncMock(return_value=bind_mock)
    return session


def _make_result(scalar_one_value=None, scalar_value=None, all_value=None, one_value=None) -> MagicMock:
    m = MagicMock()
    m.scalar_one_or_none = MagicMock(return_value=scalar_one_value)
    if scalar_value is not None:
        m.scalar = MagicMock(return_value=scalar_value)
    if all_value is not None:
        m.all = MagicMock(return_value=all_value)
        m.scalars.return_value = m
    if one_value is not None:
        m.one = MagicMock(return_value=one_value)
    return m


def _make_row(**attrs) -> MagicMock:
    m = MagicMock()
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


@pytest.fixture()
def admin_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
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


@pytest.fixture()
def runner_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
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


# ── POST /api/v1/evals/compare ───────────────────────────────────────────


class TestEvalCompare:
    URL = "/api/v1/evals/compare"

    def test_compare_returns_200(self, admin_client: TestClient) -> None:
        mock_session = _make_mock_session()

        run_a = _make_row(id=_RUN_A, created_at=_DT)
        run_b = _make_row(id=_RUN_B, created_at=_DT)
        eval_def = _make_row(id=_EVAL_DEF_1, name="Test Eval", node_id=_NODE_1)
        result_a = _make_row(
            id=_RESULT_A1,
            run_id=_RUN_A,
            eval_id=_EVAL_DEF_1,
            node_id=_NODE_1,
            passed=True,
            score=0.95,
            detail="Good",
        )
        result_b = _make_row(
            id=_RESULT_B1,
            run_id=_RUN_B,
            eval_id=_EVAL_DEF_1,
            node_id=_NODE_1,
            passed=False,
            score=0.45,
            detail="Bad",
        )

        mock_session.execute.side_effect = [
            _make_result(scalar_one_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_one_value=run_a),
            _make_result(scalar_one_value=run_b),
            _make_result(all_value=[result_a]),
            _make_result(all_value=[result_b]),
            _make_result(all_value=[eval_def]),
        ]

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.post(
            self.URL,
            json={
                "run_id_a": str(_RUN_A),
                "run_id_b": str(_RUN_B),
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["run_a"]["id"] == str(_RUN_A)
        assert data["run_b"]["id"] == str(_RUN_B)
        assert len(data["results"]) == 1
        r = data["results"][0]
        assert r["eval_name"] == "Test Eval"
        assert r["result_a"]["passed"] is True
        assert r["result_b"]["passed"] is False
        assert r["delta"] == 0.5

    def test_compare_run_not_found(self, admin_client: TestClient) -> None:
        mock_session = _make_mock_session()

        mock_session.execute.side_effect = [
            _make_result(scalar_one_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_one_value=None),
        ]

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.post(
            self.URL,
            json={
                "run_id_a": str(uuid.uuid4()),
                "run_id_b": str(_RUN_B),
            },
        )
        assert resp.status_code == 404

    def test_compare_empty_results(self, admin_client: TestClient) -> None:
        mock_session = _make_mock_session()

        run_a = _make_row(id=_RUN_A, created_at=_DT)
        run_b = _make_row(id=_RUN_B, created_at=_DT)

        mock_session.execute.side_effect = [
            _make_result(scalar_one_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_one_value=run_a),
            _make_result(scalar_one_value=run_b),
            _make_result(all_value=[]),
            _make_result(all_value=[]),
            _make_result(all_value=[]),
        ]

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.post(
            self.URL,
            json={
                "run_id_a": str(_RUN_A),
                "run_id_b": str(_RUN_B),
            },
        )
        assert resp.status_code == 200
        assert resp.json()["results"] == []


# ── GET /api/v1/evals/coverage ────────────────────────────────────────────


class TestEvalCoverage:
    URL = "/api/v1/evals/coverage"

    def test_coverage_returns_200(self, admin_client: TestClient) -> None:
        mock_session = _make_mock_session()

        pipeline = _make_row(
            id=_PIPELINE_ID,
            name="Test Pipeline",
            graph_nodes_json=[
                {"id": str(_NODE_1), "name": "Node 1"},
                {"id": str(_NODE_2), "name": "Node 2"},
            ],
        )
        eval_def = _make_row(id=_EVAL_DEF_1, node_id=_NODE_1)

        mock_session.execute.side_effect = [
            _make_result(scalar_one_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_one_value=pipeline),
            _make_result(all_value=[eval_def]),
        ]

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.get(f"{self.URL}?pipeline_id={_PIPELINE_ID}")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["nodes"]) == 2
        assert data["nodes"][0]["has_evals"] is True
        assert data["nodes"][1]["has_evals"] is False
        assert data["summary"]["covered_nodes"] == 1
        assert data["summary"]["uncovered_nodes"] == 1
        assert data["summary"]["coverage_pct"] == 50.0

    def test_coverage_pipeline_not_found(self, admin_client: TestClient) -> None:
        mock_session = _make_mock_session()

        mock_session.execute.side_effect = [
            _make_result(scalar_one_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_one_value=None),
        ]

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.get(f"{self.URL}?pipeline_id={uuid.uuid4()}")
        assert resp.status_code == 404

    def test_coverage_empty_pipeline(self, admin_client: TestClient) -> None:
        mock_session = _make_mock_session()

        pipeline = _make_row(
            id=_PIPELINE_ID,
            name="Empty Pipeline",
            graph_nodes_json=[],
        )

        mock_session.execute.side_effect = [
            _make_result(scalar_one_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_one_value=pipeline),
            _make_result(all_value=[]),
        ]

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.get(f"{self.URL}?pipeline_id={_PIPELINE_ID}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["nodes"] == []
        assert data["summary"]["total_nodes"] == 0
        assert data["summary"]["coverage_pct"] == 0.0


# ── POST /api/v1/evals/from-run ───────────────────────────────────────────


class TestEvalFromRun:
    URL = "/api/v1/evals/from-run"

    def test_from_run_returns_201(self, admin_client: TestClient) -> None:
        mock_session = _make_mock_session()

        run = _make_row(
            id=_RUN_A,
            pipeline_id=_PIPELINE_ID,
            outputs_json={str(_NODE_1): {"result": "hello world"}},
        )

        mock_session.execute.side_effect = [
            _make_result(scalar_one_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_one_value=run),
        ]
        mock_session.add = MagicMock()
        mock_session.flush = AsyncMock()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.post(
            self.URL,
            json={
                "run_id": str(_RUN_A),
                "node_id": str(_NODE_1),
                "eval_type": "regex",
                "name": "My Regex Eval",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "My Regex Eval"
        assert data["eval_type"] == "regex"
        assert "sample_output" in data

    def test_from_run_admin_required(self, runner_client: TestClient) -> None:
        mock_session = _make_mock_session()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = runner_client.post(
            self.URL,
            json={
                "run_id": str(_RUN_A),
                "node_id": str(_NODE_1),
                "eval_type": "regex",
                "name": "My Eval",
            },
        )
        assert resp.status_code == 403

    def test_from_run_run_not_found(self, admin_client: TestClient) -> None:
        mock_session = _make_mock_session()

        mock_session.execute.side_effect = [
            _make_result(scalar_one_value=None),  # set_rls_org
            _make_result(scalar_value=None),  # set_rls_user_context (user_id)
            _make_result(scalar_value=None),  # set_rls_user_context (org_role)
            _make_result(scalar_one_value=None),
        ]

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.post(
            self.URL,
            json={
                "run_id": str(uuid.uuid4()),
                "node_id": str(_NODE_1),
                "eval_type": "json_schema",
                "name": "Schema Eval",
            },
        )
        assert resp.status_code == 404

    def test_from_run_prepopulates_config_by_type(self, admin_client: TestClient) -> None:
        for eval_type in ("regex", "json_schema", "llm_judge", "custom_function"):
            mock_session = _make_mock_session()

            run = _make_row(
                id=_RUN_A,
                pipeline_id=_PIPELINE_ID,
                outputs_json={str(_NODE_1): {"output_field": "test value"}},
            )

            mock_session.execute.side_effect = [
                _make_result(scalar_one_value=None),  # set_rls_org
                _make_result(scalar_value=None),  # set_rls_user_context (user_id)
                _make_result(scalar_value=None),  # set_rls_user_context (org_role)
                _make_result(scalar_one_value=run),
            ]
            mock_session.add = MagicMock()
            mock_session.flush = AsyncMock()

            async def override_session(s=mock_session) -> AsyncGenerator[AsyncMock, None]:
                yield s

            app.dependency_overrides[get_db_session] = override_session
            resp = admin_client.post(
                self.URL,
                json={
                    "run_id": str(_RUN_A),
                    "node_id": str(_NODE_1),
                    "eval_type": eval_type,
                    "name": f"{eval_type} Eval",
                },
            )
            assert resp.status_code == 201, f"Failed for {eval_type}"
            data = resp.json()
            assert data["eval_type"] == eval_type
            assert "field" in data["config_json"]
