"""Test prompt endpoints return structured errors on DB failures (both ProgrammingError and SQLAlchemyError)."""

import json
import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.prompt_optimizer import OptimizationFailedError
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

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


def _make_mock_session(*, configure_execute: bool = False):
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    if configure_execute:
        mock_mb_result = MagicMock()
        mock_mb = MagicMock()
        mock_mb.id = uuid.uuid4()
        mock_mb.provider = "stub"
        mock_mb.model_id = "test"
        mock_mb.default_params = {}
        mock_mb_result.scalar_one_or_none = MagicMock(return_value=mock_mb)
        session.execute = AsyncMock(return_value=mock_mb_result)
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


@pytest.fixture()
def client_with_execute():
    mock_session = _make_mock_session(configure_execute=True)

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


class TestPromptOptimizeLLMErrorPaths:
    URL = f"/api/v1/agents/{_AGENT_ID}/prompts/v1/optimize"

    def _make_agent(self):
        agent = MagicMock()
        agent.id = uuid.UUID(_AGENT_ID)
        agent.organisation_id = uuid.UUID(_ORG_ID)
        agent.prompt_template = "You are {{name}}"
        agent.model_backend_id = uuid.uuid4()
        agent.template_id = None
        agent.agent_command = None
        agent.prompt_version_history = []
        return agent

    def test_optimization_failed_error_returns_structured_500(self, client_with_execute):
        with (
            patch("modulo.api.routes.agents.get_agent", return_value=self._make_agent()),
            patch("modulo.api.routes.agents.set_rls_org"),
            patch(
                "modulo.api.routes.agents.get_eval_results_with_defs",
                return_value=([{"id": str(uuid.uuid4()), "eval_id": str(uuid.uuid4()), "passed": False}], {}),
            ),
            patch("modulo.api.routes.agents.create_secrets_backend") as mock_secrets_factory,
            patch("modulo.core.model_backend_hub._build_backend"),
            patch(
                "modulo.api.routes.agents.PromptOptimizer.optimize",
                side_effect=OptimizationFailedError("LLM call failed after 3 attempts"),
            ),
        ):
            mock_secrets = MagicMock()
            mock_secrets.get_secret = AsyncMock(return_value=json.dumps({"api_key": "test"}))
            mock_secrets_factory.return_value = mock_secrets

            resp = client_with_execute.post(
                self.URL,
                json={"eval_result_ids": ["00000000-0000-0000-0000-000000000001"]},
            )
        assert resp.status_code == 500
        data = resp.json()
        assert "LLM call failed" in data["detail"]

    def test_optimize_unexpected_error_returns_structured_500(self, client_with_execute):
        with (
            patch("modulo.api.routes.agents.get_agent", return_value=self._make_agent()),
            patch("modulo.api.routes.agents.set_rls_org"),
            patch(
                "modulo.api.routes.agents.get_eval_results_with_defs",
                return_value=([{"id": str(uuid.uuid4()), "eval_id": str(uuid.uuid4()), "passed": False}], {}),
            ),
            patch("modulo.api.routes.agents.create_secrets_backend") as mock_secrets_factory,
            patch("modulo.core.model_backend_hub._build_backend"),
            patch(
                "modulo.api.routes.agents.PromptOptimizer.optimize",
                side_effect=RuntimeError("unexpected crash"),
            ),
        ):
            mock_secrets = MagicMock()
            mock_secrets.get_secret = AsyncMock(return_value=json.dumps({"api_key": "test"}))
            mock_secrets_factory.return_value = mock_secrets

            resp = client_with_execute.post(
                self.URL,
                json={"eval_result_ids": ["00000000-0000-0000-0000-000000000001"]},
            )
        assert resp.status_code == 500
        data = resp.json()
        assert "unexpectedly" in data["detail"]


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
