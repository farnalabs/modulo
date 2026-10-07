"""Audit appends for NON-request write paths (FAR-1549).

FAR-1472 closed the request-layer gap: ``audited()`` / ``audited_system()``
cover mutating REST routes and MCP tools, and ``mcp_audited`` covers the MCP
tool surface. What those helpers cannot reach is the set of writes that never
see a request at all — SAQ cron sweeps, reconcilers and boot-time seeds. Those
paths mutate org-owned state (runs terminalised, runs parked, runs purged)
with no HTTP principal in scope, so they need the same evidence with an honest
machine actor instead.

Three rules this module enforces so a caller cannot get them wrong:

* **No fabricated actor.** Every event is written with ``actor_user_id=NULL``
  plus ``payload["actor"] = SYSTEM_ACTOR`` (the same convention
  ``audited_system`` uses) and an ``actor_source`` naming the background
  process that caused the change.
* **Org context is set here, inside the transaction.** ``audit_events`` carries
  an org-only RLS policy; ``SET LOCAL`` reverts on COMMIT, so every append
  establishes ``app.organisation_id`` for the event's org in the fresh
  transaction it opens.
* **Fail open, with a log.** These writes follow an already-committed
  mutation. Rolling that mutation back because an audit append failed would be
  worse than losing one event, so a failure is logged under the caller's
  ``log_key`` and swallowed. (The destructive *act* itself is never made
  best-effort — only its record.)

The batch helper (:func:`record_run_state_change_audits`) additionally
re-selects each run and drops any whose live status is not one the caller
expects. Background sweeps collect their ids inside a transaction that may
later roll back, so an id alone is not evidence the change happened — the
status check is the phantom-event guard (the same rule
``cron_helpers._record_fact_for_terminalized_run`` applies to facts).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modulo.core.audit_logger import append_audit_event
from modulo.core.audit_logger.labels import SYSTEM_ACTOR
from modulo.db.models.run import Run
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)

__all__ = [
    "append_background_audit_event",
    "record_run_state_change_audits",
]

#: ``error_detail`` is a free-text column; cap it so one sweep's detail cannot
#: bloat a chained audit row.
_MAX_DETAIL_CHARS = 500


def _system_payload(
    payload_json: Mapping[str, Any] | None,
    *,
    actor_source: str,
) -> dict[str, Any]:
    """Copy the caller's payload and stamp the honest machine-actor markers."""
    payload = dict(payload_json or {})
    payload.setdefault("actor", SYSTEM_ACTOR)
    payload.setdefault("actor_source", actor_source)
    return payload


async def append_background_audit_event(
    factory: async_sessionmaker[AsyncSession],
    *,
    org_id: uuid.UUID,
    event_type: str,
    resource_type: str,
    resource_id: uuid.UUID | None = None,
    actor_source: str,
    payload_json: Mapping[str, Any] | None = None,
    log_key: str,
) -> bool:
    """Append one SYSTEM-actor audit event in its own session and transaction.

    Opens a fresh session (never a caller's session) so the append can neither
    roll back nor be rolled back by the mutation it records, sets the RLS org
    context inside that transaction, and appends.

    Returns:
        ``True`` when the event was recorded, ``False`` when the append failed
        and was logged under ``log_key`` (fail open — see the module
        docstring).
    """
    if not event_type.strip():
        raise ValueError("append_background_audit_event requires a non-empty event_type")
    if not actor_source.strip():
        raise ValueError("append_background_audit_event requires a non-empty actor_source")
    try:
        async with factory() as session, session.begin():
            await set_rls_org(session, org_id)
            await append_audit_event(
                session,
                org_id=org_id,
                event_type=event_type,
                actor_user_id=None,
                resource_type=resource_type,
                resource_id=resource_id,
                payload_json=_system_payload(payload_json, actor_source=actor_source),
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            log_key,
            extra={
                "org_id": str(org_id),
                "event_type": event_type,
                "resource_type": resource_type,
                "resource_id": str(resource_id) if resource_id else None,
            },
            exc_info=True,
        )
        return False
    return True


async def record_run_state_change_audits(
    factory: async_sessionmaker[AsyncSession],
    entries: Sequence[tuple[uuid.UUID, uuid.UUID]],
    *,
    event_type: str,
    expected_statuses: Collection[str],
    actor_source: str,
    log_key: str,
    summary_prefix: str,
) -> int:
    """Record one SYSTEM-actor event per background run-state change.

    Args:
        factory: Session factory for the fresh audit sessions.
        entries: ``(run_id, org_id)`` pairs collected by the sweep.
        event_type: Chained-audit event type (``audit_events.event_type``).
        expected_statuses: Live statuses the change must have actually landed
            on. A run whose re-selected status is not in this set was NOT
            changed (its sweep transaction rolled back after the id was
            collected) and is skipped with a log — the phantom-event guard.
        actor_source: Which background process caused the change
            (``dispatcher_reconcile``, ``stale_run_recovery``,
            ``slot_reconciliation``, ``hitl_park_sweep``, ...).
        log_key: Log key for append failures.
        summary_prefix: Human-readable prefix for the event's ``summary``
            payload, e.g. ``"Run terminalised by"``.

    Returns:
        The number of events recorded. Failures are logged and never raised:
        this runs after the sweep's own transaction has committed.
    """
    if not entries:
        return 0
    if not event_type.strip():
        raise ValueError("record_run_state_change_audits requires a non-empty event_type")
    if not actor_source.strip():
        raise ValueError("record_run_state_change_audits requires a non-empty actor_source")
    if not expected_statuses:
        raise ValueError("record_run_state_change_audits requires at least one expected status")

    by_org: dict[uuid.UUID, list[uuid.UUID]] = {}
    for run_id, org_id in entries:
        by_org.setdefault(org_id, []).append(run_id)

    recorded = 0
    for org_id, run_ids in by_org.items():
        try:
            recorded += await _record_one_org(
                factory,
                org_id=org_id,
                run_ids=run_ids,
                event_type=event_type,
                expected_statuses=expected_statuses,
                actor_source=actor_source,
                log_key=log_key,
                summary_prefix=summary_prefix,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # One org's audit failure must never stop the others (or the sweep
            # that is already finished).
            _log.warning(
                log_key,
                extra={"org_id": str(org_id), "event_type": event_type, "run_count": len(run_ids)},
                exc_info=True,
            )
    return recorded


async def _record_one_org(
    factory: async_sessionmaker[AsyncSession],
    *,
    org_id: uuid.UUID,
    run_ids: list[uuid.UUID],
    event_type: str,
    expected_statuses: Collection[str],
    actor_source: str,
    log_key: str,
    summary_prefix: str,
) -> int:
    """Record one organisation's run-state events in a single transaction."""
    recorded = 0
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        runs = (
            (
                await session.execute(
                    select(Run).where(
                        Run.id.in_(run_ids),
                        Run.organisation_id == org_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for run in runs:
            if run.status not in expected_statuses:
                _log.warning(
                    "audit.background.run_state_change_skipped",
                    extra={
                        "org_id": str(org_id),
                        "run_id": str(run.id),
                        "event_type": event_type,
                        "run_status": run.status,
                    },
                )
                continue
            detail = run.error_detail
            payload: dict[str, Any] = {
                "summary": f"{summary_prefix} {actor_source}",
                "pipeline_run_id": str(run.id),
                "pipeline_id": str(run.pipeline_id) if run.pipeline_id else None,
                "run_status": run.status,
                "error_code": run.error_code,
                "error_detail": detail[:_MAX_DETAIL_CHARS] if detail else None,
            }
            try:
                await append_audit_event(
                    session,
                    org_id=org_id,
                    event_type=event_type,
                    actor_user_id=None,
                    resource_type="run",
                    resource_id=run.id,
                    payload_json=_system_payload(payload, actor_source=actor_source),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Savepoint-isolated by append_audit_event's own begin_nested;
                # one bad event must not discard its siblings in this org.
                _log.warning(
                    log_key,
                    extra={
                        "org_id": str(org_id),
                        "run_id": str(run.id),
                        "event_type": event_type,
                    },
                    exc_info=True,
                )
                continue
            recorded += 1
    return recorded
