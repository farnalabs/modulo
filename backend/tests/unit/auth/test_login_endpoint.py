"""Login endpoint and /me tests via FastAPI TestClient."""

import os
import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.passwords import hash_password
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _override(admin_password: str = "testpass") -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password=admin_password,
    )


def _make_mock_user() -> MagicMock:
    user = MagicMock()
    user.id = _USER_ID
    user.email = "admin@example.com"
    user.display_name = "Admin User"
    user.org_role = "admin"
    user.active = True
    user.organisation_id = _ORG_ID
    user.password_hash = hash_password("testpass")
    return user


@pytest.fixture(autouse=True)
def _set_env() -> Generator[None, None, None]:
    """Set required env vars for middleware that calls get_settings() directly."""
    old = {k: os.environ.pop(k, None) for k in ("DATABASE_URL", "SECRET_KEY", "FERNET_KEY")}
    os.environ["DATABASE_URL"] = "postgresql+asyncpg://localhost/test"
    os.environ["SECRET_KEY"] = _VALID_32
    os.environ["FERNET_KEY"] = _VALID_32
    # bust the lru_cache so get_settings() picks up the new env values
    get_settings.cache_clear()
    try:
        yield
    finally:
        for k, v in old.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        get_settings.cache_clear()


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture()
def client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _override
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# /api/v1/auth/login
# ---------------------------------------------------------------------------


def test_login_success(client: TestClient) -> None:
    mock_user = _make_mock_user()
    mock_family = MagicMock()
    mock_family.family_id = uuid.uuid4()
    with (
        patch("modulo.api.routes.auth.get_user_by_email", new=AsyncMock(return_value=mock_user)),
        patch("modulo.api.routes.auth.authenticate_db_user", return_value=True),
        patch("modulo.api.routes.auth.update_last_login", new=AsyncMock()),
        patch("modulo.api.routes.auth.create_family", new=AsyncMock(return_value=mock_family)),
    ):
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": "testpass"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert "refresh_token" in body
    assert body["token_type"] == "bearer"
    assert len(body["access_token"]) > 0


def test_login_wrong_password(client: TestClient) -> None:
    mock_user = _make_mock_user()
    with (
        patch("modulo.api.routes.auth.get_user_by_email", new=AsyncMock(return_value=mock_user)),
        patch("modulo.api.routes.auth.authenticate_db_user", return_value=False),
    ):
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": "wrong"},
        )
    assert resp.status_code == 401


def test_login_unknown_user(client: TestClient) -> None:
    with patch("modulo.api.routes.auth.get_user_by_email", new=AsyncMock(return_value=None)):
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "testpass"},
        )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /api/v1/auth/me
# ---------------------------------------------------------------------------


def test_me_returns_username(client: TestClient) -> None:
    from datetime import UTC, datetime

    mock_user = _make_mock_user()
    mock_user.created_at = datetime.now(UTC)
    mock_family = MagicMock()
    mock_family.family_id = uuid.uuid4()
    with (
        patch("modulo.api.routes.auth.get_user_by_email", new=AsyncMock(return_value=mock_user)),
        patch("modulo.api.routes.auth.authenticate_db_user", return_value=True),
        patch("modulo.api.routes.auth.update_last_login", new=AsyncMock()),
        patch("modulo.api.routes.auth.create_family", new=AsyncMock(return_value=mock_family)),
    ):
        login_resp = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": "testpass"},
        )
    token = login_resp.json()["access_token"]

    with patch("modulo.api.routes.auth.get_user_by_id", new=AsyncMock(return_value=mock_user)):
        resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == "admin@example.com"


def test_me_without_token_returns_4xx(client: TestClient) -> None:
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code in (401, 403)


def test_me_with_invalid_token_returns_401(client: TestClient) -> None:
    resp = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer notavalidtoken"},
    )
    assert resp.status_code == 401


def test_healthz_does_not_require_auth(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
