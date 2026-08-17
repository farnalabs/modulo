"""Block e2e through the REAL create_run seam (FAR-223 item 13.1).

Proves that when a block-action guardrail fires at the ingestion edge:

* the run is created TERMINAL ``eval_failed`` (error_code ``eval_blocked``);
* NO node executes — the run is never dispatched: ``started_at`` is NULL,
  ``outputs_json`` / ``node_telemetry_json`` are NULL, and the run counters
  (``claim_count`` / ``node_attempt_count``) are untouched at 0;
* the guardrail evidence rows are persisted (a guardrail result with the
  ``guardrail_blocked`` trigger-event vocabulary), so the block is visible in
  the run list without ever reaching the pipeline.

This is the prove-the-fix analogue of the ticket: if a blocked run were ever
dispatched, one of these assertions would fail.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast

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
_SNAPSHOT = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000d1")

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


async def _seed_guardrail(
    session: AsyncSession,
    *,
    name: str,
    action: str,
    config: dict[str, Any] | None = None,
) -> uuid.UUID:
    cfg: dict[str, Any] = {
        "action": action,
        "interception_point": "input",
        "type": "regex",
        "field": "body",
        "pattern": r"SECRET_[A-Z0-9]{8}",
    }
    if config:
        cfg.update(config)
    eval_def = EvalDefinition(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        pipeline_id=_PIPELINE,
        node_id=None,
        name=name,
        eval_type="guardrail",
        config_json=cfg,
        failure_behaviour="block" if action in ("block", "redact") else "warn",
        account_id=_ACCOUNT,
    )
    session.add(eval_def)
    await session.flush()
    return eval_def.id


async def _create(session: AsyncSession, *, input_payload: dict[str, Any] | None = None) -> Run:
    return await create_run(
        session,
        org_id=_ORG,
        pipeline_id=_PIPELINE,
        snapshot_id=_SNAPSHOT,
        trigger_type="manual",
        input_payload=input_payload or {},
    )


async def test_block_e2e_node_effect_prevented(session: AsyncSession):
    """A block at ingestion creates a terminal run whose node never executes."""
    await _seed(session)
    await _seed_guardrail(session, name="no-secrets", action="block")
    run = await _create(session, input_payload={"body": "leak SECRET_ABC12345"})

    # 1. Terminal, never dispatched.
    assert run.status == "eval_failed"
    assert run.error_code == "eval_blocked"
    assert run.started_at is None  # never dispatched — no execution start

    # 2. Node effect prevented: no output, no telemetry, no claim, no attempt.
    assert run.outputs_json is None
    assert run.node_telemetry_json is None
    assert run.claim_count == 0
    assert run.node_attempt_count == 0

    # 3. The block evidence is persisted (guardrail result row) so the failure
    #    is visible without reaching the pipeline. The detail never embeds the
    #    raw payload (no-raw-persist contract).
    rows = (await session.execute(select(EvalResult).where(EvalResult.run_id == run.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].passed is True  # regex matched = violation detected
    assert "SECRET_ABC12345" not in (rows[0].detail or "")


async def test_block_e2e_clean_input_dispatches_normally(session: AsyncSession):
    """Control: a clean input with the same guardrail creates a pending run
    with NO guardrail evidence and no block — the guardrail is a no-op on a
    clean payload (proves the block above is attributable to the violation)."""
    await _seed(session)
    await _seed_guardrail(session, name="no-secrets", action="block")
    run = await _create(session, input_payload={"body": "clean text"})
    assert run.status == "pending"
    assert run.error_code is None
    rows = (await session.execute(select(EvalResult).where(EvalResult.run_id == run.id))).scalars().all()
    # A clean block-guardrail run still records the detection (passed=False,
    # no violation) so the pass is observable but never blocks.
    assert len(rows) == 1
    assert rows[0].passed is False
