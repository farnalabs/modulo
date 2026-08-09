"""Unit tests for AWSSecretsManagerBackend.

The boto3 client is fully mocked via the ``mock_boto3`` fixture, so these tests
run even when the optional *boto3* package is not installed.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from modulo.core.secrets_backend.aws import AWSSecretsManagerBackend

pytestmark = [
    pytest.mark.usefixtures("aws_env"),
]


@pytest.fixture
def mock_boto3():
    with (
        patch("modulo.core.secrets_backend.aws._MODULE_AVAILABLE", True),
        patch("modulo.core.secrets_backend.aws._boto3") as mb,
    ):
        yield mb


def _make_backend():
    """Build a backend whose client is a mock with the exception classes it references."""
    backend = AWSSecretsManagerBackend()
    mock_client = MagicMock()
    mock_client.exceptions.ResourceNotFoundException = type("ResourceNotFoundException", (Exception,), {})
    mock_client.exceptions.AccessDeniedException = type("AccessDeniedException", (Exception,), {})
    mock_client.exceptions.ResourceExistsException = type("ResourceExistsException", (Exception,), {})
    backend._client = mock_client
    return backend


class TestAWSSecretsManagerBackend:
    async def test_empty_key_raises_value_error(self, mock_boto3):
        backend = _make_backend()
        with pytest.raises(ValueError, match="non-empty"):
            await backend.get_secret("")

    async def test_get_secret_reads_from_aws(self, mock_boto3):
        backend = _make_backend()
        backend._client.get_secret_value.return_value = {"SecretString": "my-value"}

        value = await backend.get_secret("my-key")

        assert value == "my-value"
        backend._client.get_secret_value.assert_called_once_with(SecretId="my-key")

    async def test_get_secret_unknown_key_raises_key_error(self, mock_boto3):
        backend = _make_backend()
        backend._client.get_secret_value.side_effect = backend._client.exceptions.ResourceNotFoundException()

        with pytest.raises(KeyError):
            await backend.get_secret("unknown-key")

    async def test_get_secret_access_denied_raises_permission_error(self, mock_boto3):
        backend = _make_backend()
        backend._client.get_secret_value.side_effect = backend._client.exceptions.AccessDeniedException()

        with pytest.raises(PermissionError, match="access denied"):
            await backend.get_secret("restricted-key")

    async def test_get_secret_no_string_or_binary_raises_key_error(self, mock_boto3):
        """A response with neither SecretString nor SecretBinary is treated as missing."""
        backend = _make_backend()
        backend._client.get_secret_value.return_value = {}

        with pytest.raises(KeyError):
            await backend.get_secret("empty-key")

    async def test_get_secret_non_string_secret_string_raises_key_error(self, mock_boto3):
        """A non-string SecretString (e.g. a number) must not be returned as-is.

        Only str SecretString and bytes SecretBinary are trusted; anything else
        is treated as a missing secret and surfaces as KeyError.
        """
        backend = _make_backend()
        backend._client.get_secret_value.return_value = {"SecretString": 123}

        with pytest.raises(KeyError):
            await backend.get_secret("numeric-key")

    async def test_get_secret_non_bytes_binary_raises_key_error(self, mock_boto3):
        """A non-bytes SecretBinary is not trusted — treated as a missing secret."""
        backend = _make_backend()
        backend._client.get_secret_value.return_value = {"SecretBinary": "not-bytes"}

        with pytest.raises(KeyError):
            await backend.get_secret("binary-key")

    async def test_set_secret_empty_key_raises_value_error(self, mock_boto3):
        backend = _make_backend()

        with pytest.raises(ValueError, match="non-empty"):
            await backend.set_secret("", "my-value")

    async def test_delete_secret_empty_key_raises_value_error(self, mock_boto3):
        backend = _make_backend()

        with pytest.raises(ValueError, match="non-empty"):
            await backend.delete_secret("")

    async def test_set_secret_creates_new(self, mock_boto3):
        backend = _make_backend()

        await backend.set_secret("my-key", "my-value")

        backend._client.create_secret.assert_called_once_with(
            Name="my-key",
            SecretString="my-value",
            Description="Modulo secret",
        )

    async def test_set_secret_updates_existing(self, mock_boto3):
        backend = _make_backend()
        backend._client.create_secret.side_effect = backend._client.exceptions.ResourceExistsException()

        await backend.set_secret("my-key", "my-value")

        backend._client.update_secret.assert_called_once_with(
            SecretId="my-key",
            SecretString="my-value",
        )

    async def test_set_secret_timeout_wraps_as_runtime_error(self, mock_boto3):
        backend = _make_backend()

        with (
            patch.object(asyncio, "wait_for", side_effect=TimeoutError()),
            pytest.raises(RuntimeError, match="timeout writing secret"),
        ):
            await backend.set_secret("my-key", "my-value")

    async def test_set_secret_toctou_retries_create(self, mock_boto3):
        """Secret deleted between the create/update race — create is retried."""
        backend = _make_backend()
        backend._client.create_secret.side_effect = [
            backend._client.exceptions.ResourceExistsException(),
            None,
        ]
        backend._client.update_secret.side_effect = backend._client.exceptions.ResourceNotFoundException()

        await backend.set_secret("my-key", "my-value")

        assert backend._client.create_secret.call_count == 2, "Expected create to be retried after TOCTOU race"
        assert backend._client.update_secret.call_count == 1, "Expected update attempted once before race recovery"

    async def test_delete_secret_removes_from_aws(self, mock_boto3):
        backend = _make_backend()

        await backend.delete_secret("my-key")

        backend._client.delete_secret.assert_called_once_with(
            SecretId="my-key",
            RecoveryWindowInDays=7,
            ForceDeleteWithoutRecovery=False,
        )

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
        backend = _make_backend()

        with (
            patch.object(asyncio, "wait_for", side_effect=TimeoutError()),
            pytest.raises(RuntimeError, match="timeout reading secret"),
        ):
            await backend.get_secret("my-key")

    async def test_delete_secret_timeout_wraps_as_runtime_error(self, mock_boto3):
        backend = _make_backend()

        with (
            patch.object(asyncio, "wait_for", side_effect=TimeoutError()),
            pytest.raises(RuntimeError, match="timeout deleting secret"),
        ):
            await backend.delete_secret("my-key")

    async def test_get_secret_network_error_wraps_as_runtime_error(self, mock_boto3):
        backend = _make_backend()
        backend._client.get_secret_value.side_effect = ConnectionError("connection refused")

        with pytest.raises(RuntimeError, match="unexpected error reading secret"):
            await backend.get_secret("my-key")


class TestEnsureClient:
    async def test_creates_session_with_static_credentials(self, mock_boto3):
        backend = AWSSecretsManagerBackend()

        client = await backend._ensure_client()

        mock_boto3.Session.assert_called_once_with(
            region_name="us-east-1",
            aws_access_key_id="test-key",
            aws_secret_access_key="test-secret",
        )
        assert client is mock_boto3.Session.return_value.client.return_value
        mock_boto3.Session.return_value.client.assert_called_once_with("secretsmanager")

    async def test_creates_session_with_profile(self, monkeypatch, mock_boto3):
        monkeypatch.setenv("AWS_PROFILE", "my-profile")
        monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
        monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
        backend = AWSSecretsManagerBackend()

        await backend._ensure_client()

        mock_boto3.Session.assert_called_once_with(region_name="us-east-1", profile_name="my-profile")

    async def test_profile_takes_precedence_over_static_credentials(self, monkeypatch, mock_boto3):
        """When both AWS_PROFILE and static credentials are set, the profile wins."""
        monkeypatch.setenv("AWS_PROFILE", "my-profile")
        backend = AWSSecretsManagerBackend()

        await backend._ensure_client()

        mock_boto3.Session.assert_called_once_with(region_name="us-east-1", profile_name="my-profile")

    async def test_creates_session_without_credentials(self, monkeypatch, mock_boto3):
        monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
        monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
        backend = AWSSecretsManagerBackend()

        await backend._ensure_client()

        mock_boto3.Session.assert_called_once_with(region_name="us-east-1")

    async def test_caches_client_across_calls(self, mock_boto3):
        backend = AWSSecretsManagerBackend()

        first = await backend._ensure_client()
        second = await backend._ensure_client()

        assert first is second
        mock_boto3.Session.assert_called_once()

    async def test_session_failure_propagates(self, mock_boto3):
        mock_boto3.Session.side_effect = ConnectionError("connection refused")
        backend = AWSSecretsManagerBackend()

        with pytest.raises(ConnectionError, match="connection refused"):
            await backend._ensure_client()

    async def test_session_cancelled_propagates(self, mock_boto3):
        mock_boto3.Session.side_effect = asyncio.CancelledError()
        backend = AWSSecretsManagerBackend()

        with pytest.raises(asyncio.CancelledError):
            await backend._ensure_client()

    async def test_constructor_raises_without_boto3(self):
        with (
            patch("modulo.core.secrets_backend.aws._MODULE_AVAILABLE", False),
            pytest.raises(RuntimeError, match="boto3"),
        ):
            AWSSecretsManagerBackend()

    async def test_ensure_client_raises_without_boto3(self, mock_boto3):
        backend = AWSSecretsManagerBackend()
        with (
            patch("modulo.core.secrets_backend.aws._MODULE_AVAILABLE", False),
            pytest.raises(RuntimeError, match="boto3"),
        ):
            await backend._ensure_client()


class TestErrorPaths:
    async def test_set_secret_create_unexpected_error_wraps_as_runtime_error(self, mock_boto3):
        backend = _make_backend()
        backend._client.create_secret.side_effect = ConnectionError("connection refused")

        with pytest.raises(RuntimeError, match="unexpected error writing secret"):
            await backend.set_secret("my-key", "my-value")

    async def test_set_secret_update_unexpected_error_wraps_as_runtime_error(self, mock_boto3):
        backend = _make_backend()
        backend._client.create_secret.side_effect = backend._client.exceptions.ResourceExistsException()
        backend._client.update_secret.side_effect = ConnectionError("connection refused")

        with pytest.raises(RuntimeError, match="unexpected error writing secret"):
            await backend.set_secret("my-key", "my-value")

    async def test_set_secret_update_timeout_wraps_as_runtime_error(self, mock_boto3):
        backend = _make_backend()
        backend._client.create_secret.side_effect = backend._client.exceptions.ResourceExistsException()
        backend._client.update_secret.side_effect = TimeoutError()

        with pytest.raises(RuntimeError, match="timeout writing secret"):
            await backend.set_secret("my-key", "my-value")

    async def test_delete_secret_unexpected_error_wraps_as_runtime_error(self, mock_boto3):
        backend = _make_backend()
        backend._client.delete_secret.side_effect = ConnectionError("connection refused")

        with pytest.raises(RuntimeError, match="unexpected error deleting secret"):
            await backend.delete_secret("my-key")

    async def test_get_secret_cancelled_error_propagates(self, mock_boto3):
        backend = _make_backend()
        backend._client.get_secret_value.side_effect = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await backend.get_secret("my-key")

    async def test_set_secret_cancelled_error_propagates(self, mock_boto3):
        backend = _make_backend()
        backend._client.create_secret.side_effect = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await backend.set_secret("my-key", "my-value")

    async def test_set_secret_update_cancelled_error_propagates(self, mock_boto3):
        backend = _make_backend()
        backend._client.create_secret.side_effect = backend._client.exceptions.ResourceExistsException()
        backend._client.update_secret.side_effect = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await backend.set_secret("my-key", "my-value")

    async def test_delete_secret_cancelled_error_propagates(self, mock_boto3):
        backend = _make_backend()
        backend._client.delete_secret.side_effect = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await backend.delete_secret("my-key")
