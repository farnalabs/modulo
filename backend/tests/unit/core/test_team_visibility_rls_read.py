"""FAR-1515 CRITICAL 1: the candidate read must be team-blind but org-scoped.

``rls_team_isolation`` (migration 0124) hides a ``visibility='team'`` row the
request caller does not own. Before this fix
``_find_team_scope_mismatches`` SELECTed candidate connectors in the caller's
own request session, so a Team-A-only member binding Team-B's connector got
ZERO rows back, the mismatch loop skipped them silently, and the save was
accepted - at run time the hub loads the same connector team-blind and runs
with Team-B's credentials.

These tests pin the READ half of the fix at the statement level:

* on Postgres the read saves the caller's ``app.organisation_id`` /
  ``app.execution_context`` GUCs, widens to the internal-execution context
  (org AND-gated), runs the candidate SELECT, and RESTORES the caller's GUCs -
  in that order, with the restore guaranteed even when the SELECT raises,
* the widened read surfaces another team's team-private connector as a real
  mismatch (the exact row RLS used to hide),
* off Postgres (no team RLS policies) only the candidate SELECT is issued -
  no GUC statements at all.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.team_visibility import (
    ConnectorTeamMismatch,
    connector_team_mismatch_detail,
    find_connector_team_mismatches,
)
from modulo.db.models.connector_instance import ConnectorInstance

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_TEAM_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_TEAM_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
_NODE_ID = str(uuid.uuid4())


def _hidden_team_connector() -> ConnectorInstance:
    """Team-B's team-private connector: invisible to a Team-A-only caller."""
    return ConnectorInstance(
        id=uuid.uuid4(),
        organisation_id=_ORG_ID,
        name="team-b-secrets",
        owner_team_id=_TEAM_B,
        visibility="team",
    )


def _binding(conn: ConnectorInstance) -> list[dict[str, str]]:
    return [{"node_id": _NODE_ID, "connector_instance_id": str(conn.id)}]


def _pg_session(statements: list[Any], rows: list[object], *, guc_prev: tuple[Any, Any]) -> AsyncMock:
    """A postgres-dialect session recording every statement it is asked to run.

    Dispatches on SQL text: the GUC read returns ``guc_prev``, the candidate
    SELECT returns ``rows``, the set_config statements return a blank result.
    """
    session = AsyncMock()
    bind = MagicMock()
    bind.dialect.name = "postgresql"
    session.get_bind = MagicMock(return_value=bind)

    guc_result = MagicMock()
    guc_result.one.return_value = guc_prev
    select_result = MagicMock()
    select_result.scalars.return_value.all.return_value = rows
    blank = MagicMock()

    async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        # The GUC statements pass their bind params positionally.
        statements.append((str(stmt), args[0] if args else kwargs))
        sql = str(stmt)
        if "current_setting" in sql:
            return guc_result
        if "FROM connector_instances" in sql:
            return select_result
        return blank

    session.execute = AsyncMock(side_effect=_execute)
    return session


def _sqlite_session(statements: list[Any], rows: list[object]) -> AsyncMock:
    session = AsyncMock()
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.get_bind = MagicMock(return_value=bind)
    select_result = MagicMock()
    select_result.scalars.return_value.all.return_value = rows

    async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        statements.append((str(stmt), kwargs))
        return select_result

    session.execute = AsyncMock(side_effect=_execute)
    return session


@pytest.mark.asyncio
async def test_postgres_read_saves_widens_selects_then_restores_in_order() -> None:
    """The widen must bracket exactly the candidate SELECT (save -> widen ->
    select -> restore), and the widen must keep the read ORG-scoped.

    Without the widen this read runs under the caller's team filter and the
    hidden row never reaches the predicate; without the restore the rest of
    the request transaction would silently become team-blind too.
    """
    conn = _hidden_team_connector()
    statements: list[Any] = []
    session = _pg_session(statements, [conn], guc_prev=("", ""))

    mismatches = await find_connector_team_mismatches(session, _ORG_ID, _TEAM_A, _binding(conn))

    kinds = []
    for sql, _kwargs in statements:
        if "current_setting" in sql:
            kinds.append("save")
        elif "app.execution_context', 'true'" in sql:
            kinds.append("widen")
        elif "FROM connector_instances" in sql:
            kinds.append("select")
        else:
            kinds.append("restore")
    assert kinds == ["save", "widen", "select", "restore"], kinds

    widen_kwargs = statements[1][1]
    assert widen_kwargs == {"oid": str(_ORG_ID)}, "the widened read must stay AND-gated to this org"
    restore_kwargs = statements[3][1]
    assert restore_kwargs == {"oid": "", "exec_ctx": ""}, "the caller's previous GUCs must be restored"

    # The widened read surfaced Team-B's hidden row and judged it: without the
    # fix this list was EMPTY (row absent -> silently skipped -> save accepted).
    assert len(mismatches) == 1
    mismatch = mismatches[0]
    assert isinstance(mismatch, ConnectorTeamMismatch)
    assert mismatch.connector_name == "team-b-secrets"
    assert mismatch.connector_owner_team_id == _TEAM_B
    assert mismatch.pipeline_owner_team_id == _TEAM_A
    assert mismatch.node_id == _NODE_ID
    detail = connector_team_mismatch_detail(mismatches)
    assert "team-b-secrets" in detail
    assert "is team-private" in detail


@pytest.mark.asyncio
async def test_postgres_read_restores_the_context_even_when_the_select_raises() -> None:
    """A failed SELECT must not leave the request transaction team-blind."""
    conn = _hidden_team_connector()
    statements: list[Any] = []
    session = _pg_session(statements, [conn], guc_prev=("prev-org", "prev-exec"))

    async def _boom(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        statements.append((str(stmt), args[0] if args else kwargs))
        if "FROM connector_instances" in str(stmt):
            raise RuntimeError("connection lost")
        guc_result = MagicMock()
        guc_result.one.return_value = ("prev-org", "prev-exec")
        return guc_result

    session.execute = AsyncMock(side_effect=_boom)

    with pytest.raises(RuntimeError, match="connection lost"):
        await find_connector_team_mismatches(session, _ORG_ID, _TEAM_A, _binding(conn))

    restore_statements = [sql for sql, _kw in statements if "set_config" in sql and "'true'" not in sql]
    assert restore_statements, f"the context must be restored in a finally; statements: {[s for s, _ in statements]}"
    restore_kwargs = [kw for sql, kw in statements if "set_config" in sql and "'true'" not in sql]
    assert restore_kwargs[-1] == {"oid": "prev-org", "exec_ctx": "prev-exec"}


@pytest.mark.asyncio
async def test_non_postgres_read_issues_only_the_candidate_select() -> None:
    """Off Postgres there is no team RLS policy - the org-scoped SELECT is
    already team-blind, so no GUC dance is needed or issued."""
    conn = _hidden_team_connector()
    statements: list[Any] = []
    session = _sqlite_session(statements, [conn])

    mismatches = await find_connector_team_mismatches(session, _ORG_ID, _TEAM_A, _binding(conn))

    assert len(statements) == 1, f"expected exactly the candidate SELECT: {[s for s, _ in statements]}"
    assert "FROM connector_instances" in statements[0][0]
    assert len(mismatches) == 1
    assert mismatches[0].connector_name == "team-b-secrets"


@pytest.mark.asyncio
async def test_postgres_read_reports_an_unresolvable_binding_as_a_refusal() -> None:
    """The widened read is what makes `absent` definitive: no row -> fail closed.

    With the team-blind read in place, an id the organisation cannot resolve
    can never be a hidden connector - so refusing it (the named
    ``connector_team_mismatch`` error, never a silent skip) cannot produce
    false positives for hidden rows.
    """
    from modulo.core.team_visibility import ConnectorBindingMissingError

    statements: list[Any] = []
    session = _pg_session(statements, [], guc_prev=("", ""))
    missing_id = uuid.uuid4()

    with pytest.raises(ConnectorBindingMissingError) as excinfo:
        await find_connector_team_mismatches(
            session, _ORG_ID, _TEAM_A, [{"node_id": _NODE_ID, "connector_instance_id": str(missing_id)}]
        )

    assert excinfo.value.missing == [(missing_id, _NODE_ID)]
    detail = str(excinfo.value)
    assert detail.startswith("connector_team_mismatch")
    assert str(missing_id) in detail
    assert "does not resolve to a connector in this organisation" in detail
    # The widening still bracketed the read that produced the definitive
    # absence: save -> widen -> select -> restore.
    kinds = []
    for sql, _kw in statements:
        if "current_setting" in sql:
            kinds.append("save")
        elif "set_config" in sql and "'true'" in sql:
            kinds.append("widen")
        elif "FROM connector_instances" in sql:
            kinds.append("select")
        else:
            kinds.append("restore")
    assert kinds == ["save", "widen", "select", "restore"], kinds
