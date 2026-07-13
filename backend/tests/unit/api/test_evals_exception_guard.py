"""Unit tests: eval CRUD routes catch Exception + IntegrityError gracefully.

Tests that all route handlers return structured 500 on unexpected Python
errors (e.g. TypeError, KeyError) and 409 on IntegrityError (FK violations).
"""

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

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
_EVAL_DEF_ID = uuid.UUID("00000000-0000-0000-0000-000000000030")
_RUN_ID = uuid.UUID("00000000-0000-0000-0000-000000000040")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_session_raising(exception: Exception) -> AsyncMock:
    session = AsyncMock()
    configure_mock_session(session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(side_effect=exception)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    bind_mock = MagicMock()
    bind_mock.dialect.name = "postgresql"
    session.get_bind = AsyncMock(return_value=bind_mock)
    return session


def _make_session_raising_integrity_error(msg: str = "violates foreign key constraint") -> AsyncMock:
    return _make_session_raising(IntegrityError(msg, None, None))


@pytest.fixture()
def admin_client() -> TestClient:
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def _override_session(session) -> None:
    async def _get_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_db_session] = _get_session


class TestCreateEvalExceptionGuard:
    URL = "/api/v1/evals"

    def test_create_exception_returns_500(self, admin_client: TestClient) -> None:
        session = _make_session_raising(TypeError("unexpected type"))
        _override_session(session)
        resp = admin_client.post(
            self.URL,
            json={"pipeline_id": str(_PIPELINE_ID), "name": "Test", "eval_type": "regex"},
        )
        assert resp.status_code == 500
        assert "unexpected error" in resp.json()["detail"].lower()

    def test_create_integrity_error_returns_409(self, admin_client: TestClient) -> None:
        session = _make_session_raising_integrity_error()
        _override_session(session)
        resp = admin_client.post(
            self.URL,
            json={"pipeline_id": str(_PIPELINE_ID), "name": "Test", "eval_type": "regex"},
        )
        assert resp.status_code == 409
        assert "does not exist" in resp.json()["detail"].lower()

    def test_create_pass_threshold_out_of_range_rejected(self, admin_client: TestClient) -> None:
        mock_session = AsyncMock()
        configure_mock_session(mock_session)
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = MagicMock(return_value=begin_cm)
        bind_mock = MagicMock()
        bind_mock.dialect.name = "postgresql"
        mock_session.get_bind = AsyncMock(return_value=bind_mock)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.post(
            self.URL,
            json={
                "pipeline_id": str(_PIPELINE_ID),
                "name": "Bad Threshold",
                "eval_type": "regex",
                "pass_threshold": 2.5,
            },
        )
        assert resp.status_code == 422


class TestGetEvalExceptionGuard:
    URL = "/api/v1/evals"

    def test_get_exception_returns_500(self, admin_client: TestClient) -> None:
        session = _make_session_raising(ValueError("bad data"))
        _override_session(session)
        resp = admin_client.get(f"{self.URL}/{_EVAL_DEF_ID}")
        assert resp.status_code == 500
        assert "unexpected error" in resp.json()["detail"].lower()


class TestUpdateEvalExceptionGuard:
    URL = "/api/v1/evals"

    def test_update_exception_returns_500(self, admin_client: TestClient) -> None:
        session = _make_session_raising(KeyError("missing_key"))
        _override_session(session)
        resp = admin_client.put(
            f"{self.URL}/{_EVAL_DEF_ID}",
            json={"name": "Updated"},
        )
        assert resp.status_code == 500
        assert "unexpected error" in resp.json()["detail"].lower()

    def test_update_integrity_error_returns_409(self, admin_client: TestClient) -> None:
        session = _make_session_raising_integrity_error()
        _override_session(session)
        resp = admin_client.put(
            f"{self.URL}/{_EVAL_DEF_ID}",
            json={"name": "Updated"},
        )
        assert resp.status_code == 409
        assert "constraint" in resp.json()["detail"].lower()

    def test_update_pass_threshold_out_of_range_rejected(self, admin_client: TestClient) -> None:
        mock_session = AsyncMock()
        configure_mock_session(mock_session)
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = MagicMock(return_value=begin_cm)
        bind_mock = MagicMock()
        bind_mock.dialect.name = "postgresql"
        mock_session.get_bind = AsyncMock(return_value=bind_mock)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        resp = admin_client.put(
            f"{self.URL}/{_EVAL_DEF_ID}",
            json={"pass_threshold": -0.5},
        )
        assert resp.status_code == 422


class TestDeleteEvalExceptionGuard:
    URL = "/api/v1/evals"

    def test_delete_exception_returns_500(self, admin_client: TestClient) -> None:
        session = _make_session_raising(AttributeError("NoneType has no attribute"))
        _override_session(session)
        resp = admin_client.delete(f"{self.URL}/{_EVAL_DEF_ID}")
        assert resp.status_code == 500
        assert "unexpected error" in resp.json()["detail"].lower()


class TestListEvalExceptionGuard:
    URL = "/api/v1/evals"

    def test_list_exception_returns_500(self, admin_client: TestClient) -> None:
        session = _make_session_raising(RuntimeError("unexpected runtime error"))
        _override_session(session)
        resp = admin_client.get(self.URL)
        assert resp.status_code == 500
        assert "unexpected error" in resp.json()["detail"].lower()


class TestCoverageExceptionGuard:
    URL = "/api/v1/evals/coverage"

    def test_coverage_exception_returns_500(self, admin_client: TestClient) -> None:
        session = _make_session_raising(RuntimeError("engine failure"))
        _override_session(session)
        resp = admin_client.get(f"{self.URL}?pipeline_id={_PIPELINE_ID}")
        assert resp.status_code == 500
        assert "unexpected error" in resp.json()["detail"].lower()


class TestListRunEvalsExceptionGuard:
    URL = "/api/v1/runs"

    def test_list_run_evals_exception_returns_500(self, admin_client: TestClient) -> None:
        session = _make_session_raising(RuntimeError("unexpected"))
        _override_session(session)
        resp = admin_client.get(f"{self.URL}/{_RUN_ID}/evals")
        assert resp.status_code == 500
        assert "unexpected error" in resp.json()["detail"].lower()


class TestCompareEvalsExceptionGuard:
    URL = "/api/v1/evals/compare"

    def test_compare_exception_returns_500_first_block(self, admin_client: TestClient) -> None:
        session = _make_session_raising(RuntimeError("first block error"))
        _override_session(session)
        resp = admin_client.post(
            self.URL,
            json={"run_id_a": str(_RUN_ID), "run_id_b": str(_RUN_ID)},
        )
        assert resp.status_code == 500
        assert "unexpected error" in resp.json()["detail"].lower()


class TestCreateFromRunExceptionGuard:
    URL = "/api/v1/evals/from-run"

    def test_from_run_exception_returns_500(self, admin_client: TestClient) -> None:
        session = _make_session_raising(RuntimeError("unexpected"))
        _override_session(session)
        resp = admin_client.post(
            self.URL,
            json={
                "run_id": str(_RUN_ID),
                "node_id": str(uuid.uuid4()),
                "eval_type": "regex",
                "name": "From Run",
            },
        )
        assert resp.status_code == 500
        assert "unexpected error" in resp.json()["detail"].lower()

    def test_from_run_integrity_error_returns_409_second_block(self, admin_client: TestClient) -> None:
        """Second transaction block (create eval def) raises IntegrityError."""
        call_count: list[int] = [0]

        async def _aenter_impl() -> AsyncMock:
            call_count[0] += 1
            if call_count[0] == 1:
                return AsyncMock()
            raise IntegrityError("violates FK constraint", None, None)

        session = AsyncMock()
        configure_mock_session(session)
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(side_effect=_aenter_impl)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)
        bind_mock = MagicMock()
        bind_mock.dialect.name = "postgresql"
        session.get_bind = AsyncMock(return_value=bind_mock)

        run_mock = MagicMock()
        run_mock.id = _RUN_ID
        run_mock.pipeline_id = _PIPELINE_ID
        run_mock.created_at = None
        run_mock.outputs_json = {}
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run_mock)))

        _override_session(session)
        resp = admin_client.post(
            self.URL,
            json={
                "run_id": str(_RUN_ID),
                "node_id": str(uuid.uuid4()),
                "eval_type": "regex",
                "name": "From Run Integrity",
            },
        )
        assert resp.status_code == 409
        assert "does not exist" in resp.json()["detail"].lower()
