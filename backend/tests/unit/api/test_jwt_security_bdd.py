"""Unit tests for JWT Security (PRD §7.10) — access tokens, refresh tokens, token family invalidation.

Exercises the same scenarios as jwt_security.feature via FastAPI TestClient
with mocked DB dependencies.
"""

import base64
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from jose import jwt as jose_jwt
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.passwords import hash_password
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _override() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_csrf_enabled=False,
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
def _set_env() -> None:
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
def client(mock_session: AsyncMock) -> TestClient:
    async def override_session():
        yield mock_session

    app.dependency_overrides[get_settings] = _override
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()

    yield TestClient(app)

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Scenario 1: Login returns access + refresh tokens
# ---------------------------------------------------------------------------


def test_login_returns_token_pair(client: TestClient) -> None:
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
    assert isinstance(body["access_token"], str)
    assert len(body["access_token"]) > 0
    assert "refresh_token" in body
    assert isinstance(body["refresh_token"], str)
    assert len(body["refresh_token"]) > 0
    assert body["token_type"] == "bearer"


# ---------------------------------------------------------------------------
# Scenario 2: Access token grants access to /me
# ---------------------------------------------------------------------------


def test_access_token_grants_me_access(client: TestClient) -> None:
    mock_user = _make_mock_user()
    mock_user.created_at = datetime.now(UTC)

    token = _create_valid_access_token()
    with patch("modulo.api.routes.auth.get_user_by_id", new=AsyncMock(return_value=mock_user)):
        resp = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "admin@example.com"
    assert body["display_name"] == "Admin User"
    assert body["org_role"] == "admin"


# ---------------------------------------------------------------------------
# Scenario 3: Expired access token rejected (401)
# ---------------------------------------------------------------------------


def test_expired_token_rejected(client: TestClient) -> None:
    token = _create_expired_token()
    resp = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Scenario 4: Refresh token rotates the token pair
# ---------------------------------------------------------------------------


def test_refresh_rotates_token_pair(client: TestClient) -> None:
    _, refresh_token = _login_and_get_tokens(client)

    with patch("modulo.api.routes.auth.advance_sequence", new=AsyncMock(return_value=(1, False))):
        resp = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_token},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert "refresh_token" in body
    assert body["token_type"] == "bearer"
    assert body["access_token"] != refresh_token
    assert body["refresh_token"] != refresh_token


# ---------------------------------------------------------------------------
# Scenario 5: Refresh token single-use — theft detected on reuse
# ---------------------------------------------------------------------------


def test_refresh_token_reuse_detects_theft(client: TestClient) -> None:
    from modulo.auth.jwt import create_refresh_token

    family_id = str(uuid.uuid4())
    refresh_token = create_refresh_token(
        "alice",
        _VALID_32,
        organisation_id=str(_ORG_ID),
        account_id=str(_USER_ID),
        org_role="admin",
        token_family=family_id,
        token_sequence=0,
    )

    with patch("modulo.api.routes.auth.advance_sequence", new=AsyncMock(return_value=(1, False))):
        resp1 = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_token},
        )
    assert resp1.status_code == 200

    with patch("modulo.api.routes.auth.advance_sequence", new=AsyncMock(return_value=(1, True))):
        resp2 = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_token},
        )
    assert resp2.status_code == 401
    detail = resp2.json().get("detail", "")
    assert "theft" in detail.lower() or "revoked" in detail.lower()


# ---------------------------------------------------------------------------
# Scenario 6: Logout blacklists the token family
# ---------------------------------------------------------------------------


def test_logout_blacklists_family(client: TestClient) -> None:
    _, refresh_token = _login_and_get_tokens(client)

    blacklist_mock = AsyncMock(return_value=True)
    with patch("modulo.api.routes.auth.blacklist_family", new=blacklist_mock):
        resp = client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": refresh_token},
        )
    assert resp.status_code == 200
    assert resp.json()["detail"] == "Logged out"
    blacklist_mock.assert_awaited_once()


def test_refresh_rejected_after_logout(client: TestClient) -> None:
    _, refresh_token = _login_and_get_tokens(client)

    with patch("modulo.api.routes.auth.blacklist_family", new=AsyncMock(return_value=True)):
        client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})

    with patch("modulo.api.routes.auth.advance_sequence", new=AsyncMock(return_value=(0, True))):
        resp = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_token},
        )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Scenario 7: Invalid signature rejected (401)
# ---------------------------------------------------------------------------


def test_tampered_token_rejected(client: TestClient) -> None:
    token = _create_valid_access_token()
    parts = token.split(".")
    parts[2] = "tampered"
    tampered = ".".join(parts)

    resp = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {tampered}"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Scenario 8: Wrong algorithm (alg=none) rejected
# ---------------------------------------------------------------------------


def test_alg_none_token_rejected(client: TestClient) -> None:
    now = datetime.now(UTC)
    payload = {
        "sub": "alice",
        "org_id": str(_ORG_ID),
        "user_id": str(_USER_ID),
        "org_role": "admin",
        "iat": int(now.timestamp()) - 300,
        "exp": int(now.timestamp()) + 3600,
    }
    import json

    header_b64 = _b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload_b64 = _b64(json.dumps(payload).encode())
    token = f"{header_b64}.{payload_b64}."

    resp = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_valid_access_token() -> str:
    from modulo.auth.jwt import create_access_token

    return create_access_token(
        "admin@example.com",
        _VALID_32,
        organisation_id=str(_ORG_ID),
        account_id=str(_USER_ID),
        org_role="admin",
    )


def _create_expired_token() -> str:
    import time

    past = int(time.time()) - 3600
    payload = {
        "sub": "admin@example.com",
        "org_id": str(_ORG_ID),
        "user_id": str(_USER_ID),
        "org_role": "admin",
        "iat": past - 86400,
        "exp": past,
    }
    return str(jose_jwt.encode(payload, _VALID_32, algorithm="HS256"))


def _login_and_get_tokens(client: TestClient) -> tuple[str, str]:
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
    return body["access_token"], body["refresh_token"]
