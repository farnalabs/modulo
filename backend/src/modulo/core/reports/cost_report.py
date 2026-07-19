"""Scheduled cost-report generation, formatting, and email delivery."""

from __future__ import annotations

import asyncio
import csv
import html
import io
import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.cost_controller import get_cost_report
from modulo.core.email_service import EmailSendingError, send_email
from modulo.settings import get_settings

_PERIOD_TO_COST_WINDOW = {
    "daily": "day",
    "weekly": "week",
    "monthly": "month",
}


async def generate_cost_report(
    session: AsyncSession,
    org_id: uuid.UUID,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Generate a cost report using the canonical cost-controller query."""
    period = config.get("period")
    group_by = config.get("group_by")
    report_format = config.get("format")
    if not isinstance(period, str) or period not in _PERIOD_TO_COST_WINDOW:
        raise ValueError(f"Unsupported scheduled cost report period: {period}")
    if group_by not in {"team", "org"}:
        raise ValueError(f"Unsupported scheduled cost report grouping: {group_by}")
    if report_format not in {"csv", "json"}:
        raise ValueError(f"Unsupported scheduled cost report format: {report_format}")

    items = await get_cost_report(
        session,
        org_id=org_id,
        group_by=group_by,
        period=_PERIOD_TO_COST_WINDOW[period],
    )
    return {
        "period": period,
        "group_by": group_by,
        "format": report_format,
        "items": items,
    }


def format_cost_report(report: dict[str, Any]) -> dict[str, str]:
    """Format report data into the subject and bodies expected by email delivery."""
    report_format = report.get("format")
    if report_format == "csv":
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["entity_id", "entity_name", "total_spend_usd", "total_runs"])
        for item in report.get("items", []):
            writer.writerow(
                [
                    item.get("entity_id", ""),
                    item.get("entity_name", ""),
                    item.get("total_spend_usd", 0),
                    item.get("total_runs", 0),
                ]
            )
        body = output.getvalue()
    elif report_format == "json":
        body = json.dumps(report.get("items", []), indent=2, sort_keys=True)
    else:
        raise ValueError(f"Unsupported scheduled cost report format: {report_format}")

    period = str(report.get("period", ""))
    group_by = str(report.get("group_by", ""))
    subject = f"Modulo {period} cost report by {group_by}"
    return {
        "subject": subject,
        "body_text": body,
        "body_html": (f"<html><body><h2>{html.escape(subject)}</h2><pre>{html.escape(body)}</pre></body></html>"),
    }


async def deliver_cost_report(
    payload: dict[str, str],
    recipient_config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Deliver a formatted cost report to its configured email recipients."""
    if recipient_config.get("type") != "email":
        raise ValueError("Scheduled cost reports require an email recipient configuration")
    raw_emails = recipient_config.get("emails")
    if not isinstance(raw_emails, list):
        raise ValueError("Scheduled cost report recipients must be a list")
    emails = [email for email in raw_emails if isinstance(email, str) and email]
    if len(emails) != len(raw_emails) or not emails:
        raise ValueError("Scheduled cost report recipients must be non-empty email strings")

    delivered = await asyncio.to_thread(
        send_email,
        get_settings(),
        emails,
        payload["subject"],
        payload["body_html"],
        payload["body_text"],
    )
    if not delivered:
        raise EmailSendingError("SMTP is not configured for scheduled cost reports")
    return [{"type": "email", "status": "delivered", "recipient_count": len(emails)}]
