"""Verify all 4 API key route handlers return 501 on ProgrammingError."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.routes.api_keys import router as api_keys_router
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_KEY_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


_app = FastAPI()
_app.include_router(api_keys_router)


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

    _app.dependency_overrides[get_settings] = _make_settings
    _app.dependency_overrides[get_db_session] = override_session
    _app.dependency_overrides[_get_engine] = lambda: MagicMock()
    _app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    try:
        yield TestClient(_app)
    finally:
        _app.dependency_overrides.clear()


class TestApiKeysProgrammingError:
    """All 4 DB-accessing route handlers should return 501 on ProgrammingError."""

    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("post", "/api/v1/api-keys", {"name": "k", "role": "operator"}),
            ("get", "/api/v1/api-keys", None),
            ("put", f"/api/v1/api-keys/{_KEY_ID}", {"name": "k", "role": "operator"}),
            ("delete", f"/api/v1/api-keys/{_KEY_ID}", None),
        ],
    )
    def test_returns_501_on_programming_error(self, client: TestClient, method: str, path: str, body: object) -> None:
        if method == "post":
            resp = client.post(path, json=body)
        elif method == "put":
            resp = client.put(path, json=body)
        elif method == "delete":
            resp = client.delete(path)
        else:
            resp = client.get(path)
        assert resp.status_code == 501
        assert "Run database migrations" in resp.json()["detail"]
