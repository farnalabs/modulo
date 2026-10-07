"""Unit tests for ``modulo.db.crud.team_scope`` (FAR-1515).

The connector re-scope gate (``modulo.api.routes.connectors``) locates the
pipelines that bind a connector through ``pipelines_binding_connector``. That
helper is a cheap text-contains prefilter read through ``team_blind_org_scope``;
off Postgres there is no team RLS policy, so the scope helper issues no SQL and
the query runs directly. The test double's bind reports ``sqlite`` to exercise
that path without a database.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

from modulo.db.crud.team_scope import pipelines_binding_connector

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


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
