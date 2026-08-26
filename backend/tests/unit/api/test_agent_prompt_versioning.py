"""Unit tests for agent prompt versioning, diff, rollback endpoints."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_AGENT_ID = uuid.uuid4()
_SCHEMA_ID = uuid.uuid4()
_BACKEND_ID = uuid.uuid4()
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


@pytest.fixture(autouse=True)
def _stub_get_agent() -> Generator[None, None, None]:
    """The IDOR ownership check in the prompt-version routes reads the agent via
    ``get_agent`` before the write CRUD. Supply a same-org agent so the ownership
    check passes for the legitimate (same-org) principal these tests use."""
    with patch("modulo.api.routes.agents.get_agent", return_value=_make_agent()):
        yield


def _make_agent(history: list | None = None) -> MagicMock:
    a = MagicMock()
    a.id = _AGENT_ID
    a.organisation_id = _ORG_ID
    a.name = "Prompt Version Agent"
    a.description = None
    a.input_schema_id = _SCHEMA_ID
    a.input_schema_version = "1.0"
    a.output_schema_id = _SCHEMA_ID
    a.output_schema_version = "1.0"
    a.prompt_template = "current prompt v3"
    a.model_backend_id = _BACKEND_ID
    a.connector_type_refs = []
    a.evals = []
    a.retry_policy = {}
    a.token_budget = None
    a.library_id = None
    a.required_environment_capabilities = []
    a.template_id = None
    a.agent_command = None
    a.created_by = _USER_ID
    a.created_at = _NOW
    a.updated_at = _NOW
    a.prompt_version_history = (
        history
        if history is not None
        else [
            {
                "version": "v1",
                "template": "original prompt v1",
                "created_at": _NOW.isoformat(),
                "notes": "Initial version",
                "optimized_from": None,
                "eval_result_ids": [],
            },
            {
                "version": "v2",
                "template": "improved prompt v2",
                "created_at": _NOW.isoformat(),
                "notes": "Optimized for clarity",
                "optimized_from": "v1",
                "eval_result_ids": ["eval-1"],
            },
        ]
    )
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


@pytest.fixture
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
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
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


BASE = f"/api/v1/agents/{_AGENT_ID}"


class TestListPromptVersions:
    def test_list_versions_returns_sorted(self, client: TestClient) -> None:
        agent = _make_agent()
        with (
            patch("modulo.api.routes.agents.get_agent", return_value=agent),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.get(f"{BASE}/prompts")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["version"] == "v2"
        assert data[1]["version"] == "v1"

    def test_list_versions_empty(self, client: TestClient) -> None:
        agent = _make_agent(history=[])
        with (
            patch("modulo.api.routes.agents.get_agent", return_value=agent),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.get(f"{BASE}/prompts")
        assert resp.status_code == 200
        assert not resp.json()

    def test_list_versions_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.agents.get_agent", return_value=None),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.get(f"{BASE}/prompts")
        assert resp.status_code == 404


class TestGetPromptVersion:
    def test_get_version_found(self, client: TestClient) -> None:
        agent = _make_agent()
        with (
            patch("modulo.api.routes.agents.get_prompt_version", return_value=agent.prompt_version_history[0]),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.get(f"{BASE}/prompts/v1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == "v1"
        assert data["template"] == "original prompt v1"

    def test_get_version_not_found(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.agents.get_prompt_version", return_value=None),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.get(f"{BASE}/prompts/v99")
        assert resp.status_code == 404

    def test_get_current_version_returns_active_prompt(self, client: TestClient) -> None:
        agent = _make_agent()
        current_entry = {
            "version": "current",
            "template": agent.prompt_template,
            "created_at": _NOW.isoformat(),
            "notes": "Current active prompt",
            "optimized_from": None,
            "eval_result_ids": [],
        }
        with (
            patch("modulo.api.routes.agents.get_prompt_version", return_value=current_entry),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.get(f"{BASE}/prompts/current")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == "current"
        assert data["template"] == "current prompt v3"


class TestRollback:
    def test_rollback_success(self, client: TestClient) -> None:
        agent_after = MagicMock()
        agent_after.id = _AGENT_ID
        agent_after.account_id = _USER_ID
        agent_after.organisation_id = _ORG_ID
        agent_after.name = "Prompt Version Agent"
        agent_after.description = None
        agent_after.input_schema_id = _SCHEMA_ID
        agent_after.input_schema_version = "1.0"
        agent_after.output_schema_id = _SCHEMA_ID
        agent_after.output_schema_version = "1.0"
        agent_after.prompt_template = "original prompt v1"
        agent_after.model_backend_id = _BACKEND_ID
        agent_after.connector_type_refs = []
        agent_after.evals = []
        agent_after.retry_policy = {}
        agent_after.token_budget = None
        agent_after.library_id = None
        agent_after.required_environment_capabilities = []
        agent_after.template_id = None
        agent_after.agent_command = None
        agent_after.created_by = _USER_ID
        agent_after.created_at = _NOW
        agent_after.updated_at = _NOW
        agent_after.template_id = None
        agent_after.agent_command = None
        agent_after.prompt_version_history = [
            {
                "version": "v1",
                "template": "original prompt v1",
                "created_at": _NOW.isoformat(),
                "notes": "Initial version",
                "optimized_from": None,
                "eval_result_ids": [],
            },
            {
                "version": "v2",
                "template": "improved prompt v2",
                "created_at": _NOW.isoformat(),
                "notes": "Optimized for clarity",
                "optimized_from": "v1",
                "eval_result_ids": ["eval-1"],
            },
            {
                "version": "v3",
                "template": "current prompt v3",
                "created_at": _NOW.isoformat(),
                "notes": "Rolled back from v3 to v1",
                "optimized_from": None,
                "eval_result_ids": [],
            },
        ]

        with (
            patch("modulo.api.routes.agents.rollback_prompt_version", return_value=agent_after),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.put(f"{BASE}/prompts/rollback/v1", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["message"] == "Rolled back to v1"
        assert data["agent"]["prompt_template"] == "original prompt v1"

    def test_rollback_not_found(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.agents.rollback_prompt_version", return_value=None),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.put(f"{BASE}/prompts/rollback/v99", json={})
        assert resp.status_code == 404


class TestDiffVersions:
    def test_diff_returns_structured_lines(self, client: TestClient) -> None:
        agent = _make_agent()
        with (
            patch("modulo.api.routes.agents.get_agent", return_value=agent),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.post(
                f"{BASE}/prompts/diff",
                json={"version_a": "v1", "version_b": "current"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["version_a"] == "v1"
        assert data["version_b"] == "current"
        assert data["lines"]
        types = {line["type"] for line in data["lines"]}
        assert "added" in types or "removed" in types or "unchanged" in types

    def test_diff_version_not_found(self, client: TestClient) -> None:
        agent = _make_agent()
        with (
            patch("modulo.api.routes.agents.get_agent", return_value=agent),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.post(
                f"{BASE}/prompts/diff",
                json={"version_a": "v99", "version_b": "current"},
            )
        assert resp.status_code == 404

    def test_diff_agent_not_found(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.agents.get_agent", return_value=None),
            patch("modulo.api.routes.agents.set_rls_org"),
        ):
            resp = client.post(
                f"{BASE}/prompts/diff",
                json={"version_a": "v1", "version_b": "current"},
            )
        assert resp.status_code == 404
