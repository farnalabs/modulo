"""FAR-2116 pipeline-decay event vocabulary wiring.

The notifier vocabulary is spread across places that must stay in sync (the
``notifier`` constants, ``event_mapper._EVENT_CONFIG`` and
``admin_notifications.AVAILABLE_EVENTS``); a value added to one and not the
others silently drops the event — no in-app render, or no webhook subscription.
These tests pin the whole set, the severity/scope, the render templates and the
subscribe/in-app preference surface, and they render the mapper against the
REAL payload the sweep builds (never a hand-rolled fixture that could drift
from it).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.api.routes.admin_notifications import AVAILABLE_EVENTS
from modulo.core import notifier as notifier_module
from modulo.core import pipeline_decay_alerts as decay
from modulo.core.notifier.event_mapper import (
    _ACTION_URL_TEMPLATES,
    _BODY_TEMPLATES,
    _EVENT_CONFIG,
    _TITLE_TEMPLATES,
    NotificationEventMapper,
    notification_categories,
)
from modulo.db.models.notification import Notification

_DECAY_EVENTS = ("pipeline_decay", "pipeline_decay_recovered")

_EXPECTED_LEVELS = {
    "pipeline_decay": "warning",
    "pipeline_decay_recovered": "info",
}

_CONSTANT_NAMES = {
    "pipeline_decay": "EVENT_PIPELINE_DECAY",
    "pipeline_decay_recovered": "EVENT_PIPELINE_DECAY_RECOVERED",
}


def _health(*, total: int, complete: int, watchdog: int = 0) -> decay.PipelineHealth:
    return decay.PipelineHealth(
        organisation_id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        pipeline_name="Branch Fixer",
        runs_total=total,
        runs_complete=complete,
        watchdog_firings=watchdog,
    )


def _payload(event: str) -> dict[str, object]:
    """The payload the sweep ACTUALLY dispatches for that event."""
    if event == "pipeline_decay":
        return decay.build_decay_payload(_health(total=20, complete=12, watchdog=1))
    return decay.build_recovered_payload(_health(total=20, complete=20))


@pytest.mark.parametrize("event", _DECAY_EVENTS)
def test_event_constant_exported(event: str) -> None:
    name = _CONSTANT_NAMES[event]
    assert name in notifier_module.__all__
    assert getattr(notifier_module, name) == event


@pytest.mark.parametrize("event", _DECAY_EVENTS)
def test_event_has_render_config(event: str) -> None:
    assert event in _EVENT_CONFIG
    config = _EVENT_CONFIG[event]
    assert config["category"].startswith("pipeline.decay")
    assert config["level"] == _EXPECTED_LEVELS[event]
    assert config["scope"] == "org"
    assert config["ttl_hours"] > 0


@pytest.mark.parametrize("event", _DECAY_EVENTS)
def test_event_has_templates_and_action_url(event: str) -> None:
    assert event in _TITLE_TEMPLATES
    assert event in _BODY_TEMPLATES
    action_url = _ACTION_URL_TEMPLATES[event]
    assert action_url is not None
    # The figures an operator wants to verify live on the analytics panel,
    # pre-filtered to the pipeline the alert names.
    assert action_url.startswith("/analytics?pipeline_id=")


@pytest.mark.parametrize("event", _DECAY_EVENTS)
def test_event_is_webhook_subscribable(event: str) -> None:
    assert event in AVAILABLE_EVENTS


@pytest.mark.parametrize("event", _DECAY_EVENTS)
def test_categories_are_user_opt_out_able(event: str) -> None:
    assert _EVENT_CONFIG[event]["category"] in notification_categories()


def test_decay_categories_are_distinct() -> None:
    categories = [_EVENT_CONFIG[event]["category"] for event in _DECAY_EVENTS]
    assert len(set(categories)) == len(categories)


@pytest.mark.parametrize("event", _DECAY_EVENTS)
def test_templates_render_the_sweeps_real_payload(event: str) -> None:
    payload = _payload(event)
    title = _TITLE_TEMPLATES[event].format(**payload)
    body = _BODY_TEMPLATES[event].format(**payload)
    assert "[unknown]" not in title
    assert "[unknown]" not in body
    assert "None" not in title
    assert "None" not in body
    assert payload["pipeline_name"] in title
    assert str(payload["window_minutes"]) in body


def test_action_url_resolves_to_the_pipeline() -> None:
    payload = _payload("pipeline_decay")
    url = _ACTION_URL_TEMPLATES["pipeline_decay"]
    assert url is not None
    assert url.format(**payload) == f"/analytics?pipeline_id={payload['pipeline_id']}"


@pytest.mark.parametrize("event", _DECAY_EVENTS)
async def test_mapper_creates_notification_without_unknown_placeholder(event: str) -> None:
    mapper = NotificationEventMapper()
    session = AsyncMock()
    payload = _payload(event)
    with patch(
        "modulo.core.notifier.event_mapper.create_notification",
        new_callable=AsyncMock,
        return_value=MagicMock(spec=Notification),
    ) as mock_create:
        result = await mapper.create_from_event(
            session,
            org_id=uuid.uuid4(),
            event_type=event,
            payload=dict(payload),
        )

    assert result is not None
    kwargs = mock_create.call_args.kwargs
    assert "[unknown]" not in kwargs["title"]
    assert "[unknown]" not in kwargs["body"]
    assert kwargs["level"] == _EXPECTED_LEVELS[event]
    assert kwargs["action_url"] == f"/analytics?pipeline_id={payload['pipeline_id']}"
