"""Org cost-controls auto-pause (FAR-1183) — "Auto-stop on budget exceeded".

The ``circuit_breaker_enabled`` boolean persisted in the org's
``settings_json`` ``cost_controls`` block is surfaced in AdminCostControlsView
as "Auto-stop on budget exceeded". When it is ON and the org crosses its
DAILY spend limit (``organisations.daily_spend_limit``) or its spend ceiling
(``spend_ceiling_cents``), the existing org-wide trigger pause
(``PUT /api/v1/admin/orgs/{org_id}/triggers/pause`` semantics) engages
automatically so no NEW trigger-initiated runs start. In-flight runs are never
aborted, and the auto-pause never un-pauses itself — resuming stays a manual
admin action (the same audited admin toggle as a manual pause).

Called from the run-finalize path's terminal ledger block (next to the
pipeline circuit-breaker check), on the run that crosses the limit. Every
step is BEST-EFFORT: an audit/notification/pause failure must never fail the
terminal write (the ledger itself remains the enforcement authority).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.audit_logger import append_audit_event
from modulo.core.cost_settings import COST_CONTROLS_KEY
from modulo.db.models.organisation import Organisation

_log = logging.getLogger(__name__)

#: Audit event type (dot-namespaced, the AuditEvent convention).
ORG_TRIGGERS_AUTO_PAUSED_EVENT = "org.triggers_auto_paused"

AUTO_PAUSE_REASON_DAILY_LIMIT = "daily_spend_limit"
AUTO_PAUSE_REASON_SPEND_CEILING = "spend_ceiling"

_REASON_LABELS: dict[str, str] = {
    AUTO_PAUSE_REASON_DAILY_LIMIT: "the daily spend limit was exceeded",
    AUTO_PAUSE_REASON_SPEND_CEILING: "the org spend ceiling was exceeded",
}

__all__ = [
    "AUTO_PAUSE_REASON_DAILY_LIMIT",
    "AUTO_PAUSE_REASON_SPEND_CEILING",
    "ORG_TRIGGERS_AUTO_PAUSED_EVENT",
    "circuit_breaker_enabled_for_org",
    "maybe_auto_pause_org_triggers",
]


def circuit_breaker_enabled_for_org(org: object) -> bool:
    """TRUE iff the org's cost_controls ``circuit_breaker_enabled`` toggle is persisted ON.

    ``settings_json`` is a JSON column that may hold arbitrary shapes; anything
    that is not an explicit ``True`` is the toggle being off.
    """
    settings = getattr(org, "settings_json", None)
    if not isinstance(settings, dict):
        return False
    controls = settings.get(COST_CONTROLS_KEY)
    if not isinstance(controls, dict):
        return False
    return controls.get("circuit_breaker_enabled") is True


def _cents_str(cents: int | None) -> str:
    return f"{(cents or 0) / 100:.2f}"


async def maybe_auto_pause_org_triggers(
    session: AsyncSession,
    *,
    org: Organisation,
    reason: str,
    spend_cents: int,
    limit_cents: int | None,
    run_id: uuid.UUID | None = None,
) -> bool:
    """Engage the org-wide trigger pause when the cost-controls toggle is ON.

    Idempotent: an org whose triggers are already paused is left as-is (the
    auto-pause never un-pauses — resuming is a manual admin action). With the
    toggle off this is a strict no-op (limits keep their existing alert-and-
    reject behaviour).

    Fail-open envelope: any failure logs and returns ``False`` without failing
    the caller's transaction (the run's terminal write is never at risk).

    Returns ``True`` iff THIS call flipped the pause ON (the pause + audit +
    notification were written; the caller's transaction commits them).
    """
    try:
        if not circuit_breaker_enabled_for_org(org):
            return False
        if bool(getattr(org, "triggers_paused", False)):
            return False

        org.triggers_paused = True
        org.triggers_paused_at = datetime.now(UTC)
        await session.flush()

        await append_audit_event(
            session,
            org_id=org.id,
            event_type=ORG_TRIGGERS_AUTO_PAUSED_EVENT,
            resource_type="organisation",
            resource_id=org.id,
            payload_json={
                "paused": True,
                "paused_by": "cost_controls_auto_stop",
                "reason": reason,
                "spend_cents": int(spend_cents),
                "limit_cents": limit_cents,
                "run_id": str(run_id) if run_id is not None else None,
            },
        )
        await _notify_admins(org, reason=reason, spend_cents=spend_cents, limit_cents=limit_cents, run_id=run_id)
        _log.info(
            "org_triggers_auto_paused",
            extra={"org_id": str(org.id), "reason": reason, "spend_cents": spend_cents, "limit_cents": limit_cents},
        )
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("org_triggers_auto_pause_failed", extra={"org_id": str(getattr(org, "id", None))})
        return False


async def _notify_admins(
    org: Organisation,
    *,
    reason: str,
    spend_cents: int,
    limit_cents: int | None,
    run_id: uuid.UUID | None = None,
) -> None:
    """Dispatch the ``org_triggers_auto_paused`` admin notification.

    Mirrors the pipeline circuit-breaker's ``_dispatch_circuit_breaker_tripped``
    (``cost_controller.__init__``): the Notifier is lazily built from the
    shared engine + settings, and a failure is logged + swallowed — the pause
    (flag + audit) is the enforcement, the notification is best-effort. The
    event type is registered in the notification event mapper, so the in-app
    Notification record for org admins is created alongside webhook dispatch.
    """
    from modulo.core.notifier import EVENT_ORG_TRIGGERS_AUTO_PAUSED, Notifier
    from modulo.db.session import get_shared_engine
    from modulo.settings import get_settings

    payload: dict[str, Any] = {
        "reason": reason,
        "reason_label": _REASON_LABELS.get(reason, reason),
        "spend_usd": _cents_str(spend_cents),
        "limit_usd": _cents_str(limit_cents),
    }
    try:
        notifier = Notifier(get_shared_engine(), get_settings().fernet_key)
        await notifier.dispatch_event(org.id, EVENT_ORG_TRIGGERS_AUTO_PAUSED, payload, run_id=run_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "org_triggers_auto_pause_notification_failed",
            extra={"org_id": str(org.id), "event_type": EVENT_ORG_TRIGGERS_AUTO_PAUSED},
        )
