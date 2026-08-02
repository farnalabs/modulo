"""Unit tests for plan-context resolution and feature-flag override paths.

Covers previously-untested code paths in ``modulo.core.feature_flags``:
  - ``CommunityTier`` / ``LicenseKeyTier`` / ``DbPlanContext`` plan-context classes
  - ``resolve_plan_context`` resolution order and failure fall-through
  - ``FeatureFlagRegistry.resolve_flag`` override precedence
  - org/team/user override DB lookups (``_get_org_override`` etc.)
  - ``get_registry`` caching and ``get_plan_for_org``
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.feature_flags import (
    CommunityTier,
    DbPlanContext,
    FeatureFlagRegistry,
    LicenseKeyTier,
    get_plan_for_org,
    get_registry,
    resolve_plan_context,
)
from modulo.core.license import LicenseData, LicenseValidation


def _license(tier: str = "team", features: list[str] | None = None) -> LicenseData:
    return LicenseData(
        tier=tier,
        features=features or [],
        expires_at="",
        org_id="org-1",
        raw_payload={},
        raw_key="k",
    )


def _valid_validation(tier: str = "team", features: list[str] | None = None) -> LicenseValidation:
    return LicenseValidation(valid=True, license_data=_license(tier, features))


def _invalid_validation() -> LicenseValidation:
    return LicenseValidation(valid=False, error="bad key")


class TestCommunityTier:
    def test_feature_enabled_community_flag(self) -> None:
        tier = CommunityTier()
        assert tier.feature_enabled("saved_views") is True

    def test_feature_enabled_team_flag_is_off(self) -> None:
        tier = CommunityTier()
        assert tier.feature_enabled("sso") is False

    def test_feature_enabled_unknown_flag_is_false(self) -> None:
        tier = CommunityTier()
        assert tier.feature_enabled("nonexistent") is False

    def test_list_enabled_features_only_returns_active(self) -> None:
        tier = CommunityTier()
        names = {f.name for f in tier.list_enabled_features()}
        assert "saved_views" in names
        assert "sso" not in names

    def test_tier_and_license_flag(self) -> None:
        tier = CommunityTier()
        assert tier.tier() == "community"
        assert tier.has_license_key() is False


class TestLicenseKeyTier:
    def test_license_feature_is_enabled(self) -> None:
        tier = LicenseKeyTier(_license(tier="community", features=["sso"]))
        assert tier.feature_enabled("sso") is True

    def test_community_flag_still_enabled(self) -> None:
        tier = LicenseKeyTier(_license(features=[]))
        assert tier.feature_enabled("saved_views") is True

    def test_team_flag_without_license_feature_stays_off(self) -> None:
        tier = LicenseKeyTier(_license(tier="community", features=[]))
        assert tier.feature_enabled("sso") is False

    def test_unknown_flag_is_false(self) -> None:
        tier = LicenseKeyTier(_license(features=[]))
        assert tier.feature_enabled("nonexistent") is False

    def test_list_enabled_merges_tier_and_license_features(self) -> None:
        tier = LicenseKeyTier(_license(tier="community", features=["sso"]))
        names = {f.name for f in tier.list_enabled_features()}
        assert "saved_views" in names
        assert "sso" in names

    def test_tier_and_license_flag(self) -> None:
        tier = LicenseKeyTier(_license(tier="team"))
        assert tier.tier() == "team"
        assert tier.has_license_key() is True


class TestDbPlanContext:
    async def _context(
        self,
        *,
        plan_id: str = "community",
        has_license_key: bool = False,
        license_features: set[str] | None = None,
    ) -> DbPlanContext:
        session = AsyncMock()
        with (
            patch("modulo.db.crud.tier_catalog.list_tiers", new_callable=AsyncMock, return_value=[]),
            patch("modulo.db.crud.tier_catalog.list_feature_flags", new_callable=AsyncMock, return_value=[]),
        ):
            return await DbPlanContext.from_db(session, plan_id, has_license_key, license_features)

    async def test_from_db_activates_license_features(self) -> None:
        ctx = await self._context(plan_id="community", has_license_key=True, license_features={"sso"})
        assert ctx.tier() == "community"
        assert ctx.has_license_key() is True
        assert ctx.feature_enabled("sso") is True
        assert ctx.feature_enabled("saved_views") is True
        names = {f.name for f in ctx.list_enabled_features()}
        assert "sso" in names
        assert "saved_views" in names

    async def test_from_db_without_license_features(self) -> None:
        ctx = await self._context(plan_id="community", has_license_key=False)
        assert ctx.feature_enabled("sso") is False
        assert ctx.feature_enabled("saved_views") is True

    async def test_unknown_flag_is_false(self) -> None:
        ctx = await self._context(plan_id="team", has_license_key=True)
        assert ctx.feature_enabled("nonexistent") is False

    async def test_team_plan_activates_team_flags(self) -> None:
        ctx = await self._context(plan_id="team", has_license_key=True)
        assert ctx.feature_enabled("sso") is True
        names = {f.name for f in ctx.list_enabled_features()}
        assert "sso" in names


class TestResolvePlanContext:
    async def _resolve(
        self,
        *,
        org: object | None = None,
        system_license: LicenseData | None = None,
        settings_license_key: str = "",
        license_validation: LicenseValidation = _invalid_validation(),
        side_effect: Exception | None = None,
    ) -> tuple[object, AsyncMock, AsyncMock]:
        """Run resolve_plan_context with stub dependencies.

        Returns (result, from_db_mock, session) so callers can assert the exact
        DbPlanContext.from_db call made for the resolved plan.
        """
        session = AsyncMock()
        settings = MagicMock()
        settings.modulo_license_key = settings_license_key
        db_ctx = object()

        def _parse_and_verify(_key: str) -> LicenseValidation:
            if side_effect is not None:
                raise side_effect
            return license_validation

        with (
            patch(
                "modulo.core.feature_flags.DbPlanContext.from_db",
                new_callable=AsyncMock,
                return_value=db_ctx,
            ) as from_db,
            patch("modulo.core.license.get_license", return_value=system_license),
            patch("modulo.core.license.parse_and_verify", side_effect=_parse_and_verify),
        ):
            result = await resolve_plan_context(settings, session, org)
        return result, from_db, session

    async def test_org_license_key_wins(self) -> None:
        org = MagicMock()
        org.settings_json = {"license_key": "org-key"}
        org.plan_id = "fallback-plan"
        result, from_db, session = await self._resolve(
            org=org,
            license_validation=_valid_validation(tier="team", features=["sso"]),
        )
        assert result is not None
        from_db.assert_awaited_once_with(session, "team", has_license_key=True, license_features={"sso"})

    async def test_org_license_invalid_falls_through_to_system_license(self) -> None:
        org = MagicMock()
        org.settings_json = {"license_key": "bad-org-key"}
        result, from_db, session = await self._resolve(
            org=org,
            license_validation=_invalid_validation(),
            system_license=_license(tier="team", features=[]),
        )
        assert result is not None
        from_db.assert_awaited_once_with(session, "team", has_license_key=True, license_features=set())

    async def test_org_license_parse_error_falls_through(self) -> None:
        org = MagicMock()
        org.settings_json = {"license_key": "boom"}
        result, from_db, session = await self._resolve(
            org=org,
            side_effect=RuntimeError("crypto failure"),
            system_license=_license(tier="team", features=[]),
        )
        assert result is not None
        from_db.assert_awaited_once_with(session, "team", has_license_key=True, license_features=set())

    async def test_org_license_cancelled_error_propagates(self) -> None:
        org = MagicMock()
        org.settings_json = {"license_key": "k"}
        with pytest.raises(asyncio.CancelledError):
            await self._resolve(org=org, side_effect=asyncio.CancelledError())

    async def test_system_license_used_without_org_key(self) -> None:
        org = MagicMock()
        org.settings_json = None
        result, from_db, session = await self._resolve(
            org=org,
            system_license=_license(tier="team", features=["audit_viewer"]),
        )
        assert result is not None
        from_db.assert_awaited_once_with(session, "team", has_license_key=True, license_features={"audit_viewer"})

    async def test_env_license_key_used_without_system_license(self) -> None:
        result, from_db, session = await self._resolve(
            settings_license_key="env-key",
            license_validation=_valid_validation(tier="team", features=["scim"]),
        )
        assert result is not None
        from_db.assert_awaited_once_with(session, "team", has_license_key=True, license_features={"scim"})

    async def test_env_license_cancelled_error_propagates(self) -> None:
        with pytest.raises(asyncio.CancelledError):
            await self._resolve(
                settings_license_key="env-key",
                side_effect=asyncio.CancelledError(),
            )

    async def test_env_license_parse_error_falls_through(self) -> None:
        result, from_db, session = await self._resolve(
            settings_license_key="env-key",
            side_effect=RuntimeError("crypto failure"),
        )
        assert result is not None
        from_db.assert_awaited_once_with(session, "community")

    async def test_env_license_invalid_falls_through_to_org_plan_id(self) -> None:
        org = MagicMock()
        org.settings_json = None
        org.plan_id = "custom-plan"
        result, from_db, session = await self._resolve(
            org=org,
            settings_license_key="env-key",
            license_validation=_invalid_validation(),
        )
        assert result is not None
        from_db.assert_awaited_once_with(session, "custom-plan")

    async def test_org_plan_id_fallback(self) -> None:
        org = MagicMock()
        org.settings_json = None
        org.plan_id = "custom-plan"
        result, from_db, session = await self._resolve(org=org)
        assert result is not None
        from_db.assert_awaited_once_with(session, "custom-plan")

    async def test_community_fallback(self) -> None:
        result, from_db, session = await self._resolve(org=None)
        assert result is not None
        from_db.assert_awaited_once_with(session, "community")


class TestGetPlanForOrg:
    async def test_org_plan_id_wins(self) -> None:
        session = AsyncMock()
        org = MagicMock(plan_id="team")
        with patch("modulo.db.crud.organisation.get_organisation", new_callable=AsyncMock, return_value=org):
            assert await get_plan_for_org(session, uuid.uuid4()) == "team"

    async def test_missing_org_plan_uses_config(self) -> None:
        session = AsyncMock()
        org = MagicMock(plan_id=None)
        config = MagicMock(value="config-plan")
        with (
            patch("modulo.db.crud.organisation.get_organisation", new_callable=AsyncMock, return_value=org),
            patch("modulo.db.crud.system_config.get_config", new_callable=AsyncMock, return_value=config),
        ):
            assert await get_plan_for_org(session, uuid.uuid4()) == "config-plan"

    async def test_missing_config_returns_community(self) -> None:
        session = AsyncMock()
        with (
            patch("modulo.db.crud.organisation.get_organisation", new_callable=AsyncMock, return_value=None),
            patch("modulo.db.crud.system_config.get_config", new_callable=AsyncMock, return_value=None),
        ):
            assert await get_plan_for_org(session, uuid.uuid4()) == "community"

    async def test_none_org_id_uses_config(self) -> None:
        session = AsyncMock()
        config = MagicMock(value="config-plan")
        with patch("modulo.db.crud.system_config.get_config", new_callable=AsyncMock, return_value=config):
            assert await get_plan_for_org(session, None) == "config-plan"


class TestResolveFlag:
    def _registry(self) -> FeatureFlagRegistry:
        registry = FeatureFlagRegistry()
        registry._get_org_override = AsyncMock(return_value=None)
        registry._get_team_override = AsyncMock(return_value=None)
        registry._get_user_override = AsyncMock(return_value=None)
        return registry

    async def test_system_override_wins_over_all(self) -> None:
        registry = self._registry()
        registry.set_override("sso", True)
        registry._get_user_override = AsyncMock(return_value=False)
        try:
            assert (
                await registry.resolve_flag(
                    "sso",
                    org_id=uuid.uuid4(),
                    team_id=uuid.uuid4(),
                    user_id=uuid.uuid4(),
                )
                is True
            )
        finally:
            registry.clear_override("sso")

    async def test_user_override_beats_team_and_org(self) -> None:
        registry = self._registry()
        registry._get_user_override = AsyncMock(return_value=True)
        registry._get_team_override = AsyncMock(return_value=False)
        registry._get_org_override = AsyncMock(return_value=False)
        assert (
            await registry.resolve_flag(
                "sso",
                org_id=uuid.uuid4(),
                team_id=uuid.uuid4(),
                user_id=uuid.uuid4(),
            )
            is True
        )

    async def test_team_override_beats_org(self) -> None:
        registry = self._registry()
        registry._get_team_override = AsyncMock(return_value=True)
        registry._get_org_override = AsyncMock(return_value=False)
        assert await registry.resolve_flag("sso", org_id=uuid.uuid4(), team_id=uuid.uuid4()) is True

    async def test_org_override_used_when_no_higher_scope(self) -> None:
        registry = self._registry()
        registry._get_org_override = AsyncMock(return_value=True)
        assert await registry.resolve_flag("sso", org_id=uuid.uuid4()) is True

    async def test_default_state_when_no_overrides(self) -> None:
        registry = self._registry()
        assert await registry.resolve_flag("sso") is False
        assert await registry.resolve_flag("saved_views") is True

    async def test_unknown_flag_is_false(self) -> None:
        registry = self._registry()
        assert await registry.resolve_flag("nonexistent") is False

    async def test_no_db_calls_without_scoped_ids(self) -> None:
        registry = FeatureFlagRegistry()
        with (
            patch.object(registry, "_get_org_override", new_callable=AsyncMock) as org,
            patch.object(registry, "_get_team_override", new_callable=AsyncMock) as team,
            patch.object(registry, "_get_user_override", new_callable=AsyncMock) as user,
        ):
            assert await registry.resolve_flag("saved_views") is True
        org.assert_not_awaited()
        team.assert_not_awaited()
        user.assert_not_awaited()


class TestOverrideDbLookups:
    async def _lookup(
        self,
        fn: object,
        *,
        flag_name: str = "sso",
        org: object | None = None,
        team: object | None = None,
        account: object | None = None,
        org_error: Exception | None = None,
        team_error: Exception | None = None,
        account_error: Exception | None = None,
    ) -> object:
        session = AsyncMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        async_session = MagicMock(return_value=cm)
        with (
            patch("modulo.api.dependencies.get_or_create_engine", return_value=MagicMock()),
            patch("modulo.settings.get_settings", return_value=MagicMock()),
            patch("sqlalchemy.ext.asyncio.AsyncSession", async_session),
            patch(
                "modulo.db.crud.organisation.get_organisation",
                new_callable=AsyncMock,
                return_value=org,
                side_effect=org_error,
            ),
            patch(
                "modulo.db.crud.team.get_team",
                new_callable=AsyncMock,
                return_value=team,
                side_effect=team_error,
            ),
            patch(
                "modulo.db.crud.account.get_account_by_id",
                new_callable=AsyncMock,
                return_value=account,
                side_effect=account_error,
            ),
        ):
            return await fn(flag_name, uuid.uuid4())

    async def test_org_override_returned(self) -> None:
        registry = FeatureFlagRegistry()
        org = MagicMock(settings_json={"feature_overrides": {"sso": True}})
        assert await self._lookup(registry._get_org_override, org=org) is True

    async def test_org_override_disabled(self) -> None:
        registry = FeatureFlagRegistry()
        org = MagicMock(settings_json={"feature_overrides": {"sso": False}})
        assert await self._lookup(registry._get_org_override, org=org) is False

    async def test_org_override_missing_flag_returns_none(self) -> None:
        registry = FeatureFlagRegistry()
        org = MagicMock(settings_json={"feature_overrides": {"other": True}})
        assert await self._lookup(registry._get_org_override, org=org) is None

    async def test_org_without_settings_returns_none(self) -> None:
        registry = FeatureFlagRegistry()
        org = MagicMock(settings_json=None)
        assert await self._lookup(registry._get_org_override, org=org) is None

    async def test_org_lookup_error_returns_none(self) -> None:
        registry = FeatureFlagRegistry()
        assert await self._lookup(registry._get_org_override, org_error=RuntimeError("db down")) is None

    async def test_org_cancelled_error_propagates(self) -> None:
        registry = FeatureFlagRegistry()
        with pytest.raises(asyncio.CancelledError):
            await self._lookup(registry._get_org_override, org_error=asyncio.CancelledError())

    async def test_team_override_returned(self) -> None:
        registry = FeatureFlagRegistry()
        team = MagicMock(settings={"feature_overrides": {"sso": True}})
        assert await self._lookup(registry._get_team_override, team=team) is True

    async def test_team_cancelled_error_propagates(self) -> None:
        registry = FeatureFlagRegistry()
        with pytest.raises(asyncio.CancelledError):
            await self._lookup(registry._get_team_override, team_error=asyncio.CancelledError())

    async def test_team_lookup_error_returns_none(self) -> None:
        registry = FeatureFlagRegistry()
        assert await self._lookup(registry._get_team_override, team_error=RuntimeError("db down")) is None

    async def test_team_without_settings_returns_none(self) -> None:
        registry = FeatureFlagRegistry()
        team = MagicMock(settings=None)
        assert await self._lookup(registry._get_team_override, team=team) is None

    async def test_user_override_returned(self) -> None:
        registry = FeatureFlagRegistry()
        account = MagicMock(preferences={"feature_overrides": {"sso": True}})
        assert await self._lookup(registry._get_user_override, account=account) is True

    async def test_user_cancelled_error_propagates(self) -> None:
        registry = FeatureFlagRegistry()
        with pytest.raises(asyncio.CancelledError):
            await self._lookup(registry._get_user_override, account_error=asyncio.CancelledError())

    async def test_user_lookup_error_returns_none(self) -> None:
        registry = FeatureFlagRegistry()
        assert await self._lookup(registry._get_user_override, account_error=RuntimeError("db down")) is None

    async def test_user_without_preferences_returns_none(self) -> None:
        registry = FeatureFlagRegistry()
        account = MagicMock(preferences=None)
        assert await self._lookup(registry._get_user_override, account=account) is None


class TestGetRegistry:
    def test_singleton_and_community_default(self) -> None:
        import modulo.core.feature_flags as ff

        saved = ff._registry
        ff._registry = None
        try:
            first = get_registry()
            second = get_registry()
            assert first is second
            assert first.current_tier == "community"
            assert first.has_license_key is False
        finally:
            ff._registry = saved


class TestFeatureFlagRegistryDb:
    async def test_load_from_db_keeps_hardcoded_flags_when_db_empty(self) -> None:
        registry = FeatureFlagRegistry()
        session = AsyncMock()
        with (
            patch("modulo.db.crud.tier_catalog.list_tiers", new_callable=AsyncMock, return_value=[]),
            patch("modulo.db.crud.tier_catalog.list_feature_flags", new_callable=AsyncMock, return_value=[]),
        ):
            await registry.load_from_db(session)
        assert registry.get_flag("saved_views") is not None
        assert registry.get_flag("sso") is not None

    async def test_load_from_db_filters_inactive_flags(self) -> None:
        registry = FeatureFlagRegistry()
        session = AsyncMock()
        db_flags = [
            {"name": "db_on", "description": "On", "tier_id": "team", "depends_on": None, "is_active": True},
            {"name": "db_off", "description": "Off", "tier_id": "team", "depends_on": None, "is_active": False},
        ]
        with (
            patch("modulo.db.crud.tier_catalog.list_tiers", new_callable=AsyncMock, return_value=[]),
            patch("modulo.db.crud.tier_catalog.list_feature_flags", new_callable=AsyncMock, return_value=db_flags),
        ):
            await registry.load_from_db(session)
        assert registry.get_flag("db_on") is not None
        assert registry.get_flag("db_off") is None

    async def test_load_from_db_applies_custom_tier_rank(self) -> None:
        registry = FeatureFlagRegistry()
        session = AsyncMock()
        db_tiers = [{"tier_id": "community", "rank": 0}, {"tier_id": "pro", "rank": 5}]
        db_flags = [
            {"name": "pro_flag", "description": "Pro", "tier_id": "pro", "depends_on": None, "is_active": True},
        ]
        with (
            patch("modulo.db.crud.tier_catalog.list_tiers", new_callable=AsyncMock, return_value=db_tiers),
            patch("modulo.db.crud.tier_catalog.list_feature_flags", new_callable=AsyncMock, return_value=db_flags),
        ):
            await registry.load_from_db(session)
            registry.refresh(current_tier="pro", has_license_key=True)
        flag = registry.get_flag("pro_flag")
        assert flag is not None
        assert flag.currently_active is True
