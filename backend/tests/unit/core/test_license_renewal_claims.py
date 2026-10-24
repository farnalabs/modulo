"""FAR-2075 — the signed renewal claims: ``renewal_mode``, ``term``, ``licence_id``, ``issued_at``.

The running instance has no phone-home, so the signed key is the only
trustworthy statement of how a licence renews. These tests pin three things
that later slices (countdown banner, grace modal, early renewal, Stripe
mint-and-email) depend on:

1. an OLD key (no claims) still verifies and resolves to the safe defaults;
2. a NEW key round-trips every claim through the signature;
3. the claims are INSIDE the signed payload — flipping one without re-signing
   is rejected, so a caller can trust what it reads back.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import modulo.core.license as license_module
import modulo.core.license_signing as license_signing_module
from modulo.core.license import (
    DEFAULT_RENEWAL_MODE,
    RENEWAL_MODE_AUTO,
    RENEWAL_MODE_MANUAL,
    LicenseData,
    parse_and_verify,
    set_public_key,
    verify_license_key,
)
from modulo.core.license_signing import (
    LicenseSigningError,
    build_team_payload,
    encode_license_key,
    generate_team_license,
)
from modulo.core.registry.crypto import generate_keypair, sign_primitive


def _sign_payload(payload: dict[str, Any], private_key_hex: str) -> str:
    """Encode *payload* and sign it, producing a ``<payload>.<signature>`` key."""
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode().rstrip("=")
    sig_b64 = base64.urlsafe_b64encode(bytes.fromhex(sign_primitive(payload, private_key_hex))).decode().rstrip("=")
    return f"{payload_b64}.{sig_b64}"


def _payload_of(key: str) -> dict[str, Any]:
    """Decode the payload half of a key WITHOUT verifying it."""
    payload_b64 = key.split(".", 1)[0]
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    decoded: dict[str, Any] = json.loads(base64.urlsafe_b64decode(padded))
    return decoded


def _old_format_payload() -> dict[str, Any]:
    """Exactly the payload shape minted before FAR-2075 — no renewal claims."""
    return {
        "tier": "team",
        "features": ["sso"],
        "expires_at": "2999-01-01T00:00:00+00:00",
        "org_id": "org-legacy",
    }


@pytest.fixture
def kp() -> Generator[dict[str, str], None, None]:
    """Make a generated keypair the trusted anchor for the duration of a test."""
    original = license_module._LICENSE_PUBLIC_KEY_HEX
    generated = generate_keypair()
    set_public_key(generated["public_key"])
    yield generated
    set_public_key(original)


def _parsed(key: str) -> LicenseData:
    """Parse *key* and assert it verified, returning its LicenseData."""
    result = parse_and_verify(key)
    assert result.valid
    assert result.license_data is not None
    return result.license_data


# ---------------------------------------------------------------------------
# 1. Backward compatibility — the highest-risk path in this slice
# ---------------------------------------------------------------------------


class TestOldFormatKeyStillVerifies:
    """An existing key in the wild carries none of the FAR-2075 claims."""

    def test_old_key_verifies(self, kp: dict[str, str]) -> None:
        key = _sign_payload(_old_format_payload(), kp["private_key"])

        result = parse_and_verify(key)

        assert result.valid
        assert result.license_data is not None
        assert result.license_data.tier == "team"
        assert result.license_data.org_id == "org-legacy"

    def test_old_key_defaults_renewal_mode_to_manual(self, kp: dict[str, str]) -> None:
        key = _sign_payload(_old_format_payload(), kp["private_key"])

        data = _parsed(key)

        assert data.renewal_mode == RENEWAL_MODE_MANUAL

    def test_old_key_defaults_term_to_none(self, kp: dict[str, str]) -> None:
        key = _sign_payload(_old_format_payload(), kp["private_key"])

        data = _parsed(key)

        assert data.term is None

    def test_old_key_defaults_licence_id_to_none(self, kp: dict[str, str]) -> None:
        key = _sign_payload(_old_format_payload(), kp["private_key"])

        data = _parsed(key)

        assert data.licence_id is None

    def test_old_key_defaults_issued_at_to_none(self, kp: dict[str, str]) -> None:
        key = _sign_payload(_old_format_payload(), kp["private_key"])

        data = _parsed(key)

        assert data.issued_at is None

    def test_old_key_verifies_through_the_sync_verifier(self, kp: dict[str, str]) -> None:
        """Every presence-trusting call site goes through ``verify_license_key``."""
        key = _sign_payload(_old_format_payload(), kp["private_key"])

        data = verify_license_key(key)

        assert data is not None
        assert data.renewal_mode == RENEWAL_MODE_MANUAL
        assert data.term is None

    def test_old_key_payload_is_unchanged_by_parsing(self, kp: dict[str, str]) -> None:
        """Parsing must not inject claims into what the signature covered."""
        payload = _old_format_payload()
        key = _sign_payload(payload, kp["private_key"])

        data = _parsed(key)

        assert data.raw_payload == payload


# ---------------------------------------------------------------------------
# 2. New-format round-trip
# ---------------------------------------------------------------------------


class TestNewFormatRoundTrip:
    def test_generate_emits_all_claims(self, kp: dict[str, str]) -> None:
        key = generate_team_license(
            "Acme",
            term_months=24,
            private_key_hex=kp["private_key"],
            renewal_mode=RENEWAL_MODE_AUTO,
            licence_id="lic-renew-123",
        )

        data = _parsed(key)

        assert data.renewal_mode == RENEWAL_MODE_AUTO
        assert data.term == 24
        assert data.licence_id == "lic-renew-123"
        assert data.issued_at is not None

    def test_issued_at_is_a_parseable_iso_timestamp(self, kp: dict[str, str]) -> None:
        key = generate_team_license("Acme", private_key_hex=kp["private_key"])

        data = _parsed(key)

        assert data.issued_at is not None
        issued = datetime.fromisoformat(data.issued_at)
        assert issued.tzinfo is not None

    def test_claims_are_present_in_the_raw_payload(self, kp: dict[str, str]) -> None:
        key = generate_team_license(
            "Acme",
            private_key_hex=kp["private_key"],
            renewal_mode=RENEWAL_MODE_AUTO,
            term_months=12,
            licence_id="lic-raw",
        )

        payload = _payload_of(key)

        assert payload["renewal_mode"] == RENEWAL_MODE_AUTO
        assert payload["term"] == 12
        assert payload["licence_id"] == "lic-raw"
        assert "issued_at" in payload

    def test_defaults_are_manual_with_a_fresh_licence_id(self, kp: dict[str, str]) -> None:
        """Existing call sites pass no renewal arguments and must keep working."""
        first = _parsed(generate_team_license("Acme", private_key_hex=kp["private_key"]))
        second = _parsed(generate_team_license("Acme", private_key_hex=kp["private_key"]))

        assert first.renewal_mode == DEFAULT_RENEWAL_MODE
        assert first.renewal_mode == RENEWAL_MODE_MANUAL
        assert first.term == 12
        assert first.licence_id
        assert second.licence_id
        assert first.licence_id != second.licence_id

    def test_encode_license_key_round_trips_explicit_claims(self, kp: dict[str, str]) -> None:
        payload = {
            "tier": "team",
            "features": ["sso"],
            "expires_at": "2999-01-01T00:00:00+00:00",
            "org_id": "o1",
            "renewal_mode": RENEWAL_MODE_AUTO,
            "term": 36,
            "licence_id": "lic-explicit",
            "issued_at": datetime.now(UTC).isoformat(),
        }

        data = _parsed(encode_license_key(payload, kp["private_key"]))

        assert data.renewal_mode == RENEWAL_MODE_AUTO
        assert data.term == 36
        assert data.licence_id == "lic-explicit"

    def test_explicit_manual_renewal_mode_is_preserved(self, kp: dict[str, str]) -> None:
        key = generate_team_license(
            "Acme",
            private_key_hex=kp["private_key"],
            renewal_mode=RENEWAL_MODE_MANUAL,
        )

        assert _parsed(key).renewal_mode == RENEWAL_MODE_MANUAL

    def test_signer_rejects_an_unknown_renewal_mode(self) -> None:
        """A typo must not silently mint the default (possibly downgrading auto)."""
        with pytest.raises(LicenseSigningError, match="renewal_mode"):
            build_team_payload("Acme", renewal_mode="automatic")

    def test_signer_rejects_a_non_string_renewal_mode(self) -> None:
        with pytest.raises(LicenseSigningError, match="renewal_mode"):
            build_team_payload("Acme", renewal_mode=1)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 3. The claims are INSIDE the signed payload
# ---------------------------------------------------------------------------


class TestClaimsAreSigned:
    def test_flipping_renewal_mode_without_re_signing_is_rejected(self, kp: dict[str, str]) -> None:
        """A manual key cannot be edited into an auto one: the claim is signed."""
        key = generate_team_license(
            "Acme",
            private_key_hex=kp["private_key"],
            renewal_mode=RENEWAL_MODE_MANUAL,
            licence_id="lic-tamper",
        )
        payload = _payload_of(key)
        payload["renewal_mode"] = RENEWAL_MODE_AUTO
        tampered_payload_b64 = (
            base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
            .decode()
            .rstrip("=")
        )
        tampered = f"{tampered_payload_b64}.{key.split('.', 1)[1]}"

        result = parse_and_verify(tampered)

        assert result.valid is False
        assert result.license_data is None
        assert "Signature" in (result.error or "")

    def test_flipping_term_without_re_signing_is_rejected(self, kp: dict[str, str]) -> None:
        key = generate_team_license("Acme", term_months=12, private_key_hex=kp["private_key"])
        payload = _payload_of(key)
        payload["term"] = 120
        tampered_payload_b64 = (
            base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
            .decode()
            .rstrip("=")
        )
        tampered = f"{tampered_payload_b64}.{key.split('.', 1)[1]}"

        result = parse_and_verify(tampered)

        assert result.valid is False

    def test_flipping_licence_id_without_re_signing_is_rejected(self, kp: dict[str, str]) -> None:
        key = generate_team_license("Acme", private_key_hex=kp["private_key"], licence_id="lic-a")
        payload = _payload_of(key)
        payload["licence_id"] = "lic-b"
        tampered_payload_b64 = (
            base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
            .decode()
            .rstrip("=")
        )
        tampered = f"{tampered_payload_b64}.{key.split('.', 1)[1]}"

        result = parse_and_verify(tampered)

        assert result.valid is False

    def test_a_re_signed_payload_is_accepted(self, kp: dict[str, str]) -> None:
        """Control: the same claims ARE trusted when the authority signed them."""
        key = _sign_payload(
            {
                "tier": "team",
                "features": ["sso"],
                "expires_at": "2999-01-01T00:00:00+00:00",
                "org_id": "o1",
                "renewal_mode": RENEWAL_MODE_AUTO,
                "term": 120,
                "licence_id": "lic-b",
            },
            kp["private_key"],
        )

        data = _parsed(key)

        assert data.renewal_mode == RENEWAL_MODE_AUTO
        assert data.term == 120
        assert data.licence_id == "lic-b"


# ---------------------------------------------------------------------------
# 4. Invalid claim values fall back to the safe defaults
# ---------------------------------------------------------------------------


class TestInvalidClaimsFallBackSafely:
    @pytest.mark.parametrize("term", [0, -1, True, "12", 12.5])
    def test_invalid_term_resolves_to_none(self, kp: dict[str, str], term: object) -> None:
        key = _sign_payload({**_old_format_payload(), "term": term}, kp["private_key"])

        assert _parsed(key).term is None

    def test_unknown_renewal_mode_resolves_to_manual(self, kp: dict[str, str]) -> None:
        key = _sign_payload({**_old_format_payload(), "renewal_mode": "automatic"}, kp["private_key"])

        assert _parsed(key).renewal_mode == RENEWAL_MODE_MANUAL

    @pytest.mark.parametrize("bad", ["", 7, None])
    def test_invalid_licence_id_resolves_to_none(self, kp: dict[str, str], bad: object) -> None:
        key = _sign_payload({**_old_format_payload(), "licence_id": bad}, kp["private_key"])

        assert _parsed(key).licence_id is None


# ---------------------------------------------------------------------------
# 5. Stacking — an early renewal must never cost the customer days (FAR-2075)
# ---------------------------------------------------------------------------

#: Pinned "now" for the stacking tests. A fixed instant makes the boundary
#: assertions EXACT ("2027-07-05T12:00:00+00:00") instead of tolerance-based —
#: the behaviour under test is base-date selection, not month arithmetic.
_FROZEN_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


class _FrozenDatetime(datetime):
    """``datetime`` whose ``now()`` is pinned to :data:`_FROZEN_NOW`."""

    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
        return _FROZEN_NOW if tz is not None else _FROZEN_NOW.replace(tzinfo=None)


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Pin ``license_signing``'s clock so every derived instant is exact."""
    monkeypatch.setattr(license_signing_module, "datetime", _FrozenDatetime)
    return _FROZEN_NOW


class TestStackingBaseDate:
    """``expires_from`` selects the base the term runs from.

    The invariant is ``new_expiry = max(now, previous_expiry) + term``:
    renewing early stacks onto the previous expiry (no lost days), renewing
    after expiry starts a full term from today (no inherited lost days), and a
    fresh licence simply expires ``now + term``.
    """

    def test_renewal_twenty_days_early_stacks_on_the_previous_expiry(self, frozen_now: datetime) -> None:
        previous_expiry = frozen_now + timedelta(days=20)

        payload = build_team_payload("Acme", term_months=12, expires_from=previous_expiry)

        # 2026-07-05 + 12 months — the OLD expiry plus a full term.
        assert payload["expires_at"] == "2027-07-05T12:00:00+00:00"
        # And emphatically NOT the naive "now + term" that would burn 20 days.
        assert payload["expires_at"] != "2027-06-15T12:00:00+00:00"

    def test_early_renewal_extends_by_the_full_term_at_read_back(
        self, kp: dict[str, str], frozen_now: datetime
    ) -> None:
        """The stacked expiry survives signing and verification, not just the dict."""
        previous_expiry = frozen_now + timedelta(days=20)

        key = generate_team_license(
            "Acme",
            term_months=12,
            private_key_hex=kp["private_key"],
            expires_from=previous_expiry,
        )

        data = _parsed(key)
        assert data.expires_at == "2027-07-05T12:00:00+00:00"
        assert datetime.fromisoformat(data.expires_at) - previous_expiry == timedelta(days=365)

    def test_renewal_after_expiry_starts_from_now(self, frozen_now: datetime) -> None:
        previous_expiry = frozen_now - timedelta(days=5)

        payload = build_team_payload("Acme", term_months=12, expires_from=previous_expiry)

        # The floor wins: a lapsed licence gets a FULL term from today.
        assert payload["expires_at"] == "2027-06-15T12:00:00+00:00"
        # Not "previous expiry + term", which would land 5 days in the past.
        assert payload["expires_at"] != "2027-06-10T12:00:00+00:00"

    def test_fresh_licence_expires_now_plus_term(self, frozen_now: datetime) -> None:
        payload = build_team_payload("Acme", term_months=12)

        assert payload["expires_at"] == "2027-06-15T12:00:00+00:00"

    def test_one_second_past_the_boundary_renews_from_now(self, frozen_now: datetime) -> None:
        payload = build_team_payload("Acme", term_months=12, expires_from=frozen_now - timedelta(seconds=1))

        assert payload["expires_at"] == "2027-06-15T12:00:00+00:00"

    def test_one_second_before_the_boundary_stacks_on_the_previous_expiry(self, frozen_now: datetime) -> None:
        payload = build_team_payload("Acme", term_months=12, expires_from=frozen_now + timedelta(seconds=1))

        assert payload["expires_at"] == "2027-06-15T12:00:01+00:00"

    def test_stacking_does_not_move_issued_at(self, frozen_now: datetime) -> None:
        """``issued_at`` is the TRUE mint time — stacking shifts only the expiry.

        Passing a forged ``issued_at`` would have been the easy way to express
        "start where the last key ended", and it would falsify the claim.
        """
        previous_expiry = frozen_now + timedelta(days=20)

        payload = build_team_payload("Acme", term_months=12, expires_from=previous_expiry)

        assert payload["issued_at"] == "2026-06-15T12:00:00+00:00"
        assert payload["expires_at"] != payload["issued_at"]

    def test_a_stacked_renewal_carries_its_own_term_and_auto_mode(
        self, kp: dict[str, str], frozen_now: datetime
    ) -> None:
        """A renewal key states what it is, so the NEXT renewal can compute too."""
        key = generate_team_license(
            "Acme",
            term_months=12,
            private_key_hex=kp["private_key"],
            renewal_mode=RENEWAL_MODE_AUTO,
            expires_from=frozen_now + timedelta(days=20),
        )

        data = _parsed(key)
        assert data.renewal_mode == RENEWAL_MODE_AUTO
        assert data.term == 12
        assert data.expires_at == "2027-07-05T12:00:00+00:00"

    def test_a_naive_base_date_is_rejected_rather_than_assumed_utc(self) -> None:
        with pytest.raises(LicenseSigningError, match="timezone-aware"):
            build_team_payload("Acme", expires_from=datetime(2026, 6, 15, 12, 0, 0))
