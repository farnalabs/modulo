"""Notification SSE event payload helpers (FAR-250).

The SSE payload for a notification carries ONLY ``{notification_id,
category, created_at}`` on top of the generic event envelope — never title,
body, or any other content. Content is fetched through the RLS-filtered,
preference-filtered REST API at read time (read-time suppression model).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

RESOURCE_TYPE_NOTIFICATION = "notification"

# Session marker set by the notifier before it creates a Notification row.
# When present, the after_insert listener SUPPRESSES its own Redis leg
# (post-commit) because the notifier's post-commit publish owns the single
# Redis message for that create. Notification creates on sessions without
# the marker (e.g. the bundled-runner health probe) keep the listener's
# Redis leg so they are not silently lost cross-worker.
NOTIFIER_SESSION_KEY = "_modulo_notifier_sse_broadcast"


def notification_event_id(notification_id: str) -> str:
    """Deterministic event id for a notification-created event.

    Stable across producers (notifier publish, listener local delivery,
    relay reconnect backfill) so the relay's ``event_id`` dedupe can drop
    duplicates after a subscription blip.
    """
    return f"{RESOURCE_TYPE_NOTIFICATION}:{notification_id}:created"


def _isoformat(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def build_notification_event(
    *,
    org_id: str,
    notification_id: str | uuid.UUID,
    category: str,
    created_at: Any,
    version: int,
) -> dict[str, Any]:
    """Build the full SSE event dict for a created notification.

    Envelope (``type``/``id``/``action``/``version``/``org_id``) plus the
    three allowed notification fields and a deterministic ``event_id``.
    Contains no content fields.
    """
    nid = str(notification_id)
    return {
        "type": RESOURCE_TYPE_NOTIFICATION,
        "id": nid,
        "action": "created",
        "version": version,
        "org_id": org_id,
        "notification_id": nid,
        "category": category,
        "created_at": _isoformat(created_at),
        "event_id": notification_event_id(nid),
    }


def notification_event_fields(target: Any, notification_id: str) -> dict[str, Any]:
    """Capture the allowed notification fields from a mapped instance.

    Used by the ``after_insert`` listener at flush time (the committed row
    carries the server-generated ``created_at`` via INSERT..RETURNING).
    Content fields (title/body/action_url) are deliberately never read.
    """
    created_at = getattr(target, "created_at", None)
    if created_at is None:
        # Server default not yet fetched back (eager_defaults off): fall back
        # to "now" only for the live event; the relay backfill re-reads the
        # committed value from the DB.
        created_at = datetime.now(UTC)
    return {
        "notification_id": notification_id,
        "category": str(getattr(target, "category", "") or ""),
        "created_at": _isoformat(created_at),
        "event_id": notification_event_id(notification_id),
    }
