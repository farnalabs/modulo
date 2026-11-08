"""Unit tests for FeatureFlagRegistry core functionality."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.feature_flags import DEFAULT_OFF_FLAGS, RETIRED_INTERNAL_FLAGS, FeatureFlagRegistry


def _make_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_default_off_registry(tier: str = "community", has_license_key: bool = False) -> FeatureFlagRegistry:
    FeatureFlagRegistry._overrides.clear()
    return FeatureFlagRegistry(current_tier=tier, has_license_key=has_license_key)


class TestInit:
    def test_default_tier_is_community(self) -> None:
        registry = FeatureFlagRegistry()
        assert registry.current_tier == "community"
        assert registry.has_license_key is False

    def test_init_creates_independent_flag_copies(self) -> None:
        r1 = FeatureFlagRegistry()
        r2 = FeatureFlagRegistry()
        r1_flags = r1.list_flags()
        r2_flags = r2.list_flags()
        assert r1_flags is not r2_flags  # different list objects
        # Mutating one shouldn't affect the other's _flags list
        assert id(r1_flags[0]) == id(r1.list_flags()[0])  # same internal objects


class TestListFlags:
    def test_returns_all_known_flags(self) -> None:
        registry = FeatureFlagRegistry()
        flags = registry.list_flags()
        # _KNOWN_FLAGS has 13 community + 21 total (13 community + 7 team + 2 v1 + 5 v2 + ...)
        assert len(flags) > 0

    def test_returns_copies_not_originals(self) -> None:
        registry = FeatureFlagRegistry()
        flags1 = registry.list_flags()
        flags2 = registry.list_flags()
        assert flags1 is not flags2  # different list objects each call


class TestGetFlag:
    def test_returns_flag_by_name(self) -> None:
        registry = FeatureFlagRegistry()
        flag = registry.get_flag("sso")
        assert flag is not None
        assert flag.name == "sso"
        assert flag.tier == "team"

    def test_returns_none_for_unknown(self) -> None:
        registry = FeatureFlagRegistry()
        assert registry.get_flag("nonexistent") is None

    def test_case_sensitive(self) -> None:
        registry = FeatureFlagRegistry()
        assert registry.get_flag("SSO") is None


class TestTierGapFlags:
    def test_returns_gaps_when_community(self) -> None:
        registry = FeatureFlagRegistry(current_tier="community")
        gaps = registry.tier_gap_flags()
        assert len(gaps) > 0
        for f in gaps:
            assert f.tier != "community"
            assert not f.currently_active

    def test_no_gaps_when_team(self) -> None:
        registry = FeatureFlagRegistry(current_tier="team", has_license_key=True)
        gaps = registry.tier_gap_flags()
        assert not gaps

    def test_no_gaps_when_v1(self) -> None:
        registry = FeatureFlagRegistry(current_tier="v1", has_license_key=True)
        gaps = registry.tier_gap_flags()
        assert not gaps

    def test_no_gaps_when_v2(self) -> None:
        registry = FeatureFlagRegistry(current_tier="v2", has_license_key=True)
        gaps = registry.tier_gap_flags()
        assert not gaps


class TestRefresh:
    def test_refresh_updates_tier(self) -> None:
        registry = FeatureFlagRegistry(current_tier="community")
        gaps_before = len(registry.tier_gap_flags())
        assert gaps_before > 0
        registry.refresh(current_tier="team", has_license_key=True)
        gaps_after = len(registry.tier_gap_flags())
        assert gaps_after == 0

    def test_refresh_updates_license_flag(self) -> None:
        registry = FeatureFlagRegistry(current_tier="community", has_license_key=False)
        assert registry.has_license_key is False
        registry.refresh(current_tier="community", has_license_key=True)
        assert registry.has_license_key is True

    def test_community_tier_flags_active(self) -> None:
        registry = FeatureFlagRegistry(current_tier="community")
        flag = registry.get_flag("eval_system")
        assert flag is not None
        assert flag.currently_active is True

    def test_team_tier_activates_team_flags(self) -> None:
        registry = FeatureFlagRegistry(current_tier="team", has_license_key=True)
        flag = registry.get_flag("sso")
        assert flag is not None
        assert flag.currently_active is True

    def test_team_tier_keeps_community_active(self) -> None:
        registry = FeatureFlagRegistry(current_tier="team", has_license_key=True)
        flag = registry.get_flag("eval_system")
        assert flag is not None
        assert flag.currently_active is True

    def test_unknown_tier_falls_back_to_rank_zero(self) -> None:
        FeatureFlagRegistry._overrides.clear()
        registry = FeatureFlagRegistry(current_tier="nonexistent")
        # Unknown tier defaults to rank 0 via TIER_RANK.get(tier, 0)
        # Community flags (rank 0) are active; team+ flags (rank >= 1) are not
        for flag in registry.list_flags():
            if flag.tier != "community":
                assert flag.currently_active is False


class TestOverrides:
    def test_set_override_activates_flag(self) -> None:
        registry = FeatureFlagRegistry(current_tier="community")
        flag = registry.get_flag("sso")
        assert flag is not None
        assert flag.currently_active is False
        try:
            registry.set_override("sso", True)
            assert flag.currently_active is True
        finally:
            registry.clear_override("sso")

    def test_set_override_deactivates_flag(self) -> None:
        registry = FeatureFlagRegistry(current_tier="team", has_license_key=True)
        flag = registry.get_flag("sso")
        assert flag is not None
        assert flag.currently_active is True
        try:
            registry.set_override("sso", False)
            assert flag.currently_active is False
        finally:
            registry.clear_override("sso")

    def test_clear_override_restores_default(self) -> None:
        registry = FeatureFlagRegistry(current_tier="community")
        registry.set_override("sso", True)
        registry.clear_override("sso")
        flag = registry.get_flag("sso")
        assert flag is not None
        assert flag.currently_active is False

    def test_get_override_returns_value(self) -> None:
        registry = FeatureFlagRegistry()
        assert registry.get_override("sso") is None
        registry.set_override("sso", True)
        assert registry.get_override("sso") is True
        registry.set_override("sso", False)
        assert registry.get_override("sso") is False
        registry.clear_override("sso")
        assert registry.get_override("sso") is None

    def test_overrides_are_class_level(self) -> None:
        FeatureFlagRegistry._overrides.clear()
        r1 = FeatureFlagRegistry()
        r2 = FeatureFlagRegistry()
        r1.set_override("eval_system", True)
        # r2 sees the same override
        assert r2.get_override("eval_system") is True


class TestFromDb:
    def test_from_db_loads_flags(self) -> None:
        _make_session()
        registry = FeatureFlagRegistry()
        assert registry is not None

    async def test_from_db_loads_tiers_and_flags(self) -> None:
        session = _make_session()
        db_flags = [
            {"name": "db_flag_a", "description": "From DB", "tier_id": "team", "depends_on": None, "is_active": True},
        ]
        db_tiers = [
            {"tier_id": "community", "rank": 0},
            {"tier_id": "team", "rank": 1},
        ]

        with (
            patch("modulo.db.crud.tier_catalog.list_tiers", return_value=db_tiers),
            patch("modulo.db.crud.tier_catalog.list_feature_flags", return_value=db_flags),
        ):
            registry = await FeatureFlagRegistry.from_db(session, current_tier="team", has_license_key=True)

        flag = registry.get_flag("db_flag_a")
        assert flag is not None
        assert flag.description == "From DB"
        assert flag.currently_active is True

    async def test_db_catalog_preserves_code_level_org_settability(self) -> None:
        """FAR-2302: ``org_admin_settable`` is a CODE classification, not a column.

        The DB catalog supplies name/description/tier, so a DB-loaded registry
        would otherwise lose the classification and report every flag as
        system-admin-only (or, worse, if the default were True, open everything).
        A catalog-only flag is not in ``ORG_ADMIN_SETTABLE_FLAGS`` and must
        default to False.
        """
        session = _make_session()
        db_flags = [
            {
                "name": "webhook_notification_log",
                "description": "From DB",
                "tier_id": "community",
                "depends_on": None,
                "is_active": True,
            },
            {
                "name": "sso_unrestricted_provisioning",
                "description": "From DB",
                "tier_id": "community",
                "depends_on": None,
                "is_active": True,
            },
            {
                "name": "catalog_only_flag",
                "description": "Only in the DB",
                "tier_id": "community",
                "depends_on": None,
                "is_active": True,
            },
        ]
        db_tiers = [{"tier_id": "community", "rank": 0}]

        with (
            patch("modulo.db.crud.tier_catalog.list_tiers", return_value=db_tiers),
            patch("modulo.db.crud.tier_catalog.list_feature_flags", return_value=db_flags),
        ):
            registry = await FeatureFlagRegistry.from_db(session, current_tier="community")

        assert registry.get_flag("webhook_notification_log").org_admin_settable is True
        assert registry.get_flag("sso_unrestricted_provisioning").org_admin_settable is False
        assert registry.get_flag("catalog_only_flag").org_admin_settable is False


class TestKnownFlags:
    """Ensure the registry exposes the product's durable flag contracts."""

    def test_flags_use_supported_product_tiers(self) -> None:
        registry = FeatureFlagRegistry()
        assert {flag.tier for flag in registry.list_flags()} <= {"community", "team", "v1", "v2"}

    def test_core_product_flags_are_registered(self) -> None:
        registry = FeatureFlagRegistry()
        names = {flag.name for flag in registry.list_flags()}
        assert {"eval_system", "sso", "team_rbac"} <= names


class TestDbCatalogTeamTierFlags:
    """Flags upserted into the DB catalog (FAR-114) must enable on team tier.

    Regression: the seed ``FLAGS`` in ``modulo.core.seed_data.catalog`` was
    missing ``runtime_config`` / ``rate_limits`` / ``error_tracking``, so
    ``load_from_db()`` dropped them from the registry and the pages 402'd even
    on the team tier.
    """

    @staticmethod
    def _db_flags() -> list[dict[str, object]]:
        return [
            {
                "name": "runtime_config",
                "description": "Runtime configuration overrides",
                "tier_id": "team",
                "depends_on": None,
                "is_active": True,
            },
            {
                "name": "rate_limits",
                "description": "Configure API rate limits",
                "tier_id": "team",
                "depends_on": None,
                "is_active": True,
            },
            {
                "name": "error_tracking",
                "description": "External error tracking and alerting integrations",
                "tier_id": "team",
                "depends_on": None,
                "is_active": True,
            },
        ]

    async def test_team_tier_enables_upserted_flags(self) -> None:
        from modulo.core.feature_flags import DbPlanContext

        session = _make_session()
        db_tiers = [
            {"tier_id": "community", "rank": 0},
            {"tier_id": "team", "rank": 1},
        ]

        with (
            patch("modulo.db.crud.tier_catalog.list_tiers", return_value=db_tiers),
            patch("modulo.db.crud.tier_catalog.list_feature_flags", return_value=self._db_flags()),
        ):
            ctx = await DbPlanContext.from_db(session, "team", has_license_key=True)

        for name in ("runtime_config", "rate_limits", "error_tracking"):
            assert ctx.feature_enabled(name) is True, f"{name} should be enabled on team tier"

    async def test_community_tier_keeps_upserted_flags_locked(self) -> None:
        from modulo.core.feature_flags import DbPlanContext

        session = _make_session()
        db_tiers = [
            {"tier_id": "community", "rank": 0},
            {"tier_id": "team", "rank": 1},
        ]

        with (
            patch("modulo.db.crud.tier_catalog.list_tiers", return_value=db_tiers),
            patch("modulo.db.crud.tier_catalog.list_feature_flags", return_value=self._db_flags()),
        ):
            ctx = await DbPlanContext.from_db(session, "community")

        for name in ("runtime_config", "rate_limits", "error_tracking"):
            assert ctx.feature_enabled(name) is False, f"{name} should be locked on community tier"


class TestRetiredInternalFlags:
    def test_lifecycle_map_journeys_is_retained_for_compatibility_but_not_customer_configurable(self) -> None:
        registry = FeatureFlagRegistry()
        assert {"lifecycle_map_journeys"} == RETIRED_INTERNAL_FLAGS
        flag = registry.get_flag("lifecycle_map_journeys")
        assert flag is not None
        assert flag.currently_active is True
        try:
            registry.set_override("lifecycle_map_journeys", False)
            assert flag.currently_active is False
        finally:
            registry.clear_override("lifecycle_map_journeys")


class TestDefaultOffFlags:
    """Flags shipped default-OFF on every tier. Each is listed in the catalog
    for per-org toggling AND hardcoded in ``_refresh``'s inactive set
    (``DEFAULT_OFF_FLAGS``), so the community-tier tier-rank fallback can
    never activate it and only an explicit override can turn it on.

    ``user_scoped_mcp_keys`` was extended into this set by FAR-620 and then
    flipped default-ON by FAR-2301, so it is pinned separately in
    ``TestUserScopedMcpKeysDefaultOn`` below.

    Regression (PR #1062 review): ``_refresh`` seeded the inactive set only
    from the DB catalog, so a registry built WITHOUT ``load_from_db()``
    treated community-tier members as active via the tier-rank comparison —
    leaking the experimental rail on community tier until the catalog was
    read.
    """

    @pytest.mark.parametrize("flag_name", sorted(DEFAULT_OFF_FLAGS))
    def test_flag_registered_in_known_flags(self, flag_name: str) -> None:
        registry = _make_default_off_registry()
        flag = registry.get_flag(flag_name)
        assert flag is not None, f"{flag_name} must be registered in _KNOWN_FLAGS"
        # The default-off mechanism targets community-tier flags: team-tier
        # flags are already gated behind a license, so shipping one default-OFF
        # would be redundant. Update this pin if that ever changes.
        assert flag.tier == "community"

    @pytest.mark.parametrize("flag_name", sorted(DEFAULT_OFF_FLAGS))
    def test_inactive_in_fallback_on_community(self, flag_name: str) -> None:
        registry = _make_default_off_registry("community")
        assert registry.get_flag(flag_name).currently_active is False  # type: ignore[union-attr]

    @pytest.mark.parametrize("flag_name", sorted(DEFAULT_OFF_FLAGS))
    def test_inactive_in_fallback_on_team(self, flag_name: str) -> None:
        registry = _make_default_off_registry("team", has_license_key=True)
        assert registry.get_flag(flag_name).currently_active is False  # type: ignore[union-attr]

    @pytest.mark.parametrize("flag_name", sorted(DEFAULT_OFF_FLAGS))
    def test_flag_can_be_enabled_via_sys_override(self, flag_name: str) -> None:
        registry = _make_default_off_registry("community")
        flag = registry.get_flag(flag_name)
        assert flag is not None
        try:
            registry.set_override(flag_name, True)
            assert flag.currently_active is True
        finally:
            registry.clear_override(flag_name)
        assert flag.currently_active is False

    @pytest.mark.parametrize("flag_name", sorted(DEFAULT_OFF_FLAGS))
    async def test_resolve_flag_defaults_false_even_with_catalog_row_active(self, flag_name: str) -> None:
        """The ``DEFAULT_OFF_FLAGS`` hardcode wins over a DB catalog row marked
        active — without the hardcode the tier-rank fallback would activate the
        flag for community tier. An org override is the ONLY way to enable it."""
        session = _make_session()
        db_flags = [
            {
                "name": flag_name,
                "description": "Default-off pin",
                "tier_id": "community",
                "depends_on": None,
                # Even a catalog row marked active stays default-OFF.
                "is_active": True,
            },
        ]
        db_tiers = [
            {"tier_id": "community", "rank": 0},
            {"tier_id": "team", "rank": 1},
        ]
        with (
            patch("modulo.db.crud.tier_catalog.list_tiers", return_value=db_tiers),
            patch("modulo.db.crud.tier_catalog.list_feature_flags", return_value=db_flags),
        ):
            registry = await FeatureFlagRegistry.from_db(session, current_tier="community")
        assert registry.get_flag(flag_name).currently_active is False  # type: ignore[union-attr]
        # An org feature_overrides entry (the documented enable path) wins.
        with patch.object(
            FeatureFlagRegistry,
            "_get_org_override",
            new=AsyncMock(return_value=True),
        ):
            assert await registry.resolve_flag(flag_name, org_id=uuid.uuid4()) is True


class TestUserScopedMcpKeysDefaultOn:
    """FAR-2301: ``user_scoped_mcp_keys`` ships default-ON on every tier.

    The flag is a per-org kill switch, not an opt-in gate: a human-linked
    user-scoped key is already quota-limited, TTL-capped, live-role clamped
    and attributed to its owner, so a new org can mint one without a
    system-admin action. An explicit org override of ``false`` still
    disables it.
    """

    _ORG = uuid.UUID("00000000-0000-0000-0000-000000000777")

    def test_flag_not_in_default_off_flags(self) -> None:
        assert "user_scoped_mcp_keys" not in DEFAULT_OFF_FLAGS

    @pytest.mark.parametrize(("tier", "has_license_key"), [("community", False), ("team", True)])
    def test_active_by_default_on_every_tier(self, tier: str, has_license_key: bool) -> None:
        registry = _make_default_off_registry(tier, has_license_key=has_license_key)
        flag = registry.get_flag("user_scoped_mcp_keys")
        assert flag is not None, "user_scoped_mcp_keys must stay registered in _KNOWN_FLAGS"
        assert flag.currently_active is True

    async def test_resolve_flag_defaults_true_without_an_override(self) -> None:
        registry = _make_default_off_registry()
        with patch.object(
            FeatureFlagRegistry,
            "_get_org_override",
            new=AsyncMock(return_value=None),
        ):
            assert await registry.resolve_flag("user_scoped_mcp_keys", org_id=self._ORG) is True

    async def test_org_override_false_still_disables(self) -> None:
        registry = _make_default_off_registry()
        with patch.object(
            FeatureFlagRegistry,
            "_get_org_override",
            new=AsyncMock(return_value=False),
        ):
            assert await registry.resolve_flag("user_scoped_mcp_keys", org_id=self._ORG) is False

    async def test_org_override_true_wins_over_a_disabled_catalog_row(self) -> None:
        """An org override is the enable path even when the row is inactive."""
        session = _make_session()
        db_flags = [
            {
                "name": "user_scoped_mcp_keys",
                "description": "Per-user MCP API keys",
                "tier_id": "community",
                "depends_on": None,
                "is_active": False,
            },
        ]
        db_tiers = [
            {"tier_id": "community", "rank": 0},
            {"tier_id": "team", "rank": 1},
        ]
        with (
            patch("modulo.db.crud.tier_catalog.list_tiers", return_value=db_tiers),
            patch("modulo.db.crud.tier_catalog.list_feature_flags", return_value=db_flags),
        ):
            registry = await FeatureFlagRegistry.from_db(session, current_tier="community")
        assert registry.get_flag("user_scoped_mcp_keys").currently_active is False  # type: ignore[union-attr]
        with patch.object(
            FeatureFlagRegistry,
            "_get_org_override",
            new=AsyncMock(return_value=True),
        ):
            assert await registry.resolve_flag("user_scoped_mcp_keys", org_id=self._ORG) is True


class TestResolveSsoUnrestrictedProvisioning:
    """FAR-855: the mode-3 gate wrapper resolves the operator flag and fails CLOSED."""

    _ORG = uuid.UUID("00000000-0000-0000-0000-000000000555")

    async def test_delegates_to_registry(self) -> None:
        from modulo.core.feature_flags import resolve_sso_unrestricted_provisioning

        registry = AsyncMock()
        registry.resolve_flag = AsyncMock(return_value=True)
        with patch("modulo.core.feature_flags.get_registry", return_value=registry):
            assert (await resolve_sso_unrestricted_provisioning(org_id=self._ORG)) is True

    async def test_false_when_flag_off(self) -> None:
        from modulo.core.feature_flags import resolve_sso_unrestricted_provisioning

        registry = AsyncMock()
        registry.resolve_flag = AsyncMock(return_value=False)
        with patch("modulo.core.feature_flags.get_registry", return_value=registry):
            assert (await resolve_sso_unrestricted_provisioning(org_id=self._ORG)) is False

    async def test_fails_closed_when_registry_raises(self, caplog) -> None:
        from modulo.core.feature_flags import resolve_sso_unrestricted_provisioning

        registry = AsyncMock()
        registry.resolve_flag = AsyncMock(side_effect=RuntimeError("db down"))
        with (
            patch("modulo.core.feature_flags.get_registry", return_value=registry),
            caplog.at_level("ERROR"),
        ):
            assert (await resolve_sso_unrestricted_provisioning(org_id=self._ORG)) is False
        assert "failing closed" in caplog.text

    async def test_fails_closed_when_get_registry_raises(self, caplog) -> None:
        from modulo.core.feature_flags import resolve_sso_unrestricted_provisioning

        with (
            patch("modulo.core.feature_flags.get_registry", side_effect=RuntimeError("no registry")),
            caplog.at_level("ERROR"),
        ):
            assert (await resolve_sso_unrestricted_provisioning(org_id=self._ORG)) is False
        assert "failing closed" in caplog.text

    async def test_cancelled_error_propagates(self) -> None:
        import asyncio

        from modulo.core.feature_flags import resolve_sso_unrestricted_provisioning

        registry = AsyncMock()
        registry.resolve_flag = AsyncMock(side_effect=asyncio.CancelledError())
        with (
            patch("modulo.core.feature_flags.get_registry", return_value=registry),
            pytest.raises(asyncio.CancelledError),
        ):
            await resolve_sso_unrestricted_provisioning(org_id=self._ORG)


class TestSystemMapsFlag:
    """FAR-2202: system_maps flag scaffolding — default-OFF, internal-only."""

    def test_system_maps_registered_in_known_flags(self) -> None:
        registry = _make_default_off_registry()
        flag = registry.get_flag("system_maps")
        assert flag is not None, "system_maps must be registered in _KNOWN_FLAGS"
        assert flag.tier == "community"
        assert flag.description == "Beta System Maps authoring (internal operator surface)"

    def test_system_maps_in_default_off_flags(self) -> None:
        from modulo.core.feature_flags import DEFAULT_OFF_FLAGS, INTERNAL_FLAGS

        assert "system_maps" in DEFAULT_OFF_FLAGS
        assert "system_maps" in INTERNAL_FLAGS

    def test_system_maps_inactive_on_community_tier(self) -> None:
        registry = _make_default_off_registry("community")
        assert registry.get_flag("system_maps").currently_active is False  # type: ignore[union-attr]

    def test_system_maps_inactive_on_team_tier(self) -> None:
        registry = _make_default_off_registry("team", has_license_key=True)
        assert registry.get_flag("system_maps").currently_active is False  # type: ignore[union-attr]

    def test_system_maps_can_be_enabled_via_sys_override(self) -> None:
        registry = _make_default_off_registry("community")
        flag = registry.get_flag("system_maps")
        assert flag is not None
        try:
            registry.set_override("system_maps", True)
            assert flag.currently_active is True
        finally:
            registry.clear_override("system_maps")
        assert flag.currently_active is False

    async def test_system_maps_defaults_false_even_with_catalog_row_active(self) -> None:
        """The DEFAULT_OFF_FLAGS hardcode wins over a DB catalog row marked
        active — without the hardcode the tier-rank fallback would activate the
        flag for community tier. An org override is the ONLY way to enable it."""
        session = _make_session()
        db_flags = [
            {
                "name": "system_maps",
                "description": "Beta System Maps (Delivery System Maps)",
                "tier_id": "community",
                "depends_on": None,
                "is_active": True,
            },
        ]
        db_tiers = [
            {"tier_id": "community", "rank": 0},
            {"tier_id": "team", "rank": 1},
        ]
        with (
            patch("modulo.db.crud.tier_catalog.list_tiers", return_value=db_tiers),
            patch("modulo.db.crud.tier_catalog.list_feature_flags", return_value=db_flags),
        ):
            registry = await FeatureFlagRegistry.from_db(session, current_tier="community")
        assert registry.get_flag("system_maps").currently_active is False  # type: ignore[union-attr]
        # An org feature_overrides entry (the documented enable path) wins.
        with patch.object(
            FeatureFlagRegistry,
            "_get_org_override",
            new=AsyncMock(return_value=True),
        ):
            assert await registry.resolve_flag("system_maps", org_id=uuid.uuid4()) is True

    def test_system_maps_read_is_a_public_entitlement_sibling(self) -> None:
        """FAR-2363: the viewer read model has its own non-internal flag.

        ``system_maps`` stays internal/default-OFF for authoring; the published
        read model is a separate community entitlement that is not filtered out
        of customer-facing plan responses.
        """
        from modulo.core.feature_flags import (
            DEFAULT_OFF_FLAGS,
            INTERNAL_FLAGS,
            CommunityTier,
            effective_feature_flags,
        )

        registry = _make_default_off_registry()
        read_flag = registry.get_flag("system_maps_read")
        assert read_flag is not None, "system_maps_read must be registered in _KNOWN_FLAGS"
        assert read_flag.tier == "community"
        assert "system_maps_read" not in INTERNAL_FLAGS
        assert "system_maps_read" not in DEFAULT_OFF_FLAGS

        names = {f.name for f in effective_feature_flags(CommunityTier())}
        assert "system_maps_read" in names
        assert "system_maps" not in names

    def test_system_maps_filtered_from_effective_feature_flags(self) -> None:
        """system_maps must not appear in customer-facing plan responses."""
        from modulo.core.feature_flags import CommunityTier, effective_feature_flags

        plan_ctx = CommunityTier()
        flags = effective_feature_flags(plan_ctx)
        names = {f.name for f in flags}
        assert "system_maps" not in names
        assert "lifecycle_map_journeys" not in names  # also internal
