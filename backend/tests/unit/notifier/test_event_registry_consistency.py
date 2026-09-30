"""Consistency tests for the notification event registry (FAR-1319).

The event surface is declared in three places that have historically drifted:

* ``api.routes.admin_notifications.AVAILABLE_EVENTS`` — the webhook-subscribable
  list served by ``GET /api/v1/admin/notifications/available-events`` and used
  to validate endpoint subscriptions;
* ``core.notifier.event_mapper._EVENT_CONFIG`` — the in-app render config the
  ``NotificationEventMapper`` looks up (an event missing here hits the
  unknown-event no-op and its in-app notification silently never renders);
* the title/body/action-url template maps the mapper renders from.

A dispatchable event listed as "available" but absent from ``_EVENT_CONFIG`` is
exactly the ``circuit_breaker_tripped`` drift FAR-1319 fixed, so it gets a
regression test rather than a one-off correction.
"""

from __future__ import annotations

import pytest

from modulo.api.routes.admin_notifications import AVAILABLE_EVENTS
from modulo.core.notifier.event_mapper import (
    _ACTION_URL_TEMPLATES,
    _BODY_TEMPLATES,
    _EVENT_CONFIG,
    _TITLE_TEMPLATES,
)

_CONFIGURED_EVENTS = tuple(_EVENT_CONFIG)


def test_available_events_are_not_duplicated() -> None:
    assert len(AVAILABLE_EVENTS) == len(set(AVAILABLE_EVENTS))


@pytest.mark.parametrize("event_type", AVAILABLE_EVENTS)
def test_available_event_has_render_config(event_type: str) -> None:
    """Every subscribable event must resolve to an in-app notification config."""
    assert event_type in _EVENT_CONFIG


@pytest.mark.parametrize("event_type", AVAILABLE_EVENTS)
def test_available_event_has_render_templates(event_type: str) -> None:
    """Every subscribable event must render a title, a body and an action URL entry."""
    assert event_type in _TITLE_TEMPLATES
    assert event_type in _BODY_TEMPLATES
    assert event_type in _ACTION_URL_TEMPLATES


@pytest.mark.parametrize("event_type", _CONFIGURED_EVENTS)
def test_configured_event_has_render_templates(event_type: str) -> None:
    """The reverse direction: a config entry without its render templates is the
    same drift class (the mapper would fall back to a placeholder)."""
    assert event_type in _TITLE_TEMPLATES
    assert event_type in _BODY_TEMPLATES
    assert event_type in _ACTION_URL_TEMPLATES
