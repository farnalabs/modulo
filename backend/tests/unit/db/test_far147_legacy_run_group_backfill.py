"""FAR-147: legacy run-group classification backfill + legacy agent verdict tests.

The backfill tests call the PRODUCTION code in
``modulo.db.migrations.versions.0348_far147_legacy_run_group_backfill`` (the
portable ``_backfill_non_postgres`` traversal and ``classify_legacy_child``),
not a reimplementation. The PostgreSQL recursive CTE is covered by the
testcontainers integration module
``tests/integration/db/test_migration_0348_far147_run_group_backfill.py``.

The verdict tests exercise the production projection reader
(``read_run_outputs_map``), its ``_load_legacy_outputs`` wrapper and the
fail-open/tenant-integrity contract of ``_do_get_run_outputs``.
"""

import importlib.util
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import Table, create_engine, event, select, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.run_node_outputs import OutputsRlsMismatch, read_run_outputs_map
from modulo.db.models.base import Base
from modulo.db.models.eval import Eval
from modulo.db.models.journey import Journey
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.models.run_node_outputs import FINAL_ATTEMPT_KEY, META_NODE_ID, RunNodeOutput
from modulo.db.models.system_config import SystemConfig
from modulo.db.models.team import Team
from modulo.db.models.variant_group import VariantGroup

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
_OTHER_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000f9")
_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000f2")
_SNAPSHOT = uuid.UUID("00000000-0000-0000-0000-0000000000f3")
_MIGRATION_PATH = (
    Path(__file__).parents[3] / "src/modulo/db/migrations/versions/0348_far147_legacy_run_group_backfill.py"
)


def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location("migration_0348", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_GRAPH: dict[str, object] = {
    "nodes": [{"id": "n1", "node_type": "agent"}],
    "edges": [],
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
        Eval.__table__,
        SystemConfig.__table__,
        RunNodeOutput.__table__,
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
        s.info["org_id"] = _ORG
        yield s


async def _seed_org(session: AsyncSession) -> None:
    session.add(Organisation(id=_ORG, name="test org", slug=f"test-{_ORG}"))
    session.add(Organisation(id=_OTHER_ORG, name="other org", slug=f"test-{_OTHER_ORG}"))
    await session.flush()


async def _seed_pipeline(session: AsyncSession) -> None:
    session.add(Pipeline(id=_PIPELINE, organisation_id=_ORG, name="test pipeline", account_id=_ORG))
    await session.flush()


async def _seed_snapshot(session: AsyncSession) -> None:
    session.add(
        PipelineSnapshot(
            id=_SNAPSHOT,
            organisation_id=_ORG,
            pipeline_id=_PIPELINE,
            snapshot_version=1,
            graph_json=_GRAPH,
            connector_bindings_json=[],
            schema_pins_json=[],
            prompt_pins_json=[],
            model_backend_pins_json=[],
        )
    )
    await session.flush()


async def _insert_legacy_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    parent_run_id: uuid.UUID | None = None,
    langgraph_thread_id: str | None = None,
    trigger_type: str = "rerun",
    run_group_id: uuid.UUID | None = None,
    run_group_kind: str | None = None,
) -> Run:
    """Insert a legacy-style run row (group columns NULL unless overridden)."""
    if langgraph_thread_id is None:
        langgraph_thread_id = f"{_ORG}:{run_id}"
    result = await session.execute(select(Run.run_number).order_by(Run.run_number.desc()).limit(1))
    last_run_number = result.scalar_one_or_none() or 0
    run = Run(
        id=run_id,
        organisation_id=_ORG,
        pipeline_id=_PIPELINE,
        snapshot_id=_SNAPSHOT,
        trigger_type=trigger_type,
        input_hash="abc123",
        input_payload={},
        langgraph_thread_id=langgraph_thread_id,
        parent_run_id=parent_run_id,
        run_group_id=run_group_id,
        run_group_kind=run_group_kind,
        run_number=int(last_run_number) + 1,
        status="complete",
    )
    session.add(run)
    await session.flush()
    return run


async def _run_production_backfill(session: AsyncSession) -> None:
    """Invoke the production portable traversal on the session's connection."""
    migration = _load_migration()

    def _run(sync_session: Any) -> None:
        migration._backfill_non_postgres(sync_session.connection())

    await session.run_sync(_run)


async def _grouping_by_id(session: AsyncSession) -> dict[uuid.UUID, tuple[uuid.UUID | None, str | None]]:
    rows = await session.execute(select(Run.id, Run.run_group_id, Run.run_group_kind))
    return {row[0]: (row[1], row[2]) for row in rows.all()}


# ---------------------------------------------------------------------------
# Production fallback traversal (minimal SQLite schema — no partial unique
# indexes, so a NULL-kind resume candidate can exist, exactly like the legacy
# data the backfill targets).
# ---------------------------------------------------------------------------


def _create_legacy_runs_table(connection: Any) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE runs (
                id TEXT PRIMARY KEY,
                parent_run_id TEXT,
                langgraph_thread_id TEXT,
                run_group_id TEXT,
                run_group_kind TEXT,
                trigger_type TEXT NOT NULL
            )
            """
        )
    )


def _insert_legacy_chain_row(
    connection: Any,
    *,
    run_id: str,
    parent_run_id: str | None,
    thread_id: str,
    trigger_type: str = "rerun",
    run_group_id: str | None = None,
    run_group_kind: str | None = None,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO runs (id, parent_run_id, langgraph_thread_id, run_group_id, run_group_kind, trigger_type)
            VALUES (:id, :parent_run_id, :thread_id, :run_group_id, :run_group_kind, :trigger_type)
            """
        ),
        {
            "id": run_id,
            "parent_run_id": parent_run_id,
            "thread_id": thread_id,
            "run_group_id": run_group_id,
            "run_group_kind": run_group_kind,
            "trigger_type": trigger_type,
        },
    )


def _grouping_rows(connection: Any) -> dict[str, tuple[str | None, str | None]]:
    rows = connection.execute(text("SELECT id, run_group_id, run_group_kind FROM runs")).all()
    return {row[0]: (row[1], row[2]) for row in rows}


class TestLegacyRunGroupBackfillLogic:
    """The production portable traversal against a minimal legacy ``runs`` table."""

    def test_resume_child_shares_immediate_parent_thread(self) -> None:
        migration = _load_migration()
        engine = create_engine("sqlite://")
        root_id, child_id = (str(uuid.uuid4()) for _ in range(2))
        with engine.begin() as connection:
            _create_legacy_runs_table(connection)
            _insert_legacy_chain_row(connection, run_id=root_id, parent_run_id=None, thread_id="root-thread")
            _insert_legacy_chain_row(connection, run_id=child_id, parent_run_id=root_id, thread_id="root-thread")
            migration._backfill_non_postgres(connection)
            grouped = _grouping_rows(connection)
        engine.dispose()

        assert grouped[root_id] == (None, None)
        assert grouped[child_id] == (root_id, "resume")

    def test_rerun_child_on_fresh_thread_is_retry(self) -> None:
        migration = _load_migration()
        engine = create_engine("sqlite://")
        root_id, child_id = (str(uuid.uuid4()) for _ in range(2))
        with engine.begin() as connection:
            _create_legacy_runs_table(connection)
            _insert_legacy_chain_row(connection, run_id=root_id, parent_run_id=None, thread_id="root-thread")
            _insert_legacy_chain_row(connection, run_id=child_id, parent_run_id=root_id, thread_id="fresh-thread")
            migration._backfill_non_postgres(connection)
            grouped = _grouping_rows(connection)
        engine.dispose()

        assert grouped[child_id] == (root_id, "retry")

    def test_grandchild_classifies_against_immediate_parent_and_top_root(self) -> None:
        migration = _load_migration()
        engine = create_engine("sqlite://")
        root_id, child_id, grandchild_id = (str(uuid.uuid4()) for _ in range(3))
        with engine.begin() as connection:
            _create_legacy_runs_table(connection)
            _insert_legacy_chain_row(connection, run_id=root_id, parent_run_id=None, thread_id="root-thread")
            _insert_legacy_chain_row(connection, run_id=child_id, parent_run_id=root_id, thread_id="retry-thread")
            _insert_legacy_chain_row(
                connection,
                run_id=grandchild_id,
                parent_run_id=child_id,
                thread_id="retry-thread",
            )
            migration._backfill_non_postgres(connection)
            grouped = _grouping_rows(connection)
        engine.dispose()

        assert grouped[child_id] == (root_id, "retry")
        assert grouped[grandchild_id] == (root_id, "resume")

    def test_correction_and_agent_signal_children_are_never_classified(self) -> None:
        migration = _load_migration()
        engine = create_engine("sqlite://")
        root_id, correction_id, signal_id = (str(uuid.uuid4()) for _ in range(3))
        with engine.begin() as connection:
            _create_legacy_runs_table(connection)
            _insert_legacy_chain_row(connection, run_id=root_id, parent_run_id=None, thread_id="root-thread")
            _insert_legacy_chain_row(
                connection,
                run_id=correction_id,
                parent_run_id=root_id,
                thread_id="correction-thread",
                trigger_type="correction",
            )
            _insert_legacy_chain_row(
                connection,
                run_id=signal_id,
                parent_run_id=root_id,
                thread_id="signal-thread",
                trigger_type="agent_signal",
            )
            migration._backfill_non_postgres(connection)
            grouped = _grouping_rows(connection)
        engine.dispose()

        assert grouped[correction_id] == (None, None)
        assert grouped[signal_id] == (None, None)

    def test_existing_ancestor_group_root_wins_over_the_chain_top(self) -> None:
        migration = _load_migration()
        engine = create_engine("sqlite://")
        top_id, existing_group_root, parent_id, child_id = (str(uuid.uuid4()) for _ in range(4))
        with engine.begin() as connection:
            _create_legacy_runs_table(connection)
            _insert_legacy_chain_row(connection, run_id=top_id, parent_run_id=None, thread_id="top-thread")
            _insert_legacy_chain_row(
                connection,
                run_id=existing_group_root,
                parent_run_id=None,
                thread_id="group-thread",
            )
            _insert_legacy_chain_row(
                connection,
                run_id=parent_id,
                parent_run_id=top_id,
                thread_id="parent-thread",
                run_group_id=existing_group_root,
                run_group_kind="retry",
            )
            _insert_legacy_chain_row(connection, run_id=child_id, parent_run_id=parent_id, thread_id="child-thread")
            migration._backfill_non_postgres(connection)
            grouped = _grouping_rows(connection)
        engine.dispose()

        assert grouped[parent_id] == (existing_group_root, "retry")
        assert grouped[child_id] == (existing_group_root, "retry")

    def test_backfill_is_idempotent(self) -> None:
        migration = _load_migration()
        engine = create_engine("sqlite://")
        root_id, resume_id, retry_id = (str(uuid.uuid4()) for _ in range(3))
        with engine.begin() as connection:
            _create_legacy_runs_table(connection)
            _insert_legacy_chain_row(connection, run_id=root_id, parent_run_id=None, thread_id="root-thread")
            _insert_legacy_chain_row(connection, run_id=resume_id, parent_run_id=root_id, thread_id="root-thread")
            _insert_legacy_chain_row(connection, run_id=retry_id, parent_run_id=root_id, thread_id="retry-thread")
            migration._backfill_non_postgres(connection)
            first_pass = _grouping_rows(connection)
            migration._backfill_non_postgres(connection)
            second_pass = _grouping_rows(connection)
        engine.dispose()

        assert first_pass[resume_id] == (root_id, "resume")
        assert first_pass[retry_id] == (root_id, "retry")
        assert first_pass == second_pass

    def test_classify_legacy_child_ignores_a_missing_parent_thread(self) -> None:
        migration = _load_migration()

        assert migration.classify_legacy_child(child_thread_id="t", parent_thread_id=None) == "retry"
        assert migration.classify_legacy_child(child_thread_id=None, parent_thread_id=None) == "retry"
        assert migration.classify_legacy_child(child_thread_id="t", parent_thread_id="t") == "resume"


class TestLegacyRunGroupBackfillDB:
    """The production traversal against the real ``runs`` schema (SQLite port).

    The live partial unique index ``uq_runs_thread_non_resume`` forbids a
    NULL-kind row that shares its parent's thread, so a legacy RESUME candidate
    cannot be seeded here; resume classification is covered by the minimal
    legacy-schema tests above and by the PostgreSQL integration test. The rows
    this class seeds are exactly the ones the live schema can hold unclassified
    (rerun children on fresh threads, correction / agent_signal children).
    """

    async def test_backfill_classifies_rerun_child_on_real_schema(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_pipeline(session)
        await _seed_snapshot(session)
        root = await _insert_legacy_run(session, run_id=uuid.uuid4(), trigger_type="manual")
        child = await _insert_legacy_run(
            session,
            run_id=uuid.uuid4(),
            parent_run_id=root.id,
            langgraph_thread_id=f"{_ORG}:{uuid.uuid4()}",
        )

        await _run_production_backfill(session)
        grouped = await _grouping_by_id(session)

        assert grouped[root.id] == (None, None)
        assert grouped[child.id] == (root.id, "retry")

    async def test_backfill_leaves_correction_and_agent_signal_children_ungrouped(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_pipeline(session)
        await _seed_snapshot(session)
        root = await _insert_legacy_run(session, run_id=uuid.uuid4(), trigger_type="manual")
        correction = await _insert_legacy_run(
            session,
            run_id=uuid.uuid4(),
            parent_run_id=root.id,
            langgraph_thread_id=f"{_ORG}:{uuid.uuid4()}",
            trigger_type="correction",
        )
        signal_child = await _insert_legacy_run(
            session,
            run_id=uuid.uuid4(),
            parent_run_id=root.id,
            langgraph_thread_id=f"{_ORG}:{uuid.uuid4()}",
            trigger_type="agent_signal",
        )

        await _run_production_backfill(session)
        grouped = await _grouping_by_id(session)

        assert grouped[correction.id] == (None, None)
        assert grouped[signal_child.id] == (None, None)

    async def test_backfill_is_idempotent_on_real_schema(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_pipeline(session)
        await _seed_snapshot(session)
        root = await _insert_legacy_run(session, run_id=uuid.uuid4(), trigger_type="manual")
        child = await _insert_legacy_run(
            session,
            run_id=uuid.uuid4(),
            parent_run_id=root.id,
            langgraph_thread_id=f"{_ORG}:{uuid.uuid4()}",
        )

        await _run_production_backfill(session)
        first_pass = await _grouping_by_id(session)
        await _run_production_backfill(session)
        second_pass = await _grouping_by_id(session)

        assert first_pass[child.id] == (root.id, "retry")
        assert first_pass == second_pass


def test_downgrade_refuses_destructive_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _load_migration()
    monkeypatch.setattr(migration.op, "get_bind", lambda: pytest.fail("downgrade must not write"))

    with pytest.raises(RuntimeError, match="cannot be downgraded safely"):
        migration.downgrade()


# ---------------------------------------------------------------------------
# Legacy agent verdict extraction (read-only projection)
# ---------------------------------------------------------------------------


class TestLegacyAgentVerdictExtraction:
    """Test the agent-verdict extraction from outputs_json."""

    def test_engine_status_alone_is_not_a_self_reported_verdict(self) -> None:
        from modulo.api.routes.runs import _extract_legacy_agent_verdict

        outputs_json = {
            "node1": {
                "status": "failed",
                "output": {"result": "engine failure envelope"},
            }
        }

        assert _extract_legacy_agent_verdict(outputs_json) is None

    @pytest.mark.parametrize("field", ["agent_status", "agent_outcome"])
    def test_extract_failed_self_reported_verdict(self, field: str) -> None:
        """Extract 'failed' only from explicit agent self-report fields."""
        from modulo.api.routes.runs import _extract_legacy_agent_verdict

        outputs_json = {
            "node1": {
                "status": "failed",
                "output": {
                    field: "failed",
                    "error": "Agent crashed",
                },
            }
        }
        verdict = _extract_legacy_agent_verdict(outputs_json)
        assert verdict == "failed"

    def test_extract_failed_agent_outcome(self) -> None:
        """Extract 'failed' verdict from agent_outcome == 'failed'."""
        from modulo.api.routes.runs import _extract_legacy_agent_verdict

        outputs_json = {
            "node1": {
                "output": {
                    "agent_outcome": "failed",
                    "summary": "Task failed",
                }
            }
        }
        verdict = _extract_legacy_agent_verdict(outputs_json)
        assert verdict == "failed"

    def test_no_verdict_when_success(self) -> None:
        """No verdict extracted when agent_status/outcome is success."""
        from modulo.api.routes.runs import _extract_legacy_agent_verdict

        outputs_json = {
            "node1": {
                "output": {
                    "agent_status": "completed",
                    "agent_outcome": "success",
                }
            }
        }
        verdict = _extract_legacy_agent_verdict(outputs_json)
        assert verdict is None

    def test_no_verdict_for_non_dict_output(self) -> None:
        """No verdict when output is not a dict."""
        from modulo.api.routes.runs import _extract_legacy_agent_verdict

        outputs_json = {
            "node1": {
                "output": "not a dict",
            }
        }
        verdict = _extract_legacy_agent_verdict(outputs_json)
        assert verdict is None

    def test_no_verdict_for_empty_outputs(self) -> None:
        """No verdict for empty or None outputs_json."""
        from modulo.api.routes.runs import _extract_legacy_agent_verdict

        assert _extract_legacy_agent_verdict(None) is None
        assert _extract_legacy_agent_verdict({}) is None
        assert _extract_legacy_agent_verdict({"node1": "not a dict"}) is None

    def test_first_failed_verdict_returned(self) -> None:
        """First failed verdict is returned when multiple nodes have failed."""
        from modulo.api.routes.runs import _extract_legacy_agent_verdict

        outputs_json = {
            "node1": {
                "output": {
                    "agent_status": "completed",
                    "agent_outcome": "success",
                }
            },
            "node2": {
                "output": {
                    "agent_status": "failed",
                    "error": "First failure",
                }
            },
            "node3": {
                "output": {
                    "agent_outcome": "failed",
                    "summary": "Second failure",
                }
            },
        }
        verdict = _extract_legacy_agent_verdict(outputs_json)
        assert verdict == "failed"

    def test_verdict_only_for_failed_not_other_statuses(self) -> None:
        """Only 'failed' status triggers verdict, not other statuses."""
        from modulo.api.routes.runs import _extract_legacy_agent_verdict

        for status in ["completed", "running", "pending", "cancelled"]:
            outputs_json = {
                "node1": {
                    "output": {
                        "agent_status": status,
                    }
                }
            }
            verdict = _extract_legacy_agent_verdict(outputs_json)
            assert verdict is None, f"Status {status} should not produce verdict"

        for outcome in ["success", "partial", "cancelled"]:
            outputs_json = {
                "node1": {
                    "output": {
                        "agent_outcome": outcome,
                    }
                }
            }
            verdict = _extract_legacy_agent_verdict(outputs_json)
            assert verdict is None, f"Outcome {outcome} should not produce verdict"


# ---------------------------------------------------------------------------
# Narrow projection reader + its list-endpoint wrapper
# ---------------------------------------------------------------------------


async def _add_output_row(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    node_id: str,
    outputs_json: Any,
    attempt_key: str = FINAL_ATTEMPT_KEY,
    organisation_id: uuid.UUID = _ORG,
) -> None:
    session.add(
        RunNodeOutput(
            run_id=run_id,
            node_id=node_id,
            attempt_key=attempt_key,
            organisation_id=organisation_id,
            outputs_json=outputs_json,
        )
    )
    await session.flush()


async def test_read_run_outputs_map_projects_only_final_dict_rows(engine: AsyncEngine, session: AsyncSession) -> None:
    await _seed_org(session)
    run_id = uuid.uuid4()
    empty_run_id = uuid.uuid4()
    other_org_run_id = uuid.uuid4()
    await _add_output_row(session, run_id=run_id, node_id="node1", outputs_json={"output": {"agent_status": "failed"}})
    await _add_output_row(session, run_id=run_id, node_id="node2", outputs_json=cast(Any, ["not", "a", "dict"]))
    await _add_output_row(
        session,
        run_id=run_id,
        node_id=META_NODE_ID,
        outputs_json={"empty_outputs": False, "empty_telemetry": False},
    )
    await _add_output_row(
        session,
        run_id=run_id,
        node_id="node3",
        outputs_json={"output": {"agent_status": "failed"}},
        attempt_key=f"run:{run_id}:node:node3:1",
    )
    await _add_output_row(
        session,
        run_id=other_org_run_id,
        node_id="node1",
        outputs_json={"output": {"agent_status": "failed"}},
        organisation_id=_OTHER_ORG,
    )

    statements: list[str] = []

    def _capture(_conn: Any, _cursor: Any, statement: str, _params: Any, _context: Any, _many: bool) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        result = await read_run_outputs_map(
            session,
            run_ids=[run_id, empty_run_id, other_org_run_id],
            organisation_id=_ORG,
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)

    assert result[run_id] == {"node1": {"output": {"agent_status": "failed"}}}
    assert result[empty_run_id] is None
    assert result[other_org_run_id] is None
    assert len(statements) == 1, statements
    assert "outputs_json" in statements[0]
    assert "node_telemetry_json" not in statements[0]
    assert "raw_output_markers" not in statements[0]
    assert "artifacts_json" not in statements[0]


async def test_read_run_outputs_map_returns_empty_for_no_run_ids(engine: AsyncEngine, session: AsyncSession) -> None:
    statements: list[str] = []

    def _capture(_conn: Any, _cursor: Any, statement: str, _params: Any, _context: Any, _many: bool) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        result = await read_run_outputs_map(session, run_ids=[], organisation_id=_ORG)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)

    assert not result
    assert not statements


async def test_load_legacy_outputs_reads_a_whole_page_in_one_query(engine: AsyncEngine, session: AsyncSession) -> None:
    from modulo.api.routes.runs import _load_legacy_outputs

    await _seed_org(session)
    first_run_id = uuid.uuid4()
    second_run_id = uuid.uuid4()
    await _add_output_row(
        session, run_id=first_run_id, node_id="node1", outputs_json={"output": {"agent_outcome": "failed"}}
    )

    statements: list[str] = []

    def _capture(_conn: Any, _cursor: Any, statement: str, _params: Any, _context: Any, _many: bool) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        result = await _load_legacy_outputs(
            session,
            [SimpleNamespace(id=first_run_id), SimpleNamespace(id=second_run_id)],
            _ORG,
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)

    assert result == {
        first_run_id: {"node1": {"output": {"agent_outcome": "failed"}}},
        second_run_id: None,
    }
    assert len(statements) == 1, statements


# ---------------------------------------------------------------------------
# _do_get_run_outputs: fail-open on transient DB errors only
# ---------------------------------------------------------------------------


class _FakeAsyncContext:
    def __init__(self, value: Any = None) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *_exc_info: object) -> bool:
        return False


class _FakeOutputsSession:
    def begin(self) -> _FakeAsyncContext:
        return _FakeAsyncContext()


def _outputs_factory(session: _FakeOutputsSession) -> Any:
    def factory() -> _FakeAsyncContext:
        return _FakeAsyncContext(session)

    return factory


def _patch_outputs_deps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    read_result: Any = None,
    read_error: BaseException | None = None,
) -> tuple[AsyncMock, AsyncMock]:
    import modulo.api.routes.runs as runs_routes

    read_mock = AsyncMock(return_value=read_result)
    if read_error is not None:
        read_mock.side_effect = read_error
    rls_mock = AsyncMock()
    monkeypatch.setattr(runs_routes, "read_run_outputs_map", read_mock)
    monkeypatch.setattr(runs_routes, "set_rls_org", rls_mock)
    return read_mock, rls_mock


async def test_do_get_run_outputs_returns_the_run_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.api.routes.runs as runs_routes

    run_id = uuid.uuid4()
    session = _FakeOutputsSession()
    projection = {"node1": {"output": {"agent_status": "failed"}}}
    read_mock, rls_mock = _patch_outputs_deps(monkeypatch, read_result={run_id: projection})

    result = await runs_routes._do_get_run_outputs(
        _outputs_factory(session),
        SimpleNamespace(organisation_id=_ORG),
        run_id,
    )

    assert result == projection
    assert rls_mock.await_args.args == (session, _ORG)
    assert read_mock.await_args.kwargs == {"run_ids": [run_id], "organisation_id": _ORG}


async def test_do_get_run_outputs_returns_none_when_the_run_has_no_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import modulo.api.routes.runs as runs_routes

    run_id = uuid.uuid4()
    _patch_outputs_deps(monkeypatch, read_result={run_id: None})

    result = await runs_routes._do_get_run_outputs(
        _outputs_factory(_FakeOutputsSession()),
        SimpleNamespace(organisation_id=_ORG),
        run_id,
    )

    assert result is None


async def test_do_get_run_outputs_degrades_to_none_on_transient_db_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import modulo.api.routes.runs as runs_routes

    run_id = uuid.uuid4()
    _patch_outputs_deps(monkeypatch, read_error=OperationalError("SELECT 1", {}, RuntimeError("connection lost")))

    result = await runs_routes._do_get_run_outputs(
        _outputs_factory(_FakeOutputsSession()),
        SimpleNamespace(organisation_id=_ORG),
        run_id,
    )

    assert result is None


async def test_do_get_run_outputs_reraises_cancelled_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    import modulo.api.routes.runs as runs_routes

    _patch_outputs_deps(monkeypatch, read_error=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await runs_routes._do_get_run_outputs(
            _outputs_factory(_FakeOutputsSession()),
            SimpleNamespace(organisation_id=_ORG),
            uuid.uuid4(),
        )


async def test_do_get_run_outputs_reraises_programming_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.api.routes.runs as runs_routes

    _patch_outputs_deps(monkeypatch, read_error=ProgrammingError("SELECT 1", {}, RuntimeError("bad query")))

    with pytest.raises(ProgrammingError):
        await runs_routes._do_get_run_outputs(
            _outputs_factory(_FakeOutputsSession()),
            SimpleNamespace(organisation_id=_ORG),
            uuid.uuid4(),
        )


async def test_do_get_run_outputs_reraises_rls_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.api.routes.runs as runs_routes

    _patch_outputs_deps(monkeypatch, read_error=OutputsRlsMismatch("session org != row org"))

    with pytest.raises(OutputsRlsMismatch):
        await runs_routes._do_get_run_outputs(
            _outputs_factory(_FakeOutputsSession()),
            SimpleNamespace(organisation_id=_ORG),
            uuid.uuid4(),
        )
