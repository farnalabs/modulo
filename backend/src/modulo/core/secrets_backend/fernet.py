"""FernetSecretsBackend — encrypt/decrypt secrets with Fernet, store in DB.

Default implementation that preserves the current behaviour: secrets are
encrypted with ``cryptography.fernet.Fernet`` and stored in the ``secrets``
table.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError

from modulo.core.secrets_backend import SecretsBackend, validate_key
from modulo.db.models.secret import Secret
from modulo.db.rls import _TENANT_KEY

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = logging.getLogger(__name__)
_DB_TIMEOUT: float = 10.0

# Raised by every read/write path when no session is available (S1192).
_ERR_NO_DB_SESSION = "FernetSecretsBackend: no DB session set"


class FernetSecretsBackend(SecretsBackend):
    """Encrypt secrets at rest with Fernet, persisted in the *secrets* table.

    Requires a database session for persistence. If no session is provided at
    construction time you must call ``set_session()`` before any read/write
    operations, or pass one to each method call.

    Args:
        fernet_key: Base64-encoded 32-byte Fernet key.
        session: Optional SQLAlchemy async session for DB operations.
        old_key: Optional previous Fernet key for no-downtime rotation.

    """

    def __init__(
        self,
        fernet_key: str,
        session: AsyncSession | None = None,
        old_key: str | None = None,
    ) -> None:
        self._fernet = self._build_fernet(fernet_key, "fernet_key")
        self._fernet_old = self._build_fernet(old_key, "old_key") if old_key else None
        self._session = session
        self._org_id: uuid.UUID | None = None

    @staticmethod
    def _build_fernet(fernet_key: str, field: str) -> Fernet:
        """Build a Fernet instance, raising a clear config error for invalid keys.

        ``cryptography.fernet.Fernet`` raises ``binascii.Error`` or
        ``ValueError`` for keys that are not valid base64-encoded 32-byte
        values; surface those as a friendly ``ValueError`` instead.
        """
        try:
            return Fernet(fernet_key.encode())
        except ValueError as exc:
            raise ValueError(
                f"FernetSecretsBackend: invalid {field}: must be a base64-encoded 32-byte Fernet key"
            ) from exc

    def set_session(self, session: AsyncSession) -> None:
        """Set or replace the DB session used for persistence.

        Resets the cached organisation ID so it will be re-read from
        the new session on the next operation.
        """
        self._session = session
        self._org_id = None

    async def get_secret(self, key: str) -> str:
        """Retrieve and decrypt a secret.

        Raises:
            KeyError: If *key* is not found in the secrets table.
            ValueError: If the stored value cannot be decrypted (corrupted data
                or wrong Fernet key).

        """
        key = validate_key(key)
        if self._session is None:
            raise RuntimeError(_ERR_NO_DB_SESSION)

        org_id = await self._read_org_id_from_session()
        result = await asyncio.wait_for(
            self._session.execute(select(Secret).where(Secret.key == key, Secret.organisation_id == org_id).limit(1)),
            timeout=_DB_TIMEOUT,
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise KeyError(key)

        encrypted = row.encrypted_value
        if encrypted is None:
            logger.error("FernetSecretsBackend: stored secret %s has no encrypted value", key)
            raise ValueError(f"Failed to decrypt secret: {key} (corrupted data)")

        plaintext = self._decrypt_value(encrypted, key)
        return plaintext.decode()

    def _decrypt_value(self, encrypted: bytes, key: str) -> bytes:
        """Try decryption with current key, then fallback key."""
        for fernet in [self._fernet, self._fernet_old]:
            if fernet is None:
                continue
            try:
                return fernet.decrypt(encrypted)
            except InvalidToken:
                continue
        logger.error("FernetSecretsBackend: failed to decrypt secret %s with any key", key)
        raise ValueError(f"Failed to decrypt secret: {key}")

    async def _read_org_id_from_session(self) -> uuid.UUID:
        """Read ``app.organisation_id`` from the current session configuration.

        This value must be set via ``set_rls_org()`` before calling any
        operation that creates rows with an ``organisation_id`` FK.

        The org ID is cached after the first read to avoid redundant queries.

        On non-Postgres backends (SQLite, MariaDB), ``current_setting()``
        is not available — falls back to ``session.info["org_id"]``.
        """
        if self._org_id is not None:
            return self._org_id
        if self._session is None:
            raise RuntimeError(_ERR_NO_DB_SESSION)

        org_id_str: str | None = None

        try:
            result = await asyncio.wait_for(
                self._session.execute(text("SELECT current_setting('app.organisation_id', true)")),
                timeout=_DB_TIMEOUT,
            )
            org_id_str = result.scalar()
        except (OperationalError, ProgrammingError):
            logger.debug(
                "FernetSecretsBackend: current_setting not available, "
                "falling back to session.info (non-Postgres backend)"
            )

        if not org_id_str:
            info = getattr(self._session, "info", {})
            if isinstance(info, dict):
                org_id_str = info.get(_TENANT_KEY)

        if not org_id_str:
            raise RuntimeError(
                "FernetSecretsBackend: RLS organisation context not set. "
                "Call set_rls_org(session, org_id) before set_secret."
            )
        try:
            self._org_id = uuid.UUID(str(org_id_str))
        except (ValueError, AttributeError) as exc:
            raise RuntimeError(f"FernetSecretsBackend: invalid organisation_id format: {org_id_str!r}") from exc
        return self._org_id

    async def set_secret(self, key: str, value: str) -> None:
        """Encrypt *value* and upsert it under *key*."""
        key = validate_key(key)
        if self._session is None:
            raise RuntimeError(_ERR_NO_DB_SESSION)

        encrypted = self._fernet.encrypt(value.encode())
        org_id = await self._read_org_id_from_session()

        for attempt in range(2):
            try:
                async with self._session.begin_nested():
                    stmt = (
                        select(Secret)
                        .where(Secret.key == key, Secret.organisation_id == org_id)
                        .limit(1)
                        .with_for_update()
                    )
                    result = await asyncio.wait_for(self._session.execute(stmt), timeout=_DB_TIMEOUT)
                    existing = result.scalar_one_or_none()

                    if existing is not None:
                        existing.encrypted_value = encrypted
                    else:
                        self._session.add(
                            Secret(
                                id=uuid.uuid4(),
                                organisation_id=org_id,
                                key=key,
                                encrypted_value=encrypted,
                            )
                        )
                    await asyncio.wait_for(self._session.flush(), timeout=_DB_TIMEOUT)
                break
            except IntegrityError:
                if attempt == 0:
                    logger.warning("FernetSecretsBackend: TOCTOU retry on set_secret for key %s", key)
                    continue
                logger.exception("FernetSecretsBackend: TOCTOU retry exhausted for key %s", key)
                raise

    async def delete_secret(self, key: str) -> None:
        """Remove the record for *key* from the secrets table."""
        key = validate_key(key)
        if self._session is None:
            raise RuntimeError(_ERR_NO_DB_SESSION)

        org_id = await self._read_org_id_from_session()
        stmt = delete(Secret).where(Secret.key == key, Secret.organisation_id == org_id)
        try:
            await asyncio.wait_for(self._session.execute(stmt), timeout=_DB_TIMEOUT)
            await asyncio.wait_for(self._session.flush(), timeout=_DB_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("FernetSecretsBackend: error deleting secret %s", key)
            raise
