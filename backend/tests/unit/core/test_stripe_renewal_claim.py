"""FAR-2109 — stacking an early renewal onto a customer-presented licence.

The purchase surface has no pre-payment input (a Stripe purchase mints a key
and emails it), so the previous licence is presented AFTER the purchase. These
tests pin the whole claim contract at the core seam:

1. an early renewal starts the new term at the PRESENTED expiry, never at now;
2. a no-key / auto renewal lands on ``now + term``, which is exactly what
   stacking produces when the charge lands at period end — the equivalence
   that makes an upload-free auto renewal correct;
3. an unverifiable, cross-org or self-referential key is rejected with a
   distinguishable, machine-readable reason and nothing is minted;
4. one purchase yields exactly one stacked key, however often it is retried.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

import modulo.core.license as license_module
import modulo.core.license_signing as license_signing_module
import modulo.core.stripe_fulfilment as fulfilment
from modulo.core.license import (
    _LICENSE_PUBLIC_KEY_HEX,
    REASON_EXPIRED,
    REASON_MALFORMED,
    REASON_SIGNATURE_MISMATCH,
    RENEWAL_MODE_AUTO,
    parse_and_verify,
    set_public_key,
)
from modulo.core.license_signing import LicenseSigningError, generate_team_license
from modulo.core.registry.crypto import generate_keypair, sign_primitive
from modulo.core.stripe_fulfilment import (
    REASON_NO_EXPIRY,
    REASON_NO_PRESENTED_KEY,
    REASON_NOT_A_PURCHASE_KEY,
    REASON_ORG_MISMATCH,
    REASON_SAME_LICENCE,
    RenewalClaimRejectedError,
    RenewalClaimUnavailableError,
    _decode_claim_record,
    _renewal_claims,
    _store_renewal_claim,
    claim_stacked_renewal,
)
from modulo.settings import Settings

#: Pinned "now" for every test here. Fixed instants make the expiry assertions
#: EXACT ISO strings instead of tolerance-based: the behaviour under test is
#: base-date selection, not month arithmetic.
_NOW = datetime(2027, 3, 10, 12, 0, 0, tzinfo=UTC)

_VALID_32 = "a" * 32
_KP = generate_keypair()
_TEST_PRIV = _KP["private_key"]
_TEST_PUB = _KP["public_key"]


class _Clock(datetime):
    """``datetime`` whose ``now()`` is pinned to :data:`_NOW`.

    Installed over the ``datetime`` symbol of every module that reads the
    current instant (mint, expiry check, claim detail), so the whole flow sees
    one clock instead of a mix of frozen and wall-clock reads.
    """

    current: datetime = _NOW

    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
        return cls.current if tz is not None else cls.current.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(license_signing_module, "datetime", _Clock)
    monkeypatch.setattr(license_module, "datetime", _Clock)
    monkeypatch.setattr(fulfilment, "datetime", _Clock)


@pytest.fixture(autouse=True)
def _trusted_anchor() -> Generator[None, None, None]:
    _renewal_claims.clear()
    set_public_key(_TEST_PUB)
    yield
    _renewal_claims.clear()
    set_public_key(_LICENSE_PUBLIC_KEY_HEX)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_private_key=_TEST_PRIV,
        redis_url="",
    )


def _signed_key(payload: dict[str, Any], private_key: str = _TEST_PRIV) -> str:
    """Sign *payload* into a ``<payload>.<signature>`` licence key."""
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode().rstrip("=")
    sig_b64 = base64.urlsafe_b64encode(bytes.fromhex(sign_primitive(payload, private_key))).decode().rstrip("=")
    return f"{payload_b64}.{sig_b64}"


def _payload_of(key: str) -> dict[str, Any]:
    payload_b64 = key.split(".", 1)[0]
    decoded: dict[str, Any] = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
    return decoded


def _purchase_key(*, org_name: str = "Acme") -> str:
    """The key a purchase mints: one full term starting now."""
    return generate_team_license(org_name, term_months=12, private_key_hex=_TEST_PRIV)


def _previous_key(
    purchase_key: str,
    *,
    expires_at: datetime,
    org_id: str | None = None,
) -> str:
    """A signature-valid earlier licence expiring at *expires_at*."""
    purchase = parse_and_verify(purchase_key).license_data
    assert purchase is not None
    return _signed_key(
        {
            "tier": "team",
            "features": list(purchase.features),
            "expires_at": expires_at.isoformat(),
            "org_id": purchase.org_id if org_id is None else org_id,
            "renewal_mode": "manual",
            "term": 12,
            "licence_id": "licence-previous",
            "issued_at": (_NOW - timedelta(days=365)).isoformat(),
        }
    )


def _purchase_org_id(key: str) -> str:
    data = parse_and_verify(key).license_data
    assert data is not None
    return data.org_id


def _claimed_expiry(key: str) -> str:
    data = parse_and_verify(key).license_data
    assert data is not None
    return data.expires_at


class TestEarlyRenewalStacks:
    async def test_early_renewal_starts_at_the_presented_expiry(self) -> None:
        purchase = _purchase_key()
        previous = _previous_key(purchase, expires_at=_NOW + timedelta(days=60))

        claim = await claim_stacked_renewal(_make_settings(), purchase_key=purchase, presented_key=previous)

        assert claim.stacked is True
        # 2027-05-09 + 12 months — the PRESENTED expiry plus a full term.
        assert claim.expires_at == "2028-05-09T12:00:00+00:00"
        # And emphatically not the naive "now + term" that would burn 60 days.
        assert claim.expires_at != "2028-03-10T12:00:00+00:00"

    async def test_stacked_key_keeps_the_purchase_identity_and_claims(self) -> None:
        purchase = _purchase_key()
        previous = _previous_key(purchase, expires_at=_NOW + timedelta(days=60))

        claim = await claim_stacked_renewal(_make_settings(), purchase_key=purchase, presented_key=previous)

        purchase_data = parse_and_verify(purchase).license_data
        stacked_data = parse_and_verify(claim.license_key).license_data
        assert purchase_data is not None
        assert stacked_data is not None
        # Same entitlement, corrected expiry — not a second licence.
        assert stacked_data.licence_id == purchase_data.licence_id
        assert stacked_data.org_id == purchase_data.org_id
        assert stacked_data.term == purchase_data.term
        assert stacked_data.renewal_mode == purchase_data.renewal_mode
        assert stacked_data.expires_at == claim.expires_at

    async def test_stacked_key_preserves_an_auto_renewal_mode(self) -> None:
        """FAR-2136: the claim carries the purchase key's mode forward unchanged.

        A subscription purchase mints ``auto``; stacking its early renewal must
        not downgrade the corrected licence to ``manual`` (which would offer a
        purchase path to a customer who is already paying).
        """
        purchase = generate_team_license(
            "Acme",
            term_months=12,
            private_key_hex=_TEST_PRIV,
            renewal_mode=RENEWAL_MODE_AUTO,
        )
        previous = _previous_key(purchase, expires_at=_NOW + timedelta(days=60))

        claim = await claim_stacked_renewal(_make_settings(), purchase_key=purchase, presented_key=previous)

        purchase_data = parse_and_verify(purchase).license_data
        stacked_data = parse_and_verify(claim.license_key).license_data
        assert purchase_data is not None
        assert stacked_data is not None
        assert purchase_data.renewal_mode == RENEWAL_MODE_AUTO
        assert stacked_data.renewal_mode == RENEWAL_MODE_AUTO

    async def test_the_early_renewal_adds_the_full_term_to_what_was_left(self) -> None:
        purchase = _purchase_key()
        previous_expiry = _NOW + timedelta(days=60)
        previous = _previous_key(purchase, expires_at=previous_expiry)

        claim = await claim_stacked_renewal(_make_settings(), purchase_key=purchase, presented_key=previous)

        # One 12-month term later, on the same day of the month (leap-safe: the
        # assertion is the calendar date, not a day count).
        stacked_expiry = datetime.fromisoformat(claim.expires_at)
        assert (stacked_expiry.year, stacked_expiry.month, stacked_expiry.day) == (
            previous_expiry.year + 1,
            previous_expiry.month,
            previous_expiry.day,
        )


class TestNoPresentedKey:
    async def test_omitting_the_key_explains_the_cost_and_mints_nothing(self) -> None:
        purchase = _purchase_key()
        purchase_expiry = _claimed_expiry(purchase)

        with patch.object(fulfilment, "generate_team_license", wraps=generate_team_license) as mint:
            claim = await claim_stacked_renewal(_make_settings(), purchase_key=purchase, presented_key=None)

        mint.assert_not_called()
        assert claim.stacked is False
        assert claim.reason == REASON_NO_PRESENTED_KEY
        # The purchase key IS now + term; it is returned unchanged rather than
        # minted a second time for the same purchase.
        assert claim.license_key == purchase
        assert claim.expires_at == purchase_expiry
        # Plain English: what happened, what it cost, and how to recover.
        assert "No previous licence was applied" in claim.detail
        assert "no time was added" in claim.detail
        assert "re-submit" in claim.detail

    async def test_a_blank_presented_key_is_treated_as_absent(self) -> None:
        purchase = _purchase_key()

        claim = await claim_stacked_renewal(_make_settings(), purchase_key=purchase, presented_key="   ")

        assert claim.stacked is False
        assert claim.reason == REASON_NO_PRESENTED_KEY

    async def test_the_claim_is_recoverable_after_omitting_the_key(self) -> None:
        settings = _make_settings()
        purchase = _purchase_key()
        previous = _previous_key(purchase, expires_at=_NOW + timedelta(days=60))

        unstacked = await claim_stacked_renewal(settings, purchase_key=purchase, presented_key=None)
        stacked = await claim_stacked_renewal(settings, purchase_key=purchase, presented_key=previous)

        assert unstacked.stacked is False
        assert stacked.stacked is True
        assert stacked.license_key != unstacked.license_key
        assert stacked.expires_at == "2028-05-09T12:00:00+00:00"


class TestVerificationRejections:
    async def test_a_tampered_previous_key_is_rejected_by_signature(self) -> None:
        purchase = _purchase_key()
        valid = _previous_key(purchase, expires_at=_NOW + timedelta(days=60))
        _payload_b64, signature = valid.split(".", 1)
        tampered_payload = _payload_of(valid)
        tampered_payload["expires_at"] = "2099-01-01T00:00:00+00:00"
        # Re-encode the edited payload but keep the ORIGINAL signature, so the
        # key is well-formed but no longer signed by us.
        tampered = _signed_key(tampered_payload).split(".", 1)[0] + "." + signature
        assert tampered != valid

        with (
            patch.object(fulfilment, "generate_team_license", wraps=generate_team_license) as mint,
            pytest.raises(RenewalClaimRejectedError) as exc_info,
        ):
            await claim_stacked_renewal(_make_settings(), purchase_key=purchase, presented_key=tampered)

        mint.assert_not_called()
        assert exc_info.value.reason == REASON_SIGNATURE_MISMATCH
        assert exc_info.value.purchase_verified is True
        assert exc_info.value.purchase_expires_at == _claimed_expiry(purchase)

    async def test_a_malformed_previous_key_is_rejected_as_malformed(self) -> None:
        purchase = _purchase_key()

        with pytest.raises(RenewalClaimRejectedError) as exc_info:
            await claim_stacked_renewal(_make_settings(), purchase_key=purchase, presented_key="not-a-licence-key")

        assert exc_info.value.reason == REASON_MALFORMED

    async def test_an_expired_previous_key_is_rejected_as_expired(self) -> None:
        purchase = _purchase_key()
        # Past expires_at + the 14-day delivery grace.
        expired = _previous_key(purchase, expires_at=_NOW - timedelta(days=15))

        with pytest.raises(RenewalClaimRejectedError) as exc_info:
            await claim_stacked_renewal(_make_settings(), purchase_key=purchase, presented_key=expired)

        assert exc_info.value.reason == REASON_EXPIRED

    async def test_a_key_from_another_organisation_is_rejected(self) -> None:
        purchase = _purchase_key()
        foreign = _previous_key(
            purchase,
            expires_at=_NOW + timedelta(days=60),
            org_id="org-someone-else",
        )

        with (
            patch.object(fulfilment, "generate_team_license", wraps=generate_team_license) as mint,
            pytest.raises(RenewalClaimRejectedError) as exc_info,
        ):
            await claim_stacked_renewal(_make_settings(), purchase_key=purchase, presented_key=foreign)

        mint.assert_not_called()
        assert exc_info.value.reason == REASON_ORG_MISMATCH
        assert "different organisation" in exc_info.value.detail

    async def test_the_purchase_key_cannot_be_presented_as_its_own_previous_licence(self) -> None:
        purchase = _purchase_key()

        with (
            patch.object(fulfilment, "generate_team_license", wraps=generate_team_license) as mint,
            pytest.raises(RenewalClaimRejectedError) as exc_info,
        ):
            await claim_stacked_renewal(_make_settings(), purchase_key=purchase, presented_key=purchase)

        mint.assert_not_called()
        assert exc_info.value.reason == REASON_SAME_LICENCE

    async def test_an_unverifiable_purchase_key_mints_nothing(self) -> None:
        purchase = _purchase_key()
        payload = _payload_of(purchase)
        forged_purchase = _signed_key(payload, generate_keypair()["private_key"])

        with (
            patch.object(fulfilment, "generate_team_license", wraps=generate_team_license) as mint,
            pytest.raises(RenewalClaimRejectedError) as exc_info,
        ):
            await claim_stacked_renewal(_make_settings(), purchase_key=forged_purchase, presented_key=purchase)

        mint.assert_not_called()
        assert exc_info.value.reason == REASON_SIGNATURE_MISMATCH
        assert exc_info.value.purchase_verified is False

    async def test_a_legacy_key_without_renewal_claims_is_not_a_purchase_key(self) -> None:
        legacy = _signed_key(
            {
                "tier": "team",
                "features": ["sso"],
                "expires_at": (_NOW + timedelta(days=60)).isoformat(),
                "org_id": "org-legacy",
            }
        )

        with pytest.raises(RenewalClaimRejectedError) as exc_info:
            await claim_stacked_renewal(_make_settings(), purchase_key=legacy, presented_key=legacy)

        assert exc_info.value.reason == REASON_NOT_A_PURCHASE_KEY

    async def test_a_perpetual_previous_licence_has_nothing_to_stack_onto(self) -> None:
        purchase = _purchase_key()
        perpetual = _signed_key(
            {
                "tier": "team",
                "features": ["sso"],
                "expires_at": "",
                "org_id": _purchase_org_id(purchase),
                "renewal_mode": "manual",
                "term": 12,
                "licence_id": "licence-perpetual",
                "issued_at": _NOW.isoformat(),
            }
        )

        with pytest.raises(RenewalClaimRejectedError) as exc_info:
            await claim_stacked_renewal(_make_settings(), purchase_key=purchase, presented_key=perpetual)

        assert exc_info.value.reason == REASON_NO_EXPIRY


class TestIdempotency:
    async def test_a_retried_claim_returns_the_same_key_and_mints_once(self) -> None:
        settings = _make_settings()
        purchase = _purchase_key()
        previous = _previous_key(purchase, expires_at=_NOW + timedelta(days=60))

        with patch.object(fulfilment, "generate_team_license", wraps=generate_team_license) as mint:
            first = await claim_stacked_renewal(settings, purchase_key=purchase, presented_key=previous)
            second = await claim_stacked_renewal(settings, purchase_key=purchase, presented_key=previous)

        assert mint.call_count == 1
        assert first.license_key == second.license_key
        assert first.repeated is False
        assert second.repeated is True
        assert second.expires_at == first.expires_at

    async def test_one_purchase_cannot_be_stacked_against_two_previous_licences(self) -> None:
        settings = _make_settings()
        purchase = _purchase_key()
        earlier = _previous_key(purchase, expires_at=_NOW + timedelta(days=30))
        later = _previous_key(purchase, expires_at=_NOW + timedelta(days=300))

        with patch.object(fulfilment, "generate_team_license", wraps=generate_team_license) as mint:
            first = await claim_stacked_renewal(settings, purchase_key=purchase, presented_key=earlier)
            second = await claim_stacked_renewal(settings, purchase_key=purchase, presented_key=later)

        assert mint.call_count == 1
        # The later previous key would have granted more time; one purchase gets
        # exactly one stacked key, so the first claim wins.
        assert second.license_key == first.license_key
        assert second.expires_at == "2028-04-09T12:00:00+00:00"

    async def test_a_stacked_key_cannot_be_used_as_proof_of_a_second_purchase(self) -> None:
        settings = _make_settings()
        purchase = _purchase_key()
        previous = _previous_key(purchase, expires_at=_NOW + timedelta(days=60))
        stacked = await claim_stacked_renewal(settings, purchase_key=purchase, presented_key=previous)

        # The stacked key carries the purchase's licence_id, so presenting it as
        # the purchase of another claim resolves to the SAME record instead of
        # extending again.
        with patch.object(fulfilment, "generate_team_license", wraps=generate_team_license) as mint:
            again = await claim_stacked_renewal(
                settings,
                purchase_key=stacked.license_key,
                presented_key=_previous_key(purchase, expires_at=_NOW + timedelta(days=300)),
            )

        mint.assert_not_called()
        assert again.license_key == stacked.license_key
        assert again.repeated is True


class TestAutoRenewalEquivalence:
    """The justification for upload-free auto renewal: at period end,
    ``now + term`` IS the stacked expiry to within the charge jitter."""

    async def test_a_charge_at_period_end_equals_stacking(self) -> None:
        purchase = _purchase_key()
        previous = _previous_key(purchase, expires_at=_NOW)

        auto = await claim_stacked_renewal(_make_settings(), purchase_key=purchase, presented_key=None)
        stacked = await claim_stacked_renewal(_make_settings(), purchase_key=purchase, presented_key=previous)

        # A no-key (auto) renewal already lands on now + term...
        assert auto.expires_at == "2028-03-10T12:00:00+00:00"
        # ...and stacking from an expiry that coincided with the charge adds
        # exactly the same term, so no upload would have gained the customer
        # anything.
        assert stacked.expires_at == auto.expires_at

    async def test_a_charge_three_hours_after_expiry_also_equals_stacking(self) -> None:
        purchase = _purchase_key()
        previous = _previous_key(purchase, expires_at=_NOW - timedelta(hours=3))

        auto = await claim_stacked_renewal(_make_settings(), purchase_key=purchase, presented_key=None)
        stacked = await claim_stacked_renewal(_make_settings(), purchase_key=purchase, presented_key=previous)

        assert auto.expires_at == "2028-03-10T12:00:00+00:00"
        assert stacked.expires_at == auto.expires_at

    async def test_stacking_only_changes_the_result_while_days_remain(self) -> None:
        purchase = _purchase_key()
        previous = _previous_key(purchase, expires_at=_NOW + timedelta(days=60))

        auto = await claim_stacked_renewal(_make_settings(), purchase_key=purchase, presented_key=None)
        stacked = await claim_stacked_renewal(_make_settings(), purchase_key=purchase, presented_key=previous)

        assert stacked.expires_at != auto.expires_at
        assert datetime.fromisoformat(stacked.expires_at) == datetime.fromisoformat(auto.expires_at) + timedelta(
            days=60
        )


class _FakeRedisClient:
    """Minimal async Redis double for the claim's idempotency seam.

    ``get_results`` is consumed in order so a test can model "nothing stored,
    then a concurrent writer's record appears" without a real server.
    """

    def __init__(
        self,
        *,
        get_results: list[str | None] | None = None,
        get_error: BaseException | None = None,
        set_result: bool = True,
        set_error: BaseException | None = None,
    ) -> None:
        self._get_results = list(get_results or [])
        self._get_error = get_error
        self._set_result = set_result
        self._set_error = set_error
        self.set_calls: list[tuple[str, str, bool, int | None]] = []
        self.closed = False

    async def get(self, key: str) -> str | None:
        if self._get_error is not None:
            raise self._get_error
        return self._get_results.pop(0) if self._get_results else None

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        self.set_calls.append((key, value, nx, ex))
        if self._set_error is not None:
            raise self._set_error
        return self._set_result

    async def aclose(self) -> None:
        self.closed = True


def _redis_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_private_key=_TEST_PRIV,
        redis_url="redis://cache:6379/0",
    )


def _install_redis(monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
    class _Factory:
        @staticmethod
        def from_url(_url: str, **_kwargs: Any) -> Any:
            return client

    monkeypatch.setattr(fulfilment, "Redis", _Factory)


def _stored_record(licence_id: str) -> dict[str, str]:
    del licence_id
    return {
        "license_key": "stored-stacked-key",
        "expires_at": "2028-05-09T12:00:00+00:00",
        "previous_expires_at": "2027-05-09T12:00:00+00:00",
    }


def _licence_id_of(key: str) -> str:
    data = parse_and_verify(key).license_data
    assert data is not None
    assert data.licence_id is not None
    return data.licence_id


class TestPurchaseKeyRejections:
    async def test_a_perpetual_purchase_key_has_no_term_to_extend(self) -> None:
        """A purchase key WITHOUT ``expires_at`` cannot be sized or extended."""
        perpetual_purchase = _signed_key(
            {
                "tier": "team",
                "features": ["sso"],
                "expires_at": "",
                "org_id": "org-perpetual",
                "renewal_mode": "manual",
                "term": 12,
                "licence_id": "licence-perpetual-purchase",
                "issued_at": _NOW.isoformat(),
            }
        )

        with (
            patch.object(fulfilment, "generate_team_license", wraps=generate_team_license) as mint,
            pytest.raises(RenewalClaimRejectedError) as exc_info,
        ):
            await claim_stacked_renewal(_make_settings(), purchase_key=perpetual_purchase, presented_key=None)

        mint.assert_not_called()
        assert exc_info.value.reason == REASON_NO_EXPIRY
        assert exc_info.value.purchase_expires_at is None
        assert exc_info.value.purchase_verified is True


class TestUnavailablePaths:
    async def test_a_signing_failure_is_reported_as_unavailable(self) -> None:
        purchase = _purchase_key()
        previous = _previous_key(purchase, expires_at=_NOW + timedelta(days=60))

        def _no_signing_key(*_args: Any, **_kwargs: Any) -> str:
            raise LicenseSigningError("no signing key configured")

        with (
            patch.object(fulfilment, "generate_team_license", _no_signing_key),
            pytest.raises(RenewalClaimUnavailableError, match="cannot sign a replacement licence"),
        ):
            await claim_stacked_renewal(_make_settings(), purchase_key=purchase, presented_key=previous)

        # Nothing was recorded, so a retry can still mint the real key.
        assert _renewal_claims == {}

    async def test_a_stacked_key_that_fails_our_own_verification_is_not_issued(self) -> None:
        purchase = _purchase_key()
        previous = _previous_key(purchase, expires_at=_NOW + timedelta(days=60))
        purchase_data = parse_and_verify(purchase).license_data
        assert purchase_data is not None
        # A key from another authority: well-formed, but our anchor rejects it.
        foreign_key = _signed_key(
            _payload_of(purchase),
            generate_keypair()["private_key"],
        )

        with (
            patch.object(fulfilment, "generate_team_license", return_value=foreign_key),
            pytest.raises(RenewalClaimUnavailableError, match="could not verify the replacement licence"),
        ):
            await claim_stacked_renewal(_make_settings(), purchase_key=purchase, presented_key=previous)

        assert _renewal_claims == {}


class TestClaimRecordIntegrity:
    def test_a_valid_record_is_decoded_to_its_three_fields(self) -> None:
        record = _stored_record("licence-1")

        assert _decode_claim_record(json.dumps(record)) == record

    def test_unreadable_json_fails_closed(self) -> None:
        with pytest.raises(RenewalClaimUnavailableError, match="unreadable"):
            _decode_claim_record("{not-json")

    def test_a_non_object_record_fails_closed(self) -> None:
        with pytest.raises(RenewalClaimUnavailableError, match="incomplete"):
            _decode_claim_record(json.dumps(["not", "an", "object"]))

    def test_a_record_missing_a_field_fails_closed(self) -> None:
        incomplete = {"license_key": "key", "expires_at": "2028-01-01T00:00:00+00:00"}

        with pytest.raises(RenewalClaimUnavailableError, match="incomplete"):
            _decode_claim_record(json.dumps(incomplete))

    def test_a_non_string_field_fails_closed(self) -> None:
        corrupt = {"license_key": 1, "expires_at": "x", "previous_expires_at": "y"}

        with pytest.raises(RenewalClaimUnavailableError, match="incomplete"):
            _decode_claim_record(json.dumps(corrupt))

    async def test_a_store_returns_the_first_record_when_one_already_exists(self) -> None:
        """First-writer-wins: a later store publishes the record already held."""
        first = _stored_record("licence-first")
        second = _stored_record("licence-second")
        settings = _make_settings()

        assert await _store_renewal_claim(settings, "licence-race", first, 60) == first
        assert await _store_renewal_claim(settings, "licence-race", second, 60) == first


class TestRedisIdempotency:
    async def test_a_record_stored_in_redis_is_read_back_and_replayed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        purchase = _purchase_key()
        previous = _previous_key(purchase, expires_at=_NOW + timedelta(days=60))
        record = _stored_record(_licence_id_of(purchase))
        client = _FakeRedisClient(get_results=[json.dumps(record)])
        _install_redis(monkeypatch, client)

        claim = await claim_stacked_renewal(_redis_settings(), purchase_key=purchase, presented_key=previous)

        assert claim.stacked is True
        assert claim.repeated is True
        assert claim.license_key == "stored-stacked-key"
        assert claim.expires_at == record["expires_at"]
        assert claim.previous_expires_at == record["previous_expires_at"]
        assert client.closed is True

    async def test_a_corrupt_redis_record_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        purchase = _purchase_key()
        previous = _previous_key(purchase, expires_at=_NOW + timedelta(days=60))
        client = _FakeRedisClient(get_results=["{not-json"])
        _install_redis(monkeypatch, client)

        with pytest.raises(RenewalClaimUnavailableError, match="unreadable"):
            await claim_stacked_renewal(_redis_settings(), purchase_key=purchase, presented_key=previous)

        assert client.closed is True

    async def test_a_redis_read_failure_falls_back_to_the_local_record(self, monkeypatch: pytest.MonkeyPatch) -> None:
        purchase = _purchase_key()
        previous = _previous_key(purchase, expires_at=_NOW + timedelta(days=60))
        licence_id = _licence_id_of(purchase)
        record = _stored_record(licence_id)
        _renewal_claims[licence_id] = record
        client = _FakeRedisClient(get_error=RuntimeError("cache down"))
        _install_redis(monkeypatch, client)

        claim = await claim_stacked_renewal(_redis_settings(), purchase_key=purchase, presented_key=previous)

        assert claim.repeated is True
        assert claim.license_key == record["license_key"]
        assert client.closed is True

    async def test_a_redis_connection_failure_falls_back_to_the_local_record(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        purchase = _purchase_key()
        previous = _previous_key(purchase, expires_at=_NOW + timedelta(days=60))
        licence_id = _licence_id_of(purchase)
        record = _stored_record(licence_id)
        _renewal_claims[licence_id] = record

        def _broken_from_url(_url: str, **_kwargs: Any) -> Any:
            raise RuntimeError("cannot connect")

        monkeypatch.setattr(fulfilment.Redis, "from_url", _broken_from_url)

        claim = await claim_stacked_renewal(_redis_settings(), purchase_key=purchase, presented_key=previous)

        assert claim.repeated is True
        assert claim.license_key == record["license_key"]

    async def test_cancellation_is_never_swallowed_by_the_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        purchase = _purchase_key()
        previous = _previous_key(purchase, expires_at=_NOW + timedelta(days=60))
        client = _FakeRedisClient(get_error=asyncio.CancelledError())
        _install_redis(monkeypatch, client)

        with pytest.raises(asyncio.CancelledError):
            await claim_stacked_renewal(_redis_settings(), purchase_key=purchase, presented_key=previous)

    async def test_a_first_writer_stores_its_record_in_redis(self, monkeypatch: pytest.MonkeyPatch) -> None:
        purchase = _purchase_key()
        previous = _previous_key(purchase, expires_at=_NOW + timedelta(days=60))
        client = _FakeRedisClient(get_results=[None], set_result=True)
        _install_redis(monkeypatch, client)

        claim = await claim_stacked_renewal(_redis_settings(), purchase_key=purchase, presented_key=previous)

        assert claim.stacked is True
        assert claim.repeated is False
        assert len(client.set_calls) == 1
        redis_key, raw, nx, ex = client.set_calls[0]
        assert redis_key.endswith(_licence_id_of(purchase))
        assert nx is True
        assert ex == (12 + 2) * 31 * 24 * 3600
        assert json.loads(raw)["license_key"] == claim.license_key
        assert client.closed is True

    async def test_a_concurrent_writer_record_is_published_instead(self, monkeypatch: pytest.MonkeyPatch) -> None:
        purchase = _purchase_key()
        previous = _previous_key(purchase, expires_at=_NOW + timedelta(days=60))
        winner = _stored_record(_licence_id_of(purchase))
        client = _FakeRedisClient(get_results=[None, json.dumps(winner)], set_result=False)
        _install_redis(monkeypatch, client)

        claim = await claim_stacked_renewal(_redis_settings(), purchase_key=purchase, presented_key=previous)

        assert claim.license_key == winner["license_key"]
        assert claim.repeated is True
        assert claim.expires_at == winner["expires_at"]

    async def test_a_lost_concurrent_record_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        purchase = _purchase_key()
        previous = _previous_key(purchase, expires_at=_NOW + timedelta(days=60))
        client = _FakeRedisClient(get_results=[None, None], set_result=False)
        _install_redis(monkeypatch, client)

        with pytest.raises(RenewalClaimUnavailableError, match="recorded its key at the same moment"):
            await claim_stacked_renewal(_redis_settings(), purchase_key=purchase, presented_key=previous)

    async def test_a_redis_store_failure_falls_back_to_the_process_record(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        purchase = _purchase_key()
        previous = _previous_key(purchase, expires_at=_NOW + timedelta(days=60))
        client = _FakeRedisClient(get_results=[None], set_error=RuntimeError("cache down"))
        _install_redis(monkeypatch, client)

        claim = await claim_stacked_renewal(_redis_settings(), purchase_key=purchase, presented_key=previous)

        assert claim.stacked is True
        assert claim.repeated is False
        assert _renewal_claims[_licence_id_of(purchase)]["license_key"] == claim.license_key
        assert client.closed is True

    async def test_cancellation_during_the_store_is_never_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        purchase = _purchase_key()
        previous = _previous_key(purchase, expires_at=_NOW + timedelta(days=60))
        client = _FakeRedisClient(get_results=[None], set_error=asyncio.CancelledError())
        _install_redis(monkeypatch, client)

        with pytest.raises(asyncio.CancelledError):
            await claim_stacked_renewal(_redis_settings(), purchase_key=purchase, presented_key=previous)

    async def test_a_store_connection_failure_falls_back_to_the_process_record(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        purchase = _purchase_key()
        previous = _previous_key(purchase, expires_at=_NOW + timedelta(days=60))
        client = _FakeRedisClient(get_results=[None])
        calls = {"n": 0}

        class _FlakyFactory:
            @staticmethod
            def from_url(_url: str, **_kwargs: Any) -> Any:
                calls["n"] += 1
                if calls["n"] == 1:
                    return client
                raise RuntimeError("cache went away")

        monkeypatch.setattr(fulfilment, "Redis", _FlakyFactory)

        claim = await claim_stacked_renewal(_redis_settings(), purchase_key=purchase, presented_key=previous)

        assert claim.stacked is True
        assert claim.repeated is False
        assert _renewal_claims[_licence_id_of(purchase)]["license_key"] == claim.license_key
