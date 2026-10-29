"""Unit tests for modulo.core.product_analytics — consent and settings model."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from modulo.core.product_analytics.consent import (
    apply_consent_action,
    coerce_switch_value,
    default_consent_state,
    get_product_analytics_block,
    is_egress_allowed,
    is_prompt_eligible,
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

    def test_dismissed_with_unparseable_prompted_at_is_eligible(self) -> None:
        consent = {
            **default_consent_state(),
            "prompted": PROMPTED_DISMISSED,
            "prompted_at": "not-a-timestamp",
        }
        assert is_prompt_eligible(consent) is True

    def test_dismissed_with_naive_datetime_is_treated_as_utc(self) -> None:
        naive_prompted_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)
        consent = {
            **default_consent_state(),
            "prompted": PROMPTED_DISMISSED,
            "prompted_at": naive_prompted_at,
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

    def test_dismiss_after_decline_writes_dismissed_and_stays_ineligible(self) -> None:
        """A later dismiss overwrites ``prompted=no`` and starts the cooldown.

        Decline is not literally sticky: a dismiss action replaces the stored
        ``prompted`` value with ``dismissed``. The org stays ineligible while the
        cooldown runs, which is what matters to the caller.
        """
        now = datetime.now(UTC)
        declined = apply_consent_action(default_consent_state(), "decline", now=now)
        assert is_prompt_eligible(declined) is False

        dismissed = apply_consent_action(declined, "dismiss", now=now)
        assert dismissed["prompted"] == PROMPTED_DISMISSED
        assert dismissed["level"] == LEVEL_OFF
        assert is_prompt_eligible(dismissed) is False


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
    @pytest.mark.parametrize(
        ("instance_on", "org_level", "expected"),
        [
            (False, LEVEL_OFF, False),
            (True, LEVEL_OFF, False),
            (False, LEVEL_ALL, False),
            (True, LEVEL_ALL, True),
        ],
        ids=["both-off", "instance-on-org-off", "instance-off-org-on", "both-on"],
    )
    def test_egress_requires_both_switches_on(self, instance_on: bool, org_level: str, expected: bool) -> None:
        assert is_egress_allowed(instance_on, org_level) is expected


# ---------------------------------------------------------------------------
# Stored/env switch-value coercion
# ---------------------------------------------------------------------------


class TestCoerceSwitchValue:
    """Only explicitly truthy values turn the switch ON; everything else is OFF."""

    @pytest.mark.parametrize("value", [True, 1, -2, 3.5, "1", "true", " YES "])
    def test_truthy_values_are_on(self, value: object) -> None:
        assert coerce_switch_value(value) is True

    @pytest.mark.parametrize(
        "value",
        [False, 0, 0.0, "", "false", "0", "no", [], {}, None],
        ids=[
            "false",
            "zero",
            "zero_float",
            "empty_str",
            "str_false",
            "str_zero",
            "str_no",
            "empty_list",
            "empty_dict",
            "none",
        ],
    )
    def test_everything_else_is_off(self, value: object) -> None:
        """Including the containers: ``bool(value)`` must never be the semantics."""
        assert coerce_switch_value(value) is False


# ---------------------------------------------------------------------------
# Partner carve-out
# ---------------------------------------------------------------------------


class TestPartnerLicenseClaim:
    """``product_analytics_required`` now drives the switch DEFAULT, not enforcement.

    The single canonical suite for the claim reader: the licence-resolution
    tests in ``test_license_enforcement.py`` exercise the resolver, not this
    reader, so every accepted/rejected input shape is pinned here once.
    """

    @pytest.mark.parametrize(
        ("license_data", "expected"),
        [
            (SimpleNamespace(claims={PARTNER_LICENSE_CLAIM: True}), True),
            (SimpleNamespace(claims={PARTNER_LICENSE_CLAIM: "true"}), True),
            (SimpleNamespace(claims={PARTNER_LICENSE_CLAIM: False}), False),
            (SimpleNamespace(claims={PARTNER_LICENSE_CLAIM: "false"}), False),
            (SimpleNamespace(claims={}), False),
            (SimpleNamespace(raw_payload={PARTNER_LICENSE_CLAIM: True}), True),
            (SimpleNamespace(raw_payload=None), False),
            ({PARTNER_LICENSE_CLAIM: True}, True),
            ({}, False),
            (None, False),
            ("not a dict", False),
        ],
        ids=[
            "claims-true",
            "claims-string-true",
            "claims-false",
            "claims-string-false",
            "claims-absent",
            "raw-payload-true",
            "raw-payload-none",
            "mapping-true",
            "mapping-empty",
            "none",
            "malformed",
        ],
    )
    def test_claim_must_be_explicitly_true(self, license_data: object, expected: bool) -> None:
        assert partner_license_requires_analytics(license_data) is expected

    def test_unreadable_license_is_fail_safe(self) -> None:
        """A licence whose claims access raises must log and return False, never escape."""

        class _RaisingClaims:
            @property
            def claims(self) -> dict[str, object]:
                raise RuntimeError("malformed licence")

        assert partner_license_requires_analytics(_RaisingClaims()) is False
