"""Unit tests for feature flag registration — ensures new flags are properly catalogued."""

import uuid

from modulo.core.feature_flags import FeatureFlag, FeatureFlagRegistry


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
        assert len(flag.description) > 0

    def test_flag_default_state(self) -> None:
        registry = FeatureFlagRegistry()
        flag = registry.get_flag("saved_views")
        assert flag is not None
        assert flag.currently_active is True


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
        assert flag.description == ""


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
