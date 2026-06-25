"""Login endpoint and /me tests via FastAPI TestClient."""

import os
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from modulo.api.main import app
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32


def _override(admin_password: str = "testpass") -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password=admin_password,
    )


@pytest.fixture(autouse=True)
def _set_env() -> Generator[None, None, None]:
    """Set required env vars for middleware that calls get_settings() directly."""
    old = {
        k: os.environ.pop(k, None)
        for k in ("DATABASE_URL", "SECRET_KEY", "FERNET_KEY")
    }
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
def client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# /api/v1/auth/login
# ---------------------------------------------------------------------------


def test_login_success(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "testpass"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"
    assert len(body["access_token"]) > 0


def test_login_wrong_password(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "wrong"},
    )
    assert resp.status_code == 401


def test_login_unknown_user(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": "nobody", "password": "testpass"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /api/v1/auth/me
# ---------------------------------------------------------------------------


def test_me_returns_username(client: TestClient) -> None:
    token = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "testpass"},
    ).json()["access_token"]

    resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["username"] == "admin"


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
