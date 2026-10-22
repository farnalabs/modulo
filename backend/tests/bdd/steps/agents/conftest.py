"""Fixture setup for prompt versioning BDD tests.

# MOCKED: This conftest uses MagicMock-based DB fixtures instead of the
# real async SQLAlchemy stack (testcontainers Postgres + Alembic migrations).
# Scheduled for replacement with real-stack fixtures.
#
# Relies on ``tests/bdd/conftest.py`` for environment setup and shared UUIDs.
"""

from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from tests.bdd.conftest import ORG_ID, USER_ID


def make_settings():
    from modulo.settings import Settings

    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


def make_mock_session() -> AsyncMock:
    session = AsyncMock()
    # set_rls_*/scanners read these AsyncSession members synchronously; a
    # bare AsyncMock child returns a never-awaited coroutine (unraisable leak).
    session.add_all = MagicMock()
    session.expunge = MagicMock()
    _bdd_nested_cm = AsyncMock()
    _bdd_nested_cm.__aenter__ = AsyncMock(return_value=None)
    _bdd_nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=_bdd_nested_cm)
    # set_rls_* reads these AsyncSession members synchronously; a bare
    # AsyncMock child returns a never-awaited coroutine (unraisable-warning leak).
    session.info = {}
    # FAR-1639 (FAR-1629 sweep): in_transaction() and add() are SYNC
    # AsyncSession methods — bare-AsyncMock children type them as async, so
    # production code reaching set_rls_* (db/rls.py::_ensure_active_transaction)
    # passes the guard vacuously (an unawaited coroutine is truthy). Tests that
    # need the deny path override this with MagicMock(return_value=False).
    session.in_transaction = MagicMock(return_value=True)
    session.add = MagicMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    default_result = MagicMock()
    default_result.scalar_one_or_none.return_value = MagicMock()
    session.execute = AsyncMock(return_value=default_result)
    return session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    from modulo.api.dependencies import _get_engine, get_db_session
    from modulo.api.main import app
    from modulo.auth.dependencies import get_current_user
    from modulo.auth.jwt import AuthenticatedPrincipal
    from modulo.settings import get_settings

    mock_session = make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=ORG_ID,
        account_id=USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()
