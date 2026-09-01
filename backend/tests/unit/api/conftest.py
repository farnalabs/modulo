"""Test configuration for API unit tests.

Sets minimal env vars so ``get_settings()`` (called by middleware at
request time) can construct a ``Settings`` instance.
"""

import os
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.sql import Select

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "a" * 32)
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("MODULO_ADMIN_PASSWORD", "test")
os.environ.setdefault("MODULO_CSRF_ENABLED", "false")

# The org the default trigger row belongs to — matches the principal org used
# by the webhook/slack endpoint test modules.
DEFAULT_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def make_system_session_mock(
    *,
    trigger_found: bool = True,
    trigger_config: dict | None = None,
    trigger_active: bool = True,
    trigger_org_id: uuid.UUID | None = None,
    trigger_deleted: bool = False,
) -> AsyncMock:
    """One parameterized system-session mock for the FAR-523 bootstrap reads.

    The webhook receive/replay and Slack routes resolve the trigger (carrying
    the HMAC/slack signing secret and its organisation) via the SYSTEM session
    BEFORE any app-session RLS org context exists — this factory mocks that
    bootstrap session. Table-aware: ``triggers`` reads return the configured
    trigger row, everything else (``sso_providers`` et al.) falls through to an
    empty row so global lookups miss exactly as they would for an unset table.

    Args:
        trigger_found: whether the bootstrap trigger read matches a row.
        trigger_config: the trigger row's ``config_json`` (None → route sees
            an unconfigured trigger).
        trigger_active: the trigger row's ``active`` flag.
        trigger_org_id: the trigger row's ``organisation_id`` (defaults to
            :data:`DEFAULT_ORG_ID`); point it at a different org to exercise
            the cross-tenant mismatch 404.
        trigger_deleted: simulate a SOFT-DELETED trigger row. Statement-aware:
            the ``triggers`` read only misses (returns None) when the executed
            statement filters ``deleted_at`` — so a test asserting the 404
            FAILS if the soft-delete filter is dropped from the bootstrap
            query.
    """
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    empty_row = MagicMock()
    empty_row.scalar_one_or_none = MagicMock(return_value=None)
    empty_row.scalar_one = AsyncMock(return_value=0)
    empty_row.scalar = AsyncMock(return_value=0)
    empty_scalars = MagicMock()
    empty_scalars.all = MagicMock(return_value=[])
    empty_row.scalars = MagicMock(return_value=empty_scalars)
    empty_row.first = MagicMock(return_value=None)
    empty_row.all = MagicMock(return_value=[])

    trigger_mock = None
    if trigger_found:
        trigger_mock = MagicMock()
        trigger_mock.pipeline_id = uuid.uuid4()
        trigger_mock.active = trigger_active
        trigger_mock.config_json = trigger_config
        # The shared bootstrap helper derives the org from the trigger row
        # (OrgScoped NOT NULL) — the mock must carry a real org id.
        trigger_mock.organisation_id = trigger_org_id if trigger_org_id is not None else DEFAULT_ORG_ID

    def _trigger_read_result(stmt: Select) -> MagicMock:
        found: MagicMock | None = trigger_mock
        if trigger_deleted and "deleted_at" in str(stmt):
            # The row is SOFT-DELETED: a query that filters deleted_at misses
            # it (what the real DB does) — the route must then 404. If the
            # soft-delete filter were dropped from the bootstrap query, this
            # mock would return the row and the test asserting the 404 fails.
            found = None
        row = MagicMock()
        row.scalar_one_or_none = MagicMock(return_value=found)
        return row

    async def _execute(stmt: object, *_a: object, **_kw: object) -> MagicMock:
        if isinstance(stmt, Select):
            froms = stmt.get_final_froms()
            table = getattr(froms[0], "name", "") if froms else ""
            if table == "triggers":
                return _trigger_read_result(stmt)
        return empty_row

    session.execute = AsyncMock(side_effect=_execute)
    session.scalar = AsyncMock(return_value=0)
    session.scalar_one = AsyncMock(return_value=0)
    return session


class _ProvisionedSystemSettings:
    """Settings stub presenting a provisioned system database URL.

    ``modulo.api.dependencies`` resolves its settings via the module-level
    ``get_settings`` name, so tests can present a provisioned reading without
    touching the lru-cached real :class:`Settings`.
    """

    modulo_system_database_url = "postgresql+asyncpg://localhost/modulo-system-unit-test"


@pytest.fixture(autouse=True)
def _provisioned_system_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Present a PROVISIONED system engine to the API unit-test suite.

    Unit tests mock the system SESSION but run in an environment without
    ``MODULO_SYSTEM_DATABASE_URL``. The (now robust) fallback predicate
    initialises the engine factory itself, so an un-provisioned reading would
    503 every trigger delivery here. With a provisioned URL the flag reads
    False exactly as in production; the created engine is lazy and never
    connects (every system session is overridden per test).
    """
    from modulo.api import dependencies as _deps

    monkeypatch.setattr(_deps, "get_settings", lambda: _ProvisionedSystemSettings())


@pytest.fixture(autouse=True)
def _prevent_db_auth_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent ``_verify_identity`` from connecting to a real database.

    All API unit tests mock the auth layer via ``dependency_overrides``
    on ``get_current_user``, but ``get_current_tenant_user`` also calls
    ``_verify_identity()`` which connects to Postgres to confirm the
    JWT's account/org still exist.  Monkey-patching ``_verify_identity``
    here avoids the DB call for every test in this package.
    """
    monkeypatch.setattr("modulo.auth.dependencies._verify_identity", AsyncMock(return_value=None))
