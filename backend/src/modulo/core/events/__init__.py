"""Event system for real-time resource-change notifications via SSE."""

from __future__ import annotations

from typing import TYPE_CHECKING

from modulo.core.events.event_bus import EventBus, configure_event_bus, get_event_bus
from modulo.core.events.listeners import register_listeners

if TYPE_CHECKING:
    from modulo.core.events.redis_broker import RedisEventBroker


def __getattr__(name: str) -> object:
    if name == "RedisEventBroker":
        from modulo.core.events.redis_broker import RedisEventBroker
        return RedisEventBroker
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


__all__ = [
    "EventBus",
    "RedisEventBroker",
    "configure_event_bus",
    "get_event_bus",
    "register_listeners",
]
