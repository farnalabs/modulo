"""Unit tests for agent prompt optimization and apply endpoints."""

import base64
import json
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_AGENT_ID = uuid.uuid4()
_SCHEMA_ID = uuid.uuid4()
_BACKEND_ID = uuid.uuid4()
_EVAL_ID = uuid.uuid4()
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=base64.urlsafe_b64encode(b"a" * 32).decode(),
        modulo_admin_password="testpass",
    )


def _make_agent() -> MagicMock:
    a = MagicMock()
    a.id = _AGENT_ID
    a.organisation_id = _ORG_ID
    a.name = "Prompt Agent"
    a.description = None
    a.input_schema_id = _SCHEMA_ID
    a.input_schema_version = "1.0"
    a.output_schema_id = _SCHEMA_ID
    a.output_schema_version = "1.0"
    a.prompt_template = "You are an assistant. Answer: {{query}}"
    a.model_backend_id = _BACKEND_ID
    a.connector_type_refs = []
    a.evals = []
    a.retry_policy = {}
    a.token_budget = None
    a.library_id = None
    a.account_id = _USER_ID
    a.created_by = _USER_ID
    a.created_at = _NOW
    a.updated_at = _NOW
    a.prompt_version_history = []
    return a


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()

    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    default_result = MagicMock()
    default_result.scalar_one_or_none.return_value = MagicMock()
    session.execute = AsyncMock(return_value=default_result)

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


_OPTIMIZE_URL = f"/api/v1/agents/{_AGENT_ID}/prompts/v1/optimize"


class TestOptimizePrompt:
    def test_optimize_returns_suggested_prompt(self, client: TestClient) -> None:
        agent = _make_agent()
        agent.prompt_version_history = [{"version": "v1", "template": "Hello", "created_at": _NOW.isoformat()}]
        mock_optimizer = MagicMock()
        mock_optimizer.optimize = AsyncMock(
            return_value=MagicMock(
                suggested_prompt="Improved: {{query}}",
                rationale="More detail needed",
                analysis="Brevity failures",
            )
        )

        mock_backend = AsyncMock()
        mock_backend.invoke = AsyncMock(return_value=MagicMock(content="Optimized suggestion"))

        mock_mb = MagicMock()
        mock_mb.id = _BACKEND_ID

        mock_mb_result = MagicMock()
        mock_mb_result.scalar_one_or_none.return_value = mock_mb

        eval_results = [
            {
                "id": str(uuid.uuid4()),
                "eval_id": str(_EVAL_ID),
                "run_id": str(uuid.uuid4()),
                "passed": False,
                "score": 0.0,
                "detail": "Too brief",
            }
        ]
        eval_defs = {
            str(_EVAL_ID): {
                "id": str(_EVAL_ID),
                "name": "Brevity Check",
                "eval_type": "regex",
                "config_json": {},
            }
        }

        with (
            patch("modulo.api.routes.agents.get_agent", return_value=agent),
            patch(
                "modulo.api.routes.agents.get_eval_results_with_defs",
                return_value=(eval_results, eval_defs),
            ),
            patch("modulo.api.routes.agents.set_rls_org"),
            patch("modulo.api.routes.agents.create_secrets_backend") as mock_sb_factory,
            patch("modulo.core.model_backend_hub._build_backend", return_value=mock_backend),
            patch("modulo.api.routes.agents.PromptOptimizer", return_value=mock_optimizer),
        ):
            mock_sb = AsyncMock()
            mock_sb.get_secret = AsyncMock(return_value=json.dumps({"api_key": "sk-test"}))
            mock_sb_factory.return_value = mock_sb

            resp = client.post(
                _OPTIMIZE_URL,
                json={"eval_result_ids": [str(uuid.uuid4())]},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["suggested_prompt"] == "Improved: {{query}}"
        assert data["rationale"] == "More detail needed"
        assert data["analysis"] == "Brevity failures"
        assert data["version"] == "v2"

    def test_optimize_returns_404_when_agent_not_found(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.agents.get_agent", return_value=None),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.post(
                _OPTIMIZE_URL,
                json={"eval_result_ids": [str(uuid.uuid4())]},
            )
        assert resp.status_code == 404

    def test_optimize_returns_404_when_no_eval_results(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.agents.get_agent", return_value=_make_agent()),
            patch("modulo.api.routes.agents.get_eval_results_with_defs", return_value=([], {})),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.post(
                _OPTIMIZE_URL,
                json={"eval_result_ids": [str(uuid.uuid4())]},
            )
        assert resp.status_code == 404
        assert "No eval results found" in resp.json()["detail"]

    def test_optimize_returns_422_when_empty_ids(self, client: TestClient) -> None:
        resp = client.post(
            _OPTIMIZE_URL,
            json={"eval_result_ids": []},
        )
        assert resp.status_code == 422


_APPLY_URL = f"/api/v1/agents/{_AGENT_ID}/prompts/v2/apply"


class TestApplyOptimizedPrompt:
    def test_apply_returns_updated_agent(self, client: TestClient) -> None:
        agent = _make_agent()
        agent.prompt_template = "Improved prompt"

        with (
            patch("modulo.api.routes.agents.add_prompt_version", return_value=agent),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.post(
                _APPLY_URL,
                json={
                    "suggested_prompt": "Improved prompt",
                    "rationale": "Better results",
                    "optimize_version": "v1",
                    "eval_result_ids": [str(uuid.uuid4())],
                },
            )
        assert resp.status_code == 200
        assert resp.json()["prompt_template"] == "Improved prompt"

    def test_apply_returns_404_when_agent_not_found(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.agents.add_prompt_version", return_value=None),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.post(
                _APPLY_URL,
                json={"suggested_prompt": "New prompt"},
            )
        assert resp.status_code == 404

    def test_apply_returns_422_when_suggested_prompt_empty(self, client: TestClient) -> None:
        resp = client.post(
            _APPLY_URL,
            json={"suggested_prompt": ""},
        )
        assert resp.status_code == 422
