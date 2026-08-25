"""Regression tests: /api/v1/admin/monitor-config must run CRUD inside session.begin().

The DI session factory uses ``autobegin=False`` (backend/src/modulo/db/session.py),
so every ``session.execute()`` outside an explicit transaction raises
``InvalidRequestError``. These tests exercise the REAL CRUD functions against an
autobegin-aware fake session that fails loudly when ``execute()`` runs outside
``session.begin()`` — mirroring the ``_AutobeginAwareSession`` pattern in
tests/unit/hitl_manager/test_claim_expiry_job.py.
"""

import uuid
from unittest.mock import MagicMock

from modulo.api.routes.admin_monitor_config import MonitorConfigUpdate, get_monitor_config, set_monitor_config
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
    """Fake session whose execute() requires an explicit begin() first.

    Mirrors the production factory's ``autobegin=False``: executing SQL without
    first entering ``session.begin()`` fails loudly (as SQLAlchemy does with
    ``InvalidRequestError: Autobegin is disabled on this Session``).
    """

    def __init__(
        self,
        *,
        scalar_one_or_none: object = None,
        scalars_all: list[object] | None = None,
        entry_value: object = None,
    ) -> None:
        self._in_tx = False
        self._scalar_one_or_none = scalar_one_or_none
        self._scalars_all = scalars_all if scalars_all is not None else []
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
            result.scalar_one_or_none.return_value = self._scalar_one_or_none
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


async def test_get_monitor_config_runs_query_inside_begin() -> None:
    fake = _AutobeginAwareSession(entry_value={"backends": ["sentry", "builtin"]})
    result = await get_monitor_config(_current_user=_admin_principal(), session=fake)
    assert result["backends"] == ["sentry", "builtin"]


async def test_set_monitor_config_runs_write_inside_begin() -> None:
    fake = _AutobeginAwareSession()
    payload = MonitorConfigUpdate(backends=["builtin"])
    result = await set_monitor_config(req=payload, current_user=_admin_principal(), session=fake)
    assert result["backends"] == ["builtin"]
