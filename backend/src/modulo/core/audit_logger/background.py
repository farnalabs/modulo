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
:func:`record_suite_run_audit` applies the same guard to a single SuiteRun
(FAR-1561): it re-selects the row and only appends when its live ``state`` is
one the caller's event claims (a created event expects ``pending``, the start
event expects the ``pending -> running`` edge to have landed, the terminal
event expects a terminal state).
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
from modulo.db.models.eval_suite_run import SuiteRun, SuiteRunState
from modulo.db.models.run import Run
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)

__all__ = [
    "SUITE_RUN_NON_PENDING_STATES",
    "SUITE_RUN_PENDING_STATES",
    "SUITE_RUN_TERMINAL_STATES",
    "append_background_audit_event",
    "record_run_state_change_audits",
    "record_suite_run_audit",
]

#: ``error_detail`` is a free-text column; cap it so one sweep's detail cannot
#: bloat a chained audit row.
_MAX_DETAIL_CHARS = 500

#: The freshly-created SuiteRun state the ``suite_run_created`` event must
#: re-select (the create event is written after the creation transaction
#: committed, and before the execution job is enqueued).
SUITE_RUN_PENDING_STATES: frozenset[str] = frozenset({SuiteRunState.PENDING.value})

#: SuiteRun states in which the ``pending -> running`` transition has landed —
#: every state except ``pending``. A run still ``pending`` when the execution
#: job's audit runs means that job's transaction rolled back, so the start did
#: NOT land: the phantom-event guard for ``suite_run_started``.
SUITE_RUN_NON_PENDING_STATES: frozenset[str] = frozenset(
    state.value for state in SuiteRunState if state is not SuiteRunState.PENDING
)

#: Terminal SuiteRun states (``completed``/``partial``/``failed``/``cancelled``)
#: — the guard for the terminal event.
SUITE_RUN_TERMINAL_STATES: frozenset[str] = frozenset(
    state.value for state in SuiteRunState if state not in (SuiteRunState.PENDING, SuiteRunState.RUNNING)
)


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


async def record_suite_run_audit(
    factory: async_sessionmaker[AsyncSession],
    *,
    suite_run_id: uuid.UUID,
    org_id: uuid.UUID,
    event_type: str,
    expected_states: Collection[str],
    actor_source: str,
    log_key: str,
    summary_prefix: str,
    payload_json: Mapping[str, Any] | None = None,
) -> bool:
    """Record one SYSTEM-actor event for a SuiteRun lifecycle change (FAR-1561).

    A SuiteRun is an org-owned row created, started and terminalised by
    background jobs (``fire_suite_run_trigger`` / ``execute_suite_run``) with no
    HTTP principal in scope, so each change lands on the org's audit chain with
    a SYSTEM actor. The caller writes post-commit, so the row is RE-SELECTED
    here and the event is only appended when the live ``state`` is one the
    caller's event claims — the phantom-event guard: an id collected inside a
    transaction that later rolled back is not evidence the change happened.

    Args:
        factory: Session factory for the fresh audit session (never a caller's
            session — the append must outlive the transaction it records).
        suite_run_id: The SuiteRun to re-select.
        org_id: Owning organisation (bound as the RLS org context).
        event_type: Chained-audit event type (``suite_run_created`` /
            ``suite_run_started`` / ``suite_run_completed``).
        expected_states: Live states the change must have actually landed on;
            a run re-selected in any other state is skipped with a log.
        actor_source: Which background process caused the change
            (``fire_suite_run_trigger``, ``execute_suite_run``).
        log_key: Log key for skip/append failures.
        summary_prefix: Human-readable prefix for the event's ``summary``
            payload, e.g. ``"SuiteRun created by"`` — composed as
            ``f"{summary_prefix} {actor_source}"``.
        payload_json: Extra payload keys merged over the row-derived fields.

    Returns:
        ``True`` when the event was recorded, ``False`` when it was skipped
        (missing/cross-org row or unexpected state) or the append failed. Both
        skips and failures are logged; only ``CancelledError`` propagates.
    """
    if not event_type.strip():
        raise ValueError("record_suite_run_audit requires a non-empty event_type")
    if not actor_source.strip():
        raise ValueError("record_suite_run_audit requires a non-empty actor_source")
    if not expected_states:
        raise ValueError("record_suite_run_audit requires at least one expected state")

    try:
        async with factory() as session, session.begin():
            await set_rls_org(session, org_id)
            run = (
                await session.execute(
                    select(SuiteRun).where(
                        SuiteRun.id == suite_run_id,
                        SuiteRun.organisation_id == org_id,
                    )
                )
            ).scalar_one_or_none()
            if run is None:
                _log.warning(
                    "audit.background.suite_run_skipped",
                    extra={
                        "org_id": str(org_id),
                        "suite_run_id": str(suite_run_id),
                        "event_type": event_type,
                        "reason": "missing_or_cross_org",
                    },
                )
                return False
            if run.state not in expected_states:
                _log.warning(
                    "audit.background.suite_run_skipped",
                    extra={
                        "org_id": str(org_id),
                        "suite_run_id": str(suite_run_id),
                        "event_type": event_type,
                        "reason": "unexpected_state",
                        "suite_run_state": run.state,
                        "expected_states": sorted(expected_states),
                    },
                )
                return False
            payload: dict[str, Any] = {
                "summary": f"{summary_prefix} {actor_source}",
                "suite_run_id": str(run.id),
                "suite_id": str(run.suite_id),
                "dataset_id": str(run.dataset_id),
                "state": run.state,
                "total_cases": run.total_cases,
                "passed_cases": run.passed_cases,
                "failed_cases": run.failed_cases,
                "excluded_case_count": run.excluded_case_count,
                "error_detail": (run.error_detail or "")[:_MAX_DETAIL_CHARS] or None,
            }
            if payload_json:
                payload.update(payload_json)
            await append_audit_event(
                session,
                org_id=org_id,
                event_type=event_type,
                actor_user_id=None,
                resource_type="suite_run",
                resource_id=run.id,
                payload_json=_system_payload(payload, actor_source=actor_source),
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            log_key,
            extra={
                "org_id": str(org_id),
                "suite_run_id": str(suite_run_id),
                "event_type": event_type,
            },
            exc_info=True,
        )
        return False
    return True
