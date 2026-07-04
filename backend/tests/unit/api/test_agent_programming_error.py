"""Tests for ProgrammingError->501 and SQLAlchemyError->503 catch paths in agent endpoints."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

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
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_list_agents_programming_error_returns_501(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.agents.list_agents", side_effect=ProgrammingError("statement", {}, "cause")),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.get("/api/v1/agents")
    assert resp.status_code == 501


def test_list_agents_sqlalchemy_error_returns_503(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.agents.list_agents", side_effect=SQLAlchemyError("connection failed")),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.get("/api/v1/agents")
    assert resp.status_code == 503


def test_create_agent_programming_error_returns_501(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.agents.create_agent", side_effect=ProgrammingError("statement", {}, "cause")),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.post("/api/v1/agents", json=_AGENT_BODY)
    assert resp.status_code == 501


def test_get_agent_programming_error_returns_501(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.agents.get_agent", side_effect=ProgrammingError("statement", {}, "cause")),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/agents/{_AGENT_ID}")
    assert resp.status_code == 501


def test_update_agent_programming_error_returns_501(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.agents.get_agent", side_effect=ProgrammingError("statement", {}, "cause")),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.patch(f"/api/v1/agents/{_AGENT_ID}", json={"name": "x"})
    assert resp.status_code == 501


def test_delete_agent_programming_error_returns_501(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.agents.delete_agent", side_effect=ProgrammingError("statement", {}, "cause")),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.delete(f"/api/v1/agents/{_AGENT_ID}")
    assert resp.status_code == 501


def test_get_agent_sqlalchemy_error_returns_503(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.agents.get_agent", side_effect=SQLAlchemyError("connection failed")),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.get(f"/api/v1/agents/{_AGENT_ID}")
    assert resp.status_code == 503


def test_create_agent_integrity_error_returns_422(client: TestClient) -> None:
    body = {**_AGENT_BODY, "model_backend_id": str(uuid.uuid4())}
    with (
        patch("modulo.api.routes.agents.create_agent", side_effect=IntegrityError("statement", {}, "cause")),
        patch("modulo.api.routes.agents.set_rls_org"),
    ):
        resp = client.post("/api/v1/agents", json=body)
    assert resp.status_code == 422
