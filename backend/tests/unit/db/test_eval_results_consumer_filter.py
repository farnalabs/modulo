"""FAR-223 item 11 §4d — eval_results consumer filter contract (SQLite).

Guardrail results live in eval_results (rows whose eval_id points at an
eval_type='guardrail' definition). Every consumer must either include or
exclude them explicitly. These tests prove the run-detail reader
(``get_run_evals``) and the dashboard eval-rate reader exclude guardrail rows:
a guardrail's ``passed=True`` (regex matched) must never inflate a normal-eval
pass rate.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.api.routes.dashboard import _eval_rate_window
from modulo.db.crud.eval_run import get_run_evals
from modulo.db.models.account import Account
from modulo.db.models.base import Base
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import Run

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
        Run.__table__,
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


async def _seed_run(session: AsyncSession) -> uuid.UUID:
    session.add(Organisation(id=_ORG, name="test org", slug="test-org"))
    session.add(Account(id=_ACCOUNT, email="admin@example.com", display_name="admin"))
    session.add(Pipeline(id=_PIPELINE, organisation_id=_ORG, name="pipeline", account_id=_ACCOUNT, visibility="org"))
    run_id = uuid.uuid4()
    session.add(
        Run(
            id=run_id,
            organisation_id=_ORG,
            pipeline_id=_PIPELINE,
            snapshot_id=_SNAPSHOT,
            trigger_type="manual",
            input_hash="h",
            langgraph_thread_id=f"{_ORG}:{run_id}",
            run_number=1,
        )
    )
    await session.flush()
    return run_id


async def _seed_eval_def(session: AsyncSession, *, eval_type: str) -> uuid.UUID:
    eval_def = EvalDefinition(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        pipeline_id=_PIPELINE,
        node_id=None,
        name=f"{eval_type}-eval",
        eval_type=eval_type,
        config_json={},
        failure_behaviour="warn",
        account_id=_ACCOUNT,
    )
    session.add(eval_def)
    await session.flush()
    return eval_def.id


async def _seed_eval_result(
    session: AsyncSession,
    run_id: uuid.UUID,
    eval_id: uuid.UUID,
    *,
    passed: bool,
    observed: bool = False,
) -> None:
    session.add(
        EvalResult(
            organisation_id=_ORG,
            run_id=run_id,
            node_id=None,
            eval_id=eval_id,
            passed=passed,
            observed=observed,
        )
    )
    await session.flush()


async def test_get_run_evals_excludes_guardrail_rows(session: AsyncSession):
    run_id = await _seed_run(session)
    guardrail_id = await _seed_eval_def(session, eval_type="guardrail")
    normal_id = await _seed_eval_def(session, eval_type="regex")
    # A guardrail row (regex matched → passed=True) and a normal eval row.
    await _seed_eval_result(session, run_id, guardrail_id, passed=True)
    await _seed_eval_result(session, run_id, normal_id, passed=True)

    evals = await get_run_evals(session, run_id)
    assert len(evals) == 1
    assert evals[0].eval_id == normal_id


async def test_dashboard_eval_rate_window_excludes_guardrail_rows(session: AsyncSession):
    run_id = await _seed_run(session)
    guardrail_id = await _seed_eval_def(session, eval_type="guardrail")
    normal_id = await _seed_eval_def(session, eval_type="regex")
    # Guardrail "passed" (regex matched = a violation) must NOT lift the rate.
    await _seed_eval_result(session, run_id, guardrail_id, passed=True)
    await _seed_eval_result(session, run_id, normal_id, passed=True)

    now = datetime.now(UTC)
    rate = await _eval_rate_window(session, _ORG, now - timedelta(days=1), now + timedelta(days=1))
    # Only the normal eval counts → 100%.
    assert rate == 100.0
