"""Unit tests for guardrail-override remediation (FAR-208 item 6).

``guardrail_override`` extends ``recover_node`` for TERMINAL ``eval_failed``
runs: pipeline FOR UPDATE lock, optimistic status UPDATE (single-flight by
run_id), re-block safe default (the supplied input is re-run through the
guardrail pass; a still-violating input never flips the run back to pending),
post-redaction payload persisted, ``is_replay=True`` (journey increments once),
the dedicated ``guardrail_override_resume_pending`` marker set on the
node-started flip (FAR-1728: the reconcile self-heal leg's single-writer
discriminator — a boolean column, never a sentinel in the observation column
``error_detail``), and a ``guardrail.override`` audit event.
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.dispatch_phases import PHASE_FIRST_NODE_DISPATCHED
from modulo.core.pipeline_engine.recovery import (
    ConcurrentRecoveryError,
    GuardrailOverrideError,
    GuardrailOverrideNoCheckpointError,
    GuardrailOverrideRejectedError,
    guardrail_override,
)
from modulo.db.crud.run import _input_hash

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
    dispatch_phase: str | None = None,
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
    run.input_hash = "stale-hash-of-original-blocked-payload"
    run.completed_at = "2026-01-01T00:00:00+00:00"
    run.is_replay = False
    # FAR-1728: the dedicated self-heal marker starts false (server default);
    # only the node-started flip sets it.
    run.guardrail_override_resume_pending = False
    # FAR-1711: explicit None = the genuine zero-node block (run-level
    # interception at creation). Node-started fixtures pass
    # PHASE_FIRST_NODE_DISPATCHED instead.
    run.dispatch_phase = dispatch_phase
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


def _session_execute_results(
    session: AsyncMock, *, run, rows, node_started: bool = False, has_checkpoint: bool = True
) -> None:
    """Wire session.execute to return the run/row sequence used by the override.

    A node-started run (FAR-1711 M1) adds ONE checkpoint-existence probe
    between the post-lock re-read and the guardrail-row load; the probe's
    result is what decides between the ``running`` flip and the fail-closed
    no-checkpoint refusal.
    """
    run_result = MagicMock()
    run_result.scalar_one_or_none.return_value = run
    pipeline_result = MagicMock()
    pipeline_result.scalar_one.return_value = MagicMock()
    rows_result = MagicMock()
    rows_result.scalars.return_value.all.return_value = rows
    locked_result = MagicMock()
    locked_result.scalar_one_or_none.return_value = _RUN_ID
    checkpoint_result = MagicMock()
    checkpoint_result.first.return_value = (1,) if has_checkpoint else None
    results = [run_result, pipeline_result, run_result]
    if node_started:
        results.append(checkpoint_result)
    results.extend([rows_result, locked_result])
    session.execute = AsyncMock(side_effect=results)


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
    # FAR-1728: the ZERO-NODE flip does NOT set the marker — nothing is
    # resumable there (re-dispatched ``execute_run``, recovered by the
    # pending-class legs), and a "resume pending" flag would linger forever.
    # ``error_detail`` is an observation column and is left untouched (the
    # pre-0305 sentinel-string design is retired).
    assert result.guardrail_override_resume_pending is False
    assert result.error_detail == "Guardrail 'no-secrets' blocked"
    assert result.is_replay is True
    assert result.input_payload == {"body": "clean replacement text"}
    # The stored payload changed, so the persisted input_hash must be recomputed
    # from the post-redaction payload actually stored, and the block-time
    # completed_at stamp must be cleared (the run is no longer terminal).
    assert result.input_hash == _input_hash({"body": "clean replacement text"})
    assert result.input_hash != "stale-hash-of-original-blocked-payload"
    assert result.completed_at is None
    mock_audit.assert_awaited_once()
    audit_kwargs = mock_audit.await_args.kwargs
    # FAR-728: the override event carries a descriptive summary; a user-driven
    # override rides the canonical actor column, not a label key.
    assert audit_kwargs["payload_json"]["summary"] == "Guardrail override applied (run requeued as replay)"
    assert audit_kwargs["actor_user_id"] == _ACTOR_ID
    assert "actor" not in audit_kwargs["payload_json"]


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


@pytest.mark.asyncio
async def test_guardrail_override_destroy_mechanism_error_fails_closed():
    """FAR-209 Track B: a destroy-redaction mechanism error escaping the
    re-run pass must fail CLOSED (override rejected, run stays terminal), not
    surface as an unhandled 500 from the operator override endpoint.
    """
    from modulo.core.guardrails import GuardrailRedactionMechanismError

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
        patch(
            "modulo.core.guardrails.run_interception_pass_async",
            AsyncMock(side_effect=GuardrailRedactionMechanismError("guardrail_destroy_pepper unset")),
        ),
        patch("modulo.core.pipeline_engine.recovery.append_audit_event", AsyncMock()) as mock_audit,
        pytest.raises(GuardrailOverrideRejectedError) as exc_info,
    ):
        await guardrail_override(
            session,
            org_id=_ORG_ID,
            run_id=_RUN_ID,
            input_data={"body": "anything"},
            actor_id=_ACTOR_ID,
        )

    assert exc_info.value.guardrail_name == "<guardrail>"
    assert "guardrail mechanism error" in exc_info.value.detail
    assert run.status == "eval_failed"  # run never flipped
    assert run.is_replay is False
    mock_audit.assert_not_called()


@pytest.mark.asyncio
async def test_guardrail_override_node_started_run_flips_to_running_for_resume():
    """FAR-1711 regression: a node-STARTED eval-blocked run (durable
    ``first_node_dispatched`` phase — a node-scoped ``block`` eval
    terminalizes from a completed node) flips to ``running`` (the recover-
    node lock status the resume claim's stale-running arm matches), NOT
    ``pending``. The caller must re-dispatch it as ``resume_run`` — a
    ``pending`` flip re-dispatches as ``execute_run`` and RE-EXECUTES the
    node-started run, violating FAR-1649 ("a node-started run may only be
    resumed from checkpoint, never re-executed"). The zero-node control is
    ``test_guardrail_override_clean_input_flips_to_pending`` (explicit NULL
    phase -> ``pending``).

    FAR-1711 (C1): the flip must ALSO stamp ``heartbeat_at`` one full
    claim-stale window into the past, because the resume claim matches a
    ``running`` row only when its heartbeat is stale — a bare status flip
    leaves a FRESH block-time heartbeat and the run is unclaimable for a
    whole ``RUN_CLAIM_STALE_SECONDS`` window (the override is silently lost).
    """
    run = _make_run(
        input_payload={"body": "clean"},
        dispatch_phase=PHASE_FIRST_NODE_DISPATCHED,
    )
    session = _mock_session()
    _session_execute_results(session, run=run, rows=[_guardrail_row()], node_started=True, has_checkpoint=True)

    with patch("modulo.core.pipeline_engine.recovery.append_audit_event", AsyncMock()) as mock_audit:
        result = await guardrail_override(
            session,
            org_id=_ORG_ID,
            run_id=_RUN_ID,
            input_data={"body": "clean replacement text"},
            actor_id=_ACTOR_ID,
        )

    assert result.status == "running"
    assert result.error_code is None
    # FAR-1728: the node-started flip sets the DEDICATED marker column — it is
    # what the reconcile self-heal leg selects on (``is_replay`` cannot: webhook
    # replay shares it), so a lost ``resume_run`` job re-dispatches from THIS
    # row and never from a replay-shaped one. ``error_detail`` is an observation
    # column and is left untouched (never a control-flow marker).
    assert result.guardrail_override_resume_pending is True
    assert result.error_detail == "Guardrail 'no-secrets' blocked"
    assert result.is_replay is True
    # The phase is preserved through the flip — the resume machinery resets it
    # atomically with its claim, and reconcile belts still recognise the row.
    assert result.dispatch_phase == PHASE_FIRST_NODE_DISPATCHED
    # C1: the heartbeat is stamped stale by at least the claim-stale window, so
    # ``claim_resume_run_async``'s stale-running arm matches on the first try.
    from datetime import UTC, datetime

    from modulo.settings import get_settings

    stale_seconds = int(get_settings().run_claim_stale_seconds)
    assert result.heartbeat_at is not None
    heartbeat_age = (datetime.now(UTC) - result.heartbeat_at).total_seconds()
    assert heartbeat_age >= stale_seconds
    mock_audit.assert_awaited_once()


@pytest.mark.asyncio
async def test_guardrail_override_node_started_run_without_checkpoint_fails_closed():
    """FAR-1711 (M1): a node-STARTED run whose thread carries NO root
    checkpoint cannot be resumed (FAR-1649 forbids re-execution), so the
    override FAILS CLOSED — ``GuardrailOverrideNoCheckpointError``, run left
    terminal ``eval_failed``/``eval_blocked``, nothing flipped, no dispatch.
    The reachable shape is a non-interactive pipeline (FAR-432 attaches no
    checkpointer there, so none was ever persisted)."""
    run = _make_run(
        input_payload={"body": "clean"},
        dispatch_phase=PHASE_FIRST_NODE_DISPATCHED,
    )
    session = _mock_session()
    _session_execute_results(session, run=run, rows=[_guardrail_row()], node_started=True, has_checkpoint=False)

    with (
        patch("modulo.core.pipeline_engine.recovery.append_audit_event", AsyncMock()) as mock_audit,
        pytest.raises(GuardrailOverrideNoCheckpointError),
    ):
        await guardrail_override(
            session,
            org_id=_ORG_ID,
            run_id=_RUN_ID,
            input_data={"body": "clean replacement text"},
            actor_id=_ACTOR_ID,
        )

    # Fail CLOSED before any state write: the run stays the terminal block it
    # was, and no audit event is recorded for a refused override.
    assert run.status == "eval_failed"
    assert run.error_code == "eval_blocked"
    assert run.is_replay is False
    mock_audit.assert_not_called()
    # No optimistic status UPDATE was attempted (the last execute is the
    # checkpoint probe, not the flip).
    assert session.execute.await_count == 4


@pytest.mark.asyncio
async def test_guardrail_override_node_started_run_without_thread_id_fails_closed():
    """FAR-1711 (M1): a node-STARTED run whose ``langgraph_thread_id`` is NULL
    can never carry a root checkpoint, so the override fails closed WITHOUT even
    probing the ``checkpoints`` table — the ``not thread_id`` short-circuit the
    existence probe exists for. Same refusal as the no-checkpoint case, reached
    before any SQL probe (3 execute calls: run, pipeline lock, run re-read)."""
    run = _make_run(
        input_payload={"body": "clean"},
        dispatch_phase=PHASE_FIRST_NODE_DISPATCHED,
    )
    run.langgraph_thread_id = None
    session = _mock_session()
    _session_execute_results(session, run=run, rows=[_guardrail_row()], node_started=True, has_checkpoint=True)

    with (
        patch("modulo.core.pipeline_engine.recovery.append_audit_event", AsyncMock()) as mock_audit,
        pytest.raises(GuardrailOverrideNoCheckpointError),
    ):
        await guardrail_override(
            session,
            org_id=_ORG_ID,
            run_id=_RUN_ID,
            input_data={"body": "clean replacement text"},
            actor_id=_ACTOR_ID,
        )

    # Fail CLOSED before any state write; the run keeps its terminal block state.
    assert run.status == "eval_failed"
    assert run.error_code == "eval_blocked"
    assert run.is_replay is False
    mock_audit.assert_not_called()
    # The checkpoint probe short-circuits on the NULL thread id: run, pipeline
    # lock, run re-read — and NO checkpoint SELECT.
    assert session.execute.await_count == 3


def test_guardrail_override_job_type_follows_the_flip_target():
    """FAR-1711 (m1): the re-dispatch job type comes from the ONE shared
    predicate ``guardrail_override_job_type`` (owned by the module that owns
    the flip target), so the route never re-derives it from a magic status."""
    from modulo.core.pipeline_engine.recovery import guardrail_override_job_type

    node_started_run = _make_run(status="running", dispatch_phase=PHASE_FIRST_NODE_DISPATCHED)
    zero_node_run = _make_run(status="pending")
    assert guardrail_override_job_type(node_started_run) == "resume_run"
    assert guardrail_override_job_type(zero_node_run) == "execute_run"
