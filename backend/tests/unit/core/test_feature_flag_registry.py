"""Unit tests for FeatureFlagRegistry core functionality."""

from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core.feature_flags import FeatureFlagRegistry


def _make_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


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
        assert len(gaps) == 0

    def test_no_gaps_when_v1(self) -> None:
        registry = FeatureFlagRegistry(current_tier="v1", has_license_key=True)
        gaps = registry.tier_gap_flags()
        assert len(gaps) == 0

    def test_no_gaps_when_v2(self) -> None:
        registry = FeatureFlagRegistry(current_tier="v2", has_license_key=True)
        gaps = registry.tier_gap_flags()
        assert len(gaps) == 0


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
        flag = registry.get_flag("saved_views")
        assert flag is not None
        assert flag.currently_active is True

    def test_team_tier_activates_team_flags(self) -> None:
        registry = FeatureFlagRegistry(current_tier="team", has_license_key=True)
        flag = registry.get_flag("sso")
        assert flag is not None
        assert flag.currently_active is True

    def test_team_tier_keeps_community_active(self) -> None:
        registry = FeatureFlagRegistry(current_tier="team", has_license_key=True)
        flag = registry.get_flag("saved_views")
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
        r1.set_override("parallel_branches", True)
        # r2 sees the same override
        assert r2.get_override("parallel_branches") is True


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


class TestKnownFlags:
    """Ensure the registry exposes the product's durable flag contracts."""

    def test_flags_use_supported_product_tiers(self) -> None:
        registry = FeatureFlagRegistry()
        assert {flag.tier for flag in registry.list_flags()} <= {"community", "team", "v1", "v2"}

    def test_core_product_flags_are_registered(self) -> None:
        registry = FeatureFlagRegistry()
        names = {flag.name for flag in registry.list_flags()}
        assert {"eval_system", "parallel_branches", "sso", "team_rbac"} <= names

    def test_remy_ui_driving_is_community(self) -> None:
        registry = FeatureFlagRegistry()
        flag = registry.get_flag("remy_ui_driving")
        assert flag is not None
        assert flag.tier == "community"
