"""CRUD for notifications and dismissals.

All functions enforce org scoping via organisation_id filter.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, Select, delete, func, or_, select
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.notification import Dismissal, Notification, NotificationPreference
from modulo.db.models.run import TERMINAL_STATUSES, Run

LEVEL_RANK: dict[str, int] = {
    "debug": 0,
    "info": 1,
    "warning": 2,
    "error": 3,
}

DASHBOARD_LIMIT_MAX = 50
NOTIFICATIONS_LIMIT_MAX = 200
_DEFAULT_EXPIRY_DAYS = 90

# FAR-1234 — run-linked notifications are enriched AT READ TIME with the run's
# CURRENT state. A notification row is written when the event fires and is
# never re-stamped, so a ``hitl.awaiting`` raised against a run that was later
# cancelled still reads as a live request. Every read path therefore resolves
# the linked run fresh and the API reports what actually happened.
#
# The link is the run deep-link written by the event mapper
# (``/runs/{run_id}``) — there is deliberately no run FK on ``notifications``.
_RUN_LINK_RE = re.compile(
    r"^/runs/(?P<run_id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:[/?#].*)?$"
)


@dataclass(frozen=True)
class LinkedRunState:
    """Point-in-time state of a notification's linked run (FAR-1234)."""

    run_id: uuid.UUID
    status: str
    cancel_reason: str | None = None

    @property
    def terminal(self) -> bool:
        """Whether the run has reached a terminal state (``TERMINAL_STATUSES``)."""
        return self.status in TERMINAL_STATUSES


def linked_run_id(action_url: str | None) -> uuid.UUID | None:
    """Extract the run UUID from a run deep-link ``action_url``.

    Returns ``None`` for a missing/absent URL, a non-run deep link
    (``/evals``, ``/feedback/inbox``, …) or a template that never resolved
    (``/runs/[unknown]``).
    """
    if not action_url:
        return None
    match = _RUN_LINK_RE.match(action_url)
    if match is None:
        return None
    try:
        return uuid.UUID(match.group("run_id"))
    except ValueError:  # pragma: no cover - regex already constrains the shape
        return None


async def get_linked_run_states(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    notifications: Sequence[Notification],
) -> dict[uuid.UUID, LinkedRunState]:
    """Resolve the CURRENT run state for each run-linked notification.

    Returns a map keyed by ``Notification.id`` containing ONLY the
    notifications whose linked run row could be read (org-scoped). A
    notification with no run link, an unresolvable template, a deleted run or
    an invisible run is simply absent — callers degrade to "no run metadata"
    rather than failing the read.

    One batched query regardless of page size; the ``runs`` table carries the
    org-isolation RLS policy so this read never crosses a tenant.
    """
    run_link_by_notification: dict[uuid.UUID, uuid.UUID] = {}
    for notification in notifications:
        run_id = linked_run_id(notification.action_url)
        if run_id is not None:
            run_link_by_notification[notification.id] = run_id
    if not run_link_by_notification:
        return {}

    result = await session.execute(
        select(Run.id, Run.status, Run.cancel_reason).where(
            Run.organisation_id == org_id,
            Run.id.in_(set(run_link_by_notification.values())),
        )
    )
    states: dict[uuid.UUID, LinkedRunState] = {}
    for run_id_raw, status_raw, cancel_reason_raw in result.all():
        states[uuid.UUID(str(run_id_raw))] = LinkedRunState(
            run_id=uuid.UUID(str(run_id_raw)),
            status=str(status_raw),
            cancel_reason=None if cancel_reason_raw is None else str(cancel_reason_raw),
        )
    return {
        notification_id: states[run_id]
        for notification_id, run_id in run_link_by_notification.items()
        if run_id in states
    }


def _visible_to_user_clause(user_id: uuid.UUID) -> ColumnElement[bool]:
    return (
        (Notification.scope == "org")
        | (Notification.scope == "admin")
        | ((Notification.scope == "user") & (Notification.target_user_id == user_id))
    )


def _not_expired_clause() -> ColumnElement[bool]:
    """TTL clause: a notification past its ``expires_at`` is no longer live.

    Read-path enforcement of ``expires_at`` (the column was previously
    decorative — rows were written with a 72h/168h TTL but never filtered, so a
    week-old ``hitl.awaiting`` still counted as active).

    **Semantics (documented in docs/product-map/notifications/notifications.md):**
    an expired notification drops out of every *live* view — the dashboard
    panel, the unread badge/count, the inbox default (no ``status``) and
    ``status=active``. It is NOT deleted: an explicit historical filter
    (``dismissed_self`` / ``dismissed_scope``) and the by-id detail read still
    retrieve it, so nothing is lost — it simply stops presenting as
    active/actionable. ``expires_at`` is ``NOT NULL``
    (``db/models/notification.py``), so every row carries a real TTL and no
    NULL-means-never fallback is needed.
    """
    return Notification.expires_at > datetime.now(UTC)


def _hidden_from_user_clause(user_id: uuid.UUID) -> ColumnElement[bool]:
    """Clause excluding notifications dismissed out of ``user_id``'s active view.

    ``dismiss_scope='self'`` hides the row for the dismissing user only.
    ``dismiss_scope='scope'`` hides it for EVERY user in the org — the active
    filter previously keyed on ``dismissed_by_user_id`` alone, so a scope-level
    dismissal only ever hid the row for the user who performed it (defect 4).

    Correlated on ``organisation_id`` so a dismissal in one org can never hide
    a row in another on a backend without RLS.
    """
    hidden = select(Dismissal.notification_id).where(
        Dismissal.organisation_id == Notification.organisation_id,
        or_(Dismissal.dismissed_by_user_id == user_id, Dismissal.dismiss_scope == "scope"),
    )
    return Notification.id.notin_(hidden)


def apply_prefs_filter(query: Select[Any], account_id: uuid.UUID) -> Select[Any]:
    """Restrict a notifications query to categories the user has NOT opted out of.

    Shared by every notification read path (dashboard list, full list, count,
    unread count) so badge/list/count always agree (FAR-247 read-time model).

    The opt-out subquery is correlated on ``Notification.organisation_id`` so
    it is org-scoped regardless of backend: on Postgres the org-scope SELECT
    RLS policy also applies, on generic dev backends the correlation carries
    the outer query's already-scoped org.
    """
    opted_out_categories = (
        select(NotificationPreference.category)
        .where(
            NotificationPreference.account_id == account_id,
            NotificationPreference.organisation_id == Notification.organisation_id,
        )
        .scalar_subquery()
    )
    return query.where(Notification.category.notin_(opted_out_categories))


async def create_notification(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    scope: str,
    level: str,
    category: str,
    title: str,
    body: str,
    action_url: str | None = None,
    dismiss_strategy: str = "user_only",
    dismissible_at_scope: bool = False,
    target_user_id: uuid.UUID | None = None,
    expires_at: datetime | None = None,
) -> Notification:
    if expires_at is None:
        expires_at = datetime.now(UTC) + timedelta(days=_DEFAULT_EXPIRY_DAYS)
    notification = Notification(
        organisation_id=org_id,
        scope=scope,
        level=level,
        category=category,
        title=title,
        body=body,
        action_url=action_url,
        dismiss_strategy=dismiss_strategy,
        dismissible_at_scope=dismissible_at_scope,
        target_user_id=target_user_id,
        expires_at=expires_at,
    )
    session.add(notification)
    await session.flush()
    return notification


async def get_notification(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    notification_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
) -> Notification | None:
    """Fetch a notification by ID, optionally enforcing user visibility.

    When ``user_id`` is provided, applies the same visibility clause used by
    list/dashboard queries so that scope='user' notifications belonging to
    other users and scope='admin' notifications are hidden from non-admin
    callers. Without ``user_id``, returns any notification in the org (legacy
    behaviour for system-internal callers).
    """
    filters = [
        Notification.organisation_id == org_id,
        Notification.id == notification_id,
    ]
    if user_id is not None:
        filters.append(_visible_to_user_clause(user_id))
    result = await session.execute(select(Notification).where(*filters))
    return result.scalar_one_or_none()


async def get_dashboard_notifications(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    min_level: str = "warning",
    limit: int = 5,
) -> list[Notification]:
    if limit < 1 or limit > DASHBOARD_LIMIT_MAX:
        limit = 5
    min_rank = LEVEL_RANK.get(min_level, 1)
    allowed_levels = [lvl for lvl, rnk in LEVEL_RANK.items() if rnk >= min_rank]

    q = select(Notification).where(
        Notification.organisation_id == org_id,
        Notification.level.in_(allowed_levels),
        _hidden_from_user_clause(user_id),
        _not_expired_clause(),
        _visible_to_user_clause(user_id),
    )
    q = apply_prefs_filter(q, account_id=user_id)
    q = q.order_by(Notification.created_at.desc()).limit(limit)
    try:
        result = await session.execute(q)
        return list(result.scalars().all())
    except ProgrammingError:
        return []


async def get_notifications_for_user(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    level: str | None = None,
    scope: str | None = None,
    category: str | None = None,
    status_filter: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Notification]:
    q = select(Notification).where(
        Notification.organisation_id == org_id,
        _visible_to_user_clause(user_id),
    )
    q = apply_prefs_filter(q, account_id=user_id)

    if level is not None:
        q = q.where(Notification.level == level)
    if scope is not None:
        q = q.where(Notification.scope == scope)
    if category is not None:
        q = q.where(Notification.category == category)

    q = _apply_status_filter(q, user_id=user_id, status_filter=status_filter)

    q = q.order_by(Notification.created_at.desc()).offset(offset).limit(limit)
    try:
        result = await session.execute(q)
        return list(result.scalars().all())
    except ProgrammingError:
        return []


def _apply_status_filter(query: Select[Any], *, user_id: uuid.UUID, status_filter: str | None) -> Select[Any]:
    """Apply the ``status`` filter (with its TTL companion) to a query.

    Shared by :func:`get_notifications_for_user` and
    :func:`count_notifications_for_user` so a page's ``items`` and ``total``
    can never disagree.

    * ``None`` (inbox default) and ``"active"`` are LIVE views: a row past its
      ``expires_at`` is excluded, and ``"active"`` additionally excludes
      anything dismissed out of this user's active view (per-user ``self``
      dismissals AND org-wide ``scope`` dismissals).
    * ``dismissed_self`` / ``dismissed_scope`` are explicit historical filters:
      they still retrieve a dismissed row even if it has since expired.
    """
    if status_filter in (None, "active"):
        query = query.where(_not_expired_clause())
    if status_filter == "active":
        query = query.where(_hidden_from_user_clause(user_id))
    elif status_filter == "dismissed_self":
        query = query.where(
            Notification.id.in_(
                select(Dismissal.notification_id).where(
                    Dismissal.dismissed_by_user_id == user_id,
                    Dismissal.dismiss_scope == "self",
                )
            )
        )
    elif status_filter == "dismissed_scope":
        query = query.where(
            Notification.id.in_(
                select(Dismissal.notification_id).where(
                    Dismissal.dismissed_by_user_id == user_id,
                    Dismissal.dismiss_scope == "scope",
                )
            )
        )
    return query


async def count_notifications_for_user(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    level: str | None = None,
    scope: str | None = None,
    category: str | None = None,
    status_filter: str | None = None,
) -> int:
    q = select(func.count(Notification.id)).where(
        Notification.organisation_id == org_id,
        _visible_to_user_clause(user_id),
    )
    q = apply_prefs_filter(q, account_id=user_id)

    if level is not None:
        q = q.where(Notification.level == level)
    if scope is not None:
        q = q.where(Notification.scope == scope)
    if category is not None:
        q = q.where(Notification.category == category)

    q = _apply_status_filter(q, user_id=user_id, status_filter=status_filter)

    try:
        result = await session.execute(q)
        return int(result.scalar_one())
    except ProgrammingError:
        return 0


async def dismiss_notification(
    session: AsyncSession,
    *,
    notification_id: uuid.UUID,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    dismiss_scope: str = "self",
    is_admin: bool = False,
) -> Dismissal:
    result = await session.execute(
        select(Notification).where(
            Notification.organisation_id == org_id,
            Notification.id == notification_id,
        )
    )
    notification = result.scalar_one_or_none()
    if notification is None:
        raise ValueError("Notification not found")

    if dismiss_scope == "scope":
        if notification.dismiss_strategy == "user_only":
            raise ValueError("This notification cannot be dismissed for all users")
        if notification.dismiss_strategy == "org_admin" and not is_admin:
            raise ValueError("Only admins can dismiss this notification for the org")

    existing = await session.execute(
        select(Dismissal).where(
            Dismissal.notification_id == notification_id,
            Dismissal.dismissed_by_user_id == user_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise ValueError("Notification already dismissed by this user")

    dismissal = Dismissal(
        organisation_id=org_id,
        notification_id=notification_id,
        dismissed_by_user_id=user_id,
        dismiss_scope=dismiss_scope,
    )
    session.add(dismissal)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ValueError("Notification already dismissed by this user (concurrent)") from exc
    return dismissal


async def review_later(
    session: AsyncSession,
    *,
    notification_id: uuid.UUID,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
) -> Dismissal:
    return await dismiss_notification(
        session=session,
        notification_id=notification_id,
        user_id=user_id,
        org_id=org_id,
        dismiss_scope="self",
        is_admin=False,
    )


async def get_unread_count(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    min_level: str = "warning",
) -> int:
    min_rank = LEVEL_RANK.get(min_level, 1)
    allowed_levels = [lvl for lvl, rnk in LEVEL_RANK.items() if rnk >= min_rank]

    q = select(func.count(Notification.id)).where(
        Notification.organisation_id == org_id,
        Notification.level.in_(allowed_levels),
        _hidden_from_user_clause(user_id),
        _not_expired_clause(),
        _visible_to_user_clause(user_id),
    )
    q = apply_prefs_filter(q, account_id=user_id)
    try:
        result = await session.execute(q)
        return int(result.scalar_one())
    except ProgrammingError:
        return 0


async def get_opted_out_categories(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
) -> set[str]:
    """Return the categories the user has opted out of in this org."""
    result = await session.execute(
        select(NotificationPreference.category).where(
            NotificationPreference.organisation_id == org_id,
            NotificationPreference.account_id == account_id,
        )
    )
    return set(result.scalars().all())


async def set_notification_preferences(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    opt_outs: dict[str, bool],
) -> None:
    """Apply a partial opt-out mapping for a user.

    For each key, ``True`` opts out (row ensured), ``False`` opts back in
    (row removed). Keys absent from the mapping are left untouched, so a
    client may PUT a single toggle or the full map returned by GET.
    """
    if not opt_outs:
        return
    existing = await get_opted_out_categories(session, org_id=org_id, account_id=account_id)
    to_remove = existing & {category for category, opted_out in opt_outs.items() if not opted_out}
    to_add = {category for category, opted_out in opt_outs.items() if opted_out} - existing

    if to_remove:
        await session.execute(
            delete(NotificationPreference).where(
                NotificationPreference.organisation_id == org_id,
                NotificationPreference.account_id == account_id,
                NotificationPreference.category.in_(sorted(to_remove)),
            )
        )
    if to_add:
        session.add_all(
            NotificationPreference(organisation_id=org_id, account_id=account_id, category=category)
            for category in sorted(to_add)
        )
    await session.flush()
