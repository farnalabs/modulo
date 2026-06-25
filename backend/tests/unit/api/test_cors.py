"""Unit tests for CORS middleware configuration.

Tests cover preflight handling, header presence, method restrictions,
credentials behavior, cache max-age, and normal request CORS headers.
"""

from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal

_ALLOWED_ORIGIN = "http://localhost:5173"
_DISALLOWED_ORIGIN = "http://evil.com"
_VALID_32 = "a" * 32


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

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=MagicMock(),
        user_id=MagicMock(),
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestCorsPreflight:
    def test_preflight_allowed_origin_returns_200(self, client: TestClient) -> None:
        resp = client.options(
            "/api/v1/pipelines",
            headers={
                "Origin": _ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == _ALLOWED_ORIGIN

    def test_preflight_disallowed_origin_missing_acao(self, client: TestClient) -> None:
        resp = client.options(
            "/api/v1/pipelines",
            headers={
                "Origin": _DISALLOWED_ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.status_code == 200
        acao = resp.headers.get("access-control-allow-origin", "")
        assert _DISALLOWED_ORIGIN not in acao

    def test_preflight_methods_not_wildcard(self, client: TestClient) -> None:
        resp = client.options(
            "/api/v1/pipelines",
            headers={
                "Origin": _ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )
        acam = resp.headers.get("access-control-allow-methods", "")
        assert acam != "*"
        assert "GET" in acam
        assert "POST" in acam

    def test_preflight_credentials_for_allowed_origin(self, client: TestClient) -> None:
        resp = client.options(
            "/api/v1/pipelines",
            headers={
                "Origin": _ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.headers.get("access-control-allow-credentials") == "true"

    def test_preflight_disallowed_origin_no_credentials(self, client: TestClient) -> None:
        resp = client.options(
            "/api/v1/pipelines",
            headers={
                "Origin": _DISALLOWED_ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.headers.get("access-control-allow-credentials") is None

    def test_preflight_max_age_matches_config(self, client: TestClient) -> None:
        resp = client.options(
            "/api/v1/pipelines",
            headers={
                "Origin": _ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.headers.get("access-control-max-age") == "600"


class TestCorsNormalRequests:
    def test_get_request_from_allowed_origin_has_acao(self, client: TestClient) -> None:
        resp = client.get(
            "/healthz",
            headers={"Origin": _ALLOWED_ORIGIN},
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == _ALLOWED_ORIGIN

    def test_post_request_from_allowed_origin_has_acao(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/pipelines",
            json={"name": "test"},
            headers={"Origin": _ALLOWED_ORIGIN},
        )
        assert resp.headers.get("access-control-allow-origin") == _ALLOWED_ORIGIN
