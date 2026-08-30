import pytest

from modulo.core.feature_flags import FeatureFlag, FeatureFlagRegistry


@pytest.fixture(autouse=True)
def _clean_global_overrides() -> None:
    """``_overrides`` is class-level and shared across instances; isolate each
    test so the lifecycle/override tests cannot pollute each other."""
    FeatureFlagRegistry._overrides.clear()
    yield
    FeatureFlagRegistry._overrides.clear()


class TestSavedViewsFlag:
    def test_flag_is_registered(self) -> None:
        registry = FeatureFlagRegistry()
        flag = registry.get_flag("saved_views")
        assert flag is not None, "saved_views flag must be registered in _KNOWN_FLAGS"

    def test_flag_tier_is_free(self) -> None:
        registry = FeatureFlagRegistry()
        flag = registry.get_flag("saved_views")
        assert flag is not None
        assert flag.tier == "community"

    def test_flag_has_description(self) -> None:
        registry = FeatureFlagRegistry()
        flag = registry.get_flag("saved_views")
        assert flag is not None
        assert flag.description

    def test_flag_default_state(self) -> None:
        registry = FeatureFlagRegistry()
        flag = registry.get_flag("saved_views")
        assert flag is not None
        assert flag.currently_active is False


class TestMobileSidebarRailFlag:
    def test_flag_is_registered(self) -> None:
        registry = FeatureFlagRegistry()
        flag = registry.get_flag("mobile_sidebar_rail")
        assert flag is not None, "mobile_sidebar_rail flag must be registered in _KNOWN_FLAGS"

    def test_flag_tier_is_community(self) -> None:
        registry = FeatureFlagRegistry()
        flag = registry.get_flag("mobile_sidebar_rail")
        assert flag is not None
        assert flag.tier == "community"

    def test_flag_has_description(self) -> None:
        registry = FeatureFlagRegistry()
        flag = registry.get_flag("mobile_sidebar_rail")
        assert flag is not None
        assert flag.description


class TestUserManagementFlag:
    """FAR-462: basic user management is a community-tier feature — the Users
    admin view must not be tier-locked on any plan."""

    def test_flag_is_registered(self) -> None:
        registry = FeatureFlagRegistry()
        flag = registry.get_flag("user_management")
        assert flag is not None, "user_management flag must be registered in _KNOWN_FLAGS"

    def test_flag_tier_is_community(self) -> None:
        registry = FeatureFlagRegistry()
        flag = registry.get_flag("user_management")
        assert flag is not None
        assert flag.tier == "community"

    def test_flag_active_on_community_tier(self) -> None:
        registry = FeatureFlagRegistry(current_tier="community")
        flag = registry.get_flag("user_management")
        assert flag is not None
        assert flag.currently_active is True

    def test_flag_active_on_team_tier(self) -> None:
        registry = FeatureFlagRegistry(current_tier="team", has_license_key=True)
        flag = registry.get_flag("user_management")
        assert flag is not None
        assert flag.currently_active is True

    def test_flag_has_description(self) -> None:
        registry = FeatureFlagRegistry()
        flag = registry.get_flag("user_management")
        assert flag is not None
        assert flag.description


class TestFeatureFlagModel:
    def test_creates_with_minimal_fields(self) -> None:
        flag = FeatureFlag(name="test_flag", tier="community", description="test")
        assert flag.name == "test_flag"
        assert flag.tier == "community"
        assert flag.description == "test"
        assert flag.depends_on is None
        assert flag.currently_active is False

    def test_currently_active_can_be_set_false(self) -> None:
        flag = FeatureFlag(name="inactive_flag", tier="team", description="test", currently_active=False)
        assert flag.currently_active is False

    def test_currently_active_reflects_tier_comparison(self) -> None:
        flag = FeatureFlag(name="team_flag", tier="team", description="test")
        assert flag.currently_active is False

    def test_non_blocked_flag_with_currently_active(self) -> None:
        flag = FeatureFlag(name="active_flag", tier="community", description="test", currently_active=True)
        assert flag.currently_active is True

    def test_depends_on_relationship(self) -> None:
        child = FeatureFlag(name="child_flag", tier="team", description="test", depends_on="parent_flag")
        assert child.depends_on == "parent_flag"

    def test_description_defaults_to_empty(self) -> None:
        flag = FeatureFlag(name="no_desc", tier="community", description="")
        assert not flag.description


class TestRegistryLifecycle:
    """Sync registry API surfaced to the admin UI and plan-context code:
    ``refresh``, ``set_override``/``get_override``/``clear_override``, and the
    ``tier_gap_flags`` community-gap report."""

    def test_refresh_recomputes_active_state_from_tier(self) -> None:
        registry = FeatureFlagRegistry(current_tier="community")
        sso = registry.get_flag("sso")
        assert sso is not None
        assert sso.tier == "team"
        assert sso.currently_active is False

        registry.refresh("team", has_license_key=True)
        assert registry.current_tier == "team"
        assert registry.has_license_key is True
        assert sso.currently_active is True

        registry.refresh("community", has_license_key=False)
        assert registry.has_license_key is False
        assert sso.currently_active is False

    def test_set_override_forces_flag_active_at_lower_tier(self) -> None:
        registry = FeatureFlagRegistry(current_tier="community")
        registry.set_override("sso", True)

        assert registry.get_override("sso") is True
        sso = registry.get_flag("sso")
        assert sso is not None
        assert sso.currently_active is True

    def test_clear_override_restores_tier_computed_state(self) -> None:
        registry = FeatureFlagRegistry(current_tier="community")
        registry.set_override("sso", True)
        assert registry.get_flag("sso").currently_active is True

        registry.clear_override("sso")
        assert registry.get_override("sso") is None
        assert registry.get_flag("sso").currently_active is False

    def test_get_override_returns_none_when_unset(self) -> None:
        registry = FeatureFlagRegistry()
        assert registry.get_override("sso") is None

    def test_tier_gap_flags_empty_when_not_community(self) -> None:
        registry = FeatureFlagRegistry(current_tier="team")
        assert not registry.tier_gap_flags()

    def test_tier_gap_flags_reports_inactive_team_flags(self) -> None:
        registry = FeatureFlagRegistry(current_tier="community")
        gaps = registry.tier_gap_flags()

        assert gaps, "expected at least one inactive above-community flag"
        assert all(flag.tier != "community" for flag in gaps)
        assert all(not flag.currently_active for flag in gaps)
        assert any(flag.name == "sso" for flag in gaps)

    def test_tier_gap_flags_exclude_override_active_flags(self) -> None:
        registry = FeatureFlagRegistry(current_tier="community")
        registry.set_override("sso", True)

        gaps = {flag.name for flag in registry.tier_gap_flags()}
        assert "sso" not in gaps


class TestAllFlagsRegistered:
    def test_all_known_flags_have_tier(self) -> None:
        registry = FeatureFlagRegistry()
        for flag in registry.list_flags():
            assert flag.tier in {"community", "team", "v1", "v2"}, f"Flag {flag.name} has unknown tier {flag.tier}"

    def test_all_known_flags_have_unique_names(self) -> None:
        registry = FeatureFlagRegistry()
        names = [flag.name for flag in registry.list_flags()]
        assert len(names) == len(set(names)), "Duplicate flag names detected"

    def test_flags_with_depends_on_refer_to_existing_flags(self) -> None:
        registry = FeatureFlagRegistry()
        names = {flag.name for flag in registry.list_flags()}
        for flag in registry.list_flags():
            if flag.depends_on:
                assert flag.depends_on in names, f"Flag {flag.name} depends on unknown flag {flag.depends_on}"
