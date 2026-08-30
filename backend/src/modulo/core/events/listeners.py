"""SQLAlchemy event listeners that publish resource-change events to the EventBus."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from sqlalchemy import event

from modulo.core.events.event_bus import get_event_bus
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
    Notification: "notification",
}

_ACTION_MAP: dict[str, str] = {
    "after_insert": "created",
    "after_update": "updated",
    "after_delete": "deleted",
}

_background_tasks: set[asyncio.Task[Any]] = set()
_version_counters: dict[str, int] = defaultdict(int)
_version_counter_lock: threading.Lock = threading.Lock()
_listeners_registered: bool = False


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


def _next_version(org_id: str) -> int:
    """Atomically increment and return the per-org version counter."""
    with _version_counter_lock:
        version = _version_counters[org_id] + 1
        _version_counters[org_id] = version
    return version


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


def _schedule_event_publish(
    loop: asyncio.AbstractEventLoop,
    org_id: str,
    resource_type: str,
    resource_id: str,
    action_name: str,
) -> None:
    """Publish a resource-change event as a background task."""
    version = _next_version(org_id)
    task = loop.create_task(
        get_event_bus().publish(
            org_id=org_id,
            resource_type=resource_type,
            resource_id=resource_id,
            action=action_name,
            version=version,
        ),
    )
    _background_tasks.add(task)
    task.add_done_callback(
        lambda t: _on_task_done(t, resource_type, action_name, org_id),
    )


def _make_listener(action: str) -> Callable[[Any, Any, Any], None]:
    """Return an event-listener function for the given SQLAlchemy action."""

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

        loop = _get_running_loop(resource_type, action_name)
        if loop is None:
            return

        resource_id = _safe_str_attr(target, "id", resource_type, action_name)
        if resource_id is None:
            return

        _schedule_event_publish(loop, org_id, resource_type, resource_id, action_name)

    return listener


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
