"""Unit coverage for database CRUD helpers with real SQLAlchemy statements."""

from __future__ import annotations

import uuid
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import Select, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.connector_instance import list_connector_instances
from modulo.db.crud.eval_run import get_evals_for_runs, get_run_evals
from modulo.db.crud.template import _preview_data_from_content
from modulo.db.crud.trigger import apply_trigger_event_cursor
from modulo.db.repositories import RepositoryHub


async def test_connector_list_returns_empty_page_on_count_programming_error() -> None:
    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = [ProgrammingError("count", {}, Exception("broken"))]

    page = await list_connector_instances(session)

    assert not page.items
    assert page.total == 0


def test_trigger_event_cursor_ignores_malformed_cursor() -> None:
    stmt = select(1)
    assert apply_trigger_event_cursor(stmt, "bad-cursor") is stmt


def test_template_preview_applies_defaults() -> None:
    assert _preview_data_from_content({"graph_nodes": [{}], "edges": [{}]}) == {
        "nodes": [{"id": "", "label": "", "node_type": "agent"}],
        "edges": [{"source": "", "target": "", "edge_type": "normal"}],
    }


async def test_eval_run_empty_inputs_are_real_noops() -> None:
    session = AsyncMock(spec=AsyncSession)
    assert not await get_evals_for_runs(session, org_id=uuid.uuid4(), run_ids=[])
    session.execute.assert_not_awaited()


async def test_eval_run_query_uses_descending_evaluation_order() -> None:
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    session = AsyncMock(spec=AsyncSession)
    session.execute.return_value = result

    await get_run_evals(session, uuid.uuid4())

    statement = session.execute.await_args.args[0]
    assert isinstance(statement, Select)
    assert "eval_results.evaluated_at DESC" in str(statement)


async def test_repository_hub_dispatches_generic_and_proxies_calls() -> None:
    session_factory = MagicMock()
    hub = RepositoryHub(session_factory, db_type="sqlite")
    expected = object()
    execute = AsyncMock(return_value=expected)
    cast(Any, hub.repo).execute = execute

    outcome = await hub.execute(AsyncMock(spec=AsyncSession), select(1))

    assert outcome is expected
    execute.assert_awaited_once()


def test_repository_hub_dispatches_postgres() -> None:
    hub = RepositoryHub(MagicMock(), db_type="postgres")
    assert hub.repo.__class__.__name__ == "PostgresRepository"
