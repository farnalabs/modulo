"""Tests for modulo.core.stripe_fulfilment — idempotent purchase fulfilment."""

from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest

from modulo.core.email_service import EmailSendingError
from modulo.core.license import _LICENSE_PUBLIC_KEY_HEX, parse_and_verify, set_public_key
from modulo.core.license_signing import LicenseSigningError
from modulo.core.registry.crypto import generate_keypair
from modulo.core.stripe_fulfilment import (
    _licence_email_html,
    _licence_email_text,
    _processed_event_ids,
    email_team_license,
    fulfil_team_purchase,
)
from modulo.settings import Settings

_VALID_32 = "a" * 32
_KP = generate_keypair()
_TEST_PRIV = _KP["private_key"]
_TEST_PUB = _KP["public_key"]


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_private_key=_TEST_PRIV,
        redis_url="",
    )


@pytest.fixture(autouse=True)
def _reset_key_and_events() -> Generator[None, None, None]:
    _processed_event_ids.clear()
    set_public_key(_TEST_PUB)
    yield
    _processed_event_ids.clear()
    set_public_key(_LICENSE_PUBLIC_KEY_HEX)


class TestEmailContent:
    def test_html_contains_key_and_instructions(self) -> None:
        html = _licence_email_html("abc.def", "2999-01-01T00:00:00+00:00")
        assert "MODULO_LICENSE_KEY=abc.def" in html
        assert "2999-01-01T00:00:00+00:00" in html
        assert "renews annually" in html
        assert "https://docs.modulo.run" in html

    def test_text_contains_key_and_instructions(self) -> None:
        text = _licence_email_text("abc.def", "2999-01-01T00:00:00+00:00")
        assert "MODULO_LICENSE_KEY=abc.def" in text
        assert "2999-01-01T00:00:00+00:00" in text
        assert "renews annually" in text


class TestEmailTeamLicense:
    async def test_sends_license_email(self) -> None:
        settings = _make_settings()
        with patch("modulo.core.stripe_fulfilment.send_email", new=MagicMock(return_value=True)) as mock_send:
            await email_team_license(settings, "bob@acme.com", "abc.def", "2999-01-01T00:00:00+00:00")
        mock_send.assert_called_once()
        args = mock_send.call_args.args
        assert args[1] == ["bob@acme.com"]
        assert args[2] == "Your Modulo Team License"
        assert "MODULO_LICENSE_KEY=abc.def" in args[3]

    async def test_email_failure_is_logged_not_raised(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = _make_settings()
        with patch("modulo.core.stripe_fulfilment.send_email", side_effect=EmailSendingError("smtp down")):
            await email_team_license(settings, "bob@acme.com", "abc.def", "")
        assert "email_failed" in caplog.text


class TestFulfilTeamPurchase:
    async def test_generates_and_emails_license(self) -> None:
        settings = _make_settings()
        with patch("modulo.core.stripe_fulfilment.send_email", new=MagicMock(return_value=True)) as mock_send:
            license_key = await fulfil_team_purchase(
                settings,
                event_id="evt_1",
                customer_email="bob@acme.com",
                org_name="Acme",
            )
        assert license_key is not None
        validation = parse_and_verify(license_key)
        assert validation.valid is True
        assert validation.license_data is not None
        assert validation.license_data.tier == "team"
        assert validation.license_data.org_id
        mock_send.assert_called_once()

    async def test_duplicate_event_is_skipped(self) -> None:
        settings = _make_settings()
        with patch("modulo.core.stripe_fulfilment.send_email", new=MagicMock(return_value=True)) as mock_send:
            first = await fulfil_team_purchase(
                settings,
                event_id="evt_dup",
                customer_email="bob@acme.com",
                org_name="Acme",
            )
            second = await fulfil_team_purchase(
                settings,
                event_id="evt_dup",
                customer_email="bob@acme.com",
                org_name="Acme",
            )
        assert first is not None
        assert second is None
        assert mock_send.call_count == 1

    async def test_email_failure_leaves_event_unclaimed_for_retry(self) -> None:
        """Transient SMTP failure must NOT claim the event (FAR-180): the key is
        only emailed, never persisted, so a claimed-but-unemailed event would be
        permanently lost. A failed send returns None with the event unclaimed,
        and a later Stripe retry re-attempts the email successfully."""
        settings = _make_settings()
        with patch(
            "modulo.core.stripe_fulfilment.send_email",
            new=MagicMock(side_effect=EmailSendingError("smtp down")),
        ) as mock_fail:
            first = await fulfil_team_purchase(
                settings,
                event_id="evt_retry",
                customer_email="bob@acme.com",
                org_name="Acme",
            )
        assert first is None
        assert mock_fail.call_count == 1
        # Event NOT claimed — a Stripe retry must be able to re-attempt.
        assert "evt_retry" not in _processed_event_ids

        with patch(
            "modulo.core.stripe_fulfilment.send_email",
            new=MagicMock(return_value=True),
        ) as mock_ok:
            second = await fulfil_team_purchase(
                settings,
                event_id="evt_retry",
                customer_email="bob@acme.com",
                org_name="Acme",
            )
        assert second is not None
        assert mock_ok.call_count == 1
        validation = parse_and_verify(second)
        assert validation.valid is True

    async def test_license_generation_failure_returns_none_and_does_not_claim(self) -> None:
        settings = _make_settings()
        with (
            patch(
                "modulo.core.stripe_fulfilment.generate_team_license",
                side_effect=LicenseSigningError("no key"),
            ),
            patch("modulo.core.stripe_fulfilment.send_email", new=MagicMock()) as mock_send,
        ):
            result = await fulfil_team_purchase(
                settings,
                event_id="evt_3",
                customer_email="bob@acme.com",
                org_name="Acme",
            )
        assert result is None
        mock_send.assert_not_called()
        # Event NOT claimed — a Stripe retry can still fulfil it later.
        assert "evt_3" not in _processed_event_ids

    async def test_fulfilment_without_email(self) -> None:
        settings = _make_settings()
        with patch("modulo.core.stripe_fulfilment.send_email", new=MagicMock()) as mock_send:
            license_key = await fulfil_team_purchase(
                settings,
                event_id="evt_4",
                customer_email="bob@acme.com",
                org_name="Acme",
                send_key_email=False,
            )
        assert license_key is not None
        mock_send.assert_not_called()
