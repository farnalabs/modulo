"""Helpers for encrypted secret columns with legacy plaintext compatibility."""

from cryptography.fernet import Fernet, InvalidToken


class SecretStorageError(Exception):
    """Base exception for secret storage operations."""


class InvalidSecretTypeError(SecretStorageError):
    """Raised when the stored secret is neither text nor bytes."""


class DecryptionError(SecretStorageError):
    """Raised when an encrypted secret cannot be decrypted."""


class CorruptSecretError(SecretStorageError):
    """Raised when decrypted data is not valid UTF-8."""


def encrypt_stored_secret(value: str, fernet_key: str) -> bytes:
    """Encode and encrypt a secret for a binary database column."""
    return Fernet(fernet_key.encode()).encrypt(value.encode())


def decode_stored_secret(value: object, fernet_key: str) -> str:
    """Decode encrypted bytes or legacy plaintext string/byte values."""
    if isinstance(value, str):
        return value
    if not isinstance(value, bytes):
        raise InvalidSecretTypeError("Stored secret must be text or bytes")

    try:
        plaintext = Fernet(fernet_key.encode()).decrypt(value)
    except InvalidToken as exc:
        if value.startswith(b"gAAAA"):
            raise DecryptionError(
                "Stored encrypted secret cannot be decrypted"
            ) from exc
        plaintext = value
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CorruptSecretError(
            "Stored secret is not valid encrypted or UTF-8 data"
        ) from exc
