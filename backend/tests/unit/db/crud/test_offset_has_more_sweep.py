"""Offset/page paging of the CRUD list sweep must report ``has_more`` (FAR-1877).

Every offset branch below used to return ``PageResult(...)`` without passing
``has_more``, so it silently inherited the dataclass default ``False`` and a
page/page-size client could never detect that more pages exist (``total: 5``,
``has_more: false`` on every page). The cursor branches were already correct.

These tests drive the REAL CRUD functions against an in-memory SQLite engine
so the count + offset arithmetic is exercised end-to-end. Each case seeds
``page_size + 2`` rows, asserts page 1 reports ``has_more is True``, and
asserts the final page reports ``has_more is False``. Every case FAILS without
the production fix and PASSes with it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.agent import list_agents
from modulo.db.crud.base import PageResult
from modulo.db.crud.composite_template import list_composite_templates
from modulo.db.crud.connector_instance import list_connector_instances
from modulo.db.crud.environment_profile import list_environment_profiles
from modulo.db.crud.external_agent import list_external_agents
from modulo.db.crud.library_primitive import list_library_primitives
from modulo.db.crud.mcp_server import list_mcp_servers
from modulo.db.crud.model_backend import list_model_backends, list_pipeline_references_for_backend
from modulo.db.crud.node_category import list_node_categories
from modulo.db.crud.publisher import list_publishers
from modulo.db.crud.rating import list_abuse_reports, list_ratings_for_primitive
from modulo.db.crud.run import list_runs
from modulo.db.crud.schema import list_schema_versions
from modulo.db.crud.team import list_teams
from modulo.db.crud.team_membership import list_team_members
from modulo.db.crud.template import list_templates
from modulo.db.crud.view import list_views
from modulo.db.models.agent import Agent
from modulo.db.models.base import Base
from modulo.db.models.composite_template import CompositeTemplate
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.environment_profile import EnvironmentProfile
from modulo.db.models.external_agent import ExternalAgent
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.models.mcp_server import McpServer
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.node_category import NodeCategory
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.primitive_abuse_report import PrimitiveAbuseReport
from modulo.db.models.primitive_rating import PrimitiveRating
from modulo.db.models.publisher import Publisher
from modulo.db.models.run import Run
from modulo.db.models.schema import Schema, SchemaVersion
from modulo.db.models.team import Team
from modulo.db.models.team_membership import TeamMembership
from modulo.db.models.view import SavedView

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000a1")

_PAGE_SIZE = 3
_COUNT = _PAGE_SIZE + 2  # 5 rows: page 1 is full with more beyond, page 2 is final.
_FINAL_PAGE = 2

_TABLES: list[Table] = [
    Organisation.__table__,
    Agent.__table__,
    CompositeTemplate.__table__,
    ConnectorInstance.__table__,
    EnvironmentProfile.__table__,
    ExternalAgent.__table__,
    McpServer.__table__,
    LibraryPrimitive.__table__,
    ModelBackend.__table__,
    NodeCategory.__table__,
    Pipeline.__table__,
    PrimitiveAbuseReport.__table__,
    PrimitiveRating.__table__,
    Publisher.__table__,
    Run.__table__,
    Schema.__table__,
    SchemaVersion.__table__,
    Team.__table__,
    TeamMembership.__table__,
    SavedView.__table__,
]

# A setup function seeds _COUNT rows and returns a caller(page, page_size).
PageCall = Callable[[int, int], Awaitable[PageResult[Any]]]
SetupFn = Callable[[AsyncSession], Awaitable[PageCall]]


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


async def _setup_agents(session: AsyncSession) -> PageCall:
    for i in range(_COUNT):
        session.add(
            Agent(
                organisation_id=_ORG,
                name=f"agent {i}",
                prompt_template="Do something",
                account_id=_ACCOUNT,
            )
        )
    await session.flush()
    return lambda page, page_size: list_agents(session, page=page, page_size=page_size)


async def _setup_composite_templates(session: AsyncSession) -> PageCall:
    for i in range(_COUNT):
        session.add(
            CompositeTemplate(
                organisation_id=_ORG,
                account_id=_ACCOUNT,
                name=f"composite {i}",
            )
        )
    await session.flush()
    return lambda page, page_size: list_composite_templates(session, org_id=_ORG, page=page, page_size=page_size)


async def _setup_connector_instances(session: AsyncSession) -> PageCall:
    for i in range(_COUNT):
        session.add(
            ConnectorInstance(
                organisation_id=_ORG,
                name=f"connector {i}",
                connector_type_id="github",
                account_id=_ACCOUNT,
                credentials_ciphertext=b"ciphertext",
            )
        )
    await session.flush()
    return lambda page, page_size: list_connector_instances(
        session, organisation_id=_ORG, page=page, page_size=page_size
    )


async def _setup_environment_profiles(session: AsyncSession) -> PageCall:
    for i in range(_COUNT):
        session.add(
            EnvironmentProfile(
                organisation_id=_ORG,
                name=f"profile {i}",
                provider_type="local_docker",
                account_id=_ACCOUNT,
            )
        )
    await session.flush()
    return lambda page, page_size: list_environment_profiles(session, page=page, page_size=page_size)


async def _setup_external_agents(session: AsyncSession) -> PageCall:
    for i in range(_COUNT):
        session.add(ExternalAgent(organisation_id=_ORG, name=f"external {i}"))
    await session.flush()
    return lambda page, page_size: list_external_agents(session, organisation_id=_ORG, page=page, page_size=page_size)


async def _setup_mcp_servers(session: AsyncSession) -> PageCall:
    for i in range(_COUNT):
        session.add(McpServer(organisation_id=_ORG, name=f"mcp {i}", transport="http"))
    await session.flush()
    return lambda page, page_size: list_mcp_servers(session, organisation_id=_ORG, page=page, page_size=page_size)


async def _setup_library_primitives(session: AsyncSession) -> PageCall:
    for i in range(_COUNT):
        session.add(
            LibraryPrimitive(
                organisation_id=_ORG,
                source="local",
                primitive_type="agent",
                name=f"primitive {i}",
                slug=f"primitive-{i}",
                author="tester",
                version="1.0.0",
                content_json={},
            )
        )
    await session.flush()
    return lambda page, page_size: list_library_primitives(session, org_id=_ORG, page=page, page_size=page_size)


async def _setup_model_backends(session: AsyncSession) -> PageCall:
    for i in range(_COUNT):
        session.add(
            ModelBackend(
                organisation_id=_ORG,
                name=f"backend {i}",
                display_name=f"Backend {i}",
                provider="openai",
                model_id="gpt-4o",
                credentials_ciphertext=b"ciphertext",
                account_id=_ACCOUNT,
            )
        )
    await session.flush()
    return lambda page, page_size: list_model_backends(session, org_id=_ORG, page=page, page_size=page_size)


async def _setup_pipeline_references(session: AsyncSession) -> PageCall:
    backend_id = uuid.uuid4()
    for i in range(_COUNT):
        session.add(
            Pipeline(
                organisation_id=_ORG,
                name=f"pipeline {i}",
                account_id=_ACCOUNT,
                graph_nodes_json=[{"id": "n1", "model_backend_id": str(backend_id)}],
            )
        )
    await session.flush()
    return lambda page, page_size: list_pipeline_references_for_backend(
        session, org_id=_ORG, backend_id=backend_id, page=page, page_size=page_size
    )


async def _setup_node_categories(session: AsyncSession) -> PageCall:
    for i in range(_COUNT):
        session.add(
            NodeCategory(
                organisation_id=_ORG,
                name=f"category {i}",
                account_id=_ACCOUNT,
            )
        )
    await session.flush()
    return lambda page, page_size: list_node_categories(session, org_id=_ORG, page=page, page_size=page_size)


async def _setup_ratings(session: AsyncSession) -> PageCall:
    primitive_id = uuid.uuid4()
    for _ in range(_COUNT):
        session.add(
            PrimitiveRating(
                organisation_id=_ORG,
                primitive_id=primitive_id,
                thumbs_up=True,
            )
        )
    await session.flush()
    return lambda page, page_size: list_ratings_for_primitive(session, primitive_id, page=page, page_size=page_size)


async def _setup_abuse_reports(session: AsyncSession) -> PageCall:
    for _ in range(_COUNT):
        session.add(
            PrimitiveAbuseReport(
                organisation_id=_ORG,
                primitive_id=uuid.uuid4(),
                reason="Off-topic",
            )
        )
    await session.flush()
    return lambda page, page_size: list_abuse_reports(session, _ORG, page=page, page_size=page_size)


async def _setup_publishers(session: AsyncSession) -> PageCall:
    for i in range(_COUNT):
        session.add(
            Publisher(
                organisation_id=_ORG,
                name=f"publisher {i}",
                public_key_hex=f"ab{i:02d}",
            )
        )
    await session.flush()
    return lambda page, page_size: list_publishers(session, org_id=_ORG, page=page, page_size=page_size)


async def _setup_views(session: AsyncSession) -> PageCall:
    for i in range(_COUNT):
        session.add(
            SavedView(
                organisation_id=_ORG,
                name=f"view {i}",
                view_type="run_list",
                account_id=_ACCOUNT,
            )
        )
    await session.flush()
    return lambda page, page_size: list_views(session, page=page, page_size=page_size)


async def _setup_templates(session: AsyncSession) -> PageCall:
    for i in range(_COUNT):
        session.add(
            LibraryPrimitive(
                organisation_id=_ORG,
                source="local",
                primitive_type="pipeline_template",
                name=f"template {i}",
                slug=f"template-{i}",
                author="tester",
                version="1.0.0",
                content_json={},
            )
        )
    await session.flush()
    return lambda page, page_size: list_templates(session, page=page, page_size=page_size)


async def _setup_team_members(session: AsyncSession) -> PageCall:
    team_id = uuid.uuid4()
    for _ in range(_COUNT):
        session.add(
            TeamMembership(
                organisation_id=_ORG,
                team_id=team_id,
                account_id=uuid.uuid4(),
            )
        )
    await session.flush()
    return lambda page, page_size: list_team_members(session, team_id, page=page, page_size=page_size)


async def _setup_teams(session: AsyncSession) -> PageCall:
    for i in range(_COUNT):
        session.add(
            Team(
                organisation_id=_ORG,
                name=f"team {i}",
                account_id=_ACCOUNT,
            )
        )
    await session.flush()
    return lambda page, page_size: list_teams(session, _ORG, page=page, page_size=page_size)


async def _setup_schema_versions(session: AsyncSession) -> PageCall:
    schema_id = uuid.uuid4()
    session.add(
        Schema(
            organisation_id=_ORG,
            name="parent schema",
            account_id=_ACCOUNT,
            id=schema_id,
        )
    )
    for i in range(_COUNT):
        session.add(
            SchemaVersion(
                organisation_id=_ORG,
                schema_id=schema_id,
                version=f"1.0.{i}",
                version_number=i,
                definition_json={},
                account_id=_ACCOUNT,
            )
        )
    await session.flush()
    return lambda page, page_size: list_schema_versions(session, schema_id, page=page, page_size=page_size)


async def _setup_runs(session: AsyncSession) -> PageCall:
    pipeline = Pipeline(
        organisation_id=_ORG,
        name="run pipeline",
        account_id=_ACCOUNT,
    )
    session.add(pipeline)
    await session.flush()
    for i in range(_COUNT):
        session.add(
            Run(
                organisation_id=_ORG,
                pipeline_id=pipeline.id,
                snapshot_id=uuid.uuid4(),
                trigger_type="manual",
                run_number=i + 1,
                input_hash="a" * 64,
                langgraph_thread_id=f"thread-{i}",
            )
        )
    await session.flush()
    return lambda page, page_size: list_runs(session, organisation_id=_ORG, page=page, page_size=page_size)


_CASES: list[tuple[str, SetupFn]] = [
    ("agent", _setup_agents),
    ("composite_template", _setup_composite_templates),
    ("connector_instance", _setup_connector_instances),
    ("environment_profile", _setup_environment_profiles),
    ("external_agent", _setup_external_agents),
    ("mcp_server", _setup_mcp_servers),
    ("library_primitive", _setup_library_primitives),
    ("model_backend", _setup_model_backends),
    ("pipeline_references", _setup_pipeline_references),
    ("node_category", _setup_node_categories),
    ("rating_for_primitive", _setup_ratings),
    ("abuse_report", _setup_abuse_reports),
    ("publisher", _setup_publishers),
    ("view", _setup_views),
    ("template", _setup_templates),
    ("team_membership", _setup_team_members),
    ("team", _setup_teams),
    ("schema_version", _setup_schema_versions),
    ("run", _setup_runs),
]


@pytest.mark.parametrize("setup", [case[1] for case in _CASES], ids=[case[0] for case in _CASES])
async def test_first_offset_page_reports_has_more(session: AsyncSession, setup: SetupFn) -> None:
    call = await setup(session)

    page = await call(1, _PAGE_SIZE)

    assert page.total == _COUNT
    assert len(page.items) == _PAGE_SIZE
    assert page.has_more is True


@pytest.mark.parametrize("setup", [case[1] for case in _CASES], ids=[case[0] for case in _CASES])
async def test_final_offset_page_reports_no_more(session: AsyncSession, setup: SetupFn) -> None:
    call = await setup(session)

    page = await call(_FINAL_PAGE, _PAGE_SIZE)

    assert page.total == _COUNT
    assert len(page.items) == _COUNT - _PAGE_SIZE
    assert page.has_more is False
