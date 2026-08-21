"""Fault-injection suite + zero-impact normalized projection (FAR-223 item 13.3/13.4).

Proves two things through the REAL create_run seam:

13.3 **Fault-injection parity** — a pipeline with guardrails BOUND but NO
     violation behaves IDENTICALLY to a pipeline with NO guardrails: same run
     status, same post-redaction payload (no mutation on clean input), same
     absence of block evidence. The guardrail pass is a no-op on clean input.

13.4 **Zero-impact normalized projection** — guardrail interception adds zero
     observable impact to a clean run:
       * no node budget consumed (``node_attempt_count`` / ``claim_count``
         untouched — the pass runs BEFORE the first node and does not burn the
         node timeout budget);
       * no extra eval results for clean input beyond the single guardrail
         detection row (the pass is observable but not additive);
       * ``guardrail_summary`` shows the guardrail evaluated with zero
         violations (``violated == 0``) when a summary is present.

Together these assert the interception pass is a pure, side-effect-free read
on clean input — it neither mutates the persisted payload nor consumes node
execution budget.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import cast

import pytest
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.run import create_run
from modulo.db.models.account import Account
from modulo.db.models.audit_event import AuditChainHead, AuditEvent
from modulo.db.models.base import Base
from modulo.db.models.environment_profile import EnvironmentProfile
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.journey import Journey
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.models.team import Team

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_PIPELINE_NO_GR = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
_SNAPSHOT = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
_SNAPSHOT_NO_GR = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000d1")

_CLEAN_PAYLOAD = {"body": "clean text with no secret"}

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
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


def _seed_pipeline(session: AsyncSession, *, pipeline_id: uuid.UUID, snapshot_id: uuid.UUID) -> None:
    session.add(Pipeline(id=pipeline_id, organisation_id=_ORG, name="pipeline", account_id=_ACCOUNT, visibility="org"))
    session.add(
        PipelineSnapshot(
            id=snapshot_id,
            organisation_id=_ORG,
            pipeline_id=pipeline_id,
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


async def _seed(session: AsyncSession) -> None:
    session.add(Organisation(id=_ORG, name="test org", slug="test-org"))
    session.add(Account(id=_ACCOUNT, email="admin@example.com", display_name="admin"))
    _seed_pipeline(session, pipeline_id=_PIPELINE, snapshot_id=_SNAPSHOT)
    _seed_pipeline(session, pipeline_id=_PIPELINE_NO_GR, snapshot_id=_SNAPSHOT_NO_GR)
    await session.flush()


async def _seed_guardrail(session: AsyncSession, *, name: str, action: str, pipeline_id: uuid.UUID) -> None:
    eval_def = EvalDefinition(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        pipeline_id=pipeline_id,
        node_id=None,
        name=name,
        eval_type="guardrail",
        config_json={
            "action": action,
            "interception_point": "input",
            "type": "regex",
            "field": "body",
            "pattern": r"SECRET_[A-Z0-9]{8}",
        },
        failure_behaviour="block" if action in ("block", "redact") else "warn",
        account_id=_ACCOUNT,
    )
    session.add(eval_def)
    await session.flush()


async def _create(session: AsyncSession, *, pipeline_id: uuid.UUID, snapshot_id: uuid.UUID) -> Run:
    return await create_run(
        session,
        org_id=_ORG,
        pipeline_id=pipeline_id,
        snapshot_id=snapshot_id,
        trigger_type="manual",
        input_payload=dict(_CLEAN_PAYLOAD),
    )


async def _eval_rows(session: AsyncSession, run_id: uuid.UUID) -> list[EvalResult]:
    return (await session.execute(select(EvalResult).where(EvalResult.run_id == run_id))).scalars().all()


# ---------------------------------------------------------------------------
# 13.3 — fault-injection parity: clean input, guardrails bound vs not
# ---------------------------------------------------------------------------


async def test_clean_run_with_guardrails_identical_to_no_guardrails(session: AsyncSession):
    """A clean run with bound guardrails has the SAME observable outcome as a
    clean run with no guardrails: pending, no block, no redaction, no node
    execution, identical persisted payload."""
    await _seed(session)
    await _seed_guardrail(session, name="no-secrets", action="block", pipeline_id=_PIPELINE)

    with_gr = await _create(session, pipeline_id=_PIPELINE, snapshot_id=_SNAPSHOT)
    without_gr = await _create(session, pipeline_id=_PIPELINE_NO_GR, snapshot_id=_SNAPSHOT_NO_GR)

    # Identical status + no block.
    assert with_gr.status == "pending"
    assert without_gr.status == "pending"
    assert with_gr.error_code is None
    assert without_gr.error_code is None

    # Identical persisted payload (guardrail is a no-op on clean input).
    assert with_gr.input_payload == _CLEAN_PAYLOAD
    assert without_gr.input_payload == _CLEAN_PAYLOAD

    # Identical no-node-execution footprint (neither run was dispatched).
    for run in (with_gr, without_gr):
        assert run.started_at is None
        assert run.outputs_json is None
        assert run.node_telemetry_json is None
        assert run.claim_count == 0
        assert run.node_attempt_count == 0


# ---------------------------------------------------------------------------
# 13.4 — zero-impact normalized projection
# ---------------------------------------------------------------------------


async def test_clean_run_guardrail_budget_untouched(session: AsyncSession):
    """The guardrail pass consumes NO node budget: claim_count and
    node_attempt_count stay 0 on a clean guarded run (the interception runs
    BEFORE the first node and never eats the node timeout budget)."""
    await _seed(session)
    await _seed_guardrail(session, name="no-secrets", action="block", pipeline_id=_PIPELINE)
    run = await _create(session, pipeline_id=_PIPELINE, snapshot_id=_SNAPSHOT)
    assert run.status == "pending"
    assert run.claim_count == 0
    assert run.node_attempt_count == 0


async def test_clean_run_guardrail_detection_is_observable_but_not_additive(session: AsyncSession):
    """On a clean guarded run there is exactly ONE eval result (the guardrail's
    clean detection) — no extra block evidence, no additive side effects. The
    pass is observable (the detection row exists) but not additive."""
    await _seed(session)
    await _seed_guardrail(session, name="no-secrets", action="block", pipeline_id=_PIPELINE)
    run = await _create(session, pipeline_id=_PIPELINE, snapshot_id=_SNAPSHOT)
    rows = await _eval_rows(session, run.id)
    assert len(rows) == 1
    assert rows[0].passed is False  # clean regex = no match = no violation


async def test_clean_run_guardrail_summary_shows_zero_violations(session: AsyncSession):
    """When a summary is present on a clean run, it shows the guardrail
    evaluated with ZERO violations (``violated == 0``) — the normalized
    projection that the guardrail added no negative impact."""
    await _seed(session)
    await _seed_guardrail(session, name="no-secrets", action="block", pipeline_id=_PIPELINE)
    run = await _create(session, pipeline_id=_PIPELINE, snapshot_id=_SNAPSHOT)
    assert run.guardrail_summary_json is not None
    summary = cast(dict[str, int], run.guardrail_summary_json)
    assert summary["bound"] == 1
    assert summary["evaluated"] == 1
    assert summary["violated"] == 0
    assert summary["passed"] == 1
    # Invariant: evaluated + errored + skipped == bound
    assert summary["evaluated"] + summary["errored"] + summary["skipped"] == summary["bound"]
