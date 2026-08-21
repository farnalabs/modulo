"""Shared test helpers for auth unit tests."""

import base64
import json
import uuid
from typing import Self
from unittest.mock import AsyncMock, MagicMock

from modulo.auth.sso import sign_state
from modulo.settings import Settings

_VALID_32 = "a" * 32


def make_test_settings(**overrides: str | bool) -> Settings:
    base: dict[str, str | bool] = {
        "database_url": "postgresql+asyncpg://localhost/test",
        "secret_key": _VALID_32,
        "fernet_key": _VALID_32,
        "modulo_license_key": "test-license-key",
        "modulo_oidc_providers": json.dumps(
            [
                {
                    "provider_id": "google",
                    "client_id": "google-client-id",
                    "client_secret": "google-client-secret",
                    "discovery_url": "https://accounts.google.com/.well-known/openid-configuration",
                },
                {
                    "provider_id": "github",
                    "client_id": "github-client-id",
                    "client_secret": "github-client-secret",
                    "discovery_url": "https://token.actions.githubusercontent.com/.well-known/openid-configuration",
                },
            ]
        ),
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _make_id_token(email: str, name: str, sub: str = "abc123") -> str:
    header_b64 = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
    payload_b64 = (
        base64.urlsafe_b64encode(json.dumps({"email": email, "name": name, "sub": sub}).encode()).rstrip(b"=").decode()
    )
    return f"{header_b64}.{payload_b64}.signature"


def _sign_state(provider_id: str, secret_key: str = _VALID_32) -> str:
    return sign_state(f"{provider_id}:{uuid.uuid4().hex}", secret_key)


def _make_session_mock() -> AsyncMock:
    class _AsyncSessionContextManager:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(
            self,
            exc_type: object = None,
            exc_val: object = None,
            exc_tb: object = None,
        ) -> bool:
            return False

    cm = _AsyncSessionContextManager()
    session = AsyncMock()
    session.begin = MagicMock(return_value=cm)
    return session
