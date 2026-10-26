"""Unit tests for the licence-derived DEFAULT of the analytics instance switch.

FAR-2063 removed the old "degrade to Community" enforcement: a licence can no
longer change an organisation's plan. The ``product_analytics_required`` claim
now only supplies the DEFAULT for the instance switch when nothing is set
explicitly. These tests drive the REAL resolution path
(``consent.resolve_instance_analytics_switch``) with only its two external
seams patched - the ``system_config`` read and ``get_license()`` - and pin the
settled precedence:

* licence without the claim + switch unset  -> OFF (default)
* licence with the claim + switch unset     -> ON  (licence default)
* operator sets OFF while the licence needs it -> OFF (operator wins)
* operator sets ON without the claim        -> ON  (operator wins)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from modulo.core.product_analytics import consent as consent_module
from modulo.core.product_analytics.consent import (
    partner_license_requires_analytics,
    resolve_instance_analytics_switch,
)
from modulo.core.product_analytics.constants import (
    ENV_INSTANCE_SWITCH,
    PARTNER_LICENSE_CLAIM,
    SWITCH_SOURCE_DEFAULT,
    SWITCH_SOURCE_ENV,
    SWITCH_SOURCE_LICENSE,
    SWITCH_SOURCE_OPERATOR,
)


def _licence(*, claim: bool | None = None) -> SimpleNamespace:
    """A minimal LicenseData-like object carrying a signed ``raw_payload``."""
    payload: dict[str, object] = {"tier": "team"}
    if claim is not None:
        payload[PARTNER_LICENSE_CLAIM] = claim
    return SimpleNamespace(
        tier="team",
        features=[],
        expires_at="2099-01-01T00:00:00+00:00",
        org_id="test-org",
        raw_payload=payload,
        raw_key="fake-key",
    )


def _config(value: object) -> SimpleNamespace:
    return SimpleNamespace(value=value)


async def _resolve(
    *,
    stored: object,
    licence: SimpleNamespace | None,
) -> consent_module.AnalyticsSwitchState:
    """Run the real resolver with the stored row + licence seams patched."""
    config = None if stored is None else _config(stored)
    with (
        patch.object(consent_module, "get_config", new=AsyncMock(return_value=config)),
        patch.object(consent_module, "get_license", return_value=licence),
    ):
        return await resolve_instance_analytics_switch(AsyncMock())


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env var is an operator setting too - keep it out of these tests."""
    monkeypatch.delenv(ENV_INSTANCE_SWITCH, raising=False)


class TestLicenceDerivedDefault:
    @pytest.mark.asyncio
    async def test_no_licence_and_unset_switch_is_off(self) -> None:
        state = await _resolve(stored=None, licence=None)
        assert state.enabled is False
        assert state.source == SWITCH_SOURCE_DEFAULT
        assert state.licence_requires_analytics is False
        assert state.operator_value is None

    @pytest.mark.asyncio
    async def test_licence_without_claim_is_off(self) -> None:
        state = await _resolve(stored=None, licence=_licence())
        assert state.enabled is False
        assert state.source == SWITCH_SOURCE_DEFAULT
        assert state.licence_requires_analytics is False

    @pytest.mark.asyncio
    async def test_licence_with_claim_turns_the_default_on(self) -> None:
        state = await _resolve(stored=None, licence=_licence(claim=True))
        assert state.enabled is True
        assert state.source == SWITCH_SOURCE_LICENSE
        assert state.licence_requires_analytics is True
        assert state.operator_value is None

    @pytest.mark.asyncio
    async def test_claim_string_true_is_honoured(self) -> None:
        state = await _resolve(stored=None, licence=_licence(claim="true"))
        assert state.enabled is True
        assert state.source == SWITCH_SOURCE_LICENSE


class TestOperatorWins:
    @pytest.mark.asyncio
    async def test_operator_off_vetoes_a_licence_that_requires_analytics(self) -> None:
        state = await _resolve(stored=False, licence=_licence(claim=True))
        assert state.enabled is False
        assert state.source == SWITCH_SOURCE_OPERATOR
        assert state.licence_requires_analytics is True
        assert state.operator_value is False

    @pytest.mark.asyncio
    async def test_operator_on_without_a_claim(self) -> None:
        state = await _resolve(stored=True, licence=_licence())
        assert state.enabled is True
        assert state.source == SWITCH_SOURCE_OPERATOR
        assert state.operator_value is True

    @pytest.mark.asyncio
    async def test_stored_false_string_is_off_not_truthy(self) -> None:
        """Regression: a stored string ``"false"`` must never read as enabled."""
        state = await _resolve(stored="false", licence=None)
        assert state.enabled is False
        assert state.source == SWITCH_SOURCE_OPERATOR

    @pytest.mark.asyncio
    async def test_env_value_is_an_operator_setting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_INSTANCE_SWITCH, "true")
        state = await _resolve(stored=None, licence=None)
        assert state.enabled is True
        assert state.source == SWITCH_SOURCE_ENV
        assert state.operator_value is True

    @pytest.mark.asyncio
    async def test_env_off_vetoes_a_licence_that_requires_analytics(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_INSTANCE_SWITCH, "false")
        state = await _resolve(stored=None, licence=_licence(claim=True))
        assert state.enabled is False
        assert state.source == SWITCH_SOURCE_ENV
        assert state.licence_requires_analytics is True

    @pytest.mark.asyncio
    async def test_stored_value_beats_the_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_INSTANCE_SWITCH, "true")
        state = await _resolve(stored=False, licence=None)
        assert state.enabled is False
        assert state.source == SWITCH_SOURCE_OPERATOR

    @pytest.mark.parametrize("raw", ["", "   "])
    @pytest.mark.asyncio
    async def test_blank_env_var_is_off_not_a_licence_fallthrough(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        """A set-but-empty env var is an explicit OFF, never "unset".

        Regression: blank-then-strip dropped the env source entirely, so a
        licence that requires analytics turned the switch ON (fail-open on a
        telemetry control).
        """
        monkeypatch.setenv(ENV_INSTANCE_SWITCH, raw)
        state = await _resolve(stored=None, licence=_licence(claim=True))
        assert state.enabled is False
        assert state.source == SWITCH_SOURCE_ENV
        assert state.operator_value is False
        assert state.licence_requires_analytics is True


class TestPartnerLicenseClaimReader:
    def test_reads_the_signed_raw_payload(self) -> None:
        assert partner_license_requires_analytics(_licence(claim=True)) is True

    def test_absent_claim_is_false(self) -> None:
        assert partner_license_requires_analytics(_licence()) is False

    def test_false_claim_is_false(self) -> None:
        assert partner_license_requires_analytics(_licence(claim=False)) is False

    def test_string_false_claim_is_false(self) -> None:
        """``bool("false")`` is True - the reader must compare explicitly."""
        assert partner_license_requires_analytics(_licence(claim="false")) is False

    def test_mapping_input_is_accepted(self) -> None:
        assert partner_license_requires_analytics({PARTNER_LICENSE_CLAIM: True}) is True

    def test_missing_or_malformed_input_is_false(self) -> None:
        assert partner_license_requires_analytics(None) is False
        assert partner_license_requires_analytics("not-a-licence") is False
        assert partner_license_requires_analytics(SimpleNamespace(raw_payload=None)) is False
