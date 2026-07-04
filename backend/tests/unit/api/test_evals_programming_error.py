"""Unit tests: eval CRUD routes return 501 on ProgrammingError.

Tests that all 8 eval-related route handlers gracefully return
501 Not Implemented when the database raises ProgrammingError
(e.g. missing table because migrations haven't run yet).
"""

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

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


def _make_session_raising_programming_error() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(side_effect=ProgrammingError("relation does not exist", None, None))
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    bind_mock = MagicMock()
    bind_mock.dialect.name = "postgresql"
    session.get_bind = AsyncMock(return_value=bind_mock)
    return session


def _make_session_raising_sqlalchemy_error() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(side_effect=SQLAlchemyError("statement", "params", "orig"))
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    bind_mock = MagicMock()
    bind_mock.dialect.name = "postgresql"
    session.get_bind = AsyncMock(return_value=bind_mock)
    return session


def _make_session_with_data_second_begin_raises(exc_class: type[Exception], exc_args: tuple) -> AsyncMock:
    """Returns a session that succeeds on first begin(), raises exc_class on second.
    Execute mocks return valid data so code reaches the second try block."""
    call_count: list[int] = [0]

    async def _aenter_impl() -> AsyncMock:
        call_count[0] += 1
        if call_count[0] == 2:
            raise exc_class(*exc_args)
        return AsyncMock()

    session = AsyncMock()
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

    eval_result_mock = MagicMock()
    eval_result_mock.eval_id = _EVAL_DEF_ID
    eval_result_mock.node_id = None
    eval_result_mock.passed = True
    eval_result_mock.score = 1.0
    eval_result_mock.detail = None
    eval_result_mock.evaluated_at = None

    scalars_chain = MagicMock()
    scalars_chain.all.return_value = [eval_result_mock]

    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = run_mock
    execute_result.scalars.return_value = scalars_chain

    session.execute = AsyncMock(return_value=execute_result)

    return session


@pytest.fixture()
def admin_client() -> TestClient:
    app.dependency_overrides[get_settings] = _make_settings
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


class TestCreateEvalDefinitionProgrammingError:
    URL = "/api/v1/evals"

    def test_create_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.post(
            self.URL,
            json={
                "pipeline_id": str(_PIPELINE_ID),
                "name": "Test Eval",
                "eval_type": "regex",
            },
        )
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    def test_create_returns_503_on_sqlalchemy_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_sqlalchemy_error()
        _override_session(session)
        resp = admin_client.post(
            self.URL,
            json={
                "pipeline_id": str(_PIPELINE_ID),
                "name": "Test Eval",
                "eval_type": "regex",
            },
        )
        assert resp.status_code == 503
        assert "database" in resp.json()["detail"].lower()


class TestListEvalDefinitionsProgrammingError:
    URL = "/api/v1/evals"

    def test_list_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.get(self.URL)
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    def test_list_returns_503_on_sqlalchemy_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_sqlalchemy_error()
        _override_session(session)
        resp = admin_client.get(self.URL)
        assert resp.status_code == 503
        assert "database" in resp.json()["detail"].lower()


class TestGetEvalDefinitionProgrammingError:
    URL = "/api/v1/evals"

    def test_get_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.get(f"{self.URL}/{_EVAL_DEF_ID}")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    def test_get_returns_503_on_sqlalchemy_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_sqlalchemy_error()
        _override_session(session)
        resp = admin_client.get(f"{self.URL}/{_EVAL_DEF_ID}")
        assert resp.status_code == 503
        assert "database" in resp.json()["detail"].lower()


class TestUpdateEvalDefinitionProgrammingError:
    URL = "/api/v1/evals"

    def test_update_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.put(
            f"{self.URL}/{_EVAL_DEF_ID}",
            json={"name": "Updated"},
        )
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    def test_update_returns_503_on_sqlalchemy_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_sqlalchemy_error()
        _override_session(session)
        resp = admin_client.put(
            f"{self.URL}/{_EVAL_DEF_ID}",
            json={"name": "Updated"},
        )
        assert resp.status_code == 503
        assert "database" in resp.json()["detail"].lower()


class TestDeleteEvalDefinitionProgrammingError:
    URL = "/api/v1/evals"

    def test_delete_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.delete(f"{self.URL}/{_EVAL_DEF_ID}")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    def test_delete_returns_503_on_sqlalchemy_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_sqlalchemy_error()
        _override_session(session)
        resp = admin_client.delete(f"{self.URL}/{_EVAL_DEF_ID}")
        assert resp.status_code == 503
        assert "database" in resp.json()["detail"].lower()


class TestEvalCoverageProgrammingError:
    URL = "/api/v1/evals/coverage"

    def test_coverage_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.get(f"{self.URL}?pipeline_id={_PIPELINE_ID}")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    def test_coverage_returns_503_on_sqlalchemy_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_sqlalchemy_error()
        _override_session(session)
        resp = admin_client.get(f"{self.URL}?pipeline_id={_PIPELINE_ID}")
        assert resp.status_code == 503
        assert "database" in resp.json()["detail"].lower()


class TestListRunEvalsProgrammingError:
    URL = "/api/v1/runs"

    def test_list_run_evals_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.get(f"{self.URL}/{_RUN_ID}/evals")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    def test_list_run_evals_returns_503_on_sqlalchemy_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_sqlalchemy_error()
        _override_session(session)
        resp = admin_client.get(f"{self.URL}/{_RUN_ID}/evals")
        assert resp.status_code == 503
        assert "database" in resp.json()["detail"].lower()


class TestCompareEvalsProgrammingError:
    URL = "/api/v1/evals/compare"

    def test_compare_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.post(
            self.URL,
            json={
                "run_id_a": str(_RUN_ID),
                "run_id_b": str(_RUN_ID),
            },
        )
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    def test_compare_returns_503_on_sqlalchemy_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_sqlalchemy_error()
        _override_session(session)
        resp = admin_client.post(
            self.URL,
            json={
                "run_id_a": str(_RUN_ID),
                "run_id_b": str(_RUN_ID),
            },
        )
        assert resp.status_code == 503
        assert "database" in resp.json()["detail"].lower()

    def test_compare_returns_503_on_sqlalchemy_error_second_block(self, admin_client: TestClient) -> None:
        session = _make_session_with_data_second_begin_raises(SQLAlchemyError, ("statement", "params", "orig"))
        _override_session(session)
        resp = admin_client.post(
            self.URL,
            json={
                "run_id_a": str(_RUN_ID),
                "run_id_b": str(_RUN_ID),
            },
        )
        assert resp.status_code == 503
        assert "database" in resp.json()["detail"].lower()


class TestCreateEvalFromRunProgrammingError:
    URL = "/api/v1/evals/from-run"

    def test_from_run_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.post(
            self.URL,
            json={
                "run_id": str(_RUN_ID),
                "node_id": str(uuid.uuid4()),
                "eval_type": "regex",
                "name": "From Run Eval",
            },
        )
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    def test_from_run_returns_503_on_sqlalchemy_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_sqlalchemy_error()
        _override_session(session)
        resp = admin_client.post(
            self.URL,
            json={
                "run_id": str(_RUN_ID),
                "node_id": str(uuid.uuid4()),
                "eval_type": "regex",
                "name": "From Run Eval",
            },
        )
        assert resp.status_code == 503
        assert "database" in resp.json()["detail"].lower()

    def test_from_run_returns_503_on_sqlalchemy_error_second_block(self, admin_client: TestClient) -> None:
        session = _make_session_with_data_second_begin_raises(SQLAlchemyError, ("statement", "params", "orig"))
        _override_session(session)
        resp = admin_client.post(
            self.URL,
            json={
                "run_id": str(_RUN_ID),
                "node_id": str(uuid.uuid4()),
                "eval_type": "regex",
                "name": "From Run Eval",
            },
        )
        assert resp.status_code == 503
        assert "database" in resp.json()["detail"].lower()
