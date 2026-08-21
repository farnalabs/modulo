"""Unit tests for modulo.auth.secret_storage: encrypted secret helpers."""

import pytest
from cryptography.fernet import Fernet

from modulo.auth.secret_storage import (
    CorruptSecretError,
    DecryptionError,
    InvalidFernetKeyError,
    InvalidSecretTypeError,
    decode_stored_secret,
    encrypt_stored_secret,
)

_FERNET_KEY = Fernet.generate_key().decode()


def test_encrypt_stored_secret_roundtrip() -> None:
    plaintext = "my-secret-value"
    encrypted = encrypt_stored_secret(plaintext, _FERNET_KEY)
    assert isinstance(encrypted, bytes)
    assert encrypted != plaintext.encode()
    decoded = decode_stored_secret(encrypted, _FERNET_KEY)
    assert decoded == plaintext


def test_encrypt_stored_secret_with_invalid_key_raises() -> None:
    with pytest.raises(InvalidFernetKeyError, match="Fernet key is not valid"):
        encrypt_stored_secret("secret", "not-a-valid-fernet-key")


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


def test_decode_stored_secret_with_base64_string_roundtrip() -> None:
    """A base64 string persisted via the write path must decrypt back.

    Regression for the review finding that `encrypt_stored_secret(...).decode()`
    stores a `gAAAA...` str in JSON columns but `decode_stored_secret` returned
    every str unchanged, so consumers got ciphertext at runtime.
    """
    plaintext = "smtp-password"
    stored = encrypt_stored_secret(plaintext, _FERNET_KEY).decode()
    assert isinstance(stored, str)
    assert stored.startswith("gAAAA")
    assert decode_stored_secret(stored, _FERNET_KEY) == plaintext


def test_decode_stored_secret_plaintext_with_fernet_prefix_raises() -> None:
    """A plaintext prefixed with gAAAA and non-decryptable must raise."""
    with pytest.raises(DecryptionError, match="cannot be decrypted"):
        decode_stored_secret("gAAAAA-some-ciphertext", _FERNET_KEY)


def test_decode_stored_secret_with_invalid_token_raises() -> None:
    wrong_key = Fernet.generate_key().decode()
    wrong_encrypted = Fernet(wrong_key.encode()).encrypt(b"other-data")
    with pytest.raises(DecryptionError, match="cannot be decrypted"):
        decode_stored_secret(wrong_encrypted, _FERNET_KEY)


def test_decode_stored_secret_with_invalid_key_raises() -> None:
    with pytest.raises(InvalidFernetKeyError, match="Fernet key is not valid"):
        decode_stored_secret(b"gAAAAA-some-ciphertext", "not-a-valid-fernet-key")


def test_decode_stored_secret_with_non_utf8_bytes_raises() -> None:
    non_utf8 = bytes(range(128, 160))
    with pytest.raises(CorruptSecretError, match="not valid encrypted or UTF-8"):
        decode_stored_secret(non_utf8, _FERNET_KEY)


def test_decode_stored_secret_with_non_str_non_bytes_raises() -> None:
    with pytest.raises(InvalidSecretTypeError, match="Stored secret must be text or bytes"):
        decode_stored_secret(42, _FERNET_KEY)
    with pytest.raises(InvalidSecretTypeError, match="Stored secret must be text or bytes"):
        decode_stored_secret(None, _FERNET_KEY)
    with pytest.raises(InvalidSecretTypeError, match="Stored secret must be text or bytes"):
        decode_stored_secret([], _FERNET_KEY)
