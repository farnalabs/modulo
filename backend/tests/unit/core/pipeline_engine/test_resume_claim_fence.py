"""Unit coverage for the FAR-2328 inline-resume claim fence.

The MCP approve path resumes a run inline after committing the human decision.
``PipelineExecutor.resume`` can only do that safely if it compare-and-swaps the
lease the approval captured (``claim_token``) together with the run's expected
source status. These tests pin the CAS predicate itself, the superseded error,
and the ``resume`` branch that selects the fenced claim over the legacy
unfenced status write.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.errors import ResumeClaimSupersededError
from modulo.core.pipeline_engine.executor import (
    PipelineExecutor,
    SandboxCapacityExceededError,
    _claim_resume_for_executor,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _compiled_cas_params(statement: Any) -> dict[str, Any]:
    return dict(statement.compile().params)


def _session_context(session: Any) -> MagicMock:
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


class TestResumeClaimForExecutor:
    async def test_cas_match_claims_and_returns_refreshed_run(self) -> None:
        run_id = uuid.uuid4()
        run = MagicMock()
        claim_result = MagicMock()
        claim_result.scalar_one_or_none.return_value = run_id
        refresh_result = MagicMock()
        refresh_result.scalar_one_or_none.return_value = run
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[claim_result, refresh_result])

        claimed = await _claim_resume_for_executor(
            session,
            run_id=run_id,
            org_id=_ORG_ID,
            expected_claim_token="tok-expected",
            source_status="awaiting_human",
        )

        assert claimed is run
        params = _compiled_cas_params(session.execute.await_args_list[0].args[0])
        assert params["status"] == "running"
        assert "tok-expected" in params.values()
        assert "awaiting_human" in params.values()
        assert session.execute.await_count == 2

    async def test_cas_token_mismatch_is_superseded_without_refresh(self) -> None:
        run_id = uuid.uuid4()
        claim_result = MagicMock()
        claim_result.scalar_one_or_none.return_value = None
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[claim_result])

        with pytest.raises(ResumeClaimSupersededError) as raised:
            await _claim_resume_for_executor(
                session,
                run_id=run_id,
                org_id=_ORG_ID,
                expected_claim_token="tok-stale",
                source_status="awaiting_human",
            )

        assert raised.value.run_id == run_id
        params = _compiled_cas_params(session.execute.await_args.args[0])
        # Both CAS predicates are in the statement: a rotated lease and an
        # advanced status each make this UPDATE match zero rows.
        assert "tok-stale" in params.values()
        assert "awaiting_human" in params.values()
        assert session.execute.await_count == 1

    async def test_cas_source_status_mismatch_is_superseded(self) -> None:
        run_id = uuid.uuid4()
        claim_result = MagicMock()
        claim_result.scalar_one_or_none.return_value = None
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[claim_result])

        with pytest.raises(ResumeClaimSupersededError):
            await _claim_resume_for_executor(
                session,
                run_id=run_id,
                org_id=_ORG_ID,
                expected_claim_token="tok-expected",
                source_status="claimed",
            )

        params = _compiled_cas_params(session.execute.await_args.args[0])
        assert params["status"] == "running"
        assert "claimed" in params.values()
        assert "awaiting_human" not in params.values()

    async def test_cas_match_but_row_vanishes_raises_run_not_found(self) -> None:
        from modulo.core.pipeline_engine.executor import RunNotFoundError

        run_id = uuid.uuid4()
        claim_result = MagicMock()
        claim_result.scalar_one_or_none.return_value = run_id
        refresh_result = MagicMock()
        refresh_result.scalar_one_or_none.return_value = None
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[claim_result, refresh_result])

        with pytest.raises(RunNotFoundError):
            await _claim_resume_for_executor(
                session,
                run_id=run_id,
                org_id=_ORG_ID,
                expected_claim_token="tok-expected",
                source_status="awaiting_human",
            )


class TestResumeClaimArm:
    def _executor(self, session: AsyncMock) -> PipelineExecutor:
        executor = PipelineExecutor(MagicMock())
        executor._session_factory = MagicMock(return_value=_session_context(session))
        return executor

    @staticmethod
    def _session() -> AsyncMock:
        session = AsyncMock()
        session.begin = MagicMock(return_value=_session_context(session))
        return session

    async def test_expected_status_uses_fenced_claim_not_unguarded_write(self) -> None:
        session = self._session()
        executor = self._executor(session)
        run = MagicMock()
        denied = SandboxCapacityExceededError(_ORG_ID)

        with (
            patch("modulo.core.pipeline_engine.executor.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new_callable=AsyncMock),
            patch("modulo.core.pipeline_engine.executor.set_mutation_row_lock_timeout", new_callable=AsyncMock),
            patch("modulo.core.pipeline_engine.executor.get_run", new_callable=AsyncMock, return_value=run),
            patch(
                "modulo.core.pipeline_engine.executor._claim_resume_for_executor",
                new_callable=AsyncMock,
                return_value=run,
            ) as claim,
            patch("modulo.core.pipeline_engine.executor.update_run_status", new_callable=AsyncMock) as legacy_write,
            patch.object(executor, "_enforce_resume_sandbox_capacity", new=AsyncMock(side_effect=denied)),
            pytest.raises(SandboxCapacityExceededError),
        ):
            await executor.resume(
                run_id=uuid.uuid4(),
                org_id=_ORG_ID,
                resume_data={"action": "approved"},
                claim_token="tok-expected",
                expected_claim_status="awaiting_human",
            )

        claim.assert_awaited_once()
        assert claim.await_args.kwargs["expected_claim_token"] == "tok-expected"
        assert claim.await_args.kwargs["source_status"] == "awaiting_human"
        legacy_write.assert_not_awaited()

    async def test_expected_status_without_token_is_rejected_before_claim(self) -> None:
        session = self._session()
        executor = self._executor(session)
        run = MagicMock()

        with (
            patch("modulo.core.pipeline_engine.executor.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new_callable=AsyncMock),
            patch("modulo.core.pipeline_engine.executor.set_mutation_row_lock_timeout", new_callable=AsyncMock),
            patch("modulo.core.pipeline_engine.executor.get_run", new_callable=AsyncMock, return_value=run),
            patch(
                "modulo.core.pipeline_engine.executor._claim_resume_for_executor",
                new_callable=AsyncMock,
            ) as claim,
            pytest.raises(ValueError, match="requires claim_token"),
        ):
            await executor.resume(
                run_id=uuid.uuid4(),
                org_id=_ORG_ID,
                resume_data={"action": "approved"},
                expected_claim_status="awaiting_human",
            )

        claim.assert_not_awaited()

    async def test_legacy_resume_without_expected_status_keeps_unguarded_claim(self) -> None:
        session = self._session()
        executor = self._executor(session)
        run = MagicMock()
        denied = SandboxCapacityExceededError(_ORG_ID)

        with (
            patch("modulo.core.pipeline_engine.executor.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new_callable=AsyncMock),
            patch("modulo.core.pipeline_engine.executor.set_mutation_row_lock_timeout", new_callable=AsyncMock),
            patch("modulo.core.pipeline_engine.executor.get_run", new_callable=AsyncMock, return_value=run),
            patch(
                "modulo.core.pipeline_engine.executor._claim_resume_for_executor",
                new_callable=AsyncMock,
            ) as fenced_claim,
            patch("modulo.core.pipeline_engine.executor.update_run_status", new_callable=AsyncMock) as legacy_write,
            patch.object(executor, "_enforce_resume_sandbox_capacity", new=AsyncMock(side_effect=denied)),
            pytest.raises(SandboxCapacityExceededError),
        ):
            await executor.resume(
                run_id=uuid.uuid4(),
                org_id=_ORG_ID,
                resume_data={"action": "approved"},
            )

        legacy_write.assert_awaited_once()
        fenced_claim.assert_not_awaited()


class TestResumeClaimSupersededError:
    def test_construction_carries_run_id_and_readable_message(self) -> None:
        run_id = uuid.uuid4()
        error = ResumeClaimSupersededError(run_id)

        assert error.run_id == run_id
        assert str(run_id) in str(error)
        assert isinstance(error, RuntimeError)
