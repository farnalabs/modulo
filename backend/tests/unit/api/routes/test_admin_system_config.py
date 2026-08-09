"""Regression tests: /api/v1/system-admin/config must run CRUD inside session.begin().

The DI session factory uses ``autobegin=False``, so ``list_config``/``set_config``/
``delete_config`` must run inside an explicit transaction. These tests exercise the
REAL CRUD functions against an autobegin-aware fake session.
"""

import uuid
from unittest.mock import MagicMock

from modulo.api.routes.admin_system_config import (
    SetConfigRequest,
    admin_delete_config,
    admin_list_config,
    admin_set_config,
)
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.models.system_config import SystemConfig

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ADMIN_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="sysadmin@test",
        organisation_id=_ORG_ID,
        account_id=_ADMIN_ID,
        org_role="admin",
        is_system_admin=True,
    )


class _AutobeginAwareSession:
    """Fake session whose execute() requires an explicit begin() first."""

    def __init__(self, *, scalar_one_or_none: object = None, scalars_all: list[object] | None = None) -> None:
        self._in_tx = False
        self._scalar_one_or_none = scalar_one_or_none
        self._scalars_all = scalars_all if scalars_all is not None else []

    def begin(self) -> "_BeginCtx":
        return _BeginCtx(self)

    async def execute(self, stmt: object, *args: object) -> MagicMock:
        assert self._in_tx, "execute() ran outside session.begin() (autobegin=False)"
        result = MagicMock()
        result.scalar_one_or_none.return_value = self._scalar_one_or_none
        result.scalars.return_value.all.return_value = self._scalars_all
        return result

    def add(self, entity: object) -> None:
        pass

    async def delete(self, entity: object) -> None:
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


async def test_admin_list_config_runs_query_inside_begin() -> None:
    fake = _AutobeginAwareSession(scalars_all=[])
    result = await admin_list_config(current_user=_principal(), session=fake)
    assert result == []


async def test_admin_set_config_runs_write_inside_begin() -> None:
    fake = _AutobeginAwareSession()
    result = await admin_set_config(
        key="test_key",
        req=SetConfigRequest(value="test_value"),
        current_user=_principal(),
        session=fake,
    )
    assert result.key == "test_key"
    assert result.value == "test_value"


async def test_admin_delete_config_runs_write_inside_begin() -> None:
    existing = SystemConfig(key="del_key", value="val")
    fake = _AutobeginAwareSession(scalar_one_or_none=existing)
    result = await admin_delete_config(key="del_key", current_user=_principal(), session=fake)
    assert result is None
