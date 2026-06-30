"""Unit tests for the admin feature-flags API endpoint."""

from collections.abc import Generator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id="00000000-0000-0000-0000-000000000001",
        account_id="00000000-0000-0000-0000-000000000002",
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /api/v1/admin/feature-flags
# ---------------------------------------------------------------------------


class TestListFeatureFlags:
    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get("/api/v1/admin/feature-flags")
        assert resp.status_code == 200

    def test_returns_license_block(self, client: TestClient) -> None:
        resp = client.get("/api/v1/admin/feature-flags")
        body = resp.json()
        assert "license" in body
        assert body["license"]["tier"] in ("community", "team")
        assert "has_license_key" in body["license"]
        assert body["license"]["is_valid"] is True

    def test_returns_flags_list(self, client: TestClient) -> None:
        resp = client.get("/api/v1/admin/feature-flags")
        body = resp.json()
        assert "flags" in body
        assert len(body["flags"]) > 0
        for flag in body["flags"]:
            assert "name" in flag
            assert "description" in flag
            assert "tier" in flag
            assert "currently_active" in flag

    def test_returns_would_activate(self, client: TestClient) -> None:
        resp = client.get("/api/v1/admin/feature-flags")
        body = resp.json()
        assert "would_activate" in body

    def test_community_tier_has_team_flags_in_would_activate(self, client: TestClient) -> None:
        resp = client.get("/api/v1/admin/feature-flags")
        body = resp.json()
        if body["license"]["tier"] == "community":
            assert len(body["would_activate"]) > 0
            for flag in body["would_activate"]:
                assert flag["tier"] != "community"

    def test_unauthenticated_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get("/api/v1/admin/feature-flags")
        assert resp.status_code in (401, 403)

    def test_error_returns_500(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            side_effect=RuntimeError("boom"),
        ):
            resp = client.get("/api/v1/admin/feature-flags")
        assert resp.status_code == 500
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# GET /api/v1/admin/feature-flags/{flag_name}
# ---------------------------------------------------------------------------


class TestGetFeatureFlag:
    def test_returns_200_for_known_flag(self, client: TestClient) -> None:
        resp = client.get("/api/v1/admin/feature-flags/sso")
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "sso"
        assert body["tier"] == "team"

    def test_returns_404_for_unknown_flag(self, client: TestClient) -> None:
        resp = client.get("/api/v1/admin/feature-flags/nonexistent_flag")
        assert resp.status_code == 404
        body = resp.json()
        assert "detail" in body

    def test_unauthenticated_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get("/api/v1/admin/feature-flags/sso")
        assert resp.status_code in (401, 403)

    def test_error_returns_500(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            side_effect=RuntimeError("boom"),
        ):
            resp = client.get("/api/v1/admin/feature-flags/sso")
        assert resp.status_code == 500
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# Middleware-level error handling
# ---------------------------------------------------------------------------


class TestCatchAllMiddlewareFallback:
    def test_plain_json_on_serialization_failure(self) -> None:
        from modulo.api.middleware.catch_all import _make_500_response

        resp = _make_500_response(None)
        assert resp.status_code == 500
        body = resp.body
        import json
        parsed = json.loads(body)
        assert parsed["error"]["code"] == "INTERNAL_ERROR"
        assert parsed["error"]["message"] == "An unexpected error occurred"
