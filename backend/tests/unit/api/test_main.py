"""Unit tests for main.py — app factory, health check, CORS wiring."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import _verify_db_connectivity, app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        cors_origins="http://example.com,http://test.com",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        user_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestHealthEndpoint:
    def test_health_returns_200(self, client: TestClient) -> None:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_mcp_health_returns_200(self, client: TestClient) -> None:
        resp = client.get("/mcp/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestCorsWiring:
    def test_cors_allows_default_origin(self, client: TestClient) -> None:
        """Default CORS origins (from CORS_ORIGINS env var) should be allowed."""
        resp = client.options(
            "/api/v1/pipelines",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"

    def test_cors_rejects_unlisted_origin(self, client: TestClient) -> None:
        """Origins not in CORS_ORIGINS should be rejected."""
        resp = client.options(
            "/api/v1/pipelines",
            headers={
                "Origin": "http://evil.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        origin = resp.headers.get("access-control-allow-origin", "")
        assert "evil.com" not in origin


class TestDbConnectivity:
    @pytest.mark.asyncio
    async def test_db_connectivity_passes(self) -> None:
        settings = _make_settings()
        engine = MagicMock()
        conn = AsyncMock()
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=None)
        engine.connect = MagicMock(return_value=conn)

        with patch(
            "modulo.api.main.get_or_create_engine", return_value=engine
        ) as mock_engine:
            await _verify_db_connectivity(settings)
            mock_engine.assert_called_once_with(settings)
            conn.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_db_connectivity_retries_then_errors(self) -> None:
        settings = _make_settings()
        engine = MagicMock()
        conn = AsyncMock()
        conn.__aenter__ = AsyncMock(side_effect=ConnectionError("db down"))
        conn.__aexit__ = AsyncMock(return_value=None)
        engine.connect = MagicMock(return_value=conn)

        with (
            patch(
                "modulo.api.main.get_or_create_engine", return_value=engine
            ) as mock_engine,
            patch("modulo.api.main.asyncio.sleep"),
        ):
            await _verify_db_connectivity(settings)
            mock_engine.assert_called_once_with(settings)
            assert conn.execute.await_count == 0
