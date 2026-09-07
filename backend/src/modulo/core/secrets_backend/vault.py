"""VaultSecretsBackend — HashiCorp Vault KV v2 backend.

Requires the *hvac* package (optional dependency). If *hvac* is not installed
all operations raise ``RuntimeError`` with a clear installation hint.

Configured via environment variables:

- ``VAULT_ADDR`` — Vault server URL (required).
- ``VAULT_TOKEN`` — Vault token for authentication.
- ``VAULT_ROLE_ID`` + ``VAULT_SECRET_ID`` — alternative AppRole auth.
- ``VAULT_MOUNT_POINT`` — KV v2 mount path (default ``"secret"``).
- ``VAULT_PATH_PREFIX`` — path prefix (default ``"modulo/secrets"``).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from modulo.core.secrets_backend import SecretsBackend, logger, run_sync, validate_key

_TIMEOUT: float = 30.0

_MODULE_AVAILABLE: bool = True
_hvac: Any = None

try:
    import hvac  # type: ignore[import-untyped]

    _hvac = hvac
except ImportError:
    _MODULE_AVAILABLE = False


_VAULT_RATELIMIT_ERROR_CODES: set[int] = {429, 503}


class VaultSecretsBackend(SecretsBackend):
    """Read/write secrets from HashiCorp Vault KV v2 engine.

    The constructor reads configuration from environment variables (see module
    docstring). No arguments are required — everything comes from env vars.

    Raises:
        RuntimeError: If *hvac* is not installed.

    """

    def __init__(self) -> None:
        if not _MODULE_AVAILABLE:
            raise RuntimeError(
                "The 'hvac' package is required for VaultSecretsBackend. Install it with: pip install hvac"
            )

        self._addr: str = (os.environ.get("VAULT_ADDR") or "").strip()
        if not self._addr:
            raise ValueError("VaultSecretsBackend: VAULT_ADDR is not set")

        self._token: str | None = (os.environ.get("VAULT_TOKEN") or "").strip() or None
        self._role_id: str | None = (os.environ.get("VAULT_ROLE_ID") or "").strip() or None
        self._secret_id: str | None = (os.environ.get("VAULT_SECRET_ID") or "").strip() or None
        self._mount_point: str = (os.environ.get("VAULT_MOUNT_POINT") or "secret").strip()
        self._path_prefix: str = (os.environ.get("VAULT_PATH_PREFIX") or "modulo/secrets").strip()

        self._client: Any = None
        self._client_lock: asyncio.Lock = asyncio.Lock()

    async def _ensure_client(self) -> Any:
        """Return a configured hvac client, creating one if needed."""
        if not _MODULE_AVAILABLE:
            raise RuntimeError(
                "The 'hvac' package is required for VaultSecretsBackend. Install it with: pip install hvac"
            )
        if self._client is not None:
            return self._client

        async with self._client_lock:
            if self._client is not None:
                return self._client

            client = _hvac.Client(url=self._addr)

            try:
                if self._token:
                    client.token = self._token
                elif self._role_id and self._secret_id:
                    await run_sync(
                        client.auth.approle.login,
                        role_id=self._role_id,
                        secret_id=self._secret_id,
                        timeout_seconds=_TIMEOUT,
                    )
                else:
                    raise RuntimeError(
                        "VaultSecretsBackend: neither VAULT_TOKEN nor VAULT_ROLE_ID+VAULT_SECRET_ID are set"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("VaultSecretsBackend: failed to authenticate to Vault at %s", self._addr)
                raise RuntimeError("VaultSecretsBackend: failed to authenticate to Vault") from exc

            self._client = client
            return self._client

    def _secret_path(self, key: str) -> str:
        if ".." in key or key.startswith("/"):
            raise ValueError(f"VaultSecretsBackend: invalid secret key: {key!r}")
        prefix = self._path_prefix.rstrip("/")
        return f"{prefix}/{key}"

    async def get_secret(self, key: str) -> str:
        key = validate_key(key)
        client = await self._ensure_client()
        path = self._secret_path(key)

        try:
            response = await run_sync(
                client.secrets.kv.v2.read_secret_version,
                path=path,
                mount_point=self._mount_point,
                timeout_seconds=_TIMEOUT,
            )
        except _hvac.exceptions.InvalidPath:
            raise KeyError(key) from None
        except _hvac.exceptions.Forbidden as exc:
            logger.warning("VaultSecretsBackend: permission denied reading secret %s", key)
            raise PermissionError("VaultSecretsBackend: permission denied reading secret") from exc
        except TimeoutError:
            logger.exception("VaultSecretsBackend: timeout reading secret %s", key)
            raise RuntimeError("VaultSecretsBackend: timeout reading secret") from None
        except _hvac.exceptions.VaultError as exc:
            if getattr(exc, "status_code", None) in _VAULT_RATELIMIT_ERROR_CODES:
                logger.warning("VaultSecretsBackend: rate-limited reading secret %s", key)
                raise RuntimeError("VaultSecretsBackend: rate-limited reading secret") from exc
            logger.exception("VaultSecretsBackend: Vault error reading secret %s: %s", key, exc)
            raise RuntimeError("VaultSecretsBackend: unexpected error reading secret") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("VaultSecretsBackend: unexpected error reading secret %s: %s", key, exc)
            raise RuntimeError("VaultSecretsBackend: unexpected error reading secret") from exc

        data: dict[str, Any] = response.get("data", {})
        secret_data: dict[str, Any] = data.get("data", {})

        value = secret_data.get("value")
        if value is None:
            raise KeyError(key)

        return str(value)

    async def set_secret(self, key: str, value: str) -> None:
        key = validate_key(key)
        client = await self._ensure_client()
        path = self._secret_path(key)

        try:
            await run_sync(
                client.secrets.kv.v2.create_or_update_secret,
                path=path,
                secret={"value": value},
                mount_point=self._mount_point,
                timeout_seconds=_TIMEOUT,
            )
        except TimeoutError:
            logger.exception("VaultSecretsBackend: timeout writing secret %s", key)
            raise RuntimeError("VaultSecretsBackend: timeout writing secret") from None
        except _hvac.exceptions.VaultError as exc:
            if getattr(exc, "status_code", None) in _VAULT_RATELIMIT_ERROR_CODES:
                logger.warning("VaultSecretsBackend: rate-limited writing secret %s", key)
                raise RuntimeError("VaultSecretsBackend: rate-limited writing secret") from exc
            logger.exception("VaultSecretsBackend: Vault error writing secret %s: %s", key, exc)
            raise RuntimeError("VaultSecretsBackend: unexpected error writing secret") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("VaultSecretsBackend: unexpected error writing secret %s: %s", key, exc)
            raise RuntimeError("VaultSecretsBackend: unexpected error writing secret") from exc

    async def delete_secret(self, key: str) -> None:
        key = validate_key(key)
        client = await self._ensure_client()
        path = self._secret_path(key)

        try:
            await run_sync(
                client.secrets.kv.v2.delete_metadata_and_all_versions,
                path=path,
                mount_point=self._mount_point,
                timeout_seconds=_TIMEOUT,
            )
        except _hvac.exceptions.InvalidPath:
            pass
        except TimeoutError:
            logger.exception("VaultSecretsBackend: timeout deleting secret %s", key)
            raise RuntimeError("VaultSecretsBackend: timeout deleting secret") from None
        except _hvac.exceptions.VaultError as exc:
            if getattr(exc, "status_code", None) in _VAULT_RATELIMIT_ERROR_CODES:
                logger.warning("VaultSecretsBackend: rate-limited deleting secret %s", key)
                raise RuntimeError("VaultSecretsBackend: rate-limited deleting secret") from exc
            logger.exception("VaultSecretsBackend: Vault error deleting secret %s: %s", key, exc)
            raise RuntimeError("VaultSecretsBackend: unexpected error deleting secret") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("VaultSecretsBackend: unexpected error deleting secret %s: %s", key, exc)
            raise RuntimeError("VaultSecretsBackend: unexpected error deleting secret") from exc
