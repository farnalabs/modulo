"""Unit tests for modulo.auth.secret_storage: encrypted secret helpers."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet

from modulo.auth.secret_storage import (
    CorruptSecretError,
    DecryptionError,
    InvalidFernetKeyError,
    InvalidSecretTypeError,
    decode_stored_secret,
    decode_stored_secret_scoped,
    encrypt_stored_secret,
)

_FERNET_KEY = Fernet.generate_key().decode()
_ORG_ID = uuid.uuid4()


def _make_session(
    *,
    in_tx: bool = True,
    dialect: str = "sqlite",
    info: dict[str, object] | None = None,
    current_setting: str | None = None,
) -> AsyncMock:
    """Build an AsyncSession mock that supports the scoped-helper contract."""
    session = AsyncMock()
    # ``in_transaction`` is SYNC on the real Session — use a sync mock so the
    # truthiness guard behaves (AsyncMock would return an un-awaited coroutine).
    session.in_transaction = MagicMock(return_value=in_tx)
    session.info = dict(info or {})

    bind = MagicMock()
    bind.dialect.name = dialect

    async def _get_bind() -> MagicMock:
        return bind

    session.get_bind = _get_bind

    async def _exec(_stmt, _params=None):
        result = MagicMock()
        result.scalar.return_value = current_setting
        return result

    session.execute = _exec
    return session


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


# ── Context-bound decode_stored_secret_scoped ──────────────────────────────


async def test_decode_stored_secret_scoped_requires_active_transaction() -> None:
    """Decrypt outside a transaction must fail loudly (no silent unscoped read)."""
    session = _make_session(in_tx=False)
    with pytest.raises(RuntimeError, match="requires an active transaction"):
        await decode_stored_secret_scoped(session, b"anything", _FERNET_KEY)


async def test_decode_stored_secret_scoped_requires_org_context() -> None:
    """NEGATIVE: an unscoped session (no org context) must fail CI.

    This is the FAR-519/522 regression guard — decrypting without an RLS org
    context raises instead of returning plaintext.
    """
    session = _make_session(in_tx=True, info={}, current_setting="")
    with pytest.raises(RuntimeError, match="RLS organisation context not set"):
        await decode_stored_secret_scoped(session, b"anything", _FERNET_KEY)


async def test_decode_stored_secret_scoped_decrypts_within_org_scope() -> None:
    """POSITIVE: a scoped session (org bound) decrypts the stored secret."""
    plaintext = "org-bound-secret"
    encrypted = Fernet(_FERNET_KEY.encode()).encrypt(plaintext.encode())
    session = _make_session(dialect="sqlite", info={"org_id": str(_ORG_ID)})

    result = await decode_stored_secret_scoped(session, encrypted, _FERNET_KEY)

    assert result == plaintext
    # The helpers must have (re-)applied the RLS org on the session.
    assert session.info["org_id"] is not None


async def test_decode_stored_secret_scoped_resolves_org_from_postgres_context() -> None:
    """Scoping reads the org from current_setting on a Postgres session."""
    plaintext = "pg-bound-secret"
    encrypted = Fernet(_FERNET_KEY.encode()).encrypt(plaintext.encode())
    session = _make_session(dialect="postgresql", info={}, current_setting=str(_ORG_ID))

    result = await decode_stored_secret_scoped(session, encrypted, _FERNET_KEY)

    assert result == plaintext


async def test_decode_stored_secret_scoped_explicit_org_param_ignores_session_gap() -> None:
    """A caller that supplies the org explicitly may decrypt even if the session
    has no RLS context yet (the helper applies set_rls_org for it)."""
    plaintext = "explicit-org"
    encrypted = Fernet(_FERNET_KEY.encode()).encrypt(plaintext.encode())
    session = _make_session(dialect="postgresql", info={}, current_setting="")

    result = await decode_stored_secret_scoped(session, encrypted, _FERNET_KEY, org_id=_ORG_ID)

    assert result == plaintext
