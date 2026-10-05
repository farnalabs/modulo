"""FAR-1141 / ADR-042: run-level execution origin for dispatched runs.

Two layers, both exercised against the REAL code (no mock of the thing under
test):

* :func:`graph_contains_dispatch` — the pure predicate behind
  ``runs.execution_origin = 'dispatched'``, including its fail-safe arms (an
  unreadable graph must never be claimed as dispatched).
* ``create_run`` — stamps ``EXECUTION_ORIGIN_DISPATCHED`` when the run's
  FROZEN snapshot graph carries a ``dispatch`` node and leaves the column NULL
  otherwise, then persists the value. The snapshot is the run's execution
  source, so the classification reads it too — never the live pipeline row.

The DB fixture mirrors ``test_journey_create_run.py``: an in-memory SQLite
database with the real ``create_run`` path — no hand-rolled session double.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import cast

import pytest
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.run import create_run, graph_contains_dispatch
from modulo.db.models.base import Base
from modulo.db.models.eval import Eval
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.journey import Journey
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import EXECUTION_ORIGIN_DISPATCHED, Run
from modulo.db.models.team import Team
from modulo.db.models.variant_group import VariantGroup

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_SNAPSHOT = uuid.UUID("00000000-0000-0000-0000-0000000000b1")

_AGENT_GRAPH: dict[str, object] = {
    "nodes": [{"id": "n1", "node_type": "agent"}],
    "edges": [],
}
_DISPATCH_GRAPH: dict[str, object] = {
    "nodes": [
        {"id": "n1", "node_type": "agent"},
        {"id": "n2", "node_type": "dispatch"},
    ],
    "edges": [{"source": "n1", "target": "n2"}],
}

_TABLES: list[Table] = cast(
    list[Table],
    [
        Organisation.__table__,
        Pipeline.__table__,
        Team.__table__,
        Run.__table__,
        PipelineSnapshot.__table__,
        Journey.__table__,
        VariantGroup.__table__,
        EvalDefinition.__table__,
        Eval.__table__,
    ],
)


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


async def _seed_org(session: AsyncSession) -> None:
    session.add(Organisation(id=_ORG, name="test org", slug=f"test-{_ORG}"))
    await session.flush()


async def _seed_snapshot(
    session: AsyncSession, snapshot_id: uuid.UUID, graph_json: dict[str, object], *, version: int = 1
) -> None:
    session.add(
        PipelineSnapshot(
            id=snapshot_id,
            organisation_id=_ORG,
            pipeline_id=_PIPELINE,
            snapshot_version=version,
            graph_json=graph_json,
            connector_bindings_json=[],
            schema_pins_json=[],
            prompt_pins_json=[],
            model_backend_pins_json=[],
        )
    )
    await session.flush()


async def _create_run(session: AsyncSession, *, snapshot_id: uuid.UUID = _SNAPSHOT) -> Run:
    return await create_run(
        session,
        org_id=_ORG,
        pipeline_id=_PIPELINE,
        snapshot_id=snapshot_id,
        trigger_type="manual",
        input_payload={},
    )


async def _stored_origin(session: AsyncSession, run_id: uuid.UUID) -> str | None:
    return (await session.execute(select(Run.execution_origin).where(Run.id == run_id))).scalar_one()


# ---------------------------------------------------------------------------
# The pure predicate
# ---------------------------------------------------------------------------


class TestGraphContainsDispatch:
    def test_graph_with_dispatch_node_is_true(self) -> None:
        assert graph_contains_dispatch(_DISPATCH_GRAPH) is True

    def test_graph_without_dispatch_node_is_false(self) -> None:
        assert graph_contains_dispatch(_AGENT_GRAPH) is False

    def test_none_graph_fails_safe_to_false(self) -> None:
        assert graph_contains_dispatch(None) is False

    def test_empty_graph_fails_safe_to_false(self) -> None:
        assert graph_contains_dispatch({}) is False

    def test_empty_nodes_fails_safe_to_false(self) -> None:
        assert graph_contains_dispatch({"nodes": []}) is False

    def test_non_dict_graph_fails_safe_to_false(self) -> None:
        assert graph_contains_dispatch("not-a-graph") is False
        assert graph_contains_dispatch(["nodes"]) is False

    def test_missing_or_non_list_nodes_fails_safe_to_false(self) -> None:
        assert graph_contains_dispatch({"nodes": {"id": "n1"}}) is False

    def test_non_dict_node_is_ignored(self) -> None:
        assert graph_contains_dispatch({"nodes": ["dispatch", {"id": "n1", "node_type": "agent"}]}) is False
        assert graph_contains_dispatch({"nodes": [{"node_type": "dispatch"}]}) is True

    def test_a_dispatch_node_alone_is_enough(self) -> None:
        assert graph_contains_dispatch({"nodes": [{"id": "only", "node_type": "dispatch"}], "edges": []}) is True


# ---------------------------------------------------------------------------
# create_run stamps and persists it
# ---------------------------------------------------------------------------


class TestCreateRunExecutionOrigin:
    async def test_dispatch_graph_stamps_dispatched(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_snapshot(session, _SNAPSHOT, _DISPATCH_GRAPH)

        run = await _create_run(session)

        assert run.execution_origin == EXECUTION_ORIGIN_DISPATCHED
        # the wire/DB value the API and the facts reader will see
        assert EXECUTION_ORIGIN_DISPATCHED == "dispatched"

    async def test_graph_without_dispatch_leaves_origin_null(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_snapshot(session, _SNAPSHOT, _AGENT_GRAPH)

        run = await _create_run(session)

        assert run.execution_origin is None

    async def test_missing_snapshot_row_leaves_origin_null(self, session: AsyncSession) -> None:
        """Fail-safe: an unreadable graph is never claimed as dispatched."""
        await _seed_org(session)

        run = await _create_run(session, snapshot_id=uuid.uuid4())

        assert run.execution_origin is None

    async def test_dispatched_origin_is_persisted(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_snapshot(session, _SNAPSHOT, _DISPATCH_GRAPH)
        run = await _create_run(session)
        await session.flush()

        assert await _stored_origin(session, run.id) == "dispatched"

    async def test_null_origin_is_persisted(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_snapshot(session, _SNAPSHOT, _AGENT_GRAPH)
        run = await _create_run(session)
        await session.flush()

        assert await _stored_origin(session, run.id) is None

    async def test_each_run_is_classified_from_its_own_snapshot(self, session: AsyncSession) -> None:
        """Two runs of the same pipeline with different frozen snapshots get
        different origins — the classification follows the run's snapshot, not
        a pipeline-level property."""
        await _seed_org(session)
        dispatch_snapshot = uuid.uuid4()
        agent_snapshot = uuid.uuid4()
        await _seed_snapshot(session, dispatch_snapshot, _DISPATCH_GRAPH, version=1)
        await _seed_snapshot(session, agent_snapshot, _AGENT_GRAPH, version=2)

        dispatched_run = await _create_run(session, snapshot_id=dispatch_snapshot)
        executed_run = await _create_run(session, snapshot_id=agent_snapshot)

        assert dispatched_run.execution_origin == "dispatched"
        assert executed_run.execution_origin is None

    async def test_live_pipeline_nodes_do_not_drive_the_classification(self, session: AsyncSession) -> None:
        """The run's snapshot is the source (ADR-042): a dispatch node that
        only exists on the LIVE pipeline row, with a dispatch-free frozen
        snapshot, must NOT stamp ``dispatched``."""
        await _seed_org(session)
        session.add(
            Pipeline(
                id=_PIPELINE,
                organisation_id=_ORG,
                name="pipeline",
                account_id=_ORG,
                visibility="org",
                graph_nodes_json=[{"id": "live", "node_type": "dispatch"}],
            )
        )
        await _seed_snapshot(session, _SNAPSHOT, _AGENT_GRAPH)

        run = await _create_run(session)

        assert run.snapshot_id == _SNAPSHOT
        assert run.execution_origin is None
