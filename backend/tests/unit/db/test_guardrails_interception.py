"""create_run guardrail interception (FAR-208 item 2) against in-memory SQLite.

These tests exercise the REAL ``create_run`` path (no mocks of the function
under test) and assert the ingestion-edge seam:

  * zero-guardrail fast path — a normal run is created ``pending`` when no
    guardrails are bound;
  * redaction — a redact-action guardrail's static field policy is applied to
    the PERSISTED input_payload (persisted state is post-redaction) and the
    raw value never reaches the run record;
  * block — a block-action guardrail fires → the run is created TERMINAL
    ``eval_failed`` (error_code ``eval_blocked``), never dispatched, and the
    eval_results evidence rows are persisted;
  * replay detection-only — an ``is_replay=True`` run with a violating payload
    is created ``pending`` (no act, no re-block) with detection results.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.run import create_run
from modulo.db.models.account import Account
from modulo.db.models.base import Base
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.journey import Journey
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
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
        Journey.__table__,
        EvalDefinition.__table__,
        EvalResult.__table__,
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


async def _create(
    session: AsyncSession,
    *,
    input_payload: dict[str, Any] | None = None,
    is_replay: bool | None = None,
    trigger_type: str = "manual",
) -> Run:
    return await create_run(
        session,
        org_id=_ORG,
        pipeline_id=_PIPELINE,
        snapshot_id=_SNAPSHOT,
        trigger_type=trigger_type,
        input_payload=input_payload or {},
        is_replay=is_replay,
    )


async def test_create_run_zero_guardrail_fast_path(session: AsyncSession):
    await _seed(session)
    run = await _create(session, input_payload={"body": "no guards bound"})
    assert run.status == "pending"
    assert run.input_payload == {"body": "no guards bound"}


async def test_create_run_redact_action_masks_persisted_payload(session: AsyncSession):
    await _seed(session)
    await _seed_guardrail(
        session,
        name="redact-key",
        action="redact",
        config={"redaction": [{"path": "credentials.api_key", "mode": "transform"}]},
    )
    run = await _create(
        session,
        input_payload={"credentials": {"api_key": "sk-live-123"}, "body": "clean"},
    )
    # Persisted state is post-redaction — the raw value never reaches the run.
    assert run.status == "pending"
    assert run.input_payload["credentials"]["api_key"] == "\u2022\u2022\u2022\u2022\u2022\u2022"
    assert run.input_payload["body"] == "clean"


async def test_create_run_block_action_creates_terminal_eval_failed(session: AsyncSession):
    await _seed(session)
    guardrail_id = await _seed_guardrail(session, name="no-secrets", action="block")
    run = await _create(
        session,
        input_payload={"body": "leak SECRET_ABC12345"},
    )
    assert run.status == "eval_failed"
    assert run.error_code == "eval_blocked"
    assert "no-secrets" in (run.error_detail or "")
    # Evidence rows persisted with detail never carrying the raw payload.
    rows = (await session.execute(select(EvalResult).where(EvalResult.run_id == run.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].eval_id == guardrail_id
    assert "SECRET_ABC12345" not in (rows[0].detail or "")


async def test_create_run_clean_payload_not_blocked(session: AsyncSession):
    await _seed(session)
    await _seed_guardrail(session, name="no-secrets", action="block")
    run = await _create(session, input_payload={"body": "clean text"})
    assert run.status == "pending"
    assert run.input_payload == {"body": "clean text"}


async def test_create_run_replay_is_detection_only(session: AsyncSession):
    await _seed(session)
    await _seed_guardrail(session, name="no-secrets", action="block")
    # A replay of a violating payload is NOT blocked and NOT redacted — it is
    # created pending with detection results only (item 10).
    run = await _create(
        session,
        input_payload={"credentials": {"api_key": "sk-live-123"}, "body": "leak SECRET_ABC12345"},
        is_replay=True,
    )
    assert run.status == "pending"
    assert run.input_payload["credentials"]["api_key"] == "sk-live-123"
    rows = (await session.execute(select(EvalResult).where(EvalResult.run_id == run.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].passed is True  # raw regex matched → violation detected, no act


async def test_create_run_observe_mode_stamps_observed(session: AsyncSession):
    await _seed(session)
    await _seed_guardrail(session, name="shadow", action="observe")
    run = await _create(session, input_payload={"body": "leak SECRET_ABC12345"})
    assert run.status == "pending"
    rows = (await session.execute(select(EvalResult).where(EvalResult.run_id == run.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].observed is True


async def test_create_run_json_schema_block_detail_never_round_trips_raw_payload(session: AsyncSession):
    # jsonschema's ValidationError.message embeds the raw offending value
    # ('SECRET_ABC12345' is not of type 'boolean'). The no-raw-persist
    # contract must hold for json_schema detections too: neither the persisted
    # eval_results.detail nor runs.error_detail may carry the raw payload.
    await _seed(session)
    await _seed_guardrail(
        session,
        name="schema-guard",
        action="block",
        config={
            "type": "json_schema",
            "field": "body",
            "schema": {"type": "object", "required": ["safe"], "properties": {"safe": {"type": "boolean"}}},
        },
    )
    run = await _create(session, input_payload={"body": {"safe": "SECRET_ABC12345"}})
    assert run.status == "eval_failed"
    assert run.error_code == "eval_blocked"
    assert "schema-guard" in (run.error_detail or "")
    assert "SECRET_ABC12345" not in (run.error_detail or "")
    rows = (await session.execute(select(EvalResult).where(EvalResult.run_id == run.id))).scalars().all()
    assert len(rows) == 1
    assert "SECRET_ABC12345" not in (rows[0].detail or "")
