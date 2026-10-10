"""Pin tests for the audited organisation row-lock modes (FAR-1624).

PR #1491 (FAR-1624) narrowed the finalisation gate's org lock to ``FOR NO KEY
UPDATE`` so it cannot enter the KEY-SHARE -> FOR-UPDATE upgrade cycle (that gate
already pre-holds a foreign-key ``KEY SHARE`` on the org via the run row it locks
first). The multi-lens audit that produced that change deliberately LEFT the
sibling org-lock sites on plain ``FOR UPDATE``: each acquires the org lock FIRST
and therefore holds no foreign-key ``KEY SHARE`` on the org (so it cannot enter
the upgrade cycle), and org deletion needs the strongest lock to block
concurrent child inserts before a hard delete.

These tests are the defensive pin against a future "consistency sweep" that
flattens every org lock to one mode: the audited org selects must compile to
``FOR UPDATE``, never ``FOR NO KEY UPDATE`` or ``FOR SHARE``. The finalisation
gate's own ``FOR NO KEY UPDATE`` assertion lives with that change and is
deliberately not duplicated here.

Only the compiled SQL is asserted (no Postgres is required by this module's own
tests), but the module lives under ``tests/integration/`` per the task contract.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from modulo.core.runtime_config.org_flags import (
    FLAG_WORK_ITEM_AGENT_MINTING_ENABLED,
    set_org_flag,
)
from modulo.db.crud.observability import update_otel_config
from modulo.db.crud.org_deletion import (
    cancel_org_deletion,
    confirm_org_deletion,
    export_org_data,
    request_org_deletion,
)
from modulo.db.crud.organisation import get_organisation

pytestmark = pytest.mark.integration

_ORG_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ACTOR_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")

_ORG_NOT_FOUND = "Organisation not found"


class _MissingOrgResult:
    """Session result whose ``scalar_one_or_none`` reports a missing org.

    Returning ``None`` short-circuits each target function immediately after its
    org-lock SELECT (each then raises or returns on the missing org), so the
    single captured statement is the lock under test and nothing later executes.
    """

    def scalar_one_or_none(self) -> None:
        return None


class _CapturingSession:
    """Minimal AsyncSession double that records the statements issued to it."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> _MissingOrgResult:
        self.statements.append(statement)
        return _MissingOrgResult()


def _compiled_sql(statement: Any) -> str:
    compiled = statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    return str(compiled).upper()


def _assert_org_lock_is_for_update(session: _CapturingSession, label: str) -> None:
    """Assert the first (org-lock) statement is FOR UPDATE, not a weaker mode."""
    assert session.statements, f"{label}: no statement was issued"
    sql = _compiled_sql(session.statements[0])
    assert "FOR UPDATE" in sql, f"{label}: the org lock is not FOR UPDATE: {sql}"
    assert "FOR NO KEY UPDATE" not in sql, f"{label}: the org lock was weakened to FOR NO KEY UPDATE: {sql}"
    assert "FOR SHARE" not in sql, f"{label}: the org lock was weakened to FOR SHARE: {sql}"


async def test_confirm_org_deletion_org_lock_stays_for_update() -> None:
    """The hard-delete path's org select must stay FOR UPDATE (the audited decision)."""
    session = _CapturingSession()
    with pytest.raises(ValueError, match=_ORG_NOT_FOUND):
        await confirm_org_deletion(session, _ORG_ID, token="token", immediate=True)
    _assert_org_lock_is_for_update(session, "confirm_org_deletion")


async def test_other_org_deletion_paths_org_lock_stays_for_update() -> None:
    """Request, cancel and export in the deletion module keep the same FOR UPDATE lock."""
    request_session = _CapturingSession()
    with pytest.raises(ValueError, match=_ORG_NOT_FOUND):
        await request_org_deletion(request_session, _ORG_ID, _ACTOR_ID)
    _assert_org_lock_is_for_update(request_session, "request_org_deletion")

    cancel_session = _CapturingSession()
    with pytest.raises(ValueError, match=_ORG_NOT_FOUND):
        await cancel_org_deletion(cancel_session, _ORG_ID)
    _assert_org_lock_is_for_update(cancel_session, "cancel_org_deletion")

    export_session = _CapturingSession()
    with pytest.raises(ValueError, match=_ORG_NOT_FOUND):
        await export_org_data(export_session, _ORG_ID)
    _assert_org_lock_is_for_update(export_session, "export_org_data")


async def test_sibling_org_lock_sites_stay_for_update() -> None:
    """The other audited org read-modify-write sites keep FOR UPDATE, not a weaker mode."""
    otel_session = _CapturingSession()
    with pytest.raises(ValueError, match=_ORG_NOT_FOUND):
        await update_otel_config(otel_session, _ORG_ID, {})
    _assert_org_lock_is_for_update(otel_session, "update_otel_config")

    flags_session = _CapturingSession()
    with pytest.raises(LookupError, match="organisation not found"):
        await set_org_flag(flags_session, _ORG_ID, FLAG_WORK_ITEM_AGENT_MINTING_ENABLED, True)
    _assert_org_lock_is_for_update(flags_session, "set_org_flag")

    org_session = _CapturingSession()
    await get_organisation(org_session, _ORG_ID, for_update=True)
    _assert_org_lock_is_for_update(org_session, "get_organisation(for_update=True)")
