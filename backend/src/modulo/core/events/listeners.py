"""SQLAlchemy event listeners that publish resource-change events to the EventBus."""

from __future__ import annotations

import asyncio
import logging
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
_listeners_registered: bool = False


def _safe_str_attr(target: Any, attr: str, resource_type: str, action_name: str) -> str | None:
    """Safely extract a string attribute from *target*, logging on failure."""
    try:
        val = getattr(target, attr)
    except AttributeError:
        _log.warning(
            "event_listener.no_%s",
            attr.replace(".", "_"),
            extra={"resource_type": resource_type, "action": action_name},
        )
        return None
    if val is None:
        _log.warning(
            "event_listener.null_%s",
            attr.replace(".", "_"),
            extra={"resource_type": resource_type, "action": action_name},
        )
        return None
    return str(val)


def _make_listener(action: str) -> Callable[[Any, Any, Any], None]:
    """Return an event-listener function for the given SQLAlchemy action."""

    def listener(_mapper: object, _connection: object, target: Any) -> None:
        resource_type = _RESOURCE_TYPES.get(type(target))
        if resource_type is None:
            _log.warning(
                "event_listener.unknown_model",
                extra={"model": type(target).__name__, "action": action},
            )
            return

        action_name = _ACTION_MAP.get(action)
        if action_name is None:
            _log.warning(
                "event_listener.unknown_action",
                extra={"action": action},
            )
            return

        org_id = _safe_str_attr(target, "organisation_id", resource_type, action_name)
        if org_id is None:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _log.warning(
                "event_listener.no_running_loop",
                extra={"resource_type": resource_type, "action": action_name},
            )
            return

        resource_id = _safe_str_attr(target, "id", resource_type, action_name)
        if resource_id is None:
            return

        version = _version_counters[org_id] + 1
        _version_counters[org_id] = version

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

        def _on_task_done(t: asyncio.Task[Any]) -> None:
            _background_tasks.discard(t)
            if t.cancelled():
                _log.warning("event_listener.task_cancelled")
                return
            exc = t.exception()
            if exc is not None:
                _log.warning("event_listener.publish_failed", exc_info=exc)

        task.add_done_callback(_on_task_done)

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
