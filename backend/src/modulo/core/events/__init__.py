"""Event system for real-time resource-change notifications via SSE."""

from modulo.core.events.event_bus import EventBus, configure_event_bus, get_event_bus
from modulo.core.events.listeners import register_listeners
from modulo.core.events.redis_broker import RedisEventBroker

__all__ = [
    "EventBus",
    "RedisEventBroker",
    "configure_event_bus",
    "get_event_bus",
    "register_listeners",
]
