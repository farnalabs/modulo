"""Global fixtures for all unit tests."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _patch_verify_identity() -> None:
    """Prevent _verify_identity from connecting to a real database.

    The _verify_identity function in auth/dependencies creates its own
    database engine and queries the real DB to check account/org existence.
    This bypasses all FastAPI dependency overrides, causing 401 errors when
    the local Postgres is running but the test UUIDs don't match real data.
    """
    with patch("modulo.auth.dependencies._verify_identity", new_callable=AsyncMock):
        yield
