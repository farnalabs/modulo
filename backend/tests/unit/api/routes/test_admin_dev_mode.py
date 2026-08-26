"""Regression tests: /api/v1/admin/dev-mode must run CRUD inside session.begin().

The GET handler previously swallowed ``InvalidRequestError`` (autobegin=False
session) in a broad ``except Exception`` and silently degraded to the env/default
answer even when a DB override existed. These tests exercise the REAL
``get_config``/``set_config`` CRUD against an autobegin-aware fake session and
assert the DB override actually wins.
"""

import uuid
from unittest.mock import MagicMock

from modulo.api.routes.admin_dev_mode import SetDevModeRequest, get_dev_mode, set_dev_mode
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ADMIN_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _make_settings(dev_mode: bool = False) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_dev_mode=dev_mode,
    )


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="admin@test",
        organisation_id=_ORG_ID,
        account_id=_ADMIN_ID,
        org_role="admin",
        is_system_admin=True,
    )


class _AutobeginAwareSession:
    """Fake session whose execute() requires an explicit begin() first."""

    def __init__(self, *, entry_value: object = None) -> None:
        self._in_tx = False
        self._entry_value = entry_value

    def begin(self) -> "_BeginCtx":
        return _BeginCtx(self)

    async def execute(self, stmt: object, *args: object) -> MagicMock:
        assert self._in_tx, "execute() ran outside session.begin() (autobegin=False)"
        result = MagicMock()
        if self._entry_value is not None:
            entry = MagicMock()
            entry.value = self._entry_value
            result.scalar_one_or_none.return_value = entry
        else:
            result.scalar_one_or_none.return_value = None
        result.scalars.return_value.all.return_value = []
        return result

    def add(self, entity: object) -> None:
        pass

    async def flush(self) -> None:
        pass


class _BeginCtx:
    """Async context manager returned by ``_AutobeginAwareSession.begin()``."""

    def __init__(self, session: _AutobeginAwareSession) -> None:
        self._session = session

    async def __aenter__(self) -> None:
        self._session._in_tx = True

    async def __aexit__(self, *_exc: object) -> bool:
        self._session._in_tx = False
        return False


async def test_get_dev_mode_uses_db_override_inside_begin() -> None:
    """A DB override must win — never silently degrade to default (autobegin=False)."""
    fake = _AutobeginAwareSession(entry_value=True)
    result = await get_dev_mode(settings=_make_settings(dev_mode=False), session=fake, _=_principal())
    assert result == {"enabled": True, "source": "db"}


async def test_set_dev_mode_runs_write_inside_begin() -> None:
    fake = _AutobeginAwareSession()
    result = await set_dev_mode(
        req=SetDevModeRequest(enabled=True),
        _settings=_make_settings(),
        session=fake,
        _=_principal(),
    )
    assert result == {"enabled": True, "source": "db"}
