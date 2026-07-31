import pytest
from modulo.core.feature_flags import FeatureFlag, FeatureFlagRegistry


@pytest.fixture
def registry() -> FeatureFlagRegistry:
    return FeatureFlagRegistry()


class TestSavedViewsFlag:
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


class TestFeatureFlagModel:
    def test_creates_with_minimal_fields(self) -> None:
        flag = FeatureFlag(name="test_flag", description="", tier="community")
        assert flag.name == "test_flag"
        assert flag.tier == "community"
        assert flag.description == ""
        assert flag.depends_on is None
        assert flag.currently_active is False

    def test_currently_active_defaults_false(self) -> None:
        flag = FeatureFlag(name="new_flag", description="desc", tier="team")
        assert flag.currently_active is False

    def test_currently_active_can_be_set(self) -> None:
        flag = FeatureFlag(name="active_flag", description="desc", tier="community", currently_active=True)
        assert flag.currently_active is True

    def test_depends_on_relationship(self) -> None:
        child = FeatureFlag(name="child_flag", description="desc", tier="team", depends_on=["parent_flag"])
        assert child.depends_on == ["parent_flag"]

    def test_description_stored(self) -> None:
        flag = FeatureFlag(name="desc_flag", description="Some description", tier="community")
        assert flag.description == "Some description"


class TestAllFlagsRegistered:
    def test_all_known_flags_have_tier(self, registry: FeatureFlagRegistry) -> None:
        for flag in registry.list_flags():
            assert flag.tier in {"community", "team"}, f"Flag {flag.name} has unknown tier {flag.tier}"

    def test_all_known_flags_have_unique_names(self, registry: FeatureFlagRegistry) -> None:
        names = [flag.name for flag in registry.list_flags()]
        assert len(names) == len(set(names)), "Duplicate flag names detected"

    def test_flags_with_depends_on_refer_to_existing_flags(self, registry: FeatureFlagRegistry) -> None:
        names = {flag.name for flag in registry.list_flags()}
        for flag in registry.list_flags():
            for dep in flag.depends_on or []:
                assert dep in names, f"Flag {flag.name} depends on unknown flag {dep}"
