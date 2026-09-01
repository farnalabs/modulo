"""SecretsBackend ABC and factory — pluggable secret storage.

Usage:
    backend = create_secrets_backend(fernet_key=settings.fernet_key, session=db_session)
    secret = await backend.get_secret("my-key")
    await backend.set_secret("my-key", "my-value")
    await backend.delete_secret("my-key")
"""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT: float = 30.0


async def run_sync(
    func: Callable[..., Any],
    *args: Any,
    timeout_seconds: float = DEFAULT_TIMEOUT,
    **kwargs: Any,
) -> Any:
    """Run a synchronous callable in a thread pool with a timeout."""
    return await asyncio.wait_for(
        asyncio.to_thread(func, *args, **kwargs),
        timeout=timeout_seconds,
    )


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


def validate_key(key: str) -> str:
    """Validate and normalise a secret key. Raises ValueError if empty or not a string."""
    if not isinstance(key, str):
        raise ValueError("Secret key must be a non-empty string")
    stripped = key.strip()
    if not stripped:
        raise ValueError("Secret key must be a non-empty string")
    return stripped


def _check_external_secrets_licensed() -> bool:
    """Return True if the current plan permits external secrets backends.

    Uses FeatureFlagRegistry to check the ``external_secrets`` flag rather
    than an ad-hoc license check, so gating stays consistent with the
    centralized flag definition and tier catalog.
    """
    from modulo.core.feature_flags import FeatureFlagRegistry
    from modulo.core.license import get_license, parse_and_verify

    tier: str = "community"
    has_license: bool = False

    lic = get_license()
    if lic is not None:
        tier = lic.tier
        has_license = True
    else:
        raw_key = os.environ.get("MODULO_LICENSE_KEY", "")
        if raw_key:
            validation = parse_and_verify(raw_key)
            if validation.valid and validation.license_data is not None:
                tier = validation.license_data.tier
                has_license = True

    registry = FeatureFlagRegistry(current_tier=tier, has_license_key=has_license)
    flag = registry.get_flag("external_secrets")
    return flag is not None and flag.currently_active


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

    External secrets backends (``vault``, ``aws``) require a valid license key.
    Without one, the factory falls back to ``"fernet"`` and logs a warning.

    Args:
        fernet_key: Fernet encryption key (required only by FernetSecretsBackend).
        old_fernet_key: Optional previous Fernet key for no-downtime rotation.
            Ignored by ``vault`` and ``aws`` backends.
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
        case "vault" | "aws":
            if not _check_external_secrets_licensed():
                logger.warning(
                    "External secrets backend %r requires a valid license key. Falling back to 'fernet' backend.",
                    name,
                )
                from modulo.core.secrets_backend.fernet import FernetSecretsBackend

                if fernet_key is None:
                    raise ValueError("fernet_key is required when backend_name is 'fernet'")
                return FernetSecretsBackend(fernet_key=fernet_key, session=session, old_key=old_fernet_key)

            if name == "vault":
                from modulo.core.secrets_backend.vault import VaultSecretsBackend

                return VaultSecretsBackend()
            from modulo.core.secrets_backend.aws import AWSSecretsManagerBackend

            return AWSSecretsManagerBackend()
        case _:
            msg = f"Unknown MODULO_SECRETS_BACKEND: {name!r}. Must be one of: 'fernet', 'vault', 'aws'."
            raise ValueError(msg)
