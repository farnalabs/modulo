import pytest
from modulo.core.feature_flags import FeatureFlag, FeatureFlagRegistry


class TestSavedViewsFlag:
    @pytest.fixture
    def registry(self) -> FeatureFlagRegistry:
        return FeatureFlagRegistry()

    def test_flag_is_registered(self, registry: FeatureFlagRegistry) -> None:
        flag = registry.get_flag("saved_views")
        assert flag is not None

    def test_flag_tier_is_free(self, registry: FeatureFlagRegistry) -> None:
        flag = registry.get_flag("saved_views")
        assert flag is not None
        assert flag.tier == "community"

    def test_flag_has_description(self, registry: FeatureFlagRegistry) -> None:
        flag = registry.get_flag("saved_views")
        assert flag is not None
        assert len(flag.description) > 0

    def test_flag_default_state(self, registry: FeatureFlagRegistry) -> None:
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

    def test_currently_active_defaults_to_false(self) -> None:
        flag = FeatureFlag(name="inactive_flag", tier="team", description="test")
        assert flag.currently_active is False

    def test_currently_active_can_be_set_true(self) -> None:
        flag = FeatureFlag(name="active_flag", tier="community", description="test", currently_active=True)
        assert flag.currently_active is True

    def test_depends_on_relationship(self) -> None:
        child = FeatureFlag(name="child_flag", tier="team", description="test", depends_on=["parent_flag"])
        assert child.depends_on == ["parent_flag"]

    def test_depends_on_defaults_to_none(self) -> None:
        flag = FeatureFlag(name="no_deps", tier="community", description="test")
        assert flag.depends_on is None

    def test_description_is_required(self) -> None:
        with pytest.raises(TypeError):
            FeatureFlag(name="no_desc", tier="community")


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
                deps = flag.depends_on if isinstance(flag.depends_on, list) else [flag.depends_on]
                for dep in deps:
                    assert dep in names, f"Flag {flag.name} depends on unknown flag {dep}"
