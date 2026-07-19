"""Unit tests for /api/v1/connectors endpoints.

Credentials (raw credential strings) must NEVER appear in responses.
Only `has_credentials: true/false` is exposed.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_FERNET_KEY = Fernet.generate_key().decode()
_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_CONNECTOR_ID = uuid.uuid4()
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

_CRUD_PATCH_PREFIX = "modulo.api.routes.connectors."


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_FERNET_KEY,
        modulo_admin_password="testpass",
    )


def _make_connector(credentials_ciphertext: bytes = b"encrypted") -> MagicMock:
    ci = MagicMock()
    ci.id = _CONNECTOR_ID
    ci.organisation_id = _ORG_ID
    ci.name = "Test Connector"
    ci.connector_type_id = "filesystem"
    ci.credentials_ciphertext = credentials_ciphertext
    ci.config_json = {}
    ci.allowed_operations = []
    ci.status = "active"
    ci.visibility = "org"
    ci.owner_team_id = None
    ci.tier = "native"
    ci.created_at = _NOW
    ci.updated_at = _NOW
    return ci


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


_CREATE_BODY = {
    "name": "Test Connector",
    "connector_type_id": "filesystem",
    "credentials": '{"token": "secret123"}',
}


def _crud_cases() -> list[dict[str, object]]:
    page_result = MagicMock(items=[_make_connector()], total=1, page=1, page_size=20, next_cursor=None)
    connector = _make_connector()
    updated = _make_connector()
    updated.name = "Updated"
    return [
        {
            "id": "list",
            "method": "GET",
            "url": "/api/v1/connectors",
            "body": None,
            "patches": [("list_connector_instances", page_result)],
            "expected_status": 200,
            "check": lambda resp: resp.json()["total"] == 1,
        },
        {
            "id": "get",
            "method": "GET",
            "url": f"/api/v1/connectors/{_CONNECTOR_ID}",
            "body": None,
            "patches": [("get_connector_instance", connector)],
            "expected_status": 200,
            "check": lambda resp: "credentials_ciphertext" not in resp.json() and resp.json()["has_credentials"],
        },
        {
            "id": "get_not_found",
            "method": "GET",
            "url": f"/api/v1/connectors/{uuid.uuid4()}",
            "body": None,
            "patches": [("get_connector_instance", None)],
            "expected_status": 404,
        },
        {
            "id": "update",
            "method": "PATCH",
            "url": f"/api/v1/connectors/{_CONNECTOR_ID}",
            "body": {"name": "Updated"},
            "patches": [("get_connector_instance", connector), ("update_connector_instance", updated)],
            "expected_status": 200,
            "check": lambda resp: resp.json()["name"] == "Updated",
        },
        {
            "id": "update_not_found",
            "method": "PATCH",
            "url": f"/api/v1/connectors/{uuid.uuid4()}",
            "body": {"name": "x"},
            "patches": [("get_connector_instance", None), ("update_connector_instance", None)],
            "expected_status": 404,
        },
        {
            "id": "delete",
            "method": "DELETE",
            "url": f"/api/v1/connectors/{_CONNECTOR_ID}",
            "body": None,
            "patches": [("delete_connector_instance", True)],
            "expected_status": 204,
        },
        {
            "id": "delete_not_found",
            "method": "DELETE",
            "url": f"/api/v1/connectors/{uuid.uuid4()}",
            "body": None,
            "patches": [("delete_connector_instance", False)],
            "expected_status": 404,
        },
    ]


@pytest.mark.parametrize("case", _crud_cases(), ids=lambda c: c["id"])
def test_crud(client: TestClient, case: dict[str, object]) -> None:
    method = case["method"]
    url = case["url"]
    body = case.get("body")
    expected_status = case["expected_status"]
    check = case.get("check")

    patchers = []
    for func_name, ret in case["patches"]:
        patchers.append(patch(f"{_CRUD_PATCH_PREFIX}{func_name}", return_value=ret))
    patchers.append(patch(f"{_CRUD_PATCH_PREFIX}set_rls_org"))
    patchers.append(patch(f"{_CRUD_PATCH_PREFIX}set_rls_user_context"))

    for p in patchers:
        p.start()

    try:
        if method == "GET":
            resp = client.get(url)
        elif method == "POST":
            resp = client.post(url, json=body or {})
        elif method == "PATCH":
            resp = client.patch(url, json=body or {})
        elif method == "DELETE":
            resp = client.delete(url)
        elif method == "PUT":
            resp = client.put(url, json=body or {})
        else:
            raise ValueError(f"Unsupported method: {method}")

        assert resp.status_code == expected_status, f"Expected {expected_status}, got {resp.status_code}: {resp.text}"
        if check:
            assert check(resp)
    finally:
        for p in patchers:
            p.stop()


def test_create_connector_does_not_expose_credentials(client: TestClient) -> None:
    connector = _make_connector(credentials_ciphertext=b"encrypted_bytes")
    with (
        patch("modulo.api.routes.connectors.create_connector_instance", return_value=connector),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/connectors", json=_CREATE_BODY)

    assert resp.status_code == 201
    body = resp.json()
    assert "credentials_ciphertext" not in body
    assert "credentials" not in body
    assert body["has_credentials"] is True


def test_create_connector_encrypts_credentials(client: TestClient) -> None:
    captured: list[bytes] = []

    async def fake_create(session: object, **kwargs: object) -> MagicMock:
        captured.append(kwargs["credentials_ciphertext"])  # type: ignore[arg-type]
        return _make_connector(credentials_ciphertext=kwargs["credentials_ciphertext"])  # type: ignore[arg-type]

    with (
        patch("modulo.api.routes.connectors.create_connector_instance", new=fake_create),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        client.post("/api/v1/connectors", json=_CREATE_BODY)

    assert captured, "create_connector_instance was not called"
    ciphertext = captured[0]
    decrypted = Fernet(_FERNET_KEY.encode()).decrypt(ciphertext).decode()
    assert decrypted == '{"token": "secret123"}'
    assert b"secret123" not in ciphertext


def test_connector_no_credentials_shows_false(client: TestClient) -> None:
    connector = _make_connector(credentials_ciphertext=b"")
    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=connector),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/connectors/{_CONNECTOR_ID}")
    assert resp.json()["has_credentials"] is False


def test_list_connectors_unauthenticated_returns_4xx(unauth_client: TestClient) -> None:
    resp = unauth_client.get("/api/v1/connectors")
    assert resp.status_code in (401, 403)
