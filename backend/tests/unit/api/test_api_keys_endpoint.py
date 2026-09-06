"""Unit tests for /api/v1/api-keys endpoints."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.api_key import _UNSET
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

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
        modulo_license_key="test-license-key",
    )


def _make_key() -> MagicMock:
    k = MagicMock()
    k.id = _KEY_ID
    k.name = "Test Key"
    k.role = "operator"
    k.lookup_prefix = "abcd1234"
    k.created_at = _NOW
    k.team_id = None
    # FAR-620: the caller scope is stamped on real rows; serialisation guard
    # falls back to 'org' for a non-str scope.
    k.scope = "org"
    return k


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
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
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    with patch("modulo.api.routes.api_keys.resolve_role_from_membership", new=AsyncMock(return_value="admin")):
        yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def runner_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="runneruser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="runner",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    with patch("modulo.api.routes.api_keys.resolve_role_from_membership", new=AsyncMock(return_value="runner")):
        yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
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
        account_id=_USER_ID,
        org_role="operator",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    with patch("modulo.api.routes.api_keys.resolve_role_from_membership", new=AsyncMock(return_value="operator")):
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
    assert body["key_value"] == "mk_test_key"
    assert body["role"] == "operator"
    assert "hashed_secret" not in body


def test_create_api_key_emits_api_key_created_audit(client: TestClient) -> None:
    """Key minting fires the PRD §8.12 ``api_key_created`` audit event."""
    key = _make_key()
    audit = AsyncMock(return_value=MagicMock())
    with (
        patch("modulo.api.routes.api_keys.create_api_key", return_value=(key, "mk_test_key")),
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
        patch("modulo.core.audit_logger.append_audit_event", new=audit),
    ):
        resp = client.post("/api/v1/api-keys", json={"name": "Test Key", "role": "operator"})
    assert resp.status_code == 201
    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["event_type"] == "api_key_created"
    assert kwargs["org_id"] == _ORG_ID
    assert kwargs["actor_user_id"] == _USER_ID
    assert kwargs["resource_type"] == "api_key"
    assert kwargs["resource_id"] == key.id
    # FAR-620 payload stamps: auth_type (REST is JWT-only), key_scope and the
    # masked prefix — shape parity with the MCP surface.
    assert kwargs["payload_json"] == {
        "name": "Test Key",
        "role": "operator",
        "team_id": None,
        "auth_type": "jwt",
        "key_scope": "org",
        "lookup_prefix": "mk_abcd1234****",
    }


def test_create_api_key_audit_failure_does_not_block_creation(client: TestClient) -> None:
    """A broken audit append must not fail a successful key creation."""
    key = _make_key()

    async def _raise_audit(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("audit boom")

    with (
        patch("modulo.api.routes.api_keys.create_api_key", return_value=(key, "mk_test_key")),
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
        patch("modulo.core.audit_logger.append_audit_event", side_effect=_raise_audit),
    ):
        resp = client.post("/api/v1/api-keys", json={"name": "Test Key", "role": "operator"})
    assert resp.status_code == 201
    assert resp.json()["key_value"] == "mk_test_key"


def test_create_api_key_returns_full_key_once(client: TestClient) -> None:
    key = _make_key()
    with (
        patch("modulo.api.routes.api_keys.create_api_key", return_value=(key, "mk_abc123")),
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/api-keys", json={"name": "k", "role": "runner"})
    assert resp.json()["key_value"] == "mk_abc123"


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
    assert resp.json()["key_value"] == "mk_key"


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
        patch("modulo.api.routes.api_keys.revoke_api_key", return_value=_make_key()),
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
    ):
        resp = client.delete(f"/api/v1/api-keys/{_KEY_ID}")
    assert resp.status_code == 200
    assert resp.json()["revoked"] is True


def test_revoke_api_key_emits_api_key_revoked_audit(client: TestClient) -> None:
    """Key revocation fires the PRD §8.12 ``api_key_revoked`` audit event."""
    audit = AsyncMock(return_value=MagicMock())
    revoked_key = _make_key()
    revoked_key.scope = "user"
    revoked_key.lookup_prefix = "zzzz9999"
    with (
        patch("modulo.api.routes.api_keys.revoke_api_key", return_value=revoked_key),
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
        patch("modulo.core.audit_logger.append_audit_event", new=audit),
    ):
        resp = client.delete(f"/api/v1/api-keys/{_KEY_ID}")
    assert resp.status_code == 200
    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["event_type"] == "api_key_revoked"
    assert kwargs["org_id"] == _ORG_ID
    assert kwargs["actor_user_id"] == _USER_ID
    assert kwargs["resource_type"] == "api_key"
    assert kwargs["resource_id"] == _KEY_ID
    # FAR-620 payload stamps (shape parity with the MCP surface): the revoked
    # row supplies the key's scope + masked prefix.
    assert kwargs["payload_json"] == {
        "revoked_by": str(_USER_ID),
        "auth_type": "jwt",
        "key_scope": "user",
        "lookup_prefix": "mk_zzzz9999****",
    }


def test_revoke_api_key_not_found_does_not_emit_audit(client: TestClient) -> None:
    """A 404 revoke (unknown key) must not fire the revoke audit event."""
    audit = AsyncMock(return_value=MagicMock())
    with (
        patch("modulo.api.routes.api_keys.revoke_api_key", return_value=None),
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
        patch("modulo.core.audit_logger.append_audit_event", new=audit),
    ):
        resp = client.delete(f"/api/v1/api-keys/{uuid.uuid4()}")
    assert resp.status_code == 404
    audit.assert_not_awaited()


def test_revoke_api_key_audit_failure_does_not_fail_revocation(client: TestClient) -> None:
    """A broken audit append must not fail a completed revocation."""

    async def _raise_audit(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("audit boom")

    with (
        patch("modulo.api.routes.api_keys.revoke_api_key", return_value=_make_key()),
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
        patch("modulo.core.audit_logger.append_audit_event", side_effect=_raise_audit),
    ):
        resp = client.delete(f"/api/v1/api-keys/{_KEY_ID}")
    assert resp.status_code == 200
    assert resp.json()["revoked"] is True


def test_revoke_api_key_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.api_keys.revoke_api_key", return_value=None),
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


def test_list_api_keys_runner_gets_403(runner_client: TestClient) -> None:
    """Runner (who holds api_key.update) must be denied listing org keys (floor raised to operator)."""
    resp = runner_client.get("/api/v1/api-keys")
    assert resp.status_code == 403
    assert "Only admin or operator users can list API keys" in resp.json()["detail"]


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
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    with (
        patch("modulo.api.routes.api_keys.create_api_key", return_value=(key, "mk_team_key")),
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
        patch("modulo.api.routes.api_keys.resolve_plan_context", return_value=mock_plan),
    ):
        resp = client.post(
            "/api/v1/api-keys",
            json={"name": "Team Key", "role": "operator", "team_id": str(_TEAM_ID)},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["team_id"] == str(_TEAM_ID)
    assert body["key_value"] == "mk_team_key"


def test_create_api_key_with_team_id_requires_admin(operator_client: TestClient) -> None:
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    with patch("modulo.api.routes.api_keys.resolve_plan_context", return_value=mock_plan):
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
        patch("modulo.core.audit_logger.set_rls_org") as mock_audit_org,
        patch("modulo.core.audit_logger.set_rls_user_context") as mock_audit_ctx,
    ):
        client.post("/api/v1/api-keys", json={"name": "k", "role": "operator"})
    # The main create tx establishes RLS via the route-module helpers...
    mock_org.assert_awaited_once()
    mock_ctx.assert_awaited_once_with(ANY, _USER_ID, "admin")
    # ...and the shared append_audit_event_isolated helper re-establishes RLS in
    # the fresh api_key_created audit transaction (SET LOCAL reverts on COMMIT),
    # so the audit-logger helpers are each awaited once with the same identity.
    mock_audit_org.assert_awaited_once()
    mock_audit_ctx.assert_awaited_once_with(ANY, _USER_ID, "admin")


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
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    with (
        patch("modulo.api.routes.api_keys.update_api_key", return_value=key),
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
        patch("modulo.api.routes.api_keys.resolve_plan_context", return_value=mock_plan),
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
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    with patch("modulo.api.routes.api_keys.resolve_plan_context", return_value=mock_plan):
        resp = operator_client.put(
            f"/api/v1/api-keys/{_KEY_ID}",
            json={"name": "k", "team_id": str(_TEAM_ID)},
        )
    assert resp.status_code == 403


def test_update_api_key_with_expires_at(client: TestClient) -> None:
    key = _make_key()
    key.name = "Expiring Key"
    with (
        patch("modulo.api.routes.api_keys.update_api_key", return_value=key),
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
    ):
        resp = client.put(
            f"/api/v1/api-keys/{_KEY_ID}",
            json={"name": "Expiring Key", "expires_at": "2026-12-31T00:00:00"},
        )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Expiring Key"


def test_create_api_key_rejects_empty_name(client: TestClient) -> None:
    resp = client.post("/api/v1/api-keys", json={"name": "", "role": "operator"})
    assert resp.status_code == 422


def test_update_api_key_rejects_empty_name(client: TestClient) -> None:
    resp = client.put(
        f"/api/v1/api-keys/{_KEY_ID}",
        json={"name": ""},
    )
    assert resp.status_code == 422


def test_create_api_key_rejects_past_expires_at(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/api-keys",
        json={"name": "k", "role": "runner", "expires_at": "2020-01-01T00:00:00"},
    )
    assert resp.status_code == 422


def test_create_api_key_rejects_blank_whitespace_name(client: TestClient) -> None:
    resp = client.post("/api/v1/api-keys", json={"name": "   ", "role": "operator"})
    assert resp.status_code == 422


def test_create_api_key_strips_whitespace_name(client: TestClient) -> None:
    key = _make_key()
    key.name = "Stripped Key"
    with (
        patch("modulo.api.routes.api_keys.create_api_key", return_value=(key, "mk_key")) as create,
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/api-keys", json={"name": "  Stripped Key  ", "role": "operator"})
    assert resp.status_code == 201
    assert create.call_args.kwargs["name"] == "Stripped Key"


def test_update_api_key_rejects_past_expires_at(client: TestClient) -> None:
    resp = client.put(
        f"/api/v1/api-keys/{_KEY_ID}",
        json={"name": "k", "expires_at": "2020-01-01T00:00:00"},
    )
    assert resp.status_code == 422


def test_update_api_key_strips_whitespace_name(client: TestClient) -> None:
    key = _make_key()
    key.name = "Trimmed"
    with (
        patch("modulo.api.routes.api_keys.update_api_key", return_value=key) as update,
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
    ):
        resp = client.put(
            f"/api/v1/api-keys/{_KEY_ID}",
            json={"name": "  Trimmed  "},
        )
    assert resp.status_code == 200
    assert update.call_args.kwargs["name"] == "Trimmed"


# ---------------------------------------------------------------------------
# team_id transitions
# ---------------------------------------------------------------------------


def test_create_api_key_with_unknown_team_returns_409(client: TestClient) -> None:
    """A team_id that references a non-existent team trips the FK and maps to 409."""
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    with (
        patch(
            "modulo.api.routes.api_keys.create_api_key",
            side_effect=IntegrityError(
                "stmt",
                {},
                Exception("insert or update on table 'org_api_keys' violates foreign key constraint"),
            ),
        ),
        patch("modulo.api.routes.api_keys.resolve_plan_context", return_value=mock_plan),
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
    ):
        resp = client.post(
            "/api/v1/api-keys",
            json={"name": "Team Key", "role": "operator", "team_id": str(uuid.uuid4())},
        )
    assert resp.status_code == 409


def test_update_api_key_clears_team_id(client: TestClient) -> None:
    """PUT with team_id=null moves a team-scoped key back to org-wide (admin)."""
    key = _make_key()
    key.name = "Widened Key"
    key.team_id = None
    with (
        patch("modulo.api.routes.api_keys.update_api_key", return_value=key) as update,
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
    ):
        resp = client.put(
            f"/api/v1/api-keys/{_KEY_ID}",
            json={"name": "Widened Key", "team_id": None},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["team_id"] is None
    # The clear is signalled explicitly — update_api_key receives team_id=None,
    # not the "not provided" sentinel.
    assert update.call_args.kwargs["team_id"] is None


def test_update_api_key_without_team_id_passes_unset_sentinel(client: TestClient) -> None:
    """PUT without a team_id key must not disturb the existing team scope."""
    key = _make_key()
    key.name = "Scoped Key"
    key.team_id = _TEAM_ID
    with (
        patch("modulo.api.routes.api_keys.update_api_key", return_value=key) as update,
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
    ):
        resp = client.put(
            f"/api/v1/api-keys/{_KEY_ID}",
            json={"name": "Scoped Key"},
        )
    assert resp.status_code == 200
    assert update.call_args.kwargs["team_id"] is _UNSET


def test_update_api_key_clear_team_requires_admin(operator_client: TestClient) -> None:
    """Clearing the team scope is an admin-only operation (same as setting it)."""
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    with patch("modulo.api.routes.api_keys.resolve_plan_context", return_value=mock_plan):
        resp = operator_client.put(
            f"/api/v1/api-keys/{_KEY_ID}",
            json={"name": "k", "team_id": None},
        )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# FAR-620: user-scoped key minting (scope param, flag gate, quota, immutability)
# ---------------------------------------------------------------------------


def _flag_registry(enabled: bool) -> MagicMock:
    registry = MagicMock()
    registry.resolve_flag = AsyncMock(return_value=enabled)
    return registry


def test_create_user_scoped_key_flag_on(client: TestClient) -> None:
    """scope='user' + flag ON ⇒ minted with scope='user' (response carries the
    scope; the quota helper ran)."""
    key = _make_key()
    key.scope = "user"
    with (
        patch("modulo.api.routes.api_keys.get_registry", return_value=_flag_registry(True)),
        patch("modulo.api.routes.api_keys.create_api_key", return_value=(key, "mk_user_key")) as mint,
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
        patch("modulo.api.routes.api_keys._enforce_user_key_quota", new=AsyncMock()) as quota,
    ):
        resp = client.post(
            "/api/v1/api-keys",
            json={"name": "user:duncan", "role": "operator", "scope": "user"},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["scope"] == "user"
    assert mint.await_args.kwargs["scope"] == "user"
    quota.assert_awaited_once()


def test_create_user_scoped_key_flag_off_rejected_422(client: TestClient) -> None:
    """Flag OFF ⇒ minting a user-scoped key is REJECTED 422 — never silently
    downgraded to an org key."""
    with (
        patch("modulo.api.routes.api_keys.get_registry", return_value=_flag_registry(False)),
        patch("modulo.api.routes.api_keys.create_api_key", return_value=(_make_key(), "mk_x")) as mint,
    ):
        resp = client.post(
            "/api/v1/api-keys",
            json={"name": "user:duncan", "role": "operator", "scope": "user"},
        )
    assert resp.status_code == 422
    assert "not enabled" in resp.json()["detail"]
    mint.assert_not_called()


def test_create_user_scoped_key_flag_read_failure_fails_closed(client: TestClient) -> None:
    """A flag resolution error is treated as OFF (fail-closed) ⇒ 422."""
    registry = MagicMock()
    registry.resolve_flag = AsyncMock(side_effect=RuntimeError("db down"))
    with (
        patch("modulo.api.routes.api_keys.get_registry", return_value=registry),
        patch("modulo.api.routes.api_keys.create_api_key", return_value=(_make_key(), "mk_x")) as mint,
    ):
        resp = client.post(
            "/api/v1/api-keys",
            json={"name": "user:duncan", "role": "operator", "scope": "user"},
        )
    assert resp.status_code == 422
    mint.assert_not_called()


def test_create_api_key_rejects_unknown_scope(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/api-keys",
        json={"name": "k", "role": "operator", "scope": "team"},
    )
    assert resp.status_code == 422


def test_create_org_key_default_scope_no_quota(client: TestClient) -> None:
    """scope omitted (or 'org') keeps today's behaviour exactly: org key
    minted, NO quota check, response carries scope 'org'."""
    key = _make_key()
    key.scope = "org"
    with (
        patch("modulo.api.routes.api_keys.get_registry", return_value=_flag_registry(False)) as registry,
        patch("modulo.api.routes.api_keys.create_api_key", return_value=(key, "mk_org_key")) as mint,
        patch("modulo.api.routes.api_keys.set_rls_org"),
        patch("modulo.api.routes.api_keys.set_rls_user_context"),
        patch("modulo.api.routes.api_keys._enforce_user_key_quota", new=AsyncMock()) as quota,
    ):
        resp = client.post("/api/v1/api-keys", json={"name": "Org Key", "role": "operator"})
    assert resp.status_code == 201
    assert resp.json()["scope"] == "org"
    assert mint.await_args.kwargs["scope"] == "org"
    quota.assert_not_awaited()
    # Org-key minting never consults the user-keys flag (byte-identical).
    registry.return_value.resolve_flag.assert_not_awaited()


def test_update_api_key_rejects_scope_payload_422(client: TestClient) -> None:
    """The caller scope is IMMUTABLE: a PUT payload carrying ``scope`` is
    rejected 422, never applied or silently ignored."""
    with patch("modulo.api.routes.api_keys.update_api_key", return_value=_make_key()) as update:
        resp = client.put(
            f"/api/v1/api-keys/{_KEY_ID}",
            json={"name": "k", "scope": "user"},
        )
    assert resp.status_code == 422
    assert "immutable" in resp.json()["detail"]
    update.assert_not_called()


def test_update_api_key_without_scope_still_works(client: TestClient) -> None:
    """A PUT payload that does NOT mention scope is unaffected."""
    with patch("modulo.api.routes.api_keys.update_api_key", return_value=_make_key()) as update:
        resp = client.put(f"/api/v1/api-keys/{_KEY_ID}", json={"name": "Renamed"})
    assert resp.status_code == 200
    update.assert_awaited_once()


class TestUserKeyQuota:
    """Per-(account, org) quota of 10 ACTIVE user-scoped keys, TOCTOU-safe
    via the FOR UPDATE account-row lock (the me.py pattern)."""

    @staticmethod
    def _session(count: int, *, account: Any = ...) -> tuple[AsyncMock, list[Any]]:
        session = AsyncMock()
        executed: list[Any] = []
        # Ellipsis default = "a real account row"; explicit None = missing row.
        resolved_account = MagicMock() if account is ... else account
        session.get = AsyncMock(return_value=resolved_account)
        account_result = MagicMock()
        account_result.scalar_one.return_value = count

        async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> MagicMock:
            executed.append(stmt)
            return account_result

        session.execute = _execute
        return session, executed

    @staticmethod
    def _principal() -> Any:
        from modulo.auth.jwt import TenantPrincipal

        return TenantPrincipal(
            username="u",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="operator",
        )

    @pytest.mark.asyncio
    async def test_under_quota_passes(self) -> None:
        """9 active keys < the 10 quota ⇒ the quota gate raises nothing."""
        from fastapi import HTTPException

        from modulo.api.routes.api_keys import _enforce_user_key_quota

        session, _executed = self._session(9)
        try:
            await _enforce_user_key_quota(session, self._principal())
        except HTTPException as exc:  # pragma: no cover — the assertion path
            raise AssertionError(f"quota gate must not fire under quota: {exc.status_code}") from exc

    @pytest.mark.asyncio
    async def test_at_quota_rejected_429(self) -> None:
        from fastapi import HTTPException

        from modulo.api.routes.api_keys import _enforce_user_key_quota

        session, _executed = self._session(10)
        with pytest.raises(HTTPException) as excinfo:
            await _enforce_user_key_quota(session, self._principal())
        # Distinct error shape from the role mint-cap (403).
        assert excinfo.value.status_code == 429

    @pytest.mark.asyncio
    async def test_account_row_locked_for_update(self) -> None:
        """TOCTOU: the account row is locked FOR UPDATE so concurrent mints
        serialise and the second re-counts after the first commits."""
        from modulo.api.routes.api_keys import _enforce_user_key_quota

        session, _executed = self._session(0)
        await _enforce_user_key_quota(session, self._principal())
        session.get.assert_awaited_once()
        assert session.get.await_args.kwargs.get("with_for_update") is True
        assert session.get.await_args.args[1] == _USER_ID

    @pytest.mark.asyncio
    async def test_count_filters_active_user_keys_per_account_org(self) -> None:
        """Revoked keys do not count; the count is scoped per-(account, org)."""
        from modulo.api.routes.api_keys import _enforce_user_key_quota

        session, executed = self._session(0)
        await _enforce_user_key_quota(session, self._principal())
        assert len(executed) == 1
        stmt = str(executed[0])
        assert "revoked_at" in stmt
        assert "account_id" in stmt
        assert "organisation_id" in stmt
        assert "scope" in stmt

    @pytest.mark.asyncio
    async def test_missing_account_denies_403(self) -> None:
        from fastapi import HTTPException

        from modulo.api.routes.api_keys import _enforce_user_key_quota

        session, _executed = self._session(0, account=None)
        with pytest.raises(HTTPException) as excinfo:
            await _enforce_user_key_quota(session, self._principal())
        assert excinfo.value.status_code == 403


class TestUserKeyQuotaConcurrentMint:
    """TOCTOU regression: two concurrent mints by the same account must NOT
    both pass the quota check. The FOR UPDATE lock serialises them — session
    B's re-read (after blocking on A's lock) sees A's committed mint and is
    rejected, where an unlocked check would have double-spent the quota."""

    @staticmethod
    def _quota_session(count: int) -> AsyncMock:
        session = AsyncMock()
        session.get = AsyncMock(return_value=MagicMock())
        result = MagicMock()
        result.scalar_one.return_value = count

        async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> MagicMock:
            return result

        session.execute = _execute
        return session

    @pytest.mark.asyncio
    async def test_second_concurrent_mint_sees_post_commit_count(self) -> None:
        from fastapi import HTTPException

        from modulo.api.routes.api_keys import _enforce_user_key_quota
        from modulo.auth.jwt import TenantPrincipal

        principal = TenantPrincipal(
            username="u",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="operator",
        )
        # Session A locks the account and counts 9 (under quota → mint). The
        # mint commits. Session B's FOR UPDATE get() blocks until A commits,
        # then re-counts — 10 active keys → 429.
        session_a = self._quota_session(9)
        await _enforce_user_key_quota(session_a, principal)
        session_b = self._quota_session(10)
        with pytest.raises(HTTPException) as excinfo:
            await _enforce_user_key_quota(session_b, principal)
        assert excinfo.value.status_code == 429
