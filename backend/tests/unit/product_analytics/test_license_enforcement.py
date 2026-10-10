"""Unit tests for product analytics license enforcement."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from modulo.core.product_analytics import consent as consent_module
from modulo.core.product_analytics.consent import is_license_enforcement_enabled
from modulo.core.product_analytics.license_enforcement import (
    PRODUCT_ANALYTICS_REQUIRED_KEY,
    _extract_org_license,
    _license_requires_analytics,
    _org_analytics_level,
    check_product_analytics_requirement,
    is_enforcement_active,
    should_degrade_to_community,
)


def _license_data(
    *,
    tier: str = "team",
    payload: dict | None = None,
) -> SimpleNamespace:
    """Create a minimal LicenseData-like object for testing."""
    raw_payload: dict = {"tier": tier}
    if payload:
        raw_payload.update(payload)
    return SimpleNamespace(
        tier=tier,
        features=[],
        expires_at="2099-01-01T00:00:00+00:00",
        org_id="test-org",
        raw_payload=raw_payload,
        raw_key="fake-key",
    )


def _org(settings_json: dict | None = None) -> SimpleNamespace:
    """Create a minimal org-like object for testing."""
    return SimpleNamespace(settings_json=settings_json or {})


class TestExtractOrgLicense:
    def test_no_settings(self) -> None:
        org = SimpleNamespace(settings_json=None)
        assert _extract_org_license(org) is None

    def test_empty_settings(self) -> None:
        assert _extract_org_license(_org({})) is None

    def test_no_license_key(self) -> None:
        assert _extract_org_license(_org({"other": "value"})) is None

    def test_valid_license_key(self) -> None:
        license_data = _license_data()
        with patch("modulo.core.product_analytics.license_enforcement.parse_and_verify") as mock_verify:
            mock_verify.return_value = SimpleNamespace(valid=True, license_data=license_data)
            result = _extract_org_license(_org({"license_key": "fake-key"}))
        assert result is license_data
        mock_verify.assert_called_once_with("fake-key")

    def test_invalid_license_key(self) -> None:
        with patch("modulo.core.product_analytics.license_enforcement.parse_and_verify") as mock_verify:
            mock_verify.return_value = SimpleNamespace(valid=False, license_data=None)
            assert _extract_org_license(_org({"license_key": "bad-key"})) is None

    def test_parse_exception(self) -> None:
        with patch(
            "modulo.core.product_analytics.license_enforcement.parse_and_verify",
            side_effect=RuntimeError("boom"),
        ):
            assert _extract_org_license(_org({"license_key": "key"})) is None


class TestLicenseRequiresAnalytics:
    def test_no_license(self) -> None:
        assert _license_requires_analytics(None) is False

    def test_no_claim(self) -> None:
        data = _license_data()
        assert _license_requires_analytics(data) is False

    def test_claim_true(self) -> None:
        data = _license_data(payload={PRODUCT_ANALYTICS_REQUIRED_KEY: True})
        assert _license_requires_analytics(data) is True

    def test_claim_string_true(self) -> None:
        data = _license_data(payload={PRODUCT_ANALYTICS_REQUIRED_KEY: "true"})
        assert _license_requires_analytics(data) is True

    def test_claim_false(self) -> None:
        data = _license_data(payload={PRODUCT_ANALYTICS_REQUIRED_KEY: False})
        assert _license_requires_analytics(data) is False

    def test_raw_payload_not_dict(self) -> None:
        data = SimpleNamespace(raw_payload="not-a-dict")
        assert _license_requires_analytics(data) is False


class TestOrgAnalyticsLevel:
    def test_no_settings(self) -> None:
        org = SimpleNamespace(settings_json=None)
        assert _org_analytics_level(org) is None

    def test_no_product_analytics_key(self) -> None:
        assert _org_analytics_level(_org({})) is None

    def test_level_all(self) -> None:
        org = _org({"product_analytics": {"level": "all"}})
        assert _org_analytics_level(org) == "all"

    def test_level_off(self) -> None:
        org = _org({"product_analytics": {"level": "off"}})
        assert _org_analytics_level(org) == "off"

    def test_level_unset(self) -> None:
        org = _org({"product_analytics": {}})
        assert _org_analytics_level(org) is None

    def test_level_unknown_value(self) -> None:
        org = _org({"product_analytics": {"level": "unknown"}})
        assert _org_analytics_level(org) is None


class TestCheckProductAnalyticsRequirement:
    def test_no_license_key_not_required(self) -> None:
        org = _org({})
        assert check_product_analytics_requirement(org) == "not_required"

    def test_paid_license_no_claim_not_required(self) -> None:
        org = _org({"license_key": "key"})
        with patch("modulo.core.product_analytics.license_enforcement.parse_and_verify") as mock_verify:
            mock_verify.return_value = SimpleNamespace(valid=True, license_data=_license_data(tier="team"))
            assert check_product_analytics_requirement(org) == "not_required"

    def test_license_with_claim_level_all_satisfied(self) -> None:
        org = _org(
            {
                "license_key": "key",
                "product_analytics": {"level": "all"},
            }
        )
        with patch("modulo.core.product_analytics.license_enforcement.parse_and_verify") as mock_verify:
            mock_verify.return_value = SimpleNamespace(
                valid=True,
                license_data=_license_data(payload={PRODUCT_ANALYTICS_REQUIRED_KEY: True}),
            )
            assert check_product_analytics_requirement(org) == "satisfied"

    def test_license_with_claim_level_unset_pending(self) -> None:
        org = _org({"license_key": "key"})
        with patch("modulo.core.product_analytics.license_enforcement.parse_and_verify") as mock_verify:
            mock_verify.return_value = SimpleNamespace(
                valid=True,
                license_data=_license_data(payload={PRODUCT_ANALYTICS_REQUIRED_KEY: True}),
            )
            assert check_product_analytics_requirement(org) == "pending"

    def test_license_with_claim_level_off_degraded(self) -> None:
        org = _org(
            {
                "license_key": "key",
                "product_analytics": {"level": "off"},
            }
        )
        with patch("modulo.core.product_analytics.license_enforcement.parse_and_verify") as mock_verify:
            mock_verify.return_value = SimpleNamespace(
                valid=True,
                license_data=_license_data(payload={PRODUCT_ANALYTICS_REQUIRED_KEY: True}),
            )
            assert check_product_analytics_requirement(org) == "degraded"


class TestIsEnforcementActive:
    async def test_kill_switch_absent_enforced(self) -> None:
        session = AsyncMock()
        with patch(
            "modulo.db.crud.system_config.get_config",
            return_value=None,
        ):
            assert await is_enforcement_active(session) is True

    async def test_kill_switch_false_enforced(self) -> None:
        session = AsyncMock()
        with patch(
            "modulo.db.crud.system_config.get_config",
            return_value=SimpleNamespace(value=False),
        ):
            assert await is_enforcement_active(session) is True

    async def test_kill_switch_true_disabled(self) -> None:
        session = AsyncMock()
        with patch(
            "modulo.db.crud.system_config.get_config",
            return_value=SimpleNamespace(value=True),
        ):
            assert await is_enforcement_active(session) is False

    async def test_kill_switch_str_true_disables(self) -> None:
        session = AsyncMock()
        with patch(
            "modulo.db.crud.system_config.get_config",
            return_value=SimpleNamespace(value="true"),
        ):
            assert await is_enforcement_active(session) is False

    async def test_kill_switch_str_false_enforces(self) -> None:
        """Regression (FAR-1636): stored "false" must mean the switch is OFF → enforced."""
        session = AsyncMock()
        with patch(
            "modulo.db.crud.system_config.get_config",
            return_value=SimpleNamespace(value="false"),
        ):
            assert await is_enforcement_active(session) is True

    async def test_kill_switch_str_zero_enforces(self) -> None:
        session = AsyncMock()
        with patch(
            "modulo.db.crud.system_config.get_config",
            return_value=SimpleNamespace(value="0"),
        ):
            assert await is_enforcement_active(session) is True

    async def test_kill_switch_str_no_enforces(self) -> None:
        session = AsyncMock()
        with patch(
            "modulo.db.crud.system_config.get_config",
            return_value=SimpleNamespace(value="no"),
        ):
            assert await is_enforcement_active(session) is True

    async def test_kill_switch_str_yes_disables(self) -> None:
        session = AsyncMock()
        with patch(
            "modulo.db.crud.system_config.get_config",
            return_value=SimpleNamespace(value="yes"),
        ):
            assert await is_enforcement_active(session) is False

    async def test_kill_switch_read_error_fail_safe(self) -> None:
        from sqlalchemy.exc import SQLAlchemyError

        session = AsyncMock()
        with patch(
            "modulo.db.crud.system_config.get_config",
            side_effect=SQLAlchemyError("db error"),
        ):
            assert await is_enforcement_active(session) is True


class TestEnforcementHelpersAgree:
    """FAR-1636: the two kill-switch helpers must share ONE canonical semantics."""

    @pytest.mark.parametrize(
        "stored",
        [
            None,
            True,
            False,
            "true",
            "false",
            "1",
            "0",
            "yes",
            "no",
        ],
        ids=["absent", "bool_true", "bool_false", "str_true", "str_false", "str_one", "str_zero", "str_yes", "str_no"],
    )
    async def test_both_helpers_return_the_same_verdict(self, stored: object) -> None:
        session = AsyncMock()
        config = None if stored is None else SimpleNamespace(value=stored)
        with (
            patch("modulo.db.crud.system_config.get_config", return_value=config),
            patch.object(consent_module, "get_config", new=AsyncMock(return_value=config)),
        ):
            license_active = await is_enforcement_active(session)
            consent_active = await is_license_enforcement_enabled(session)

        assert license_active is consent_active


class TestShouldDegradeToCommunity:
    def test_enforcement_off_no_degrade(self) -> None:
        org = _org(
            {
                "license_key": "key",
                "product_analytics": {"level": "off"},
            }
        )
        with patch("modulo.core.product_analytics.license_enforcement.parse_and_verify") as mock_verify:
            mock_verify.return_value = SimpleNamespace(
                valid=True,
                license_data=_license_data(payload={PRODUCT_ANALYTICS_REQUIRED_KEY: True}),
            )
            assert should_degrade_to_community(org, enforcement_active=False) is False

    def test_enforcement_on_degraded_degrades(self) -> None:
        org = _org(
            {
                "license_key": "key",
                "product_analytics": {"level": "off"},
            }
        )
        with patch("modulo.core.product_analytics.license_enforcement.parse_and_verify") as mock_verify:
            mock_verify.return_value = SimpleNamespace(
                valid=True,
                license_data=_license_data(payload={PRODUCT_ANALYTICS_REQUIRED_KEY: True}),
            )
            assert should_degrade_to_community(org, enforcement_active=True) is True

    def test_enforcement_on_satisfied_no_degrade(self) -> None:
        org = _org(
            {
                "license_key": "key",
                "product_analytics": {"level": "all"},
            }
        )
        with patch("modulo.core.product_analytics.license_enforcement.parse_and_verify") as mock_verify:
            mock_verify.return_value = SimpleNamespace(
                valid=True,
                license_data=_license_data(payload={PRODUCT_ANALYTICS_REQUIRED_KEY: True}),
            )
            assert should_degrade_to_community(org, enforcement_active=True) is False

    def test_enforcement_on_pending_no_degrade(self) -> None:
        org = _org({"license_key": "key"})
        with patch("modulo.core.product_analytics.license_enforcement.parse_and_verify") as mock_verify:
            mock_verify.return_value = SimpleNamespace(
                valid=True,
                license_data=_license_data(payload={PRODUCT_ANALYTICS_REQUIRED_KEY: True}),
            )
            assert should_degrade_to_community(org, enforcement_active=True) is False

    def test_enforcement_on_not_required_no_degrade(self) -> None:
        org = _org({"license_key": "key"})
        with patch("modulo.core.product_analytics.license_enforcement.parse_and_verify") as mock_verify:
            mock_verify.return_value = SimpleNamespace(valid=True, license_data=_license_data(tier="team"))
            assert should_degrade_to_community(org, enforcement_active=True) is False
