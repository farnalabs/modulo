"""Pin tests for the audited organisation row-lock modes (FAR-1624, FAR-1832).

PR #1491 (FAR-1624) narrowed the finalisation gate's org lock to ``FOR NO KEY
UPDATE`` so it cannot enter the KEY-SHARE -> FOR-UPDATE upgrade cycle (that gate
already pre-holds a foreign-key ``KEY SHARE`` on the org via the run row it locks
first). The multi-lens audit that produced that change deliberately LEFT the
sibling org-lock sites on plain ``FOR UPDATE``: each acquires the org lock FIRST
and therefore holds no foreign-key ``KEY SHARE`` on the org (so it cannot enter
the upgrade cycle), and org deletion needs the strongest lock to block
concurrent child inserts before a hard delete.

FAR-1832 then CONVERTED the org-settings RMW sites (sandbox/HITL writers,
analytics consent, OTEL config, runtime flags, licence) to single-statement
atomic patches — those writers no longer take a ``FOR UPDATE`` pre-lock at all
(the UPDATE statement's own row lock is statement-scoped, and on the generic
non-Postgres dialects the patch helper's fallback keeps the lock it needs).

FAR-1823 then REMOVED the org-row FOR UPDATE lock from ``export_org_data``:
live captures now use a read-only REPEATABLE READ snapshot on a separate
connection, and the stored ``export_bundle_json`` is returned directly without
any lock. The org-row lock never gave cross-table consistency because child-row
UPDATEs were never blocked by it — the export reads multiple child tables that
the org lock does not protect.

This file pins BOTH halves: the deletion-FSM sites that must KEEP ``FOR UPDATE``
(``request_org_deletion`` FSM re-read, ``confirm_org_deletion``, ``cancel_org_deletion``),
and the converted config writers + export path that must NOT take one.
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


class _PgDialect:
    name = "postgresql"


class _PgBind:
    """Minimal bind double reporting the postgresql dialect."""

    def __init__(self) -> None:
        self.dialect = _PgDialect()


class _MissingOrgResult:
    """Session result reporting a missing org on every accessor the writers use.

    Returning ``None``/``None`` short-circuits each target function right after
    its statement issues (the single-statement patch path reads the row back via
    ``first()``, the ORM path via ``scalar_one_or_none()``), so the captured
    statements are exactly the writes under test and nothing later executes.
    """

    def scalar_one_or_none(self) -> None:
        return None

    def first(self) -> None:
        return None


class _GucResult:
    """Result for the ``current_setting('app.organisation_id')`` read.

    FAR-1832 QA F4: the settings patch's tenant guard is dialect-FIRST on
    Postgres — it reads the GUC and ignores ``session.info`` entirely — so a
    Postgres-dialect session double must answer the GUC read with the bound
    org (exactly what ``set_rls_org``'s ``set_config`` write produces in
    production) for the guard to pass.
    """

    def __init__(self, org_id: str) -> None:
        self._org_id = org_id

    def scalar(self) -> str:
        return self._org_id


class _CapturingSession:
    """Minimal AsyncSession double that records the statements issued to it.

    Presents the postgresql dialect (so the writers take their production
    single-statement path) and a bound org RLS context (so the settings patch's
    fail-closed tenant guard passes): the GUC read answers with the org id the
    way a real ``set_rls_org``-bound Postgres session would, and
    ``info['org_id']`` mirrors it (harmless on Postgres — the guard ignores it).
    """

    def __init__(self) -> None:
        self.statements: list[Any] = []
        self.info: dict[str, Any] = {"org_id": str(_ORG_ID)}

    def get_bind(self) -> _PgBind:
        return _PgBind()

    async def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> Any:
        self.statements.append(statement)
        if "current_setting('app.organisation_id'" in str(statement):
            return _GucResult(str(_ORG_ID))
        return _MissingOrgResult()


def _compiled_sql(statement: Any) -> str:
    # No literal_binds: the converted writers bind JSON/ARRAY parameters the
    # compiler cannot inline, and every assertion here is structural (lock
    # mode / UPDATE shape / column name) — placeholders render fine.
    compiled = statement.compile(dialect=postgresql.dialect())
    return str(compiled).upper()


def _assert_org_lock_is_for_update(session: _CapturingSession, label: str, statement_index: int = 0) -> None:
    """Assert the specified (org-lock) statement is FOR UPDATE, not a weaker mode."""
    assert session.statements, f"{label}: no statement was issued"
    sql = _compiled_sql(session.statements[statement_index])
    assert "FOR UPDATE" in sql, f"{label}: the org lock is not FOR UPDATE: {sql}"
    assert "FOR NO KEY UPDATE" not in sql, f"{label}: the org lock was weakened to FOR NO KEY UPDATE: {sql}"
    assert "FOR SHARE" not in sql, f"{label}: the org lock was weakened to FOR SHARE: {sql}"


def _assert_no_for_update_prelock(session: _CapturingSession, label: str) -> None:
    """Assert NO issued statement takes a row lock (FAR-1832: no pre-lock)."""
    assert session.statements, f"{label}: no statement was issued"
    for statement in session.statements:
        sql = _compiled_sql(statement)
        assert "FOR UPDATE" not in sql, f"{label}: writer still pre-locks the org row: {sql}"


def _assert_issued_an_update(session: _CapturingSession, column: str, label: str) -> None:
    """Assert a single-statement UPDATE touching *column* was issued."""
    column = column.upper()
    for statement in session.statements:
        sql = _compiled_sql(statement)
        if sql.startswith("UPDATE") and column in sql:
            return
    raise AssertionError(
        f"{label}: no UPDATE touching {column} was issued: {[_compiled_sql(s) for s in session.statements]}"
    )


async def test_confirm_org_deletion_org_lock_stays_for_update() -> None:
    """The hard-delete path's org select must stay FOR UPDATE (the audited decision)."""
    session = _CapturingSession()
    with pytest.raises(ValueError, match=_ORG_NOT_FOUND):
        await confirm_org_deletion(session, _ORG_ID, token="token", immediate=True)
    _assert_org_lock_is_for_update(session, "confirm_org_deletion")


async def test_other_org_deletion_paths_org_lock_stays_for_update() -> None:
    """Request and cancel in the deletion module keep the FOR UPDATE lock.

    export_org_data no longer takes an org-row lock (FAR-1823): live captures
    use a read-only REPEATABLE READ snapshot on a separate connection, and the
    stored export_bundle_json is returned directly without any lock.
    """

    class _RequestDeletionSession(_CapturingSession):
        """Session that returns valid org first, then missing for request_org_deletion."""

        def __init__(self) -> None:
            super().__init__()
            self._org_read_count = 0

        async def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> Any:
            self.statements.append(statement)
            if "current_setting('app.organisation_id'" in str(statement):
                return _GucResult(str(_ORG_ID))
            # Check if this is an org read (SELECT ... FROM organisations)
            if "FROM organisations" in str(statement) or "FROM ORGANISATIONS" in str(statement).upper():
                self._org_read_count += 1
                if self._org_read_count == 1:
                    # First org read: return valid org (pre-lock validation)
                    from unittest.mock import MagicMock

                    org = MagicMock()
                    org.id = _ORG_ID
                    org.status = "active"
                    org.deleted_at = None
                    org.deletion_token = None
                    org.deletion_token_expires_at = None
                    org.export_bundle_json = None
                    result = MagicMock()
                    result.scalar_one_or_none.return_value = org
                    return result
                # Second org read: return missing (FSM re-read under lock)
                return _MissingOrgResult()
            return _MissingOrgResult()

    request_session = _RequestDeletionSession()
    # Patch _collect_org_export_in_snapshot to avoid real DB connection
    from unittest.mock import AsyncMock, patch

    target = "modulo.db.crud.org_deletion._collect_org_export_in_snapshot"
    with patch(target, AsyncMock(return_value={"organisation": []})), pytest.raises(ValueError, match=_ORG_NOT_FOUND):
        await request_org_deletion(request_session, _ORG_ID, _ACTOR_ID)
    # request_org_deletion has two org reads: 1) pre-lock validation (no FOR UPDATE)
    # 2) FSM FOR UPDATE re-read. Check the second statement (index 1).
    _assert_org_lock_is_for_update(request_session, "request_org_deletion", statement_index=1)

    cancel_session = _CapturingSession()
    with pytest.raises(ValueError, match=_ORG_NOT_FOUND):
        await cancel_org_deletion(cancel_session, _ORG_ID)
    _assert_org_lock_is_for_update(cancel_session, "cancel_org_deletion")

    # export_org_data: no org-row FOR UPDATE lock (snapshot path).
    export_session = _CapturingSession()
    with patch(target, AsyncMock(return_value={"organisation": []})), pytest.raises(ValueError, match=_ORG_NOT_FOUND):
        await export_org_data(export_session, _ORG_ID)
    # export_org_data does a pre-lock validation read (no FOR UPDATE), then calls
    # _collect_org_export_in_snapshot (patched). Assert no FOR UPDATE was issued.
    for stmt in export_session.statements:
        sql = _compiled_sql(stmt)
        assert "FOR UPDATE" not in sql, f"export_org_data must not issue FOR UPDATE: {sql}"


async def test_get_organisation_for_update_still_takes_for_update() -> None:
    """The opt-in generic read keeps its FOR UPDATE — it is the generic-dialect
    implementation of the atomic settings patch's read-merge-write fallback."""
    org_session = _CapturingSession()
    await get_organisation(org_session, _ORG_ID, for_update=True)
    _assert_org_lock_is_for_update(org_session, "get_organisation(for_update=True)")


async def test_sibling_config_writers_no_longer_prelock_the_org_row() -> None:
    """FAR-1832: the converted org-settings writers are single-statement atomic
    patches — each issues a settings UPDATE and NO ``FOR UPDATE`` pre-lock.

    Fails against the old shape: there, the first statement of each writer was
    a ``SELECT ... FOR UPDATE`` on the org row (the RMW pre-lock this ticket
    removed).
    """
    otel_session = _CapturingSession()
    with pytest.raises(ValueError, match=_ORG_NOT_FOUND):
        await update_otel_config(otel_session, _ORG_ID, {})
    _assert_no_for_update_prelock(otel_session, "update_otel_config")
    _assert_issued_an_update(otel_session, "otel_config_json", "update_otel_config")

    flags_session = _CapturingSession()
    with pytest.raises(LookupError, match="organisation not found"):
        await set_org_flag(flags_session, _ORG_ID, FLAG_WORK_ITEM_AGENT_MINTING_ENABLED, True)
    _assert_no_for_update_prelock(flags_session, "set_org_flag")
    _assert_issued_an_update(flags_session, "settings_json", "set_org_flag")
