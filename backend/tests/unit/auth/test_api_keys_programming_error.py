"""Verify all 4 API key route handlers return 501 on ProgrammingError."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings


_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_KEY_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
        modulo_public_url="http://localhost:8000",
        modulo_license_key="test-license-key",
    )


def _make_mock_session(raise_on_begin: bool = False) -> AsyncMock:
    session = AsyncMock()
    if raise_on_begin:
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(side_effect=ProgrammingError("mock", {}, None))
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)
    else:
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session(raise_on_begin=True)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
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


class TestApiKeysProgrammingError:
    """All 4 DB-accessing route handlers should return 501 on ProgrammingError."""

    def test_create_api_key_returns_501_on_programming_error(self, client: TestClient) -> None:
        resp = client.post("/api/v1/api-keys", json={"name": "k", "role": "operator"})
        assert resp.status_code == 501
        assert "Run database migrations" in resp.json()["detail"]

    def test_list_api_keys_returns_501_on_programming_error(self, client: TestClient) -> None:
        resp = client.get("/api/v1/api-keys")
        assert resp.status_code == 501
        assert "Run database migrations" in resp.json()["detail"]

    def test_update_api_key_returns_501_on_programming_error(self, client: TestClient) -> None:
        resp = client.put(
            f"/api/v1/api-keys/{_KEY_ID}",
            json={"name": "k", "role": "operator"},
        )
        assert resp.status_code == 501
        assert "Run database migrations" in resp.json()["detail"]

    def test_revoke_api_key_returns_501_on_programming_error(self, client: TestClient) -> None:
        resp = client.delete(f"/api/v1/api-keys/{_KEY_ID}")
        assert resp.status_code == 501
        assert "Run database migrations" in resp.json()["detail"]
