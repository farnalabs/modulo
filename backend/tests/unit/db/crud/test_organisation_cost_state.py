"""Unit tests for the org cost-state CRUD (FAR-1833).

The Postgres-backed behaviour (RLS isolation, real ``ON CONFLICT`` semantics)
is covered by ``tests/integration/crud/test_organisation_cost_state.py``. These
tests pin the statement shape and the fail-loud guards at the unit seam so the
unit coverage job exercises the module without a database.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.organisation_cost_state import (
    increment_org_cumulative_spend,
    upsert_org_cost_state,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def session() -> AsyncMock:
    return AsyncMock(spec=AsyncSession)


def _compiled(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


async def test_upsert_writes_only_the_named_ceiling_columns(session: AsyncMock) -> None:
    await upsert_org_cost_state(
        session,
        _ORG_ID,
        provided={"max_run_cost_cents": 1500, "spend_ceiling_cents": None},
    )

    session.execute.assert_awaited_once()
    sql = _compiled(session.execute.await_args.args[0])
    assert "INSERT INTO organisation_cost_state" in sql
    assert "ON CONFLICT (org_id) DO UPDATE" in sql
    assert "max_run_cost_cents" in sql
    assert "spend_ceiling_cents" in sql
    # The cumulative is owned by increment_org_cumulative_spend, never an upsert.
    assert "org_cumulative_spend_cents" not in sql


async def test_upsert_rejects_an_unknown_column(session: AsyncMock) -> None:
    with pytest.raises(ValueError, match="unknown column"):
        await upsert_org_cost_state(session, _ORG_ID, provided={"not_a_column": 1})

    session.execute.assert_not_awaited()


async def test_upsert_rejects_an_empty_mapping(session: AsyncMock) -> None:
    with pytest.raises(ValueError, match="at least one column"):
        await upsert_org_cost_state(session, _ORG_ID, provided={})

    session.execute.assert_not_awaited()


async def test_increment_returns_the_database_computed_total(session: AsyncMock) -> None:
    result = MagicMock()
    result.scalar_one = MagicMock(return_value=12)
    session.execute = AsyncMock(return_value=result)

    total = await increment_org_cumulative_spend(session, _ORG_ID, delta_cents=7)

    assert total == 12
    session.execute.assert_awaited_once()
    sql = _compiled(session.execute.await_args.args[0])
    assert "ON CONFLICT (org_id) DO UPDATE" in sql
    # The addition is computed by the database in the same statement.
    assert "org_cumulative_spend_cents + 7" in sql
    assert "RETURNING organisation_cost_state.org_cumulative_spend_cents" in sql
