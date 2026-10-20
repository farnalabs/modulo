"""FAR-1344 capacity-event vocabulary wiring.

The notifier vocabulary is spread across three places that must stay in sync
(the ``__init__`` constants, ``event_mapper._EVENT_CONFIG`` and
``admin_notifications.AVAILABLE_EVENTS``); a value added to one and not the
others silently drops the event (no in-app render, or no webhook subscription).
These tests pin the whole set, the ladder's per-rung severity, the render
templates and the subscribe/in-app preference surface.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.api.routes.admin_notifications import AVAILABLE_EVENTS
from modulo.core import notifier as notifier_module
from modulo.core.notifier.event_mapper import (
    _ACTION_URL_TEMPLATES,
    _BODY_TEMPLATES,
    _EVENT_CONFIG,
    _TITLE_TEMPLATES,
    NotificationEventMapper,
    notification_categories,
)
from modulo.db.models.notification import Notification

_CAPACITY_EVENTS = (
    "queue_pressure_notice",
    "queue_pressure_warning",
    "queue_pressure_critical",
    "capacity_backpressure",
    "capacity_saturation",
    "capacity_recovered",
    "capacity_redis_pressure",
)

_EXPECTED_LEVELS = {
    "queue_pressure_notice": "info",
    "queue_pressure_warning": "warning",
    "queue_pressure_critical": "error",
    "capacity_backpressure": "warning",
    "capacity_saturation": "warning",
    "capacity_recovered": "info",
    "capacity_redis_pressure": "warning",
}

_CONSTANT_NAMES = {
    "queue_pressure_notice": "EVENT_QUEUE_PRESSURE_NOTICE",
    "queue_pressure_warning": "EVENT_QUEUE_PRESSURE_WARNING",
    "queue_pressure_critical": "EVENT_QUEUE_PRESSURE_CRITICAL",
    "capacity_backpressure": "EVENT_CAPACITY_BACKPRESSURE",
    "capacity_saturation": "EVENT_CAPACITY_SATURATION",
    "capacity_recovered": "EVENT_CAPACITY_RECOVERED",
    "capacity_redis_pressure": "EVENT_CAPACITY_REDIS_PRESSURE",
}


@pytest.mark.parametrize("event", _CAPACITY_EVENTS)
def test_event_constant_exported(event: str) -> None:
    name = _CONSTANT_NAMES[event]
    assert name in notifier_module.__all__
    assert getattr(notifier_module, name) == event


@pytest.mark.parametrize("event", _CAPACITY_EVENTS)
def test_event_has_render_config(event: str) -> None:
    assert event in _EVENT_CONFIG
    config = _EVENT_CONFIG[event]
    assert config["category"].startswith("capacity.")
    assert config["level"] == _EXPECTED_LEVELS[event]
    assert config["scope"] in ("org", "admin")


@pytest.mark.parametrize("event", _CAPACITY_EVENTS)
def test_event_has_templates_and_action_url(event: str) -> None:
    assert event in _TITLE_TEMPLATES
    assert event in _BODY_TEMPLATES
    assert event in _ACTION_URL_TEMPLATES
    assert _ACTION_URL_TEMPLATES[event] == "/analytics"


@pytest.mark.parametrize("event", _CAPACITY_EVENTS)
def test_event_is_webhook_subscribable(event: str) -> None:
    assert event in AVAILABLE_EVENTS


def test_queue_ladder_severity_escalates() -> None:
    levels = [
        _EVENT_CONFIG[e]["level"]
        for e in ("queue_pressure_notice", "queue_pressure_warning", "queue_pressure_critical")
    ]
    assert levels == ["info", "warning", "error"]


def test_capacity_categories_are_user_opt_out_able() -> None:
    categories = notification_categories()
    for event in _CAPACITY_EVENTS:
        assert _EVENT_CONFIG[event]["category"] in categories


def test_capacity_categories_are_distinct() -> None:
    categories = [_EVENT_CONFIG[e]["category"] for e in _CAPACITY_EVENTS]
    assert len(set(categories)) == len(categories)


@pytest.mark.parametrize("event", _CAPACITY_EVENTS)
def test_every_webhook_event_is_renderable(event: str) -> None:
    """Every webhook-subscribable event must resolve in the in-app mapper.

    ``AVAILABLE_EVENTS`` is documented as a subset of ``_EVENT_CONFIG``; a
    subscribable event with no render config produces a webhook but never an
    in-app record, which is exactly the "invisible in practice" defect FAR-1344
    exists to fix.
    """
    assert event in _EVENT_CONFIG


_PAYLOADS = {
    "queue_pressure_notice": {
        "pipeline_name": "nightly",
        "pending_depth": 2,
        "oldest_pending_age_minutes": "1",
        "limit_label": "org run concurrency limit",
        "current_value": 5,
        "limit_value": 5,
        "suggested_action": "Raise the limit.",
    },
    "queue_pressure_warning": {
        "pipeline_name": "nightly",
        "pending_depth": 4,
        "oldest_pending_age_minutes": "12",
        "limit_label": "pipeline concurrency limit",
        "current_value": 2,
        "limit_value": 2,
        "suggested_action": "Raise the limit.",
    },
    "queue_pressure_critical": {
        "pipeline_name": "nightly",
        "pending_depth": 9,
        "oldest_pending_age_minutes": "40",
        "limit_label": "org sandbox concurrency limit",
        "current_value": 3,
        "limit_value": 3,
        "suggested_action": "Raise the limit.",
    },
    "capacity_backpressure": {
        "pipeline_name": "nightly",
        "reason_label": "queue depth is over the pipeline's limit (queue_depth=9 limit=5)",
        "pending_depth": 9,
        "suggested_action": "Raise the limit.",
    },
    "capacity_saturation": {
        "limit_label": "org run concurrency limit",
        "limit_value": 5,
        "sustained_minutes": 5,
        "pending_depth": 3,
        "suggested_action": "Raise the limit.",
    },
    "capacity_recovered": {
        "subject_label": "nightly",
        "previous_level": "warning",
    },
    "capacity_redis_pressure": {
        "used_memory_bytes": 900 * 1024 * 1024,
        "maxmemory_bytes": 1024 * 1024 * 1024,
        "used_memory_label": "900 MB",
        "maxmemory_label": "1 GB",
        "used_percent": 87.9,
        "policy": "volatile-lru",
    },
}


@pytest.mark.parametrize("event", _CAPACITY_EVENTS)
async def test_mapper_creates_notification_without_unknown_placeholder(event: str) -> None:
    mapper = NotificationEventMapper()
    session = AsyncMock()
    with patch(
        "modulo.core.notifier.event_mapper.create_notification",
        new_callable=AsyncMock,
        return_value=MagicMock(spec=Notification),
    ) as mock_create:
        result = await mapper.create_from_event(
            session,
            org_id=uuid.uuid4(),
            event_type=event,
            payload=dict(_PAYLOADS[event]),
        )
    assert result is not None
    title = mock_create.call_args.kwargs["title"]
    body = mock_create.call_args.kwargs["body"]
    assert "[unknown]" not in title
    assert "[unknown]" not in body
    assert mock_create.call_args.kwargs["level"] == _EXPECTED_LEVELS[event]
