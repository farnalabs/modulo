"""Helpers for encrypted secret columns with legacy plaintext compatibility."""

from cryptography.fernet import Fernet, InvalidToken


def encrypt_stored_secret(value: str, fernet_key: str) -> bytes:
    """Encode and encrypt a secret for a binary database column."""
    return Fernet(fernet_key.encode()).encrypt(value.encode())


def decode_stored_secret(value: object, fernet_key: str) -> str:
    """Decode encrypted bytes or legacy plaintext string/byte values."""
    if isinstance(value, str):
        return value
    if not isinstance(value, bytes):
        raise ValueError("Stored secret must be text or bytes")

    try:
        plaintext = Fernet(fernet_key.encode()).decrypt(value)
    except InvalidToken as exc:
        if value.startswith(b"gAAAA"):
            raise ValueError("Stored encrypted secret cannot be decrypted") from exc
        plaintext = value
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Stored secret is not valid encrypted or UTF-8 data") from exc
