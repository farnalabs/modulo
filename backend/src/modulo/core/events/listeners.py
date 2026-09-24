"""SQLAlchemy event listeners that publish resource-change events to the EventBus.

FAR-250: delivery is deferred to COMMIT. The mapper ``after_insert`` hook
fires at flush time, before the transaction commits — scheduling the publish
there emitted phantom events for rolled-back inserts. Snapshots are now
queued on the owning session and delivered by an ``after_commit`` hook (or
dropped on an outermost rollback), so a rolled-back transaction never emits.

The local fan-out is NEVER removed (it feeds the dashboard panel + SSE when
Redis is down). For ``notification`` creates the Redis leg is suppressed
(``broadcast_redis=False``): the notifier's post-commit publish owns the
single Redis message per create — no double-publish.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session, SessionTransaction, object_session

from modulo.core.events.event_bus import get_event_bus
from modulo.core.events.notification_events import (
    NOTIFIER_SESSION_KEY,
    RESOURCE_TYPE_NOTIFICATION,
    notification_event_fields,
)
from modulo.db.models.agent import Agent
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.feedback_record import FeedbackRecord
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.notification import Notification
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import Run
from modulo.db.models.schema import Schema
from modulo.db.models.team import Team
from modulo.db.models.trigger import Trigger

_log = logging.getLogger(__name__)

_RESOURCE_TYPES: dict[type, str] = {
    Run: "run",
    Pipeline: "pipeline",
    Agent: "agent",
    Schema: "schema",
    ConnectorInstance: "connector",
    ModelBackend: "model_backend",
    Team: "team",
    Trigger: "trigger",
    EvalDefinition: "eval",
    FeedbackRecord: "feedback",
    LibraryPrimitive: "library",
    Notification: RESOURCE_TYPE_NOTIFICATION,
}

_ACTION_MAP: dict[str, str] = {
    "after_insert": "created",
    "after_update": "updated",
    "after_delete": "deleted",
}

# session.info keys for the commit-deferred delivery queue.
_PENDING_KEY = "_modulo_pending_events"
_HOOKED_KEY = "_modulo_commit_hooks"

_background_tasks: set[asyncio.Task[Any]] = set()
_version_counters: dict[str, int] = defaultdict(int)
_version_counter_lock: threading.Lock = threading.Lock()
_listeners_registered: bool = False


@dataclass(frozen=True)
class _PendingEvent:
    """Immutable snapshot of one resource-change event, queued until commit."""

    org_id: str
    resource_type: str
    resource_id: str
    action_name: str
    version: int
    broadcast_redis: bool
    extra: dict[str, Any] = field(default_factory=dict)


def _safe_str_attr(target: Any, attr: str, resource_type: str, action_name: str) -> str | None:
    """Safely extract a string attribute from *target*, logging on failure."""
    try:
        val = getattr(target, attr, None)
    except Exception:
        _log.warning(
            "event_listener.attr_error_%s",
            attr.replace(".", "_"),
            extra={"resource_type": resource_type, "action": action_name},
            exc_info=True,
        )
        return None
    if val is None:
        attr_name = attr.replace(".", "_")
        _log.warning(
            "event_listener.null_%s",
            attr_name,
            extra={"resource_type": resource_type, "action": action_name},
        )
        return None
    return str(val)


def _resolve_resource_type(target: Any, action: str) -> str | None:
    """Return the resource type for *target*, logging and returning ``None`` if unknown."""
    resource_type = _RESOURCE_TYPES.get(type(target))
    if resource_type is None:
        _log.warning(
            "event_listener.unknown_model",
            extra={"model": type(target).__name__, "action": action},
        )
    return resource_type


def _resolve_action_name(action: str) -> str | None:
    """Return the canonical action name for *action*, logging and returning ``None`` if unknown."""
    action_name = _ACTION_MAP.get(action)
    if action_name is None:
        _log.warning(
            "event_listener.unknown_action",
            extra={"action": action},
        )
    return action_name


def _get_running_loop(resource_type: str, action_name: str) -> asyncio.AbstractEventLoop | None:
    """Return the running event loop, logging and returning ``None`` if none is running."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        _log.warning(
            "event_listener.no_running_loop",
            extra={"resource_type": resource_type, "action": action_name},
        )
        return None


def next_event_version(org_id: str) -> int:
    """Atomically increment and return the per-org version counter."""
    with _version_counter_lock:
        version = _version_counters[org_id] + 1
        _version_counters[org_id] = version
    return version


# Backwards-compatible private alias (older tests/modules reference it).
_next_version = next_event_version


def _on_task_done(
    task: asyncio.Task[Any],
    resource_type: str,
    action_name: str,
    org_id: str,
) -> None:
    """Discard a finished publish task and log any failure or cancellation."""
    _background_tasks.discard(task)
    if task.cancelled():
        _log.warning(
            "event_listener.task_cancelled",
            extra={"resource_type": resource_type, "action": action_name, "org_id": org_id},
        )
        return
    exc = task.exception()
    if exc is not None:
        _log.warning(
            "event_listener.publish_failed",
            exc_info=exc,
            extra={"resource_type": resource_type, "action": action_name, "org_id": org_id},
        )


def _schedule_event_publish(loop: asyncio.AbstractEventLoop, pending: _PendingEvent) -> None:
    """Publish one pending resource-change event as a background task."""
    task = loop.create_task(
        get_event_bus().publish(
            org_id=pending.org_id,
            resource_type=pending.resource_type,
            resource_id=pending.resource_id,
            action=pending.action_name,
            version=pending.version,
            broadcast_redis=pending.broadcast_redis,
            extra=pending.extra or None,
        ),
    )
    _background_tasks.add(task)
    task.add_done_callback(
        lambda t: _on_task_done(t, pending.resource_type, pending.action_name, pending.org_id),
    )


def _deliver_pending(session: Session) -> None:
    """``after_commit`` hook: deliver every event snapshotted during the transaction."""
    pending: list[_PendingEvent] = session.info.pop(_PENDING_KEY, [])
    if not pending:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _log.warning("event_listener.no_running_loop", extra={"pending_count": len(pending)})
        return
    for snap in pending:
        _schedule_event_publish(loop, snap)


def _on_soft_rollback(session: Session, previous_transaction: SessionTransaction) -> None:
    """``after_soft_rollback`` hook: drop pending events on the OUTERMOST rollback only.

    SQLAlchemy invokes this as ``after_soft_rollback(session,
    previous_transaction)`` (see ``SessionEvents.after_soft_rollback``): the
    second argument is the just-closed ``SessionTransaction`` marker and is
    ALWAYS a truthy object — never a bool. (An earlier revision misread it as
    an ``outter: bool`` flag, so ``if not outter`` never fired and EVERY
    rollback — including inner ``begin_nested()`` savepoint rollbacks —
    silently cleared the queue, losing events whose rows still commit with the
    outer transaction.)

    Outermost-ness is therefore derived from real session state: after an
    inner savepoint rollback the enclosing transaction is still on the stack
    (``session.in_transaction()`` is True) and the queue must survive — the
    ``begin_nested()`` bounded-retry paths (audit_logger, cost_controller
    finalize, bundled_runner health_probe) roll back savepoints while the
    outer transaction still commits. Only when the root transaction has closed
    (``in_transaction()`` False) is the queue dropped (rollback-no-phantom).

    A savepoint-scoped insert that later rolls back while the outer
    transaction commits can therefore still emit — a known, narrow residual
    accepted in favour of never losing events on the dominant savepoint paths.
    """
    if session.in_transaction():
        # Inner savepoint rollback: an enclosing transaction still holds the
        # stack and will deliver the queue on its commit. Keep pending events.
        return
    dropped: list[_PendingEvent] = session.info.pop(_PENDING_KEY, [])
    if dropped:
        _log.info(
            "event_listener.pending_dropped_on_rollback",
            extra={"pending_count": len(dropped)},
        )


def _ensure_commit_hooks(session: Session) -> None:
    """Register the after_commit/after_rollback hooks once per session instance."""
    if session.info.get(_HOOKED_KEY):
        return
    session.info[_HOOKED_KEY] = True
    event.listen(session, "after_commit", _deliver_pending)
    event.listen(session, "after_soft_rollback", _on_soft_rollback)


def _queue_for_commit(session: Session, pending: _PendingEvent) -> None:
    """Queue *pending* for delivery when *session*'s transaction commits."""
    queue: list[_PendingEvent] = session.info.setdefault(_PENDING_KEY, [])
    queue.append(pending)
    _ensure_commit_hooks(session)


def _notification_broadcast_and_extra(
    target: Any,
    resource_id: str,
    session: Session | None,
) -> tuple[bool, dict[str, Any]]:
    """Notification creates: payload fields + double-publish suppression.

    Returns ``(broadcast_redis, extra)``. The Redis leg is suppressed ONLY
    when the creating session is notifier-owned (marker set by
    ``Notifier._dispatch_inline``): the notifier's post-commit publish owns
    the single Redis message for that create (FAR-250 double-publish
    neutralization). Creates on any other session (bundled-runner health
    probe, direct CRUD) keep the listener's post-commit Redis leg so they
    still reach other processes. The local fan-out always stays.
    """
    broadcast_redis = True
    if session is not None and session.info.get(NOTIFIER_SESSION_KEY):
        broadcast_redis = False
    return broadcast_redis, notification_event_fields(target, resource_id)


def _make_listener(action: str) -> Callable[[Any, Any, Any], None]:
    """Return an event-listener function for the given SQLAlchemy action.

    The listener resolves the event fields at flush time (snapshots must not
    read mutated state later) and queues delivery on the owning session's
    commit. Targets with no owning session (detached/manual test instances)
    publish immediately, preserving the historic no-session behaviour.
    """

    def listener(_mapper: object, _connection: object, target: Any) -> None:
        resource_type = _resolve_resource_type(target, action)
        if resource_type is None:
            return

        action_name = _resolve_action_name(action)
        if action_name is None:
            return

        org_id = _safe_str_attr(target, "organisation_id", resource_type, action_name)
        if org_id is None:
            return

        resource_id = _safe_str_attr(target, "id", resource_type, action_name)
        if resource_id is None:
            return

        broadcast_redis = True
        extra: dict[str, Any] = {}
        session = _resolve_session(target)
        if resource_type == RESOURCE_TYPE_NOTIFICATION:
            broadcast_redis, extra = _notification_broadcast_and_extra(target, resource_id, session)

        if session is not None:
            pending = _PendingEvent(
                org_id=org_id,
                resource_type=resource_type,
                resource_id=resource_id,
                action_name=action_name,
                version=next_event_version(org_id),
                broadcast_redis=broadcast_redis,
                extra=extra,
            )
            _queue_for_commit(session, pending)
            return

        # No owning session (detached instance / unit-test target): publish
        # immediately, exactly as the pre-FAR-250 listener did.
        loop = _get_running_loop(resource_type, action_name)
        if loop is None:
            return
        pending = _PendingEvent(
            org_id=org_id,
            resource_type=resource_type,
            resource_id=resource_id,
            action_name=action_name,
            version=next_event_version(org_id),
            broadcast_redis=broadcast_redis,
            extra=extra,
        )
        _schedule_event_publish(loop, pending)

    return listener


def _resolve_session(target: Any) -> Session | None:
    """Return the sync Session owning *target*, or ``None`` when detached.

    ``object_session`` returns the sync Session even under async usage (the
    AsyncSession wraps one sync Session); the ``sync_session`` getattr keeps
    this safe if a wrapped instance ever reaches here. Non-Session values
    (mock targets in unit tests) fall through to the immediate path.
    """
    try:
        session = object_session(target)
    except Exception:
        return None
    if session is None:
        return None
    session = getattr(session, "sync_session", session)
    if not isinstance(session, Session):
        return None
    return session


def register_listeners() -> None:
    """Register all model event listeners. Call once at startup."""
    global _listeners_registered
    if _listeners_registered:
        _log.warning("event_listeners.already_registered")
        return
    models = list(_RESOURCE_TYPES)
    for action in ("after_insert", "after_update", "after_delete"):
        listener_fn = _make_listener(action)
        for model in models:
            event.listen(model, action, listener_fn)
    _listeners_registered = True
    _log.info("event_listeners.registered", extra={"model_count": len(models)})
