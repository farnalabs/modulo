"""Test configuration for API unit tests.

Sets minimal env vars so ``get_settings()`` (called by middleware at
request time) can construct a ``Settings`` instance.

Autouse fixtures deliberately do NOT live here. pytest keys a conftest's
autouse-fixture names to the exact ``Package`` node object current when the
conftest was parsed, so an explicit multi-file argv that detours out of
``tests/unit/api/`` and back in collects the later file under a fresh
``tests/unit/api`` Package node that never had this conftest's autouse names
registered — the fixtures silently drop and its routes 401 (FAR-1229). The
unit-level autouse fixtures (``_patch_verify_identity``,
``_far681_any_credential_default``, ``_provisioned_system_engine``) ride a
node shared by every argv item under ``tests/unit/`` and are stable under any
interleaving. Put autouse fixtures in ``tests/unit/conftest.py``; keep only
importable helpers here.
"""

import os
import uuid
from unittest.mock import AsyncMock, MagicMock

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

# FAR-1597: the provisioned-system-engine fixture moved to tests/unit/conftest.py
# so it holds at a stable collection scope (a directory-level Package node can
# drop conftest autouse fixtures when an argv section detours out of this
# directory). Keep the settings stub importable from HERE for compatibility —
# the in-flight FAR-1569 Slack-isolation fix imports it as
# ``from tests.unit.api.conftest import _ProvisionedSystemSettings`` — until
# that branch's import is retargeted.
from tests.unit.conftest import _ProvisionedSystemSettings  # noqa: E402, F401


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
