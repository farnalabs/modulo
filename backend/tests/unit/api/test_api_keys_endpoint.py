"""Unit tests for /api/v1/api-keys endpoints."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import ANY, AsyncMock, MagicMock, patch

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
_KEY_ID = uuid.uuid4()
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_public_url="http://localhost:8000",
    )


def _make_key() -> MagicMock:
    k = MagicMock()
    k.id = _KEY_ID
    k.name = "Test Key"
    k.role = "operator"
    k.lookup_prefix = "abcd1234"
    k.created_at = _NOW
    k.team_id = None
    return k


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
        username="testuser",
        organisation_id=_ORG_ID,
        user_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def operator_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="operatoruser",
        organisation_id=_ORG_ID,
        user_id=_USER_ID,
        org_role="operator",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /api/v1/api-keys
# ---------------------------------------------------------------------------


def test_create_api_key_returns_201(client: TestClient) -> None:
    key = _make_key()
    with (
        patch("modulo.api.routes.api_keys.create_api_key", return_value=(key, "mk_test_key")),
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/api-keys", json={"name": "Test Key", "role": "operator"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["full_key"] == "mk_test_key"
    assert body["role"] == "operator"
    assert "hashed_secret" not in body


def test_create_api_key_returns_full_key_once(client: TestClient) -> None:
    key = _make_key()
    with (
        patch("modulo.api.routes.api_keys.create_api_key", return_value=(key, "mk_abc123")),
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/api-keys", json={"name": "k", "role": "runner"})
    assert resp.json()["full_key"] == "mk_abc123"


def test_create_api_key_rejects_admin_role(client: TestClient) -> None:
    resp = client.post("/api/v1/api-keys", json={"name": "k", "role": "admin"})
    assert resp.status_code == 422


def test_create_api_key_with_expires_at(client: TestClient) -> None:
    key = _make_key()
    with (
        patch("modulo.api.routes.api_keys.create_api_key", return_value=(key, "mk_key")),
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
    ):
        resp = client.post(
            "/api/v1/api-keys",
            json={"name": "k", "role": "runner", "expires_at": "2026-12-31T00:00:00"},
        )
    assert resp.status_code == 201
    assert resp.json()["full_key"] == "mk_key"


# ---------------------------------------------------------------------------
# GET /api/v1/api-keys
# ---------------------------------------------------------------------------


def test_list_api_keys_returns_200(client: TestClient) -> None:
    entries = [{"id": str(_KEY_ID), "name": "Test", "role": "operator"}]
    with (
        patch("modulo.api.routes.api_keys.list_api_keys", return_value=entries),
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
    ):
        resp = client.get("/api/v1/api-keys")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


# ---------------------------------------------------------------------------
# DELETE /api/v1/api-keys/{id}
# ---------------------------------------------------------------------------


def test_revoke_api_key_returns_200(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.api_keys.revoke_api_key", return_value=True),
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
    ):
        resp = client.delete(f"/api/v1/api-keys/{_KEY_ID}")
    assert resp.status_code == 200
    assert resp.json()["revoked"] is True


def test_revoke_api_key_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.api_keys.revoke_api_key", return_value=False),
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
    ):
        resp = client.delete(f"/api/v1/api-keys/{uuid.uuid4()}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/v1/api-keys/mcp-config
# ---------------------------------------------------------------------------


def test_mcp_config_returns_url_and_snippet(client: TestClient) -> None:
    resp = client.get("/api/v1/api-keys/mcp-config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mcp_url"] == "http://localhost:8000/mcp"
    assert "mcpServers" in body["config_snippet"]
    assert "modulo" in body["config_snippet"]["mcpServers"]


def test_list_api_keys_unauthenticated_returns_4xx(unauth_client: TestClient) -> None:
    resp = unauth_client.get("/api/v1/api-keys")
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# PUT /api/v1/api-keys/{id}
# ---------------------------------------------------------------------------


def test_update_api_key_returns_200(client: TestClient) -> None:
    key = _make_key()
    key.name = "Updated Key"
    key.role = "runner"
    with (
        patch("modulo.api.routes.api_keys.update_api_key", return_value=key),
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
    ):
        resp = client.put(
            f"/api/v1/api-keys/{_KEY_ID}",
            json={"name": "Updated Key", "role": "runner"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Updated Key"
    assert body["role"] == "runner"


def test_update_api_key_partial_name(client: TestClient) -> None:
    key = _make_key()
    key.name = "Only Name Updated"
    with (
        patch("modulo.api.routes.api_keys.update_api_key", return_value=key),
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
    ):
        resp = client.put(
            f"/api/v1/api-keys/{_KEY_ID}",
            json={"name": "Only Name Updated"},
        )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Only Name Updated"


def test_update_api_key_rejects_invalid_role(client: TestClient) -> None:
    resp = client.put(
        f"/api/v1/api-keys/{_KEY_ID}",
        json={"name": "k", "role": "admin"},
    )
    assert resp.status_code == 422


def test_update_api_key_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.api_keys.update_api_key", return_value=None),
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
    ):
        resp = client.put(
            f"/api/v1/api-keys/{uuid.uuid4()}",
            json={"name": "k"},
        )
    assert resp.status_code == 404


def test_create_api_key_with_team_id_returns_team_id(client: TestClient) -> None:
    key = _make_key()
    key.team_id = _TEAM_ID
    with (
        patch("modulo.api.routes.api_keys.create_api_key", return_value=(key, "mk_team_key")),
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
    ):
        resp = client.post(
            "/api/v1/api-keys",
            json={"name": "Team Key", "role": "operator", "team_id": str(_TEAM_ID)},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["team_id"] == str(_TEAM_ID)
    assert body["full_key"] == "mk_team_key"


def test_create_api_key_with_team_id_requires_admin(operator_client: TestClient) -> None:
    resp = operator_client.post(
        "/api/v1/api-keys",
        json={"name": "Team Key", "role": "operator", "team_id": str(_TEAM_ID)},
    )
    assert resp.status_code == 403


def test_create_api_key_calls_set_rls_user_context(client: TestClient) -> None:
    key = _make_key()
    with (
        patch("modulo.api.routes.api_keys.create_api_key", return_value=(key, "mk_key")),
        patch("modulo.api.routes.api_keys.set_rls_org") as mock_org,
        patch("modulo.api.routes.api_keys.set_rls_user_context") as mock_ctx,
    ):
        client.post("/api/v1/api-keys", json={"name": "k", "role": "operator"})
    mock_org.assert_awaited_once()
    mock_ctx.assert_awaited_once_with(ANY, _USER_ID, "admin")


def test_list_api_keys_calls_set_rls_user_context(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.api_keys.list_api_keys", return_value=[]),
        patch("modulo.api.routes.api_keys.set_rls_org") as mock_org,
        patch("modulo.api.routes.api_keys.set_rls_user_context") as mock_ctx,
    ):
        client.get("/api/v1/api-keys")
    mock_org.assert_awaited_once()
    mock_ctx.assert_awaited_once_with(ANY, _USER_ID, "admin")


def test_update_api_key_with_team_id_returns_team_id(client: TestClient) -> None:
    key = _make_key()
    key.name = "Team Key Updated"
    key.team_id = _TEAM_ID
    with (
        patch("modulo.api.routes.api_keys.update_api_key", return_value=key),
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
    ):
        resp = client.put(
            f"/api/v1/api-keys/{_KEY_ID}",
            json={"name": "Team Key Updated", "role": "operator", "team_id": str(_TEAM_ID)},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["team_id"] == str(_TEAM_ID)
    assert body["name"] == "Team Key Updated"


def test_update_api_key_with_team_id_requires_admin(operator_client: TestClient) -> None:
    resp = operator_client.put(
        f"/api/v1/api-keys/{_KEY_ID}",
        json={"name": "k", "team_id": str(_TEAM_ID)},
    )
    assert resp.status_code == 403
