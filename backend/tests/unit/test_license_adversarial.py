"""Adversarial license-key validation tests.

Prove that the license verification code rejects:
- Tampered signatures
- Expired keys
- Forged payloads
- Algorithm confusion / downgrade
- Clock skew boundary

The thesis: "An adversary who gains access to a valid license key must not be
able to forge, extend, or replay it."
"""

from __future__ import annotations

import base64
import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from modulo.core.license import (
    _LICENSE_PUBLIC_KEY_HEX,
    LicenseError,
    parse_and_verify,
    set_public_key,
)
from modulo.core.registry.crypto import generate_keypair, sign_primitive

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def authority_keys() -> dict[str, str]:
    """Generate a single Ed25519 keypair used as the "license authority"."""
    return generate_keypair()


@pytest.fixture(scope="module", autouse=True)
def _use_test_public_key(authority_keys: dict[str, str]) -> Generator[None, None, None]:
    """Replace the module-level public key with our test key for these tests."""
    original = _LICENSE_PUBLIC_KEY_HEX
    set_public_key(authority_keys["public_key"])
    yield
    set_public_key(original)


@pytest.fixture(scope="module")
def wrong_keys() -> dict[str, str]:
    """A second keypair — the "wrong" authority."""
    return generate_keypair()


@pytest.fixture
def valid_payload() -> dict[str, Any]:
    """A structurally valid license payload."""
    future = (datetime.now(UTC) + timedelta(days=365)).isoformat()
    return {
        "tier": "team",
        "features": ["audit_viewer", "sso", "scim"],
        "expires_at": future,
        "org_id": "00000000-0000-0000-0000-000000000001",
    }


@pytest.fixture
def valid_license_key(valid_payload: dict[str, Any], authority_keys: dict[str, str]) -> str:
    """Encode and sign a valid payload as a license key string."""
    payload_bytes = json.dumps(valid_payload, separators=(",", ":"), sort_keys=True).encode()
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
    sig = sign_primitive(valid_payload, authority_keys["private_key"])
    sig_b64 = base64.urlsafe_b64encode(bytes.fromhex(sig)).rstrip(b"=").decode()
    return f"{payload_b64}.{sig_b64}"


# ── Test module import: must not crash ────────────────────────────────────


class TestLicenseModuleImport:
    """Verify the license module loads without errors."""

    def test_parse_and_verify_importable(self) -> None:
        from modulo.core.license import parse_and_verify

        assert callable(parse_and_verify)


# ── Tampered signature ────────────────────────────────────────────────────


class TestTamperedSignature:
    """Prove signature tampering is detected."""

    def test_tampered_signature_rejected(self, valid_license_key: str, valid_payload: dict[str, Any]) -> None:
        sig_b64 = valid_license_key.split(".")[1]
        tampered = list(base64.urlsafe_b64decode(sig_b64 + "=="))
        tampered[0] ^= 0xFF
        tampered_sig_b64 = base64.urlsafe_b64encode(bytes(tampered)).rstrip(b"=").decode()
        tampered_key = f"{valid_license_key.split('.', maxsplit=1)[0]}.{tampered_sig_b64}"

        result = parse_and_verify(tampered_key)
        assert result.valid is False
        assert "Signature verification failed" in (result.error or "")

    def test_truncated_signature_rejected(self, valid_license_key: str) -> None:
        parts = valid_license_key.split(".")
        truncated_sig = parts[1][:10] + "abcd"
        tampered_key = f"{parts[0]}.{truncated_sig}"

        result = parse_and_verify(tampered_key)
        assert result.valid is False

    def test_extra_bytes_in_signature_rejected(self, valid_license_key: str) -> None:
        tampered_key = valid_license_key + "extra"
        result = parse_and_verify(tampered_key)
        assert result.valid is False


# ── Expired key ───────────────────────────────────────────────────────────


class TestExpiredKey:
    """Prove expired license keys are rejected."""

    def test_expired_key_rejected(self, authority_keys: dict[str, str], valid_payload: dict[str, Any]) -> None:
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        payload = {**valid_payload, "expires_at": past}
        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
        sig = sign_primitive(payload, authority_keys["private_key"])
        sig_b64 = base64.urlsafe_b64encode(bytes.fromhex(sig)).rstrip(b"=").decode()
        key = f"{payload_b64}.{sig_b64}"

        result = parse_and_verify(key)
        assert result.valid is False
        assert "expired" in (result.error or "").lower()

    def test_expired_boundary_just_before(self, authority_keys: dict[str, str]) -> None:
        now = datetime.now(UTC)
        one_second_ago = (now - timedelta(seconds=1)).isoformat()
        payload = {
            "tier": "team",
            "features": [],
            "expires_at": one_second_ago,
            "org_id": "00000000-0000-0000-0000-000000000001",
        }
        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
        sig = sign_primitive(payload, authority_keys["private_key"])
        sig_b64 = base64.urlsafe_b64encode(bytes.fromhex(sig)).rstrip(b"=").decode()
        key = f"{payload_b64}.{sig_b64}"

        result = parse_and_verify(key)
        assert result.valid is False
        assert "expired" in (result.error or "").lower()

    def test_future_key_accepted(self, authority_keys: dict[str, str]) -> None:
        far_future = (datetime.now(UTC) + timedelta(days=365)).isoformat()
        payload = {
            "tier": "team",
            "features": [],
            "expires_at": far_future,
            "org_id": "00000000-0000-0000-0000-000000000001",
        }
        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
        sig = sign_primitive(payload, authority_keys["private_key"])
        sig_b64 = base64.urlsafe_b64encode(bytes.fromhex(sig)).rstrip(b"=").decode()
        key = f"{payload_b64}.{sig_b64}"

        result = parse_and_verify(key)
        assert result.valid is True


# ── Wrong org ──────────────────────────────────────────────────────────────


class TestWrongOrg:
    """Verify the org_id field in the payload is extracted and can be validated externally.

    ``parse_and_verify`` does not itself validate org_id (that is the caller's
    responsibility), but we test that the field is correctly extracted.
    """

    def test_org_id_extracted_from_payload(self, valid_license_key: str) -> None:
        result = parse_and_verify(valid_license_key)
        assert result.valid is True
        assert result.license_data is not None
        assert result.license_data.org_id == "00000000-0000-0000-0000-000000000001"

    def test_different_org_id_in_payload(self, authority_keys: dict[str, str]) -> None:
        payload = {
            "tier": "team",
            "features": ["audit_viewer"],
            "expires_at": (datetime.now(UTC) + timedelta(days=365)).isoformat(),
            "org_id": "99999999-9999-9999-9999-999999999999",
        }
        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
        sig = sign_primitive(payload, authority_keys["private_key"])
        sig_b64 = base64.urlsafe_b64encode(bytes.fromhex(sig)).rstrip(b"=").decode()
        key = f"{payload_b64}.{sig_b64}"

        result = parse_and_verify(key)
        assert result.valid is True
        assert result.license_data is not None
        assert result.license_data.org_id == "99999999-9999-9999-9999-999999999999"


# ── Forged payload ────────────────────────────────────────────────────────


class TestForgedPayload:
    """Prove a payload signed with a wrong key is rejected."""

    def test_wrong_key_rejected(self, valid_payload: dict[str, Any], wrong_keys: dict[str, str]) -> None:
        payload_bytes = json.dumps(valid_payload, separators=(",", ":"), sort_keys=True).encode()
        payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
        sig = sign_primitive(valid_payload, wrong_keys["private_key"])
        sig_b64 = base64.urlsafe_b64encode(bytes.fromhex(sig)).rstrip(b"=").decode()
        forged_key = f"{payload_b64}.{sig_b64}"

        result = parse_and_verify(forged_key)
        assert result.valid is False
        assert "Signature verification failed" in (result.error or "")

    def test_random_payload_rejected(self, wrong_keys: dict[str, str]) -> None:
        payload = {"tier": "v2", "features": ["all"], "expires_at": "", "org_id": "hacked"}
        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
        sig = sign_primitive(payload, wrong_keys["private_key"])
        sig_b64 = base64.urlsafe_b64encode(bytes.fromhex(sig)).rstrip(b"=").decode()
        forged_key = f"{payload_b64}.{sig_b64}"

        result = parse_and_verify(forged_key)
        assert result.valid is False

    def test_empty_payload_rejected(self) -> None:
        payload_bytes = b"{}"
        payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
        bad_key = f"{payload_b64}.{payload_b64}"
        result = parse_and_verify(bad_key)
        assert result.valid is False


# ── Algorithm confusion ───────────────────────────────────────────────────


class TestAlgorithmConfusion:
    """Prove algorithm downgrade / confusion attacks are rejected.

    ``parse_and_verify`` uses Ed25519 only — no algorithm negotiation exists.
    Non-Ed25519 signatures or malformed signature bytes must be rejected.
    """

    def test_rsa_signature_rejected(self, valid_payload: dict[str, Any]) -> None:
        payload_bytes = json.dumps(valid_payload, separators=(",", ":"), sort_keys=True).encode()
        payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
        rsa_sig = "a" * 128
        rsa_sig_b64 = base64.urlsafe_b64encode(rsa_sig.encode()).rstrip(b"=").decode()
        key = f"{payload_b64}.{rsa_sig_b64}"

        result = parse_and_verify(key)
        assert result.valid is False

    def test_hmac_signature_rejected(self, valid_payload: dict[str, Any]) -> None:
        payload_bytes = json.dumps(valid_payload, separators=(",", ":"), sort_keys=True).encode()
        payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
        hmac_sig = b"a" * 32
        hmac_b64 = base64.urlsafe_b64encode(hmac_sig).rstrip(b"=").decode()
        key = f"{payload_b64}.{hmac_b64}"

        result = parse_and_verify(key)
        assert result.valid is False

    def test_hex_encoded_sig_misuse_rejected(self, valid_payload: dict[str, Any]) -> None:
        payload_bytes = json.dumps(valid_payload, separators=(",", ":"), sort_keys=True).encode()
        payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
        hex_sig = "deadbeef" * 8
        hex_b64 = base64.urlsafe_b64encode(hex_sig.encode()).rstrip(b"=").decode()
        key = f"{payload_b64}.{hex_b64}"

        result = parse_and_verify(key)
        assert result.valid is False

    def test_none_algorithm_sig_field_rejected(self) -> None:
        payload = {
            "tier": "community",
            "features": [],
            "expires_at": "",
            "org_id": "",
            "alg": "none",
        }
        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
        key = f"{payload_b64}.{payload_b64}"
        result = parse_and_verify(key)
        assert result.valid is False


# ── Clock skew / boundary ─────────────────────────────────────────────────


class TestClockSkewBoundary:
    """Test behaviour just before and after the expiry boundary."""

    def test_valid_up_to_last_microsecond(self, authority_keys: dict[str, str]) -> None:
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=1)
        payload = {
            "tier": "team",
            "features": [],
            "expires_at": expires.isoformat(),
            "org_id": "00000000-0000-0000-0000-000000000001",
        }
        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
        sig = sign_primitive(payload, authority_keys["private_key"])
        sig_b64 = base64.urlsafe_b64encode(bytes.fromhex(sig)).rstrip(b"=").decode()
        key = f"{payload_b64}.{sig_b64}"

        result = parse_and_verify(key)
        assert result.valid is True

    def test_no_expiry_field_accepted(self, authority_keys: dict[str, str]) -> None:
        payload = {
            "tier": "community",
            "features": [],
            "expires_at": "",
            "org_id": "00000000-0000-0000-0000-000000000001",
        }
        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
        sig = sign_primitive(payload, authority_keys["private_key"])
        sig_b64 = base64.urlsafe_b64encode(bytes.fromhex(sig)).rstrip(b"=").decode()
        key = f"{payload_b64}.{sig_b64}"

        result = parse_and_verify(key)
        assert result.valid is True

    def test_invalid_expires_at_format_rejected(self, authority_keys: dict[str, str]) -> None:
        payload = {
            "tier": "team",
            "features": [],
            "expires_at": "not-a-date",
            "org_id": "00000000-0000-0000-0000-000000000001",
        }
        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
        sig = sign_primitive(payload, authority_keys["private_key"])
        sig_b64 = base64.urlsafe_b64encode(bytes.fromhex(sig)).rstrip(b"=").decode()
        key = f"{payload_b64}.{sig_b64}"

        result = parse_and_verify(key)
        assert result.valid is False
        assert "Invalid expires_at" in (result.error or "")

    def test_clock_skew_after_expiry_fails(self, authority_keys: dict[str, str]) -> None:
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        payload = {
            "tier": "team",
            "features": [],
            "expires_at": past,
            "org_id": "00000000-0000-0000-0000-000000000001",
        }
        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
        sig = sign_primitive(payload, authority_keys["private_key"])
        sig_b64 = base64.urlsafe_b64encode(bytes.fromhex(sig)).rstrip(b"=").decode()
        key = f"{payload_b64}.{sig_b64}"

        result = parse_and_verify(key)
        assert result.valid is False


# ── Malformed key format ─────────────────────────────────────────────────


class TestMalformedKeyFormat:
    """Prove malformed license keys are rejected gracefully."""

    def test_missing_dot_rejected(self) -> None:
        result = parse_and_verify("no-dot-separator")
        assert result.valid is False
        assert "format" in (result.error or "").lower()

    def test_multiple_dots_rejected(self) -> None:
        result = parse_and_verify("part1.part2.part3")
        assert result.valid is False

    def test_empty_string_rejected(self) -> None:
        result = parse_and_verify("")
        assert result.valid is False
        assert "format" in (result.error or "").lower()

    def test_invalid_base64_rejected(self) -> None:
        result = parse_and_verify("!!!payload@@@.###signature$$$")
        assert result.valid is False
        assert "base64" in (result.error or "").lower()

    def test_non_dict_payload_rejected(self, authority_keys: dict[str, str]) -> None:
        payload = ["tier", "community"]
        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
        sig = sign_primitive(payload, authority_keys["private_key"])
        sig_b64 = base64.urlsafe_b64encode(bytes.fromhex(sig)).rstrip(b"=").decode()
        key = f"{payload_b64}.{sig_b64}"

        result = parse_and_verify(key)
        assert result.valid is False
        assert "object" in (result.error or "").lower()


# ── Hypothesis fuzz tests ─────────────────────────────────────────────────


class TestLicenseKeyFuzz:
    """Hypothesis-driven fuzz: random strings as license keys must not crash."""

    @given(st.text(min_size=0, max_size=200))
    def test_random_strings_dont_crash(self, key_input: str) -> None:
        try:
            result = parse_and_verify(key_input)
        except LicenseError:
            pass
        except Exception as exc:
            pytest.fail(f"parse_and_verify crashed on {key_input!r}: {exc}")
        else:
            assert isinstance(result.valid, bool)


# ── Public key tampering ──────────────────────────────────────────────────


class TestPublicKeyTampering:
    """Prove that changing the public key invalidates existing signatures."""

    def test_alternate_public_key_rejects_canonical(self, valid_license_key: str, wrong_keys: dict[str, str]) -> None:
        set_public_key(wrong_keys["public_key"])
        try:
            result = parse_and_verify(valid_license_key)
            assert result.valid is False
        finally:
            from modulo.core.license import _LICENSE_PUBLIC_KEY_HEX

            set_public_key(_LICENSE_PUBLIC_KEY_HEX)
