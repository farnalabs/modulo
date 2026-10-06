"""FAR-1141 / ADR-042: `execution_origin` on the run-keyed AUDIT payloads.

The conformance criterion reaches the audit log too: an operator reading an
audit event for a dispatched run must be able to tell that the work happened on
the customer's substrate, rather than in Modulo. Two payloads are pinned here —
the executor's run-lifecycle events, one per ``resource_type="run"`` write site
that composes a run-level payload:

* ``run_started`` — fired at the pending→running claim, the primary run
  lifecycle event (executor ``_claim_run_and_audit``).
* ``eval.blocked`` — fired on guardrail-eval terminalization (executor
  ``_record_eval_blocked_audit``).

(``context_write_by_non_setter`` composes a node-scoped payload — node id /
role / attempted keys — not a run-level one, so it is deliberately out of
scope. ``node.recovery`` is covered alongside its own harness in
``test_recovery.py``.)

The ``node.recovery`` / HITL denial sites are covered in their own modules.

Both sides of the line are asserted for every payload: a dispatched run reads
``"dispatched"``, and a ``MagicMock`` run stand-in whose unset attribute
resolves to a mock degrades to ``None`` — audit payloads are immutable and
hash-linked, so a repr must never reach them.

No DB: the session and run rows are in-memory stand-ins.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.pipeline_engine.executor import PipelineExecutor

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_RUN_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


def _mock_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _executor_with_session(session: AsyncMock) -> PipelineExecutor:
    @asynccontextmanager
    async def _ctx() -> AsyncGenerator[AsyncSession, None]:
        yield session

    executor = PipelineExecutor(MagicMock())
    executor._session_factory = MagicMock(side_effect=lambda: _ctx())
    return executor


def _run(**overrides: Any) -> MagicMock:
    run = MagicMock()
    run.id = _RUN_ID
    run.pipeline_id = _PIPELINE_ID
    run.status = "pending"
    run.trigger_type = "manual"
    run.trigger_id = None
    run.account_id = None
    for key, value in overrides.items():
        setattr(run, key, value)
    return run


# ---------------------------------------------------------------------------
# run_started — the primary run-lifecycle payload
# ---------------------------------------------------------------------------


async def _run_started_payload(run: MagicMock) -> dict[str, Any]:
    executor = _executor_with_session(_mock_session())
    pipeline = MagicMock()
    pipeline.name = "PR Reviewer Agent"
    with (
        patch("modulo.core.pipeline_engine.executor.update_run_status", new=AsyncMock(return_value=run)),
        patch("modulo.core.pipeline_engine.executor.get_run", new=AsyncMock(return_value=run)),
        patch("modulo.core.pipeline_engine.executor.get_pipeline", new=AsyncMock(return_value=pipeline)),
        patch("modulo.core.pipeline_engine.executor.append_audit_event", new=AsyncMock()) as audit,
    ):
        await executor._claim_run_and_audit(
            session=AsyncMock(),
            run_id=_RUN_ID,
            org_id=_ORG_ID,
            pipeline_id=_PIPELINE_ID,
        )
    assert audit.await_count == 1, "the claim transition must fire exactly one run_started event"
    kwargs = audit.await_args.kwargs
    assert kwargs["event_type"] == "run_started"
    assert kwargs["resource_type"] == "run"
    assert kwargs["resource_id"] == _RUN_ID
    return kwargs["payload_json"]


class TestRunStartedExecutionOrigin:
    async def test_dispatched_run_payload_carries_origin(self) -> None:
        payload = await _run_started_payload(_run(execution_origin="dispatched"))
        assert payload["execution_origin"] == "dispatched"

    async def test_executed_run_payload_carries_null(self) -> None:
        payload = await _run_started_payload(_run(execution_origin=None))
        assert payload["execution_origin"] is None

    async def test_mock_run_stand_in_degrades_to_null(self) -> None:
        """The stand-in's unset attribute is a MagicMock — an audit payload is
        immutable, so it must serialise as NULL, never as a repr."""
        payload = await _run_started_payload(_run())
        assert payload["execution_origin"] is None

    async def test_the_other_payload_keys_are_untouched(self) -> None:
        """Additive only: the FAR-728 summary/actor contract must not shift."""
        payload = await _run_started_payload(_run(execution_origin="dispatched"))
        assert payload["pipeline_id"] == str(_PIPELINE_ID)
        # manual runs attribute the source to the user request (labels.py).
        assert payload["summary"] == (
            f'Pipeline "PR Reviewer Agent" ({str(_PIPELINE_ID)[:8]}) run triggered by user request'
        )
        assert payload["actor"] is not None


# ---------------------------------------------------------------------------
# eval.blocked — guardrail-eval terminalization payload
# ---------------------------------------------------------------------------


async def _eval_blocked_payload(run: MagicMock) -> dict[str, Any]:
    executor = _executor_with_session(_mock_session())
    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org"),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context"),
        patch("modulo.core.pipeline_engine.executor.get_run", new=AsyncMock(return_value=run)),
        patch("modulo.core.pipeline_engine.executor.append_audit_event", new=AsyncMock()) as audit,
    ):
        await executor._record_eval_blocked_audit(
            org_id=_ORG_ID,
            run_id=_RUN_ID,
            pipeline_id=_PIPELINE_ID,
            error_detail="score 0.3 below threshold 0.8",
        )
    assert audit.await_count == 1, "a blocked eval must still write its audit event"
    kwargs = audit.await_args.kwargs
    assert kwargs["event_type"] == "eval.blocked"
    assert kwargs["resource_type"] == "run"
    assert kwargs["resource_id"] == _RUN_ID
    return kwargs["payload_json"]


class TestEvalBlockedExecutionOrigin:
    async def test_dispatched_run_payload_carries_origin(self) -> None:
        payload = await _eval_blocked_payload(_run(execution_origin="dispatched"))
        assert payload["execution_origin"] == "dispatched"

    async def test_executed_run_payload_carries_null(self) -> None:
        payload = await _eval_blocked_payload(_run(execution_origin=None))
        assert payload["execution_origin"] is None

    async def test_mock_run_stand_in_degrades_to_null(self) -> None:
        payload = await _eval_blocked_payload(_run())
        assert payload["execution_origin"] is None

    async def test_sanitized_error_detail_contract_is_untouched(self) -> None:
        """Additive only: the write-site redaction the immutability tests pin
        must still land alongside the new key."""
        payload = await _eval_blocked_payload(_run(execution_origin="dispatched"))
        assert payload["error_detail"] == "score 0.3 below threshold 0.8"
        assert payload["pipeline_id"] == str(_PIPELINE_ID)
        assert payload["actor"] is not None
        assert payload["summary"] is not None
