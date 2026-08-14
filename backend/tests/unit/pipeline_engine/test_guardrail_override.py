"""Unit tests for guardrail-override remediation (FAR-208 item 6).

``guardrail_override`` extends ``recover_node`` for TERMINAL ``eval_failed``
runs: pipeline FOR UPDATE lock, optimistic status UPDATE (single-flight by
run_id), re-block safe default (the supplied input is re-run through the
guardrail pass; a still-violating input never flips the run back to pending),
post-redaction payload persisted, ``is_replay=True`` (journey increments once),
and a ``guardrail.override`` audit event.
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.pipeline_engine.recovery import (
    ConcurrentRecoveryError,
    GuardrailOverrideError,
    GuardrailOverrideRejectedError,
    guardrail_override,
)

_ORG_ID = uuid.uuid4()
_PIPELINE_ID = uuid.uuid4()
_SNAPSHOT_ID = uuid.uuid4()
_RUN_ID = uuid.uuid4()
_ACTOR_ID = uuid.uuid4()
_GUARDRAIL_ID = uuid.uuid4()


def _make_run(
    *,
    status: str = "eval_failed",
    error_code: str = "eval_blocked",
    input_payload: dict[str, Any] | None = None,
) -> MagicMock:
    run = MagicMock()
    run.id = _RUN_ID
    run.pipeline_id = _PIPELINE_ID
    run.snapshot_id = _SNAPSHOT_ID
    run.langgraph_thread_id = f"{_ORG_ID}:{_RUN_ID}"
    run.status = status
    run.error_code = error_code
    run.error_detail = "Guardrail 'no-secrets' blocked"
    run.input_payload = input_payload
    run.is_replay = False
    return run


def _mock_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=begin_cm)
    session.in_transaction.return_value = True
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.get_bind.return_value = bind
    session.info = {}
    session.flush = AsyncMock()
    return session


def _guardrail_row() -> MagicMock:
    row = MagicMock()
    row.id = _GUARDRAIL_ID
    row.organisation_id = _ORG_ID
    row.pipeline_id = _PIPELINE_ID
    row.node_id = None
    row.name = "no-secrets"
    row.eval_type = "guardrail"
    row.config_json = {
        "action": "block",
        "interception_point": "input",
        "type": "regex",
        "field": "body",
        "pattern": r"SECRET_[A-Z0-9]{8}",
    }
    row.failure_behaviour = "block"
    row.pass_threshold = None
    row.suite_id = None
    return row


def _session_execute_results(session: AsyncMock, *, run, rows) -> None:
    """Wire session.execute to return the run/row sequence used by the override."""
    run_result = MagicMock()
    run_result.scalar_one_or_none.return_value = run
    pipeline_result = MagicMock()
    pipeline_result.scalar_one.return_value = MagicMock()
    rows_result = MagicMock()
    rows_result.scalars.return_value.all.return_value = rows
    locked_result = MagicMock()
    locked_result.scalar_one_or_none.return_value = _RUN_ID
    session.execute = AsyncMock(side_effect=[run_result, pipeline_result, run_result, rows_result, locked_result])


@pytest.mark.asyncio
async def test_guardrail_override_clean_input_flips_to_pending():
    run = _make_run(input_payload={"body": "leak SECRET_ABC12345"})
    session = _mock_session()
    _session_execute_results(session, run=run, rows=[_guardrail_row()])

    with patch("modulo.core.pipeline_engine.recovery.append_audit_event", AsyncMock()) as mock_audit:
        result = await guardrail_override(
            session,
            org_id=_ORG_ID,
            run_id=_RUN_ID,
            input_data={"body": "clean replacement text"},
            actor_id=_ACTOR_ID,
        )

    assert result.status == "pending"
    assert result.error_code is None
    assert result.is_replay is True
    assert result.input_payload == {"body": "clean replacement text"}
    mock_audit.assert_awaited_once()


@pytest.mark.asyncio
async def test_guardrail_override_still_violating_input_is_reblocked():
    run = _make_run()
    session = _mock_session()
    # Only 4 execute calls happen before the pass raises (no status UPDATE).
    run_result = MagicMock()
    run_result.scalar_one_or_none.return_value = run
    pipeline_result = MagicMock()
    pipeline_result.scalar_one.return_value = MagicMock()
    rows_result = MagicMock()
    rows_result.scalars.return_value.all.return_value = [_guardrail_row()]
    session.execute = AsyncMock(side_effect=[run_result, pipeline_result, run_result, rows_result])

    with (
        patch("modulo.core.pipeline_engine.recovery.append_audit_event", AsyncMock()) as mock_audit,
        pytest.raises(GuardrailOverrideRejectedError) as exc_info,
    ):
        await guardrail_override(
            session,
            org_id=_ORG_ID,
            run_id=_RUN_ID,
            input_data={"body": "still has SECRET_ABC12345"},
            actor_id=_ACTOR_ID,
        )

    assert exc_info.value.guardrail_name == "no-secrets"
    assert run.status == "eval_failed"  # run never flipped
    assert run.is_replay is False
    mock_audit.assert_not_called()


@pytest.mark.asyncio
async def test_guardrail_override_refuses_non_guardrail_failed_run():
    run = _make_run(status="failed", error_code="agent.failed")
    session = _mock_session()
    run_result = MagicMock()
    run_result.scalar_one_or_none.return_value = run
    pipeline_result = MagicMock()
    pipeline_result.scalar_one.return_value = MagicMock()
    session.execute = AsyncMock(side_effect=[run_result, pipeline_result, run_result])

    with pytest.raises(GuardrailOverrideError) as exc_info:
        await guardrail_override(
            session,
            org_id=_ORG_ID,
            run_id=_RUN_ID,
            input_data={"body": "anything"},
        )
    assert "eval_failed/eval_blocked" in exc_info.value.reason


@pytest.mark.asyncio
async def test_guardrail_override_concurrent_override_loses_race():
    run = _make_run()
    session = _mock_session()
    run_result = MagicMock()
    run_result.scalar_one_or_none.return_value = run
    pipeline_result = MagicMock()
    pipeline_result.scalar_one.return_value = MagicMock()
    rows_result = MagicMock()
    rows_result.scalars.return_value.all.return_value = [_guardrail_row()]
    locked_result = MagicMock()
    locked_result.scalar_one_or_none.return_value = None  # another override won
    session.execute = AsyncMock(side_effect=[run_result, pipeline_result, run_result, rows_result, locked_result])

    with pytest.raises(ConcurrentRecoveryError):
        await guardrail_override(
            session,
            org_id=_ORG_ID,
            run_id=_RUN_ID,
            input_data={"body": "clean replacement text"},
        )


@pytest.mark.asyncio
async def test_guardrail_override_persists_post_redaction_payload():
    redact_row = MagicMock()
    redact_row.id = _GUARDRAIL_ID
    redact_row.organisation_id = _ORG_ID
    redact_row.pipeline_id = _PIPELINE_ID
    redact_row.node_id = None
    redact_row.name = "redact-key"
    redact_row.eval_type = "guardrail"
    redact_row.config_json = {
        "action": "redact",
        "interception_point": "input",
        "type": "regex",
        "field": "body",
        "pattern": r"SECRET_[A-Z0-9]{8}",
        "redaction": [{"path": "credentials.api_key", "mode": "transform"}],
    }
    redact_row.failure_behaviour = "warn"
    redact_row.pass_threshold = None
    redact_row.suite_id = None

    run = _make_run(input_payload={})
    session = _mock_session()
    _session_execute_results(session, run=run, rows=[redact_row])

    with patch("modulo.core.pipeline_engine.recovery.append_audit_event", AsyncMock()):
        result = await guardrail_override(
            session,
            org_id=_ORG_ID,
            run_id=_RUN_ID,
            input_data={"credentials": {"api_key": "sk-live-123"}, "body": "clean"},
        )

    assert result.status == "pending"
    assert result.input_payload["credentials"]["api_key"] == "\u2022\u2022\u2022\u2022\u2022\u2022"
    assert result.input_payload["body"] == "clean"
