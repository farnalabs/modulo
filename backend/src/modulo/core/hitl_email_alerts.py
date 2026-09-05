"""User-configurable HITL email alerts (FAR-602, backend slice).

When a HITL gate fires, org members holding the ``hitl.claim`` permission
whose per-user preference resolves TRUE for (org, pipeline) receive an email.
Preferences live in the EXISTING ``Account.preferences`` JSONB column under
the ``hitl_email`` key — no schema change:

    {"hitl_email": {"default": false, "pipeline_overrides": {"<pipeline_uuid>": true}}}

- Absent key (or a malformed value) resolves FALSE: emails are OFF by default.
- ``pipeline_overrides[pipeline_id]`` wins over ``default`` when present.
- Recipients = ACTIVE org members whose org role is at or above the
  ``hitl.claim`` minimum (runner) and whose resolved preference is TRUE.

Dispatch is scheduled fire-and-forget from ``HITLManager.create_gate``
(interrupt time). The background task opens its OWN session (the caller's
session belongs to the interrupt transaction and is closed by the time the
task runs) with the org's RLS context set, resolves the recipients in a SHORT
transaction, and only then sends — the SMTP loop (smtplib with retries, one
connection per recipient) must never pin a pooled connection from the shared
engine, which the API and SAQ worker share. Every failure is logged as
``hitl_email.dispatch_failed`` (warning) and never raised into the caller —
a broken email path must not affect gate creation or the run interrupt.
``send_email`` is synchronous, so it runs via ``asyncio.to_thread`` — the
same offload pattern as the worker-liveness watchdog's ``_send_email_alert``.

Accepted trade-off: the dispatch is scheduled at gate-fire time, BEFORE the
caller's interrupt transaction commits — if that transaction subsequently
rolls back, recipients get an email for a gate that never persisted. The
alternative (an after-commit outbox) is follow-up work; gates essentially
never roll back (the insert is the first write of the interrupt transaction).

``normalize_hitl_email_prefs`` is the single parser for the stored block and
is shared by the recipient resolver AND the preference API, so the API view
and the resolver agree mechanically.
"""

from __future__ import annotations

import asyncio
import html
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modulo.auth.permissions import resolve_required
from modulo.auth.team_rbac import ORG_ROLE_HIERARCHY
from modulo.core.email_service import send_email
from modulo.db.models.account import Account
from modulo.db.models.org_membership import OrgMembership
from modulo.db.rls import set_rls_org
from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)

# Key under ``Account.preferences`` holding this feature's settings.
PREFERENCE_KEY = "hitl_email"

_SUBJECT_TEMPLATE = "HITL gate awaiting review - {gate_label}"

# The claim permission whose holders are eligible recipients, and the org
# roles that satisfy it (role level >= the permission's minimum role, per the
# same registry/hierarchy the REST ``require_permission`` gate consults).
_CLAIM_PERMISSION = "hitl.claim"
_CLAIM_ROLES: frozenset[str] = frozenset(
    role
    for role, level in ORG_ROLE_HIERARCHY.items()
    if level >= ORG_ROLE_HIERARCHY[resolve_required(_CLAIM_PERMISSION)]
)

# Strong references to the in-flight dispatch tasks (asyncio tasks without a
# retained reference can be garbage-collected mid-flight). Entries are removed
# by the done callback as each task completes.
_PENDING_DISPATCH_TASKS: set[asyncio.Task[None]] = set()


def normalize_hitl_email_prefs(preferences: Any) -> tuple[bool, dict[str, bool]]:
    """Parse the stored ``hitl_email`` preference block.

    Returns ``(default, pipeline_overrides)`` with strict booleans; anything
    absent or malformed resolves to the all-off default. The SINGLE parser
    for the stored shape — shared by :func:`resolve_hitl_email_pref` (dispatch
    resolution) and the preference API response so both agree mechanically.
    """
    if not isinstance(preferences, dict):
        return False, {}
    hitl_prefs = preferences.get(PREFERENCE_KEY)
    if not isinstance(hitl_prefs, dict):
        return False, {}
    raw_overrides = hitl_prefs.get("pipeline_overrides")
    overrides = (
        {str(key): value for key, value in raw_overrides.items() if isinstance(value, bool)}
        if isinstance(raw_overrides, dict)
        else {}
    )
    return hitl_prefs.get("default") is True, overrides


def resolve_hitl_email_pref(preferences: Any, pipeline_id: uuid.UUID) -> bool:
    """Resolve one account's HITL email preference for *pipeline_id*.

    Pure and unit-testable. ``pipeline_overrides[pipeline_id]`` wins over the
    user-level default when the key exists; anything absent or malformed
    resolves to False (emails are OFF by default).
    """
    default, overrides = normalize_hitl_email_prefs(preferences)
    override = overrides.get(str(pipeline_id))
    resolved = override if override is not None else default
    return resolved is True


async def resolve_hitl_email_recipients(
    session: AsyncSession,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
) -> list[str]:
    """Email addresses of the org users who should receive gate alerts.

    Org members holding the ``hitl.claim`` permission (org role at or above
    the permission's minimum, active membership) whose resolved preference is
    TRUE for this pipeline. The explicit ``organisation_id`` filter mirrors
    the RLS context the caller must set (defense-in-depth + non-Postgres
    parity with the ``do_orm_execute`` tenant-filter listener).
    """
    stmt = (
        select(Account.email, Account.preferences)
        .join(OrgMembership, OrgMembership.account_id == Account.id)
        .where(
            OrgMembership.organisation_id == org_id,
            OrgMembership.role.in_(_CLAIM_ROLES),
            OrgMembership.deactivated_at.is_(None),
            Account.active.is_(True),
        )
    )
    rows = (await session.execute(stmt)).all()
    recipients = [email for email, preferences in rows if resolve_hitl_email_pref(preferences, pipeline_id)]
    # (account_id, organisation_id) is unique, so duplicates cannot occur
    # today; the order-preserving dedupe is cheap insurance against drift.
    return list(dict.fromkeys(recipients))


def _run_link(settings: Settings, run_id: uuid.UUID) -> str:
    """Deep link to the run awaiting review (settings base URL + /runs/<id>)."""
    return f"{settings.modulo_public_url.rstrip('/')}/runs/{run_id}"


def _build_email(gate_label: str, run_url: str) -> tuple[str, str, str]:
    """Build the plain-text + HTML (subject, body_html, body_text) email."""
    subject = _SUBJECT_TEMPLATE.format(gate_label=gate_label)
    body_text = f"A HITL gate is awaiting review.\n\nGate: {gate_label}\nRun: {run_url}"
    body_html = (
        "<html><body>"
        "<p>A HITL gate is awaiting review.</p>"
        f"<p>Gate: {html.escape(gate_label)}<br>"
        f'Run: <a href="{html.escape(run_url, quote=True)}">{html.escape(run_url)}</a></p>'
        "</body></html>"
    )
    return subject, body_html, body_text


async def send_hitl_email_alerts(
    recipients: list[str],
    run_id: uuid.UUID,
    gate_label: str,
) -> None:
    """Send the gate-awaiting email to every recipient. Never raises.

    Needs NO database session: call it AFTER the resolution transaction has
    closed so the SMTP loop — synchronous smtplib with retries, one
    connection per recipient — never pins a pooled connection from the
    shared engine while it runs. One recipient's failure never blocks the
    others. Any failure is logged as ``hitl_email.dispatch_failed`` (warning)
    and swallowed: a broken email path must never raise into the caller.
    """
    if not recipients:
        return
    extra = {"run_id": str(run_id), "gate_label": gate_label, "recipient_count": len(recipients)}
    try:
        settings = get_settings()
        subject, body_html, body_text = _build_email(gate_label, _run_link(settings, run_id))
        for recipient in recipients:
            try:
                await asyncio.to_thread(send_email, settings, [recipient], subject, body_html, body_text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.warning("hitl_email.dispatch_failed: %s", exc, extra=extra)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning("hitl_email.dispatch_failed: %s", exc, extra=extra)


async def dispatch_hitl_email_alerts(
    session: AsyncSession,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    run_id: uuid.UUID,
    gate_label: str,
) -> None:
    """Resolve the recipients via *session* and send. Never raises.

    NO-THROW contract: any failure (resolution, settings, a single send) is
    logged as ``hitl_email.dispatch_failed`` (warning) and swallowed so the
    caller — the gate-fire path — is never affected. NOTE: this convenience
    compose keeps the caller's session open across the SMTP sends; the
    production background path (:func:`_run_hitl_email_dispatch`) splits the
    two phases (resolve in a short transaction, then send off-DB) so the
    SMTP loop never pins a pooled connection. Use the same split when the
    caller's session is transactional.
    """
    extra = {"org_id": str(org_id), "pipeline_id": str(pipeline_id), "run_id": str(run_id), "gate_label": gate_label}
    try:
        recipients = await resolve_hitl_email_recipients(session, org_id, pipeline_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning("hitl_email.dispatch_failed: %s", exc, extra=extra)
        return
    await send_hitl_email_alerts(recipients, run_id, gate_label)


def _dispatch_session_factory() -> async_sessionmaker[AsyncSession]:
    """Session factory for the background dispatch task.

    Built on the process-global shared engine (the same engine the API,
    dispatch and SAQ worker share). Lazy import keeps ``db.session``'s module
    state out of this module's import graph.
    """
    from modulo.db.session import get_shared_engine

    return async_sessionmaker(get_shared_engine(), expire_on_commit=False, autobegin=False)


async def _run_hitl_email_dispatch(
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    run_id: uuid.UUID,
    gate_label: str,
) -> None:
    """Background task body: resolve in a short RLS transaction, then send.

    The gate-fire path's session belongs to the interrupt transaction and is
    closed by the time this task runs, so the dispatch resolves recipients in
    its own short transaction with the org's RLS context set — and the SMTP
    sends happen AFTER that transaction closes, so a slow SMTP host can never
    pin a connection from the shared pool. Never raises.
    """
    try:
        factory = _dispatch_session_factory()
        async with factory() as session, session.begin():
            await set_rls_org(session, org_id)
            recipients = await resolve_hitl_email_recipients(session, org_id, pipeline_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning(
            "hitl_email.dispatch_failed: %s",
            exc,
            extra={
                "org_id": str(org_id),
                "pipeline_id": str(pipeline_id),
                "run_id": str(run_id),
                "gate_label": gate_label,
            },
        )
        return
    await send_hitl_email_alerts(recipients, run_id, gate_label)


def schedule_hitl_email_dispatch(
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    run_id: uuid.UUID,
    gate_label: str,
) -> None:
    """Fire-and-forget the HITL email dispatch (called from ``create_gate``).

    Never raises and never awaits: the task is scheduled on the running loop
    with a strong reference retained until completion. ``gate_label`` is the
    gate's node id (the human-readable label used in the subject line).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _log.warning(
            "hitl_email.dispatch_no_running_loop",
            extra={"org_id": str(org_id), "run_id": str(run_id)},
        )
        return
    task = loop.create_task(
        _run_hitl_email_dispatch(org_id, pipeline_id, run_id, gate_label),
        name=f"hitl-email-dispatch-{run_id}",
    )
    _PENDING_DISPATCH_TASKS.add(task)
    task.add_done_callback(_PENDING_DISPATCH_TASKS.discard)
