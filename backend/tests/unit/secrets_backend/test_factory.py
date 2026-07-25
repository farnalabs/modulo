"""Tests for create_secrets_backend factory."""

import os
from unittest.mock import patch

import pytest

from modulo.core.secrets_backend import create_secrets_backend, validate_key
from modulo.core.secrets_backend.fernet import FernetSecretsBackend
from conftest import FERNET_TEST_KEY
from base import external_backend_patches


def test_fernet_backend_created_by_default():
    backend = create_secrets_backend(fernet_key=FERNET_TEST_KEY)
    assert isinstance(backend, FernetSecretsBackend)


def test_fernet_backend_created_by_name():
    backend = create_secrets_backend(fernet_key=FERNET_TEST_KEY, backend_name="fernet")
    assert isinstance(backend, FernetSecretsBackend)


def test_vault_backend_created_by_name():
    from modulo.core.secrets_backend.vault import VaultSecretsBackend

    with external_backend_patches("vault", {"VAULT_ADDR": "http://vault:8200", "VAULT_TOKEN": "x"}):
        backend = create_secrets_backend(fernet_key=FERNET_TEST_KEY, backend_name="vault")
        assert isinstance(backend, VaultSecretsBackend)


@pytest.mark.skip(reason="Flaky: unawaited coroutine warning in worktree env")
def test_aws_backend_created_by_name():
    from modulo.core.secrets_backend.aws import AWSSecretsManagerBackend

    with external_backend_patches(
        "aws",
        {"AWS_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y"},
    ):
        backend = create_secrets_backend(fernet_key=FERNET_TEST_KEY, backend_name="aws")
        assert isinstance(backend, AWSSecretsManagerBackend)


def test_unknown_backend_raises_value_error():
    with pytest.raises(ValueError, match="Unknown"):
        create_secrets_backend(fernet_key=FERNET_TEST_KEY, backend_name="nonexistent")


def test_env_var_used_when_no_backend_name():
    with patch.dict(os.environ, {"MODULO_SECRETS_BACKEND": "fernet"}):
        backend = create_secrets_backend(fernet_key=FERNET_TEST_KEY)
        assert isinstance(backend, FernetSecretsBackend)


@pytest.mark.parametrize(
    "backend_name,env_vars,expected_cls",
    [
        pytest.param(
            "vault",
            {"VAULT_ADDR": "http://vault:8200", "VAULT_TOKEN": "x"},
            "vault",
            id="vault",
        ),
        pytest.param(
            "aws",
            {"AWS_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y"},
            "aws",
            id="aws",
        ),
    ],
)
def test_fernet_key_optional_for_external_backend(backend_name, env_vars, expected_cls):
    if expected_cls == "vault":
        from modulo.core.secrets_backend.vault import VaultSecretsBackend as cls
    else:
        from modulo.core.secrets_backend.aws import AWSSecretsManagerBackend as cls

    with external_backend_patches(backend_name, env_vars):
        backend = create_secrets_backend(fernet_key=None, backend_name=backend_name)
        assert isinstance(backend, cls)


@pytest.mark.parametrize("name", ["  Vault  ", "  vault  "])
def test_backend_name_normalized(name):
    from modulo.core.secrets_backend.vault import VaultSecretsBackend

    with external_backend_patches("vault", {"VAULT_ADDR": "http://vault:8200", "VAULT_TOKEN": "x"}):
        backend = create_secrets_backend(fernet_key=None, backend_name=name)
        assert isinstance(backend, VaultSecretsBackend)


def test_fernet_key_required_when_backend_is_fernet():
    with pytest.raises(ValueError, match="fernet_key is required"):
        create_secrets_backend(fernet_key=None)


def test_empty_key_raises_value_error():
    with pytest.raises(ValueError, match="non-empty"):
        validate_key("")
