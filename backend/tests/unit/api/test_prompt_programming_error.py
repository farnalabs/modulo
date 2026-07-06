"""Test prompt endpoints return structured errors on DB failures (both ProgrammingError and SQLAlchemyError)."""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = "00000000-0000-0000-0000-000000000001"
_USER_ID = "00000000-0000-0000-0000-000000000002"
_AGENT_ID = "00000000-0000-0000-0000-000000000099"


def _make_settings():
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session():
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture()
def client():
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


class TestPromptOptimizeErrorPaths:
    URL = f"/api/v1/agents/{_AGENT_ID}/prompts/v1/optimize"

    def test_programming_error_returns_501(self, client):
        with (
            patch("modulo.api.routes.agents.get_agent", side_effect=ProgrammingError("stmt", {}, None)),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.post(self.URL, json={"eval_result_ids": ["00000000-0000-0000-0000-000000000001"]})
        assert resp.status_code == 501

    def test_sqlalchemy_error_returns_503(self, client):
        with (
            patch("modulo.api.routes.agents.get_agent", side_effect=SQLAlchemyError("connection failed")),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.post(self.URL, json={"eval_result_ids": ["00000000-0000-0000-0000-000000000001"]})
        assert resp.status_code == 503


class TestPromptApplyErrorPaths:
    URL = f"/api/v1/agents/{_AGENT_ID}/prompts/v1/apply"

    def test_programming_error_returns_501(self, client):
        with (
            patch("modulo.api.routes.agents.add_prompt_version", side_effect=ProgrammingError("stmt", {}, None)),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.post(self.URL, json={"suggested_prompt": "New prompt"})
        assert resp.status_code == 501

    def test_sqlalchemy_error_returns_503(self, client):
        with (
            patch("modulo.api.routes.agents.add_prompt_version", side_effect=SQLAlchemyError("connection failed")),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.post(self.URL, json={"suggested_prompt": "New prompt"})
        assert resp.status_code == 503


class TestPromptListVersionsErrorPaths:
    URL = f"/api/v1/agents/{_AGENT_ID}/prompts"

    def test_programming_error_returns_501(self, client):
        with (
            patch("modulo.api.routes.agents.get_agent", side_effect=ProgrammingError("stmt", {}, None)),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.get(self.URL)
        assert resp.status_code == 501

    def test_sqlalchemy_error_returns_503(self, client):
        with (
            patch("modulo.api.routes.agents.get_agent", side_effect=SQLAlchemyError("connection failed")),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.get(self.URL)
        assert resp.status_code == 503


class TestPromptGetVersionErrorPaths:
    URL = f"/api/v1/agents/{_AGENT_ID}/prompts/v1"

    def test_programming_error_returns_501(self, client):
        with (
            patch("modulo.api.routes.agents.get_prompt_version", side_effect=ProgrammingError("stmt", {}, None)),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.get(self.URL)
        assert resp.status_code == 501

    def test_sqlalchemy_error_returns_503(self, client):
        with (
            patch("modulo.api.routes.agents.get_prompt_version", side_effect=SQLAlchemyError("connection failed")),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.get(self.URL)
        assert resp.status_code == 503


class TestPromptRollbackErrorPaths:
    URL = f"/api/v1/agents/{_AGENT_ID}/prompts/rollback/v1"

    def test_programming_error_returns_501(self, client):
        with (
            patch("modulo.api.routes.agents.rollback_prompt_version", side_effect=ProgrammingError("stmt", {}, None)),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.put(self.URL, json={})
        assert resp.status_code == 501

    def test_sqlalchemy_error_returns_503(self, client):
        with (
            patch("modulo.api.routes.agents.rollback_prompt_version", side_effect=SQLAlchemyError("connection failed")),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.put(self.URL, json={})
        assert resp.status_code == 503


class TestPromptDiffErrorPaths:
    URL = f"/api/v1/agents/{_AGENT_ID}/prompts/diff"

    def test_programming_error_returns_501(self, client):
        with (
            patch("modulo.api.routes.agents.get_agent", side_effect=ProgrammingError("stmt", {}, None)),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.post(self.URL, json={"version_a": "v1", "version_b": "v2"})
        assert resp.status_code == 501

    def test_sqlalchemy_error_returns_503(self, client):
        with (
            patch("modulo.api.routes.agents.get_agent", side_effect=SQLAlchemyError("connection failed")),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.post(self.URL, json={"version_a": "v1", "version_b": "v2"})
        assert resp.status_code == 503
