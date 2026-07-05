"""Fixture setup for prompt versioning BDD tests."""

import os
import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "b" * 32)
os.environ.setdefault("MODULO_CSRF_ENABLED", "false")
os.environ.setdefault("REDIS_URL", "")

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def make_settings() -> "Settings":
    from modulo.settings import Settings
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


def make_mock_session() -> AsyncMock:
    session = AsyncMock()
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
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()
