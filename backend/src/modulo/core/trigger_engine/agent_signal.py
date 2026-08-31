"""Agent signal trigger — cross-pipeline signal on node completion.

When a source pipeline's designated node completes execution, fires a child
pipeline run with the completed node's output as input.

Trigger config_json structure::

    {
        "source_pipeline_id": "<uuid>",   # pipeline to watch
        "source_node_id": "<node_id>",    # node within source pipeline to watch
        "snapshot_id": "<uuid>",          # snapshot for child run
    }
"""

import asyncio
import hashlib
import logging
import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.exceptions import TriggersPausedError
from modulo.core.trigger_engine import is_guardrail_blocked_run, record_dependent_suppressed
from modulo.db.crud.run import create_run
from modulo.db.models.run import ACTIVE_RUN_STATUSES, Run
from modulo.db.models.trigger import Trigger
from modulo.db.models.trigger_event import TriggerEvent
from modulo.db.settings_resolver import PAUSE_SKIP_REASON, org_is_paused

_log = logging.getLogger(__name__)

_ACTIVE_STATUSES = ACTIVE_RUN_STATUSES


async def fire_agent_signal(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    source_run_id: uuid.UUID,
    source_pipeline_id: uuid.UUID,
    completed_node_id: str,
    node_output: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Check for and fire agent_signal triggers matching the completed node.

    Queries active Trigger rows where ``trigger_type='agent_signal'`` and
    ``config_json->>'source_pipeline_id'`` + ``source_node_id`` match the
    completed pipeline + node. Creates a child pipeline run for each match.

    Returns a list of ``{trigger_id, run_id, status}`` dicts describing each
    attempted fire.
    """
    results: list[dict[str, Any]] = []

    stmt = select(Trigger).where(
        Trigger.trigger_type == "agent_signal",
        Trigger.active.is_(True),
        Trigger.organisation_id == org_id,
    )
    result = await session.execute(stmt)
    triggers = list(result.scalars().all())
    if not triggers:
        return results

    str_source_pipeline_id = str(source_pipeline_id)

    for trigger in triggers:
        results.extend(
            await _process_trigger(
                session,
                org_id=org_id,
                trigger=trigger,
                source_run_id=source_run_id,
                source_pipeline_id=source_pipeline_id,
                str_source_pipeline_id=str_source_pipeline_id,
                completed_node_id=completed_node_id,
                node_output=node_output,
            )
        )

    return results


async def _process_trigger(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    trigger: Trigger,
    source_run_id: uuid.UUID,
    source_pipeline_id: uuid.UUID,
    str_source_pipeline_id: str,
    completed_node_id: str,
    node_output: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Evaluate a single agent_signal trigger against the completed node.

    Returns the list of result dicts (usually zero or one) to merge into the
    caller's ``results``. Pure guard-clause flow: every non-matching or blocked
    condition returns early so there is no deep nesting.
    """
    config = trigger.config_json or {}
    str_trigger_id = str(trigger.id)

    # Check if this trigger watches the completed pipeline+node.
    if str(config.get("source_pipeline_id")) != str_source_pipeline_id:
        return []
    if str(config.get("source_node_id")) != completed_node_id:
        return []

    # Org-wide pause kill-switch — checked EARLY (before the concurrency
    # check and snapshot resolution) so a paused org does no wasted snapshot
    # work and records ``paused`` instead of concurrency_limit_reached /
    # invalid_snapshot_id. Read failures PROPAGATE (never fabricate "paused");
    # the create_run gate below stays the TOCTOU backstop.
    if await org_is_paused(session, org_id):
        await _log_signal_event(
            session,
            trigger,
            org_id,
            result="paused",
            error_detail="Org triggers paused",
        )
        return [
            {
                "trigger_id": str_trigger_id,
                "status": "skipped",
                "reason": PAUSE_SKIP_REASON,
            }
        ]

    # Concurrency check — skip if too many active runs on child pipeline.
    active_count = await _count_active_runs(session, trigger.id)
    if active_count >= trigger.max_concurrent_runs:
        await _log_signal_event(
            session,
            trigger,
            org_id,
            result="concurrency_limit_reached",
            error_detail=f"Active runs: {active_count}, limit: {trigger.max_concurrent_runs}",
        )
        return [
            {
                "trigger_id": str_trigger_id,
                "status": "skipped",
                "reason": "concurrency_limit",
                "active_runs": active_count,
            }
        ]

    # FAR-213 dependent-trigger suppression: a guardrail-blocked source run
    # (terminal ``eval_failed`` / ``eval_blocked``) must NEVER fire this
    # dependent trigger — its external side effects are being compensated,
    # not published. Checked at fire time (defense-in-depth: the executor
    # only reaches this call for completing runs, but the guard is the
    # durable invariant). The suppression is audited best-effort with a
    # summary-only payload.
    if await is_guardrail_blocked_run(session, source_run_id):
        _log.info(
            "agent_signal.dependent_suppressed source_run=%s trigger=%s",
            source_run_id,
            str_trigger_id,
        )
        await record_dependent_suppressed(
            session,
            org_id=org_id,
            run_id=source_run_id,
            trigger_count=1,
        )
        return [
            {
                "trigger_id": str_trigger_id,
                "status": "skipped",
                "reason": "source_run_guardrail_blocked",
            }
        ]

    # Build input payload from node output.
    input_payload: dict[str, Any] = {
        "source_run_id": str(source_run_id),
        "source_pipeline_id": str_source_pipeline_id,
        "source_node_id": completed_node_id,
    }
    if node_output is not None:
        input_payload["node_output"] = node_output

    # Resolve snapshot ID from trigger config (pinned, or latest on pipeline).
    snapshot_id, snapshot_skip = await _resolve_snapshot_id(session, trigger, org_id)
    if snapshot_skip is not None:
        return [snapshot_skip]
    if snapshot_id is None:
        # Invariant: _resolve_snapshot_id returns (None, skip) together. Raising
        # instead of asserting keeps the guard live under `python -O`.
        raise RuntimeError(f"agent_signal trigger {trigger.id}: snapshot_id unresolved with no skip result")

    # Create child run within a SAVEPOINT so a failed insert only rolls back
    # the child-run, leaving the caller's transaction usable. A pause gate
    # re-raises TriggersPausedError for the single paused-event handler below.
    try:
        child_run, create_skip = await _create_child_run_in_savepoint(
            session,
            org_id=org_id,
            trigger=trigger,
            input_payload=input_payload,
            snapshot_id=snapshot_id,
            source_run_id=source_run_id,
        )
    except TriggersPausedError:
        # Org-wide pause (kill-switch). Exactly ONE paused event per blocked
        # signal, written in the outer transaction (the savepoint already
        # rolled back the failed child-run insert). No _log.exception spam —
        # a paused org is an expected condition, not an error.
        await _log_signal_event(
            session,
            trigger,
            org_id,
            result="paused",
            error_detail="Org triggers paused",
        )
        return [
            {
                "trigger_id": str_trigger_id,
                "status": "skipped",
                "reason": PAUSE_SKIP_REASON,
            }
        ]

    if create_skip is not None:
        return [create_skip]
    if child_run is None:
        # Invariant: _create_child_run_in_savepoint returns (None, skip) together.
        # Raising instead of asserting keeps the guard live under `python -O`.
        raise RuntimeError(f"agent_signal trigger {trigger.id}: child run creation failed with no skip result")

    # Log TriggerEvent.
    await _log_signal_event(
        session,
        trigger,
        org_id,
        result="signal_fired",
        run_id=child_run.id,
    )

    _log.info(
        "Agent signal trigger %s fired child run %s (source pipeline %s, node %s)",
        trigger.id,
        child_run.id,
        source_pipeline_id,
        completed_node_id,
    )

    return [
        {
            "trigger_id": str(trigger.id),
            "run_id": str(child_run.id),
            "status": "fired",
        }
    ]


async def _resolve_snapshot_id(
    session: AsyncSession,
    trigger: Trigger,
    org_id: uuid.UUID,
) -> tuple[uuid.UUID | None, dict[str, Any] | None]:
    """Resolve the snapshot ID a child run should use.

    Returns ``(snapshot_id, None)`` on success, or ``(None, skip_result)`` when
    the trigger must be skipped (invalid or missing snapshot). The skip result
    is a ready-to-append entry for the caller's ``results`` list.
    """
    config = trigger.config_json or {}
    snapshot_id_str = config.get("snapshot_id")
    if snapshot_id_str:
        try:
            return uuid.UUID(snapshot_id_str), None
        except (ValueError, TypeError):
            _log.warning(
                "Agent signal trigger %s has invalid snapshot_id: %s — skipping",
                trigger.id,
                snapshot_id_str,
            )
            await _log_signal_event(
                session,
                trigger,
                org_id,
                result="poll_error",
                error_detail=f"Invalid snapshot_id: {snapshot_id_str}",
            )
            return None, {
                "trigger_id": str(trigger.id),
                "status": "skipped",
                "reason": "invalid_snapshot_id",
            }

    # No snapshot pinned in config — fall back to the target pipeline's latest
    # snapshot (same resolution cron triggers use). A zero-UUID here would fail
    # the cross-org FK trigger on runs.snapshot_id.
    snap_result = await session.execute(
        text("SELECT id FROM pipeline_snapshots WHERE pipeline_id = :pid ORDER BY created_at DESC LIMIT 1"),
        {"pid": str(trigger.pipeline_id)},
    )
    latest_snapshot_id = snap_result.scalar_one_or_none()
    if latest_snapshot_id is None:
        _log.warning(
            "Agent signal trigger %s has no snapshot for pipeline %s — skipping",
            trigger.id,
            trigger.pipeline_id,
        )
        await _log_signal_event(
            session,
            trigger,
            org_id,
            result="poll_error",
            error_detail=f"No snapshot found for pipeline {trigger.pipeline_id}",
        )
        return None, {
            "trigger_id": str(trigger.id),
            "status": "skipped",
            "reason": "no_snapshot",
        }
    return uuid.UUID(str(latest_snapshot_id)), None


async def _create_child_run_in_savepoint(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    trigger: Trigger,
    input_payload: dict[str, Any],
    snapshot_id: uuid.UUID,
    source_run_id: uuid.UUID,
) -> tuple[Run | None, dict[str, Any] | None]:
    """Create the child run inside a SAVEPOINT-guarded nested transaction.

    Returns ``(child_run, None)`` on success, or ``(child_run_placeholder, skip_result)``
    when the insert fails (constraint violation, deadlock, etc.). The failure is
    reported as a ``validation_failed`` event and the skip result is a
    ready-to-append entry for the caller's ``results`` list. ``TriggersPausedError``
    and ``asyncio.CancelledError`` propagate so the caller's pause gate can handle
    them.
    """
    str_trigger_id = str(trigger.id)
    try:
        async with session.begin_nested():
            child_run = await create_run(
                session,
                org_id=org_id,
                pipeline_id=trigger.pipeline_id,
                snapshot_id=snapshot_id,
                trigger_type="agent_signal",
                trigger_id=trigger.id,
                input_payload=input_payload,
                parent_run_id=source_run_id,
            )
    except (TriggersPausedError, asyncio.CancelledError):
        raise
    except Exception as exc:
        _log.exception("Failed to create child run for agent signal trigger %s", str_trigger_id)
        await _log_signal_event(
            session,
            trigger,
            org_id,
            result="validation_failed",
            error_detail=str(exc)[:200],
        )
        return None, {
            "trigger_id": str_trigger_id,
            "status": "error",
            "reason": "create_run_failed",
        }
    return child_run, None


async def _count_active_runs(session: AsyncSession, trigger_id: uuid.UUID) -> int:
    from sqlalchemy import func as sa_func

    result = await session.execute(
        select(sa_func.count()).where(
            Run.trigger_id == trigger_id,
            Run.status.in_(_ACTIVE_STATUSES),
            Run.cancellation_requested.is_(False),
        )
    )
    return int(result.scalar_one() or 0)


async def _log_signal_event(
    session: AsyncSession,
    trigger: Trigger,
    org_id: uuid.UUID,
    *,
    result: str,
    run_id: uuid.UUID | None = None,
    error_detail: str | None = None,
) -> TriggerEvent:
    """Create a TriggerEvent row for an agent_signal fire attempt."""
    from modulo.core.pipeline_engine.error_codes import sanitize_error_text

    payload_hash = hashlib.sha256(f"agent_signal:{trigger.id}".encode()).hexdigest()
    event = TriggerEvent(
        organisation_id=org_id,
        trigger_id=trigger.id,
        trigger_type="agent_signal",
        raw_payload_hash=payload_hash,
        validation_result=result,
        run_id=run_id,
        error_detail=None if error_detail is None else sanitize_error_text(error_detail),
    )
    session.add(event)
    await session.flush()
    return event
