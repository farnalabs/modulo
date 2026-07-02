"""Tests that agent endpoints return 501 on ProgrammingError."""

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
_AGENT_ID = uuid.uuid4()
_SCHEMA_ID = uuid.uuid4()
_BACKEND_ID = uuid.uuid4()

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


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_session_that_raises_on_begin() -> AsyncMock:
    session = AsyncMock()
    session.begin = MagicMock(
        side_effect=ProgrammingError("mock", "mock", "mock")
    )
    return session


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_session_that_raises_on_begin()

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
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_list_agents_returns_501_on_programming_error(client: TestClient) -> None:
    resp = client.get("/api/v1/agents")
    assert resp.status_code == 501
    assert "not available" in resp.json()["detail"].lower()


def test_create_agent_returns_501_on_programming_error(client: TestClient) -> None:
    resp = client.post("/api/v1/agents", json=_AGENT_BODY)
    assert resp.status_code == 501
    assert "not available" in resp.json()["detail"].lower()


def test_get_agent_returns_501_on_programming_error(client: TestClient) -> None:
    resp = client.get(f"/api/v1/agents/{_AGENT_ID}")
    assert resp.status_code == 501
    assert "not available" in resp.json()["detail"].lower()


def test_update_agent_returns_501_on_programming_error(client: TestClient) -> None:
    resp = client.patch(f"/api/v1/agents/{_AGENT_ID}", json={"name": "Updated"})
    assert resp.status_code == 501
    assert "not available" in resp.json()["detail"].lower()


def test_delete_agent_returns_501_on_programming_error(client: TestClient) -> None:
    resp = client.delete(f"/api/v1/agents/{_AGENT_ID}")
    assert resp.status_code == 501
    assert "not available" in resp.json()["detail"].lower()
