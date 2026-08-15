"""QA lens pass on the feature_flags test package.

Covers the plan-context adapters (``CommunityTier`` / ``LicenseKeyTier`` /
``DbPlanContext``), the ``resolve_plan_context`` resolution order (org license >
in-memory license > env-var license > org plan_id > community fallback), the
per-entity flag override resolution (``resolve_flag`` precedence plus the
org/team/user DB lookups and their failure paths), the process-global registry
singleton, ``get_plan_for_org``, the ``load_from_db`` transaction entry paths,
and golden-value pins for the tier contract that must never regress.
"""

import asyncio
import contextlib
import logging
import sys
import types
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import modulo.core.feature_flags as feature_flags_module
from modulo.core.feature_flags import (
    TIER_RANK,
    CommunityTier,
    DbPlanContext,
    FeatureFlagRegistry,
    LicenseKeyTier,
    get_plan_for_org,
    get_registry,
    resolve_plan_context,
)
from modulo.core.license import LicenseData

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")

_LOGGER = "modulo.core.feature_flags"


@pytest.fixture(autouse=True)
def _clean_global_state() -> None:
    """Overrides are class-level and the process-global registry is a module
    singleton — isolate every test so the tier contract pins can't be polluted."""
    FeatureFlagRegistry._overrides.clear()
    feature_flags_module._registry = None
    yield
    FeatureFlagRegistry._overrides.clear()
    feature_flags_module._registry = None


def _make_db_session(*, in_transaction: bool = False, async_in_transaction: bool = False) -> AsyncMock:
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    if async_in_transaction:
        session.in_transaction = AsyncMock(return_value=True)
    else:
        session.in_transaction = MagicMock(return_value=in_transaction)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@contextlib.contextmanager
def _fake_crud_module(module: str, **attrs) -> contextlib.AbstractContextManager[None]:
    """Install a fake ``modulo.db.crud.*`` / ``modulo.api.dependencies`` module.

    The real modules cannot always be imported in this sandbox (the shared
    ``modulo.db.crud.base`` uses PEP 695 generics, Python >= 3.12), and the
    functions under test import them lazily at call time — so a ``sys.modules``
    injection is sufficient and works on every interpreter version.
    """
    fake = types.ModuleType(module)
    for name, value in attrs.items():
        setattr(fake, name, value)
    with patch.dict(sys.modules, {module: fake}):
        yield


def _license(tier: str = "team", features: list[str] | None = None) -> LicenseData:
    return LicenseData(
        tier=tier,
        features=features or [],
        expires_at="",
        org_id="org-1",
        raw_payload={},
        raw_key="k",
    )


def _db_flags() -> list[dict[str, object]]:
    return [
        {"name": "sso", "description": "SSO", "tier_id": "team", "depends_on": None, "is_active": True},
        {
            "name": "saved_views",
            "description": "Saved views",
            "tier_id": "community",
            "depends_on": None,
            "is_active": True,
        },
    ]


def _db_tiers() -> list[dict[str, object]]:
    return [
        {"tier_id": "community", "rank": 0},
        {"tier_id": "team", "rank": 1},
    ]


class TestCommunityTier:
    def test_feature_enabled_reflects_active_flag(self) -> None:
        ctx = CommunityTier()
        assert ctx.feature_enabled("saved_views") is True
        assert ctx.feature_enabled("sso") is False

    def test_feature_enabled_unknown_flag_returns_false(self) -> None:
        ctx = CommunityTier()
        assert ctx.feature_enabled("does_not_exist") is False

    def test_list_enabled_features_only_active(self) -> None:
        ctx = CommunityTier()
        names = {f.name for f in ctx.list_enabled_features()}
        assert "saved_views" in names
        assert "sso" not in names

    def test_tier_and_license_accessors(self) -> None:
        ctx = CommunityTier()
        assert ctx.tier() == "community"
        assert ctx.has_license_key() is False


class TestLicenseKeyTier:
    def test_feature_enabled_via_tier(self) -> None:
        ctx = LicenseKeyTier(_license(tier="team", features=[]))
        assert ctx.feature_enabled("sso") is True

    def test_feature_enabled_via_explicit_feature_list(self) -> None:
        ctx = LicenseKeyTier(_license(tier="community", features=["sso"]))
        assert ctx.feature_enabled("sso") is True

    def test_feature_enabled_unknown_flag_returns_false(self) -> None:
        ctx = LicenseKeyTier(_license())
        assert ctx.feature_enabled("does_not_exist") is False

    def test_list_enabled_features_includes_explicit_license_features(self) -> None:
        ctx = LicenseKeyTier(_license(tier="community", features=["sso"]))
        names = {f.name for f in ctx.list_enabled_features()}
        assert "sso" in names
        assert "saved_views" in names

    def test_tier_and_license_accessors(self) -> None:
        ctx = LicenseKeyTier(_license())
        assert ctx.tier() == "team"
        assert ctx.has_license_key() is True


class TestDbPlanContext:
    async def _ctx(
        self,
        tier: str = "community",
        has_license_key: bool = False,
        license_features: set[str] | None = None,
    ) -> DbPlanContext:
        session = _make_db_session()
        with (
            patch("modulo.db.crud.tier_catalog.list_tiers", return_value=_db_tiers()),
            patch("modulo.db.crud.tier_catalog.list_feature_flags", return_value=_db_flags()),
        ):
            return await DbPlanContext.from_db(
                session, tier, has_license_key=has_license_key, license_features=license_features
            )

    async def test_feature_enabled_unknown_flag_returns_false(self) -> None:
        ctx = await self._ctx("community")
        assert ctx.feature_enabled("does_not_exist") is False

    async def test_license_features_activate_flags_regardless_of_tier(self) -> None:
        ctx = await self._ctx("community", has_license_key=True, license_features={"sso"})
        assert ctx.feature_enabled("sso") is True
        assert ctx.feature_enabled("saved_views") is True

    async def test_no_license_features_keeps_tier_defaults(self) -> None:
        ctx = await self._ctx("community", has_license_key=True)
        assert ctx.feature_enabled("sso") is False
        assert ctx.feature_enabled("saved_views") is True

    async def test_list_enabled_features(self) -> None:
        ctx = await self._ctx("community", has_license_key=True, license_features={"sso"})
        names = {f.name for f in ctx.list_enabled_features()}
        assert names == {"sso", "saved_views"}

    async def test_tier_and_license_accessors(self) -> None:
        ctx = await self._ctx("team", has_license_key=True)
        assert ctx.tier() == "team"
        assert ctx.has_license_key() is True


class TestResolvePlanContext:
    @pytest.fixture
    def mock_from_db(self):
        mock = AsyncMock(return_value=MagicMock())
        with patch.object(DbPlanContext, "from_db", mock):
            yield mock

    @staticmethod
    def _invalid() -> SimpleNamespace:
        return SimpleNamespace(valid=False, license_data=None)

    @staticmethod
    def _valid(tier: str = "team", features: list[str] | None = None) -> SimpleNamespace:
        return SimpleNamespace(
            valid=True,
            license_data=SimpleNamespace(tier=tier, features=features or []),
        )

    async def test_org_license_key_takes_priority(self, mock_from_db) -> None:
        org = SimpleNamespace(settings_json={"license_key": "org-key"}, plan_id="team")
        settings = SimpleNamespace(modulo_license_key="env-key")
        with (
            patch("modulo.core.license.parse_and_verify", return_value=self._valid(features=["sso"])),
            patch("modulo.core.license.get_license", return_value=_license(features=["inmem"])),
        ):
            await resolve_plan_context(settings, MagicMock(), org)
        mock_from_db.assert_awaited_once()
        assert mock_from_db.await_args.args[1] == "team"
        assert mock_from_db.await_args.kwargs["has_license_key"] is True
        assert mock_from_db.await_args.kwargs["license_features"] == {"sso"}

    async def test_invalid_org_license_falls_through_to_in_memory(self, mock_from_db) -> None:
        org = SimpleNamespace(settings_json={"license_key": "bad"}, plan_id="team")
        with (
            patch("modulo.core.license.parse_and_verify", return_value=self._invalid()),
            patch("modulo.core.license.get_license", return_value=_license(features=["inmem"])),
        ):
            await resolve_plan_context(SimpleNamespace(modulo_license_key=""), MagicMock(), org)
        mock_from_db.assert_awaited_once()
        assert mock_from_db.await_args.args[1] == "team"
        assert mock_from_db.await_args.kwargs["license_features"] == {"inmem"}

    async def test_org_license_parse_error_logs_and_continues(self, mock_from_db, caplog) -> None:
        org = SimpleNamespace(settings_json={"license_key": "boom"}, plan_id=None)
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        with (
            patch("modulo.core.license.parse_and_verify", side_effect=ValueError("bad key")),
            patch("modulo.core.license.get_license", return_value=None),
        ):
            await resolve_plan_context(SimpleNamespace(modulo_license_key=""), MagicMock(), org)
        assert any("Failed to parse org-level license key" in r.getMessage() for r in caplog.records)
        mock_from_db.assert_awaited_once()
        assert mock_from_db.await_args.args[1] == "community"

    async def test_org_license_cancelled_error_reraised(self, mock_from_db) -> None:
        org = SimpleNamespace(settings_json={"license_key": "boom"}, plan_id=None)
        with (
            patch("modulo.core.license.parse_and_verify", side_effect=asyncio.CancelledError()),
            patch("modulo.core.license.get_license", return_value=None),
            pytest.raises(asyncio.CancelledError),
        ):
            await resolve_plan_context(SimpleNamespace(modulo_license_key=""), MagicMock(), org)
        mock_from_db.assert_not_awaited()

    async def test_in_memory_license_wins_over_env(self, mock_from_db) -> None:
        with patch("modulo.core.license.get_license", return_value=_license(features=["inmem"])):
            await resolve_plan_context(SimpleNamespace(modulo_license_key="env-key"), MagicMock(), None)
        mock_from_db.assert_awaited_once()
        assert mock_from_db.await_args.args[1] == "team"
        assert mock_from_db.await_args.kwargs["license_features"] == {"inmem"}

    async def test_env_license_used_when_no_org_and_no_in_memory(self, mock_from_db) -> None:
        with (
            patch("modulo.core.license.get_license", return_value=None),
            patch("modulo.core.license.parse_and_verify", return_value=self._valid(features=["env"])),
        ):
            await resolve_plan_context(SimpleNamespace(modulo_license_key="env-key"), MagicMock(), None)
        mock_from_db.assert_awaited_once()
        assert mock_from_db.await_args.args[1] == "team"
        assert mock_from_db.await_args.kwargs["license_features"] == {"env"}

    async def test_env_license_parse_error_logs_and_falls_back(self, mock_from_db, caplog) -> None:
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        with (
            patch("modulo.core.license.get_license", return_value=None),
            patch("modulo.core.license.parse_and_verify", side_effect=RuntimeError("boom")),
        ):
            await resolve_plan_context(SimpleNamespace(modulo_license_key="env-key"), MagicMock(), None)
        assert any("Failed to parse env-var license key" in r.getMessage() for r in caplog.records)
        mock_from_db.assert_awaited_once()
        assert mock_from_db.await_args.args[1] == "community"

    async def test_env_license_cancelled_error_reraised(self, mock_from_db) -> None:
        with (
            patch("modulo.core.license.get_license", return_value=None),
            patch("modulo.core.license.parse_and_verify", side_effect=asyncio.CancelledError()),
            pytest.raises(asyncio.CancelledError),
        ):
            await resolve_plan_context(SimpleNamespace(modulo_license_key="env-key"), MagicMock(), None)
        mock_from_db.assert_not_awaited()

    async def test_org_community_plan_uses_community_db_context(self, mock_from_db) -> None:
        org = SimpleNamespace(settings_json=None, plan_id="community")
        with (
            patch("modulo.core.license.get_license", return_value=None),
            patch("modulo.core.license.parse_and_verify", return_value=self._invalid()),
        ):
            await resolve_plan_context(SimpleNamespace(modulo_license_key=""), MagicMock(), org)
        mock_from_db.assert_awaited_once()
        assert mock_from_db.await_args.args[1] == "community"
        assert mock_from_db.await_args.kwargs.get("has_license_key", False) is False

    async def test_org_paid_plan_without_license_downgrades_to_community(self, mock_from_db, caplog) -> None:
        org = SimpleNamespace(settings_json=None, plan_id="team")
        caplog.set_level(logging.INFO, logger=_LOGGER)
        with (
            patch("modulo.core.license.get_license", return_value=None),
            patch("modulo.core.license.parse_and_verify", return_value=self._invalid()),
        ):
            await resolve_plan_context(SimpleNamespace(modulo_license_key=""), MagicMock(), org)
        assert any("plan.team_without_license_falling_back_to_community" in r.getMessage() for r in caplog.records)
        mock_from_db.assert_awaited_once()
        assert mock_from_db.await_args.args[1] == "community"

    async def test_no_org_no_license_community_fallback(self, mock_from_db) -> None:
        with (
            patch("modulo.core.license.get_license", return_value=None),
            patch("modulo.core.license.parse_and_verify", return_value=self._invalid()),
        ):
            await resolve_plan_context(SimpleNamespace(modulo_license_key=""), MagicMock(), None)
        mock_from_db.assert_awaited_once()
        assert mock_from_db.await_args.args[1] == "community"


class TestResolveFlag:
    def _registry(self, tier: str = "community") -> FeatureFlagRegistry:
        FeatureFlagRegistry._overrides.clear()
        return FeatureFlagRegistry(current_tier=tier)

    async def test_system_override_wins_over_entity_overrides(self) -> None:
        registry = self._registry()
        registry.set_override("sso", True)
        with (
            patch.object(registry, "_get_user_override", new=AsyncMock(return_value=False)),
            patch.object(registry, "_get_team_override", new=AsyncMock(return_value=False)),
            patch.object(registry, "_get_org_override", new=AsyncMock(return_value=False)),
        ):
            assert await registry.resolve_flag("sso", org_id=_ORG_ID) is True

    async def test_user_override_wins_over_team_and_org(self) -> None:
        registry = self._registry()
        with (
            patch.object(registry, "_get_user_override", new=AsyncMock(return_value=True)),
            patch.object(registry, "_get_team_override", new=AsyncMock(return_value=False)),
            patch.object(registry, "_get_org_override", new=AsyncMock(return_value=False)),
        ):
            assert await registry.resolve_flag("sso", user_id=_USER_ID) is True

    async def test_team_override_wins_over_org(self) -> None:
        registry = self._registry()
        with (
            patch.object(registry, "_get_user_override", new=AsyncMock(return_value=None)),
            patch.object(registry, "_get_team_override", new=AsyncMock(return_value=True)),
            patch.object(registry, "_get_org_override", new=AsyncMock(return_value=False)),
        ):
            assert await registry.resolve_flag("sso", team_id=_TEAM_ID) is True

    async def test_org_override_used_when_user_and_team_none(self) -> None:
        registry = self._registry()
        with (
            patch.object(registry, "_get_user_override", new=AsyncMock(return_value=None)),
            patch.object(registry, "_get_team_override", new=AsyncMock(return_value=None)),
            patch.object(registry, "_get_org_override", new=AsyncMock(return_value=True)),
        ):
            assert await registry.resolve_flag("sso", org_id=_ORG_ID) is True

    async def test_no_overrides_falls_back_to_flag_default(self) -> None:
        registry = self._registry()
        with (
            patch.object(registry, "_get_user_override", new=AsyncMock(return_value=None)),
            patch.object(registry, "_get_team_override", new=AsyncMock(return_value=None)),
            patch.object(registry, "_get_org_override", new=AsyncMock(return_value=None)),
        ):
            assert await registry.resolve_flag("sso") is False
            assert await registry.resolve_flag("saved_views") is True

    async def test_unknown_flag_returns_false(self) -> None:
        registry = self._registry()
        assert await registry.resolve_flag("does_not_exist") is False

    async def test_user_override_not_queried_without_user_id(self) -> None:
        registry = self._registry()
        user = AsyncMock(return_value=None)
        with patch.object(registry, "_get_user_override", new=user):
            await registry.resolve_flag("saved_views")
        user.assert_not_awaited()


class TestEntityOverrideLookups:
    @staticmethod
    def _session(*, in_transaction: bool = False, async_in_transaction: bool = False) -> AsyncMock:
        return _make_db_session(in_transaction=in_transaction, async_in_transaction=async_in_transaction)

    @staticmethod
    def _org_patch(session: AsyncMock, org, side_effect=None) -> contextlib.AbstractContextManager[None]:
        stack = contextlib.ExitStack()
        stack.enter_context(
            _fake_crud_module("modulo.api.dependencies", get_or_create_engine=MagicMock(return_value=MagicMock()))
        )
        stack.enter_context(
            _fake_crud_module(
                "modulo.db.crud.organisation",
                get_organisation=AsyncMock(return_value=org, side_effect=side_effect),
            )
        )
        stack.enter_context(patch("modulo.settings.get_settings", return_value=MagicMock()))
        stack.enter_context(patch("sqlalchemy.ext.asyncio.AsyncSession", return_value=session))
        return stack

    async def test_org_override_reads_feature_overrides(self) -> None:
        registry = FeatureFlagRegistry()
        org = SimpleNamespace(settings_json={"feature_overrides": {"sso": True}})
        session = self._session()
        with self._org_patch(session, org):
            assert await registry._get_org_override("sso", _ORG_ID) is True

    async def test_org_override_false_value_returns_false(self) -> None:
        registry = FeatureFlagRegistry()
        org = SimpleNamespace(settings_json={"feature_overrides": {"sso": False}})
        session = self._session()
        with self._org_patch(session, org):
            assert await registry._get_org_override("sso", _ORG_ID) is False

    async def test_org_override_missing_flag_returns_none(self) -> None:
        registry = FeatureFlagRegistry()
        org = SimpleNamespace(settings_json={"feature_overrides": {"other": True}})
        session = self._session()
        with self._org_patch(session, org):
            assert await registry._get_org_override("sso", _ORG_ID) is None

    async def test_org_override_no_org_returns_none(self) -> None:
        registry = FeatureFlagRegistry()
        session = self._session()
        with self._org_patch(session, None):
            assert await registry._get_org_override("sso", _ORG_ID) is None

    async def test_org_override_error_logs_and_returns_none(self, caplog) -> None:
        registry = FeatureFlagRegistry()
        caplog.set_level(logging.ERROR, logger=_LOGGER)
        session = self._session()
        with self._org_patch(session, None, side_effect=RuntimeError("db down")):
            assert await registry._get_org_override("sso", _ORG_ID) is None
        assert any("Failed to check org flag override" in r.getMessage() for r in caplog.records)

    async def test_org_override_active_transaction_skips_begin(self) -> None:
        registry = FeatureFlagRegistry()
        org = SimpleNamespace(settings_json={"feature_overrides": {"sso": True}})
        session = self._session(in_transaction=True)
        with self._org_patch(session, org):
            assert await registry._get_org_override("sso", _ORG_ID) is True
        session.begin.assert_not_called()

    async def test_org_override_async_in_transaction_is_awaited(self) -> None:
        registry = FeatureFlagRegistry()
        org = SimpleNamespace(settings_json={"feature_overrides": {"sso": True}})
        session = self._session(async_in_transaction=True)
        with self._org_patch(session, org):
            assert await registry._get_org_override("sso", _ORG_ID) is True
        session.begin.assert_not_called()

    async def test_team_override_reads_settings(self) -> None:
        registry = FeatureFlagRegistry()
        team = SimpleNamespace(settings={"feature_overrides": {"sso": True}})
        session = self._session()
        with (
            _fake_crud_module("modulo.api.dependencies", get_or_create_engine=MagicMock(return_value=MagicMock())),
            _fake_crud_module("modulo.db.crud.team", get_team=AsyncMock(return_value=team)),
            patch("modulo.settings.get_settings", return_value=MagicMock()),
            patch("sqlalchemy.ext.asyncio.AsyncSession", return_value=session),
        ):
            assert await registry._get_team_override("sso", _TEAM_ID) is True

    async def test_team_override_error_logs_and_returns_none(self, caplog) -> None:
        registry = FeatureFlagRegistry()
        caplog.set_level(logging.ERROR, logger=_LOGGER)
        session = self._session()
        with (
            _fake_crud_module("modulo.api.dependencies", get_or_create_engine=MagicMock(return_value=MagicMock())),
            _fake_crud_module("modulo.db.crud.team", get_team=AsyncMock(side_effect=RuntimeError("db down"))),
            patch("modulo.settings.get_settings", return_value=MagicMock()),
            patch("sqlalchemy.ext.asyncio.AsyncSession", return_value=session),
        ):
            assert await registry._get_team_override("sso", _TEAM_ID) is None
        assert any("Failed to check team flag override" in r.getMessage() for r in caplog.records)

    async def test_user_override_reads_preferences(self) -> None:
        registry = FeatureFlagRegistry()
        account = SimpleNamespace(preferences={"feature_overrides": {"sso": True}})
        session = self._session()
        with (
            _fake_crud_module("modulo.api.dependencies", get_or_create_engine=MagicMock(return_value=MagicMock())),
            _fake_crud_module("modulo.db.crud.account", get_account_by_id=AsyncMock(return_value=account)),
            patch("modulo.settings.get_settings", return_value=MagicMock()),
            patch("sqlalchemy.ext.asyncio.AsyncSession", return_value=session),
        ):
            assert await registry._get_user_override("sso", _USER_ID) is True

    async def test_user_override_error_logs_and_returns_none(self, caplog) -> None:
        registry = FeatureFlagRegistry()
        caplog.set_level(logging.ERROR, logger=_LOGGER)
        session = self._session()
        with (
            _fake_crud_module("modulo.api.dependencies", get_or_create_engine=MagicMock(return_value=MagicMock())),
            _fake_crud_module(
                "modulo.db.crud.account",
                get_account_by_id=AsyncMock(side_effect=RuntimeError("db down")),
            ),
            patch("modulo.settings.get_settings", return_value=MagicMock()),
            patch("sqlalchemy.ext.asyncio.AsyncSession", return_value=session),
        ):
            assert await registry._get_user_override("sso", _USER_ID) is None
        assert any("Failed to check user flag override" in r.getMessage() for r in caplog.records)

    async def test_team_override_active_transaction_skips_begin(self) -> None:
        registry = FeatureFlagRegistry()
        team = SimpleNamespace(settings={"feature_overrides": {"sso": True}})
        session = self._session(in_transaction=True)
        with (
            _fake_crud_module("modulo.api.dependencies", get_or_create_engine=MagicMock(return_value=MagicMock())),
            _fake_crud_module("modulo.db.crud.team", get_team=AsyncMock(return_value=team)),
            patch("modulo.settings.get_settings", return_value=MagicMock()),
            patch("sqlalchemy.ext.asyncio.AsyncSession", return_value=session),
        ):
            assert await registry._get_team_override("sso", _TEAM_ID) is True
        session.begin.assert_not_called()

    async def test_user_override_active_transaction_skips_begin(self) -> None:
        registry = FeatureFlagRegistry()
        account = SimpleNamespace(preferences={"feature_overrides": {"sso": True}})
        session = self._session(in_transaction=True)
        with (
            _fake_crud_module("modulo.api.dependencies", get_or_create_engine=MagicMock(return_value=MagicMock())),
            _fake_crud_module("modulo.db.crud.account", get_account_by_id=AsyncMock(return_value=account)),
            patch("modulo.settings.get_settings", return_value=MagicMock()),
            patch("sqlalchemy.ext.asyncio.AsyncSession", return_value=session),
        ):
            assert await registry._get_user_override("sso", _USER_ID) is True
        session.begin.assert_not_called()

    async def test_team_override_async_in_transaction_probe_is_awaited(self) -> None:
        registry = FeatureFlagRegistry()
        team = SimpleNamespace(settings={"feature_overrides": {"sso": True}})
        session = self._session(async_in_transaction=True)
        with (
            _fake_crud_module("modulo.api.dependencies", get_or_create_engine=MagicMock(return_value=MagicMock())),
            _fake_crud_module("modulo.db.crud.team", get_team=AsyncMock(return_value=team)),
            patch("modulo.settings.get_settings", return_value=MagicMock()),
            patch("sqlalchemy.ext.asyncio.AsyncSession", return_value=session),
        ):
            assert await registry._get_team_override("sso", _TEAM_ID) is True
        session.begin.assert_not_called()

    async def test_user_override_async_in_transaction_probe_is_awaited(self) -> None:
        registry = FeatureFlagRegistry()
        account = SimpleNamespace(preferences={"feature_overrides": {"sso": True}})
        session = self._session(async_in_transaction=True)
        with (
            _fake_crud_module("modulo.api.dependencies", get_or_create_engine=MagicMock(return_value=MagicMock())),
            _fake_crud_module("modulo.db.crud.account", get_account_by_id=AsyncMock(return_value=account)),
            patch("modulo.settings.get_settings", return_value=MagicMock()),
            patch("sqlalchemy.ext.asyncio.AsyncSession", return_value=session),
        ):
            assert await registry._get_user_override("sso", _USER_ID) is True
        session.begin.assert_not_called()

    async def test_org_override_cancelled_error_reraised(self) -> None:
        registry = FeatureFlagRegistry()
        session = self._session()
        with (
            _fake_crud_module("modulo.api.dependencies", get_or_create_engine=MagicMock(return_value=MagicMock())),
            _fake_crud_module(
                "modulo.db.crud.organisation",
                get_organisation=AsyncMock(side_effect=asyncio.CancelledError()),
            ),
            patch("modulo.settings.get_settings", return_value=MagicMock()),
            patch("sqlalchemy.ext.asyncio.AsyncSession", return_value=session),
            pytest.raises(asyncio.CancelledError),
        ):
            await registry._get_org_override("sso", _ORG_ID)

    async def test_team_override_cancelled_error_reraised(self) -> None:
        registry = FeatureFlagRegistry()
        session = self._session()
        with (
            _fake_crud_module("modulo.api.dependencies", get_or_create_engine=MagicMock(return_value=MagicMock())),
            _fake_crud_module("modulo.db.crud.team", get_team=AsyncMock(side_effect=asyncio.CancelledError())),
            patch("modulo.settings.get_settings", return_value=MagicMock()),
            patch("sqlalchemy.ext.asyncio.AsyncSession", return_value=session),
            pytest.raises(asyncio.CancelledError),
        ):
            await registry._get_team_override("sso", _TEAM_ID)

    async def test_user_override_cancelled_error_reraised(self) -> None:
        registry = FeatureFlagRegistry()
        session = self._session()
        with (
            _fake_crud_module("modulo.api.dependencies", get_or_create_engine=MagicMock(return_value=MagicMock())),
            _fake_crud_module(
                "modulo.db.crud.account",
                get_account_by_id=AsyncMock(side_effect=asyncio.CancelledError()),
            ),
            patch("modulo.settings.get_settings", return_value=MagicMock()),
            patch("sqlalchemy.ext.asyncio.AsyncSession", return_value=session),
            pytest.raises(asyncio.CancelledError),
        ):
            await registry._get_user_override("sso", _USER_ID)


class TestGetRegistry:
    def test_singleton_is_cached(self) -> None:
        r1 = get_registry()
        r2 = get_registry()
        assert r1 is r2

    def test_reset_creates_new_singleton(self) -> None:
        r1 = get_registry()
        feature_flags_module._registry = None
        r2 = get_registry()
        assert r2 is not r1

    def test_default_registry_is_community(self) -> None:
        r = get_registry()
        assert r.current_tier == "community"
        assert r.has_license_key is False


class TestGetPlanForOrg:
    async def test_org_plan_id_wins(self) -> None:
        org = SimpleNamespace(plan_id="team")
        with (
            _fake_crud_module("modulo.db.crud.organisation", get_organisation=AsyncMock(return_value=org)),
            _fake_crud_module("modulo.db.crud.system_config", get_config=AsyncMock(return_value=None)),
        ):
            assert await get_plan_for_org(MagicMock(), _ORG_ID) == "team"

    async def test_org_without_plan_uses_config_default(self) -> None:
        org = SimpleNamespace(plan_id=None)
        config = SimpleNamespace(value="team")
        with (
            _fake_crud_module("modulo.db.crud.organisation", get_organisation=AsyncMock(return_value=org)),
            _fake_crud_module("modulo.db.crud.system_config", get_config=AsyncMock(return_value=config)),
        ):
            assert await get_plan_for_org(MagicMock(), _ORG_ID) == "team"

    async def test_no_org_uses_config_default(self) -> None:
        config = SimpleNamespace(value="pro")
        with (
            _fake_crud_module("modulo.db.crud.organisation", get_organisation=AsyncMock(return_value=None)),
            _fake_crud_module("modulo.db.crud.system_config", get_config=AsyncMock(return_value=config)),
        ):
            assert await get_plan_for_org(MagicMock(), None) == "pro"

    async def test_no_org_no_config_community(self) -> None:
        with (
            _fake_crud_module("modulo.db.crud.organisation", get_organisation=AsyncMock(return_value=None)),
            _fake_crud_module("modulo.db.crud.system_config", get_config=AsyncMock(return_value=None)),
        ):
            assert await get_plan_for_org(MagicMock(), None) == "community"


class TestLoadFromDbTransactionPaths:
    async def _load(
        self,
        session: AsyncMock,
        *,
        in_transaction: bool,
        async_in_transaction: bool = False,
    ) -> FeatureFlagRegistry:
        registry = FeatureFlagRegistry()
        if async_in_transaction:
            session.in_transaction = AsyncMock(return_value=True)
        else:
            session.in_transaction = MagicMock(return_value=in_transaction)
        with (
            patch("modulo.db.crud.tier_catalog.list_tiers", return_value=_db_tiers()),
            patch("modulo.db.crud.tier_catalog.list_feature_flags", return_value=_db_flags()),
        ):
            await registry.load_from_db(session)
        return registry

    async def test_opens_transaction_when_not_active(self) -> None:
        session = AsyncMock()
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)
        registry = await self._load(session, in_transaction=False)
        session.begin.assert_called_once()
        begin_cm.__aenter__.assert_awaited_once()
        assert registry.get_flag("sso") is not None

    async def test_awaits_async_in_transaction_probe(self) -> None:
        session = AsyncMock()
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)
        await self._load(session, in_transaction=True, async_in_transaction=True)
        session.begin.assert_not_called()

    async def test_reuses_active_transaction(self) -> None:
        session = AsyncMock()
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)
        await self._load(session, in_transaction=True)
        session.begin.assert_not_called()


class TestGoldenPins:
    def test_tier_rank_pinned(self) -> None:
        assert TIER_RANK == {"community": 0, "team": 1}

    def test_core_flag_tier_contract_pinned(self) -> None:
        registry = FeatureFlagRegistry()
        team_flags = {"sso", "team_rbac", "audit_viewer", "admin_spend_limits", "runtime_config", "rate_limits"}
        community_flags = {"parallel_branches", "eval_system", "webhook_trigger", "saved_views", "remy"}
        for name in team_flags:
            flag = registry.get_flag(name)
            assert flag is not None
            assert flag.tier == "team", f"{name} must stay team-tier"
        for name in community_flags:
            flag = registry.get_flag(name)
            assert flag is not None
            assert flag.tier == "community", f"{name} must stay community-tier"

    def test_remy_ui_driving_tier_pinned(self) -> None:
        registry = FeatureFlagRegistry()
        flag = registry.get_flag("remy_ui_driving")
        assert flag is not None
        assert flag.tier == "community"

    def test_mobile_sidebar_rail_default_off_on_every_tier(self) -> None:
        for tier in ("community", "team"):
            FeatureFlagRegistry._overrides.clear()
            registry = FeatureFlagRegistry(current_tier=tier)
            flag = registry.get_flag("mobile_sidebar_rail")
            assert flag is not None
            assert flag.currently_active is False, f"mobile_sidebar_rail must be default-OFF on {tier}"

    def test_community_tier_activates_only_community_flags(self) -> None:
        registry = FeatureFlagRegistry(current_tier="community")
        for flag in registry.list_flags():
            if flag.name == "mobile_sidebar_rail":
                assert flag.currently_active is False
            elif flag.tier == "community":
                assert flag.currently_active is True, f"{flag.name} should be active on community"
            else:
                assert flag.currently_active is False, f"{flag.name} should be inactive on community"

    def test_team_tier_activates_all_flags_except_inactive_experiments(self) -> None:
        registry = FeatureFlagRegistry(current_tier="team", has_license_key=True)
        for flag in registry.list_flags():
            if flag.name == "mobile_sidebar_rail":
                assert flag.currently_active is False
            else:
                assert flag.currently_active is True, f"{flag.name} should be active on team"
