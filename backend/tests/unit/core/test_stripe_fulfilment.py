"""Tests for modulo.core.stripe_fulfilment — idempotent purchase fulfilment."""

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.email_service import EmailSendingError
from modulo.core.license import (
    _LICENSE_PUBLIC_KEY_HEX,
    REASON_SIGNATURE_MISMATCH,
    RENEWAL_MODE_AUTO,
    RENEWAL_MODE_MANUAL,
    parse_and_verify,
    set_public_key,
)
from modulo.core.license_signing import LicenseSigningError, _add_months
from modulo.core.registry.crypto import generate_keypair
from modulo.core.stripe_fulfilment import (
    REASON_STARTS_AT_IN_PAST,
    REASON_STARTS_AT_MALFORMED,
    REASON_STARTS_AT_TOO_FAR_AHEAD,
    StartsAtRejectedError,
    _licence_email_html,
    _licence_email_text,
    _processed_event_ids,
    email_team_license,
    fulfil_team_purchase,
    resolve_starts_at,
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

    async def test_unverifiable_generated_key_is_not_emailed_or_claimed(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A generated key that fails verification against our own public key
        must NOT be emailed, and must leave the event unclaimed.

        Reproduces the misconfigured-keypair deployment state: the licence is
        signed with the configured private key while the effective public key
        is a different authority, so ``parse_and_verify`` rejects it. Emailing
        such a key would deliver a licence the product rejects on upload, and
        claiming the event would tell Stripe the purchase was fulfilled — so the
        failure path must fail CLOSED (return None, no email, no claim) and let
        Stripe re-attempt once the deployment is corrected.
        """
        settings = _make_settings()
        other = generate_keypair()
        set_public_key(other["public_key"])
        try:
            with (
                patch(
                    "modulo.core.stripe_fulfilment.send_email",
                    new=MagicMock(return_value=True),
                ) as mock_send,
                caplog.at_level("ERROR", logger="modulo.core.stripe_fulfilment"),
            ):
                result = await fulfil_team_purchase(
                    settings,
                    event_id="evt_unverifiable",
                    customer_email="bob@acme.com",
                    org_name="Acme",
                )
        finally:
            set_public_key(_TEST_PUB)
        assert result is None
        mock_send.assert_not_called()
        assert "evt_unverifiable" not in _processed_event_ids
        # FAR-2099: the misconfigured-keypair warning carries the SAME
        # structured classification the install surfaces return, so the
        # operator can tell a signature/anchor mismatch from a corrupt key.
        assert f"reason={REASON_SIGNATURE_MISMATCH}" in caplog.text
        assert "MODULO_LICENSE_PUBLIC_KEY" in caplog.text

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

    async def test_claim_race_after_email_returns_none(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A concurrent delivery can win the post-email claim after this
        delivery's pre-email duplicate check passed. The losing delivery must
        not report success: it returns ``None`` (the winner owns fulfilment)
        and logs the duplicate, while the email has already been sent once."""
        settings = _make_settings()
        with (
            patch("modulo.core.stripe_fulfilment.send_email", new=MagicMock(return_value=True)) as mock_send,
            patch("modulo.core.stripe_fulfilment._is_event_claimed", new=AsyncMock(return_value=False)),
            patch("modulo.core.stripe_fulfilment._claim_event_id", new=AsyncMock(return_value=False)),
            caplog.at_level("INFO", logger="modulo.core.stripe_fulfilment"),
        ):
            result = await fulfil_team_purchase(
                settings,
                event_id="evt_claim_race",
                customer_email="bob@acme.com",
                org_name="Acme",
            )
        assert result is None
        mock_send.assert_called_once()
        assert "duplicate_event" in caplog.text
        assert "evt_claim_race" in caplog.text

    async def test_subscription_purchase_mints_auto_renewal(self) -> None:
        """FAR-2136: a subscription purchase's key says ``auto``, so the shell's
        renewal notice states the renewal date and offers no purchase path."""
        settings = _make_settings()
        with patch("modulo.core.stripe_fulfilment.send_email", new=MagicMock(return_value=True)):
            license_key = await fulfil_team_purchase(
                settings,
                event_id="evt_sub_auto",
                customer_email="bob@acme.com",
                org_name="Acme",
                renewal_mode=RENEWAL_MODE_AUTO,
            )
        assert license_key is not None
        validation = parse_and_verify(license_key)
        assert validation.license_data is not None
        assert validation.license_data.renewal_mode == RENEWAL_MODE_AUTO

    async def test_default_fulfilment_mints_manual_renewal(self) -> None:
        """A caller that does not state the mode can never promise a renewal."""
        settings = _make_settings()
        with patch("modulo.core.stripe_fulfilment.send_email", new=MagicMock(return_value=True)):
            license_key = await fulfil_team_purchase(
                settings,
                event_id="evt_default_manual",
                customer_email="bob@acme.com",
                org_name="Acme",
            )
        assert license_key is not None
        validation = parse_and_verify(license_key)
        assert validation.license_data is not None
        assert validation.license_data.renewal_mode == RENEWAL_MODE_MANUAL


# ---------------------------------------------------------------------------
# FAR-2146 — the customer-chosen licence start
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)


class TestResolveStartsAt:
    """The boundary rules: future-only, at most 90 days out, and never clamped."""

    def test_absent_metadata_means_start_now(self) -> None:
        assert resolve_starts_at(None) is None
        assert resolve_starts_at("") is None
        assert resolve_starts_at("   ") is None

    def test_a_bare_date_is_interpreted_at_midnight_utc(self) -> None:
        # ``now`` is pinned so the assertion does not drift out of the 90-day
        # horizon as the real clock moves.
        now = datetime(2027, 1, 15, tzinfo=UTC)

        assert resolve_starts_at("2027-03-12", now=now) == datetime(2027, 3, 12, tzinfo=UTC)

    def test_today_is_accepted_for_the_whole_day(self) -> None:
        """A date-only choice is a calendar day, not the instant of UTC midnight."""
        late_today = datetime(2026, 10, 25, 23, 0, 0, tzinfo=UTC)

        assert resolve_starts_at("2026-10-25", now=late_today) == datetime(2026, 10, 25, tzinfo=UTC)

    def test_yesterday_is_rejected_as_in_the_past(self) -> None:
        with pytest.raises(StartsAtRejectedError) as raised:
            resolve_starts_at("2026-10-24", now=_NOW)

        assert raised.value.reason == REASON_STARTS_AT_IN_PAST
        assert raised.value.value == "2026-10-24"
        assert "already passed" in raised.value.detail

    def test_a_past_full_datetime_is_rejected(self) -> None:
        with pytest.raises(StartsAtRejectedError) as raised:
            resolve_starts_at("2026-10-25T11:59:59+00:00", now=_NOW)

        assert raised.value.reason == REASON_STARTS_AT_IN_PAST

    def test_exactly_ninety_days_ahead_is_allowed(self) -> None:
        latest = (_NOW + timedelta(days=90)).date()

        assert resolve_starts_at(latest.isoformat(), now=_NOW) == datetime(
            latest.year, latest.month, latest.day, tzinfo=UTC
        )

    def test_more_than_ninety_days_ahead_is_rejected(self) -> None:
        too_far = (_NOW + timedelta(days=91)).date()

        with pytest.raises(StartsAtRejectedError) as raised:
            resolve_starts_at(too_far.isoformat(), now=_NOW)

        assert raised.value.reason == REASON_STARTS_AT_TOO_FAR_AHEAD
        assert "90 days ahead" in raised.value.detail

    def test_a_full_datetime_beyond_the_horizon_is_rejected(self) -> None:
        too_far = (_NOW + timedelta(days=91)).isoformat()

        with pytest.raises(StartsAtRejectedError) as raised:
            resolve_starts_at(too_far, now=_NOW)

        assert raised.value.reason == REASON_STARTS_AT_TOO_FAR_AHEAD

    @pytest.mark.parametrize("value", ["not-a-date", "2027-13-01", "next tuesday"])
    def test_an_unparseable_value_is_rejected_as_malformed(self, value: str) -> None:
        with pytest.raises(StartsAtRejectedError) as raised:
            resolve_starts_at(value, now=_NOW)

        assert raised.value.reason == REASON_STARTS_AT_MALFORMED
        assert value in raised.value.detail


class TestFulfilmentWithChosenStart:
    async def test_a_chosen_start_mints_a_key_that_verifies_with_the_claim(self) -> None:
        settings = _make_settings()
        start = datetime.now(UTC) + timedelta(days=60)

        license_key = await fulfil_team_purchase(
            settings,
            event_id="evt_future_start",
            customer_email="bob@acme.com",
            org_name="Acme",
            send_key_email=False,
            starts_at=start.date().isoformat(),
        )

        assert license_key is not None
        data = parse_and_verify(license_key).license_data
        assert data is not None
        # A bare date resolves to midnight UTC, and the term runs from THERE
        # (not from the mint time).
        expected_start = datetime(start.year, start.month, start.day, tzinfo=UTC)
        assert data.starts_at == expected_start.isoformat()
        assert datetime.fromisoformat(data.expires_at) == _add_months(expected_start, 12)
        assert datetime.fromisoformat(data.issued_at or "") < expected_start

    async def test_a_past_start_is_rejected_and_nothing_is_minted_or_claimed(self) -> None:
        settings = _make_settings()
        past = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
        with (
            patch("modulo.core.stripe_fulfilment.send_email", new=MagicMock()) as mock_send,
            pytest.raises(StartsAtRejectedError) as raised,
        ):
            await fulfil_team_purchase(
                settings,
                event_id="evt_past_start",
                customer_email="bob@acme.com",
                org_name="Acme",
                starts_at=past,
            )

        assert raised.value.reason == REASON_STARTS_AT_IN_PAST
        mock_send.assert_not_called()
        # Unclaimed, so a corrected event can still be fulfilled.
        assert "evt_past_start" not in _processed_event_ids

    async def test_a_start_beyond_the_horizon_is_rejected(self) -> None:
        settings = _make_settings()
        too_far = (datetime.now(UTC) + timedelta(days=120)).date().isoformat()

        with pytest.raises(StartsAtRejectedError) as raised:
            await fulfil_team_purchase(
                settings,
                event_id="evt_far_start",
                customer_email="bob@acme.com",
                org_name="Acme",
                starts_at=too_far,
            )

        assert raised.value.reason == REASON_STARTS_AT_TOO_FAR_AHEAD
        assert "evt_far_start" not in _processed_event_ids

    async def test_absent_start_mints_exactly_as_before(self) -> None:
        settings = _make_settings()

        license_key = await fulfil_team_purchase(
            settings,
            event_id="evt_no_start",
            customer_email="bob@acme.com",
            org_name="Acme",
            send_key_email=False,
        )

        assert license_key is not None
        data = parse_and_verify(license_key).license_data
        assert data is not None
        assert data.starts_at is None

    async def test_the_email_states_when_the_licence_becomes_active(self) -> None:
        settings = _make_settings()
        start = (datetime.now(UTC) + timedelta(days=45)).date().isoformat()

        with patch("modulo.core.stripe_fulfilment.send_email", new=MagicMock(return_value=True)) as mock_send:
            await fulfil_team_purchase(
                settings,
                event_id="evt_email_start",
                customer_email="bob@acme.com",
                org_name="Acme",
                starts_at=start,
            )

        html_body = mock_send.call_args.args[3]
        text_body = mock_send.call_args.args[4]
        assert start in html_body
        assert "It becomes active on" in html_body
        assert f"It becomes active on {start}" in text_body
