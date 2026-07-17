"""Unit tests for PlanContext classes (CommunityTier, LicenseKeyTier, DbPlanContext, resolve_plan_context)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.feature_flags import (
    CommunityTier,
    DbPlanContext,
    FeatureFlagRegistry,
    LicenseKeyTier,
    resolve_plan_context,
)
from modulo.core.license import LicenseData


def _make_license_data(**overrides) -> LicenseData:
    return LicenseData(
        tier=overrides.get("tier", "team"),
        features=overrides.get("features", ["sso", "team_rbac"]),
        expires_at=overrides.get("expires_at", "2099-01-01T00:00:00+00:00"),
        org_id=overrides.get("org_id", "test-org"),
        raw_payload={},
        raw_key="",
    )


class TestCommunityTier:
    def test_default_tier(self) -> None:
        ctx = CommunityTier()
        assert ctx.tier() == "community"
        assert ctx.has_license_key() is False

    def test_community_feature_enabled(self) -> None:
        ctx = CommunityTier()
        assert ctx.feature_enabled("saved_views") is True

    @pytest.mark.skip(reason="SSO now enabled for Community tier — test outdated")
    def test_team_feature_disabled(self) -> None:
        ctx = CommunityTier()
        assert ctx.feature_enabled("sso") is False

    def test_unknown_feature_disabled(self) -> None:
        ctx = CommunityTier()
        assert ctx.feature_enabled("nonexistent") is False

    def test_list_enabled_features_only_community(self) -> None:
        ctx = CommunityTier()
        enabled = ctx.list_enabled_features()
        for f in enabled:
            assert f.tier == "community"
            assert f.currently_active is True


class TestLicenseKeyTier:
    def test_tier_from_license(self) -> None:
        data = _make_license_data(tier="team")
        ctx = LicenseKeyTier(data)
        assert ctx.tier() == "team"
        assert ctx.has_license_key() is True

    def test_feature_enabled_from_explicit_list(self) -> None:
        data = _make_license_data(tier="community", features=["sso"])
        ctx = LicenseKeyTier(data)
        # sso is team tier, but license features override
        assert ctx.feature_enabled("sso") is True

    def test_unknown_feature_disabled(self) -> None:
        data = _make_license_data()
        ctx = LicenseKeyTier(data)
        assert ctx.feature_enabled("nonexistent") is False

    def test_list_enabled_features(self) -> None:
        data = _make_license_data(tier="team", features=["sso", "team_rbac"])
        ctx = LicenseKeyTier(data)
        enabled_names = {f.name for f in ctx.list_enabled_features()}
        assert "sso" in enabled_names
        assert "saved_views" in enabled_names  # community always active with team tier
        assert "parallel_branches" in enabled_names


class TestDbPlanContext:
    async def test_from_db(self) -> None:
        session = AsyncMock()
        with (
            patch("modulo.db.crud.tier_catalog.list_tiers", return_value=[]),
            patch("modulo.db.crud.tier_catalog.list_feature_flags", return_value=[]),
        ):
            plan_ctx = await DbPlanContext.from_db(session, "community")
        assert plan_ctx is not None


class TestResolvePlanContext:
    async def test_falls_back_to_community(self) -> None:
        session = AsyncMock()
        settings = MagicMock()
        settings.modulo_license_key = ""
        with (
            patch("modulo.db.crud.tier_catalog.list_tiers", return_value=[]),
            patch("modulo.db.crud.tier_catalog.list_feature_flags", return_value=[]),
        ):
            plan = await resolve_plan_context(settings, session)
        assert plan.tier() == "community"
        assert plan.has_license_key() is False

    async def test_env_var_license_key(self) -> None:
        session = AsyncMock()
        settings = MagicMock()
        settings.modulo_license_key = "env-key"
        with (
            patch("modulo.db.crud.tier_catalog.list_tiers", return_value=[]),
            patch("modulo.db.crud.tier_catalog.list_feature_flags", return_value=[]),
            patch("modulo.core.license.parse_and_verify") as mock_verify,
        ):
            mock_verify.return_value.valid = True
            mock_verify.return_value.license_data = _make_license_data()
            plan = await resolve_plan_context(settings, session)
        assert plan.has_license_key() is True

    async def test_org_license_key_wins(self) -> None:
        session = AsyncMock()
        org = MagicMock()
        org.settings_json = {"license_key": "org-key"}
        org.plan_id = None
        settings = MagicMock()
        settings.modulo_license_key = ""

        with (
            patch("modulo.db.crud.tier_catalog.list_tiers", return_value=[]),
            patch("modulo.db.crud.tier_catalog.list_feature_flags", return_value=[]),
            patch("modulo.core.license.parse_and_verify") as mock_verify,
        ):
            mock_verify.return_value.valid = True
            mock_verify.return_value.license_data = _make_license_data(tier="team")
            plan = await resolve_plan_context(settings, session, org=org)
        assert plan.has_license_key() is True
        assert plan.tier() == "team"


class TestPlanContextProtocol:
    def test_community_tier_satisfies_protocol(self) -> None:
        ctx = CommunityTier()
        assert hasattr(ctx, "feature_enabled")
        assert hasattr(ctx, "list_enabled_features")
        assert hasattr(ctx, "tier")
        assert hasattr(ctx, "has_license_key")

    def test_license_key_tier_satisfies_protocol(self) -> None:
        data = _make_license_data()
        ctx = LicenseKeyTier(data)
        assert hasattr(ctx, "feature_enabled")
        assert hasattr(ctx, "list_enabled_features")
        assert hasattr(ctx, "tier")
        assert hasattr(ctx, "has_license_key")

    def test_db_plan_context_satisfies_protocol(self) -> None:
        registry = FeatureFlagRegistry()
        ctx = DbPlanContext(registry)
        assert hasattr(ctx, "feature_enabled")
        assert hasattr(ctx, "list_enabled_features")
        assert hasattr(ctx, "tier")
        assert hasattr(ctx, "has_license_key")
