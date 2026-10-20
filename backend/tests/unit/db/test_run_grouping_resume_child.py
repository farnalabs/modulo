"""FAR-153 / §15.1: run grouping + resume-as-child through the REAL ``create_run``.

Two layers, both exercised against the REAL code (no mock of the thing under
test) on an in-memory SQLite database whose schema comes from the ORM
metadata — i.e. the two partial unique indexes from
``models.run.__table_args__`` are created and ENFORCED:

* **thread model** — a retry child gets a fresh thread and runs its graph
  CLEAN; a resume child created with ``thread_id=<parent's thread>``
  shares that thread (impossible under the old global unique the migration
  drops); a SECOND resume of the same stalled parent is rejected at the DB
  (the ``uq_runs_resume_parent`` partial unique — the 409 half of
  "double-resume rejected 409/DB" rides the route layer's generic
  IntegrityError mapping); a resume child that itself stalls can be resumed
  again because its parent is a DIFFERENT row, so resume-of-resume chains
  work; every NON-resume row keeps global thread uniqueness
  (``uq_runs_thread_non_resume`` — two roots cannot share a thread, and a
  retry can never inherit its parent's thread);
* **grouping kwargs contract** — ``run_group_kind`` is the closed
  vocabulary ``{'retry','resume'}``, both kinds are child rows (parent
  required) that must carry the group's root id, a resume must carry its
  parent's thread AND that thread must actually BE the parent's (a
  wrong-thread resume — even a thread owned by a run in another org — is
  rejected at the persist boundary), and every violation raises
  ``ValueError`` at the persist boundary BEFORE any DB write.

The DB fixture mirrors ``test_run_execution_origin.py``: real
``create_run`` against in-memory SQLite — no hand-rolled session double.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import cast

import pytest
from sqlalchemy import Table, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.run import create_run
from modulo.db.models.base import Base
from modulo.db.models.eval import Eval
from modulo.db.models.journey import Journey
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import RUN_GROUP_KIND_RESUME, RUN_GROUP_KIND_RETRY, Run
from modulo.db.models.team import Team
from modulo.db.models.variant_group import VariantGroup

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000f2")
_SNAPSHOT = uuid.UUID("00000000-0000-0000-0000-0000000000f3")

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


async def _count(session: AsyncSession, *where: object) -> int:
    return int((await session.execute(select(func.count()).select_from(Run).where(*where))).scalar_one())


async def _seed_root(session: AsyncSession) -> Run:
    await _seed_org(session)
    await _seed_snapshot(session)
    return await _create_run(session)


# ---------------------------------------------------------------------------
# Defaults: existing callers are completely unchanged
# ---------------------------------------------------------------------------


class TestCreateRunDefaults:
    async def test_default_run_gets_fresh_thread_and_no_group(self, session: AsyncSession) -> None:
        run = await _seed_root(session)
        # §15.1: the default thread shape (and only the resume override) may
        # differ from f"{org}:{run_id}" — every existing caller relies on it.
        assert run.langgraph_thread_id == f"{_ORG}:{run.id}"
        assert run.run_group_id is None
        assert run.run_group_kind is None
        assert run.parent_run_id is None


# ---------------------------------------------------------------------------
# Retry: child row, fresh thread, graph CLEAN
# ---------------------------------------------------------------------------


class TestRetryChild:
    async def test_retry_child_gets_fresh_thread_and_stores_the_group(self, session: AsyncSession) -> None:
        root = await _seed_root(session)
        child = await _create_run(
            session,
            parent_run_id=root.id,
            run_group_id=root.id,
            run_group_kind=RUN_GROUP_KIND_RETRY,
        )
        # §15.1: retry = fresh thread f"{org}:{child_run_id}" — NEVER the parent's.
        assert child.langgraph_thread_id == f"{_ORG}:{child.id}"
        assert child.langgraph_thread_id != root.langgraph_thread_id
        assert child.run_group_kind == RUN_GROUP_KIND_RETRY
        assert child.run_group_id == root.id
        assert child.parent_run_id == root.id

    async def test_retry_child_cannot_inherit_the_parent_thread(self, session: AsyncSession) -> None:
        """The retry-runs-CLEAN rule is DB-enforced: a retry is a non-resume
        row, so ``uq_runs_thread_non_resume`` rejects any attempt to give it
        a thread another non-resume row already holds."""
        root = await _seed_root(session)
        with pytest.raises(IntegrityError):
            await _create_run(
                session,
                parent_run_id=root.id,
                run_group_id=root.id,
                run_group_kind=RUN_GROUP_KIND_RETRY,
                thread_id=root.langgraph_thread_id,
            )


# ---------------------------------------------------------------------------
# Resume: child row, inherits the parent's thread, continues the checkpoint
# ---------------------------------------------------------------------------


class TestResumeChild:
    async def test_resume_child_inherits_parent_thread(self, session: AsyncSession) -> None:
        root = await _seed_root(session)
        child = await _create_run(
            session,
            parent_run_id=root.id,
            run_group_id=root.id,
            run_group_kind=RUN_GROUP_KIND_RESUME,
            thread_id=root.langgraph_thread_id,
        )
        # §15.1: resume CONTINUES the parent's checkpoint on the SAME thread.
        assert child.langgraph_thread_id == root.langgraph_thread_id
        assert child.run_group_kind == RUN_GROUP_KIND_RESUME
        assert child.run_group_id == root.id
        assert child.parent_run_id == root.id

    async def test_second_resume_of_the_same_parent_is_rejected_at_the_db(self, session: AsyncSession) -> None:
        """Exactly-one-resume-per-parent (``uq_runs_resume_parent``): the
        second resume of the same stall fails on the partial unique — the DB
        half of "double-resume rejected 409/DB" (the 409 is the route
        layer's generic IntegrityError mapping when the resume endpoint
        lands)."""
        root = await _seed_root(session)
        await _create_run(
            session,
            parent_run_id=root.id,
            run_group_id=root.id,
            run_group_kind=RUN_GROUP_KIND_RESUME,
            thread_id=root.langgraph_thread_id,
        )
        with pytest.raises(IntegrityError):
            await _create_run(
                session,
                parent_run_id=root.id,
                run_group_id=root.id,
                run_group_kind=RUN_GROUP_KIND_RESUME,
                thread_id=root.langgraph_thread_id,
            )
        # The rejected row left nothing behind: exactly ONE resume child.
        assert await _count(session, Run.run_group_kind == RUN_GROUP_KIND_RESUME) == 1

    async def test_resume_of_resume_chain_is_accepted(self, session: AsyncSession) -> None:
        """A resume child that stalls can itself be resumed: its parent is a
        DIFFERENT row, so the exactly-one-resume fence admits it, and the
        whole chain keeps sharing the root's thread (only resume rows may)."""
        root = await _seed_root(session)
        first = await _create_run(
            session,
            parent_run_id=root.id,
            run_group_id=root.id,
            run_group_kind=RUN_GROUP_KIND_RESUME,
            thread_id=root.langgraph_thread_id,
        )
        second = await _create_run(
            session,
            parent_run_id=first.id,
            run_group_id=root.id,
            run_group_kind=RUN_GROUP_KIND_RESUME,
            thread_id=root.langgraph_thread_id,
        )
        assert second.parent_run_id == first.id
        assert second.run_group_id == root.id
        assert second.langgraph_thread_id == root.langgraph_thread_id
        # root (NULL kind) + two resume rows all sit on ONE thread — the
        # thread partial unique indexes only the root, so the share is legal.
        assert await _count(session, Run.langgraph_thread_id == root.langgraph_thread_id) == 3


# ---------------------------------------------------------------------------
# Global thread uniqueness survives for every NON-resume row
# ---------------------------------------------------------------------------


class TestThreadUniquenessForNonResumeRows:
    async def test_two_roots_cannot_share_a_thread(self, session: AsyncSession) -> None:
        """The partial unique replaces the global one for NULL/'retry' rows —
        their thread uniqueness is exactly as strict as before 0300."""
        await _seed_org(session)
        await _seed_snapshot(session)
        first = await _create_run(session, thread_id="shared-thread")
        assert first.langgraph_thread_id == "shared-thread"
        with pytest.raises(IntegrityError):
            await _create_run(session, thread_id="shared-thread")

    async def test_a_root_and_a_retry_cannot_share_a_thread(self, session: AsyncSession) -> None:
        root = await _seed_root(session)
        with pytest.raises(IntegrityError):
            await _create_run(
                session,
                parent_run_id=root.id,
                run_group_id=root.id,
                run_group_kind=RUN_GROUP_KIND_RETRY,
                thread_id=root.langgraph_thread_id,
            )


# ---------------------------------------------------------------------------
# The grouping kwargs contract (validated BEFORE any DB work)
# ---------------------------------------------------------------------------


class TestGroupingKwargsValidation:
    async def test_unknown_kind_is_rejected(self, session: AsyncSession) -> None:
        await _seed_org(session)
        with pytest.raises(ValueError, match="invalid run_group_kind"):
            await _create_run(session, run_group_kind="resumed")

    async def test_retry_child_requires_a_parent(self, session: AsyncSession) -> None:
        await _seed_org(session)
        with pytest.raises(ValueError, match="requires parent_run_id"):
            await _create_run(session, run_group_kind=RUN_GROUP_KIND_RETRY)

    async def test_grouped_child_requires_the_root_group_id(self, session: AsyncSession) -> None:
        root = await _seed_root(session)
        with pytest.raises(ValueError, match="requires run_group_id"):
            await _create_run(session, parent_run_id=root.id, run_group_kind=RUN_GROUP_KIND_RETRY)

    async def test_resume_requires_the_parent_thread(self, session: AsyncSession) -> None:
        root = await _seed_root(session)
        with pytest.raises(ValueError, match="requires thread_id"):
            await _create_run(
                session,
                parent_run_id=root.id,
                run_group_id=root.id,
                run_group_kind=RUN_GROUP_KIND_RESUME,
            )

    async def test_resume_thread_must_be_the_parents(self, session: AsyncSession) -> None:
        """A resume child may pass only its PARENT's thread: both partial
        unique indexes exclude resume rows, so without this persist-boundary
        read a wrong-thread resume — even a thread owned by a run in another
        org — would pass validation and every DB fence silently. Wrong
        thread rejected BEFORE any write; the correct thread still accepted.
        """
        root = await _seed_root(session)
        with pytest.raises(ValueError, match="must equal the parent run's langgraph_thread_id"):
            await _create_run(
                session,
                parent_run_id=root.id,
                run_group_id=root.id,
                run_group_kind=RUN_GROUP_KIND_RESUME,
                thread_id="00000000-0000-0000-0000-0000000000ee:some-other-run",
            )
        # The rejection happened before any write: no resume row landed.
        assert await _count(session, Run.run_group_kind == RUN_GROUP_KIND_RESUME) == 0
        # The CORRECT thread is still accepted — the child continues the
        # parent's checkpoint.
        child = await _create_run(
            session,
            parent_run_id=root.id,
            run_group_id=root.id,
            run_group_kind=RUN_GROUP_KIND_RESUME,
            thread_id=root.langgraph_thread_id,
        )
        assert child.langgraph_thread_id == root.langgraph_thread_id

    async def test_resume_parent_must_exist_in_this_org(self, session: AsyncSession) -> None:
        """The parent read is org-scoped: a resume whose parent_run_id does
        not resolve to a run of THIS org (missing row, or a row owned by
        another org) is rejected before any write — a resume never crosses
        orgs."""
        root = await _seed_root(session)
        with pytest.raises(ValueError, match="not found in this org"):
            await _create_run(
                session,
                parent_run_id=uuid.uuid4(),
                run_group_id=root.id,
                run_group_kind=RUN_GROUP_KIND_RESUME,
                thread_id=root.langgraph_thread_id,
            )
        assert await _count(session, Run.run_group_kind == RUN_GROUP_KIND_RESUME) == 0

    async def test_empty_thread_override_is_rejected(self, session: AsyncSession) -> None:
        await _seed_org(session)
        with pytest.raises(ValueError, match="non-empty"):
            await _create_run(session, thread_id="")

    async def test_validation_runs_before_any_database_work(self, session: AsyncSession) -> None:
        """A grouping-contract violation raises with NO org/snapshot seeded —
        proof the guard fires before any DB read or write (the
        ``run_idempotency_key`` persist-boundary pattern)."""
        with pytest.raises(ValueError, match="invalid run_group_kind"):
            await _create_run(session, run_group_kind="bogus")
