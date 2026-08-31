"""Unit tests for FAR-152 work_intact terminalization + migration 0087.

work_intact is computed AT TERMINALIZATION from completed-node artifacts + the
full DAG ran (��15.3) �?" never from the async evidence probe. It restores the
false-failure banner for harness-crash incidents #1/#3, and is suppressed
(False) for A1-elevated runs (a run that self-reported failure is not
complete, ��15.4). Also covers the reconciliation chain's run_evidence table +
runs.work_intact surface (0110_schema_pipeline_runtime) and the executor's
post-commit evidence-probe wiring.
"""

import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from modulo.core.pipeline_engine.evidence import EvidenceResult
from modulo.core.pipeline_engine.executor import PipelineExecutor, _apply_work_intact
from modulo.db.models.base import Base
from modulo.db.models.run import Run
from modulo.db.models.run_evidence import RunEvidence

_NODE_A = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


class _FakeEvidenceProvider:
    def __init__(self, result: EvidenceResult) -> None:
        self.result = result
        self.probes: list[tuple[Any, Any]] = []

    async def git_diff_empty(self, run_id, node_id):
        self.probes.append((run_id, node_id))
        return self.result

    async def sandbox_filesystem_probe(self, run_id, node_id):
        return self.result


def _declared_success_output() -> dict[str, Any]:
    return {"output": {"status": "completed", "agent_status": "completed", "agent_outcome": "success"}}


def _completed_output() -> dict[str, Any]:
    return {"output": {"status": "completed", "summary": "all good"}}


@pytest.fixture
def executor() -> PipelineExecutor:
    return PipelineExecutor(MagicMock())


# ---------------------------------------------------------------------------
# _compute_run_work_intact — the terminalization verdict
# ---------------------------------------------------------------------------


class TestComputeRunWorkIntact:
    def test_non_terminal_returns_none(self, executor: PipelineExecutor) -> None:
        assert executor._compute_run_work_intact("awaiting_human", None, {"a": _completed_output()}, {"a"}) is None

    def test_terminal_complete_full_dag_is_true(self, executor: PipelineExecutor) -> None:
        assert executor._compute_run_work_intact("complete", None, {"a": _completed_output()}, {"a"}) is True

    def test_terminal_failed_full_dag_is_true(self, executor: PipelineExecutor) -> None:
        # A harness/sandbox failure with intact work → the false-failure banner.
        assert (
            executor._compute_run_work_intact("failed", "harness.db.connection_lost", {"a": _completed_output()}, {"a"})
            is True
        )

    def test_truncated_run_not_intact(self, executor: PipelineExecutor) -> None:
        # A run truncated at node 1 of 2 is NOT work-intact (§2.3.2).
        assert (
            executor._compute_run_work_intact(
                "failed", "harness.db.connection_lost", {"a": _completed_output()}, {"a", "b"}
            )
            is False
        )

    def test_agent_failed_elevation_is_false(self, executor: PipelineExecutor) -> None:
        # A1 elevation (§15.4): the run is NOT complete — honest verdict False.
        assert (
            executor._compute_run_work_intact("failed", "agent.failed", {"a": _declared_success_output()}, {"a"})
            is False
        )

    def test_invalid_artifact_not_intact(self, executor: PipelineExecutor) -> None:
        assert executor._compute_run_work_intact("failed", "harness.db.connection_lost", {"a": {}}, {"a"}) is False


# ---------------------------------------------------------------------------
# _apply_work_intact — fenced terminalization UPDATE
# ---------------------------------------------------------------------------


@pytest.fixture
async def runs_engine():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[Run.__table__]))
        # runs.work_intact (migration 0091) is now MAPPED on the Run ORM model,
        # so create_all provides the column — no manual ALTER needed (a
        # duplicate ALTER TABLE would fail with "duplicate column name").
    yield engine
    await engine.dispose()


async def _insert_run(factory, *, claim_token: str | None = None) -> tuple[uuid.UUID, str | None]:
    run_id = uuid.uuid4()
    async with factory() as session, session.begin():
        session.add(
            Run(
                id=run_id,
                organisation_id=uuid.uuid4(),
                pipeline_id=uuid.uuid4(),
                snapshot_id=uuid.uuid4(),
                trigger_type="manual",
                status="running",
                run_number=1,
                input_hash="a" * 64,
                langgraph_thread_id=f"thread-{run_id}",
            )
        )
        await session.flush()
    if claim_token is None:
        return run_id, None
    async with factory() as session, session.begin():
        row = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
        return run_id, row.claim_token


async def _read_work_intact(factory, run_id: uuid.UUID) -> bool | None:
    """Read runs.work_intact via raw SQL — the column is added by migration
    0087 and is intentionally NOT on the ORM Run model (out of this work's
    file scope; a follow-up maps it). The id is bound with the Uuid type so it
    matches SQLite's hex storage."""
    from sqlalchemy import Uuid, bindparam

    async with factory() as session, session.begin():
        value = (
            await session.execute(
                text("SELECT work_intact FROM runs WHERE id = :rid").bindparams(bindparam("rid", type_=Uuid())),
                {"rid": run_id},
            )
        ).scalar_one_or_none()
    # SQLite stores BOOLEAN as INTEGER (0/1), so the raw value is an int, not a
    # Python bool. Coerce to bool so callers can assert ``is True`` regardless
    # of backend, while preserving None for "column never written".
    if value is None:
        return None
    return bool(value)


class TestApplyWorkIntact:
    async def test_unfenced_write_updates_column(self, runs_engine) -> None:
        factory = async_sessionmaker(runs_engine, expire_on_commit=False, autobegin=False)
        run_id, _ = await _insert_run(factory)
        async with factory() as session, session.begin():
            await _apply_work_intact(session, run_id, True, claim_token=None)
        assert await _read_work_intact(factory, run_id) is True

    async def test_fenced_write_with_matching_token_updates(self, runs_engine) -> None:
        factory = async_sessionmaker(runs_engine, expire_on_commit=False, autobegin=False)
        run_id, token = await _insert_run(factory, claim_token="sentinel")
        async with factory() as session, session.begin():
            await _apply_work_intact(session, run_id, True, claim_token=token)
        assert await _read_work_intact(factory, run_id) is True

    async def test_fenced_write_with_stale_token_is_noop(self, runs_engine) -> None:
        factory = async_sessionmaker(runs_engine, expire_on_commit=False, autobegin=False)
        run_id, _ = await _insert_run(factory, claim_token="sentinel")
        async with factory() as session, session.begin():
            # A superseded executor's token no longer matches → no write.
            await _apply_work_intact(session, run_id, True, claim_token="other-token")
        assert await _read_work_intact(factory, run_id) is None


# ---------------------------------------------------------------------------
# migration 0008 — run_evidence table + runs.work_intact (reconciliation chain)
# ---------------------------------------------------------------------------

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"


class TestMigrationRunEvidenceFinalState:
    """The run_evidence surface now ships in the reconciliation chain
    (``0110_schema_pipeline_runtime``) instead of the deleted ``0091_run_evidence``
    migration. Assert the reconciliation DDL creates the run_evidence table with
    its evidence columns and adds ``runs.work_intact``."""

    def _source(self) -> str:
        path = _VERSIONS / "0110_schema_pipeline_runtime.py"
        assert path.exists(), f"Migration file missing: {path}"
        return path.read_text(encoding="utf-8")

    def test_0008_creates_run_evidence_table(self) -> None:
        source = self._source()
        assert "CREATE TABLE IF NOT EXISTS public.run_evidence" in source
        for column in ("run_id", "node_id", "evidence_state", "evidence_detail", "evidence_written_at"):
            assert f'"{column}"' in source, f"0008 run_evidence missing column {column}"

    def test_0008_creates_run_evidence_pk_and_fk(self) -> None:
        source = self._source()
        assert "pk_run_evidence_run_node" in source
        assert "run_evidence_run_id_fkey" in source

    def test_0008_adds_runs_work_intact(self) -> None:
        source = self._source()
        assert 'ADD COLUMN IF NOT EXISTS "work_intact" boolean' in source


# ---------------------------------------------------------------------------
# executor wiring — _run_post_terminal_evidence_probes
# ---------------------------------------------------------------------------


@pytest.fixture
async def sqlite_factory():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[RunEvidence.__table__, Run.__table__])
        )
    factory = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
    yield factory, engine
    await engine.dispose()


async def _evidence_rows(factory) -> list[RunEvidence]:
    async with factory() as session, session.begin():
        return list((await session.execute(select(RunEvidence))).scalars().all())


class TestPostTerminalEvidenceProbes:
    async def test_complete_run_with_declared_success_probes(self, sqlite_factory) -> None:
        factory, engine = sqlite_factory
        provider = _FakeEvidenceProvider(EvidenceResult.verified_empty)
        executor = PipelineExecutor(engine, evidence_provider=provider)
        run_id = uuid.uuid4()

        await executor._run_post_terminal_evidence_probes(
            run_id=run_id,
            org_id=uuid.uuid4(),
            final_status="complete",
            completed_node_outputs={str(_NODE_A): _declared_success_output()},
        )

        assert provider.probes == [(run_id, str(_NODE_A))]
        rows = await _evidence_rows(factory)
        assert len(rows) == 1
        assert rows[0].run_id == run_id
        assert rows[0].evidence_state == "verified_empty"

    async def test_failed_run_skips_probe(self, sqlite_factory) -> None:
        factory, engine = sqlite_factory
        provider = _FakeEvidenceProvider(EvidenceResult.verified_empty)
        executor = PipelineExecutor(engine, evidence_provider=provider)

        await executor._run_post_terminal_evidence_probes(
            run_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            final_status="failed",
            completed_node_outputs={str(_NODE_A): _declared_success_output()},
        )

        assert not provider.probes
        assert not await _evidence_rows(factory)

    async def test_non_declared_success_node_skips_probe(self, sqlite_factory) -> None:
        factory, engine = sqlite_factory
        provider = _FakeEvidenceProvider(EvidenceResult.verified_empty)
        executor = PipelineExecutor(engine, evidence_provider=provider)

        await executor._run_post_terminal_evidence_probes(
            run_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            final_status="complete",
            completed_node_outputs={str(_NODE_A): {"output": {"agent_status": "completed"}}},
        )

        assert not provider.probes
        assert not await _evidence_rows(factory)
