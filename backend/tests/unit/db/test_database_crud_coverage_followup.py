"""Focused branch coverage for FAR-366 database follow-up paths."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.connector_instance import (
    clear_degraded_markers,
    list_connector_instances,
    mark_instances_degraded,
)
from modulo.db.crud.pagination import CursorPaginator
from modulo.db.crud.pipeline_snapshot import (
    _apply_agent_fields,
    _build_parameter_binding,
    _collect_parameter_set_ids,
    _resolve_node_parameters,
)
from modulo.db.crud.rating import (
    get_rating_aggregate,
    list_abuse_reports,
    list_ratings_for_primitive,
    submit_rating,
)
from modulo.db.crud.template import list_templates
from modulo.db.crud.trigger import list_triggers
from modulo.db.models.agent import Agent
from modulo.db.models.parameter_schema import ParameterSchema
from modulo.db.models.parameter_set import ParameterSet
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.repositories import RepositoryHub


def _result(*, scalar_one: object = None, rows: list[object] | None = None) -> MagicMock:
    result = MagicMock()
    result.scalar_one.return_value = scalar_one
    result.scalar_one_or_none.return_value = scalar_one
    result.scalars.return_value = result
    result.all.return_value = rows or []
    return result


async def test_connector_cursor_without_exclusions_and_empty_marker_updates() -> None:
    session = AsyncMock(spec=AsyncSession)
    session.execute.return_value = _result(rows=[])
    cursor = CursorPaginator.encode_cursor(datetime.now(UTC), uuid.uuid4())
    page = await list_connector_instances(session, cursor=cursor, excluded_tiers=[])
    assert not page.items
    await mark_instances_degraded(session, {})
    await clear_degraded_markers(session, [])


async def test_template_list_supports_blank_search_and_programming_error() -> None:
    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = [ProgrammingError("count", {}, RuntimeError("missing"))]
    page = await list_templates(session, category=None, search="   ")
    assert not page.items
    assert page.total == 0


async def test_trigger_list_team_scope_path() -> None:
    session = AsyncMock(spec=AsyncSession)
    session.execute.return_value = _result(rows=[])
    page = await list_triggers(session, uuid.uuid4(), team_id=uuid.uuid4(), limit=3)
    assert page.page_size == 3


def test_snapshot_agent_and_parameter_helpers_cover_optional_fields() -> None:
    agent = Agent(
        id=uuid.uuid4(),
        name="agent",
        prompt_template="prompt",
        token_budget=100,
        agent_commands=["run"],
        parameter_schema_id=uuid.uuid4(),
        schema_profile="strict",
    )
    node: dict[str, object] = {"agent_id": str(agent.id), "schema_profile": "explicit"}
    assert _apply_agent_fields(node, agent) == agent.parameter_schema_id
    assert node["schema_profile"] == "explicit"
    assert _apply_agent_fields({}, Agent(id=uuid.uuid4(), name="empty")) is None

    schema = ParameterSchema(id=uuid.uuid4(), parameters=[{"name": "one", "default": 1}, "invalid"])
    parameter_set = ParameterSet(id=uuid.uuid4(), values={"one": 2, "two": 3})
    node = {"parameter_set_id": str(parameter_set.id), "parameter_overrides": {"two": 4}}
    assert _resolve_node_parameters(schema, {parameter_set.id: parameter_set}, node) == {"one": 2, "two": 4}
    assert _collect_parameter_set_ids([node, {}, {"parameter_set_id": None}]) == {parameter_set.id}
    binding = _build_parameter_binding(node, schema.id, {"one": 2}, parameter_set.id)
    assert binding["parameter_set_id"] == str(parameter_set.id)


async def test_rating_none_account_and_empty_or_broken_queries() -> None:
    session = AsyncMock(spec=AsyncSession)
    primitive_id = uuid.uuid4()
    rating = await submit_rating(
        session,
        org_id=uuid.uuid4(),
        primitive_id=primitive_id,
        thumbs_up=False,
        account_id=None,
    )
    assert rating.primitive_id == primitive_id
    session.execute.side_effect = [ProgrammingError("count", {}, RuntimeError("missing"))]
    assert await get_rating_aggregate(session, primitive_id) == (None, 0)
    session.execute.side_effect = [ProgrammingError("count", {}, RuntimeError("missing"))]
    assert not (await list_ratings_for_primitive(session, primitive_id)).items
    session.execute.side_effect = [ProgrammingError("count", {}, RuntimeError("missing"))]
    assert not (await list_abuse_reports(session, uuid.uuid4())).items


def test_repository_hub_proxies_tenant_and_pagination_calls() -> None:
    session_factory = MagicMock()
    hub = RepositoryHub(session_factory, db_type="sqlite")
    session = AsyncMock(spec=AsyncSession)
    stmt = select(PipelineSnapshot)
    org_id = uuid.uuid4()
    result = MagicMock()
    result.scalar_one_or_none.return_value = 0
    result.scalars.return_value.all.return_value = []
    session.execute.return_value = result
    import asyncio

    asyncio.run(hub.set_org_context(session, org_id))
    filtered = hub.apply_tenant_filter(stmt, org_id)
    assert "organisation_id" in str(filtered)
    asyncio.run(hub.paginate(session, stmt, 1, 2))
    asyncio.run(hub.execute(session, stmt))
