"""SecretsBackend ABC and factory — pluggable secret storage.

Usage:
    backend = create_secrets_backend(fernet_key=settings.fernet_key, session=db_session)
    secret = await backend.get_secret("my-key")
    await backend.set_secret("my-key", "my-value")
    await backend.delete_secret("my-key")
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class SecretsBackend(ABC):
    """Abstract base for secret storage backends.

    All methods are async-safe. Implementations must not log or leak secret
    values in exception messages, tracebacks, or span attributes.
    """

    @abstractmethod
    async def get_secret(self, key: str) -> str:
        """Retrieve a secret by key. Raises KeyError if not found."""
        ...

    @abstractmethod
    async def set_secret(self, key: str, value: str) -> None:
        """Store or update a secret. Overwrites any existing value for *key*."""
        ...

    @abstractmethod
    async def delete_secret(self, key: str) -> None:
        """Delete a secret by key. No-op if the key does not exist."""
        ...


def create_secrets_backend(
    *,
    fernet_key: str | None = None,
    old_fernet_key: str | None = None,
    session: AsyncSession | None = None,
    backend_name: str | None = None,
) -> SecretsBackend:
    """Factory: return the configured SecretsBackend.

    Reads *backend_name* (default ``MODULO_SECRETS_BACKEND`` env var, fallback
    ``"fernet"``) and constructs the matching implementation.

    Args:
        fernet_key: Fernet encryption key (required only by FernetSecretsBackend).
        session: Optional SQLAlchemy async session (required by FernetSecretsBackend
            when storing secrets in the database).
        backend_name: Override the backend name. If *None* the env var
            ``MODULO_SECRETS_BACKEND`` is read.

    Returns:
        A ready-to-use ``SecretsBackend`` instance.

    Raises:
        ValueError: If *backend_name* is not one of ``"fernet"``, ``"vault"``,
            or ``"aws"``.
    """
    name = (backend_name or os.environ.get("MODULO_SECRETS_BACKEND") or "fernet").lower().strip()

    match name:
        case "fernet":
            from modulo.core.secrets_backend.fernet import FernetSecretsBackend

            if fernet_key is None:
                raise ValueError("fernet_key is required when backend_name is 'fernet'")
            return FernetSecretsBackend(fernet_key=fernet_key, session=session, old_key=old_fernet_key)
        case "vault":
            from modulo.core.secrets_backend.vault import VaultSecretsBackend

            return VaultSecretsBackend()
        case "aws":
            from modulo.core.secrets_backend.aws import AWSSecretsManagerBackend

            return AWSSecretsManagerBackend()
        case _:
            msg = f"Unknown MODULO_SECRETS_BACKEND: {name!r}. Must be one of: 'fernet', 'vault', 'aws'."
            raise ValueError(msg)
