"""Login endpoint and /me tests via FastAPI TestClient."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.routes.auth import router as auth_router
from modulo.api.routes.health import router as health_router
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
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
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
    user.is_system_admin = False
    user.must_change_password = False
    return user


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    monkeypatch.setenv("SECRET_KEY", _VALID_32)
    monkeypatch.setenv("FERNET_KEY", _VALID_32)
    get_settings.cache_clear()


@pytest.fixture
def mock_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = []
    result_mock.scalars.return_value = scalars_mock
    session.execute.return_value = result_mock
    return session


@pytest.fixture
def app() -> FastAPI:
    _app = FastAPI()
    _app.include_router(auth_router)
    _app.include_router(health_router)
    return _app


@pytest.fixture
def client(mock_session: AsyncMock, app: FastAPI) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _override
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_login_success(client: TestClient) -> None:
    mock_user = _make_mock_user()
    mock_family = MagicMock()
    mock_family.family_id = uuid.uuid4()
    mock_membership = MagicMock()
    mock_membership.organisation_id = _ORG_ID
    mock_membership.role = "admin"
    with (
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=mock_user)),
        patch("modulo.api.routes.auth.authenticate_db_user", return_value=True),
        patch("modulo.api.routes.auth.update_last_login", new=AsyncMock()),
        patch("modulo.api.routes.auth.create_family", new=AsyncMock(return_value=mock_family)),
        patch("modulo.api.routes.auth.list_memberships_for_account", new=AsyncMock(return_value=[mock_membership])),
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
    assert body["access_token"]
    assert body["must_change_password"] is False


def test_login_response_flags_must_change_password(client: TestClient) -> None:
    """FAR-460: an admin reset leaves must_change_password=True; the login
    response carries the flag so the frontend can force the change flow."""
    mock_user = _make_mock_user()
    mock_user.must_change_password = True
    mock_family = MagicMock()
    mock_family.family_id = uuid.uuid4()
    mock_membership = MagicMock()
    mock_membership.organisation_id = _ORG_ID
    mock_membership.role = "admin"
    with (
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=mock_user)),
        patch("modulo.api.routes.auth.authenticate_db_user", return_value=True),
        patch("modulo.api.routes.auth.update_last_login", new=AsyncMock()),
        patch("modulo.api.routes.auth.create_family", new=AsyncMock(return_value=mock_family)),
        patch("modulo.api.routes.auth.list_memberships_for_account", new=AsyncMock(return_value=[mock_membership])),
    ):
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": "testpass"},
        )
    assert resp.status_code == 200
    assert resp.json()["must_change_password"] is True


def test_login_wrong_password(client: TestClient) -> None:
    mock_user = _make_mock_user()
    with (
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=mock_user)),
        patch("modulo.api.routes.auth.authenticate_db_user", return_value=False),
    ):
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": "wrong"},
        )
    assert resp.status_code == 401


def test_login_wrong_password_when_rate_limiter_unavailable(client: TestClient) -> None:
    mock_user = _make_mock_user()
    with (
        patch("modulo.api.routes.auth.get_auth_rate_limiter", return_value=None),
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=mock_user)),
        patch("modulo.api.routes.auth.authenticate_db_user", return_value=False),
    ):
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": "wrong"},
        )
    assert resp.status_code == 401


def test_login_unknown_user(client: TestClient) -> None:
    with patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=None)):
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "testpass"},
        )
    assert resp.status_code == 401


def test_me_returns_username(client: TestClient) -> None:
    mock_user = _make_mock_user()
    mock_user.created_at = datetime.now(UTC)
    mock_family = MagicMock()
    mock_family.family_id = uuid.uuid4()
    mock_membership = MagicMock()
    mock_membership.organisation_id = _ORG_ID
    mock_membership.role = "admin"
    with (
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=mock_user)),
        patch("modulo.api.routes.auth.authenticate_db_user", return_value=True),
        patch("modulo.api.routes.auth.update_last_login", new=AsyncMock()),
        patch("modulo.api.routes.auth.create_family", new=AsyncMock(return_value=mock_family)),
        patch("modulo.api.routes.auth.list_memberships_for_account", new=AsyncMock(return_value=[mock_membership])),
    ):
        login_resp = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": "testpass"},
        )
    token = login_resp.json()["access_token"]

    with (
        patch("modulo.api.routes.auth.get_account_by_id", new=AsyncMock(return_value=mock_user)),
        patch("modulo.api.routes.auth.resolve_role_from_membership", new=AsyncMock(return_value="admin")),
    ):
        resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == "admin@example.com"
    assert resp.json()["must_change_password"] is False


def test_me_reports_must_change_password(client: TestClient) -> None:
    """FAR-460: /me carries the flag so restored sessions re-enter the
    forced-change gate without requiring a fresh login."""
    mock_user = _make_mock_user()
    mock_user.created_at = datetime.now(UTC)
    mock_user.must_change_password = True
    token = _login(client)

    with (
        patch("modulo.api.routes.auth.get_account_by_id", new=AsyncMock(return_value=mock_user)),
        patch("modulo.api.routes.auth.resolve_role_from_membership", new=AsyncMock(return_value="admin")),
    ):
        resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["must_change_password"] is True


def _login(client: TestClient) -> str:
    """Mint an access token for the default mock user (helper for /me tests)."""
    mock_family = MagicMock()
    mock_family.family_id = uuid.uuid4()
    mock_membership = MagicMock()
    mock_membership.organisation_id = _ORG_ID
    mock_membership.role = "admin"
    with (
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=_make_mock_user())),
        patch("modulo.api.routes.auth.authenticate_db_user", return_value=True),
        patch("modulo.api.routes.auth.update_last_login", new=AsyncMock()),
        patch("modulo.api.routes.auth.create_family", new=AsyncMock(return_value=mock_family)),
        patch("modulo.api.routes.auth.list_memberships_for_account", new=AsyncMock(return_value=[mock_membership])),
    ):
        login_resp = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": "testpass"},
        )
    return str(login_resp.json()["access_token"])


def test_me_removed_member_returns_401(client: TestClient) -> None:
    """ADR 017: a removed/deactivated member gets 401 from /me, not a stale role."""
    from datetime import UTC, datetime

    mock_user = _make_mock_user()
    mock_user.created_at = datetime.now(UTC)
    mock_family = MagicMock()
    mock_family.family_id = uuid.uuid4()
    mock_membership = MagicMock()
    mock_membership.organisation_id = _ORG_ID
    mock_membership.role = "admin"
    with (
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=mock_user)),
        patch("modulo.api.routes.auth.authenticate_db_user", return_value=True),
        patch("modulo.api.routes.auth.update_last_login", new=AsyncMock()),
        patch("modulo.api.routes.auth.create_family", new=AsyncMock(return_value=mock_family)),
        patch("modulo.api.routes.auth.list_memberships_for_account", new=AsyncMock(return_value=[mock_membership])),
    ):
        login_resp = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": "testpass"},
        )
    token = login_resp.json()["access_token"]

    with (
        patch("modulo.api.routes.auth.get_account_by_id", new=AsyncMock(return_value=mock_user)),
        patch("modulo.api.routes.auth.resolve_role_from_membership", new=AsyncMock(return_value=None)),
    ):
        resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


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
