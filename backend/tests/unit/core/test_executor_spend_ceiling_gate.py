"""Regression tests for the FAR-391 executor pre-gate (Major 1 of the PR review).

The pre-gate must terminalize a run as ``cost_ceiling_exceeded`` BEFORE any
billable work when the org has zero remaining budget — i.e. an org exactly AT
its ceiling (cumulative == spend_ceiling) or a kill-switch ceiling of 0. The
gate achieves this by evaluating the org ceiling with a minimal 1-cent next-step
charge so ``cumulative >= spend_ceiling`` is refused (the finalize ledger block
keeps the stricter ``>`` comparison for the authoritative billing refusal).
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core.pipeline_engine.executor import PipelineExecutor
from modulo.core.spend_ceiling import ORG_CEILING_EXCEEDED
from modulo.db.models.organisation import Organisation
from modulo.db.models.run import Run


@asynccontextmanager
async def _acm(obj):
    yield obj


def _make_session(org: Organisation | None) -> MagicMock:
    session = AsyncMock()

    def _execute(stmt):
        result = MagicMock()
        text = str(stmt).lower()
        result.scalar_one_or_none = MagicMock(return_value=org if "organisations" in text else None)
        return result

    session.execute = AsyncMock(side_effect=_execute)
    # ``session.begin()`` is used as an async context manager in the gate.
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_self(org: Organisation | None, run: Run | None) -> tuple[MagicMock, MagicMock]:
    session = _make_session(org)
    fake_self = MagicMock()
    fake_self._session_factory = MagicMock(return_value=_acm(session))
    fake_self._log = MagicMock()
    return fake_self, session


def _org(*, spend_ceiling_cents, org_cumulative_spend_cents=0) -> Organisation:
    org = MagicMock(spec=Organisation)
    org.id = uuid.uuid4()
    org.spend_ceiling_cents = spend_ceiling_cents
    org.org_cumulative_spend_cents = org_cumulative_spend_cents
    return org


def _run() -> Run:
    run = MagicMock(spec=Run)
    run.id = uuid.uuid4()
    return run


async def test_pre_gate_blocks_when_at_ceiling() -> None:
    org = _org(spend_ceiling_cents=5000, org_cumulative_spend_cents=5000)
    run = _run()
    fake_self, _session = _make_self(org, run)
    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            new=AsyncMock(),
        ) as update_status,
        patch(
            "modulo.core.pipeline_engine.executor.get_run",
            new=AsyncMock(return_value=run),
        ),
    ):
        halted = await PipelineExecutor._check_spend_ceiling_gate(
            fake_self, run_id=run.id, org_id=org.id, claim_token=None
        )
    assert halted is not None
    update_status.assert_awaited_once()
    assert update_status.call_args.kwargs["error_code"] == ORG_CEILING_EXCEEDED


async def test_pre_gate_blocks_kill_switch_zero_ceiling() -> None:
    org = _org(spend_ceiling_cents=0, org_cumulative_spend_cents=0)
    run = _run()
    fake_self, _session = _make_self(org, run)
    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            new=AsyncMock(),
        ) as update_status,
        patch(
            "modulo.core.pipeline_engine.executor.get_run",
            new=AsyncMock(return_value=run),
        ),
    ):
        halted = await PipelineExecutor._check_spend_ceiling_gate(
            fake_self, run_id=run.id, org_id=org.id, claim_token=None
        )
    assert halted is not None
    update_status.assert_awaited_once()


async def test_pre_gate_allows_when_budget_remaining() -> None:
    org = _org(spend_ceiling_cents=5000, org_cumulative_spend_cents=4999)
    run = _run()
    fake_self, _session = _make_self(org, run)
    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            new=AsyncMock(),
        ) as update_status,
        patch(
            "modulo.core.pipeline_engine.executor.get_run",
            new=AsyncMock(return_value=run),
        ),
    ):
        halted = await PipelineExecutor._check_spend_ceiling_gate(
            fake_self, run_id=run.id, org_id=org.id, claim_token=None
        )
    assert halted is None
    update_status.assert_not_awaited()


async def test_pre_gate_allows_when_no_ceiling() -> None:
    org = _org(spend_ceiling_cents=None, org_cumulative_spend_cents=0)
    run = _run()
    fake_self, _session = _make_self(org, run)
    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch(
            "modulo.core.pipeline_engine.executor.update_run_status",
            new=AsyncMock(),
        ) as update_status,
        patch(
            "modulo.core.pipeline_engine.executor.get_run",
            new=AsyncMock(return_value=run),
        ),
    ):
        halted = await PipelineExecutor._check_spend_ceiling_gate(
            fake_self, run_id=run.id, org_id=org.id, claim_token=None
        )
    assert halted is None
    update_status.assert_not_awaited()
