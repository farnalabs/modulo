"""FAR-1711 C1/M1: the override's re-dispatch is provably claimable (real seam).

Two independent proofs the pre-PR QA gate demanded:

* **C1 (real Postgres)** — apply the REAL ``guardrail_override`` to a
  node-started (``first_node_dispatched``) eval-blocked run, then call the
  REAL :func:`modulo.core.pipeline_execution.claim_resume_run_async` against
  the same throwaway Postgres and assert a claim token IS returned. Before
  the fix the flip left a FRESH block-time ``heartbeat_at``, so the claim's
  stale-running arm (``heartbeat_at < now() - run_claim_stale_seconds``)
  matched ZERO rows, ``resume_run`` returned ``{"status": "not_claimed"}``
  (success to SAQ — no retry) and the override was silently lost. This is
  deliberately NOT a MagicMock that pre-sets ``status="running"``: the flip,
  the heartbeat stamp and the claim all run against a real database.

* **M1 (premise + fail-closed)** — a node-started run in a NON-interactive
  pipeline has no checkpoint (FAR-432 attaches no checkpointer there), and a
  resume of it does not fail loudly: LangGraph seeds a fresh checkpoint and
  RE-EXECUTES every node (the FAR-1649/FAR-1711 sin). The premise test below
  pins that behaviour against the installed LangGraph, and the Postgres class
  pins the override's fail-closed refusal (``GuardrailOverrideNoCheckpointError``,
  run left terminal).

The Postgres is supplied the same way the mint-budget row-lock proof does it:
an externally-provisioned throwaway DB named by
``GUARDRAIL_OVERRIDE_TEST_PG_URL``, else a testcontainers ``PostgresContainer``
spun up on demand — capability-gated (skipped where Docker is absent), never
coverage-gated.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.dispatch_phases import PHASE_FIRST_NODE_DISPATCHED
from modulo.core.pipeline_engine.recovery import GuardrailOverrideNoCheckpointError, guardrail_override
from modulo.core.pipeline_execution import claim_resume_run_async
from modulo.db.crud.run import create_run
from modulo.db.models.account import Account
from modulo.db.models.base import Base
from modulo.db.models.eval import Eval
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run

_PG_URL_ENV = "GUARDRAIL_OVERRIDE_TEST_PG_URL"

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000011")
_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
_SNAPSHOT = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000d3")
_ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000d4")

# The ModuloPostgresSaver runtime-DDL root-checkpoint key — only the columns
# the override's existence probe and the claim touch. ``run_number_counters``
# is migration-only DDL (no ORM model) that ``create_run``'s Postgres
# allocation path writes to.
_CHECKPOINT_DDL = text(
    "CREATE TABLE IF NOT EXISTS checkpoints ("
    "organisation_id UUID NOT NULL, thread_id TEXT NOT NULL, "
    "checkpoint_ns TEXT NOT NULL DEFAULT '', checkpoint_id TEXT NOT NULL, "
    "PRIMARY KEY (organisation_id, thread_id, checkpoint_ns, checkpoint_id))"
)
_RUN_NUMBER_COUNTER_DDL = text(
    "CREATE TABLE IF NOT EXISTS run_number_counters ("
    "organisation_id UUID PRIMARY KEY, next_run_number INTEGER NOT NULL DEFAULT 1)"
)


# ---------------------------------------------------------------------------
# M1 premise: what a resume of a no-checkpoint run ACTUALLY does
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_langgraph_resume_without_a_checkpoint_reexecutes_every_node() -> None:
    """The FAR-1711 M1 premise, pinned against the installed LangGraph.

    ``executor.resume`` attaches a checkpointer and calls ``aupdate_state``
    then ``astream_events(None, ...)``. When the thread has NO prior
    checkpoint (a non-interactive pipeline never persisted one), LangGraph
    does NOT raise ``InvalidUpdateError``: ``_infer_as_node`` resolves the
    fresh thread to ``__start__``, a new checkpoint is seeded, and the stream
    runs EVERY node again. That silent re-execution of a node-started run is
    the exact FAR-1649/FAR-1711 sin — which is why ``guardrail_override``
    refuses such a run (``GuardrailOverrideNoCheckpointError``) instead of
    dispatching ``resume_run``. This test fails (no side effects observed) if
    LangGraph ever starts failing loudly instead, so the fail-closed branch's
    justification stays honest.
    """
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from typing_extensions import TypedDict

    class _State(TypedDict):
        x: int

    executed: list[str] = []

    def _node_a(state: _State) -> dict[str, int]:
        executed.append("a")
        return {"x": state.get("x", 0) + 1}

    def _node_b(state: _State) -> dict[str, int]:
        executed.append("b")
        return {"x": state.get("x", 0) + 1}

    builder = StateGraph(_State)
    builder.add_node("a", _node_a)
    builder.add_node("b", _node_b)
    builder.add_edge(START, "a")
    builder.add_edge("a", "b")
    builder.add_edge("b", END)
    compiled = builder.compile()

    # Mirror the resume path: a checkpointer attached at resume time on a
    # thread that never had one during execute.
    compiled.checkpointer = InMemorySaver()
    config = {"configurable": {"thread_id": f"{_ORG}:{uuid.uuid4()}"}}
    await compiled.aupdate_state(config, {"_hitl_decision": {}, "_claim_lease": "tok"})
    async for _event in compiled.astream_events(None, config, version="v2"):
        pass

    # PT024: the message goes in ``pytest.fail`` — an assertion message is
    # for comparison diagnostics, not fixed explanations.
    if executed != ["a", "b"]:
        pytest.fail(
            "a no-checkpoint resume must re-execute the whole graph (the FAR-1649 sin); "
            f"executed nodes were {executed!r}"
        )


# ---------------------------------------------------------------------------
# C1 + M1 against a real Postgres
# ---------------------------------------------------------------------------


@pytest.fixture(scope="class")
async def pg_url() -> AsyncIterator[str]:
    """Throwaway Postgres URL (see the module docstring for the two strategies)."""
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
        await conn.execute(_CHECKPOINT_DDL)
        await conn.execute(_RUN_NUMBER_COUNTER_DDL)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="class")
async def seeded_engine(pg_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """Class-scoped: seed the org/pipeline/guardrail universe ONCE.

    Both tests share the same fixture UUIDs, so per-test re-seeding would
    collide on the primary keys; the second test needs the rows to exist too
    (``create_run`` refuses an unknown organisation).
    """
    maker = _session_maker(pg_engine)
    async with maker() as session:
        await _seed(session)
        await session.commit()
    return pg_engine


def _session_maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed(session: AsyncSession) -> None:
    already = (await session.execute(sa.select(Organisation.id).where(Organisation.id == _ORG))).scalar_one_or_none()
    if already is not None:
        # The class-scoped async fixture is re-instantiated per test under the
        # suite's function loop scope — seed once, then no-op.
        return
    # Explicit per-table flushes (dependency order): a single bulk flush on a
    # fresh schema is free to emit the ``evals`` insert before its referenced
    # ``pipelines`` row lands, which Postgres rejects with a foreign-key
    # violation (SQLite, with FKs off, never surfaces this).
    session.add(Organisation(id=_ORG, name="override-claim org", slug="override-claim-org"))
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
    config = {
        "action": "block",
        "interception_point": "input",
        "type": "regex",
        "field": "body",
        "pattern": r"SECRET_[A-Z0-9]{8}",
    }
    session.add(
        EvalDefinition(
            id=uuid.uuid4(),
            organisation_id=_ORG,
            pipeline_id=_PIPELINE,
            node_id=None,
            name="no-secrets",
            eval_type="guardrail",
            config_json=config,
            failure_behaviour="block",
            account_id=_ACCOUNT,
        )
    )
    session.add(
        Eval(
            id=uuid.uuid4(),
            organisation_id=_ORG,
            pipeline_id=_PIPELINE,
            node_id=None,
            name="no-secrets",
            eval_type="guardrail",
            config_json=config,
            account_id=_ACCOUNT,
        )
    )
    await session.flush()


async def _node_started_blocked_run(session: AsyncSession) -> uuid.UUID:
    run = await create_run(
        session,
        org_id=_ORG,
        pipeline_id=_PIPELINE,
        snapshot_id=_SNAPSHOT,
        trigger_type="manual",
        input_payload={"body": "leak SECRET_ABC12345"},
    )
    run.dispatch_phase = PHASE_FIRST_NODE_DISPATCHED
    await session.flush()
    return run.id


async def _seed_root_checkpoint(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Insert the root-namespace checkpoint a completed superstep leaves."""
    thread = (await session.execute(sa.select(Run.langgraph_thread_id).where(Run.id == run_id))).scalar_one()
    await session.execute(
        text(
            "INSERT INTO checkpoints (organisation_id, thread_id, checkpoint_ns, checkpoint_id) "
            "VALUES (:oid, :tid, '', :cid)"
        ),
        {"oid": str(_ORG), "tid": thread, "cid": uuid.uuid4().hex},
    )


class TestRealPostgresResumeClaim:
    """C1 + M1 against a throwaway Postgres (see the module docstring)."""

    async def test_override_flip_is_immediately_claimable_by_the_real_claim(self, seeded_engine: AsyncEngine) -> None:
        """C1/M3: the flipped row is claimed by the REAL
        :func:`claim_resume_run_async` on the first attempt — a token is
        returned, never ``None``.

        This is the C1 regression guard: without the ``heartbeat_at`` stamp
        the same claim matches zero rows and returns ``None``."""
        maker = _session_maker(seeded_engine)
        async with maker() as session:
            await _seed(session)
            run_id = await _node_started_blocked_run(session)
            await _seed_root_checkpoint(session, run_id)
            flipped = await guardrail_override(
                session,
                org_id=_ORG,
                run_id=run_id,
                input_data={"body": "clean replacement text"},
                actor_id=_ACTOR,
            )
            assert flipped.status == "running"
            await session.commit()

        token = await claim_resume_run_async(seeded_engine, str(run_id), str(_ORG))
        assert token is not None, "the override flip left the run UNCLAIMABLE (C1: heartbeat was not stamped stale)"
        assert isinstance(token, str)
        assert token

        # The claim actually moved the row: phase reset + fresh heartbeat.
        async with maker() as session:
            claimed = (await session.execute(sa.select(Run).where(Run.id == run_id))).scalar_one()
            assert claimed.status == "running"
            assert claimed.dispatch_phase == "claimed"
            assert claimed.heartbeat_at is not None

    async def test_node_started_run_without_checkpoint_is_refused_and_stays_terminal(
        self, seeded_engine: AsyncEngine
    ) -> None:
        """M1 (real seam): no root checkpoint -> fail closed with
        ``GuardrailOverrideNoCheckpointError``; the run is left the terminal
        ``eval_failed``/``eval_blocked`` block it was, so it stays overridable
        and is never handed to a re-executing resume."""
        maker = _session_maker(seeded_engine)
        async with maker() as session:
            run_id = await _node_started_blocked_run(session)
            # NO checkpoint row for this thread — the non-interactive-pipeline
            # shape (FAR-432 attaches no checkpointer there).
            await session.commit()

        async with maker() as session:
            with pytest.raises(GuardrailOverrideNoCheckpointError):
                await guardrail_override(
                    session,
                    org_id=_ORG,
                    run_id=run_id,
                    input_data={"body": "clean replacement text"},
                    actor_id=_ACTOR,
                )
            await session.commit()

        async with maker() as session:
            refused = (await session.execute(sa.select(Run).where(Run.id == run_id))).scalar_one()
            assert refused.status == "eval_failed"
            assert refused.error_code == "eval_blocked"
            assert refused.is_replay is False
