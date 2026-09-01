"""Alert dispatch — routes triggered alerts to in_app, email, or webhook."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.auth.secret_storage import decode_stored_secret_scoped
from modulo.core.email_service import EmailSendingError, send_email
from modulo.core.error_tracking.metrics import record_alert_delivery_failed
from modulo.db.models.account import Account
from modulo.db.models.error_group import ErrorGroup
from modulo.db.models.org_membership import OrgMembership
from modulo.settings import get_settings

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
        await _dispatch_email(org_id, alert, sample_message, admin_url, session)
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
    org_id: uuid.UUID,
    alert: TriggeredAlert,
    sample_message: str,
    admin_url: str,
    session: AsyncSession,
) -> None:
    """Send alert notification email to org admins via the configured SMTP provider."""
    from modulo.db.models.organisation import Organisation

    settings = get_settings()

    try:
        org = await session.get(Organisation, org_id)

        if org:
            email_cfg = (org.settings_json or {}).get("email", {})
            smtp_host = email_cfg.get("smtp_host", "")
        else:
            smtp_host = ""
            email_cfg = {}

        effective_smtp_host = smtp_host or settings.smtp_host
        if not effective_smtp_host:
            _log.warning("alert.email_disabled_no_smtp_host", extra={"rule": alert.rule_name, "org_id": str(org_id)})
            return

        stored_smtp_password = email_cfg.get("smtp_password", settings.smtp_password)
        try:
            stored_smtp_password = await decode_stored_secret_scoped(
                session, stored_smtp_password, settings.fernet_key, org_id=org_id
            )
        except Exception:
            _log.exception("alert.email_smtp_password_decrypt_failed")

        effective_settings = settings.model_copy(
            update={
                "smtp_host": effective_smtp_host,
                "smtp_port": email_cfg.get("smtp_port", settings.smtp_port),
                "smtp_username": email_cfg.get("smtp_username", settings.smtp_username),
                "smtp_password": stored_smtp_password,
                "email_from": email_cfg.get("email_from", settings.email_from),
            }
        )

        _log.info(
            "alert.email_pending",
            extra={
                "rule": alert.rule_name,
                "group_id": str(alert.error_group_id),
                "summary": _build_summary(alert, sample_message),
                "admin_url": admin_url,
            },
        )

        result = await session.execute(
            select(OrgMembership).where(
                OrgMembership.organisation_id == org_id,
                OrgMembership.role == "admin",
                OrgMembership.deactivated_at.is_(None),
            )
        )
        memberships = list(result.scalars().all())

        if not memberships:
            _log.warning("alert.email_no_admins", extra={"org_id": str(org_id), "rule": alert.rule_name})
            return

        account_ids = [m.account_id for m in memberships]

        account_result = await session.execute(
            select(Account).where(Account.id.in_(account_ids), Account.active.is_(True))
        )
        admin_accounts = list(account_result.scalars().all())

        if not admin_accounts:
            _log.warning("alert.email_no_active_admins", extra={"org_id": str(org_id), "rule": alert.rule_name})
            return

        to_emails = [a.email for a in admin_accounts]

        subject = f"[Modulo Alert] {alert.level}: {alert.rule_name}"

        body_html = (
            "<html><body>"
            f"<h2>Modulo Alert: {alert.rule_name}</h2>"
            f"<p><strong>Level:</strong> {alert.level}</p>"
            f"<p><strong>Rule:</strong> {alert.rule_name}</p>"
            f"<p><strong>Message:</strong> {_escape_html(sample_message[:1000])}</p>"
            f"<p><strong>Count:</strong> {alert.count}</p>"
            f"<p><strong>Fingerprint:</strong> {alert.fingerprint}</p>"
            f"<p><strong>Environment:</strong> {_escape_html(alert.environment or 'N/A')}</p>"
            f'<p><a href="{_escape_html(admin_url)}">View in Modulo</a></p>'
            "</body></html>"
        )

        body_text = (
            f"Modulo Alert: {alert.rule_name}\n"
            f"Level: {alert.level}\n"
            f"Rule: {alert.rule_name}\n"
            f"Message: {sample_message[:500]}\n"
            f"Count: {alert.count}\n"
            f"Fingerprint: {alert.fingerprint}\n"
            f"Environment: {alert.environment or 'N/A'}\n"
            f"View: {admin_url}"
        )

        try:
            success = await asyncio.to_thread(
                send_email,
                effective_settings,
                to_emails,
                subject,
                body_html,
                body_text,
            )
            if success:
                _log.info(
                    "alert.email_sent",
                    extra={"rule": alert.rule_name, "to_count": len(to_emails)},
                )
        except EmailSendingError as exc:
            record_alert_delivery_failed(str(alert.rule_id), "email")
            _log.warning(
                "alert.email_send_failed",
                extra={"rule": alert.rule_name, "error": str(exc)},
            )
    except Exception:
        _log.exception("alert.email_dispatch_error", extra={"rule": alert.rule_name})


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
        "alert_id": str(alert.alert_id),
        "rule": alert.rule_name,
        "group_id": str(alert.error_group_id),
        "fingerprint": alert.fingerprint,
        "message": sample_message,
        "level": alert.level,
        "count": alert.count,
        "environment": alert.environment or "",
        "url": admin_url,
        "signal": alert.signal or "",
        "elevation_signal": alert.elevation_signal or "",
        "attempt_n": alert.attempt_n,
        "run_group_id": str(alert.run_group_id) if alert.run_group_id else None,
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
                record_alert_delivery_failed(str(alert.rule_id), "webhook")
                _log.warning(
                    "alert.webhook_http_error",
                    extra={"status": resp.status_code, "rule_id": str(alert.rule_id)},
                )
        except httpx.RequestError as exc:
            record_alert_delivery_failed(str(alert.rule_id), "webhook")
            _log.warning(
                "alert.webhook_request_failed",
                extra={"rule_id": str(alert.rule_id), "error": str(exc)},
            )


async def dispatch_alert_resolved(
    org_id: uuid.UUID,
    *,
    group_id: uuid.UUID,
    signal: str,
    reason: str,
    session: AsyncSession,
    webhook_url: str | None = None,
) -> None:
    """Emit an ``alert_resolved`` lifecycle event for an earlier critical now moot.

    Recorded in-app (``notification_delivery_log``) and, when the matched rule
    carries a webhook, delivered to it so a moot critical never stays open
    (FAR-151 §15.5). Best-effort: failures are logged, never propagated.
    """
    from modulo.db.models.notification_delivery import NotificationDeliveryLog

    session.add(
        NotificationDeliveryLog(
            organisation_id=org_id,
            event_type="alert_resolved",
            status="in_app",
            attempt_count=1,
            last_error=f"{signal} resolved: {reason}",
        )
    )

    if webhook_url:
        payload: dict[str, Any] = {
            "event": "alert_resolved",
            "group_id": str(group_id),
            "signal": signal,
            "reason": reason,
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    webhook_url,
                    content=body,
                    headers={"Content-Type": "application/json", "User-Agent": "Modulo-Error-Alert/1.0"},
                )
                if not resp.is_success:
                    _log.warning(
                        "alert.resolved_webhook_http_error",
                        extra={"status": resp.status_code, "signal": signal},
                    )
        except httpx.RequestError as exc:
            _log.warning(
                "alert.resolved_webhook_request_failed",
                extra={"signal": signal, "error": str(exc)},
            )

    _log.info(
        "alert.resolved",
        extra={"signal": signal, "group_id": str(group_id)},
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


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _build_summary(alert: TriggeredAlert, sample_message: str) -> str:
    return f"[{alert.level}] {alert.rule_name}: {sample_message[:200]} (count={alert.count})"
