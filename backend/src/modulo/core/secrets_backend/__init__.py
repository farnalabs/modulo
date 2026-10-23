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

from modulo.core.secrets_backend.references import (
    SECRET_REF_SCHEME,
    CredentialReferenceError,
    is_secret_ref,
    parse_secret_ref,
    resolve_credential,
    resolve_credential_reference,
)

__all__ = [
    "DEFAULT_TIMEOUT",
    "SECRET_REF_SCHEME",
    "CredentialReferenceError",
    "SecretsBackend",
    "create_secrets_backend",
    "is_secret_ref",
    "parse_secret_ref",
    "resolve_credential",
    "resolve_credential_reference",
    "run_sync",
    "validate_key",
]

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

    #: Whether this backend partitions secrets by organisation. The default
    #: ``FernetSecretsBackend`` overrides this to ``True`` — every read/write is
    #: scoped by ``secrets.organisation_id``. Externally-hosted backends (Vault,
    #: AWS Secrets Manager) resolve a GLOBAL key namespace (the same key name is
    #: the same secret for every organisation) and cannot be re-keyed per-org
    #: without breaking already-stored secrets, so they keep the ``False``
    #: default. FAR-1640 uses this flag to fail closed on the credential-
    #: reference path in a multi-org deployment (see ``references``).
    organisation_scoped: bool = False

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
    """Return True if the current plan permits CHANGING the secrets-backend configuration.

    This is a CONFIGURATION-SURFACE entitlement, not a runtime gate. It
    answers "may an operator switch to / edit an external backend?", and must
    never decide which backend the running instance resolves credentials with
    (FAR-2059 / FAR-2060): a licence lapse must not silently downgrade a
    configured ``vault`` / ``aws`` deployment to ``fernet``, because that turns
    the lapse into credential-resolution failures at run time with only a log
    warning to explain it.

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


def _warn_if_external_backend_unconfigured(name: str) -> None:
    """Warn when an external backend is selected but lacks its own configuration.

    Licence state is deliberately NOT consulted. This is the only case in
    which the factory should say anything about an external backend: the
    backend's own missing configuration. The selected backend is still built —
    a genuine misconfiguration fails hard in its constructor rather than
    silently resolving credentials through a different backend.

    Only Vault has a hard "unconfigured" signal (``VAULT_ADDR`` unset — the
    constructor refuses). AWS resolves credentials through its ambient chain
    (instance role, web identity, container credentials, …), so a missing
    static key is not a misconfiguration and must not produce log noise on
    every factory call.
    """
    if name == "vault" and not (os.environ.get("VAULT_ADDR") or "").strip():
        logger.warning(
            "Secrets backend 'vault' is selected but VAULT_ADDR is unset; "
            "VaultSecretsBackend construction will fail rather than silently "
            "falling back to fernet.",
        )


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

    External secrets backends (``vault``, ``aws``) are built whenever they are
    CONFIGURED. Licence state does NOT downgrade them (FAR-2059 / FAR-2060):
    ``external_secrets`` gates the ability to CHANGE the configuration, never
    the runtime resolution path. A lapse must not quietly switch a configured
    Vault/AWS deployment to fernet, because that would turn the lapse into
    credential-resolution failures mid-run.

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
            # Deliberately NO licence check here. The configured backend is the
            # backend the instance resolves credentials with; see the module
            # docstring and ``_check_external_secrets_licensed``.
            _warn_if_external_backend_unconfigured(name)
            if name == "vault":
                from modulo.core.secrets_backend.vault import VaultSecretsBackend

                return VaultSecretsBackend()
            from modulo.core.secrets_backend.aws import AWSSecretsManagerBackend

            return AWSSecretsManagerBackend()
        case _:
            msg = f"Unknown MODULO_SECRETS_BACKEND: {name!r}. Must be one of: 'fernet', 'vault', 'aws'."
            raise ValueError(msg)
