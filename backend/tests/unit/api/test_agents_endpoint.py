"""Unit tests for /api/v1/agents endpoints."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
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


def _make_agent() -> MagicMock:
    a = MagicMock()
    a.id = _AGENT_ID
    a.organisation_id = _ORG_ID
    a.name = "Test Agent"
    a.description = "A test agent for unit tests"
    a.input_schema_id = _SCHEMA_ID
    a.input_schema_version = "1.0"
    a.output_schema_id = _SCHEMA_ID
    a.output_schema_version = "1.0"
    a.prompt_template = "Hello"
    a.model_backend_id = _BACKEND_ID
    a.connector_type_refs = []
    a.evals = []
    a.retry_policy = {}
    a.token_budget = None
    a.library_id = None
    a.account_id = uuid.uuid4()
    a.required_environment_capabilities = []
    a.created_at = _NOW
    a.updated_at = _NOW
    return a


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


_AGENT_BODY = {
    "name": "Test Agent",
    "description": "A test agent for unit tests",
    "input_schema_id": str(_SCHEMA_ID),
    "input_schema_version": "1.0",
    "output_schema_id": str(_SCHEMA_ID),
    "output_schema_version": "1.0",
    "prompt_template": "Hello",
    "model_backend_id": str(_BACKEND_ID),
}


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
        account_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_list_agents_returns_200(client: TestClient) -> None:
    page_result = MagicMock(items=[_make_agent()], total=1, page=1, page_size=20)
    with (
        patch("modulo.api.routes.agents.list_agents", return_value=page_result),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.get("/api/v1/agents")
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


def test_create_agent_returns_201(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.agents.create_agent", return_value=_make_agent()),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.post("/api/v1/agents", json=_AGENT_BODY)
    assert resp.status_code == 201
    assert resp.json()["name"] == "Test Agent"


def test_get_agent_returns_200(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.agents.get_agent", return_value=_make_agent()),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/agents/{_AGENT_ID}")
    assert resp.status_code == 200


def test_get_agent_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.agents.get_agent", return_value=None),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/agents/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_update_agent_returns_200(client: TestClient) -> None:
    agent = _make_agent()
    agent.name = "Updated"
    with (
        patch("modulo.api.routes.agents.get_agent", return_value=agent),
        patch("modulo.api.routes.agents.update_agent", return_value=agent),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.patch(f"/api/v1/agents/{_AGENT_ID}", json={"name": "Updated"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated"


def test_update_agent_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.agents.get_agent", return_value=None),
        patch("modulo.api.routes.agents.update_agent", return_value=None),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.patch(f"/api/v1/agents/{uuid.uuid4()}", json={"name": "x"})
    assert resp.status_code == 404


def test_delete_agent_returns_204(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.agents.delete_agent", return_value=True),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.delete(f"/api/v1/agents/{_AGENT_ID}")
    assert resp.status_code == 204


def test_delete_agent_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.agents.delete_agent", return_value=False),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.delete(f"/api/v1/agents/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_list_agents_unauthenticated_returns_4xx(unauth_client: TestClient) -> None:
    resp = unauth_client.get("/api/v1/agents")
    assert resp.status_code in (401, 403)


def test_create_agent_with_max_input_length(client: TestClient) -> None:
    body = {**_AGENT_BODY, "max_input_length": 5000}
    agent = _make_agent()
    agent.max_input_length = 5000
    with (
        patch("modulo.api.routes.agents.create_agent", return_value=agent),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.post("/api/v1/agents", json=body)
    assert resp.status_code == 201
    assert resp.json()["max_input_length"] == 5000


def test_create_agent_without_max_input_length_defaults_to_null(client: TestClient) -> None:
    agent = _make_agent()
    agent.max_input_length = None
    with (
        patch("modulo.api.routes.agents.create_agent", return_value=agent),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.post("/api/v1/agents", json=_AGENT_BODY)
    assert resp.status_code == 201
    assert resp.json()["max_input_length"] is None


def test_update_agent_max_input_length(client: TestClient) -> None:
    agent = _make_agent()
    agent.max_input_length = 10000
    with (
        patch("modulo.api.routes.agents.get_agent", return_value=agent),
        patch("modulo.api.routes.agents.update_agent", return_value=agent),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.patch(f"/api/v1/agents/{_AGENT_ID}", json={"max_input_length": 10000})
    assert resp.status_code == 200
    assert resp.json()["max_input_length"] == 10000


# ── Generic agent criteria validation tests ──────────────────────────────


def test_create_generic_agent_missing_description_returns_422(client: TestClient) -> None:
    body = {**_AGENT_BODY, "description": None}
    resp = client.post("/api/v1/agents", json=body)
    assert resp.status_code == 422
    assert "description" in resp.json()["detail"].lower()


def test_create_generic_agent_with_library_id_skips_description_check(client: TestClient) -> None:
    body = {**_AGENT_BODY, "description": None, "library_id": str(uuid.uuid4())}
    agent = _make_agent()
    agent.library_id = uuid.UUID(body["library_id"])
    with (
        patch("modulo.api.routes.agents.create_agent", return_value=agent),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.post("/api/v1/agents", json=body)
    assert resp.status_code == 201


def test_create_non_executable_agent_missing_description_returns_422(client: TestClient) -> None:
    body = {**_AGENT_BODY, "description": None, "is_executable": False}
    resp = client.post("/api/v1/agents", json=body)
    assert resp.status_code == 422
    assert "description" in resp.json()["detail"].lower()


def test_create_generic_agent_with_description_succeeds(client: TestClient) -> None:
    body = {**_AGENT_BODY, "description": "Valid generic agent"}
    agent = _make_agent()
    agent.description = "Valid generic agent"
    with (
        patch("modulo.api.routes.agents.create_agent", return_value=agent),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.post("/api/v1/agents", json=body)
    assert resp.status_code == 201
    assert resp.json()["description"] == "Valid generic agent"


def test_update_generic_agent_clearing_description_returns_422(client: TestClient) -> None:
    agent = _make_agent()
    agent.description = "Current description"
    with (
        patch("modulo.api.routes.agents.get_agent", return_value=agent),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.patch(f"/api/v1/agents/{_AGENT_ID}", json={"description": ""})
    assert resp.status_code == 422
    assert "description" in resp.json()["detail"].lower()


def test_update_library_agent_clearing_description_succeeds(client: TestClient) -> None:
    agent = _make_agent()
    agent.library_id = uuid.uuid4()
    agent.description = "library-sourced"
    with (
        patch("modulo.api.routes.agents.get_agent", return_value=agent),
        patch("modulo.api.routes.agents.update_agent", return_value=agent),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.patch(f"/api/v1/agents/{_AGENT_ID}", json={"description": ""})
    assert resp.status_code == 200


def test_update_agent_making_non_executable_without_description_returns_422(client: TestClient) -> None:
    agent = _make_agent()
    agent.description = ""
    with (
        patch("modulo.api.routes.agents.get_agent", return_value=agent),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.patch(
            f"/api/v1/agents/{_AGENT_ID}",
            json={"is_executable": False},
        )
    assert resp.status_code == 422
    assert "description" in resp.json()["detail"].lower()
