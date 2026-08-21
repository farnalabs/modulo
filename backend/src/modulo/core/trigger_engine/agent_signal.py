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
        config = trigger.config_json or {}
        str_trigger_id = str(trigger.id)
        source_pid = config.get("source_pipeline_id")
        source_nid = config.get("source_node_id")

        # Check if this trigger watches the completed pipeline+node.
        if str(source_pid) != str_source_pipeline_id:
            continue
        if str(source_nid) != completed_node_id:
            continue

        # Org-wide pause kill-switch — checked EARLY (before the concurrency
        # check and snapshot resolution) so a paused org does no wasted snapshot
        # work and records ``paused`` instead of concurrency_limit_reached /
        # invalid_snapshot_id. Exactly ONE paused event per blocked signal,
        # written in the outer transaction. Read failures PROPAGATE (never
        # fabricate "paused"); the create_run gate below stays the TOCTOU
        # backstop.
        if await org_is_paused(session, org_id):
            await _log_signal_event(
                session,
                trigger,
                org_id,
                result="paused",
                error_detail="Org triggers paused",
            )
            results.append(
                {
                    "trigger_id": str(trigger.id),
                    "status": "skipped",
                    "reason": PAUSE_SKIP_REASON,
                }
            )
            continue

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
            results.append(
                {
                    "trigger_id": str(trigger.id),
                    "status": "skipped",
                    "reason": "concurrency_limit",
                    "active_runs": active_count,
                }
            )
            continue

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
            results.append(
                {
                    "trigger_id": str_trigger_id,
                    "status": "skipped",
                    "reason": "source_run_guardrail_blocked",
                }
            )
            continue

        # Build input payload from node output.
        input_payload: dict[str, Any] = {
            "source_run_id": str(source_run_id),
            "source_pipeline_id": str_source_pipeline_id,
            "source_node_id": completed_node_id,
        }
        if node_output is not None:
            input_payload["node_output"] = node_output

        # Resolve snapshot ID from trigger config.
        snapshot_id_str = config.get("snapshot_id")
        if snapshot_id_str:
            try:
                snapshot_id = uuid.UUID(snapshot_id_str)
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
                results.append(
                    {
                        "trigger_id": str(trigger.id),
                        "status": "skipped",
                        "reason": "invalid_snapshot_id",
                    }
                )
                continue
        else:
            # No snapshot pinned in config — fall back to the target pipeline's
            # latest snapshot (same resolution cron triggers use). A zero-UUID
            # here would fail the cross-org FK trigger on runs.snapshot_id.
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
                results.append(
                    {
                        "trigger_id": str(trigger.id),
                        "status": "skipped",
                        "reason": "no_snapshot",
                    }
                )
                continue
            snapshot_id = uuid.UUID(str(latest_snapshot_id))

        # Create child run linked to source via parent_run_id.
        #
        # Wrap the insert in a SAVEPOINT so a failed create_run (constraint
        # violation, deadlock, etc.) rolls back only the child-run insert and
        # leaves the caller's transaction usable. Without this, the failed
        # flush poisons the whole transaction and the exception-handling code
        # below (which touches the same session) explodes with a misleading
        # "Can't operate on closed transaction" error.
        try:
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
            except TriggersPausedError:
                # Pause gate: re-raise so the savepoint rolls back, then let the
                # outer handler write the single paused event in the outer tx.
                raise
            except asyncio.CancelledError:
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
                results.append(
                    {
                        "trigger_id": str_trigger_id,
                        "status": "error",
                        "reason": "create_run_failed",
                    }
                )
                continue
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
            results.append(
                {
                    "trigger_id": str_trigger_id,
                    "status": "skipped",
                    "reason": PAUSE_SKIP_REASON,
                }
            )
            continue

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

        results.append(
            {
                "trigger_id": str(trigger.id),
                "run_id": str(child_run.id),
                "status": "fired",
            }
        )

    return results


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
