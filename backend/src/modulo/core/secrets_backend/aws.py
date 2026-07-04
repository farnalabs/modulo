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

from modulo.core.secrets_backend import SecretsBackend

_TIMEOUT: float = 30.0
_RETRY_ATTEMPTS: int = 3

_MODULE_AVAILABLE: bool = True
_boto3: Any = None

try:
    import boto3  # type: ignore[import-untyped]
    from botocore.config import Config as BotoCoreConfig  # type: ignore[import-untyped]

    _boto3 = boto3
    _BotoCoreConfig = BotoCoreConfig
except ImportError:
    _MODULE_AVAILABLE = False
    _BotoCoreConfig = None


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

        self._region: str = os.environ.get("AWS_REGION", "us-east-1")
        self._profile: str | None = os.environ.get("AWS_PROFILE") or None
        self._access_key: str | None = os.environ.get("AWS_ACCESS_KEY_ID") or None
        self._secret_key: str | None = os.environ.get("AWS_SECRET_ACCESS_KEY") or None

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

            session_kwargs: dict[str, Any] = {"region_name": self._region}

            if self._profile:
                session_kwargs["profile_name"] = self._profile
            elif self._access_key and self._secret_key:
                session_kwargs["aws_access_key_id"] = self._access_key
                session_kwargs["aws_secret_access_key"] = self._secret_key

            config = _BotoCoreConfig(retries={"max_attempts": _RETRY_ATTEMPTS, "mode": "adaptive"})
            session = await asyncio.to_thread(_boto3.Session, **session_kwargs)
            self._client = await asyncio.to_thread(session.client, "secretsmanager", config=config)
            return self._client

    async def get_secret(self, key: str) -> str:
        client = await self._ensure_client()
        response: Any = None

        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(client.get_secret_value, SecretId=key),
                timeout=_TIMEOUT,
            )
        except client.exceptions.ResourceNotFoundException:
            raise KeyError(key) from None
        except client.exceptions.AccessDeniedException as exc:
            raise PermissionError("AWSSecretsManagerBackend: access denied reading secret") from exc
        except TimeoutError:
            raise RuntimeError("AWSSecretsManagerBackend: timeout reading secret") from None
        except Exception:
            raise RuntimeError("AWSSecretsManagerBackend: unexpected error reading secret") from None

        secret_string = response.get("SecretString")
        if isinstance(secret_string, str):
            return secret_string

        secret_binary = response.get("SecretBinary")
        if isinstance(secret_binary, bytes):
            return secret_binary.decode()

        raise KeyError(key)

    async def set_secret(self, key: str, value: str) -> None:
        client = await self._ensure_client()

        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    client.create_secret,
                    Name=key,
                    SecretString=value,
                    Description="Modulo secret",
                ),
                timeout=_TIMEOUT,
            )
        except client.exceptions.ResourceExistsException:
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        client.update_secret,
                        SecretId=key,
                        SecretString=value,
                    ),
                    timeout=_TIMEOUT,
                )
            except client.exceptions.ResourceNotFoundException as exc:
                raise RuntimeError("AWSSecretsManagerBackend: secret was deleted during update") from exc
            except client.exceptions.AccessDeniedException as exc:
                raise PermissionError("AWSSecretsManagerBackend: access denied writing secret") from exc
            except TimeoutError:
                raise RuntimeError("AWSSecretsManagerBackend: timeout writing secret") from None
            except Exception:
                raise RuntimeError("AWSSecretsManagerBackend: unexpected error writing secret") from None
        except client.exceptions.AccessDeniedException as exc:
            raise PermissionError("AWSSecretsManagerBackend: access denied writing secret") from exc
        except TimeoutError:
            raise RuntimeError("AWSSecretsManagerBackend: timeout writing secret") from None
        except Exception:
            raise RuntimeError("AWSSecretsManagerBackend: unexpected error writing secret") from None

    async def delete_secret(self, key: str) -> None:
        client = await self._ensure_client()

        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    client.delete_secret,
                    SecretId=key,
                    RecoveryWindowInDays=7,
                    ForceDeleteWithoutRecovery=False,
                ),
                timeout=_TIMEOUT,
            )
        except client.exceptions.ResourceNotFoundException:
            pass
        except client.exceptions.InvalidRequestException:
            pass
        except client.exceptions.AccessDeniedException as exc:
            raise PermissionError("AWSSecretsManagerBackend: access denied deleting secret") from exc
        except TimeoutError:
            raise RuntimeError("AWSSecretsManagerBackend: timeout deleting secret") from None
        except Exception:
            raise RuntimeError("AWSSecretsManagerBackend: unexpected error deleting secret") from None
