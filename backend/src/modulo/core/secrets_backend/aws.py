"""AWSSecretsManagerBackend — AWS Secrets Manager backend.

Requires the *boto3* package (optional dependency). If *boto3* is not installed
all operations raise ``RuntimeError`` with a clear installation hint.

Configured via environment variables:

- ``AWS_ACCESS_KEY_ID`` — AWS access key.
- ``AWS_SECRET_ACCESS_KEY`` — AWS secret key.
- ``AWS_REGION`` — AWS region (default ``"us-east-1"``).
- ``AWS_PROFILE`` — AWS profile name (alternative to static credentials).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from modulo.core.secrets_backend import SecretsBackend, logger, run_sync, validate_key

_TIMEOUT: float = 30.0
_DEFAULT_REGION: str = "us-east-1"
_RECOVERY_WINDOW_DAYS: int = 7
_FORCE_DELETE_WITHOUT_RECOVERY: bool = False
_SECRET_DESCRIPTION: str = "Modulo secret"

_MODULE_AVAILABLE: bool = True
_boto3: Any = None

try:
    import boto3  # type: ignore[import-untyped]

    _boto3 = boto3
except ImportError:
    _MODULE_AVAILABLE = False


class AWSSecretsManagerBackend(SecretsBackend):
    """Read/write secrets from AWS Secrets Manager.

    The constructor reads configuration from environment variables (see module
    docstring). No arguments are required.

    Raises:
        RuntimeError: If *boto3* is not installed.

    """

    def __init__(self) -> None:
        if not _MODULE_AVAILABLE:
            raise RuntimeError(
                "The 'boto3' package is required for AWSSecretsManagerBackend. Install it with: pip install boto3"
            )

        self._region: str = (os.environ.get("AWS_REGION") or _DEFAULT_REGION).strip()
        self._profile: str | None = (os.environ.get("AWS_PROFILE") or "").strip() or None
        self._access_key: str | None = (os.environ.get("AWS_ACCESS_KEY_ID") or "").strip() or None
        self._secret_key: str | None = (os.environ.get("AWS_SECRET_ACCESS_KEY") or "").strip() or None

        self._client: Any = None
        self._client_lock: asyncio.Lock = asyncio.Lock()

    async def _ensure_client(self) -> Any:
        if not _MODULE_AVAILABLE:
            raise RuntimeError(
                "The 'boto3' package is required for AWSSecretsManagerBackend. Install it with: pip install boto3"
            )
        if self._client is not None:
            return self._client

        async with self._client_lock:
            if self._client is not None:
                return self._client

            try:
                session_kwargs: dict[str, Any] = {"region_name": self._region}

                if self._profile:
                    session_kwargs["profile_name"] = self._profile
                elif self._access_key and self._secret_key:
                    session_kwargs["aws_access_key_id"] = self._access_key
                    session_kwargs["aws_secret_access_key"] = self._secret_key

                session = _boto3.Session(**session_kwargs)
                self._client = session.client("secretsmanager")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("AWSSecretsManagerBackend: failed to create client")
                raise
            return self._client

    async def get_secret(self, key: str) -> str:
        key = validate_key(key)
        client = await self._ensure_client()

        try:
            response = await run_sync(
                client.get_secret_value,
                SecretId=key,
                timeout_seconds=_TIMEOUT,
            )
        except client.exceptions.ResourceNotFoundException:
            raise KeyError(key) from None
        except client.exceptions.AccessDeniedException as exc:
            logger.warning("AWSSecretsManagerBackend: access denied reading secret %s", key)
            raise PermissionError("AWSSecretsManagerBackend: access denied reading secret") from exc
        except TimeoutError:
            logger.exception("AWSSecretsManagerBackend: timeout reading secret %s", key)
            raise RuntimeError("AWSSecretsManagerBackend: timeout reading secret") from None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("AWSSecretsManagerBackend: unexpected error reading secret %s: %s", key, exc)
            raise RuntimeError("AWSSecretsManagerBackend: unexpected error reading secret") from exc

        secret_string = response.get("SecretString")
        if isinstance(secret_string, str):
            return secret_string

        secret_binary = response.get("SecretBinary")
        if isinstance(secret_binary, bytes):
            return secret_binary.decode()

        raise KeyError(key)

    async def set_secret(self, key: str, value: str) -> None:
        key = validate_key(key)
        client = await self._ensure_client()

        try:
            await run_sync(
                client.create_secret,
                Name=key,
                SecretString=value,
                Description=_SECRET_DESCRIPTION,
                timeout_seconds=_TIMEOUT,
            )
        except client.exceptions.ResourceExistsException:
            try:
                await run_sync(
                    client.update_secret,
                    SecretId=key,
                    SecretString=value,
                    timeout_seconds=_TIMEOUT,
                )
            except client.exceptions.ResourceNotFoundException:
                # Secret was deleted between create_secret raising
                # ResourceExistsException and update_secret — retry the create
                # once to close the TOCTOU window.
                logger.warning("AWSSecretsManagerBackend: secret %s deleted mid-write, retrying create", key)
                await run_sync(
                    client.create_secret,
                    Name=key,
                    SecretString=value,
                    Description=_SECRET_DESCRIPTION,
                    timeout_seconds=_TIMEOUT,
                )
            except TimeoutError:
                logger.exception("AWSSecretsManagerBackend: timeout writing secret %s", key)
                raise RuntimeError("AWSSecretsManagerBackend: timeout writing secret") from None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("AWSSecretsManagerBackend: unexpected error writing secret %s: %s", key, exc)
                raise RuntimeError("AWSSecretsManagerBackend: unexpected error writing secret") from exc
        except TimeoutError:
            logger.exception("AWSSecretsManagerBackend: timeout writing secret %s", key)
            raise RuntimeError("AWSSecretsManagerBackend: timeout writing secret") from None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("AWSSecretsManagerBackend: unexpected error writing secret %s: %s", key, exc)
            raise RuntimeError("AWSSecretsManagerBackend: unexpected error writing secret") from exc

    async def delete_secret(self, key: str) -> None:
        key = validate_key(key)
        client = await self._ensure_client()

        try:
            await run_sync(
                client.delete_secret,
                SecretId=key,
                RecoveryWindowInDays=_RECOVERY_WINDOW_DAYS,
                ForceDeleteWithoutRecovery=_FORCE_DELETE_WITHOUT_RECOVERY,
                timeout_seconds=_TIMEOUT,
            )
        except client.exceptions.ResourceNotFoundException:
            pass
        except TimeoutError:
            logger.exception("AWSSecretsManagerBackend: timeout deleting secret %s", key)
            raise RuntimeError("AWSSecretsManagerBackend: timeout deleting secret") from None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("AWSSecretsManagerBackend: unexpected error deleting secret %s: %s", key, exc)
            raise RuntimeError("AWSSecretsManagerBackend: unexpected error deleting secret") from exc
