"""Unit tests for EvalRun CRUD operations (mocked session, no DB)."""

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_RUN_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_EVAL_ID_NORMAL = uuid.UUID("00000000-0000-0000-0000-000000000004")
_EVAL_ID_NORMAL2 = uuid.UUID("00000000-0000-0000-0000-000000000005")


@pytest.fixture
def mock_session() -> AsyncMock:
    return AsyncMock(spec=AsyncSession)


def _make_eval_result(**overrides: Any) -> MagicMock:
    er = MagicMock()
    er.id = overrides.get("id", uuid.uuid4())
    er.run_id = overrides.get("run_id", _RUN_ID)
    er.eval_id = overrides.get("eval_id", _EVAL_ID_NORMAL)
    er.organisation_id = overrides.get("organisation_id", _ORG_ID)
    er.outcome = overrides.get("outcome", "pass")
    er.passed = overrides.get("passed", True)
    er.score = overrides.get("score", 0.9)
    er.detail = overrides.get("detail", "ok")
    er.evaluated_at = overrides.get("evaluated_at", datetime.now(UTC))
    er.__table__ = MagicMock()
    return er


def _compiled_sql(statement: Any) -> str:
    return str(statement.compile(compile_kwargs={"literal_binds": True}))


def _exec_result(scalar_value: Any = None, scalars_list: list[Any] | None = None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=scalar_value)
    result.scalar_one = MagicMock(return_value=1)
    result.scalar = MagicMock(return_value=scalar_value)
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=scalars_list or [])
    result.scalars = MagicMock(return_value=scalars)
    return result


class TestNonGuardrailEvalResultsClause:
    def test_excludes_guardrail_type(self) -> None:
        from sqlalchemy.sql.elements import BooleanClauseList

        from farna.db.crud.eval_run import non_guardrail_eval_results_clause

        clause = non_guardrail_eval_results_clause()
        # The clause should be a BooleanClauseList (AND of two conditions)
        assert isinstance(clause, BooleanClauseList)

    def test_excludes_error_outcome(self) -> None:
        from farna.db.crud.eval_run import non_guardrail_eval_results_clause

        sql = _compiled_sql(non_guardrail_eval_results_clause())

        # ADR 066 D18: an outcome='error' row records that the judge produced no
        # verdict, so it must be excluded from every quality aggregate.
        assert "eval_results.outcome IS DISTINCT FROM 'error'" in sql
        # FAR-1725: the guardrail set is sourced from `evals` (the post-cutover
        # storage), not the frozen `eval_definitions` snapshot.
        assert "eval_results.eval_id NOT IN (SELECT evals.id" in sql
        assert "evals.eval_type = 'guardrail'" in sql


class TestGetRunEvals:
    async def test_returns_eval_results_for_run(self, mock_session: AsyncMock) -> None:
        er1 = _make_eval_result(eval_id=_EVAL_ID_NORMAL, outcome="pass")
        er2 = _make_eval_result(eval_id=_EVAL_ID_NORMAL2, outcome="fail")
        mock_session.execute = AsyncMock(return_value=_exec_result(scalars_list=[er1, er2]))

        from farna.db.crud.eval_run import get_run_evals

        results = await get_run_evals(mock_session, _RUN_ID)

        assert len(results) == 2
        assert results[0] is er1
        assert results[1] is er2
        mock_session.execute.assert_awaited_once()

    async def test_returns_empty_list_when_none_found(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_result(scalars_list=[]))
        from farna.db.crud.eval_run import get_run_evals

        results = await get_run_evals(mock_session, _RUN_ID)

        assert results == []
        mock_session.execute.assert_awaited_once()

    async def test_orders_by_evaluated_at_desc(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_result(scalars_list=[]))
        from farna.db.crud.eval_run import get_run_evals

        await get_run_evals(mock_session, _RUN_ID)

        statement = mock_session.execute.await_args.args[0]
        sql = _compiled_sql(statement)
        assert f"eval_results.run_id = '{_RUN_ID.hex}'" in sql
        assert "eval_results.outcome IS DISTINCT FROM 'error'" in sql
        assert "ORDER BY eval_results.evaluated_at DESC" in sql


class TestGetEvalsForRuns:
    async def test_returns_empty_on_empty_run_ids(self, mock_session: AsyncMock) -> None:
        from farna.db.crud.eval_run import get_evals_for_runs

        results = await get_evals_for_runs(mock_session, org_id=_ORG_ID, run_ids=[])

        assert results == []
        mock_session.execute.assert_not_awaited()

    async def test_returns_eval_results_filtered_by_org_and_runs(self, mock_session: AsyncMock) -> None:
        er1 = _make_eval_result(run_id=_RUN_ID, eval_id=_EVAL_ID_NORMAL, outcome="pass")
        er2 = _make_eval_result(run_id=_RUN_ID, eval_id=_EVAL_ID_NORMAL2, outcome="fail")
        mock_session.execute = AsyncMock(return_value=_exec_result(scalars_list=[er1, er2]))

        from farna.db.crud.eval_run import get_evals_for_runs

        results = await get_evals_for_runs(mock_session, org_id=_ORG_ID, run_ids=[_RUN_ID])

        assert len(results) == 2
        assert results[0] is er1
        assert results[1] is er2
        mock_session.execute.assert_awaited_once()

    async def test_returns_empty_when_no_results(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_result(scalars_list=[]))
        from farna.db.crud.eval_run import get_evals_for_runs

        results = await get_evals_for_runs(mock_session, org_id=_ORG_ID, run_ids=[_RUN_ID])

        assert results == []
        mock_session.execute.assert_awaited_once()

    async def test_filters_by_organisation_id(self, mock_session: AsyncMock) -> None:
        er = _make_eval_result(run_id=_RUN_ID, eval_id=_EVAL_ID_NORMAL)
        mock_session.execute = AsyncMock(return_value=_exec_result(scalars_list=[er]))
        from farna.db.crud.eval_run import get_evals_for_runs

        await get_evals_for_runs(mock_session, org_id=_ORG_ID, run_ids=[_RUN_ID])

        statement = mock_session.execute.await_args.args[0]
        sql = _compiled_sql(statement)
        assert f"eval_results.organisation_id = '{_ORG_ID.hex}'" in sql
        assert f"eval_results.run_id IN ('{_RUN_ID.hex}')" in sql

    async def test_handles_multiple_run_ids(self, mock_session: AsyncMock) -> None:
        run_id2 = uuid.uuid4()
        er1 = _make_eval_result(run_id=_RUN_ID, eval_id=_EVAL_ID_NORMAL)
        er2 = _make_eval_result(run_id=run_id2, eval_id=_EVAL_ID_NORMAL2)
        mock_session.execute = AsyncMock(return_value=_exec_result(scalars_list=[er1, er2]))
        from farna.db.crud.eval_run import get_evals_for_runs

        results = await get_evals_for_runs(mock_session, org_id=_ORG_ID, run_ids=[_RUN_ID, run_id2])

        assert len(results) == 2
        mock_session.execute.assert_awaited_once()

    async def test_orders_by_evaluated_at_asc(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_result(scalars_list=[]))
        from farna.db.crud.eval_run import get_evals_for_runs

        await get_evals_for_runs(mock_session, org_id=_ORG_ID, run_ids=[_RUN_ID])

        statement = mock_session.execute.await_args.args[0]
        sql = _compiled_sql(statement)
        assert "ORDER BY eval_results.evaluated_at" in sql
        assert "DESC" not in sql
