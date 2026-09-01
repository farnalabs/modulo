"""CRUD for Trigger records."""

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.trigger_engine import TriggerConfigInvalidError, TriggerNotFoundError
from modulo.db.crud.base import PageResult
from modulo.db.crud.pagination import CursorPaginator
from modulo.db.crud.team_scope import team_scope_clause
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.trigger import Trigger
from modulo.db.models.trigger_event import TriggerEvent
from modulo.util import sanitise_log_value

_log = logging.getLogger(__name__)


def apply_trigger_event_cursor(q: Select[Any], cursor: str | None) -> Select[Any]:
    """Apply the ``<created_at_iso>_<id>`` keyset filter to a TriggerEvent query.

    The admin and operator trigger-event listing endpoints both paginate the
    event log with this ``{created_at}_{id}`` cursor. Keeping the parse+filter
    here (rather than duplicated inline in the routes) prevents the two copies
    drifting apart. A malformed cursor is ignored (logged) so a stale or
    hostile cursor cannot break the page query; *q* is returned unchanged in
    that case.
    """
    if not cursor:
        return q
    try:
        cursor_ts_str, cursor_id = cursor.split("_", 1)
        cursor_dt = datetime.fromisoformat(cursor_ts_str)
        cursor_uuid = uuid.UUID(cursor_id)
    except (ValueError, AttributeError):
        _log.warning("Malformed cursor ignored: %s", sanitise_log_value(cursor), exc_info=True)
        return q
    return q.where(
        (TriggerEvent.created_at < cursor_dt)
        | ((TriggerEvent.created_at == cursor_dt) & (TriggerEvent.id < cursor_uuid))
    )


async def list_triggers(
    session: AsyncSession,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID | None = None,
    cursor: str | None = None,
    limit: int = 20,
    team_id: uuid.UUID | None = None,
) -> PageResult[Trigger]:
    q = (
        select(Trigger)
        .join(Pipeline, Trigger.pipeline_id == Pipeline.id)
        .where(
            Trigger.organisation_id == org_id,
            Pipeline.deleted_at.is_(None),
            Trigger.deleted_at.is_(None),
        )
    )
    if pipeline_id is not None:
        q = q.where(Trigger.pipeline_id == pipeline_id)
    if team_id is not None:
        # A team-scoped caller sees triggers for its own team's pipelines plus
        # org-level pipelines (no owner team) — the same boundary the MCP
        # guard applies.
        q = q.where(team_scope_clause(Pipeline.owner_team_id, team_id))
    q = q.order_by(Trigger.created_at.desc())

    if cursor is not None:
        paginator = CursorPaginator(sort_field="created_at", sort_dir="desc")
        cp = await paginator.paginate(
            session,
            q,
            cursor=cursor,
            limit=limit,
            model=Trigger,
            compute_total=True,
        )
        return PageResult(
            items=cp.items,
            total=cp.total or 0,
            page=1,
            page_size=limit,
            next_cursor=cp.next_cursor,
            has_more=cp.has_more,
        )

    result = await session.execute(q)
    items = list(result.scalars().all())
    total = len(items)
    return PageResult(items=items, total=total, page=1, page_size=limit, has_more=False)


async def soft_delete_trigger(
    session: AsyncSession,
    trigger_id: uuid.UUID,
) -> Trigger | None:
    """Mark a trigger as deleted (soft delete). Returns None if not found or already deleted."""
    result = await session.execute(
        update(Trigger)
        .where(Trigger.id == trigger_id, Trigger.deleted_at.is_(None))
        .values(deleted_at=func.now())
        .returning(Trigger)
    )
    await session.flush()
    return result.scalar_one_or_none()


async def restore_trigger(
    session: AsyncSession,
    trigger_id: uuid.UUID,
) -> Trigger | None:
    """Restore a soft-deleted trigger. Returns None if not found."""
    result = await session.execute(
        update(Trigger)
        .where(Trigger.id == trigger_id, Trigger.deleted_at.is_not(None))
        .values(deleted_at=None)
        .returning(Trigger)
    )
    await session.flush()
    return result.scalar_one_or_none()


async def load_trigger_and_org_global(
    system_session: AsyncSession,
    trigger_id: uuid.UUID,
    principal_org_id: uuid.UUID | None,
) -> tuple[Trigger, uuid.UUID]:
    """Bootstrap-read the trigger row instance-globally via the SYSTEM session.

    The trigger row must be read BEFORE the app session's RLS org context can
    exist: the org is derived FROM the trigger (for unauthenticated webhook and
    Slack deliveries there is no principal org at all), and the HMAC/slack
    signing secret needed to authenticate the delivery lives on the trigger row
    itself — a chicken-and-egg bootstrap. On Postgres the app session runs as
    ``modulo_app`` (NOBYPASSRLS, non-owner), so a pre-context read of the
    org-scoped ``triggers`` table matches ZERO rows and every delivery 404s
    (the FAR-457 silent-empty failure class; the BDD/integration harnesses
    connect as the table owner, where RLS does not apply, so they cannot catch
    it). The system session (``modulo_system``, BYPASSRLS) resolves the
    bootstrap row instance-globally — the same mechanism as the pre-auth SSO
    provider resolution. The caller pins the app session to the returned org
    with ``set_rls_org`` and still enforces authenticity AFTER this read and
    BEFORE any tenant-scoped mutation.

    Because the trigger row carries its own ``organisation_id`` (OrgScoped,
    NOT NULL), the org always resolves — the old pipeline-fallback lookup was
    redundant and is not performed here (this also removes a system-session
    query from the pre-auth hot path). Tenancy: a principal referencing a
    trigger owned by ANOTHER org is rejected with the same 404 as a missing
    trigger (fail closed — no cross-tenant enumeration); unauthenticated
    callers (``principal_org_id=None``) proceed to the route's own
    signature/HMAC verification against the trigger's secret.

    Raises:
        TriggerNotFoundError: the trigger does not exist (including a
            SOFT-DELETED trigger — deliveries must not be accepted for rows
            that are deleted, so ``deleted_at`` is filtered here), or it
            belongs to a different org than the authenticated principal's.
        TriggerConfigInvalidError: the trigger's ``config_json`` is present
            but not a JSON object (schema drift / manual edit). Validating
            HERE — the single bootstrap site — closes the per-route
            isinstance-guard gaps (e.g. authenticated replay previously
            skipped its guard and could 500 on ``AttributeError``).
    """
    async with system_session.begin():
        trigger_row = await system_session.execute(
            select(Trigger).where(Trigger.id == trigger_id, Trigger.deleted_at.is_(None))
        )
        trigger = trigger_row.scalar_one_or_none()
    if trigger is None:
        raise TriggerNotFoundError(trigger_id=trigger_id)
    cfg = trigger.config_json
    if cfg is not None and not isinstance(cfg, dict):
        # None → unconfigured (routes treat it as {}); dict → well-formed. ANY
        # other value — including falsy [] / "" / 0 — is corruption and would
        # AttributeError on the route's ``.get`` reads (external ingress → 500).
        _log.warning(
            "trigger.bootstrap_config_invalid trigger=%s config_type=%s",
            sanitise_log_value(str(trigger_id)),
            type(cfg).__name__,
        )
        raise TriggerConfigInvalidError(trigger_id=trigger_id)
    org_id: uuid.UUID = trigger.organisation_id
    if principal_org_id is not None and principal_org_id != org_id:
        # Cross-tenant reference: same 404 as a missing trigger so the route
        # cannot be used to enumerate other orgs' triggers.
        _log.warning(
            "trigger.bootstrap_org_mismatch trigger=%s principal_org=%s trigger_org=%s",
            sanitise_log_value(str(trigger_id)),
            sanitise_log_value(str(principal_org_id)),
            sanitise_log_value(str(org_id)),
        )
        raise TriggerNotFoundError(trigger_id=trigger_id)
    return trigger, org_id
