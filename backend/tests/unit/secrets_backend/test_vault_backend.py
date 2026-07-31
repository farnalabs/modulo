"""Unit tests for VaultSecretsBackend."""

from unittest.mock import MagicMock, patch

import pytest

from modulo.core.secrets_backend.vault import VaultSecretsBackend

try:
    import hvac  # noqa: F401

    _HVAC_AVAILABLE = True
except ImportError:
    _HVAC_AVAILABLE = False

pytestmark = [
    pytest.mark.skipif(not _HVAC_AVAILABLE, reason="hvac package not installed"),
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


def _make_backend(mock_hvac: MagicMock) -> VaultSecretsBackend:
    backend = VaultSecretsBackend()
    backend._client = MagicMock()
    return backend


class TestVaultSecretsBackend:
    async def test_empty_key_raises_value_error(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend(mock_hvac)
        with pytest.raises(ValueError, match="non-empty"):
            await backend.get_secret("")

    async def test_get_secret_reads_from_vault(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend(mock_hvac)
        backend._client.secrets.kv.v2.read_secret_version.return_value = {
            "data": {"data": {"value": "my-value"}},
        }

        value = await backend.get_secret("my-key")

        assert value == "my-value"
        backend._client.secrets.kv.v2.read_secret_version.assert_called_once()

    async def test_get_secret_unknown_key_raises_key_error(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend(mock_hvac)
        backend._client.secrets.kv.v2.read_secret_version.side_effect = mock_hvac.exceptions.InvalidPath()

        with pytest.raises(KeyError):
            await backend.get_secret("unknown-key")

    async def test_set_secret_writes_to_vault(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend(mock_hvac)

        await backend.set_secret("my-key", "my-value")

        backend._client.secrets.kv.v2.create_or_update_secret.assert_called_once()

    async def test_delete_secret_removes_from_vault(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend(mock_hvac)

        await backend.delete_secret("my-key")

        backend._client.secrets.kv.v2.delete_metadata_and_all_versions.assert_called_once()

    async def test_delete_secret_noop_when_missing(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend(mock_hvac)
        backend._client.secrets.kv.v2.delete_metadata_and_all_versions.side_effect = mock_hvac.exceptions.InvalidPath()

        await backend.delete_secret("missing-key")
        backend._client.secrets.kv.v2.delete_metadata_and_all_versions.assert_called_once()

    async def test_get_secret_timeout_wraps_as_runtime_error(self, mock_hvac: MagicMock) -> None:
        import asyncio

        backend = _make_backend(mock_hvac)

        with (
            patch.object(asyncio, "wait_for", side_effect=TimeoutError()),
            pytest.raises(RuntimeError, match="timeout reading secret"),
        ):
            await backend.get_secret("my-key")

    async def test_get_secret_network_error_wraps_as_runtime_error(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend(mock_hvac)
        backend._client.secrets.kv.v2.read_secret_version.side_effect = ConnectionError("connection refused")

        with pytest.raises(RuntimeError, match="unexpected error reading secret"):
            await backend.get_secret("my-key")

    async def test_get_secret_rate_limited_wraps_as_runtime_error(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend(mock_hvac)
        rate_limit_error = mock_hvac.exceptions.VaultError("rate limited")
        rate_limit_error.status_code = 429
        backend._client.secrets.kv.v2.read_secret_version.side_effect = rate_limit_error

        with pytest.raises(RuntimeError, match="rate-limited"):
            await backend.get_secret("my-key")

    async def test_get_secret_vault_error_wraps_as_runtime_error(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend(mock_hvac)
        backend._client.secrets.kv.v2.read_secret_version.side_effect = mock_hvac.exceptions.VaultError("bad request")

        with pytest.raises(RuntimeError, match="unexpected error reading secret"):
            await backend.get_secret("my-key")

    async def test_set_secret_timeout_wraps_as_runtime_error(self, mock_hvac: MagicMock) -> None:
        import asyncio

        backend = _make_backend(mock_hvac)

        with (
            patch.object(asyncio, "wait_for", side_effect=TimeoutError()),
            pytest.raises(RuntimeError, match="timeout writing secret"),
        ):
            await backend.set_secret("my-key", "my-value")

    async def test_set_secret_network_error_wraps_as_runtime_error(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend(mock_hvac)
        backend._client.secrets.kv.v2.create_or_update_secret.side_effect = ConnectionError("connection refused")

        with pytest.raises(RuntimeError, match="unexpected error writing secret"):
            await backend.set_secret("my-key", "my-value")

    async def test_delete_secret_timeout_wraps_as_runtime_error(self, mock_hvac: MagicMock) -> None:
        import asyncio

        backend = _make_backend(mock_hvac)

        with (
            patch.object(asyncio, "wait_for", side_effect=TimeoutError()),
            pytest.raises(RuntimeError, match="timeout deleting secret"),
        ):
            await backend.delete_secret("my-key")

    async def test_delete_secret_network_error_wraps_as_runtime_error(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend(mock_hvac)
        delete_fn = backend._client.secrets.kv.v2.delete_metadata_and_all_versions
        delete_fn.side_effect = ConnectionError("connection refused")

        with pytest.raises(RuntimeError, match="unexpected error deleting secret"):
            await backend.delete_secret("my-key")

    async def test_secret_path_rejects_dot_dot(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend(mock_hvac)
        with pytest.raises(ValueError, match="invalid secret key"):
            await backend.get_secret("../../etc/passwd")

    async def test_secret_path_rejects_leading_slash(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend(mock_hvac)
        with pytest.raises(ValueError, match="invalid secret key"):
            await backend.get_secret("/absolute/path")

    async def test_get_secret_forbidden_raises_permission_error(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend(mock_hvac)
        backend._client.secrets.kv.v2.read_secret_version.side_effect = mock_hvac.exceptions.Forbidden()

        with pytest.raises(PermissionError, match="permission denied"):
            await backend.get_secret("restricted-key")

    async def test_get_secret_key_path_contains_dot_dot_raises(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend(mock_hvac)
        with pytest.raises(ValueError, match="invalid secret key"):
            await backend.get_secret("key/../subkey")

    async def test_get_secret_key_contains_only_dot_dot_raises(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend(mock_hvac)
        with pytest.raises(ValueError, match="invalid secret key"):
            await backend.get_secret("..")

    async def test_set_secret_rejects_dot_dot_path(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend(mock_hvac)
        with pytest.raises(ValueError, match="invalid secret key"):
            await backend.set_secret("../traversal", "value")

    async def test_delete_secret_rejects_dot_dot_path(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend(mock_hvac)
        with pytest.raises(ValueError, match="invalid secret key"):
            await backend.delete_secret("../traversal")

    async def test_missing_addr_raises_value_error(self, monkeypatch) -> None:
        monkeypatch.delenv("VAULT_ADDR", raising=False)
        monkeypatch.delenv("VAULT_TOKEN", raising=False)
        with pytest.raises(ValueError, match="VAULT_ADDR is not set"):
            VaultSecretsBackend()

    async def test_secret_path_normalizes_trailing_slash(self, mock_hvac: MagicMock) -> None:
        backend = _make_backend(mock_hvac)
        backend._path_prefix = "modulo/secrets/"
        assert backend._secret_path("my-key") == "modulo/secrets/my-key"

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
