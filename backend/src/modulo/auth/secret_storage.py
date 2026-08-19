"""Helpers for encrypted secret columns with legacy plaintext compatibility."""

from cryptography.fernet import Fernet, InvalidToken


class SecretStorageError(Exception):
    """Base exception for secret storage operations."""


class InvalidFernetKeyError(SecretStorageError):
    """Raised when the Fernet key is invalid or malformed."""


class InvalidSecretTypeError(SecretStorageError):
    """Raised when the stored secret is neither text nor bytes."""


class DecryptionError(SecretStorageError):
    """Raised when an encrypted secret cannot be decrypted."""


class CorruptSecretError(SecretStorageError):
    """Raised when decrypted data is not valid UTF-8."""


def encrypt_stored_secret(value: str, fernet_key: str) -> bytes:
    """Encode and encrypt a secret for a binary database column."""
    try:
        f = Fernet(fernet_key.encode())
    except (ValueError, TypeError) as exc:
        raise InvalidFernetKeyError("Provided Fernet key is not valid") from exc
    return f.encrypt(value.encode())


_FERNET_STR_PREFIX = "gAAAA"  # base64url prefix of every Fernet token


def _is_encrypted_token(value: object) -> bool:
    """Return True when a stored value looks like an encrypted Fernet token."""
    if isinstance(value, str):
        return value.startswith(_FERNET_STR_PREFIX)
    if isinstance(value, bytes):
        return value.startswith(_FERNET_STR_PREFIX.encode())
    return False


def decode_stored_secret(value: object, fernet_key: str) -> str:
    """Decode an encrypted secret (bytes or base64 string) or legacy plaintext.

    The write path may persist the Fernet token either as raw bytes (binary
    columns) or as a base64 ``str`` (JSON columns, via ``.decode()``). Both
    encrypted shapes share a common type with the plaintext/legacy fallback:
    anything that is an encrypted token is decrypted, everything else is
    returned as-is.
    """
    if isinstance(value, str):
        if not _is_encrypted_token(value):
            return value
        raw = _decode_fernet(value.encode(), fernet_key)
    elif isinstance(value, bytes):
        raw = _decode_fernet(value, fernet_key)
    else:
        raise InvalidSecretTypeError("Stored secret must be text or bytes")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CorruptSecretError("Stored secret is not valid encrypted or UTF-8 data") from exc


def _decode_fernet(token: bytes, fernet_key: str) -> bytes:
    """Fernet-decrypt ``token``; fall back to raw bytes when not encrypted."""
    try:
        f = Fernet(fernet_key.encode())
    except (ValueError, TypeError) as exc:
        raise InvalidFernetKeyError("Provided Fernet key is not valid") from exc
    try:
        return f.decrypt(token)
    except InvalidToken as exc:
        if _is_encrypted_token(token):
            raise DecryptionError("Stored encrypted secret cannot be decrypted") from exc
        return token
