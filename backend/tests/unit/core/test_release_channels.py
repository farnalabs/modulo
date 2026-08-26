"""Unit tests for modulo.core.release_channels.

``should_rollback`` / ``resolve_channel_binding`` are already exercised through
the FAR-402 P6 snapshot tests; this file pins the rest of the release-channel
contract that has no other coverage: ``is_routable_channel`` (only ``stable`` /
``canary`` are routable, ``none`` is deliberately excluded), the empty-channel
``error_rate_pct`` guard, and the binding-resolution normalisation edges
(whitespace/case, non-string values, custom defaults).
"""

import pytest

from modulo.core.release_channels import (
    DEFAULT_RELEASE_CHANNEL,
    RELEASE_CHANNEL_MAX_LEN,
    ROUTABLE_RELEASE_CHANNELS,
    VALID_RELEASE_CHANNELS,
    ChannelMetrics,
    ReleaseChannelThresholds,
    is_routable_channel,
    resolve_channel_binding,
    should_rollback,
)


class TestIsRoutableChannel:
    def test_routable_channels(self) -> None:
        assert is_routable_channel("stable")
        assert is_routable_channel("canary")

    def test_none_is_never_routable(self) -> None:
        assert not is_routable_channel("none")
        assert "none" not in ROUTABLE_RELEASE_CHANNELS
        assert "none" in VALID_RELEASE_CHANNELS

    def test_unknown_and_miscased_values_are_not_routable(self) -> None:
        assert not is_routable_channel("bogus")
        assert not is_routable_channel("STABLE")


class TestChannelMetricsErrorRate:
    def test_empty_channel_never_reports_an_error_rate(self) -> None:
        assert ChannelMetrics(observed_runs=0, error_runs=3).error_rate_pct == 0.0

    def test_error_rate_is_error_runs_over_observed_runs(self) -> None:
        assert ChannelMetrics(observed_runs=3, error_runs=1).error_rate_pct == pytest.approx(100.0 / 3)


class TestResolveChannelBinding:
    def test_value_is_trimmed_and_lowercased(self) -> None:
        assert resolve_channel_binding({"release_channel": "  STABLE "}) == "stable"

    def test_non_string_value_falls_back_to_default(self) -> None:
        assert resolve_channel_binding({"release_channel": 5}) == DEFAULT_RELEASE_CHANNEL

    def test_custom_default_is_used_for_invalid_binding(self) -> None:
        assert resolve_channel_binding({"release_channel": "weird"}, default="canary") == "canary"

    def test_explicit_none_binding_is_preserved(self) -> None:
        assert resolve_channel_binding({"release_channel": "none"}) == "none"


class TestShouldRollbackThresholds:
    def test_error_rate_uses_configured_threshold(self) -> None:
        thresholds = ReleaseChannelThresholds(rollback_threshold_error_rate_pct=25.0, rollback_min_observed_runs=4)
        assert should_rollback(ChannelMetrics(observed_runs=4, error_runs=1), thresholds)
        assert not should_rollback(ChannelMetrics(observed_runs=4, error_runs=0), thresholds)


class TestChannelContractConstants:
    def test_column_length_bound_is_ten(self) -> None:
        assert RELEASE_CHANNEL_MAX_LEN == 10

    def test_default_channel_is_none(self) -> None:
        assert DEFAULT_RELEASE_CHANNEL == "none"
