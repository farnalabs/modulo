from collections.abc import Generator
from unittest.mock import patch

import pytest


@pytest.fixture
def vault_env() -> Generator[None, None, None]:
    with patch.dict("os.environ", {"VAULT_ADDR": "http://localhost:8200", "VAULT_TOKEN": "test-token"}):
        yield


@pytest.fixture
def aws_env() -> Generator[None, None, None]:
    with patch.dict(
        "os.environ",
        {"AWS_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "test-key", "AWS_SECRET_ACCESS_KEY": "test-secret"},
    ):
        yield
