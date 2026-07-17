"""Test configuration for API unit tests.

Sets minimal env vars so ``get_settings()`` (called by middleware at
request time) can construct a ``Settings`` instance.
"""

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "a" * 32)
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("MODULO_ADMIN_PASSWORD", "test")
os.environ.setdefault("MODULO_CSRF_ENABLED", "false")


@pytest.fixture(autouse=True)
def _noop_verify_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent _verify_identity from querying the database in unit tests.

    ``get_current_tenant_user`` calls ``_verify_identity`` which connects
    to the database to check that the account and organisation still exist.
    Unit tests have no DB tables or seed data, so the query would raise
    401 ``auth.account_not_found`` on every authenticated request.

    Monkeypatch it to a no-op so that individual test ``client`` fixtures
    can override ``get_current_user`` and return a fake principal without
    needing to also override ``get_current_tenant_user``.
    """

    async def _noop(principal: object) -> None:
        return None

    monkeypatch.setattr(
        "modulo.auth.dependencies._verify_identity",
        _noop,
    )
