"""Unit tests for connector CRUD ProgrammingError→501, SQLAlchemyError→503, IntegrityError→409."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_FERNET_KEY = Fernet.generate_key().decode()
_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_CONNECTOR_ID = uuid.uuid4()


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_FERNET_KEY,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
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
        username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


_CREATE_BODY = {
    "name": "Test Connector",
    "connector_type_id": "filesystem",
    "credentials": '{"token": "secret123"}',
}
_UPDATE_BODY = {"name": "Updated"}


def _make_mock_connector() -> MagicMock:
    ci = MagicMock()
    ci.id = _CONNECTOR_ID
    ci.organisation_id = _ORG_ID
    ci.name = "Test Connector"
    ci.connector_type_id = "filesystem"
    ci.credentials_ciphertext = b"encrypted"
    ci.config_json = {}
    ci.allowed_operations = []
    ci.status = "active"
    ci.visibility = "org"
    ci.tier = "native"
    return ci


def test_create_connector_programming_error_returns_501(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.connectors.create_connector_instance", side_effect=ProgrammingError("stmt", {}, None)),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/connectors", json=_CREATE_BODY)
    assert resp.status_code == 501
    assert "migrations" in resp.json()["detail"].lower()


def test_create_connector_sqlalchemy_error_returns_503(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.connectors.create_connector_instance", side_effect=SQLAlchemyError("connection dead")),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/connectors", json=_CREATE_BODY)
    assert resp.status_code == 503
    assert "database error" in resp.json()["detail"].lower()


def test_create_connector_integrity_error_returns_409(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.connectors.create_connector_instance", side_effect=IntegrityError("stmt", {}, None)),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/connectors", json=_CREATE_BODY)
    assert resp.status_code == 409
    assert "constraint violation" in resp.json()["detail"].lower()


def test_list_connectors_programming_error_returns_501(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.connectors.list_connector_instances", side_effect=ProgrammingError("stmt", {}, None)),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/connectors")
    assert resp.status_code == 501
    assert "migrations" in resp.json()["detail"].lower()


def test_list_connectors_sqlalchemy_error_returns_503(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.connectors.list_connector_instances", side_effect=SQLAlchemyError("connection dead")),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/connectors")
    assert resp.status_code == 503
    assert "database error" in resp.json()["detail"].lower()


def test_get_connector_programming_error_returns_501(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", side_effect=ProgrammingError("stmt", {}, None)),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/connectors/{_CONNECTOR_ID}")
    assert resp.status_code == 501
    assert "migrations" in resp.json()["detail"].lower()


def test_get_connector_sqlalchemy_error_returns_503(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", side_effect=SQLAlchemyError("connection dead")),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/connectors/{_CONNECTOR_ID}")
    assert resp.status_code == 503
    assert "database error" in resp.json()["detail"].lower()


def test_update_connector_programming_error_returns_501(client: TestClient) -> None:
    mock_ci = _make_mock_connector()
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=mock_ci),
        patch("modulo.api.routes.connectors.update_connector_instance", side_effect=ProgrammingError("stmt", {}, None)),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/connectors/{_CONNECTOR_ID}", json=_UPDATE_BODY)
    assert resp.status_code == 501
    assert "migrations" in resp.json()["detail"].lower()


def test_update_connector_sqlalchemy_error_returns_503(client: TestClient) -> None:
    mock_ci = _make_mock_connector()
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=mock_ci),
        patch("modulo.api.routes.connectors.update_connector_instance", side_effect=SQLAlchemyError("connection dead")),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/connectors/{_CONNECTOR_ID}", json=_UPDATE_BODY)
    assert resp.status_code == 503
    assert "database error" in resp.json()["detail"].lower()


def test_delete_connector_programming_error_returns_501(client: TestClient) -> None:
    mock_ci = _make_mock_connector()
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=mock_ci),
        patch("modulo.api.routes.connectors.delete_connector_instance", side_effect=ProgrammingError("stmt", {}, None)),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.delete(f"/api/v1/connectors/{_CONNECTOR_ID}")
    assert resp.status_code == 501
    assert "migrations" in resp.json()["detail"].lower()


def test_delete_connector_sqlalchemy_error_returns_503(client: TestClient) -> None:
    mock_ci = _make_mock_connector()
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=mock_ci),
        patch("modulo.api.routes.connectors.delete_connector_instance", side_effect=SQLAlchemyError("connection dead")),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.delete(f"/api/v1/connectors/{_CONNECTOR_ID}")
    assert resp.status_code == 503
    assert "database error" in resp.json()["detail"].lower()
