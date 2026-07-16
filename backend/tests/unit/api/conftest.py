"""Test configuration for API unit tests.

Sets minimal env vars so ``get_settings()`` (called by middleware at
request time) can construct a ``Settings`` instance.
"""

import os
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "a" * 32)
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("MODULO_ADMIN_PASSWORD", "test")
os.environ.setdefault("MODULO_CSRF_ENABLED", "false")


@pytest.fixture(autouse=True)
def _prevent_db_auth_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent ``_verify_identity`` from connecting to a real database.

    All API unit tests mock the auth layer via ``dependency_overrides``
    on ``get_current_user``, but ``get_current_tenant_user`` also calls
    ``_verify_identity()`` which connects to Postgres to confirm the
    JWT's account/org still exist.  Monkey-patching ``_verify_identity``
    here avoids the DB call for every test in this package.
    """
    monkeypatch.setattr("modulo.auth.dependencies._verify_identity", AsyncMock(return_value=None))
