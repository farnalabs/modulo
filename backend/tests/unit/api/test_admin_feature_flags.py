"""Unit tests for the admin feature-flags API endpoint."""

import uuid
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.routes.admin_feature_flags import _resolve_tier
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.feature_flags import FeatureFlagRegistry
from modulo.core.license import LicenseData, LicenseValidation
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_db_session] = lambda: MagicMock()
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id="00000000-0000-0000-0000-000000000001",
        account_id="00000000-0000-0000-0000-000000000002",
        org_role="admin",
        is_system_admin=True,
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _clear_registry_overrides() -> Generator[None, None, None]:
    """Clear the class-level FeatureFlagRegistry._overrides after each test.

    The toggle endpoint calls ``registry.set_override`` which mutates the
    shared class variable. Without cleanup it leaks into other test modules
    that construct a registry (e.g. tests/unit/core/test_plan_context.py) when
    they run in the same pytest process, making team-tier flags appear active
    on community tier.
    """
    yield
    FeatureFlagRegistry._overrides.clear()


def _mock_registry() -> FeatureFlagRegistry:
    """Return a FeatureFlagRegistry with hardcoded flags (no DB)."""
    return FeatureFlagRegistry(current_tier="community", has_license_key=False)


# ---------------------------------------------------------------------------
# GET /api/v1/admin/feature-flags
# ---------------------------------------------------------------------------


class TestListFeatureFlags:
    def test_returns_200(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            return_value=_mock_registry(),
        ):
            resp = client.get("/api/v1/admin/feature-flags")
        assert resp.status_code == 200

    def test_returns_license_block(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            return_value=_mock_registry(),
        ):
            resp = client.get("/api/v1/admin/feature-flags")
        body = resp.json()
        assert "license" in body
        assert body["license"]["tier"] in ("community", "team")
        assert "has_license_key" in body["license"]
        assert body["license"]["is_valid"] is True

    def test_returns_flags_list(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            return_value=_mock_registry(),
        ):
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
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            return_value=_mock_registry(),
        ):
            resp = client.get("/api/v1/admin/feature-flags")
        body = resp.json()
        assert "would_activate" in body

    def test_community_tier_has_team_flags_in_would_activate(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            return_value=_mock_registry(),
        ):
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
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            return_value=_mock_registry(),
        ):
            resp = client.get("/api/v1/admin/feature-flags/sso")
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "sso"
        assert body["tier"] == "team"

    def test_returns_404_for_unknown_flag(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            return_value=_mock_registry(),
        ):
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
# ProgrammingError -> 501
# ---------------------------------------------------------------------------


class TestProgrammingError:
    def test_list_returns_501_on_programming_error(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            side_effect=ProgrammingError("mock", "mock", "mock"),
        ):
            resp = client.get("/api/v1/admin/feature-flags")
        assert resp.status_code == 501
        body = resp.json()
        assert body["error"]["code"] == "NOT_IMPLEMENTED"

    def test_get_returns_501_on_programming_error(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            side_effect=ProgrammingError("mock", "mock", "mock"),
        ):
            resp = client.get("/api/v1/admin/feature-flags/sso")
        assert resp.status_code == 501
        body = resp.json()
        assert body["error"]["code"] == "NOT_IMPLEMENTED"

    def test_toggle_returns_501_on_programming_error(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            side_effect=ProgrammingError("mock", "mock", "mock"),
        ):
            resp = client.put("/api/v1/admin/feature-flags/sso", json={"enabled": True})
        assert resp.status_code == 501
        body = resp.json()
        assert body["error"]["code"] == "NOT_IMPLEMENTED"


# ---------------------------------------------------------------------------
# PUT /api/v1/admin/feature-flags/{flag_name} — toggle
# ---------------------------------------------------------------------------


class TestToggleFeatureFlag:
    def test_toggle_known_flag_returns_200(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            return_value=_mock_registry(),
        ):
            resp = client.put("/api/v1/admin/feature-flags/sso", json={"enabled": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "sso"
        assert "overridden" in body

    def test_toggle_unknown_flag_returns_404(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            return_value=_mock_registry(),
        ):
            resp = client.put(
                "/api/v1/admin/feature-flags/nonexistent",
                json={"enabled": True},
            )
        assert resp.status_code == 404
        body = resp.json()
        assert "detail" in body

    def test_toggle_unauthenticated_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.put(
            "/api/v1/admin/feature-flags/sso",
            json={"enabled": True},
        )
        assert resp.status_code in (401, 403)

    def test_toggle_error_returns_500(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_feature_flags._build_registry",
            side_effect=RuntimeError("boom"),
        ):
            resp = client.put(
                "/api/v1/admin/feature-flags/sso",
                json={"enabled": True},
            )
        assert resp.status_code == 500
        body = resp.json()
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
        assert parsed["detail"] == "An unexpected error occurred"
        assert parsed["type"] == "urn:problem:modulo:internal_error"
        assert parsed["status"] == 500


# ---------------------------------------------------------------------------
# _resolve_tier — license gating for the frontend tier path
# ---------------------------------------------------------------------------
# Regression for the PR #854 review finding: _resolve_tier duplicated the
# 4-step license resolution and returned the bare org.plan_id for team/pro
# with no license check. It now delegates to resolve_plan_context so a bare
# non-community plan_id with no valid signed license resolves to community —
# closing the UI bypass where FeatureGate.vue / featureEnabled() would have
# shown paid features for an unlicensed org.


def _license_data(tier: str = "team", features: list[str] | None = None) -> LicenseData:
    return LicenseData(
        tier=tier,
        features=features or ["sso"],
        expires_at="",
        org_id="test-org",
        raw_payload={},
        raw_key="",
    )


def _valid_validation(tier: str = "team") -> LicenseValidation:
    return LicenseValidation(valid=True, license_data=_license_data(tier))


def _invalid_validation() -> LicenseValidation:
    return LicenseValidation(valid=False, error="bad key")


def _principal(org_id: uuid.UUID | None = None) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="testuser",
        organisation_id=org_id or uuid.uuid4(),
        account_id=uuid.uuid4(),
        org_role="admin",
    )


def _session() -> AsyncMock:
    session = AsyncMock()
    session.begin = MagicMock(return_value=AsyncMock())
    return session


def _settings(license_key: str = "") -> MagicMock:
    settings = MagicMock()
    settings.modulo_license_key = license_key
    return settings


def _org(plan_id: str, settings_json: dict | None = None) -> MagicMock:
    org = MagicMock()
    org.settings_json = settings_json if settings_json is not None else {}
    org.plan_id = plan_id
    return org


async def _resolve_tier_value(org: MagicMock, *, license_key: str = "") -> str:
    """Run _resolve_tier with no org/system license present by default."""
    session = _session()
    with (
        patch("modulo.api.routes.admin_feature_flags.get_organisation", new_callable=AsyncMock, return_value=org),
        patch("modulo.db.crud.tier_catalog.list_tiers", new_callable=AsyncMock, return_value=[]),
        patch("modulo.db.crud.tier_catalog.list_feature_flags", new_callable=AsyncMock, return_value=[]),
        patch("modulo.core.license.get_license", return_value=None),
        patch("modulo.core.license.parse_and_verify", return_value=_invalid_validation()),
    ):
        return await _resolve_tier(_settings(license_key), session, _principal())


class TestResolveTierLicensing:
    async def test_team_plan_id_without_license_returns_community(self) -> None:
        """Bare plan_id='team' with NO license must NOT return team tier.

        The old _resolve_tier returned org.plan_id directly (step 4), so the
        UI plan store showed team features for an unlicensed org.
        """
        assert await _resolve_tier_value(_org("team")) == "community"

    async def test_custom_plan_id_without_license_returns_community(self) -> None:
        assert await _resolve_tier_value(_org("custom-plan")) == "community"

    async def test_team_plan_id_with_env_license_returns_team(self) -> None:
        org = _org("team")
        session = _session()
        with (
            patch("modulo.api.routes.admin_feature_flags.get_organisation", new_callable=AsyncMock, return_value=org),
            patch("modulo.db.crud.tier_catalog.list_tiers", new_callable=AsyncMock, return_value=[]),
            patch("modulo.db.crud.tier_catalog.list_feature_flags", new_callable=AsyncMock, return_value=[]),
            patch("modulo.core.license.get_license", return_value=None),
            patch("modulo.core.license.parse_and_verify", return_value=_valid_validation(tier="team")),
        ):
            tier = await _resolve_tier(_settings("env-key"), session, _principal())
        assert tier == "team"

    async def test_org_license_key_wins_over_plan_id(self) -> None:
        org = _org("team", settings_json={"license_key": "org-key"})
        session = _session()
        with (
            patch("modulo.api.routes.admin_feature_flags.get_organisation", new_callable=AsyncMock, return_value=org),
            patch("modulo.db.crud.tier_catalog.list_tiers", new_callable=AsyncMock, return_value=[]),
            patch("modulo.db.crud.tier_catalog.list_feature_flags", new_callable=AsyncMock, return_value=[]),
            patch("modulo.core.license.parse_and_verify", return_value=_valid_validation(tier="team")),
        ):
            tier = await _resolve_tier(_settings(), session, _principal())
        assert tier == "team"

    async def test_community_plan_id_returns_community(self) -> None:
        assert await _resolve_tier_value(_org("community")) == "community"
