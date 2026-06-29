"""Unit tests for feature flag registration — ensures new flags are properly catalogued."""

from modulo.core.feature_flags import FeatureFlagRegistry


class TestSavedViewsFlag:
    def test_flag_is_registered(self) -> None:
        registry = FeatureFlagRegistry()
        flag = registry.get_flag("saved_views")
        assert flag is not None, "saved_views flag must be registered in _KNOWN_FLAGS"

    def test_flag_tier_is_free(self) -> None:
        registry = FeatureFlagRegistry()
        flag = registry.get_flag("saved_views")
        assert flag is not None
        assert flag.tier == "free"

    def test_flag_has_description(self) -> None:
        registry = FeatureFlagRegistry()
        flag = registry.get_flag("saved_views")
        assert flag is not None
        assert len(flag.description) > 0
