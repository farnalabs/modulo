"""FernetSecretsBackend — encrypt/decrypt secrets with Fernet, store in DB.

Default implementation that preserves the current behaviour: secrets are
encrypted with ``cryptography.fernet.Fernet`` and stored in the ``secrets``
table.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from modulo.core.secrets_backend import SecretsBackend
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
        """Set or replace the DB session used for persistence."""
        self._session = session

    async def get_secret(self, key: str) -> str:
        """Retrieve and decrypt a secret.

        Raises:
            KeyError: If *key* is not found in the secrets table.
            ValueError: If the stored value cannot be decrypted (corrupted data
                or wrong Fernet key).
        """
        if self._session is None:
            raise RuntimeError("FernetSecretsBackend: no DB session set")

        result = await self._session.execute(select(Secret).where(Secret.key == key).limit(1))
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
                    raise ValueError("Failed to decrypt secret") from exc
            else:
                raise ValueError("Failed to decrypt secret") from None

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
        if self._session is None:
            raise RuntimeError("FernetSecretsBackend: no DB session set")

        encrypted = self._fernet.encrypt(value.encode())
        org_id = await self._read_org_id_from_session()

        stmt = pg_insert(Secret).values(
            id=uuid.uuid4(),
            organisation_id=org_id,
            key=key,
            encrypted_value=encrypted,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["organisation_id", "key"],
            set_={"encrypted_value": encrypted},
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def delete_secret(self, key: str) -> None:
        """Remove the record for *key* from the secrets table."""
        if self._session is None:
            raise RuntimeError("FernetSecretsBackend: no DB session set")

        stmt = text("DELETE FROM secrets WHERE key = :key")
        await self._session.execute(stmt, {"key": key})
        await self._session.flush()
