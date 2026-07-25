"""Unit tests for AWSSecretsManagerBackend."""

from unittest.mock import MagicMock

import pytest

from modulo.core.secrets_backend.aws import AWSSecretsManagerBackend

try:
    import boto3  # noqa: F401
    _BOTO3_AVAILABLE = True
except ImportError:
    _BOTO3_AVAILABLE = False

from base import ExternalSecretsBackendTestBase, make_aws_backend

pytestmark = [
    pytest.mark.skipif(not _BOTO3_AVAILABLE, reason="boto3 package not installed"),
    pytest.mark.usefixtures("aws_env", "mock_boto3"),
]


class TestAWSSecretsManagerBackend(ExternalSecretsBackendTestBase):

    def create_backend(self):
        return make_aws_backend()

    def setup_get_success(self, backend, value="my-value"):
        backend._client.get_secret_value.return_value = {"SecretString": value}

    def setup_get_not_found(self, backend):
        backend._client.get_secret_value.side_effect = (
            backend._client.exceptions.ResourceNotFoundException()
        )

    def assert_get_called(self, backend, key="my-key"):
        backend._client.get_secret_value.assert_called_once_with(SecretId=key)

    def setup_create_conflict(self, backend):
        backend._client.create_secret.side_effect = (
            backend._client.exceptions.ResourceExistsException()
        )

    def assert_set_new_called(self, backend, key="my-key", value="my-value"):
        backend._client.create_secret.assert_called_once()

    def assert_set_update_called(self, backend, key="my-key", value="my-value"):
        backend._client.update_secret.assert_called_once_with(
            SecretId=key, SecretString=value,
        )

    def setup_delete_not_found(self, backend):
        backend._client.delete_secret.side_effect = (
            backend._client.exceptions.ResourceNotFoundException()
        )

    def assert_delete_called(self, backend, key="my-key"):
        backend._client.delete_secret.assert_called_once_with(
            SecretId=key,
            RecoveryWindowInDays=7,
            ForceDeleteWithoutRecovery=False,
        )

    def setup_network_error(self, backend):
        backend._client.get_secret_value.side_effect = ConnectionError("connection refused")

    # -- AWS-specific tests -------------------------------------------------

    async def test_get_secret_binary_decoded(self):
        backend = self.create_backend()
        backend._client.get_secret_value.return_value = {"SecretBinary": b"binary-value-utf8"}
        value = await backend.get_secret("binary-key")
        assert value == "binary-value-utf8"
        backend._client.get_secret_value.assert_called_once_with(SecretId="binary-key")
