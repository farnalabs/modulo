"""FernetSecretsBackend — encrypt/decrypt secrets with Fernet, store in DB.

Default implementation that preserves the current behaviour: secrets are
encrypted with ``cryptography.fernet.Fernet`` and stored in the ``secrets``
table.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, select, text

from modulo.core.secrets_backend import SecretsBackend, validate_key
from modulo.db.models.secret import Secret

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


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
        self._fernet = Fernet(fernet_key.encode())
        self._fernet_old = Fernet(old_key.encode()) if old_key else None
        self._session = session
        self._org_id: uuid.UUID | None = None

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
            raise RuntimeError("FernetSecretsBackend: no DB session set")

        org_id = await self._read_org_id_from_session()
        result = await self._session.execute(
            select(Secret).where(Secret.key == key, Secret.organisation_id == org_id).limit(1)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise KeyError(key)

        try:
            plaintext = self._fernet.decrypt(row.encrypted_value)
        except InvalidToken:
            if self._fernet_old is not None:
                try:
                    plaintext = self._fernet_old.decrypt(row.encrypted_value)
                except InvalidToken as exc:
                    raise ValueError(f"Failed to decrypt secret: {key}") from exc
            else:
                raise ValueError(f"Failed to decrypt secret: {key}") from None

        return plaintext.decode()

    async def _read_org_id_from_session(self) -> uuid.UUID:
        """Read ``app.organisation_id`` from the current session configuration.

        This value must be set via ``set_rls_org()`` before calling any
        operation that creates rows with an ``organisation_id`` FK.

        The org ID is cached after the first read to avoid redundant queries.
        """
        if self._org_id is not None:
            return self._org_id
        if self._session is None:
            raise RuntimeError("FernetSecretsBackend: no DB session set")
        result = await self._session.execute(text("SELECT current_setting('app.organisation_id', true)"))
        org_id_str: str | None = result.scalar()
        if not org_id_str:
            raise RuntimeError(
                "FernetSecretsBackend: RLS organisation context not set. "
                "Call set_rls_org(session, org_id) before set_secret."
            )
        self._org_id = uuid.UUID(org_id_str)
        return self._org_id

    async def set_secret(self, key: str, value: str) -> None:
        """Encrypt *value* and upsert it under *key*."""
        key = validate_key(key)
        if self._session is None:
            raise RuntimeError("FernetSecretsBackend: no DB session set")

        encrypted = self._fernet.encrypt(value.encode())
        org_id = await self._read_org_id_from_session()

        stmt = select(Secret).where(Secret.key == key, Secret.organisation_id == org_id).limit(1).with_for_update()
        result = await self._session.execute(stmt)
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
        await self._session.flush()

    async def delete_secret(self, key: str) -> None:
        """Remove the record for *key* from the secrets table."""
        key = validate_key(key)
        if self._session is None:
            raise RuntimeError("FernetSecretsBackend: no DB session set")

        org_id = await self._read_org_id_from_session()
        stmt = delete(Secret).where(Secret.key == key, Secret.organisation_id == org_id)
        await self._session.execute(stmt)
        await self._session.flush()
