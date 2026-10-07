"""Unit tests for ``modulo.db.crud.team_scope`` (FAR-1515).

The connector re-scope gate (``modulo.api.routes.connectors``) locates the
pipelines that bind a connector through ``pipelines_binding_connector``. That
helper is a cheap text-contains prefilter read through ``team_blind_org_scope``;
off Postgres there is no team RLS policy, so the scope helper issues no SQL and
the query runs directly. The test double's bind reports ``sqlite`` to exercise
that path without a database.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.db.crud.team_scope import (
    BlindPipelineScope,
    pipeline_team_scope_team_blind,
    pipelines_binding_connector,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f1")


async def test_pipelines_binding_connector_returns_the_prefilter_rows() -> None:
    connector_id = uuid.uuid4()
    first, second = MagicMock(), MagicMock()

    session = AsyncMock()
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.get_bind = MagicMock(return_value=bind)
    result = MagicMock()
    result.scalars = MagicMock(return_value=[first, second])
    session.execute = AsyncMock(return_value=result)

    rows = await pipelines_binding_connector(session, _ORG_ID, connector_id)

    assert rows == [first, second]
    assert session.execute.await_count == 1


async def test_pipelines_binding_connector_returns_empty_when_nothing_binds() -> None:
    session = AsyncMock()
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.get_bind = MagicMock(return_value=bind)
    result = MagicMock()
    result.scalars = MagicMock(return_value=[])
    session.execute = AsyncMock(return_value=result)

    rows = await pipelines_binding_connector(session, _ORG_ID, uuid.uuid4())

    assert rows == []
    assert session.execute.await_count == 1


def _first_result(row: object) -> MagicMock:
    result = MagicMock()
    result.first = MagicMock(return_value=row)
    return result


async def test_pipeline_team_scope_team_blind_non_postgres_absent_row_returns_none() -> None:
    # Off Postgres there is no team RLS policy, so the plain org-scoped read IS
    # the team-blind view. An absent row must resolve to None (not raise).
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_first_result(None))

    with patch("modulo.db.rls._ensure_active_transaction", AsyncMock(return_value="sqlite")):
        scope = await pipeline_team_scope_team_blind(session, _PIPELINE_ID)

    assert scope is None
    assert session.execute.await_count == 1


async def test_pipeline_team_scope_team_blind_postgres_reads_then_clears_guc() -> None:
    # On Postgres the read flips the execution-context GUC on, reads the row,
    # then clears the GUC in the finally so the rest of the txn stays
    # caller-facing. Two execute calls: the read + the GUC clear.
    owner = uuid.uuid4()
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_first_result((owner, "team")))

    with (
        patch("modulo.db.rls._ensure_active_transaction", AsyncMock(return_value="postgresql")),
        patch("modulo.db.rls.set_rls_execution_context", new_callable=AsyncMock) as mock_set,
    ):
        scope = await pipeline_team_scope_team_blind(session, _PIPELINE_ID)

    assert scope == BlindPipelineScope(owner_team_id=owner, visibility="team")
    mock_set.assert_awaited_once_with(session)
    assert session.execute.await_count == 2


async def test_pipeline_team_scope_team_blind_postgres_absent_row_returns_none() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_first_result(None))

    with (
        patch("modulo.db.rls._ensure_active_transaction", AsyncMock(return_value="postgresql")),
        patch("modulo.db.rls.set_rls_execution_context", new_callable=AsyncMock),
    ):
        scope = await pipeline_team_scope_team_blind(session, _PIPELINE_ID)

    assert scope is None
