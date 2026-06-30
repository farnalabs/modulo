"""Unit tests for ws-token endpoint and create_ws_token utility."""

import os
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import (
    AuthenticatedPrincipal,
    create_access_token,
    create_ws_token,
    decode_principal,
)
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32


@pytest.fixture(autouse=True)
def _set_env() -> Generator[None, None, None]:
    """Set required env vars for middleware that calls get_settings() directly."""
    old = {k: os.environ.pop(k, None) for k in ("DATABASE_URL", "SECRET_KEY", "FERNET_KEY")}
    os.environ["DATABASE_URL"] = "postgresql+asyncpg://localhost/test"
    os.environ["SECRET_KEY"] = _VALID_32
    os.environ["FERNET_KEY"] = _VALID_32
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


_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


# ---------------------------------------------------------------------------
# create_ws_token
# ---------------------------------------------------------------------------


def test_create_ws_token_is_short_lived():
    settings = _make_settings()
    token = create_ws_token(
        "testuser",
        settings.secret_key,
        organisation_id=str(_ORG_ID),
        account_id=str(_USER_ID),
        org_role="admin",
    )
    principal = decode_principal(token, settings.secret_key)
    assert principal.username == "testuser"
    assert principal.organisation_id == _ORG_ID


def test_create_ws_token_expires_quickly():
    settings = _make_settings()
    token = create_ws_token(
        "u",
        settings.secret_key,
        organisation_id=str(_ORG_ID),
        account_id=str(_USER_ID),
        org_role="admin",
    )
    from jose import jwt

    payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    exp = datetime.fromtimestamp(payload["exp"], tz=UTC)
    iat = datetime.fromtimestamp(payload["iat"], tz=UTC)
    ttl = exp - iat
    assert ttl <= timedelta(minutes=15)
    assert ttl >= timedelta(minutes=14)


def test_create_ws_token_has_purpose_claim():
    settings = _make_settings()
    token = create_ws_token(
        "u",
        settings.secret_key,
        organisation_id=str(_ORG_ID),
        account_id=str(_USER_ID),
        org_role="admin",
    )
    from jose import jwt

    payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    assert payload.get("purpose") == "ws"


def test_create_ws_token_carries_identity():
    settings = _make_settings()
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    token = create_ws_token(
        "alice",
        settings.secret_key,
        organisation_id=str(org_id),
        account_id=str(user_id),
        org_role="operator",
    )
    principal = decode_principal(token, settings.secret_key)
    assert principal.username == "alice"
    assert principal.organisation_id == org_id
    assert principal.account_id == user_id
    assert principal.org_role == "operator"


def test_access_token_not_accepted_as_ws_token():
    """Regular access tokens lack purpose claim and must be rejected for WS use."""
    settings = _make_settings()
    token = create_access_token(
        "testuser",
        settings.secret_key,
        organisation_id=str(_ORG_ID),
        account_id=str(_USER_ID),
        org_role="admin",
    )
    with pytest.raises(JWTError, match="purpose"):
        decode_principal(token, settings.secret_key, allowed_purposes=["ws"])


def test_ws_token_rejected_with_wrong_key():
    settings = _make_settings()
    token = create_ws_token(
        "testuser",
        settings.secret_key,
        organisation_id=str(_ORG_ID),
        account_id=str(_USER_ID),
        org_role="admin",
    )
    with pytest.raises(JWTError):
        decode_principal(token, "b" * 32)


# ---------------------------------------------------------------------------
# POST /api/v1/auth/ws-token endpoint
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture()
def client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:

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

    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_ws_token_endpoint_returns_200(client: TestClient) -> None:
    resp = client.post("/api/v1/auth/ws-token")
    assert resp.status_code == 200
    body = resp.json()
    assert "ws_token" in body
    assert len(body["ws_token"]) > 20
    assert body["token_type"] == "ws-jwt"
    assert body["expires_in_seconds"] == 60


def test_ws_token_decodes_correctly(client: TestClient) -> None:
    resp = client.post("/api/v1/auth/ws-token")
    token = resp.json()["ws_token"]
    settings = _make_settings()
    principal = decode_principal(token, settings.secret_key, allowed_purposes=["ws"])
    assert principal.username == "testuser"
    assert principal.organisation_id == _ORG_ID
    assert principal.account_id == _USER_ID


def test_ws_token_endpoint_unauthenticated_returns_4xx() -> None:
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides.pop(get_current_user, None)  # remove override
    try:
        resp = TestClient(app).post("/api/v1/auth/ws-token")
        assert resp.status_code in (401, 403)
    finally:
        app.dependency_overrides.clear()
