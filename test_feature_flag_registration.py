import pytest
from modulo.core.feature_flags import FeatureFlagRegistry


class TestSavedViewsFlag:
    @pytest.fixture
    def registry(self) -> FeatureFlagRegistry:
        return FeatureFlagRegistry()

    def test_flag_is_registered(self, registry: FeatureFlagRegistry) -> None:
        flag = registry.get_flag("saved_views")
        assert flag is not None, "saved_views flag must be registered in _KNOWN_FLAGS"

    def test_flag_tier_is_free(self, registry: FeatureFlagRegistry) -> None:
        flag = registry.get_flag("saved_views")
        assert flag is not None
        assert flag.tier == "community"

    def test_flag_has_description(self, registry: FeatureFlagRegistry) -> None:
        flag = registry.get_flag("saved_views")
        assert flag is not None
        assert len(flag.description) > 0
