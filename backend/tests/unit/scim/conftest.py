"""Test configuration and shared helpers for SCIM unit tests.

Sets minimal env vars so ``get_settings()`` (called by module-level imports
in ``modulo.api.main``) can construct a ``Settings`` instance, and exposes
shared constants/factories used by both SCIM test modules.
"""

import os
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

if TYPE_CHECKING:
    from modulo.settings import Settings

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "a" * 32)
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("MODULO_ADMIN_PASSWORD", "test")
os.environ.setdefault("MODULO_CSRF_ENABLED", "false")

VALID_32 = "a" * 32
ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
NOW = datetime(2025, 1, 1, tzinfo=UTC)

SCIM_TOKEN = "test-scim-token-12345"


def make_settings(**overrides: object) -> "Settings":
    """Build a minimal Settings for SCIM tests; override any field via kwargs."""
    from modulo.settings import Settings

    base: dict[str, object] = {
        "database_url": "postgresql+asyncpg://localhost/test",
        "secret_key": VALID_32,
        "fernet_key": VALID_32,
        "modulo_admin_password": "testpass",
        "modulo_license_key": "enterprise-license",
        "modulo_scim_token": SCIM_TOKEN,
        "modulo_public_url": "http://localhost:8000",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def make_mock_session() -> MagicMock:
    """Build a MagicMock session whose transaction context is fully mocked."""
    session = MagicMock()
    session.execute = AsyncMock()
    session.flush = AsyncMock()
    session.delete = AsyncMock()
    session.rollback = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session
