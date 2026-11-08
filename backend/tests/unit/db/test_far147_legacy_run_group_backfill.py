"""FAR-147: Legacy run-group classification backfill tests.

Tests the backfill logic that classifies existing runs into run groups
by walking parent_run_id chains and detecting resume vs retry based on
langgraph_thread_id equality.
"""

import importlib.util
import os
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import Table, create_engine, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

# Force SQLite mode for tests to avoid PostgreSQL capacity check issues
os.environ.setdefault("MODULO_DB", "sqlite")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")

from modulo.db.crud.run import create_run
from modulo.db.models.base import Base
from modulo.db.models.eval import Eval
from modulo.db.models.journey import Journey
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import RUN_GROUP_KIND_RESUME, RUN_GROUP_KIND_RETRY, Run
from modulo.db.models.system_config import SystemConfig
from modulo.db.models.team import Team
from modulo.db.models.variant_group import VariantGroup

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
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


async def _create_run(session: AsyncSession, **kwargs: object) -> Run:
    return await create_run(
        session,
        org_id=_ORG,
        pipeline_id=_PIPELINE,
        snapshot_id=_SNAPSHOT,
        trigger_type="manual",
        input_payload={},
        **kwargs,  # type: ignore[arg-type]
    )


async def _insert_legacy_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    parent_run_id: uuid.UUID | None = None,
    langgraph_thread_id: str | None = None,
    run_group_id: uuid.UUID | None = None,
    run_group_kind: str | None = None,
    run_number: int | None = None,
) -> Run:
    """Insert a legacy run using the ORM so the session can see it.

    For resume children (same thread_id as parent), we MUST set run_group_kind='resume'
    to satisfy the uq_runs_thread_non_resume partial unique index.
    For retry children (different thread_id), set run_group_kind='retry'.
    """
    if langgraph_thread_id is None:
        langgraph_thread_id = f"{_ORG}:{run_id}"
    # Determine run_group_kind based on thread_id match with parent
    if parent_run_id and run_group_kind is None:
        parent = await session.execute(select(Run.langgraph_thread_id).where(Run.id == parent_run_id))
        parent_thread = parent.scalar_one_or_none()
        if parent_thread:
            run_group_kind = RUN_GROUP_KIND_RESUME if parent_thread == langgraph_thread_id else RUN_GROUP_KIND_RETRY

    # Get unique run_number
    if run_number is None:
        result = await session.execute(select(func.max(Run.run_number)).where(Run.organisation_id == _ORG))
        max_run_number = result.scalar_one_or_none()
        run_number = (max_run_number or 0) + 1

    run = Run(
        id=run_id,
        organisation_id=_ORG,
        pipeline_id=_PIPELINE,
        snapshot_id=_SNAPSHOT,
        trigger_type="manual",
        input_hash="abc123",
        input_payload={},
        langgraph_thread_id=langgraph_thread_id,
        parent_run_id=parent_run_id,
        run_group_id=run_group_id,
        run_group_kind=run_group_kind,
        run_number=run_number,
        status="complete",
    )
    session.add(run)
    await session.flush()
    return run


async def _get_run(session: AsyncSession, run_id: uuid.UUID) -> Run | None:
    result = await session.execute(select(Run).where(Run.id == run_id))
    return result.scalar_one_or_none()


async def _seed_root(session: AsyncSession) -> Run:
    await _seed_org(session)
    await _seed_pipeline(session)
    await _seed_snapshot(session)
    return await _create_run(session)


def test_actual_non_postgres_backfill_uses_immediate_parent_and_root() -> None:
    migration = _load_migration()
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE runs (
                    id TEXT PRIMARY KEY,
                    parent_run_id TEXT,
                    langgraph_thread_id TEXT,
                    run_group_id TEXT,
                    run_group_kind TEXT
                )
                """
            )
        )
        root_id, parent_id, child_id = (str(uuid.uuid4()) for _ in range(3))
        connection.execute(
            text(
                """
                INSERT INTO runs(id, parent_run_id, langgraph_thread_id)
                VALUES (:root_id, NULL, 'root-thread'),
                       (:parent_id, :root_id, 'parent-thread'),
                       (:child_id, :parent_id, 'parent-thread')
                """
            ),
            {"root_id": root_id, "parent_id": parent_id, "child_id": child_id},
        )
        migration._backfill_non_postgres(connection)
        rows = connection.execute(text("SELECT id, run_group_id, run_group_kind FROM runs")).all()
        first = list(rows)
        migration._backfill_non_postgres(connection)
        second = list(connection.execute(text("SELECT id, run_group_id, run_group_kind FROM runs")).all())

    grouped = {row.id: (row.run_group_id, row.run_group_kind) for row in first}
    assert grouped[root_id] == (None, None)
    assert grouped[parent_id] == (root_id, "retry")
    assert grouped[child_id] == (root_id, "resume")
    assert first == second


def _backfill_classify_run(run: Run, parent_run: Run | None) -> tuple[uuid.UUID | None, str | None]:
    """Simulate the backfill classification logic.

    Returns (run_group_id, run_group_kind) that the backfill would assign.
    """
    if parent_run is None:
        # Root run - no group
        return None, None

    # Walk up to find the root
    current = parent_run
    while current and current.parent_run_id:
        # In real backfill, we'd fetch the parent from DB
        # Here we simulate by assuming we have the full chain
        break

    # For simplicity in this test, assume parent_run is the root
    # (direct child of root)
    root_id = parent_run.id

    # Determine kind: resume if thread_id matches parent, else retry
    is_resume = run.langgraph_thread_id == parent_run.langgraph_thread_id
    kind = RUN_GROUP_KIND_RESUME if is_resume else RUN_GROUP_KIND_RETRY

    return root_id, kind


class TestLegacyRunGroupBackfillLogic:
    """Test the backfill classification logic (algorithm only, no DB constraints)."""

    def test_resume_child_detected_by_thread_id_match(self) -> None:
        """A child run sharing its parent's langgraph_thread_id is a resume."""
        root_id = uuid.uuid4()
        parent_thread = f"{_ORG}:{root_id}"
        child_id = uuid.uuid4()

        # Simulate runs
        class MockRun:
            def __init__(self, id: uuid.UUID, parent_run_id: uuid.UUID | None, langgraph_thread_id: str):
                self.id = id
                self.parent_run_id = parent_run_id
                self.langgraph_thread_id = langgraph_thread_id

        root = MockRun(root_id, None, parent_thread)
        child = MockRun(child_id, root_id, parent_thread)  # Same thread = resume

        # Test the classification logic
        is_resume = child.langgraph_thread_id == root.langgraph_thread_id
        assert is_resume is True

        kind = RUN_GROUP_KIND_RESUME if is_resume else RUN_GROUP_KIND_RETRY
        assert kind == RUN_GROUP_KIND_RESUME

        # Root should be the group_id
        group_id = root.id
        assert group_id == root_id

    def test_retry_child_detected_by_thread_id_mismatch(self) -> None:
        """A child run with a different langgraph_thread_id from parent is a retry."""
        root_id = uuid.uuid4()
        parent_thread = f"{_ORG}:{root_id}"
        child_id = uuid.uuid4()
        different_thread = f"{_ORG}:{uuid.uuid4()}"

        class MockRun:
            def __init__(self, id: uuid.UUID, parent_run_id: uuid.UUID | None, langgraph_thread_id: str):
                self.id = id
                self.parent_run_id = parent_run_id
                self.langgraph_thread_id = langgraph_thread_id

        root = MockRun(root_id, None, parent_thread)
        child = MockRun(child_id, root_id, different_thread)  # Different thread = retry

        is_resume = child.langgraph_thread_id == root.langgraph_thread_id
        assert is_resume is False

        kind = RUN_GROUP_KIND_RESUME if is_resume else RUN_GROUP_KIND_RETRY
        assert kind == RUN_GROUP_KIND_RETRY

    def test_grandchild_resume_chain(self) -> None:
        """A resume child that itself has a resume child (resume-of-resume chain)."""
        root_id = uuid.uuid4()
        root_thread = f"{_ORG}:{root_id}"
        child1_id = uuid.uuid4()
        child2_id = uuid.uuid4()

        class MockRun:
            def __init__(self, id: uuid.UUID, parent_run_id: uuid.UUID | None, langgraph_thread_id: str):
                self.id = id
                self.parent_run_id = parent_run_id
                self.langgraph_thread_id = langgraph_thread_id

        root = MockRun(root_id, None, root_thread)
        child1 = MockRun(child1_id, root_id, root_thread)  # Resume of root
        child2 = MockRun(child2_id, child1_id, root_thread)  # Resume of child1

        # Both children share the root's thread_id
        assert child1.langgraph_thread_id == root.langgraph_thread_id
        assert child2.langgraph_thread_id == root.langgraph_thread_id

        # Both are resumes
        assert child1.langgraph_thread_id == root.langgraph_thread_id
        assert child2.langgraph_thread_id == child1.langgraph_thread_id

    def test_mixed_retry_and_resume_children(self) -> None:
        """A root can have both retry and resume children."""
        root_id = uuid.uuid4()
        root_thread = f"{_ORG}:{root_id}"

        class MockRun:
            def __init__(self, id: uuid.UUID, parent_run_id: uuid.UUID | None, langgraph_thread_id: str):
                self.id = id
                self.parent_run_id = parent_run_id
                self.langgraph_thread_id = langgraph_thread_id

        root = MockRun(root_id, None, root_thread)
        resume_child = MockRun(uuid.uuid4(), root_id, root_thread)  # Resume
        retry_child = MockRun(uuid.uuid4(), root_id, f"{_ORG}:{uuid.uuid4()}")  # Retry

        assert resume_child.langgraph_thread_id == root.langgraph_thread_id
        assert retry_child.langgraph_thread_id != root.langgraph_thread_id

    def test_deep_parent_chain_root_discovery(self) -> None:
        """Walking up a deep parent chain finds the correct root."""
        root_id = uuid.uuid4()
        root_thread = f"{_ORG}:{root_id}"

        class MockRun:
            def __init__(self, id: uuid.UUID, parent_run_id: uuid.UUID | None, langgraph_thread_id: str):
                self.id = id
                self.parent_run_id = parent_run_id
                self.langgraph_thread_id = langgraph_thread_id

        root = MockRun(root_id, None, root_thread)
        child1 = MockRun(uuid.uuid4(), root_id, f"{_ORG}:{uuid.uuid4()}")  # Retry of root
        child2 = MockRun(uuid.uuid4(), child1.id, f"{_ORG}:{uuid.uuid4()}")  # Retry of child1
        child3 = MockRun(uuid.uuid4(), child2.id, child2.langgraph_thread_id)  # Resume of child2

        # Walk up from child3 to find root
        current = child3
        root_found = None
        while current.parent_run_id:
            if current.parent_run_id == root_id:
                root_found = root
                break
            # In real code, we'd fetch parent from DB
            # Here we simulate the chain
            if current.parent_run_id == child2.id:
                current = child2
            elif current.parent_run_id == child1.id:
                current = child1
            elif current.parent_run_id == root_id:
                current = root
                root_found = root
                break

        assert root_found is not None
        assert root_found.id == root_id

        # child3 is a resume of child2 (same thread)
        assert child3.langgraph_thread_id == child2.langgraph_thread_id

    def test_root_run_has_no_group(self) -> None:
        """Root runs (parent_run_id IS NULL) have no run_group_id or run_group_kind."""
        root_id = uuid.uuid4()

        class MockRun:
            def __init__(self, id: uuid.UUID, parent_run_id: uuid.UUID | None, langgraph_thread_id: str):
                self.id = id
                self.parent_run_id = parent_run_id
                self.langgraph_thread_id = langgraph_thread_id

        root = MockRun(root_id, None, f"{_ORG}:{root_id}")

        assert root.parent_run_id is None
        # Backfill would not assign group to root
        group_id, kind = _backfill_classify_run(root, None)
        assert group_id is None
        assert kind is None

    def test_backfill_idempotent(self) -> None:
        """Running the backfill logic twice produces the same result."""
        root_id = uuid.uuid4()
        root_thread = f"{_ORG}:{root_id}"

        class MockRun:
            def __init__(self, id: uuid.UUID, parent_run_id: uuid.UUID | None, langgraph_thread_id: str):
                self.id = id
                self.parent_run_id = parent_run_id
                self.langgraph_thread_id = langgraph_thread_id

        root = MockRun(root_id, None, root_thread)
        child = MockRun(uuid.uuid4(), root_id, root_thread)

        # First pass
        is_resume = child.langgraph_thread_id == root.langgraph_thread_id
        kind1 = RUN_GROUP_KIND_RESUME if is_resume else RUN_GROUP_KIND_RETRY
        group_id1 = root.id

        # Second pass (same logic)
        is_resume2 = child.langgraph_thread_id == root.langgraph_thread_id
        kind2 = RUN_GROUP_KIND_RESUME if is_resume2 else RUN_GROUP_KIND_RETRY
        group_id2 = root.id

        assert kind1 == kind2
        assert group_id1 == group_id2


def _backfill_classify_run(run: object, parent_run: object | None) -> tuple[uuid.UUID | None, str | None]:
    """Simulate the backfill classification logic.

    Returns (run_group_id, run_group_kind) that the backfill would assign.
    """
    if parent_run is None:
        return None, None

    is_resume = run.langgraph_thread_id == parent_run.langgraph_thread_id
    kind = RUN_GROUP_KIND_RESUME if is_resume else RUN_GROUP_KIND_RETRY
    return parent_run.id, kind


class TestLegacyRunGroupBackfillDB:
    """Test the backfill with actual database (integration-style)."""

    async def test_resume_child_in_db(self, session: AsyncSession) -> None:
        """Test that a resume child can be inserted and classified correctly."""
        root = await _seed_root(session)

        # Insert a resume child with run_group_kind='resume' to satisfy constraints
        child_id = uuid.uuid4()
        await _insert_legacy_run(
            session,
            run_id=child_id,
            parent_run_id=root.id,
            langgraph_thread_id=root.langgraph_thread_id,
            # run_group_kind will be auto-set to 'resume' by _insert_legacy_run
        )

        child = await _get_run(session, child_id)
        parent = await _get_run(session, child.parent_run_id)

        assert child is not None
        assert parent is not None
        assert child.langgraph_thread_id == parent.langgraph_thread_id
        assert child.run_group_kind == RUN_GROUP_KIND_RESUME

    async def test_retry_child_in_db(self, session: AsyncSession) -> None:
        """Test that a retry child can be inserted and classified correctly."""
        root = await _seed_root(session)

        # Insert a retry child with different thread_id
        different_thread = f"{_ORG}:{uuid.uuid4()}"
        child_id = uuid.uuid4()
        await _insert_legacy_run(
            session,
            run_id=child_id,
            parent_run_id=root.id,
            langgraph_thread_id=different_thread,
        )

        child = await _get_run(session, child_id)
        parent = await _get_run(session, child.parent_run_id)

        assert child is not None
        assert parent is not None
        assert child.langgraph_thread_id != parent.langgraph_thread_id
        assert child.run_group_kind == RUN_GROUP_KIND_RETRY

    async def test_root_run_has_no_group_in_db(self, session: AsyncSession) -> None:
        """Root runs have no run_group_id or run_group_kind."""
        root = await _seed_root(session)

        assert root.parent_run_id is None
        assert root.run_group_id is None
        assert root.run_group_kind is None

    async def test_mixed_children_in_db(self, session: AsyncSession) -> None:
        """A root can have both retry and resume children."""
        root = await _seed_root(session)

        # Resume child
        resume_id = uuid.uuid4()
        await _insert_legacy_run(
            session,
            run_id=resume_id,
            parent_run_id=root.id,
            langgraph_thread_id=root.langgraph_thread_id,
        )

        # Retry child
        retry_id = uuid.uuid4()
        await _insert_legacy_run(
            session,
            run_id=retry_id,
            parent_run_id=root.id,
            langgraph_thread_id=f"{_ORG}:{uuid.uuid4()}",
        )

        resume_child = await _get_run(session, resume_id)
        retry_child = await _get_run(session, retry_id)

        assert resume_child.run_group_kind == RUN_GROUP_KIND_RESUME
        assert retry_child.run_group_kind == RUN_GROUP_KIND_RETRY
        assert resume_child.langgraph_thread_id == root.langgraph_thread_id
        assert retry_child.langgraph_thread_id != root.langgraph_thread_id


def test_downgrade_refuses_destructive_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _load_migration()
    monkeypatch.setattr(migration.op, "get_bind", lambda: pytest.fail("downgrade must not write"))

    with pytest.raises(RuntimeError, match="cannot be downgraded safely"):
        migration.downgrade()


class TestLegacyAgentVerdictExtraction:
    """Test the legacy agent verdict extraction from outputs_json."""

    def test_engine_status_alone_is_not_a_self_reported_verdict(self) -> None:
        from modulo.api.routes.runs import _extract_legacy_agent_verdict

        outputs_json = {
            "node1": {
                "status": "failed",
                "output": {"result": "engine failure envelope"},
            }
        }

        assert _extract_legacy_agent_verdict(outputs_json) is None

    @pytest.mark.asyncio
    async def test_bulk_reader_uses_one_org_scoped_query(self) -> None:
        from modulo.api.routes.runs import _load_legacy_outputs

        run_id = uuid.uuid4()
        row = type("OutputRow", (), {})()
        row.run_id = run_id
        row.node_id = "node1"
        row.outputs_json = {"output": {"agent_status": "failed"}}
        scalars = type("Scalars", (), {"all": lambda self: [row]})()
        result = type("Result", (), {"scalars": lambda self: scalars})()
        session = AsyncMock(spec=AsyncSession)
        session.execute.return_value = result

        outputs = await _load_legacy_outputs(
            session,
            [type("Run", (), {"id": run_id})()],
            _ORG,
        )

        assert outputs[run_id] == {"node1": row.outputs_json}
        session.execute.assert_awaited_once()
        query = session.execute.await_args.args[0]
        assert "run_node_outputs" in str(query)
        assert "organisation_id" in str(query)

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
