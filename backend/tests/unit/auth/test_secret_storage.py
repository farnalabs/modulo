"""Unit tests for modulo.auth.secret_storage: encrypted secret helpers."""

import pytest
from cryptography.fernet import Fernet

from modulo.auth.secret_storage import decode_stored_secret, encrypt_stored_secret

_FERNET_KEY = Fernet.generate_key().decode()


def test_encrypt_stored_secret_roundtrip() -> None:
    plaintext = "my-secret-value"
    encrypted = encrypt_stored_secret(plaintext, _FERNET_KEY)
    assert isinstance(encrypted, bytes)
    assert encrypted != plaintext.encode()
    decoded = decode_stored_secret(encrypted, _FERNET_KEY)
    assert decoded == plaintext


def test_decode_stored_secret_with_fernet_bytes() -> None:
    plaintext = "secret-data"
    encrypted = Fernet(_FERNET_KEY.encode()).encrypt(plaintext.encode())
    result = decode_stored_secret(encrypted, _FERNET_KEY)
    assert result == plaintext


def test_decode_stored_secret_with_legacy_plaintext_string() -> None:
    result = decode_stored_secret("legacy-plaintext", _FERNET_KEY)
    assert result == "legacy-plaintext"


def test_decode_stored_secret_with_legacy_plaintext_bytes() -> None:
    result = decode_stored_secret(b"legacy-bytes", _FERNET_KEY)
    assert result == "legacy-bytes"


def test_decode_stored_secret_with_invalid_token_raises() -> None:
    wrong_key = Fernet.generate_key().decode()
    wrong_encrypted = Fernet(wrong_key.encode()).encrypt(b"other-data")
    with pytest.raises(ValueError, match="cannot be decrypted"):
        decode_stored_secret(wrong_encrypted, _FERNET_KEY)


def test_decode_stored_secret_with_non_utf8_bytes_raises() -> None:
    non_utf8 = bytes(range(128, 160))
    with pytest.raises(ValueError, match="not valid encrypted or UTF-8"):
        decode_stored_secret(non_utf8, _FERNET_KEY)


def test_decode_stored_secret_with_non_str_non_bytes_raises() -> None:
    with pytest.raises(ValueError, match="Stored secret must be text or bytes"):
        decode_stored_secret(42, _FERNET_KEY)
    with pytest.raises(ValueError, match="Stored secret must be text or bytes"):
        decode_stored_secret(None, _FERNET_KEY)
    with pytest.raises(ValueError, match="Stored secret must be text or bytes"):
        decode_stored_secret([], _FERNET_KEY)
