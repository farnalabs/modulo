"""Shared fixtures and constants for secrets backend tests."""

import uuid
from collections.abc import Generator
from unittest.mock import patch

import pytest

from cryptography.fernet import Fernet

FERNET_TEST_KEY = Fernet.generate_key().decode()
SECRET_TEST_VALUE = "my-secret-value"
ORG_ID = uuid.UUID(int=42)


@pytest.fixture
def vault_env() -> Generator[None, None, None]:
    with patch.dict("os.environ", {"VAULT_ADDR": "http://localhost:8200", "VAULT_TOKEN": "test-token"}):
        yield


@pytest.fixture
def aws_env() -> Generator[None, None, None]:
    with patch.dict(
        "os.environ",
        {
            "AWS_REGION": "us-east-1",
            "AWS_ACCESS_KEY_ID": "test-key",
            "AWS_SECRET_ACCESS_KEY": "test-secret",
        },
    ):
        yield


@pytest.fixture
def mock_boto3():
    with (
        patch("modulo.core.secrets_backend.aws._MODULE_AVAILABLE", True),
        patch("modulo.core.secrets_backend.aws._boto3") as mb,
    ):
        yield mb


@pytest.fixture
def mock_hvac():
    with (
        patch("modulo.core.secrets_backend.vault._MODULE_AVAILABLE", True),
        patch("modulo.core.secrets_backend.vault._hvac") as mh,
    ):
        mh.exceptions.InvalidPath = type("InvalidPath", (Exception,), {})
        mh.exceptions.Forbidden = type("Forbidden", (Exception,), {})
        yield mh
