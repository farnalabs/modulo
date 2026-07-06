"""Unit tests for AWSSecretsManagerBackend."""

import os
from unittest.mock import MagicMock, patch

import pytest

from modulo.core.secrets_backend.aws import AWSSecretsManagerBackend

try:
    import boto3  # noqa: F401

    _BOTO3_AVAILABLE = True
except ImportError:
    _BOTO3_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _BOTO3_AVAILABLE,
    reason="boto3 package not installed",
)


@pytest.fixture(autouse=True)
def _env():
    with patch.dict(
        os.environ,
        {
            "AWS_REGION": "us-east-1",
            "AWS_ACCESS_KEY_ID": "test-key",
            "AWS_SECRET_ACCESS_KEY": "test-secret",
        },
    ):
        yield


@pytest.fixture
def mock_boto3():
    with patch("modulo.core.secrets_backend.aws._MODULE_AVAILABLE", True):
        with patch("modulo.core.secrets_backend.aws._boto3") as mb:
            yield mb


def _make_backend():
    backend = AWSSecretsManagerBackend()
    mock_client = MagicMock()
    mock_client.exceptions.ResourceNotFoundException = type("RNF", (Exception,), {})
    mock_client.exceptions.AccessDeniedException = type("ADE", (Exception,), {})
    mock_client.exceptions.ResourceExistsException = type("REE", (Exception,), {})
    backend._client = mock_client
    return backend


class TestAWSSecretsManagerBackend:
    async def test_get_secret_reads_from_aws(self, mock_boto3):
        backend = _make_backend()
        backend._client.get_secret_value.return_value = {"SecretString": "my-value"}

        value = await backend.get_secret("my-key")

        assert value == "my-value"
        backend._client.get_secret_value.assert_called_once_with(SecretId="my-key")

    async def test_get_secret_unknown_key_raises(self, mock_boto3):
        backend = _make_backend()
        backend._client.get_secret_value.side_effect = backend._client.exceptions.ResourceNotFoundException()

        with pytest.raises(KeyError):
            await backend.get_secret("unknown-key")

    async def test_set_secret_creates_new(self, mock_boto3):
        backend = _make_backend()

        await backend.set_secret("my-key", "my-value")

        backend._client.create_secret.assert_called_once()

    async def test_set_secret_updates_existing(self, mock_boto3):
        backend = _make_backend()
        backend._client.create_secret.side_effect = backend._client.exceptions.ResourceExistsException()

        await backend.set_secret("my-key", "my-value")

        backend._client.update_secret.assert_called_once_with(
            SecretId="my-key",
            SecretString="my-value",
        )

    async def test_delete_secret_removes_from_aws(self, mock_boto3):
        backend = _make_backend()

        await backend.delete_secret("my-key")

        backend._client.delete_secret.assert_called_once()

    async def test_get_secret_binary_decoded(self, mock_boto3):
        """SecretBinary is decoded as UTF-8 when SecretString is absent."""
        backend = _make_backend()
        backend._client.get_secret_value.return_value = {"SecretBinary": b"binary-value-utf8"}

        value = await backend.get_secret("binary-key")

        assert value == "binary-value-utf8"
        backend._client.get_secret_value.assert_called_once_with(SecretId="binary-key")

    async def test_delete_secret_noop_when_missing(self, mock_boto3):
        backend = _make_backend()
        backend._client.delete_secret.side_effect = backend._client.exceptions.ResourceNotFoundException()

        # Should not raise despite the underlying AWS exception
        await backend.delete_secret("missing-key")
        backend._client.delete_secret.assert_called_once_with(
            SecretId="missing-key",
            RecoveryWindowInDays=7,
            ForceDeleteWithoutRecovery=False,
        )

    async def test_get_secret_timeout_wraps_as_runtime_error(self, mock_boto3):
        import asyncio

        backend = _make_backend()

        with patch.object(asyncio, "wait_for", side_effect=TimeoutError()):
            with pytest.raises(RuntimeError, match="timeout reading secret"):
                await backend.get_secret("my-key")

    async def test_get_secret_network_error_wraps_as_runtime_error(self, mock_boto3):
        backend = _make_backend()
        backend._client.get_secret_value.side_effect = ConnectionError("connection refused")

        with pytest.raises(RuntimeError, match="unexpected error reading secret"):
            await backend.get_secret("my-key")
