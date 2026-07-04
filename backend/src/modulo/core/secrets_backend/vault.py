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

from modulo.core.secrets_backend import SecretsBackend

_TIMEOUT: float = 30.0

_MODULE_AVAILABLE: bool = True
_hvac: Any = None

try:
    import hvac  # type: ignore[import-untyped]

    _hvac = hvac
except ImportError:
    _MODULE_AVAILABLE = False


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

        self._addr: str = os.environ.get("VAULT_ADDR", "")
        if not self._addr:
            raise ValueError("VaultSecretsBackend: VAULT_ADDR is not set")

        self._token: str | None = os.environ.get("VAULT_TOKEN") or None
        self._role_id: str | None = os.environ.get("VAULT_ROLE_ID") or None
        self._secret_id: str | None = os.environ.get("VAULT_SECRET_ID") or None
        self._mount_point: str = os.environ.get("VAULT_MOUNT_POINT", "secret")
        self._path_prefix: str = os.environ.get("VAULT_PATH_PREFIX", "modulo/secrets")

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

            client = _hvac.Client(url=self._addr, timeout=_TIMEOUT)

            if self._token:
                client.token = self._token
            elif self._role_id and self._secret_id:
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(
                            client.auth.approle.login,
                            role_id=self._role_id,
                            secret_id=self._secret_id,
                        ),
                        timeout=_TIMEOUT,
                    )
                except TimeoutError:
                    raise RuntimeError("VaultSecretsBackend: timeout authenticating with AppRole") from None
                except _hvac.exceptions.Unauthorized as exc:
                    raise PermissionError("VaultSecretsBackend: AppRole authentication failed") from exc
            else:
                raise RuntimeError("VaultSecretsBackend: neither VAULT_TOKEN nor VAULT_ROLE_ID+VAULT_SECRET_ID are set")

            self._client = client
            return self._client

    def _secret_path(self, key: str) -> str:
        sanitised = key.replace("\\", "/").lstrip("/")
        if ".." in sanitised.split("/"):
            raise ValueError("VaultSecretsBackend: secret key must not contain '..' segments")
        return f"{self._path_prefix}/{sanitised}"

    async def get_secret(self, key: str) -> str:
        client = await self._ensure_client()
        path = self._secret_path(key)

        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    client.secrets.kv.v2.read_secret_version,
                    path=path,
                    mount_point=self._mount_point,
                ),
                timeout=_TIMEOUT,
            )
        except _hvac.exceptions.InvalidPath:
            raise KeyError(key) from None
        except _hvac.exceptions.Forbidden as exc:
            raise PermissionError("VaultSecretsBackend: permission denied reading secret") from exc
        except TimeoutError:
            raise RuntimeError("VaultSecretsBackend: timeout reading secret") from None
        except Exception:
            raise RuntimeError("VaultSecretsBackend: unexpected error reading secret") from None

        data: dict[str, Any] = response.get("data", {})
        secret_data: dict[str, Any] = data.get("data", {})

        value = secret_data.get("value")
        if value is None:
            raise KeyError(key)

        return str(value)

    async def set_secret(self, key: str, value: str) -> None:
        client = await self._ensure_client()
        path = self._secret_path(key)

        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    client.secrets.kv.v2.create_or_update_secret,
                    path=path,
                    secret={"value": value},
                    mount_point=self._mount_point,
                ),
                timeout=_TIMEOUT,
            )
        except _hvac.exceptions.Forbidden as exc:
            raise PermissionError("VaultSecretsBackend: permission denied writing secret") from exc
        except TimeoutError:
            raise RuntimeError("VaultSecretsBackend: timeout writing secret") from None
        except Exception:
            raise RuntimeError("VaultSecretsBackend: unexpected error writing secret") from None

    async def delete_secret(self, key: str) -> None:
        client = await self._ensure_client()
        path = self._secret_path(key)

        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    client.secrets.kv.v2.delete_metadata_and_all_versions,
                    path=path,
                    mount_point=self._mount_point,
                ),
                timeout=_TIMEOUT,
            )
        except _hvac.exceptions.InvalidPath:
            pass
        except _hvac.exceptions.Forbidden as exc:
            raise PermissionError("VaultSecretsBackend: permission denied deleting secret") from exc
        except TimeoutError:
            raise RuntimeError("VaultSecretsBackend: timeout deleting secret") from None
        except Exception:
            raise RuntimeError("VaultSecretsBackend: unexpected error deleting secret") from None
