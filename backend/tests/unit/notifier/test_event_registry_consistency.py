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

import re
from pathlib import Path

import pytest

from modulo.api.routes.admin_notifications import AVAILABLE_EVENTS
from modulo.core.notifier.event_mapper import (
    _ACTION_URL_TEMPLATES,
    _BODY_TEMPLATES,
    _EVENT_CONFIG,
    _TITLE_TEMPLATES,
)

_CONFIGURED_EVENTS = tuple(_EVENT_CONFIG)

# The two en-US event-label maps the UI reads when it renders a registry event
# as a human-readable name: AdminNotificationDeliveryLogView's delivery-log
# event filter and TeamNotificationEndpoints' subscription picker. Both are fed
# from GET /api/v1/admin/notifications/available-events, i.e. AVAILABLE_EVENTS,
# and both fall back to the raw snake_case name when the key is missing - which
# is exactly the silent drift this test closes.
_LOCALE_PATH = Path(__file__).resolve().parents[4] / "frontend" / "src" / "locales" / "en-US.js"
_EVENT_LABEL_MAPS = (
    ("views", "AdminNotificationDeliveryLogView"),
    ("components", "TeamNotificationEndpoints"),
)
_LOCALE_PAIR_RE = re.compile(r'"([A-Za-z0-9_]+)"\s*:\s*"([^"]*)"')


def _locale_block(text: str, header: str) -> str:
    """Return the brace-balanced body of the ``"<header>"`` object literal."""
    start = text.index(header)
    open_brace = text.index("{", start)
    depth = 0
    for offset in range(open_brace, len(text)):
        char = text[offset]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace + 1 : offset]
    raise AssertionError(f"unterminated locale object for {header!r}")


def _event_label_map(section: str, component: str) -> dict[str, str]:
    text = _LOCALE_PATH.read_text(encoding="utf-8")
    body = _locale_block(text, f'"{section}": {{')
    body = _locale_block(body, f'"{component}": {{')
    return {match.group(1): match.group(2) for match in _LOCALE_PAIR_RE.finditer(body)}


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


@pytest.mark.parametrize(("section", "component"), _EVENT_LABEL_MAPS)
def test_available_events_have_frontend_labels(section: str, component: str) -> None:
    """Every subscribable event must render as a human-readable label, never its
    raw snake_case name.

    The UI degrades to the raw key when the en-US entry is missing, so an event
    added to ``AVAILABLE_EVENTS`` without a label still works - it just reads
    like an identifier in the delivery-log filter and the endpoint editor's
    picker. That is the ``trigger_streak_alert`` gap FAR-1410 closed.
    """
    labels = _event_label_map(section, component)
    missing = [event for event in AVAILABLE_EVENTS if event not in labels]
    assert not missing, (
        f"{section}.{component} is missing en-US labels for {missing}; "
        f"add them next to the existing event labels in {_LOCALE_PATH}"
    )
    blank = [event for event in AVAILABLE_EVENTS if not labels.get(event, "").strip()]
    assert not blank, f"{section}.{component} has blank labels for {blank}"
