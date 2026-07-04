"""Alert dispatch — routes triggered alerts to in_app, email, or webhook."""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.error_group import ErrorGroup

if TYPE_CHECKING:
    from modulo.core.error_tracking.alerting import TriggeredAlert

_log = logging.getLogger(__name__)

SLACK_WEBHOOK_RE = re.compile(r"hooks\.slack\.com|slack\.com/api/")

LEVEL_EMOJI = {
    "critical": "\U0001f534",
    "error": "\U0001f7e1",
    "warning": "\u26aa",
}


async def dispatch_alert(
    org_id: uuid.UUID,
    alert: TriggeredAlert,
    session: AsyncSession,
    error_group: ErrorGroup | None = None,
) -> None:
    """Dispatch a single alert to its configured action type.

    This function is intentionally top-level (not a method on a class) so
    callers can swap it in tests.  It swallows all exceptions so that a
    single failing dispatch doesn't crash the alert evaluation loop.
    """
    sample_message = error_group.sample_event.message if error_group and error_group.sample_event else ""
    admin_url = f"/admin/errors/{alert.error_group_id}"

    if alert.action_type == "in_app":
        await _dispatch_in_app(org_id, alert, sample_message, admin_url, session)
    elif alert.action_type == "email":
        await _dispatch_email(org_id, alert, sample_message, admin_url)
    elif alert.action_type == "webhook":
        await _dispatch_webhook(alert, sample_message, admin_url)
    else:
        _log.warning(
            "alert.unknown_action_type",
            extra={"action_type": alert.action_type, "rule_id": str(alert.rule_id)},
        )


async def _dispatch_in_app(
    org_id: uuid.UUID,
    alert: TriggeredAlert,
    sample_message: str,
    _admin_url: str,
    session: AsyncSession,
) -> None:
    """Create an in-app notification via the notification_delivery_log table."""
    from modulo.db.models.notification_delivery import NotificationDeliveryLog

    entry = NotificationDeliveryLog(
        organisation_id=org_id,
        event_type="error_alert",
        status="in_app",
        attempt_count=1,
        last_error=_build_summary(alert, sample_message),
    )
    session.add(entry)

    _log.info(
        "alert.in_app",
        extra={"rule": alert.rule_name, "group_id": str(alert.error_group_id)},
    )


async def _dispatch_email(
    _org_id: uuid.UUID,
    alert: TriggeredAlert,
    sample_message: str,
    admin_url: str,
) -> None:
    """Placeholder — sends email to org admins.

    In production this would use the org's email provider (SendGrid, SMTP, etc.).
    For now we log the intent.
    """
    _log.info(
        "alert.email",
        extra={
            "rule": alert.rule_name,
            "group_id": str(alert.error_group_id),
            "summary": _build_summary(alert, sample_message),
            "admin_url": admin_url,
        },
    )


async def _dispatch_webhook(
    alert: TriggeredAlert,
    sample_message: str,
    admin_url: str,
) -> None:
    """POST an error alert payload to the configured webhook URL.

    Detects Slack webhook URLs and adds an emoji level prefix.
    """
    webhook_url = alert.webhook_url
    if not webhook_url:
        _log.warning("alert.webhook_no_url", extra={"rule_id": str(alert.rule_id)})
        return

    is_slack = bool(SLACK_WEBHOOK_RE.search(webhook_url))
    emoji = LEVEL_EMOJI.get(alert.level, "")

    payload: dict[str, Any] = {
        "event": "error_alert",
        "rule": alert.rule_name,
        "group_id": str(alert.error_group_id),
        "fingerprint": alert.fingerprint,
        "message": sample_message,
        "level": alert.level,
        "count": alert.count,
        "environment": alert.environment or "",
        "url": admin_url,
    }

    if is_slack:
        payload = _format_slack_payload(payload, emoji)

    body = json.dumps(payload, separators=(",", ":")).encode()

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(
                webhook_url,
                content=body,
                headers={"Content-Type": "application/json", "User-Agent": "Modulo-Error-Alert/1.0"},
            )
            if not resp.is_success:
                _log.warning(
                    "alert.webhook_http_error",
                    extra={"status": resp.status_code, "rule_id": str(alert.rule_id)},
                )
        except httpx.RequestError as exc:
            _log.warning(
                "alert.webhook_request_failed",
                extra={"rule_id": str(alert.rule_id), "error": str(exc)},
            )


def _format_slack_payload(payload: dict[str, Any], emoji: str) -> dict[str, Any]:
    return {
        "text": f"{emoji} *Error Alert: {payload['rule']}*\n"
        f"• Group: `{payload['group_id']}`\n"
        f"• Level: {payload['level']}\n"
        f"• Count: {payload['count']}\n"
        f"• Message: {payload['message'][:500]}\n"
        f"• Environment: {payload['environment']}\n"
        f"• <{payload['url']}|View in Modulo>",
    }


def _build_summary(alert: TriggeredAlert, sample_message: str) -> str:
    return f"[{alert.level}] {alert.rule_name}: {sample_message[:200]} (count={alert.count})"

