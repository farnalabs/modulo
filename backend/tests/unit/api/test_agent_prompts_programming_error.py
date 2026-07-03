"""Unit tests for agent prompt optimization endpoints — ProgrammingError→501."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

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
_AGENT_ID = uuid.uuid4()


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
    default_result = MagicMock()
    default_result.scalar_one_or_none.return_value = MagicMock()
    session.execute = AsyncMock(side_effect=ProgrammingError("mock", {}, ""))
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
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestOptimizePromptProgrammingError:
    URL = f"/api/v1/agents/{_AGENT_ID}/prompts/v1/optimize"

    def test_optimize_returns_501_on_programming_error(self, client: TestClient) -> None:
        resp = client.post(self.URL, json={"eval_result_ids": [str(uuid.uuid4())]})
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestGetPromptVersionProgrammingError:
    URL = f"/api/v1/agents/{_AGENT_ID}/prompts/v1"

    def test_get_version_returns_501_on_programming_error(self, client: TestClient) -> None:
        resp = client.get(self.URL)
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestListPromptVersionsProgrammingError:
    URL = f"/api/v1/agents/{_AGENT_ID}/prompts"

    def test_list_versions_returns_501_on_programming_error(self, client: TestClient) -> None:
        resp = client.get(self.URL)
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestRollbackPromptProgrammingError:
    URL = f"/api/v1/agents/{_AGENT_ID}/prompts/rollback/v1"

    def test_rollback_returns_501_on_programming_error(self, client: TestClient) -> None:
        resp = client.put(self.URL, json={})
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestDiffPromptProgrammingError:
    URL = f"/api/v1/agents/{_AGENT_ID}/prompts/diff"

    def test_diff_returns_501_on_programming_error(self, client: TestClient) -> None:
        resp = client.post(self.URL, json={"version_a": "v1", "version_b": "v2"})
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


class TestApplyOptimizedPromptProgrammingError:
    URL = f"/api/v1/agents/{_AGENT_ID}/prompts/v2/apply"

    def test_apply_returns_501_on_programming_error(self, client: TestClient) -> None:
        resp = client.post(self.URL, json={"suggested_prompt": "New prompt"})
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()
