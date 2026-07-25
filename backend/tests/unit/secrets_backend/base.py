"""Shared base classes and utilities for secrets backend tests."""

import asyncio
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers for Fernet backend tests
# ---------------------------------------------------------------------------

def make_mock_execute(org_id=None, scalar_result=None):
    """Return an async mock execute that handles current_setting queries.

    Parameters
    ----------
    org_id : uuid.UUID or str or None
        Value returned by ``current_setting`` queries (as string).
    scalar_result : any
        Value returned by ``scalar_one_or_none`` for non-current_setting
        statements.
    """
    async def mock_execute(stmt, *args, **kwargs):
        result = MagicMock()
        if "current_setting" in str(stmt):
            result.scalar.return_value = str(org_id) if org_id else None
        else:
            result.scalar_one_or_none.return_value = scalar_result
        return result
    return mock_execute


def set_org_id(session, org_id):
    """Patch *session.execute* so ``current_setting`` queries return *org_id*.

    Non-current_setting statements are forwarded to the original execute.
    """
    real_execute = session.execute

    async def mock_execute(stmt, *args, **kwargs):
        result = MagicMock()
        if "current_setting" in str(stmt):
            result.scalar.return_value = str(org_id)
        else:
            return await real_execute(stmt, *args, **kwargs)
        return result

    session.execute = AsyncMock(side_effect=mock_execute)


# ---------------------------------------------------------------------------
# Helpers for external secrets backend tests
# ---------------------------------------------------------------------------

def make_aws_backend():
    """Return an AWSSecretsManagerBackend with a pre-configured mock client."""
    from modulo.core.secrets_backend.aws import AWSSecretsManagerBackend

    backend = AWSSecretsManagerBackend()
    mock_client = MagicMock()
    mock_client.exceptions.ResourceNotFoundException = type("RNF", (Exception,), {})
    mock_client.exceptions.AccessDeniedException = type("ADE", (Exception,), {})
    mock_client.exceptions.ResourceExistsException = type("REE", (Exception,), {})
    backend._client = mock_client
    return backend


def make_vault_backend():
    """Return a VaultSecretsBackend with a mock client."""
    from modulo.core.secrets_backend.vault import VaultSecretsBackend

    backend = VaultSecretsBackend()
    backend._client = MagicMock()
    return backend


@contextmanager
def external_backend_patches(module_name, env_vars):
    """Context manager with the common patches needed for external backend tests.

    Parameters
    ----------
    module_name : str
        ``"vault"`` or ``"aws"``.
    env_vars : dict
        Environment variables required by the backend.
    """
    lib_map = {
        "vault": "modulo.core.secrets_backend.vault._hvac",
        "aws": "modulo.core.secrets_backend.aws._boto3",
    }
    module = f"modulo.core.secrets_backend.{module_name}"
    lib = lib_map[module_name]
    with (
        patch("modulo.core.secrets_backend._check_external_secrets_licensed", return_value=True),
        patch(f"{module}._MODULE_AVAILABLE", True),
        patch(lib),
        patch.dict("os.environ", env_vars),
    ):
        yield


# ---------------------------------------------------------------------------
# Shared test base for external backends
# ---------------------------------------------------------------------------

class ExternalSecretsBackendTestBase:
    """Mixin providing 9 shared test methods for external secrets backends.

    Subclasses **must** implement the hook methods listed below.
    """

    # -- hooks (override in subclass) ---------------------------------------

    def create_backend(self):
        raise NotImplementedError

    def setup_get_success(self, backend, value="my-value"):
        raise NotImplementedError

    def setup_get_not_found(self, backend):
        raise NotImplementedError

    def assert_get_called(self, backend, key="my-key"):
        raise NotImplementedError

    def setup_create_conflict(self, backend):
        raise NotImplementedError

    def assert_set_new_called(self, backend, key="my-key", value="my-value"):
        raise NotImplementedError

    def assert_set_update_called(self, backend, key="my-key", value="my-value"):
        raise NotImplementedError

    def setup_delete_not_found(self, backend):
        raise NotImplementedError

    def assert_delete_called(self, backend, key="my-key"):
        raise NotImplementedError

    def setup_network_error(self, backend):
        raise NotImplementedError

    # -- optional hooks (default no-op) --------------------------------------

    @staticmethod
    def setup_create_success(backend):
        pass

    @staticmethod
    def setup_delete_success(backend):
        pass

    # -- shared tests --------------------------------------------------------

    async def test_empty_key_raises_value_error(self):
        backend = self.create_backend()
        with pytest.raises(ValueError, match="non-empty"):
            await backend.get_secret("")

    async def test_get_secret_reads_from_backend(self):
        backend = self.create_backend()
        self.setup_get_success(backend)
        value = await backend.get_secret("my-key")
        assert value == "my-value"
        self.assert_get_called(backend)

    async def test_get_secret_unknown_key_raises_key_error(self):
        backend = self.create_backend()
        self.setup_get_not_found(backend)
        with pytest.raises(KeyError):
            await backend.get_secret("unknown-key")

    async def test_set_secret_creates_new(self):
        backend = self.create_backend()
        self.setup_create_success(backend)
        await backend.set_secret("my-key", "my-value")
        self.assert_set_new_called(backend)

    async def test_set_secret_updates_existing(self):
        backend = self.create_backend()
        self.setup_create_conflict(backend)
        await backend.set_secret("my-key", "my-value")
        self.assert_set_update_called(backend)

    async def test_delete_secret_removes_from_backend(self):
        backend = self.create_backend()
        self.setup_delete_success(backend)
        await backend.delete_secret("my-key")
        self.assert_delete_called(backend)

    async def test_delete_secret_noop_when_missing(self):
        backend = self.create_backend()
        self.setup_delete_not_found(backend)
        await backend.delete_secret("missing-key")
        self.assert_delete_called(backend, key="missing-key")

    async def test_get_secret_timeout_wraps_as_runtime_error(self):
        backend = self.create_backend()
        with (
            patch.object(asyncio, "wait_for", side_effect=TimeoutError()),
            pytest.raises(RuntimeError, match="timeout reading secret"),
        ):
            await backend.get_secret("my-key")

    async def test_get_secret_network_error_wraps_as_runtime_error(self):
        backend = self.create_backend()
        self.setup_network_error(backend)
        with pytest.raises(RuntimeError, match="unexpected error reading secret"):
            await backend.get_secret("my-key")
