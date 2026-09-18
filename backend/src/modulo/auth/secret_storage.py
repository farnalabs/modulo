"""Helpers for encrypted secret columns with legacy plaintext compatibility.

Two layers exist for decryption:

* :func:`decode_stored_secret` — the PURE, session-less helper. It has no DB
  session or RLS context and therefore cannot guarantee that the value it
  decrypts was read under tenant isolation. It is the **UNSCOPED FALLBACK** and
  must ONLY be used outside a request/DB path (e.g. CLI tooling, key rotation,
  pure transforms that never touch a tenant-scoped read). Request and DB paths
  MUST use :func:`decode_stored_secret_scoped` instead, which refuses to decrypt
  without an active transaction and an RLS org context.

* :func:`decode_stored_secret_scoped` — the CONTEXT-BOUND decrypt for
  request/DB paths. It requires an active transaction and an RLS org context,
  raising ``RuntimeError`` when either is missing, so that "decrypt without
  org scope" fails loudly (and is testable) instead of silently leaking a
  plaintext outside a tenant context (the FAR-519/522 gap class).
"""

import uuid
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


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

    **UNSCOPED FALLBACK** — this function is a pure, session-less helper. It has
    no DB session and no RLS org context, so it cannot guarantee the value was
    read under tenant isolation. Use it ONLY outside a request/DB path (CLI,
    key rotation, pure transforms that never touch a tenant-scoped read); a
    request/DB path MUST use :func:`decode_stored_secret_scoped`.

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


async def decode_stored_secret_scoped(
    session: "AsyncSession",
    value: object,
    fernet_key: str,
    org_id: uuid.UUID | None = None,
) -> str:
    """CONTEXT-BOUND decrypt: only within an active transaction + RLS org scope.

    This is the sanctioned decrypt entrypoint for request/DB paths. It makes
    "decrypt without org RLS scope" fail loudly (and be testable) instead of
    structurally permitting it — the FAR-519/522 gap, where the pure
    :func:`decode_stored_secret` could be called with no session or org.

    Preconditions, each enforced by raising ``RuntimeError``:

    * an active DB transaction — ``session.in_transaction()`` must be truthy
      (the caller MUST wrap in ``async with session.begin():``);
    * an RLS org context — either passed as ``org_id`` or already bound to the
      session (Postgres ``current_setting('app.organisation_id')`` or
      ``session.info['org_id']`` on generic backends).

    After the guard passes, the helper (re-)applies ``set_rls_org`` within the
    transaction and delegates the actual decryption to
    :func:`decode_stored_secret`.

    Args:
        session: The SQLAlchemy async session bound to the current transaction.
        value: The stored secret column value (Fernet bytes/base64 str or a
            legacy plaintext).
        fernet_key: The Fernet key used to encrypt the value.
        org_id: Optional explicit org. When ``None`` it is read from the session's
            RLS context.

    Returns:
        The decrypted plaintext.

    Raises:
        RuntimeError: If there is no active transaction or no RLS org context.
    """
    from modulo.db.rls import set_rls_org

    if not session.in_transaction():
        raise RuntimeError(
            "decode_stored_secret_scoped requires an active transaction; wrap the call in `async with session.begin():`"
        )

    if org_id is None:
        org_id = await _read_rls_org_id(session)

    if org_id is None:
        raise RuntimeError("RLS organisation context not set")

    await set_rls_org(session, org_id)

    return decode_stored_secret(value, fernet_key)


async def _read_rls_org_id(session: "AsyncSession") -> uuid.UUID | None:
    """Read the RLS org id currently bound to *session*, or ``None``.

    Generic backends store the org in ``session.info['org_id']``; Postgres reads
    ``current_setting('app.organisation_id', true)`` (empty string when unset).
    A value that cannot be coerced to a UUID is treated as absent.
    """
    from sqlalchemy import text

    info = getattr(session, "info", None)
    if isinstance(info, dict):
        value = info.get("org_id")
        if value is not None:
            parsed = _coerce_org_id(value)
            if parsed is not None:
                return parsed

    try:
        result = await session.execute(text("SELECT current_setting('app.organisation_id', true)"))
    except Exception:
        return None
    raw: object = result.scalar()
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip() == "":
        return None
    return _coerce_org_id(raw)


def _coerce_org_id(value: object) -> uuid.UUID | None:
    """Return a ``UUID`` when *value* is a valid UUID/str, else ``None``."""
    try:
        return uuid.UUID(str(value).strip())
    except (ValueError, AttributeError, TypeError):
        return None
