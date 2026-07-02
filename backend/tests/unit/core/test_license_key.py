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


@pytest.fixture(autouse=True)
def _reset_key() -> Generator[None, None, None]:
    yield
    set_public_key(_ORIGINAL_KEY)


class TestDefaultPublicKey:
    def test_key_is_64_hex_chars(self) -> None:
        assert len(_LICENSE_PUBLIC_KEY_HEX) == 64

    def test_key_is_valid_hex(self) -> None:
        bytes.fromhex(_LICENSE_PUBLIC_KEY_HEX)

    def test_key_contains_only_lowercase_hex(self) -> None:
        assert re.fullmatch(r"[0-9a-f]{64}", _LICENSE_PUBLIC_KEY_HEX)


class TestValidatePublicKeyHex:
    def test_accepts_valid_key(self) -> None:
        _validate_public_key_hex("a" * 64)

    def test_rejects_short_key(self) -> None:
        with pytest.raises(ValueError, match="must be 64 hex chars"):
            _validate_public_key_hex("a" * 63)

    def test_rejects_long_key(self) -> None:
        with pytest.raises(ValueError, match="must be 64 hex chars"):
            _validate_public_key_hex("a" * 65)

    def test_rejects_empty_string(self) -> None:
        with pytest.raises(ValueError, match="must be 64 hex chars"):
            _validate_public_key_hex("")

    def test_rejects_non_hex_chars(self) -> None:
        with pytest.raises(ValueError, match="not valid hex"):
            _validate_public_key_hex("z" + "a" * 63)

    def test_rejects_uppercase_hex(self) -> None:
        _validate_public_key_hex("A" * 64)


class TestSetPublicKeyValidates:
    def test_set_public_key_rejects_short_key(self) -> None:
        with pytest.raises(ValueError, match="must be 64 hex chars"):
            set_public_key("a" * 63)

    def test_set_public_key_accepts_valid_key(self) -> None:
        kp = generate_keypair()
        set_public_key(kp["public_key"])


class TestSignThenVerifyWithDefaultKey:
    def test_default_key_rejects_unknown_signature(self) -> None:
        kp = generate_keypair()
        payload = {"tier": "community", "org_id": "test"}
        sig_hex = sign_primitive(payload, kp["private_key"])
        sig_bytes = bytes.fromhex(sig_hex)

        payload_b64 = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        ).decode().rstrip("=")
        sig_b64 = base64.urlsafe_b64encode(sig_bytes).decode().rstrip("=")
        key = f"{payload_b64}.{sig_b64}"

        result = parse_and_verify(key)
        assert result.valid is False
        assert "Signature" in (result.error or "")
