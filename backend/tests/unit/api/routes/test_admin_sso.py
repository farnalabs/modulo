"""Regression tests: /api/v1/admin/sso must run CRUD inside session.begin().

The DI session factory uses ``autobegin=False``, so the SSO provider CRUD
functions must run inside an explicit transaction. These tests exercise the
REAL ``list_providers`` CRUD against an autobegin-aware fake session.
"""

import uuid
from unittest.mock import MagicMock

from modulo.api.routes.admin_sso import get_providers
from modulo.auth.jwt import TenantPrincipal

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ADMIN_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _admin_principal() -> TenantPrincipal:
    return TenantPrincipal(
        username="admin@test",
        organisation_id=_ORG_ID,
        account_id=_ADMIN_ID,
        org_role="admin",
    )


class _AutobeginAwareSession:
    """Fake session whose execute() requires an explicit begin() first."""

    def __init__(self, *, scalars_all: list[object] | None = None) -> None:
        self._in_tx = False
        self._scalars_all = scalars_all if scalars_all is not None else []

    def begin(self) -> "_BeginCtx":
        return _BeginCtx(self)

    async def execute(self, stmt: object, *args: object) -> MagicMock:
        assert self._in_tx, "execute() ran outside session.begin() (autobegin=False)"
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        result.scalars.return_value.all.return_value = self._scalars_all
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


async def test_get_providers_runs_query_inside_begin() -> None:
    fake = _AutobeginAwareSession(scalars_all=[])
    result = await get_providers(_=None, current_user=_admin_principal(), session=fake)
    assert result == []
