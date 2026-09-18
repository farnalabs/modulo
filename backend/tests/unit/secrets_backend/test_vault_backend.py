"""Unit tests for VaultSecretsBackend.

The hvac client is fully mocked via the ``mock_hvac`` fixture, so these tests
run even when the optional *hvac* package is not installed.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from modulo.core.secrets_backend.vault import VaultSecretsBackend

pytestmark = [
    pytest.mark.usefixtures("vault_env"),
]


@pytest.fixture
def mock_hvac() -> MagicMock:
    with (
        patch("modulo.core.secrets_backend.vault._MODULE_AVAILABLE", True),
        patch("modulo.core.secrets_backend.vault._hvac") as mh,
    ):
        mh.exceptions.InvalidPath = type("InvalidPath", (Exception,), {})
        mh.exceptions.Forbidden = type("Forbidden", (Exception,), {})
        mh.exceptions.VaultError = type("VaultError", (Exception,), {})
        yield mh


def _make_backend() -> VaultSecretsBackend:
    backend = VaultSecretsBackend()
    backend._client = MagicMock()
    return backend


class TestVaultSecretsBackend:
    async def test_empty_key_raises_value_error(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        with pytest.raises(ValueError, match="non-empty"):
            await backend.get_secret("")

    async def test_get_secret_reads_from_vault(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        backend._client.secrets.kv.v2.read_secret_version.return_value = {
            "data": {"data": {"value": "my-value"}},
        }

        value = await backend.get_secret("my-key")

        assert value == "my-value"
        backend._client.secrets.kv.v2.read_secret_version.assert_called_once_with(
            path="modulo/secrets/my-key",
            mount_point="secret",
        )

    async def test_get_secret_unknown_key_raises_key_error(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        backend._client.secrets.kv.v2.read_secret_version.side_effect = mock_hvac.exceptions.InvalidPath()

        with pytest.raises(KeyError):
            await backend.get_secret("unknown-key")

    async def test_get_secret_missing_value_raises_key_error(self, mock_hvac: MagicMock) -> None:
        """A response with no ``value`` under data.data is treated as missing."""
        backend = _make_backend()
        backend._client.secrets.kv.v2.read_secret_version.return_value = {"data": {"data": {}}}

        with pytest.raises(KeyError):
            await backend.get_secret("empty-key")

    async def test_get_secret_non_string_value_coerced(self, mock_hvac: MagicMock) -> None:
        """A non-string stored value is coerced with str() rather than rejected."""
        backend = _make_backend()
        backend._client.secrets.kv.v2.read_secret_version.return_value = {"data": {"data": {"value": 123}}}

        value = await backend.get_secret("numeric-key")

        assert value == "123"

    async def test_set_secret_empty_key_raises_value_error(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        with pytest.raises(ValueError, match="non-empty"):
            await backend.set_secret("", "my-value")

    async def test_delete_secret_empty_key_raises_value_error(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        with pytest.raises(ValueError, match="non-empty"):
            await backend.delete_secret("")

    async def test_set_secret_writes_to_vault(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()

        await backend.set_secret("my-key", "my-value")

        backend._client.secrets.kv.v2.create_or_update_secret.assert_called_once_with(
            path="modulo/secrets/my-key",
            secret={"value": "my-value"},
            mount_point="secret",
        )

    async def test_set_secret_rate_limited_wraps_as_runtime_error(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        rate_limit_error = mock_hvac.exceptions.VaultError("rate limited")
        rate_limit_error.status_code = 503
        backend._client.secrets.kv.v2.create_or_update_secret.side_effect = rate_limit_error

        with pytest.raises(RuntimeError, match="rate-limited"):
            await backend.set_secret("my-key", "my-value")

    async def test_delete_secret_removes_from_vault(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()

        await backend.delete_secret("my-key")

        backend._client.secrets.kv.v2.delete_metadata_and_all_versions.assert_called_once_with(
            path="modulo/secrets/my-key",
            mount_point="secret",
        )

    async def test_delete_secret_rate_limited_wraps_as_runtime_error(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        rate_limit_error = mock_hvac.exceptions.VaultError("rate limited")
        rate_limit_error.status_code = 429
        backend._client.secrets.kv.v2.delete_metadata_and_all_versions.side_effect = rate_limit_error

        with pytest.raises(RuntimeError, match="rate-limited"):
            await backend.delete_secret("my-key")

    async def test_delete_secret_noop_when_missing(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        backend._client.secrets.kv.v2.delete_metadata_and_all_versions.side_effect = mock_hvac.exceptions.InvalidPath()

        await backend.delete_secret("missing-key")
        backend._client.secrets.kv.v2.delete_metadata_and_all_versions.assert_called_once()

    async def test_get_secret_timeout_wraps_as_runtime_error(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()

        with (
            patch.object(asyncio, "wait_for", side_effect=TimeoutError()),
            pytest.raises(RuntimeError, match="timeout reading secret"),
        ):
            await backend.get_secret("my-key")

    async def test_get_secret_network_error_wraps_as_runtime_error(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        backend._client.secrets.kv.v2.read_secret_version.side_effect = ConnectionError("connection refused")

        with pytest.raises(RuntimeError, match="unexpected error reading secret"):
            await backend.get_secret("my-key")

    async def test_get_secret_rate_limited_wraps_as_runtime_error(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        rate_limit_error = mock_hvac.exceptions.VaultError("rate limited")
        rate_limit_error.status_code = 429
        backend._client.secrets.kv.v2.read_secret_version.side_effect = rate_limit_error

        with pytest.raises(RuntimeError, match="rate-limited"):
            await backend.get_secret("my-key")

    async def test_get_secret_vault_error_wraps_as_runtime_error(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        backend._client.secrets.kv.v2.read_secret_version.side_effect = mock_hvac.exceptions.VaultError("bad request")

        with pytest.raises(RuntimeError, match="unexpected error reading secret"):
            await backend.get_secret("my-key")

    async def test_set_secret_timeout_wraps_as_runtime_error(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()

        with (
            patch.object(asyncio, "wait_for", side_effect=TimeoutError()),
            pytest.raises(RuntimeError, match="timeout writing secret"),
        ):
            await backend.set_secret("my-key", "my-value")

    async def test_set_secret_network_error_wraps_as_runtime_error(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        backend._client.secrets.kv.v2.create_or_update_secret.side_effect = ConnectionError("connection refused")

        with pytest.raises(RuntimeError, match="unexpected error writing secret"):
            await backend.set_secret("my-key", "my-value")

    async def test_set_secret_vault_error_wraps_as_runtime_error(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        backend._client.secrets.kv.v2.create_or_update_secret.side_effect = mock_hvac.exceptions.VaultError(
            "bad request"
        )

        with pytest.raises(RuntimeError, match="unexpected error writing secret"):
            await backend.set_secret("my-key", "my-value")

    async def test_set_secret_cancelled_error_propagates(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        backend._client.secrets.kv.v2.create_or_update_secret.side_effect = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await backend.set_secret("my-key", "my-value")

    async def test_delete_secret_timeout_wraps_as_runtime_error(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()

        with (
            patch.object(asyncio, "wait_for", side_effect=TimeoutError()),
            pytest.raises(RuntimeError, match="timeout deleting secret"),
        ):
            await backend.delete_secret("my-key")

    async def test_delete_secret_network_error_wraps_as_runtime_error(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        delete_fn = backend._client.secrets.kv.v2.delete_metadata_and_all_versions
        delete_fn.side_effect = ConnectionError("connection refused")

        with pytest.raises(RuntimeError, match="unexpected error deleting secret"):
            await backend.delete_secret("my-key")

    async def test_delete_secret_vault_error_wraps_as_runtime_error(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        delete_fn = backend._client.secrets.kv.v2.delete_metadata_and_all_versions
        delete_fn.side_effect = mock_hvac.exceptions.VaultError("bad request")

        with pytest.raises(RuntimeError, match="unexpected error deleting secret"):
            await backend.delete_secret("my-key")

    async def test_delete_secret_cancelled_error_propagates(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        delete_fn = backend._client.secrets.kv.v2.delete_metadata_and_all_versions
        delete_fn.side_effect = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await backend.delete_secret("my-key")

    async def test_get_secret_cancelled_error_propagates(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        backend._client.secrets.kv.v2.read_secret_version.side_effect = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await backend.get_secret("my-key")

    async def test_secret_path_rejects_dot_dot(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        with pytest.raises(ValueError, match="invalid secret key"):
            await backend.get_secret("../../etc/passwd")

    async def test_secret_path_rejects_leading_slash(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        with pytest.raises(ValueError, match="invalid secret key"):
            await backend.get_secret("/absolute/path")

    async def test_get_secret_forbidden_raises_permission_error(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        backend._client.secrets.kv.v2.read_secret_version.side_effect = mock_hvac.exceptions.Forbidden()

        with pytest.raises(PermissionError, match="permission denied"):
            await backend.get_secret("restricted-key")

    async def test_get_secret_key_path_contains_dot_dot_raises(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        with pytest.raises(ValueError, match="invalid secret key"):
            await backend.get_secret("key/../subkey")

    async def test_get_secret_key_contains_only_dot_dot_raises(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        with pytest.raises(ValueError, match="invalid secret key"):
            await backend.get_secret("..")

    async def test_set_secret_rejects_dot_dot_path(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        with pytest.raises(ValueError, match="invalid secret key"):
            await backend.set_secret("../traversal", "value")

    async def test_set_secret_rejects_leading_slash(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        with pytest.raises(ValueError, match="invalid secret key"):
            await backend.set_secret("/absolute/path", "value")

    async def test_delete_secret_rejects_dot_dot_path(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        with pytest.raises(ValueError, match="invalid secret key"):
            await backend.delete_secret("../traversal")

    async def test_delete_secret_rejects_leading_slash(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        with pytest.raises(ValueError, match="invalid secret key"):
            await backend.delete_secret("/absolute/path")

    def test_missing_addr_raises_value_error(self, monkeypatch, mock_hvac: MagicMock) -> None:
        monkeypatch.delenv("VAULT_ADDR", raising=False)
        monkeypatch.delenv("VAULT_TOKEN", raising=False)
        with pytest.raises(ValueError, match="VAULT_ADDR is not set"):
            VaultSecretsBackend()

    def test_secret_path_normalizes_trailing_slash(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend()
        backend._path_prefix = "modulo/secrets/"
        assert backend._secret_path("my-key") == "modulo/secrets/my-key"

    async def test_custom_mount_point_and_path_prefix_from_env(self, monkeypatch, mock_hvac: MagicMock) -> None:
        """VAULT_MOUNT_POINT / VAULT_PATH_PREFIX env vars drive every call.

        Also verifies the constructor trims whitespace and the path prefix
        normalizes a trailing slash.
        """
        monkeypatch.setenv("VAULT_MOUNT_POINT", "  custom-mount  ")
        monkeypatch.setenv("VAULT_PATH_PREFIX", "custom/prefix/")
        backend = _make_backend()
        backend._client.secrets.kv.v2.read_secret_version.return_value = {
            "data": {"data": {"value": "my-value"}},
        }

        value = await backend.get_secret("my-key")

        assert value == "my-value"
        backend._client.secrets.kv.v2.read_secret_version.assert_called_once_with(
            path="custom/prefix/my-key",
            mount_point="custom-mount",
        )

        await backend.set_secret("my-key", "my-value")
        backend._client.secrets.kv.v2.create_or_update_secret.assert_called_once_with(
            path="custom/prefix/my-key",
            secret={"value": "my-value"},
            mount_point="custom-mount",
        )

    async def test_delete_secret_forbidden_wraps_as_runtime_error(self, mock_hvac: MagicMock) -> None:
        """A permission-denied delete is a real failure, not a no-op like InvalidPath."""
        backend = _make_backend()
        delete_fn = backend._client.secrets.kv.v2.delete_metadata_and_all_versions
        delete_fn.side_effect = mock_hvac.exceptions.Forbidden()

        with pytest.raises(RuntimeError, match="unexpected error deleting secret"):
            await backend.delete_secret("my-key")

    async def test_ensure_client_auth_failure_raises_runtime_error(self, mock_hvac: MagicMock) -> None:
        backend = VaultSecretsBackend()
        backend._token = None
        backend._role_id = "role-id"
        backend._secret_id = "secret-id"
        backend._client = None

        def login(**kwargs):
            raise mock_hvac.exceptions.VaultError("invalid credentials")

        mock_hvac.Client.return_value.auth.approle.login = login

        with pytest.raises(RuntimeError, match="failed to authenticate to Vault"):
            await backend._ensure_client()

    async def test_ensure_client_auth_cancelled_propagates(self, monkeypatch, mock_hvac: MagicMock) -> None:
        monkeypatch.delenv("VAULT_TOKEN", raising=False)
        monkeypatch.setenv("VAULT_ROLE_ID", "role-id")
        monkeypatch.setenv("VAULT_SECRET_ID", "secret-id")
        backend = VaultSecretsBackend()

        def login(**kwargs):
            raise asyncio.CancelledError

        mock_hvac.Client.return_value.auth.approle.login = login

        with pytest.raises(asyncio.CancelledError):
            await backend._ensure_client()

    def test_constructor_raises_without_hvac(self) -> None:
        with (
            patch("modulo.core.secrets_backend.vault._MODULE_AVAILABLE", False),
            pytest.raises(RuntimeError, match="hvac"),
        ):
            VaultSecretsBackend()

    async def test_ensure_client_raises_without_hvac(self, mock_hvac: MagicMock) -> None:
        backend = VaultSecretsBackend()
        with (
            patch("modulo.core.secrets_backend.vault._MODULE_AVAILABLE", False),
            pytest.raises(RuntimeError, match="hvac"),
        ):
            await backend._ensure_client()

    async def test_ensure_client_uses_token(self, mock_hvac: MagicMock) -> None:
        backend = VaultSecretsBackend()
        mock_client = mock_hvac.Client.return_value

        client = await backend._ensure_client()

        assert client is mock_client
        mock_hvac.Client.assert_called_once_with(url="http://localhost:8200")
        assert mock_client.token == "test-token"
        mock_client.auth.approle.login.assert_not_called()

    async def test_ensure_client_uses_approle_login(self, monkeypatch, mock_hvac: MagicMock) -> None:
        monkeypatch.delenv("VAULT_TOKEN", raising=False)
        monkeypatch.setenv("VAULT_ROLE_ID", "role-id")
        monkeypatch.setenv("VAULT_SECRET_ID", "secret-id")
        backend = VaultSecretsBackend()
        mock_client = mock_hvac.Client.return_value

        client = await backend._ensure_client()

        assert client is mock_client
        mock_client.auth.approle.login.assert_called_once_with(
            role_id="role-id",
            secret_id="secret-id",
        )

    async def test_ensure_client_caches_client(self, mock_hvac: MagicMock) -> None:
        backend = VaultSecretsBackend()

        first = await backend._ensure_client()
        second = await backend._ensure_client()

        assert first is second
        mock_hvac.Client.assert_called_once_with(url="http://localhost:8200")

    async def test_ensure_client_missing_credentials_raises(self, monkeypatch, mock_hvac: MagicMock) -> None:
        monkeypatch.delenv("VAULT_TOKEN", raising=False)
        monkeypatch.delenv("VAULT_ROLE_ID", raising=False)
        monkeypatch.delenv("VAULT_SECRET_ID", raising=False)
        backend = VaultSecretsBackend()

        with pytest.raises(RuntimeError, match="failed to authenticate to Vault"):
            await backend._ensure_client()
