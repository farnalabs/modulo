"""Unit tests for sensitive data masking and reveal endpoint."""

import json
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
    mask_config_json,
    mask_sensitive_value,
    merge_masked_config_json,
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
    session = AsyncMock()
    configure_mock_session(session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


_session_holder: list[AsyncMock] = []


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


# ---------------------------------------------------------------------------
# Unit: mask_sensitive_value
# ---------------------------------------------------------------------------


class TestMaskSensitiveValue:
    def test_masks_non_empty_string(self) -> None:
        assert mask_sensitive_value("secret123") == SENSITIVE_VALUE_MASK

    def test_returns_empty_string_for_empty(self) -> None:
        assert not mask_sensitive_value("")

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
            "passwd",
            "db_passwd",
        ],
        ids=[
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
            "passwd",
            "db_passwd",
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
        ids=["name", "description", "url", "host", "port", "timeout", "model", "provider", "endpoint", "visibility"],
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

    def test_masks_nested_header_token(self) -> None:
        """A secret in a nested ``headers.Authorization`` header key is masked.

        ``Authorization`` is not a sensitive key-name, but its value is a Bearer
        secret, so it must be masked by VALUE (``mask_secret_values_in_text``);
        a sibling ``token`` key is masked by KEY.
        """
        config = {"headers": {"Authorization": "Bearer github_pat_abc", "token": "abc123"}}
        result = mask_config_json(config)
        assert result["headers"]["Authorization"] == f"Bearer {SENSITIVE_VALUE_MASK}"
        assert result["headers"]["token"] == SENSITIVE_VALUE_MASK

    def test_masks_base_url_and_path_embedded_token(self) -> None:
        """A token embedded in a ``base_url`` / ``path`` string is value-masked."""
        config = {
            "base_url": "https://user:pass@example.com",
            "path": "https://user:pass@example.com/api",
        }
        result = mask_config_json(config)
        assert result["base_url"] == f"https://user:{SENSITIVE_VALUE_MASK}@example.com"
        assert result["path"] == f"https://user:{SENSITIVE_VALUE_MASK}@example.com/api"

    def test_masks_nested_operations_param(self) -> None:
        """A per-resource ``operations`` param key is masked at depth."""
        config = {"operations": {"get": {"params": {"api_key": "sk-123", "offset": "0"}}}}
        result = mask_config_json(config)
        assert result["operations"]["get"]["params"]["api_key"] == SENSITIVE_VALUE_MASK
        assert result["operations"]["get"]["params"]["offset"] == "0"

    def test_masks_list_under_sensitive_key(self) -> None:
        """A list under a sensitive key has every element masked."""
        config = {"tokens": ["abc", "def"], "hosts": ["a.example.com", "b.example.com"]}
        result = mask_config_json(config)
        assert result["tokens"] == [SENSITIVE_VALUE_MASK, SENSITIVE_VALUE_MASK]
        assert result["hosts"] == ["a.example.com", "b.example.com"]

    def test_empty_dict(self) -> None:
        assert not mask_config_json({})


# ---------------------------------------------------------------------------
# Unit: merge_masked_config_json (PATCH read-modify-write round-trip)
# ---------------------------------------------------------------------------


class TestMergeMaskedConfigJson:
    def test_patch_of_masked_list_does_not_overwrite_stored_secrets(self) -> None:
        """A PATCH of a masked ``tokens`` list must NOT clobber the stored list.

        Regression for the critical finding: ``_deep_merge`` used to hit the
        dict-branch ``else: merged[k] = v`` for a list value, wholesale-replacing
        the stored list with the incoming masked list, so the real secrets were
        lost after a GET->PATCH-back round-trip.
        """
        stored = {"tokens": ["real-A", "real-B"]}
        incoming = {"tokens": [SENSITIVE_VALUE_MASK, SENSITIVE_VALUE_MASK]}
        result = merge_masked_config_json(stored, incoming)
        assert result["tokens"] == ["real-A", "real-B"]

    def test_patch_of_list_of_dicts_preserves_real_nested_secret(self) -> None:
        """A list-of-dicts (``operations``) round-trip preserves the nested secret.

        Each dict element is merged recursively, so a masked ``params.api_key``
        inside a dict element is skipped while the real value is kept.
        """
        stored = {"operations": [{"name": "get", "params": {"api_key": "real-key", "offset": "0"}}]}
        incoming = {"operations": [{"name": "get", "params": {"api_key": SENSITIVE_VALUE_MASK, "offset": "0"}}]}
        result = merge_masked_config_json(stored, incoming)
        assert result["operations"][0] == {"name": "get", "params": {"api_key": "real-key", "offset": "0"}}

    def test_patch_of_masked_list_preserves_non_secret_siblings(self) -> None:
        """Sibling keys and non-masked list elements still merge as expected."""
        stored = {"tokens": ["real-A"], "name": "connector"}
        incoming = {"tokens": [SENSITIVE_VALUE_MASK], "name": "renamed"}
        result = merge_masked_config_json(stored, incoming)
        assert result["tokens"] == ["real-A"]
        assert result["name"] == "renamed"

    def test_patch_of_real_new_list_entries_replaces_stored(self) -> None:
        """A genuinely new (non-masked) list entry still replaces the stored one."""
        stored = {"refs": [".agents"]}
        incoming = {"refs": ["backend", "frontend"]}
        result = merge_masked_config_json(stored, incoming)
        assert result["refs"] == ["backend", "frontend"]

    def test_list_shrink_narrows_stored_non_secret_list(self) -> None:
        """A PATCH that SHORTENS a non-secret list must narrow the stored value.

        Regression for the MAJOR review finding: ``_merge_list`` used to merge
        positionally, so a PATCH sending a shorter ``allowed_hosts`` (SSRF egress
        allowlist) silently preserved the stale tail elements and the allowlist
        could be widened but never narrowed. A list with no masked echo is now a
        whole-list replacement.
        """
        stored = {"allowed_hosts": ["a.example.com", "b.example.com"]}
        incoming = {"allowed_hosts": ["a.example.com"]}
        result = merge_masked_config_json(stored, incoming)
        assert result["allowed_hosts"] == ["a.example.com"]

    def test_list_clear_replaces_with_empty(self) -> None:
        """A PATCH sending an empty non-secret list must clear the stored value."""
        stored = {"allowed_hosts": ["a.example.com", "b.example.com"]}
        incoming = {"allowed_hosts": []}
        result = merge_masked_config_json(stored, incoming)
        cleared = result["allowed_hosts"]
        assert isinstance(cleared, list)
        assert not cleared

    def test_list_of_dicts_without_masked_echo_replaces_whole(self) -> None:
        """A fully-specified list-of-dicts (no masked echo) replaces wholesale.

        Demonstrates the same narrowing semantics apply to list-of-dicts whose
        elements carry no secrets: removing a dict element from the incoming
        list removes it from the stored value.
        """
        stored = {"operations": [{"name": "get"}, {"name": "post"}]}
        incoming = {"operations": [{"name": "get"}]}
        result = merge_masked_config_json(stored, incoming)
        assert result["operations"] == [{"name": "get"}]

    def test_masked_list_still_preserves_stored_secrets(self) -> None:
        """A list containing a masked echo still preserves the stored value.

        Guards the original secret-clobbering fix: once ANY element is a masked
        echo the merge stays positional, so a round-tripped mask literal never
        overwrites a real secret.
        """
        stored = {"tokens": ["real-A", "real-B"]}
        incoming = {"tokens": [SENSITIVE_VALUE_MASK, SENSITIVE_VALUE_MASK]}
        result = merge_masked_config_json(stored, incoming)
        assert result["tokens"] == ["real-A", "real-B"]


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
        assert not dumped["secret"]


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
            username="runner", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="runner"
        )
        resp = client.post(
            "/api/v1/admin/sensitive/reveal",
            json={"resource_type": "sso_provider", "resource_id": str(uuid.uuid4())},
        )
        assert resp.status_code == 403
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="admin", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
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

    @pytest.mark.parametrize("stored", [b"legacy-byte-secret", "legacy-string-secret"])
    def test_reveal_legacy_sso_secret_forms(self, client: TestClient, stored: bytes | str) -> None:
        provider_id = uuid.uuid4()
        mock_provider = MagicMock(client_secret=stored)
        self._setup_session_execute(mock_provider)

        with patch("modulo.api.middleware.sensitive_mask.Redis.from_url", side_effect=RuntimeError("offline")):
            resp = client.post(
                "/api/v1/admin/sensitive/reveal",
                json={"resource_type": "sso_provider", "resource_id": str(provider_id)},
            )

        assert resp.status_code == 200
        assert resp.json()["value"] == (stored.decode() if isinstance(stored, bytes) else stored)

    def test_reveal_decrypts_sso_secret(self, client: TestClient) -> None:
        provider_id = uuid.uuid4()
        encrypted = Fernet(_FERNET_KEY.encode()).encrypt(b"encrypted-secret")
        mock_provider = MagicMock(client_secret=encrypted)
        self._setup_session_execute(mock_provider)

        with patch("modulo.api.middleware.sensitive_mask.Redis.from_url", side_effect=RuntimeError("offline")):
            resp = client.post(
                "/api/v1/admin/sensitive/reveal",
                json={"resource_type": "sso_provider", "resource_id": str(provider_id)},
            )

        assert resp.status_code == 200
        assert resp.json()["value"] == "encrypted-secret"

    @pytest.mark.parametrize("stored", [object(), b"\xff\xfe"])
    def test_reveal_rejects_invalid_sso_secret_forms(self, client: TestClient, stored: object) -> None:
        provider_id = uuid.uuid4()
        mock_provider = MagicMock(client_secret=stored)
        self._setup_session_execute(mock_provider)

        resp = client.post(
            "/api/v1/admin/sensitive/reveal",
            json={"resource_type": "sso_provider", "resource_id": str(provider_id)},
        )

        assert resp.status_code == 500
        assert resp.json()["detail"] == "Stored SSO provider secret is invalid"

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

    def test_reveal_invalid_resource_id_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/admin/sensitive/reveal",
            json={"resource_type": "connector", "resource_id": "not-a-uuid"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "resource_id must be a valid UUID"

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
    mock_connector.tier = "community"
    mock_connector.allowed_operations = []
    mock_connector.status = "active"
    mock_connector.visibility = "org"
    mock_connector.owner_team_id = None
    mock_connector.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    mock_connector.updated_at = datetime(2025, 1, 1, tzinfo=UTC)
    # Nullable degraded markers: a bare MagicMock would auto-create these as
    # non-serialisable mocks, so mirror a healthy ORM row explicitly.
    mock_connector.degraded_at = None
    mock_connector.last_skip_error = None

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
