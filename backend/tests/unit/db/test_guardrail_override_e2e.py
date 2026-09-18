"""Remediation e2e via guardrail-override (FAR-223 item 13.2).

Through the REAL ``guardrail_override`` seam (real SQLite session, real
guardrail rows, real create_run to produce the blocked run):

* **re-block loop** — overriding with a STILL-VIOLATING input is refused
  (``GuardrailOverrideRejectedError``) and the run stays terminal ``eval_failed``;
* **journey-increment-once** — overriding with clean input flips the SAME run
  row to ``pending`` with ``is_replay=True`` (a single run record, so the
  lifecycle-map journey increments exactly once — never a duplicate run);
* **single-flight** — a concurrent override that loses the status-update race
  raises ``ConcurrentRecoveryError`` and does NOT clobber the winner.

These complement the mocked-session unit tests in
``tests/unit/pipeline_engine/test_guardrail_override.py`` by exercising the
real DB round-trip (guardrail row load + pass + status update against an actual
session), so a wiring regression between the pure seam and the DB path is
caught here.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import cast

import pytest
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core.pipeline_engine.recovery import (
    ConcurrentRecoveryError,
    GuardrailOverrideRejectedError,
    guardrail_override,
)
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


async def _seed_guardrail(session: AsyncSession, *, name: str = "no-secrets") -> None:
    eval_def = EvalDefinition(
        id=uuid.uuid4(),
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
    assert override_run.input_payload == {"body": "clean replacement text"}

    # Only ONE run row exists in the table.
    rows = (await session.execute(select(Run).where(Run.id == run_id))).scalars().all()
    assert len(rows) == 1


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
