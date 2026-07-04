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

import hashlib
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.run import create_run
from modulo.db.models.run import Run
from modulo.db.models.trigger import Trigger
from modulo.db.models.trigger_event import TriggerEvent

_log = logging.getLogger(__name__)

_ACTIVE_STATUSES = ("pending", "running", "awaiting_human", "claimed", "waiting_for_lock")


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
        Trigger.active == True,  # noqa: E712
        Trigger.organisation_id == org_id,
    )
    result = await session.execute(stmt)
    triggers = list(result.scalars().all())
    if not triggers:
        return results

    str_source_pipeline_id = str(source_pipeline_id)

    for trigger in triggers:
        config = trigger.config_json or {}
        source_pid = config.get("source_pipeline_id")
        source_nid = config.get("source_node_id")

        # Check if this trigger watches the completed pipeline+node.
        if str(source_pid) != str_source_pipeline_id:
            continue
        if str(source_nid) != completed_node_id:
            continue

        # Concurrency check — skip if too many active runs on child pipeline.
        active_count = await _count_active_runs(session, trigger.pipeline_id)
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
            snapshot_id = uuid.uuid4()

        # Create child run linked to source via parent_run_id.
        try:
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
        except Exception as exc:
            _log.exception("Failed to create child run for agent signal trigger %s", trigger.id)
            await _log_signal_event(
                session,
                trigger,
                org_id,
                result="error",
                error_detail=str(exc)[:200],
            )
            results.append(
                {
                    "trigger_id": str(trigger.id),
                    "status": "error",
                    "reason": "create_run_failed",
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


async def _count_active_runs(session: AsyncSession, pipeline_id: uuid.UUID) -> int:
    from sqlalchemy import func as sa_func

    result = await session.execute(
        select(sa_func.count()).where(
            Run.pipeline_id == pipeline_id,
            Run.status.in_(_ACTIVE_STATUSES),
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
    payload_hash = hashlib.sha256(f"agent_signal:{trigger.id}".encode()).hexdigest()
    event = TriggerEvent(
        organisation_id=org_id,
        trigger_id=trigger.id,
        trigger_type="agent_signal",
        raw_payload_hash=payload_hash,
        validation_result=result,
        run_id=run_id,
        error_detail=error_detail,
    )
    session.add(event)
    await session.flush()
    return event
