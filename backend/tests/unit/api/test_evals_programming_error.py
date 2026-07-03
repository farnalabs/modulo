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
from sqlalchemy.exc import ProgrammingError

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


class TestListEvalDefinitionsProgrammingError:
    URL = "/api/v1/evals"

    def test_list_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.get(self.URL)
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestGetEvalDefinitionProgrammingError:
    URL = "/api/v1/evals"

    def test_get_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.get(f"{self.URL}/{_EVAL_DEF_ID}")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


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


class TestDeleteEvalDefinitionProgrammingError:
    URL = "/api/v1/evals"

    def test_delete_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.delete(f"{self.URL}/{_EVAL_DEF_ID}")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestEvalCoverageProgrammingError:
    URL = "/api/v1/evals/coverage"

    def test_coverage_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.get(f"{self.URL}?pipeline_id={_PIPELINE_ID}")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestListRunEvalsProgrammingError:
    URL = "/api/v1/runs"

    def test_list_run_evals_returns_501_on_programming_error(self, admin_client: TestClient) -> None:
        session = _make_session_raising_programming_error()
        _override_session(session)
        resp = admin_client.get(f"{self.URL}/{_RUN_ID}/evals")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


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
