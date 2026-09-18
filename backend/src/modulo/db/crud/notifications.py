"""CRUD for notifications and dismissals.

All functions enforce org scoping via organisation_id filter.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, Select, delete, func, select
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from modulo.db.models.notification import Dismissal, Notification, NotificationPreference

LEVEL_RANK: dict[str, int] = {
    "debug": 0,
    "info": 1,
    "warning": 2,
    "error": 3,
}

DASHBOARD_LIMIT_MAX = 50
NOTIFICATIONS_LIMIT_MAX = 200
_DEFAULT_EXPIRY_DAYS = 90


def _visible_to_user_clause(user_id: uuid.UUID) -> ColumnElement[bool]:
    return (
        (Notification.scope == "org")
        | (Notification.scope == "admin")
        | ((Notification.scope == "user") & (Notification.target_user_id == user_id))
    )


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

    dismissal_alias = aliased(Dismissal)
    dismissed_subq = (
        select(dismissal_alias.notification_id).where(dismissal_alias.dismissed_by_user_id == user_id).subquery()
    )

    q = select(Notification).where(
        Notification.organisation_id == org_id,
        Notification.level.in_(allowed_levels),
        Notification.id.notin_(select(dismissed_subq.c.notification_id)),
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

    if status_filter == "active":
        dismissed_subq = (
            select(Dismissal.notification_id)
            .where(
                Dismissal.dismissed_by_user_id == user_id,
            )
            .subquery()
        )
        q = q.where(Notification.id.notin_(select(dismissed_subq.c.notification_id)))
    elif status_filter == "dismissed_self":
        q = q.where(
            Notification.id.in_(
                select(Dismissal.notification_id).where(
                    Dismissal.dismissed_by_user_id == user_id,
                    Dismissal.dismiss_scope == "self",
                )
            )
        )
    elif status_filter == "dismissed_scope":
        q = q.where(
            Notification.id.in_(
                select(Dismissal.notification_id).where(
                    Dismissal.dismissed_by_user_id == user_id,
                    Dismissal.dismiss_scope == "scope",
                )
            )
        )

    q = q.order_by(Notification.created_at.desc()).offset(offset).limit(limit)
    try:
        result = await session.execute(q)
        return list(result.scalars().all())
    except ProgrammingError:
        return []


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

    if status_filter == "active":
        dismissed_subq = select(Dismissal.notification_id).where(Dismissal.dismissed_by_user_id == user_id).subquery()
        q = q.where(Notification.id.notin_(select(dismissed_subq.c.notification_id)))
    elif status_filter == "dismissed_self":
        q = q.where(
            Notification.id.in_(
                select(Dismissal.notification_id).where(
                    Dismissal.dismissed_by_user_id == user_id,
                    Dismissal.dismiss_scope == "self",
                )
            )
        )
    elif status_filter == "dismissed_scope":
        q = q.where(
            Notification.id.in_(
                select(Dismissal.notification_id).where(
                    Dismissal.dismissed_by_user_id == user_id,
                    Dismissal.dismiss_scope == "scope",
                )
            )
        )

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

    dismissed_subq = select(Dismissal.notification_id).where(Dismissal.dismissed_by_user_id == user_id).subquery()

    q = select(func.count(Notification.id)).where(
        Notification.organisation_id == org_id,
        Notification.level.in_(allowed_levels),
        Notification.id.notin_(select(dismissed_subq.c.notification_id)),
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
