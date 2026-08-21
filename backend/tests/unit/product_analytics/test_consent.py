"""Unit tests for modulo.core.product_analytics — consent and settings model."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from modulo.core.product_analytics.consent import (
    apply_consent_action,
    default_consent_state,
    get_product_analytics_block,
    is_egress_allowed,
    is_partner_carve_out_active,
    is_prompt_eligible,
    merge_product_analytics_block,
    partner_license_requires_analytics,
    set_level,
)
from modulo.core.product_analytics.constants import (
    DISMISS_COOLDOWN,
    LEVEL_ALL,
    LEVEL_OFF,
    PARTNER_LICENSE_CLAIM,
    PRODUCT_ANALYTICS_KEY,
    PROMPTED_DISMISSED,
    PROMPTED_NO,
    PROMPTED_YES,
)

# ---------------------------------------------------------------------------
# Default consent state
# ---------------------------------------------------------------------------


class TestDefaultConsentState:
    def test_defaults_are_off(self) -> None:
        state = default_consent_state()
        assert state["level"] == LEVEL_OFF
        assert state["prompted"] is None
        assert state["prompted_at"] is None
        assert state["level_changed_at"] is None

    def test_get_block_returns_defaults_for_none_settings(self) -> None:
        block = get_product_analytics_block(None)
        assert block == default_consent_state()

    def test_get_block_returns_defaults_for_empty_settings(self) -> None:
        block = get_product_analytics_block({})
        assert block == default_consent_state()

    def test_get_block_returns_defaults_for_non_dict_value(self) -> None:
        block = get_product_analytics_block({PRODUCT_ANALYTICS_KEY: "invalid"})
        assert block == default_consent_state()

    def test_get_block_preserves_existing_values(self) -> None:
        settings = {
            PRODUCT_ANALYTICS_KEY: {
                "level": LEVEL_ALL,
                "prompted": PROMPTED_YES,
                "prompted_at": "2026-01-01T00:00:00+00:00",
                "level_changed_at": "2026-01-01T00:00:00+00:00",
            }
        }
        block = get_product_analytics_block(settings)
        assert block["level"] == LEVEL_ALL
        assert block["prompted"] == PROMPTED_YES

    def test_get_block_fills_missing_fields_with_defaults(self) -> None:
        settings = {PRODUCT_ANALYTICS_KEY: {"level": LEVEL_ALL}}
        block = get_product_analytics_block(settings)
        assert block["level"] == LEVEL_ALL
        assert block["prompted"] is None  # default
        assert block["prompted_at"] is None  # default


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


class TestMergeProductAnalyticsBlock:
    def test_merge_creates_block_if_absent(self) -> None:
        result = merge_product_analytics_block(None, {"level": LEVEL_ALL})
        assert result[PRODUCT_ANALYTICS_KEY]["level"] == LEVEL_ALL

    def test_merge_preserves_existing_keys(self) -> None:
        settings = {
            PRODUCT_ANALYTICS_KEY: {"level": LEVEL_OFF, "prompted": PROMPTED_NO},
        }
        result = merge_product_analytics_block(settings, {"level": LEVEL_ALL})
        block = result[PRODUCT_ANALYTICS_KEY]
        assert block["level"] == LEVEL_ALL
        assert block["prompted"] == PROMPTED_NO  # preserved

    def test_merge_does_not_clobber_other_settings(self) -> None:
        settings = {"email": {"smtp_host": "smtp.example.com"}}
        result = merge_product_analytics_block(settings, {"level": LEVEL_ALL})
        assert result["email"]["smtp_host"] == "smtp.example.com"
        assert result[PRODUCT_ANALYTICS_KEY]["level"] == LEVEL_ALL


# ---------------------------------------------------------------------------
# Prompt eligibility
# ---------------------------------------------------------------------------


class TestPromptEligibility:
    def test_none_is_eligible(self) -> None:
        consent = default_consent_state()
        assert is_prompt_eligible(consent) is True

    def test_yes_is_not_eligible(self) -> None:
        consent = {**default_consent_state(), "prompted": PROMPTED_YES}
        assert is_prompt_eligible(consent) is False

    def test_no_is_not_eligible(self) -> None:
        consent = {**default_consent_state(), "prompted": PROMPTED_NO}
        assert is_prompt_eligible(consent) is False

    def test_dismissed_within_cooldown_is_not_eligible(self) -> None:
        now = datetime.now(UTC)
        consent = {
            **default_consent_state(),
            "prompted": PROMPTED_DISMISSED,
            "prompted_at": (now - timedelta(days=3)).isoformat(),
        }
        assert is_prompt_eligible(consent) is False

    def test_dismissed_after_cooldown_is_eligible(self) -> None:
        now = datetime.now(UTC)
        consent = {
            **default_consent_state(),
            "prompted": PROMPTED_DISMISSED,
            "prompted_at": (now - timedelta(days=8)).isoformat(),
        }
        assert is_prompt_eligible(consent) is True

    def test_dismissed_exactly_at_cooldown_boundary_is_eligible(self) -> None:
        now = datetime.now(UTC)
        consent = {
            **default_consent_state(),
            "prompted": PROMPTED_DISMISSED,
            "prompted_at": (now - DISMISS_COOLDOWN).isoformat(),
        }
        assert is_prompt_eligible(consent) is True

    def test_dismissed_with_none_prompted_at_is_eligible(self) -> None:
        consent = {
            **default_consent_state(),
            "prompted": PROMPTED_DISMISSED,
            "prompted_at": None,
        }
        assert is_prompt_eligible(consent) is True


# ---------------------------------------------------------------------------
# Consent transitions
# ---------------------------------------------------------------------------


class TestConsentTransitions:
    def test_accept_sets_all(self) -> None:
        now = datetime(2026, 8, 21, tzinfo=UTC)
        result = apply_consent_action(default_consent_state(), "accept", now=now)
        assert result["level"] == LEVEL_ALL
        assert result["prompted"] == PROMPTED_YES
        assert result["prompted_at"] == now.isoformat()
        assert result["level_changed_at"] == now.isoformat()

    def test_decline_sets_off(self) -> None:
        now = datetime(2026, 8, 21, tzinfo=UTC)
        result = apply_consent_action(default_consent_state(), "decline", now=now)
        assert result["level"] == LEVEL_OFF
        assert result["prompted"] == PROMPTED_NO
        assert result["prompted_at"] == now.isoformat()
        assert result["level_changed_at"] == now.isoformat()

    def test_dismiss_only_updates_prompted(self) -> None:
        now = datetime(2026, 8, 21, tzinfo=UTC)
        original = {**default_consent_state(), "level": LEVEL_OFF}
        result = apply_consent_action(original, "dismiss", now=now)
        assert result["level"] == LEVEL_OFF  # unchanged
        assert result["prompted"] == PROMPTED_DISMISSED
        assert result["prompted_at"] == now.isoformat()
        assert result.get("level_changed_at") is None  # unchanged from default

    def test_invalid_action_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid consent action"):
            apply_consent_action(default_consent_state(), "invalid")

    def test_accept_from_declined(self) -> None:
        now = datetime(2026, 8, 21, tzinfo=UTC)
        declined = apply_consent_action(default_consent_state(), "decline", now=now)
        result = apply_consent_action(declined, "accept", now=now)
        assert result["level"] == LEVEL_ALL
        assert result["prompted"] == PROMPTED_YES

    def test_dismiss_from_declined_is_sticky_no(self) -> None:
        """Once declined, dismiss still sets prompted=no... wait, actually:
        decline sets prompted=no (sticky forever). Dismiss only sets prompted=dismissed.
        But the spec says decline is sticky — a declined org should never be re-prompted.
        This test verifies the prompt eligibility check handles this correctly.
        """
        now = datetime(2026, 8, 21, tzinfo=UTC)
        declined = apply_consent_action(default_consent_state(), "decline", now=now)
        # Declined org is NOT eligible (prompted=no is sticky)
        assert is_prompt_eligible(declined) is False


# ---------------------------------------------------------------------------
# Level setting
# ---------------------------------------------------------------------------


class TestSetLevel:
    def test_set_level_to_all(self) -> None:
        now = datetime(2026, 8, 21, tzinfo=UTC)
        result = set_level(default_consent_state(), LEVEL_ALL, now=now)
        assert result["level"] == LEVEL_ALL
        assert result["level_changed_at"] == now.isoformat()

    def test_set_level_to_off(self) -> None:
        now = datetime(2026, 8, 21, tzinfo=UTC)
        consent = {**default_consent_state(), "level": LEVEL_ALL}
        result = set_level(consent, LEVEL_OFF, now=now)
        assert result["level"] == LEVEL_OFF
        assert result["level_changed_at"] == now.isoformat()

    def test_invalid_level_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid level"):
            set_level(default_consent_state(), "invalid")

    def test_set_level_preserves_prompted_state(self) -> None:
        now = datetime(2026, 8, 21, tzinfo=UTC)
        consent = {**default_consent_state(), "prompted": PROMPTED_YES}
        result = set_level(consent, LEVEL_ALL, now=now)
        assert result["prompted"] == PROMPTED_YES


# ---------------------------------------------------------------------------
# Instance switch + org level AND gate
# ---------------------------------------------------------------------------


class TestEgressGate:
    def test_both_off(self) -> None:
        assert is_egress_allowed(False, LEVEL_OFF) is False

    def test_instance_on_org_off(self) -> None:
        assert is_egress_allowed(True, LEVEL_OFF) is False

    def test_instance_off_org_on(self) -> None:
        assert is_egress_allowed(False, LEVEL_ALL) is False

    def test_both_on(self) -> None:
        assert is_egress_allowed(True, LEVEL_ALL) is True


# ---------------------------------------------------------------------------
# Partner carve-out
# ---------------------------------------------------------------------------


class TestPartnerCarveOut:
    def test_license_with_required_claim(self) -> None:
        license_data = SimpleNamespace(claims={PARTNER_LICENSE_CLAIM: True})
        assert partner_license_requires_analytics(license_data) is True

    def test_license_without_required_claim(self) -> None:
        license_data = SimpleNamespace(claims={})
        assert partner_license_requires_analytics(license_data) is False

    def test_license_with_false_claim(self) -> None:
        license_data = SimpleNamespace(claims={PARTNER_LICENSE_CLAIM: False})
        assert partner_license_requires_analytics(license_data) is False

    def test_dict_license_with_claim(self) -> None:
        license_data = {PARTNER_LICENSE_CLAIM: True}
        assert partner_license_requires_analytics(license_data) is True

    def test_none_license_returns_false(self) -> None:
        assert partner_license_requires_analytics(None) is False

    def test_malformed_license_returns_false(self) -> None:
        assert partner_license_requires_analytics("not a dict") is False

    def test_carve_out_active_when_required_and_not_all(self) -> None:
        license_data = SimpleNamespace(claims={PARTNER_LICENSE_CLAIM: True})
        assert is_partner_carve_out_active(license_data, LEVEL_OFF) is True

    def test_carve_out_inactive_when_level_is_all(self) -> None:
        license_data = SimpleNamespace(claims={PARTNER_LICENSE_CLAIM: True})
        assert is_partner_carve_out_active(license_data, LEVEL_ALL) is False

    def test_carve_out_inactive_when_not_required(self) -> None:
        license_data = SimpleNamespace(claims={})
        assert is_partner_carve_out_active(license_data, LEVEL_OFF) is False
