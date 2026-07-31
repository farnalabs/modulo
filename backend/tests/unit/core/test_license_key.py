"""Tests for the license module default key format and validation."""

import base64
import json
import re
from collections.abc import Generator

import pytest

from modulo.core.license import (
    _LICENSE_PUBLIC_KEY_HEX,
    _validate_public_key_hex,
    parse_and_verify,
    set_public_key,
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
