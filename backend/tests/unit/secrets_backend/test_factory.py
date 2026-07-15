"""Tests for create_secrets_backend factory."""

import os
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from modulo.core.secrets_backend import create_secrets_backend, validate_key
from modulo.core.secrets_backend.fernet import FernetSecretsBackend

_KEY = Fernet.generate_key().decode()


def test_fernet_backend_created_by_default():
    backend = create_secrets_backend(fernet_key=_KEY)
    assert isinstance(backend, FernetSecretsBackend)


def test_fernet_backend_created_by_name():
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    assert isinstance(backend, FernetSecretsBackend)


def test_vault_backend_created_by_name():
    from modulo.core.secrets_backend.vault import VaultSecretsBackend

    with (
        patch("modulo.core.secrets_backend._check_external_secrets_licensed", return_value=True),
        patch("modulo.core.secrets_backend.vault._MODULE_AVAILABLE", True),
        patch("modulo.core.secrets_backend.vault._hvac"),
        patch.dict(os.environ, {"VAULT_ADDR": "http://vault:8200", "VAULT_TOKEN": "x"}),
    ):
        backend = create_secrets_backend(fernet_key=_KEY, backend_name="vault")
        assert isinstance(backend, VaultSecretsBackend)


@pytest.mark.skip(reason="Flaky: unawaited coroutine warning in worktree env")
def test_aws_backend_created_by_name():
    from modulo.core.secrets_backend.aws import AWSSecretsManagerBackend

    with (
        patch("modulo.core.secrets_backend._check_external_secrets_licensed", return_value=True),
        patch("modulo.core.secrets_backend.aws._MODULE_AVAILABLE", True),
        patch("modulo.core.secrets_backend.aws._boto3"),
        patch.dict(
            os.environ,
            {
                "AWS_REGION": "us-east-1",
                "AWS_ACCESS_KEY_ID": "x",
                "AWS_SECRET_ACCESS_KEY": "y",
            },
        ),
    ):
        backend = create_secrets_backend(fernet_key=_KEY, backend_name="aws")
        assert isinstance(backend, AWSSecretsManagerBackend)


def test_unknown_backend_raises_value_error():
    with pytest.raises(ValueError, match="Unknown"):
        create_secrets_backend(fernet_key=_KEY, backend_name="nonexistent")


def test_env_var_used_when_no_backend_name():
    with patch.dict(os.environ, {"MODULO_SECRETS_BACKEND": "fernet"}):
        backend = create_secrets_backend(fernet_key=_KEY)
        assert isinstance(backend, FernetSecretsBackend)


@pytest.mark.parametrize(
    "backend_name,expected_cls,env_vars,module_patch,lib_patch",
    [
        pytest.param(
            "vault",
            None,
            {"VAULT_ADDR": "http://vault:8200", "VAULT_TOKEN": "x"},
            "modulo.core.secrets_backend.vault",
            "modulo.core.secrets_backend.vault._hvac",
            id="vault",
        ),
        pytest.param(
            "aws",
            None,
            {"AWS_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y"},
            "modulo.core.secrets_backend.aws",
            "modulo.core.secrets_backend.aws._boto3",
            id="aws",
        ),
    ],
)
def test_fernet_key_optional_for_external_backend(backend_name, expected_cls, env_vars, module_patch, lib_patch):
    if backend_name == "vault":
        from modulo.core.secrets_backend.vault import VaultSecretsBackend as expected_cls  # noqa: N813
    else:
        from modulo.core.secrets_backend.aws import AWSSecretsManagerBackend as expected_cls  # noqa: N813
    with (
        patch("modulo.core.secrets_backend._check_external_secrets_licensed", return_value=True),
        patch(f"{module_patch}._MODULE_AVAILABLE", True),
        patch(lib_patch),
        patch.dict(os.environ, env_vars),
    ):
        backend = create_secrets_backend(fernet_key=None, backend_name=backend_name)
        assert isinstance(backend, expected_cls)


@pytest.mark.parametrize("name", ["  Vault  ", "  vault  "])
def test_backend_name_normalized(name):
    """Factory lowercases and strips backend_name before matching."""
    from modulo.core.secrets_backend.vault import VaultSecretsBackend

    with (
        patch("modulo.core.secrets_backend._check_external_secrets_licensed", return_value=True),
        patch("modulo.core.secrets_backend.vault._MODULE_AVAILABLE", True),
        patch("modulo.core.secrets_backend.vault._hvac"),
        patch.dict(os.environ, {"VAULT_ADDR": "http://vault:8200", "VAULT_TOKEN": "x"}),
    ):
        backend = create_secrets_backend(fernet_key=None, backend_name=name)
        assert isinstance(backend, VaultSecretsBackend), f"Expected VaultSecretsBackend for name={name!r}"


def test_fernet_key_required_when_backend_is_fernet():
    with pytest.raises(ValueError, match="fernet_key is required"):
        create_secrets_backend(fernet_key=None)


def test_empty_key_raises_value_error():
    with pytest.raises(ValueError, match="non-empty"):
        validate_key("")
