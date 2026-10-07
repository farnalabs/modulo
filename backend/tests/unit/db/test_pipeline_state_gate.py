"""FAR-1528: the ``create_run`` pipeline-state gate (archived / soft-deleted).

Regression proof for the archived-pipeline hole: ``Pipeline.archived_at`` was
referenced ONLY by list/count/dashboard queries, so the run path never checked
it — an archived pipeline's triggers kept firing and manual runs still started.
These tests drive the REAL ``create_run`` against an in-memory SQLite database
(no mocks of the function under test):

  * an ARCHIVED pipeline is refused — the regression test; it FAILS without
    the gate and PASSES with it,
  * a soft-deleted pipeline is refused,
  * an ACTIVE pipeline still creates its run (control — the gate refuses
    nothing else),
  * a PAUSED pipeline (FAR-1530, ``run_enabled = FALSE``) is refused with
    ``state="paused"`` — the regression test for this slice; it FAILS without
    the gate extension (the gate's SELECT would not carry ``run_enabled`` and
    the helper would not know the state) and PASSES with it,
  * a pipeline row that is absent is NOT refused by this gate (lifecycle
    state is its scope; existence is enforced by the ``runs.pipeline_id``
    FK upstream/downstream — pinned so the scope does not drift),
  * the gate's read failure PROPAGATES (fail closed: a DB error is never
    converted into a refusal state and never into "runnable"),
  * the gate's read is ORG-SCOPED (RLS equivalence: the raw ``text()`` select
    carries ``organisation_id = :org`` so SQLite/MariaDB see what Postgres'
    ``rls_org_isolation`` policy would show them),
  * the state-priority helper FAR-1530 extended: deleted > archived > paused.
"""

import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import cast

import pytest
from sqlalchemy import Table, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.exceptions import PipelineNotRunnableError
from modulo.db.crud.run import _enforce_pipeline_state_gate, _pipeline_not_runnable_state, create_run
from modulo.db.models.base import Base
from modulo.db.models.eval import Eval
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.journey import Journey
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.models.team import Team
from modulo.db.models.variant_group import VariantGroup

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_SNAPSHOT = uuid.UUID("00000000-0000-0000-0000-0000000000b1")

# Same table set the real ``create_run`` path touches (journey stamping,
# guardrail interception, run row, snapshot FK target).
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


async def _seed_pipeline(
    session: AsyncSession,
    *,
    archived_at: datetime | None = None,
    deleted_at: datetime | None = None,
    run_enabled: bool = True,
) -> None:
    session.add(
        Pipeline(
            id=_PIPELINE,
            organisation_id=_ORG,
            name="pipeline",
            account_id=_ORG,
            visibility="org",
            archived_at=archived_at,
            deleted_at=deleted_at,
            # ck_pipelines_run_enabled: a disabled row must carry its cause.
            run_enabled=run_enabled,
            run_disabled_reason=None if run_enabled else "operator",
            run_disabled_at=None if run_enabled else datetime.now(UTC),
        )
    )
    await session.flush()


async def _create(session: AsyncSession) -> Run:
    return await create_run(
        session,
        org_id=_ORG,
        pipeline_id=_PIPELINE,
        snapshot_id=_SNAPSHOT,
        trigger_type="manual",
        input_payload={"data": 1},
    )


async def _run_count(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count()).select_from(Run))).scalar_one())


class _ReadFailingSession:
    """Session double whose read raises, exactly like a DB outage would."""

    async def execute(self, *args: object, **kwargs: object) -> object:
        raise OperationalError(
            "SELECT archived_at, deleted_at, run_enabled FROM pipelines", {}, Exception("connection lost")
        )


class TestCreateRunRefusesNonRunnablePipelines:
    async def test_archived_pipeline_run_is_refused(self, session: AsyncSession) -> None:
        """REGRESSION: an archived pipeline must not start a run (FAR-1528).

        Before the gate this create SUCCEEDED — archived_at was only ever
        read by list/count/dashboard queries.
        """
        await _seed_org(session)
        await _seed_pipeline(session, archived_at=datetime.now(UTC))

        with pytest.raises(PipelineNotRunnableError) as excinfo:
            await _create(session)

        assert excinfo.value.state == "archived"
        assert excinfo.value.pipeline_id == _PIPELINE
        # The refusal happens before any run row exists — nothing persists.
        assert await _run_count(session) == 0

    async def test_soft_deleted_pipeline_run_is_refused(self, session: AsyncSession) -> None:
        await _seed_org(session)
        await _seed_pipeline(session, deleted_at=datetime.now(UTC))

        with pytest.raises(PipelineNotRunnableError) as excinfo:
            await _create(session)

        assert excinfo.value.state == "deleted"
        assert excinfo.value.pipeline_id == _PIPELINE
        assert await _run_count(session) == 0

    async def test_active_pipeline_still_creates_run(self, session: AsyncSession) -> None:
        """Control: the gate refuses ONLY the non-runnable states."""
        await _seed_org(session)
        await _seed_pipeline(session)

        run = await _create(session)

        assert run.id is not None
        assert await _run_count(session) == 1

    async def test_paused_pipeline_run_is_refused(self, session: AsyncSession) -> None:
        """REGRESSION (FAR-1530): a Paused pipeline must not start a run.

        ``run_enabled = FALSE`` is present + visible but NON-EXECUTING — every
        origin (manual REST, MCP trigger, webhook, cron, polling, ...)
        converges on ``create_run``, so this one refusal covers them all.
        FAILS without the gate extension: before it, the gate's SELECT carried
        no ``run_enabled`` and a paused pipeline's manual run was created
        exactly like an active one.
        """
        await _seed_org(session)
        await _seed_pipeline(session, run_enabled=False)

        with pytest.raises(PipelineNotRunnableError) as excinfo:
            await _create(session)

        assert excinfo.value.state == "paused"
        assert excinfo.value.pipeline_id == _PIPELINE
        # The refusal happens before any run row exists — nothing persists.
        assert await _run_count(session) == 0

    async def test_enabled_pipeline_with_a_cause_still_creates_run(self, session: AsyncSession) -> None:
        """Scope pin: the gate reads ``run_enabled``, never the cause columns.

        A row whose reason/timestamp columns are populated but whose
        ``run_enabled`` is TRUE must not be refused on the cause alone (the
        state, not the metadata, is the gate's subject). Such a row cannot be
        written through the CHECK constraints — it exists only as a direct
        column write, which is exactly why the helper keys on the boolean.
        """
        await _seed_org(session)
        pipeline = Pipeline(
            id=_PIPELINE,
            organisation_id=_ORG,
            name="pipeline",
            account_id=_ORG,
            visibility="org",
            run_enabled=True,
            run_disabled_reason="operator",
            run_disabled_at=datetime.now(UTC),
        )
        session.add(pipeline)
        await session.flush()

        run = await _create(session)

        assert run.id is not None
        assert await _run_count(session) == 1

    async def test_absent_pipeline_row_is_not_refused_by_the_state_gate(self, session: AsyncSession) -> None:
        """Scope pin: this gate owns LIFECYCLE state, not row existence.

        With no pipeline row there is no ``archived_at``/``deleted_at`` to
        refuse on; existence is enforced by the ``runs.pipeline_id`` RESTRICT
        FK (Postgres) and by every caller's entry filter. Read *failures* still
        propagate (see ``TestPipelineStateGateReadFailure``).
        """
        await _seed_org(session)

        run = await _create(session)

        assert run.id is not None


class TestPipelineStateGateReadFailure:
    async def test_read_failure_propagates_and_is_never_a_state(self) -> None:
        """Fail closed: a DB error in the gate PROPAGATES — it is never
        converted to ``PipelineNotRunnableError`` and never swallowed into
        "runnable" (``pytest.raises(OperationalError)`` fails if either
        happened)."""
        with pytest.raises(OperationalError):
            await _enforce_pipeline_state_gate(cast(AsyncSession, _ReadFailingSession()), uuid.uuid4(), uuid.uuid4())


class TestPipelineStateGateOrgScope:
    """RLS equivalence for the gate's raw ``text()`` read (FAR-1528).

    On Postgres the ``rls_org_isolation`` policy scopes this SELECT; on
    SQLite/MariaDB the ORM tenant filter does not reach ``text()`` statements,
    so the gate carries its own ``organisation_id = :org`` predicate. A row the
    session's org cannot see must be ROW ABSENT (not a refusal), exactly like
    the team-visibility case pinned above.
    """

    async def test_archived_pipeline_of_another_org_is_row_absent(
        self, session: AsyncSession, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The archived row belongs to ``_ORG``; asking with a different org id
        sees nothing to refuse on. The gate's own row-absent log is the
        observable proof that the read (not the refusal) was org-scoped."""
        await _seed_org(session)
        await _seed_pipeline(session, archived_at=datetime.now(UTC))

        with caplog.at_level(logging.WARNING, logger="modulo.db.crud.run"):
            await _enforce_pipeline_state_gate(session, _PIPELINE, uuid.uuid4())

        assert any("pipeline_row_absent" in record.getMessage() for record in caplog.records)

    async def test_archived_pipeline_of_the_owning_org_is_refused(self, session: AsyncSession) -> None:
        """Control: the org predicate does not soften the refusal for the
        row's own org."""
        await _seed_org(session)
        await _seed_pipeline(session, archived_at=datetime.now(UTC))

        with pytest.raises(PipelineNotRunnableError) as excinfo:
            await _enforce_pipeline_state_gate(session, _PIPELINE, _ORG)

        assert excinfo.value.state == "archived"


class TestPipelineNotRunnableStateHelper:
    """The one place the not-runnable state set is defined (FAR-1530 extended
    it with Paused, at the same choke point)."""

    def test_active_pipeline_is_runnable(self) -> None:
        assert _pipeline_not_runnable_state(archived_at=None, deleted_at=None, run_enabled=True) is None

    def test_archived_is_refused(self) -> None:
        stamp = datetime.now(UTC)
        assert _pipeline_not_runnable_state(archived_at=stamp, deleted_at=None, run_enabled=True) == "archived"

    def test_soft_deleted_is_refused(self) -> None:
        stamp = datetime.now(UTC)
        assert _pipeline_not_runnable_state(archived_at=None, deleted_at=stamp, run_enabled=True) == "deleted"

    def test_deleted_outranks_archived(self) -> None:
        stamp = datetime.now(UTC)
        assert _pipeline_not_runnable_state(archived_at=stamp, deleted_at=stamp, run_enabled=True) == "deleted"

    def test_paused_is_refused(self) -> None:
        """FAR-1530: ``run_enabled=False`` is the Paused refusal state."""
        assert _pipeline_not_runnable_state(archived_at=None, deleted_at=None, run_enabled=False) == "paused"

    def test_archived_outranks_paused(self) -> None:
        """An archived pipeline already implies non-executing: report the
        archived state, not paused."""
        assert (
            _pipeline_not_runnable_state(archived_at=datetime.now(UTC), deleted_at=None, run_enabled=False)
            == "archived"
        )

    def test_deleted_outranks_paused(self) -> None:
        assert (
            _pipeline_not_runnable_state(archived_at=None, deleted_at=datetime.now(UTC), run_enabled=False) == "deleted"
        )

    def test_run_enabled_is_required(self) -> None:
        """Fail closed: a caller that forgets ``run_enabled`` must fail loudly
        (TypeError) instead of silently reading a paused pipeline as runnable."""
        with pytest.raises(TypeError):
            _pipeline_not_runnable_state(archived_at=None, deleted_at=None)  # type: ignore[call-arg]
