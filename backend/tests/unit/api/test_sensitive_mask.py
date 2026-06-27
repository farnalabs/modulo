"""Unit tests for sensitive data masking and reveal endpoint."""

import json
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.api.middleware.sensitive_mask import (
    SENSITIVE_VALUE_MASK,
    SensitiveValue,
    is_sensitive_key,
    mask_config_json,
    mask_sensitive_value,
)
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_VALID_32 = "a" * 32


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


_session_holder: list[AsyncMock] = []


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()
    _session_holder.clear()
    _session_holder.append(mock_session)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin", organisation_id=_ORG_ID, user_id=_USER_ID, org_role="admin"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Unit: mask_sensitive_value
# ---------------------------------------------------------------------------


class TestMaskSensitiveValue:
    def test_masks_non_empty_string(self) -> None:
        assert mask_sensitive_value("secret123") == SENSITIVE_VALUE_MASK

    def test_returns_empty_string_for_empty(self) -> None:
        assert mask_sensitive_value("") == ""

    def test_constant_is_six_bullets(self) -> None:
        assert SENSITIVE_VALUE_MASK == "••••••"


# ---------------------------------------------------------------------------
# Unit: is_sensitive_key
# ---------------------------------------------------------------------------


class TestIsSensitiveKey:
    @pytest.mark.parametrize(
        "key",
        [
            "token",
            "api_key",
            "secret",
            "password",
            "key",
            "credential",
            "API_KEY",
            "ApiKey",
            "api-key",
            "client_secret",
            "webhook_secret",
            "access_token",
            "api_key_openai",
            "auth_token",
        ],
    )
    def test_returns_true_for_sensitive_keys(self, key: str) -> None:
        assert is_sensitive_key(key) is True

    @pytest.mark.parametrize(
        "key",
        [
            "name",
            "description",
            "url",
            "host",
            "port",
            "timeout",
            "model",
            "provider",
            "endpoint",
            "visibility",
        ],
    )
    def test_returns_false_for_non_sensitive_keys(self, key: str) -> None:
        assert is_sensitive_key(key) is False


# ---------------------------------------------------------------------------
# Unit: mask_config_json
# ---------------------------------------------------------------------------


class TestMaskConfigJson:
    def test_masks_sensitive_values(self) -> None:
        config = {
            "api_key": "sk-123456",
            "token": "abc-def",
            "name": "My Connector",
            "url": "https://example.com",
            "client_secret": "s3cr3t!",
        }
        result = mask_config_json(config)
        assert result["api_key"] == SENSITIVE_VALUE_MASK
        assert result["token"] == SENSITIVE_VALUE_MASK
        assert result["client_secret"] == SENSITIVE_VALUE_MASK
        assert result["name"] == "My Connector"
        assert result["url"] == "https://example.com"

    def test_preserves_nested_non_string_types(self) -> None:
        config = {"timeout": 30, "enabled": True, "tags": ["a", "b"]}
        result = mask_config_json(config)
        assert result == config

    def test_empty_dict(self) -> None:
        assert mask_config_json({}) == {}


# ---------------------------------------------------------------------------
# Unit: SensitiveValue Pydantic type
# ---------------------------------------------------------------------------


class TestSensitiveValue:
    def test_serializes_to_mask(self) -> None:
        from pydantic import BaseModel

        class TestModel(BaseModel):
            secret: SensitiveValue | None = None

        obj = TestModel(secret="my-real-secret")
        dumped = obj.model_dump()
        assert dumped["secret"] == SENSITIVE_VALUE_MASK

    def test_handles_none(self) -> None:
        from pydantic import BaseModel

        class TestModel(BaseModel):
            secret: SensitiveValue | None = None

        obj = TestModel(secret=None)
        dumped = obj.model_dump()
        assert dumped["secret"] is None

    def test_handles_empty_string(self) -> None:
        from pydantic import BaseModel

        class TestModel(BaseModel):
            secret: SensitiveValue | None = None

        obj = TestModel(secret="")
        dumped = obj.model_dump()
        assert dumped["secret"] == ""


# ---------------------------------------------------------------------------
# Reveal endpoint
# ---------------------------------------------------------------------------


class TestRevealEndpoint:
    def test_reveal_requires_auth(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(
            "/api/v1/admin/sensitive/reveal",
            json={"resource_type": "sso_provider", "resource_id": str(uuid.uuid4())},
        )
        assert resp.status_code in (401, 403)

    def test_reveal_requires_admin_role(self, client: TestClient) -> None:
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="runner", organisation_id=_ORG_ID, user_id=_USER_ID, org_role="runner"
        )
        resp = client.post(
            "/api/v1/admin/sensitive/reveal",
            json={"resource_type": "sso_provider", "resource_id": str(uuid.uuid4())},
        )
        assert resp.status_code == 403
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="admin", organisation_id=_ORG_ID, user_id=_USER_ID, org_role="admin"
        )

    def _setup_session_execute(self, return_value: MagicMock | None) -> None:
        session = _session_holder[0]
        execute_result = AsyncMock()
        execute_result.scalar_one_or_none = MagicMock(return_value=return_value)
        session.execute = AsyncMock(return_value=execute_result)

    def test_reveal_sso_client_secret(self, client: TestClient) -> None:
        provider_id = uuid.uuid4()
        mock_provider = MagicMock()
        mock_provider.id = provider_id
        mock_provider.organisation_id = _ORG_ID
        mock_provider.client_secret = "sso-secret-value"
        self._setup_session_execute(mock_provider)

        with (
            patch("modulo.api.middleware.sensitive_mask.Redis.from_url") as mock_redis_factory,
        ):
            mock_redis = AsyncMock()
            mock_redis.setex = AsyncMock()
            mock_redis.aclose = AsyncMock()
            mock_redis_factory.return_value = mock_redis

            resp = client.post(
                "/api/v1/admin/sensitive/reveal",
                json={"resource_type": "sso_provider", "resource_id": str(provider_id)},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["value"] == "sso-secret-value"
        assert len(body["token"]) == 36  # UUID
        assert body["expires_in_seconds"] == 30

    def test_reveal_connector_config(self, client: TestClient) -> None:
        connector_id = uuid.uuid4()
        mock_connector = MagicMock()
        mock_connector.id = connector_id
        mock_connector.organisation_id = _ORG_ID
        mock_connector.config_json = {"api_key": "real-key", "name": "test"}
        self._setup_session_execute(mock_connector)

        with patch("modulo.api.middleware.sensitive_mask.Redis.from_url") as mock_redis_factory:
            mock_redis = AsyncMock()
            mock_redis.setex = AsyncMock()
            mock_redis.aclose = AsyncMock()
            mock_redis_factory.return_value = mock_redis

            resp = client.post(
                "/api/v1/admin/sensitive/reveal",
                json={"resource_type": "connector", "resource_id": str(connector_id)},
            )

        assert resp.status_code == 200
        body = resp.json()
        parsed = json.loads(body["value"])
        assert parsed["api_key"] == "real-key"
        assert parsed["name"] == "test"

    def test_reveal_unknown_resource_type(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/admin/sensitive/reveal",
            json={"resource_type": "unknown", "resource_id": str(uuid.uuid4())},
        )
        assert resp.status_code == 400

    def test_reveal_not_found(self, client: TestClient) -> None:
        self._setup_session_execute(None)

        resp = client.post(
            "/api/v1/admin/sensitive/reveal",
            json={"resource_type": "sso_provider", "resource_id": str(uuid.uuid4())},
        )
        assert resp.status_code == 404

    def test_reveal_graceful_without_redis(self, client: TestClient) -> None:
        """Reveal endpoint should work even if Redis is unavailable."""
        provider_id = uuid.uuid4()
        mock_provider = MagicMock()
        mock_provider.id = provider_id
        mock_provider.organisation_id = _ORG_ID
        mock_provider.client_secret = "sso-secret-value"
        self._setup_session_execute(mock_provider)

        with patch(
            "modulo.api.middleware.sensitive_mask.Redis.from_url",
            side_effect=RuntimeError("Redis unavailable"),
        ):
            resp = client.post(
                "/api/v1/admin/sensitive/reveal",
                json={"resource_type": "sso_provider", "resource_id": str(provider_id)},
            )

        assert resp.status_code == 200
        assert resp.json()["value"] == "sso-secret-value"


# ---------------------------------------------------------------------------
# Integration: connector response config_json masking
# ---------------------------------------------------------------------------


def test_connector_response_masks_config_json(client: TestClient) -> None:
    """Verify that connector responses mask sensitive config_json values."""
    connector_id = uuid.uuid4()
    mock_connector = MagicMock()
    mock_connector.id = connector_id
    mock_connector.organisation_id = _ORG_ID
    mock_connector.name = "Test Connector"
    mock_connector.connector_type_id = "filesystem"
    mock_connector.credentials_ciphertext = b"encrypted"
    mock_connector.config_json = {
        "api_key": "sk-123456",
        "token": "abc-def",
        "name": "My Connector",
        "url": "https://example.com",
    }
    mock_connector.allowed_operations = []
    mock_connector.status = "active"
    mock_connector.visibility = "org"
    mock_connector.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    mock_connector.updated_at = datetime(2025, 1, 1, tzinfo=UTC)

    with (
        patch("modulo.api.routes.connectors.get_connector_instance", return_value=mock_connector),
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/connectors/{connector_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["config_json"]["api_key"] == SENSITIVE_VALUE_MASK
    assert body["config_json"]["token"] == SENSITIVE_VALUE_MASK
    assert body["config_json"]["name"] == "My Connector"
    assert body["config_json"]["url"] == "https://example.com"
