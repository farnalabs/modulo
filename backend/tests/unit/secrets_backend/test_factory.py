"""Tests for create_secrets_backend factory."""

import os
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from modulo.core.secrets_backend import create_secrets_backend
from modulo.core.secrets_backend.aws import AWSSecretsManagerBackend
from modulo.core.secrets_backend.fernet import FernetSecretsBackend
from modulo.core.secrets_backend.vault import VaultSecretsBackend

_KEY = Fernet.generate_key().decode()


def test_fernet_backend_created_by_default():
    backend = create_secrets_backend(fernet_key=_KEY)
    assert isinstance(backend, FernetSecretsBackend)


def test_fernet_backend_created_by_name():
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    assert isinstance(backend, FernetSecretsBackend)


def test_vault_backend_created_by_name():
    with patch("modulo.core.secrets_backend.vault._MODULE_AVAILABLE", True):
        with patch("modulo.core.secrets_backend.vault._hvac"):
            with patch.dict(os.environ, {"VAULT_ADDR": "http://vault:8200", "VAULT_TOKEN": "x"}):
                backend = create_secrets_backend(fernet_key=_KEY, backend_name="vault")
                assert isinstance(backend, VaultSecretsBackend)


def test_aws_backend_created_by_name():
    with patch("modulo.core.secrets_backend.aws._MODULE_AVAILABLE", True):
        with patch("modulo.core.secrets_backend.aws._boto3"):
            with patch.dict(
                os.environ,
                {
                    "AWS_REGION": "us-east-1",
                    "AWS_ACCESS_KEY_ID": "x",
                    "AWS_SECRET_ACCESS_KEY": "y",
                },
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


def test_fernet_key_optional_for_vault_backend():
    with patch("modulo.core.secrets_backend.vault._MODULE_AVAILABLE", True):
        with patch("modulo.core.secrets_backend.vault._hvac"):
            with patch.dict(os.environ, {"VAULT_ADDR": "http://vault:8200", "VAULT_TOKEN": "x"}):
                backend = create_secrets_backend(fernet_key=None, backend_name="vault")
                assert isinstance(backend, VaultSecretsBackend)


def test_fernet_key_optional_for_aws_backend():
    with patch("modulo.core.secrets_backend.aws._MODULE_AVAILABLE", True):
        with patch("modulo.core.secrets_backend.aws._boto3"):
            with patch.dict(
                os.environ,
                {
                    "AWS_REGION": "us-east-1",
                    "AWS_ACCESS_KEY_ID": "x",
                    "AWS_SECRET_ACCESS_KEY": "y",
                },
            ):
                backend = create_secrets_backend(fernet_key=None, backend_name="aws")
                assert isinstance(backend, AWSSecretsManagerBackend)


def test_fernet_key_required_when_backend_is_fernet():
    with pytest.raises(ValueError, match="fernet_key is required"):
        create_secrets_backend(fernet_key=None)
