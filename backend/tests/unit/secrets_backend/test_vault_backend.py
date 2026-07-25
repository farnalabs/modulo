"""Unit tests for VaultSecretsBackend."""

import pytest

from modulo.core.secrets_backend.vault import VaultSecretsBackend

try:
    import hvac  # noqa: F401
    _HVAC_AVAILABLE = True
except ImportError:
    _HVAC_AVAILABLE = False

from base import ExternalSecretsBackendTestBase, make_vault_backend

pytestmark = [
    pytest.mark.skipif(not _HVAC_AVAILABLE, reason="hvac package not installed"),
    pytest.mark.usefixtures("vault_env", "mock_hvac"),
]


class TestVaultSecretsBackend(ExternalSecretsBackendTestBase):

    def create_backend(self):
        return make_vault_backend()

    def setup_get_success(self, backend, value="my-value"):
        backend._client.secrets.kv.v2.read_secret_version.return_value = {
            "data": {"data": {"value": value}},
        }

    def setup_get_not_found(self, backend):
        from modulo.core.secrets_backend.vault import _hvac
        backend._client.secrets.kv.v2.read_secret_version.side_effect = (
            _hvac.exceptions.InvalidPath()
        )

    def assert_get_called(self, backend, key="my-key"):
        backend._client.secrets.kv.v2.read_secret_version.assert_called_once()

    def setup_create_conflict(self, backend):
        pass

    def assert_set_new_called(self, backend, key="my-key", value="my-value"):
        backend._client.secrets.kv.v2.create_or_update_secret.assert_called_once()

    def assert_set_update_called(self, backend, key="my-key", value="my-value"):
        backend._client.secrets.kv.v2.create_or_update_secret.assert_called_once()

    def setup_delete_not_found(self, backend):
        from modulo.core.secrets_backend.vault import _hvac
        backend._client.secrets.kv.v2.delete_metadata_and_all_versions.side_effect = (
            _hvac.exceptions.InvalidPath()
        )

    def assert_delete_called(self, backend, key="my-key"):
        backend._client.secrets.kv.v2.delete_metadata_and_all_versions.assert_called_once()

    def setup_network_error(self, backend):
        backend._client.secrets.kv.v2.read_secret_version.side_effect = ConnectionError(
            "connection refused"
        )
