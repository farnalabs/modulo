"""BDD-aligned unit tests for DOM sensitive data masking and reveal (§6.17).

Maps each Gherkin scenario from dom_sensitive_data.feature to a direct
pytest function so the same behaviour is verified without the BDD parser.
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
from modulo.api.middleware.sensitive_mask import (
    SENSITIVE_VALUE_MASK,
    SensitiveValue,
    is_sensitive_key,
)
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_VALID_32 = "a" * 32
_FERNET_KEY = Fernet.generate_key().decode()


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_FERNET_KEY,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


_session_holder: list[AsyncMock] = []


def _setup_session_execute(return_value: MagicMock | None) -> None:
    session = _session_holder[0]
    execute_result = AsyncMock()
    execute_result.scalar_one_or_none = MagicMock(return_value=return_value)
    session.execute = AsyncMock(return_value=execute_result)


@pytest.fixture
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
        username="admin", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
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
def viewer_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()
    _session_holder.clear()
    _session_holder.append(mock_session)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="viewer", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="viewer"
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


# ===========================================================================
# Scenario: Credentials masked in API response
# ===========================================================================


class TestCredentialsMaskedInApiResponse:
    """Maps to 'Credentials masked in API response' scenario."""

    def _build_mock_connector(self) -> MagicMock:
        connector_id = uuid.uuid4()
        mock_connector = MagicMock()
        mock_connector.id = connector_id
        mock_connector.organisation_id = _ORG_ID
        mock_connector.name = "Test Connector"
        mock_connector.connector_type_id = "filesystem"
        mock_connector.credentials_ciphertext = b"encrypted"
        mock_connector.config_json = {
            "api_key": "sk-123456",
            "name": "My Connector",
        }
        mock_connector.allowed_operations = []
        mock_connector.status = "active"
        mock_connector.visibility = "org"
        mock_connector.owner_team_id = None
        mock_connector.tier = "community"
        mock_connector.created_at = datetime(2025, 1, 1, tzinfo=UTC)
        mock_connector.updated_at = datetime(2025, 1, 1, tzinfo=UTC)
        # Nullable degraded markers: a bare MagicMock would auto-create these as
        # non-serialisable mocks, so mirror a healthy ORM row explicitly.
        mock_connector.degraded_at = None
        mock_connector.last_skip_error = None
        return mock_connector

    def test_api_key_is_masked(self, client: TestClient) -> None:
        mock_connector = self._build_mock_connector()

        with (
            patch(
                "modulo.api.routes.connectors.get_connector_instance",
                return_value=mock_connector,
            ),
            patch("modulo.api.routes.connectors.set_rls_org"),
            patch("modulo.api.routes.connectors.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/connectors/{mock_connector.id}")

        assert resp.status_code == 200
        assert resp.json()["config_json"]["api_key"] == SENSITIVE_VALUE_MASK

    def test_non_sensitive_field_preserved(self, client: TestClient) -> None:
        mock_connector = self._build_mock_connector()

        with (
            patch(
                "modulo.api.routes.connectors.get_connector_instance",
                return_value=mock_connector,
            ),
            patch("modulo.api.routes.connectors.set_rls_org"),
            patch("modulo.api.routes.connectors.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/connectors/{mock_connector.id}")

        assert resp.status_code == 200
        assert resp.json()["config_json"]["name"] == "My Connector"


# ===========================================================================
# Scenario: Sensitive key detection
# ===========================================================================


class TestSensitiveKeyDetection:
    """Maps to 'Sensitive key detection' scenario."""

    @pytest.mark.parametrize(
        "key",
        [
            "api_key",
            "token",
            "secret",
            "password",
            "credential",
            "API_KEY",
            "client_secret",
            "webhook_secret",
            "passwd",
        ],
        ids=[
            "api_key",
            "token",
            "secret",
            "password",
            "credential",
            "API_KEY",
            "client_secret",
            "webhook_secret",
            "passwd",
        ],
    )
    def test_true_for_sensitive_patterns(self, key: str) -> None:
        assert is_sensitive_key(key) is True


class TestNonSensitiveKeyDetection:
    """Maps to 'Non-sensitive key detection' scenario."""

    @pytest.mark.parametrize(
        "key",
        [
            "description",
            "name",
            "url",
            "host",
            "port",
            "timeout",
            "model",
            "provider",
        ],
        ids=["description", "name", "url", "host", "port", "timeout", "model", "provider"],
    )
    def test_false_for_innocuous_keys(self, key: str) -> None:
        assert is_sensitive_key(key) is False


# ===========================================================================
# Scenario: Admin reveals SSO client secret
# ===========================================================================


class TestRevealSsoClientSecret:
    """Maps to 'Admin reveals SSO client secret' scenario."""

    def test_reveal_returns_plaintext(self, client: TestClient) -> None:
        provider_id = uuid.uuid4()
        mock_provider = MagicMock()
        mock_provider.id = provider_id
        mock_provider.organisation_id = _ORG_ID
        mock_provider.client_secret = "sso-secret-value"
        _setup_session_execute(mock_provider)

        with patch("modulo.api.middleware.sensitive_mask.Redis.from_url") as mock_redis_factory:
            mock_redis = AsyncMock()
            mock_redis.setex = AsyncMock()
            mock_redis.aclose = AsyncMock()
            mock_redis_factory.return_value = mock_redis

            resp = client.post(
                "/api/v1/admin/sensitive/reveal",
                json={
                    "resource_type": "sso_provider",
                    "resource_id": str(provider_id),
                },
            )

        assert resp.status_code == 200
        assert resp.json()["value"] == "sso-secret-value"

    def test_reveal_includes_token(self, client: TestClient) -> None:
        provider_id = uuid.uuid4()
        mock_provider = MagicMock()
        mock_provider.id = provider_id
        mock_provider.organisation_id = _ORG_ID
        mock_provider.client_secret = "sso-secret-value"
        _setup_session_execute(mock_provider)

        with patch("modulo.api.middleware.sensitive_mask.Redis.from_url") as mock_redis_factory:
            mock_redis = AsyncMock()
            mock_redis.setex = AsyncMock()
            mock_redis.aclose = AsyncMock()
            mock_redis_factory.return_value = mock_redis

            resp = client.post(
                "/api/v1/admin/sensitive/reveal",
                json={
                    "resource_type": "sso_provider",
                    "resource_id": str(provider_id),
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert "token" in body
        assert len(body["token"]) == 36


# ===========================================================================
# Scenario: Reveal response includes 30-second expiry
# ===========================================================================


class TestRevealExpiry:
    """Maps to 'Reveal response includes 30-second expiry' scenario."""

    def test_expires_in_seconds_is_30(self, client: TestClient) -> None:
        provider_id = uuid.uuid4()
        mock_provider = MagicMock()
        mock_provider.id = provider_id
        mock_provider.organisation_id = _ORG_ID
        mock_provider.client_secret = "test-secret"
        _setup_session_execute(mock_provider)

        with patch("modulo.api.middleware.sensitive_mask.Redis.from_url") as mock_redis_factory:
            mock_redis = AsyncMock()
            mock_redis.setex = AsyncMock()
            mock_redis.aclose = AsyncMock()
            mock_redis_factory.return_value = mock_redis

            resp = client.post(
                "/api/v1/admin/sensitive/reveal",
                json={
                    "resource_type": "sso_provider",
                    "resource_id": str(provider_id),
                },
            )

        assert resp.status_code == 200
        assert resp.json()["expires_in_seconds"] == 30


# ===========================================================================
# Scenario: Non-admin cannot reveal sensitive values
# ===========================================================================


class TestNonAdminReveal:
    """Maps to 'Non-admin cannot reveal sensitive values' scenario."""

    def test_viewer_gets_403(self, client: TestClient) -> None:
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="viewer",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="viewer",
        )

        resp = client.post(
            "/api/v1/admin/sensitive/reveal",
            json={
                "resource_type": "sso_provider",
                "resource_id": str(uuid.uuid4()),
            },
        )
        assert resp.status_code == 403

        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="admin",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="admin",
        )

    def test_viewer_fixture_gets_403(self, viewer_client: TestClient) -> None:
        resp = viewer_client.post(
            "/api/v1/admin/sensitive/reveal",
            json={
                "resource_type": "sso_provider",
                "resource_id": str(uuid.uuid4()),
            },
        )
        assert resp.status_code == 403


# ===========================================================================
# Scenario: Unknown resource type returns 400
# ===========================================================================


class TestUnknownResourceType:
    """Maps to 'Unknown resource type returns 400' scenario."""

    def test_unknown_type_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/admin/sensitive/reveal",
            json={
                "resource_type": "unknown",
                "resource_id": str(uuid.uuid4()),
            },
        )
        assert resp.status_code == 400
        body = resp.json()
        assert "detail" in body
        assert "unknown" in body["detail"].lower()


# ===========================================================================
# Supporting: SensitiveValue Pydantic serialization
# ===========================================================================


class TestSensitiveValueSerialization:
    """The SensitiveValue type ensures automatic masking in Pydantic models."""

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
        assert not dumped["secret"]
