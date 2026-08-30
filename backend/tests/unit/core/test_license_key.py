"""Tests for the license module default key format and validation."""

import base64
import json
import re
from collections.abc import Generator
from types import SimpleNamespace

import pytest

from modulo.core.license import (
    _LICENSE_PUBLIC_KEY_HEX,
    LicenseData,
    LicenseError,
    _check_expired,
    _decode_license_key,
    _validate_public_key_hex,
    check_production_public_key,
    clear_license,
    get_license,
    parse_and_verify,
    set_public_key,
    store_license,
)
from modulo.core.registry.crypto import generate_keypair, sign_primitive

_ORIGINAL_KEY = _LICENSE_PUBLIC_KEY_HEX


def _build_signed_key(payload: dict, private_key_hex: str) -> str:
    """Encode a payload + signature into the <payload>.<signature> license key format."""
    payload_b64 = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        .decode()
        .rstrip("=")
    )
    sig_b64 = base64.urlsafe_b64encode(bytes.fromhex(sign_primitive(payload, private_key_hex))).decode().rstrip("=")
    return f"{payload_b64}.{sig_b64}"


@pytest.fixture(autouse=True)
def _reset_key() -> Generator[None, None, None]:
    yield
    set_public_key(_ORIGINAL_KEY)


class TestDefaultPublicKey:
    def test_key_is_64_hex_chars(self) -> None:
        assert len(_LICENSE_PUBLIC_KEY_HEX) == 64

    def test_key_is_valid_hex(self) -> None:
        raw = bytes.fromhex(_LICENSE_PUBLIC_KEY_HEX)
        assert raw.hex() == _LICENSE_PUBLIC_KEY_HEX
        assert len(raw) == 32  # Ed25519 public key

    def test_key_contains_only_lowercase_hex(self) -> None:
        assert re.fullmatch(r"[0-9a-f]{64}", _LICENSE_PUBLIC_KEY_HEX)


class TestValidatePublicKeyHex:
    @pytest.mark.parametrize(
        ("key", "expect_error", "error_match"),
        [
            ("a" * 64, False, None),
            ("A" * 64, False, None),
            ("a" * 63, True, "must be 64 hex chars"),
            ("a" * 65, True, "must be 64 hex chars"),
            ("", True, "must be 64 hex chars"),
            ("z" + "a" * 63, True, "not valid hex"),
        ],
    )
    def test_validate_key(self, key: str, expect_error: bool, error_match: str | None) -> None:
        if expect_error:
            with pytest.raises(ValueError, match=error_match):
                _validate_public_key_hex(key)
        else:
            assert _validate_public_key_hex(key) is None


class TestSetPublicKeyValidates:
    def test_set_public_key_rejects_short_key(self) -> None:
        with pytest.raises(ValueError, match="must be 64 hex chars"):
            set_public_key("a" * 63)

    def test_set_public_key_accepts_valid_key(self) -> None:
        kp = generate_keypair()
        set_public_key(kp["public_key"])

        # a license signed by the newly-configured keypair must now verify
        payload = {"tier": "team", "org_id": "test"}
        result = parse_and_verify(_build_signed_key(payload, kp["private_key"]))
        assert result.valid is True
        assert result.license_data is not None
        assert result.license_data.tier == "team"
        assert result.license_data.org_id == "test"


class TestSignThenVerifyWithDefaultKey:
    def test_default_key_rejects_unknown_signature(self) -> None:
        kp = generate_keypair()
        payload = {"tier": "community", "org_id": "test"}
        result = parse_and_verify(_build_signed_key(payload, kp["private_key"]))
        assert result.valid is False
        assert "Signature" in (result.error or "")


# ---------------------------------------------------------------------------
# _decode_license_key
# ---------------------------------------------------------------------------


class TestDecodeLicenseKey:
    def test_missing_dot_raises_format_error(self) -> None:
        with pytest.raises(LicenseError, match=r"expected <payload>.<signature>"):
            _decode_license_key("no-dot-separator")

    def test_invalid_base64_raises(self) -> None:
        with pytest.raises(LicenseError, match="Invalid base64 encoding"):
            _decode_license_key("abcd.a")

    def test_roundtrip_decodes_signed_key(self) -> None:
        payload = {"tier": "community", "org_id": "test"}
        kp = generate_keypair()
        key = _build_signed_key(payload, kp["private_key"])
        payload_bytes, sig_bytes = _decode_license_key(key)
        assert json.loads(payload_bytes) == payload
        assert len(sig_bytes) == 64  # Ed25519 signature


# ---------------------------------------------------------------------------
# _check_expired
# ---------------------------------------------------------------------------


class TestCheckExpired:
    def test_future_date_is_not_expired(self) -> None:
        assert _check_expired("2999-01-01T00:00:00+00:00") is None

    def test_past_date_is_expired(self) -> None:
        assert _check_expired("2000-01-01T00:00:00+00:00") == "License has expired"

    def test_invalid_format(self) -> None:
        assert _check_expired("not-a-date") == "Invalid expires_at format: not-a-date"


# ---------------------------------------------------------------------------
# parse_and_verify error paths
# ---------------------------------------------------------------------------


class TestParseAndVerifyMalformedKeys:
    def test_missing_dot_returns_invalid(self) -> None:
        result = parse_and_verify("no-dot-separator")
        assert result.valid is False
        assert result.license_data is None
        assert "Invalid license key format" in (result.error or "")

    def test_invalid_base64_returns_invalid(self) -> None:
        result = parse_and_verify("abcd.a")
        assert result.valid is False
        assert "Invalid base64 encoding" in (result.error or "")

    def test_non_json_payload_returns_invalid(self) -> None:
        payload_b64 = base64.urlsafe_b64encode(b"not json").decode().rstrip("=")
        sig_b64 = base64.urlsafe_b64encode(b"x" * 64).decode().rstrip("=")
        result = parse_and_verify(f"{payload_b64}.{sig_b64}")
        assert result.valid is False
        assert "Invalid JSON payload" in (result.error or "")

    def test_non_dict_payload_returns_invalid(self) -> None:
        payload_b64 = base64.urlsafe_b64encode(b"[1, 2]").decode().rstrip("=")
        sig_b64 = base64.urlsafe_b64encode(b"x" * 64).decode().rstrip("=")
        result = parse_and_verify(f"{payload_b64}.{sig_b64}")
        assert result.valid is False
        assert "Payload must be a JSON object" in (result.error or "")


class TestParseAndVerifySigned:
    """Signed-key verification against a configured (non-default) public key."""

    @pytest.fixture
    def kp(self) -> dict:
        kp = generate_keypair()
        set_public_key(kp["public_key"])
        return kp

    def test_valid_signed_key_is_verified(self, kp: dict) -> None:
        result = parse_and_verify(
            _build_signed_key(
                {"tier": "team", "org_id": "o1", "features": ["hitl"], "expires_at": "2999-01-01T00:00:00+00:00"},
                kp["private_key"],
            )
        )
        assert result.valid is True
        assert result.license_data is not None
        assert result.license_data.tier == "team"
        assert result.license_data.org_id == "o1"
        assert result.license_data.features == ["hitl"]

    def test_expired_signed_key_rejected(self, kp: dict) -> None:
        result = parse_and_verify(
            _build_signed_key(
                {"tier": "community", "expires_at": "2000-01-01T00:00:00+00:00"},
                kp["private_key"],
            )
        )
        assert result.valid is False
        assert result.error == "License has expired"

    def test_invalid_expires_at_format_rejected(self, kp: dict) -> None:
        result = parse_and_verify(
            _build_signed_key(
                {"tier": "community", "expires_at": "not-a-date"},
                kp["private_key"],
            )
        )
        assert result.valid is False
        assert result.error == "Invalid expires_at format: not-a-date"

    def test_signature_by_unknown_key_rejected(self, kp: dict) -> None:
        other = generate_keypair()
        result = parse_and_verify(_build_signed_key({"tier": "community"}, other["private_key"]))
        assert result.valid is False
        assert result.error == "Signature verification failed"

    def test_tampered_payload_rejected(self, kp: dict) -> None:
        key = _build_signed_key({"tier": "community", "org_id": "o1"}, kp["private_key"])
        sig_b64 = key.split(".", 1)[1]
        tampered_payload = base64.urlsafe_b64encode(json.dumps({"tier": "team"}).encode()).decode().rstrip("=")
        result = parse_and_verify(f"{tampered_payload}.{sig_b64}")
        assert result.valid is False
        assert result.error == "Signature verification failed"


# ---------------------------------------------------------------------------
# In-memory license store lifecycle
# ---------------------------------------------------------------------------


class TestLicenseStore:
    def test_get_license_returns_none_when_empty(self) -> None:
        clear_license()
        assert get_license() is None

    def test_store_then_get_returns_data(self) -> None:
        clear_license()
        data = LicenseData(
            tier="community",
            features=["f1"],
            expires_at="",
            org_id="o1",
            raw_payload={"tier": "community"},
            raw_key="key",
        )
        store_license("ignored", data)
        assert get_license() is data

    def test_store_without_expiry_is_returned(self) -> None:
        clear_license()
        data = LicenseData(
            tier="team",
            features=[],
            expires_at="",
            org_id="o2",
            raw_payload={},
            raw_key="key",
        )
        store_license("ignored", data)
        assert get_license() is data

    def test_get_license_clears_expired_stored_license(self) -> None:
        clear_license()
        data = LicenseData(
            tier="community",
            features=[],
            expires_at="2000-01-01T00:00:00+00:00",
            org_id="o1",
            raw_payload={},
            raw_key="key",
        )
        store_license("ignored", data)
        assert get_license() is None
        assert get_license() is None  # cleared, not just hidden

    def test_clear_license_empties_store(self) -> None:
        data = LicenseData(
            tier="community",
            features=[],
            expires_at="",
            org_id="o1",
            raw_payload={},
            raw_key="key",
        )
        store_license("ignored", data)
        clear_license()
        assert get_license() is None


# ---------------------------------------------------------------------------
# check_production_public_key
# ---------------------------------------------------------------------------


class TestCheckProductionPublicKey:
    def test_dev_key_logs_critical_when_not_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        set_public_key(_ORIGINAL_KEY)
        with caplog.at_level("CRITICAL", logger="modulo.core.license"):
            check_production_public_key(SimpleNamespace(debug=False))
        assert "LICENSE_CRITICAL" in caplog.text
        assert "dev/test license public key" in caplog.text

    def test_dev_key_silent_when_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        set_public_key(_ORIGINAL_KEY)
        with caplog.at_level("CRITICAL", logger="modulo.core.license"):
            check_production_public_key(SimpleNamespace(debug=True))
        assert "LICENSE_CRITICAL" not in caplog.text

    def test_configured_key_silent_when_not_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        kp = generate_keypair()
        set_public_key(kp["public_key"])
        with caplog.at_level("CRITICAL", logger="modulo.core.license"):
            check_production_public_key(SimpleNamespace(debug=False))
        assert "LICENSE_CRITICAL" not in caplog.text
