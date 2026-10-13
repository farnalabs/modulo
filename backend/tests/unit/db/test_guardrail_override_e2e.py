"""Remediation e2e via guardrail-override (FAR-223 item 13.2).

Through the REAL ``guardrail_override`` seam (real SQLite session, real
guardrail rows, real create_run to produce the blocked run):

* **re-block loop** — overriding with a STILL-VIOLATING input is refused
  (``GuardrailOverrideRejectedError``) and the run stays terminal ``eval_failed``;
* **journey-increment-once** — overriding with clean input flips the SAME run
  row with ``is_replay=True`` (a single run record, so the lifecycle-map
  journey increments exactly once — never a duplicate run); the flip target
  is ``pending`` for the genuine zero-node block and ``running`` for a
  node-started one (FAR-1711: the caller re-dispatches ``resume_run`` there,
  never re-executes), and the node-started flip is IMMEDIATELY claimable by
  the resume claim's stale-running arm (the ``heartbeat_at`` stamp — C1);
* **fail-closed without a checkpoint** — a node-started run with no root
  checkpoint is refused (``GuardrailOverrideNoCheckpointError``, FAR-1711
  M1) and left terminal, never flipped;
* **single-flight** — a concurrent override that loses the status-update race
  raises ``ConcurrentRecoveryError`` and does NOT clobber the winner.

Plus a REAL-POSTGRES (testcontainers) leg — ``TestRealPostgresMarkerDurability``
— proving the FAR-1728 dedicated marker column
(``runs.guardrail_override_resume_pending``, migration 0307) is DURABLY
persisted on read-back after a node-started ``eval_blocked`` flip, that the
reconcile leg's SQL predicate + row-side recheck re-select the row, and that
the resume claim clears the marker — never an in-memory ORM assertion alone.

These complement the mocked-session unit tests in
``tests/unit/pipeline_engine/test_guardrail_override.py`` by exercising the
real DB round-trip (guardrail row load + pass + status update against an actual
session), so a wiring regression between the pure seam and the DB path is
caught here.
"""

import os
import re
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
import sqlalchemy as sa
from sqlalchemy import Table, bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core import cron_helpers
from modulo.core.dispatch_phases import PHASE_FIRST_NODE_DISPATCHED
from modulo.core.pipeline_engine.recovery import (
    ConcurrentRecoveryError,
    GuardrailOverrideNoCheckpointError,
    GuardrailOverrideRejectedError,
    guardrail_override,
)
from modulo.core.pipeline_execution import claim_resume_run_async
from modulo.db.crud.run import create_run
from modulo.db.models.account import Account
from modulo.db.models.audit_event import AuditChainHead, AuditEvent
from modulo.db.models.base import Base
from modulo.db.models.environment_profile import EnvironmentProfile
from modulo.db.models.eval import Eval
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.evidence import Evidence
from modulo.db.models.journey import Journey
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.models.team import Team

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_SNAPSHOT = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
_ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000d2")

_TABLES: list[Table] = cast(
    list[Table],
    [
        Organisation.__table__,
        Pipeline.__table__,
        Account.__table__,
        Team.__table__,
        Run.__table__,
        PipelineSnapshot.__table__,
        Journey.__table__,
        EvalDefinition.__table__,
        EvalResult.__table__,
        Evidence.__table__,
        Eval.__table__,
        AuditEvent.__table__,
        AuditChainHead.__table__,
        EnvironmentProfile.__table__,
    ],
)


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
        # FAR-1711 (M1): the override's resumable-checkpoint probe reads the
        # ModuloPostgresSaver ``checkpoints`` table (runtime DDL, not an ORM
        # model) — the SQLite e2e fixture mirrors its org/thread/ns key so the
        # node-started branch can be exercised for real, with and without a
        # checkpoint row.
        await conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS checkpoints ("
            "organisation_id UUID NOT NULL, thread_id TEXT NOT NULL, "
            "checkpoint_ns TEXT NOT NULL DEFAULT '', checkpoint_id TEXT NOT NULL, "
            "PRIMARY KEY (organisation_id, thread_id, checkpoint_ns, checkpoint_id))"
        )
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


async def _seed(session: AsyncSession) -> None:
    session.add(Organisation(id=_ORG, name="test org", slug="test-org"))
    session.add(Account(id=_ACCOUNT, email="admin@example.com", display_name="admin"))
    session.add(Pipeline(id=_PIPELINE, organisation_id=_ORG, name="pipeline", account_id=_ACCOUNT, visibility="org"))
    session.add(
        PipelineSnapshot(
            id=_SNAPSHOT,
            organisation_id=_ORG,
            pipeline_id=_PIPELINE,
            snapshot_version=1,
            graph_json={"nodes": [], "edges": []},
            connector_bindings_json=[],
            schema_pins_json=[],
            prompt_pins_json=[],
            model_backend_pins_json=[],
            guardrail_pins_json=None,
            run_context_defaults={},
        )
    )
    await session.flush()


async def _seed_guardrail(session: AsyncSession, *, name: str = "no-secrets") -> None:
    guardrail_id = uuid.uuid4()
    eval_def = EvalDefinition(
        id=guardrail_id,
        organisation_id=_ORG,
        pipeline_id=_PIPELINE,
        node_id=None,
        name=name,
        eval_type="guardrail",
        config_json={
            "action": "block",
            "interception_point": "input",
            "type": "regex",
            "field": "body",
            "pattern": r"SECRET_[A-Z0-9]{8}",
        },
        failure_behaviour="block",
        account_id=_ACCOUNT,
    )
    session.add(eval_def)
    eval_row = Eval(
        id=guardrail_id,
        organisation_id=_ORG,
        pipeline_id=_PIPELINE,
        node_id=None,
        name=name,
        eval_type="guardrail",
        config_json={
            "action": "block",
            "interception_point": "input",
            "type": "regex",
            "field": "body",
            "pattern": r"SECRET_[A-Z0-9]{8}",
        },
        account_id=_ACCOUNT,
    )
    session.add(eval_row)
    await session.flush()


async def _blocked_run(session: AsyncSession) -> Run:
    """Produce a guardrail-blocked terminal run through the REAL create_run."""
    return await create_run(
        session,
        org_id=_ORG,
        pipeline_id=_PIPELINE,
        snapshot_id=_SNAPSHOT,
        trigger_type="manual",
        input_payload={"body": "leak SECRET_ABC12345"},
    )


async def _seed_root_checkpoint(session: AsyncSession, run: Run) -> None:
    """Insert the root-namespace checkpoint row a completed superstep leaves.

    Mirrors the ModuloPostgresSaver runtime-DDL key
    ``(organisation_id, thread_id, checkpoint_ns, checkpoint_id)`` — only the
    key columns matter to the override's existence probe.
    """
    await session.execute(
        text(
            "INSERT INTO checkpoints (organisation_id, thread_id, checkpoint_ns, checkpoint_id) "
            "VALUES (:oid, :tid, '', :cid)"
        ),
        {"oid": str(_ORG), "tid": run.langgraph_thread_id, "cid": uuid.uuid4().hex},
    )
    await session.flush()


async def test_override_reblock_loop_stays_terminal(session: AsyncSession):
    """A still-violating override input is refused; the run stays terminal and
    is never flipped — the re-block loop is safe through the real seam."""
    await _seed(session)
    await _seed_guardrail(session)
    run = await _blocked_run(session)
    assert run.status == "eval_failed"
    assert run.error_code == "eval_blocked"
    run_id = run.id

    with pytest.raises(GuardrailOverrideRejectedError):
        await guardrail_override(
            session,
            org_id=_ORG,
            run_id=run_id,
            input_data={"body": "still SECRET_XYZ99999"},
            actor_id=_ACTOR,
        )

    fresh = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
    assert fresh.status == "eval_failed"
    assert fresh.error_code == "eval_blocked"
    assert fresh.is_replay is False


async def test_override_journey_increments_once(session: AsyncSession):
    """Clean override flips the SAME run row to pending with is_replay=True —
    a single run record, so the lifecycle journey increments exactly once (no
    duplicate run)."""
    await _seed(session)
    await _seed_guardrail(session)
    run = await _blocked_run(session)
    run_id = run.id
    assert run.status == "eval_failed"

    override_run = await guardrail_override(
        session,
        org_id=_ORG,
        run_id=run_id,
        input_data={"body": "clean replacement text"},
        actor_id=_ACTOR,
    )
    assert override_run.id == run_id  # SAME run row — journey increments once
    assert override_run.status == "pending"
    assert override_run.error_code is None
    assert override_run.is_replay is True
    # FAR-1728: the ZERO-NODE flip does NOT set the self-heal marker (nothing
    # is resumable there; the pending-class legs recover a lost execute_run).
    assert override_run.guardrail_override_resume_pending is False
    assert override_run.input_payload == {"body": "clean replacement text"}

    # Only ONE run row exists in the table.
    rows = (await session.execute(select(Run).where(Run.id == run_id))).scalars().all()
    assert len(rows) == 1


async def test_override_node_started_run_flips_to_running_for_resume(session: AsyncSession):
    """FAR-1711 regression (real seam): a node-STARTED eval-blocked run —
    durable ``first_node_dispatched`` phase (FAR-1422) left behind by a
    node-scoped ``block`` eval that terminalized the run from a completed
    node — flips to ``running`` (the recover-node lock status the resume
    claim's stale-running arm matches), NOT ``pending``. The caller must
    re-dispatch it as ``resume_run`` (checkpoint resume, FAR-1649: never
    re-execute a node-started run); a ``pending`` flip would re-dispatch as
    ``execute_run`` and re-execute it. The zero-node control is
    ``test_override_journey_increments_once`` (NULL phase -> ``pending``).

    FAR-1711 (C1, real seam): the flip also stamps ``heartbeat_at`` one full
    claim-stale window into the past so the resume claim is IMMEDIATELY
    claimable — asserted here as the exact predicate
    ``_RESUME_CLAIM_UPDATE_SQL``'s stale-running arm evaluates. A block-time
    run keeps a FRESH heartbeat, so without the stamp the claim matches ZERO
    rows for a whole ``RUN_CLAIM_STALE_SECONDS`` window and the override is
    silently lost (see ``test_guardrail_override_resume_claim.py`` for the
    real ``claim_resume_run_async`` proof on Postgres)."""
    await _seed(session)
    await _seed_guardrail(session)
    run = await _blocked_run(session)
    run_id = run.id
    assert run.status == "eval_failed"
    # Stamp the durable node-start marker the executor leaves on a
    # node-scoped eval block (the phase never advances past it).
    run.dispatch_phase = PHASE_FIRST_NODE_DISPATCHED
    await _seed_root_checkpoint(session, run)
    block_detail = run.error_detail  # the block-time observation message

    override_run = await guardrail_override(
        session,
        org_id=_ORG,
        run_id=run_id,
        input_data={"body": "clean replacement text"},
        actor_id=_ACTOR,
    )
    assert override_run.status == "running"
    assert override_run.error_code is None
    assert override_run.is_replay is True
    # FAR-1728: the node-started flip sets the DEDICATED marker column (the
    # reconcile self-heal leg's discriminator), and leaves ``error_detail``
    # untouched — an observation column must never carry a control-flow
    # marker (the pre-0307 sentinel-string design is retired).
    assert override_run.guardrail_override_resume_pending is True
    assert override_run.error_detail == block_detail
    # Preserved through the flip — the resume claim resets it atomically.
    assert override_run.dispatch_phase == PHASE_FIRST_NODE_DISPATCHED

    fresh = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
    assert fresh.status == "running"
    # The marker survives the flush on the SAME session object.
    assert fresh.guardrail_override_resume_pending is True
    # C1: the persisted heartbeat satisfies the resume claim's stale-running
    # predicate IMMEDIATELY (``heartbeat_at < now() - run_claim_stale_seconds``).
    # Evaluated against the DB clock, spelled per-dialect: Postgres is the
    # claim SQL's own form; SQLite stores the column as an ISO string and
    # exposes the DB clock as ``datetime('now')`` (UTC), which compares
    # lexicographically in the same order.
    from datetime import UTC, datetime

    from modulo.settings import get_settings

    stale_seconds = int(get_settings().run_claim_stale_seconds)
    if session.get_bind().dialect.name == "postgresql":
        claimable_sql = text(
            "SELECT 1 FROM runs WHERE id = :rid AND status = 'running' "
            "AND heartbeat_at < now() - (:stale * interval '1 second')"
        )
    else:
        claimable_sql = text(
            "SELECT 1 FROM runs WHERE id = :rid AND status = 'running' "
            "AND heartbeat_at < datetime('now', '-' || :stale || ' seconds')"
        )
    # ``runs.id`` is a ``Uuid`` column: on SQLite it is stored in the
    # dialect's own encoding, so the raw ``:rid`` bind must go through the
    # same type processor the ORM write used (``str(run_id)`` alone would not
    # match the stored form).
    claimable = await session.execute(
        claimable_sql.bindparams(bindparam("rid", type_=sa.Uuid())),
        {"rid": run_id, "stale": stale_seconds},
    )
    assert claimable.first() is not None, "flipped row is NOT claimable by the resume claim's stale-running arm"
    assert fresh.heartbeat_at is not None
    assert (datetime.now(UTC) - fresh.heartbeat_at).total_seconds() >= stale_seconds


async def test_override_node_started_run_without_checkpoint_fails_closed(session: AsyncSession):
    """FAR-1711 (M1, real seam): a node-STARTED eval-blocked run whose thread
    carries NO root checkpoint — the non-interactive-pipeline shape, where the
    executor attaches no checkpointer (FAR-432) — is refused with
    ``GuardrailOverrideNoCheckpointError`` and left terminal ``eval_failed`` /
    ``eval_blocked``. Resuming it would silently RE-EXECUTE it from the start
    (FAR-1649), so fail closed: the block stays terminal and the remediation
    is a NEW run (re-calling the override returns the same 409)."""
    await _seed(session)
    await _seed_guardrail(session)
    run = await _blocked_run(session)
    run_id = run.id
    run.dispatch_phase = PHASE_FIRST_NODE_DISPATCHED
    await session.flush()

    with pytest.raises(GuardrailOverrideNoCheckpointError):
        await guardrail_override(
            session,
            org_id=_ORG,
            run_id=run_id,
            input_data={"body": "clean replacement text"},
            actor_id=_ACTOR,
        )

    fresh = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
    assert fresh.status == "eval_failed"
    assert fresh.error_code == "eval_blocked"
    assert fresh.is_replay is False


async def test_override_single_flight_race_loses(session: AsyncSession, monkeypatch: pytest.MonkeyPatch):
    """A concurrent override that loses the status-update race raises
    ConcurrentRecoveryError and does not clobber the winner. Simulated by
    making the status UPDATE match no row (as if another override won first)."""
    await _seed(session)
    await _seed_guardrail(session)
    run = await _blocked_run(session)
    run_id = run.id

    # After the pass succeeds, make the optimistic status UPDATE match no row
    # (the winner already flipped it) by intercepting the UPDATE statement.
    original_execute = session.execute
    real_update_seen = False

    async def _fake_execute(statement, *args, **kwargs):
        nonlocal real_update_seen
        compiled = str(statement)
        if "UPDATE runs SET status=" in compiled and "RETURNING" in compiled:
            real_update_seen = True

            class _LostRace:
                def scalar_one_or_none(self):
                    return None

            return _LostRace()
        return await original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(session, "execute", _fake_execute)

    with pytest.raises(ConcurrentRecoveryError):
        await guardrail_override(
            session,
            org_id=_ORG,
            run_id=run_id,
            input_data={"body": "clean replacement text"},
            actor_id=_ACTOR,
        )
    assert real_update_seen is True


# ---------------------------------------------------------------------------
# REAL POSTGRES — FAR-1728 marker durability (fix 2)
# ---------------------------------------------------------------------------

_PG_URL_ENV = "GUARDRAIL_OVERRIDE_TEST_PG_URL"

# The ModuloPostgresSaver runtime-DDL root-checkpoint key — only the columns
# the override's existence probe and the claim touch. ``run_number_counters``
# is migration-only DDL (no ORM model) that ``create_run``'s Postgres
# allocation path writes to.
_PG_CHECKPOINT_DDL = text(
    "CREATE TABLE IF NOT EXISTS checkpoints ("
    "organisation_id UUID NOT NULL, thread_id TEXT NOT NULL, "
    "checkpoint_ns TEXT NOT NULL DEFAULT '', checkpoint_id TEXT NOT NULL, "
    "PRIMARY KEY (organisation_id, thread_id, checkpoint_ns, checkpoint_id))"
)
_PG_RUN_NUMBER_COUNTER_DDL = text(
    "CREATE TABLE IF NOT EXISTS run_number_counters ("
    "organisation_id UUID PRIMARY KEY, next_run_number INTEGER NOT NULL DEFAULT 1)"
)

# The reconcile leg's stale window (2 * SAQ_JOB_HEARTBEAT at the default) —
# the SQL predicate's heartbeat gate. The flip stamps the heartbeat one
# CLAIM-stale window into the past (immediately claimable by the resume claim,
# which is a SHORTER window); the reconcile leg only selects once the heartbeat
# is stale past ITS window, i.e. once the lost-job state has aged. The test
# ages the heartbeat explicitly to simulate that passage of time — the
# subject under test is marker durability + re-selection, not the timing.
_RECONCILE_STALE_WINDOW = 600


async def _seed_pg(session: AsyncSession) -> None:
    """Per-table flushes in dependency order — Postgres refuses a single bulk
    flush on a fresh schema (SQLite, with FKs off, never surfaces this)."""
    already = (await session.execute(sa.select(Organisation.id).where(Organisation.id == _ORG))).scalar_one_or_none()
    if already is not None:
        # The class-scoped async fixture is re-instantiated per test under the
        # suite's session loop scope — seed once, then no-op.
        return
    session.add(Organisation(id=_ORG, name="override-marker org", slug="override-marker-org"))
    await session.flush()
    session.add(Account(id=_ACCOUNT, email="ops@example.com", display_name="ops"))
    await session.flush()
    # The override's audit event carries the acting account id — seed it too
    # so the (fail-open) audit write does not log a foreign-key violation.
    session.add(Account(id=_ACTOR, email="actor@example.com", display_name="actor"))
    await session.flush()
    session.add(Pipeline(id=_PIPELINE, organisation_id=_ORG, name="pipeline", account_id=_ACCOUNT, visibility="org"))
    await session.flush()
    session.add(
        PipelineSnapshot(
            id=_SNAPSHOT,
            organisation_id=_ORG,
            pipeline_id=_PIPELINE,
            snapshot_version=1,
            graph_json={"nodes": [], "edges": []},
            connector_bindings_json=[],
            schema_pins_json=[],
            prompt_pins_json=[],
            model_backend_pins_json=[],
            guardrail_pins_json=None,
            run_context_defaults={},
        )
    )
    await session.flush()


# Module-level (never class-scoped instance methods — pytest 9 treats those as
# a removed-in-10 error; same shape as the resume-claim proof's fixtures).
@pytest.fixture(scope="class")
async def pg_url() -> AsyncIterator[str]:
    """Throwaway Postgres URL (see ``TestRealPostgresMarkerDurability``)."""
    env_url = os.environ.get(_PG_URL_ENV)
    if env_url:
        if env_url.startswith("postgresql://"):
            env_url = env_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        yield env_url
        return

    try:
        from testcontainers.community.postgres import PostgresContainer
    except Exception as exc:  # capability-gated skip (no driver / no Docker), never a silent pass
        pytest.skip(f"testcontainers Postgres unavailable (driver missing): {exc}")

    try:
        container = PostgresContainer("postgres:16-alpine")
        container.start()
    except Exception as exc:  # capability-gated skip (no driver / no Docker), never a silent pass
        pytest.skip(f"testcontainers Postgres unavailable (no Docker): {exc}")
    try:
        raw = container.get_connection_url()
        url = re.sub(r"^postgresql(\+[a-z0-9]+)?://", "postgresql+asyncpg://", raw, flags=re.IGNORECASE)
        yield url
    finally:
        container.stop()


@pytest.fixture(scope="class")
async def pg_engine(pg_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(pg_url)
    async with engine.begin() as conn:
        # FULL metadata, not a hand-picked subset: the ORM write paths reach
        # tables whose FKs cross the ones this test asserts on (``create_run``
        # upserts ``journeys``, whose FK references ``lifecycle_maps``), and
        # Postgres — unlike SQLite — refuses to create a table whose referenced
        # table is absent.
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn))
        await conn.execute(_PG_CHECKPOINT_DDL)
        await conn.execute(_PG_RUN_NUMBER_COUNTER_DDL)
    yield engine
    await engine.dispose()


class TestRealPostgresMarkerDurability:
    """FAR-1728 fix 2: the marker is a real, durable COLUMN, proven on Postgres.

    The throwaway Postgres is supplied the same way the resume-claim proof
    (``tests/unit/pipeline_engine/test_guardrail_override_resume_claim.py``)
    does it: an externally-provisioned DB named by
    ``GUARDRAIL_OVERRIDE_TEST_PG_URL``, else a testcontainers
    ``PostgresContainer`` spun up on demand — capability-gated (skipped where
    Docker is absent), never coverage-gated. The schema comes from the REAL
    ORM metadata (``Base.metadata.create_all``), so the model column this test
    exercises is the same one migration 0307 adds.
    """

    async def test_marker_is_durable_reselected_and_cleared_on_claim(self, pg_engine: AsyncEngine) -> None:
        """The durability chain, end to end, on a REAL Postgres:

        1. a node-started ``eval_blocked`` run is flipped by the REAL
           ``guardrail_override``;
        2. after COMMIT, a FRESH session reads the row back and finds
           ``guardrail_override_resume_pending`` TRUE (durable persistence —
           not an in-memory ORM assertion) while ``error_detail`` still
           carries the block-time observation message, never a control-flow
           marker;
        3. the reconcile leg's REAL SQL predicate
           (``_build_re_dispatch_predicate``) re-selects the row once its
           heartbeat has aged past the reconcile stale window (the lost-job
           state), and the row-side recheck
           (``_is_override_resume_row``) agrees;
        4. the REAL ``claim_resume_run_async`` claims the row and CLEARS the
           marker in the same UPDATE (single clearer), proven by a second
           fresh-session read-back.

        Fail-without-fix: on a tree without the dedicated column the flip
        cannot set it (no attribute) and the predicate cannot reference it —
        every assertion on the marker fails.
        """
        maker = async_sessionmaker(pg_engine, expire_on_commit=False)

        # -- seed + blocked node-started run + checkpoint --------------------
        async with maker() as seed_session:
            await _seed_pg(seed_session)
            await _seed_guardrail(seed_session)
            run = await _blocked_run(seed_session)
            run_id = run.id
            run.dispatch_phase = PHASE_FIRST_NODE_DISPATCHED
            await _seed_root_checkpoint(seed_session, run)
            block_detail = run.error_detail  # the block-time observation message
            assert run.status == "eval_failed"
            await seed_session.commit()

        # -- the flip (real seam) --------------------------------------------
        async with maker() as session:
            flipped = await guardrail_override(
                session,
                org_id=_ORG,
                run_id=run_id,
                input_data={"body": "clean replacement text"},
                actor_id=_ACTOR,
            )
            assert flipped.status == "running"
            await session.commit()

        # -- durable read-back in a FRESH session ----------------------------
        async with maker() as session:
            fresh = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
            assert fresh.guardrail_override_resume_pending is True, (
                "the override flip's marker must be durably persisted on read-back after commit"
            )
            # The observation column is untouched — never a control-flow marker.
            assert fresh.error_detail == block_detail
            # Mirror the lost-job state this self-heal leg exists for: the
            # override route reached ``dispatch_run`` (which stamps
            # ``dispatcher='saq'`` + ``dispatched_at`` as part of the enqueue)
            # and the SAQ job was then LOST — and age the heartbeat past the
            # reconcile stale window (the flip's own stamp makes the row
            # IMMEDIATELY claimable, which is a shorter window).
            await session.execute(
                sa.update(Run)
                .where(Run.id == run_id)
                .values(
                    dispatcher="saq",
                    dispatched_at=datetime.now(UTC),
                    heartbeat_at=datetime.now(UTC) - timedelta(seconds=_RECONCILE_STALE_WINDOW + 60),
                )
            )
            await session.commit()

        # -- the reconcile leg re-selects the row (real predicate + recheck) --
        async with maker() as session:
            predicate = cron_helpers._build_re_dispatch_predicate(
                reenqueue_window=600,
                stale_window=_RECONCILE_STALE_WINDOW,
                capacity_redispatch_seconds=120,
            )
            selected = (await session.execute(select(Run).where(Run.id == run_id, predicate))).scalars().all()
            assert len(selected) == 1, (
                "the reconcile recovery predicate must re-select the flipped row once its heartbeat is stale"
            )
            row = selected[0]
            assert cron_helpers._is_override_resume_row(row, _RECONCILE_STALE_WINDOW) is True

        # -- the resume claim clears the marker (single clearer) --------------
        token = await claim_resume_run_async(pg_engine, str(run_id), str(_ORG))
        assert token is not None, "the flipped row must be claimable by the real resume claim"

        async with maker() as session:
            claimed = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
            assert claimed.dispatch_phase == "claimed"
            assert claimed.guardrail_override_resume_pending is False, (
                "the resume claim must clear the marker in the same UPDATE (single clearer)"
            )
            # And the row has left the self-heal class: the same predicate no
            # longer selects it (phase is 'claimed', marker false).
            predicate = cron_helpers._build_re_dispatch_predicate(
                reenqueue_window=600,
                stale_window=_RECONCILE_STALE_WINDOW,
                capacity_redispatch_seconds=120,
            )
            still = (await session.execute(select(Run.id).where(Run.id == run_id, predicate))).scalars().all()
            assert not still, "a claimed row must never re-match the self-heal leg"
