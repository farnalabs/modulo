"""FAR-223 item 11 §4d — eval_results consumer filter contract (SQLite).

Guardrail results live in eval_results (rows whose ``eval_id`` points at an
``eval_type='guardrail'`` definition). Every consumer must either include or
exclude them explicitly. These tests prove the run-detail reader
(``get_run_evals``), the dashboard eval-rate reader and the prompt-optimizer
feed exclude guardrail rows: a guardrail's ``passed=True`` (regex matched)
must never inflate a normal-eval pass rate.

Definitions are seeded into ``evals`` — the storage source since the
FAR-1100/1101 cutover and the table ``eval_results.eval_id`` references.
Seeding ``eval_definitions`` here would silently make the exclusion a no-op
(the FAR-1725 regression); the container-tier twin in
``tests/integration/db/test_far1725_guardrail_clause.py`` pins the real
Postgres storage model.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import Table
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.api.routes.admin import _eval_by_type, _eval_summary
from modulo.api.routes.dashboard import _eval_rate_window
from modulo.api.routes.variant_batches import _batch_load_eval_stats
from modulo.db.crud.eval_run import get_run_evals
from modulo.db.models.account import Account
from modulo.db.models.base import Base
from modulo.db.models.eval import Eval
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
        Eval.__table__,
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
    eval_def = Eval(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        pipeline_id=_PIPELINE,
        node_id=None,
        name=f"{eval_type}-eval",
        eval_type=eval_type,
        config_json={},
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
    outcome: str | None = None,
    error_code: str | None = None,
) -> uuid.UUID:
    result = EvalResult(
        organisation_id=_ORG,
        run_id=run_id,
        node_id=None,
        eval_id=eval_id,
        outcome=outcome if outcome is not None else ("pass" if passed else "fail"),
        passed=passed,
        error_code=error_code,
        observed=observed,
    )
    session.add(result)
    await session.flush()
    return result.id


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


async def test_prompt_optimizer_feed_excludes_guardrail_rows(session: AsyncSession):
    """The prompt-optimizer feed (``get_eval_results_with_defs``) drops
    guardrail rows even though the fetch is by explicit eval-result id — a
    guardrail's inverted passed semantics (regex matched = violation) would
    corrupt the optimizer's failure context. Distinct code path from the SQL
    clause: this filter runs in Python after the by-id fetch."""
    from modulo.db.crud.agent import get_eval_results_with_defs

    run_id = await _seed_run(session)
    guardrail_id = await _seed_eval_def(session, eval_type="guardrail")
    normal_id = await _seed_eval_def(session, eval_type="regex")
    guardrail_result_id = await _seed_eval_result(session, run_id, guardrail_id, passed=True)
    normal_result_id = await _seed_eval_result(session, run_id, normal_id, passed=True)

    results_list, defs_map = await get_eval_results_with_defs(session, [guardrail_result_id, normal_result_id], _ORG)
    # Only the normal eval reaches the optimizer; the guardrail row is dropped.
    assert [r["id"] for r in results_list] == [str(normal_result_id)]
    # The definitions map still carries both eval definitions — only the RESULT
    # rows are excluded from the feed.
    assert str(guardrail_id) in defs_map
    assert str(normal_id) in defs_map


# ---------------------------------------------------------------------------
# ADR 066 D18 — an ``outcome='error'`` row produced NO verdict.
#
# It is the failed instrument, not a failed check, so every quality aggregate
# must drop it from BOTH the numerator and the denominator. Each aggregate
# below reads through ``non_guardrail_eval_results_clause`` (or, for the admin
# by-type breakdown, applies the same predicate explicitly).
# ---------------------------------------------------------------------------


async def _seed_error_result(session: AsyncSession, run_id: uuid.UUID, eval_id: uuid.UUID) -> uuid.UUID:
    """Persist a judge row that produced no verdict — ``passed`` projects False."""
    return await _seed_eval_result(
        session,
        run_id,
        eval_id,
        passed=False,
        outcome="error",
        error_code="judge.timeout",
    )


async def test_get_run_evals_excludes_error_rows(session: AsyncSession):
    run_id = await _seed_run(session)
    normal_id = await _seed_eval_def(session, eval_type="regex")
    await _seed_eval_result(session, run_id, normal_id, passed=True)
    await _seed_error_result(session, run_id, normal_id)

    evals = await get_run_evals(session, run_id)
    # The verdict row survives; the no-verdict row is not a normal eval outcome.
    assert [row.outcome for row in evals] == ["pass"]


async def test_dashboard_eval_rate_window_excludes_error_rows(session: AsyncSession):
    """An error row must not enter the denominator (nor the failed numerator)."""
    run_id = await _seed_run(session)
    normal_id = await _seed_eval_def(session, eval_type="regex")
    await _seed_eval_result(session, run_id, normal_id, passed=True)
    await _seed_error_result(session, run_id, normal_id)

    now = datetime.now(UTC)
    rate = await _eval_rate_window(session, _ORG, now - timedelta(days=1), now + timedelta(days=1))
    # Both rows are in the window; only the verdict row counts → 100%, not 50%.
    assert rate == 100.0


async def test_admin_eval_summary_and_by_type_exclude_error_rows(session: AsyncSession):
    run_id = await _seed_run(session)
    normal_id = await _seed_eval_def(session, eval_type="regex")
    await _seed_eval_result(session, run_id, normal_id, passed=True)
    await _seed_eval_result(session, run_id, normal_id, passed=False)
    await _seed_error_result(session, run_id, normal_id)

    summary = await _eval_summary(session)
    # Two verdicts (one pass, one fail); the error row is neither.
    assert (summary.total_results, summary.passed, summary.failed) == (2, 1, 1)

    by_type = await _eval_by_type(session, _ORG)
    assert [(row.eval_type, row.total, row.passed, row.failed) for row in by_type] == [("regex", 2, 1, 1)]


async def test_variant_batch_eval_stats_exclude_error_rows(session: AsyncSession):
    run_id = await _seed_run(session)
    normal_id = await _seed_eval_def(session, eval_type="regex")
    await _seed_eval_result(session, run_id, normal_id, passed=True)
    await _seed_eval_result(session, run_id, normal_id, passed=False)
    await _seed_error_result(session, run_id, normal_id)

    stats = await _batch_load_eval_stats(session, [run_id])
    assert stats[run_id] == (2, 1)


async def test_the_stage_two_contract_rejects_a_null_outcome(session: AsyncSession):
    """FAR-2058 stage 2 closes the FAR-2040 expand window at the DATABASE layer.

    During the expand rollout ``outcome`` was nullable on old rows, so the
    consumer clause used ``IS DISTINCT FROM`` to keep them counted. Migration
    ``0335`` backfills those rows from ``passed`` and sets the column NOT NULL,
    so the legacy shape can no longer be persisted at all — the invariant this
    test now pins. A Core insert bypasses the ORM ``before_insert`` guard,
    proving the database itself is the enforcer rather than the application.
    """
    run_id = await _seed_run(session)
    normal_id = await _seed_eval_def(session, eval_type="regex")
    with pytest.raises(IntegrityError, match=r"eval_results\.outcome"):
        await session.execute(
            EvalResult.__table__.insert().values(
                organisation_id=_ORG,
                run_id=run_id,
                node_id=None,
                eval_id=normal_id,
                outcome=None,
                passed=True,
                observed=False,
            )
        )
    await session.rollback()
