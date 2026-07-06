"""Unit tests for VaultSecretsBackend."""

import os
from unittest.mock import MagicMock, patch

import pytest

from modulo.core.secrets_backend.vault import VaultSecretsBackend

try:
    import hvac  # noqa: F401

    _HVAC_AVAILABLE = True
except ImportError:
    _HVAC_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _HVAC_AVAILABLE,
    reason="hvac package not installed",
)


@pytest.fixture(autouse=True)
def _env():
    with patch.dict(
        os.environ,
        {
            "VAULT_ADDR": "http://localhost:8200",
            "VAULT_TOKEN": "test-token",
        },
    ):
        yield


@pytest.fixture
def mock_hvac():
    with patch("modulo.core.secrets_backend.vault._MODULE_AVAILABLE", True):
        with patch("modulo.core.secrets_backend.vault._hvac") as mh:
            mh.exceptions.InvalidPath = type("InvalidPath", (Exception,), {})
            mh.exceptions.Forbidden = type("Forbidden", (Exception,), {})
            yield mh


def _make_backend(mock_hvac):
    backend = VaultSecretsBackend()
    backend._client = MagicMock()
    return backend


class TestVaultSecretsBackend:
    async def test_empty_key_raises_value_error(self, mock_hvac):
        backend = _make_backend(mock_hvac)
        with pytest.raises(ValueError, match="non-empty"):
            await backend.get_secret("")

    async def test_get_secret_reads_from_vault(self, mock_hvac):
        backend = _make_backend(mock_hvac)
        backend._client.secrets.kv.v2.read_secret_version.return_value = {
            "data": {"data": {"value": "my-value"}},
        }

        value = await backend.get_secret("my-key")

        assert value == "my-value"
        backend._client.secrets.kv.v2.read_secret_version.assert_called_once()

    async def test_get_secret_unknown_key_raises(self, mock_hvac):
        backend = _make_backend(mock_hvac)
        backend._client.secrets.kv.v2.read_secret_version.side_effect = mock_hvac.exceptions.InvalidPath()

        with pytest.raises(KeyError):
            await backend.get_secret("unknown-key")

    async def test_set_secret_writes_to_vault(self, mock_hvac):
        backend = _make_backend(mock_hvac)

        await backend.set_secret("my-key", "my-value")

        backend._client.secrets.kv.v2.create_or_update_secret.assert_called_once()

    async def test_delete_secret_removes_from_vault(self, mock_hvac):
        backend = _make_backend(mock_hvac)

        await backend.delete_secret("my-key")

        backend._client.secrets.kv.v2.delete_metadata_and_all_versions.assert_called_once()

    async def test_delete_secret_noop_when_missing(self, mock_hvac):
        backend = _make_backend(mock_hvac)
        backend._client.secrets.kv.v2.delete_metadata_and_all_versions.side_effect = mock_hvac.exceptions.InvalidPath()

        # Should not raise despite the underlying Vault exception
        await backend.delete_secret("missing-key")
        backend._client.secrets.kv.v2.delete_metadata_and_all_versions.assert_called_once()

    async def test_get_secret_timeout_wraps_as_runtime_error(self, mock_hvac):
        import asyncio

        backend = _make_backend(mock_hvac)

        with patch.object(asyncio, "wait_for", side_effect=TimeoutError()):
            with pytest.raises(RuntimeError, match="timeout reading secret"):
                await backend.get_secret("my-key")

    async def test_get_secret_network_error_wraps_as_runtime_error(self, mock_hvac):
        backend = _make_backend(mock_hvac)
        backend._client.secrets.kv.v2.read_secret_version.side_effect = ConnectionError("connection refused")

        with pytest.raises(RuntimeError, match="unexpected error reading secret"):
            await backend.get_secret("my-key")
