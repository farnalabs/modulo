"""Admin-only trigger event log endpoints."""

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple, cast

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from modulo.api.constants import MSG_INTERNAL_SERVER_ERROR
from modulo.api.db_error_handling import handle_db_errors, raise_session_contract_error
from modulo.api.dependencies import get_db_session, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.db.crud.trigger import apply_trigger_event_cursor
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.trigger import Trigger
from modulo.db.models.trigger_event import TriggerEvent
from modulo.db.rls import set_rls_org
from modulo.db.soft_delete import include_soft_deleted

_CODE_ADMIN_TRIGGERS_LIST_TRIGGER = "admin_triggers.list_trigger_events"

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/trigger-events", tags=["admin-trigger-events"])

# FAR-1344 — the capacity/backpressure outcomes of a delivery. These are the
# TriggerEvent.validation_result values that mean "the queue/limit refused,
# coalesced or held this delivery", and they are what the trigger-event log
# surfaces as a capacity count/trend so an operator sees pressure building
# without drilling into an event.
CAPACITY_VALIDATION_RESULTS: tuple[str, ...] = (
    "backpressure_skipped",
    "concurrency_limit_reached",
    "coalesced",
)

# The trend window: the last 24h vs the 24h before it.
_RESULT_COUNT_WINDOW_HOURS = 24


class TriggerEventResultCounts(BaseModel):
    """Capacity-outcome counts for the last two 24h windows (FAR-1344).

    ``recent``/``previous`` are keyed by ``validation_result`` for the capacity
    vocabulary only — an org-wide summary, not a filtered page count.
    """

    window_hours: int
    recent: dict[str, int]
    previous: dict[str, int]
    total_recent: int
    total_previous: int
    direction: str  # "up" | "down" | "flat"


class TriggerEventItem(BaseModel):
    id: str
    trigger_id: str
    trigger_type: str
    validation_result: str
    received_at: str | None = None
    created_at: str | None = None
    run_id: str | None = None
    pipeline_id: str | None = None
    pipeline_name: str | None = None
    # True when the event's pipeline has been soft-deleted or archived. The
    # event history is preserved either way (FAR-2226), but the UI must not
    # offer a link into an editor the pipeline no longer has.
    pipeline_deleted: bool = False
    error_detail: str | None = None


class _TriggerEventRow(NamedTuple):
    """One event plus its pipeline projection.

    The joins are INNER joins: ``trigger_events.trigger_id`` and
    ``triggers.pipeline_id`` are both non-nullable FKs, so neither side can be
    NULL and the projection needs no nullable-name branching. The soft-delete
    filters are opted out for this historical read (FAR-2226), so a
    soft-deleted trigger or pipeline keeps its events reachable.
    """

    event: TriggerEvent
    pipeline_id: uuid.UUID
    pipeline_name: str
    pipeline_deleted: bool


class TriggerEventListResponse(BaseModel):
    items: list[TriggerEventItem]
    next_cursor: str | None = None
    prev_cursor: str | None = None
    total: int
    result_counts: TriggerEventResultCounts | None = None


async def _run_trigger_event_query(query: Callable[[], Awaitable[Any]], fail_log: str) -> Any:
    """Run a trigger-event DB query, mapping DB errors to the HTTP error contract.

    The result is intentionally ``Any`` (explicit): the PEP 695 generic syntax
    ruff prefers here is not parseable by the SonarQube 9.9 Python analyzer,
    which would silently skip this file from all analysis.
    """
    try:
        return await query()
    except ProgrammingError:
        _log.exception(_CODE_ADMIN_TRIGGERS_LIST_TRIGGER)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "admin_triggers._run_trigger_event_query")
        _log.exception(_CODE_ADMIN_TRIGGERS_LIST_TRIGGER)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception(fail_log)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None


def _event_query_scope(principal: TenantPrincipal) -> tuple[ColumnElement[bool], ...]:
    """The org predicates shared by the page and total queries.

    Kept in one place so the two statements can never drift apart — a
    divergence is exactly what made ``total`` overstate the reachable rows.
    """
    return (
        TriggerEvent.organisation_id == principal.organisation_id,
        Trigger.organisation_id == principal.organisation_id,
        Pipeline.organisation_id == principal.organisation_id,
    )


async def _fetch_trigger_event_rows(
    session: AsyncSession,
    principal: TenantPrincipal,
    trigger_type: str | None,
    validation_result: str | None,
    cursor: str | None,
    limit: int,
) -> list[_TriggerEventRow]:
    """Query trigger events with optional type/result filters and a keyset cursor."""

    async def query() -> list[_TriggerEventRow]:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            q = (
                select(TriggerEvent)
                .join(Trigger, Trigger.id == TriggerEvent.trigger_id)
                .join(Pipeline, Pipeline.id == Trigger.pipeline_id)
                .where(*_event_query_scope(principal))
            )
            if trigger_type:
                q = q.where(TriggerEvent.trigger_type == trigger_type)
            if validation_result:
                q = q.where(TriggerEvent.validation_result == validation_result)
            if cursor:
                q = apply_trigger_event_cursor(q, cursor)
            # Historical read: an event must survive the soft-deletion of its
            # trigger or pipeline, otherwise history silently disappears from
            # the admin log (FAR-2226). The opt-out rides the statement through
            # the generative calls below (verified: execution options survive).
            q = include_soft_deleted(q)
            # ``add_columns`` does not widen the statement's ReturnRows type in
            # SQLAlchemy's stubs, so the projected row shape is asserted here
            # instead of casting the statement to Any.
            statement = cast(
                "Select[TriggerEvent, uuid.UUID, str, bool]",
                q.add_columns(
                    Trigger.pipeline_id,
                    Pipeline.name,
                    sa.or_(Pipeline.deleted_at.is_not(None), Pipeline.archived_at.is_not(None)),
                ),
            )
            statement = statement.order_by(TriggerEvent.created_at.desc(), TriggerEvent.id.desc()).limit(limit + 1)
            rows = (await session.execute(statement)).all()
            return [
                _TriggerEventRow(event, pipeline_id, pipeline_name, deleted)
                for event, pipeline_id, pipeline_name, deleted in rows
            ]

    result: Any = await _run_trigger_event_query(query, "admin list_trigger_events failed")
    rows: list[_TriggerEventRow] = cast("list[_TriggerEventRow]", result)
    return rows


async def _count_trigger_events_total(session: AsyncSession, principal: TenantPrincipal) -> int:
    """Count every reachable trigger event in the caller's org (ignores filters).

    "Reachable" must mean the same thing as the page query: the event's trigger
    and pipeline still join, soft-deleted or not. Counting rows the page query
    cannot return made ``showing X of N`` overstate N and left trailing pages
    empty (FAR-2226).
    """

    async def query() -> int:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            stmt = (
                select(func.count(TriggerEvent.id))
                .join(Trigger, Trigger.id == TriggerEvent.trigger_id)
                .join(Pipeline, Pipeline.id == Trigger.pipeline_id)
                .where(*_event_query_scope(principal))
            )
            count_result = await session.execute(include_soft_deleted(stmt))
            return count_result.scalar() or 0

    result: Any = await _run_trigger_event_query(query, "admin list_trigger_events count query failed")
    total: int = cast("int", result)
    return total


def _trend_direction(total_recent: int, total_previous: int) -> str:
    if total_recent > total_previous:
        return "up"
    if total_recent < total_previous:
        return "down"
    return "flat"


async def _fetch_result_counts(
    session: AsyncSession,
    principal: TenantPrincipal,
    *,
    now: datetime,
) -> TriggerEventResultCounts:
    """Capacity-outcome counts for the last two 24h windows (FAR-1344).

    One grouped query over the capacity vocabulary (never the whole org table):
    the recent window is the last ``_RESULT_COUNT_WINDOW_HOURS``, the previous
    window the same span immediately before it. The two are counted with
    conditional aggregates so the trend is one round trip.
    """
    cutoff_recent = now - timedelta(hours=_RESULT_COUNT_WINDOW_HOURS)
    cutoff_previous = now - timedelta(hours=_RESULT_COUNT_WINDOW_HOURS * 2)
    recent_expr = func.count().filter(TriggerEvent.created_at >= cutoff_recent)
    previous_expr = func.count().filter(
        sa.and_(TriggerEvent.created_at >= cutoff_previous, TriggerEvent.created_at < cutoff_recent)
    )

    async def query() -> list[Any]:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            stmt = (
                select(TriggerEvent.validation_result, recent_expr, previous_expr)
                .where(
                    TriggerEvent.organisation_id == principal.organisation_id,
                    TriggerEvent.validation_result.in_(CAPACITY_VALIDATION_RESULTS),
                    TriggerEvent.created_at >= cutoff_previous,
                )
                .group_by(TriggerEvent.validation_result)
            )
            return list((await session.execute(stmt)).all())

    rows: list[Any] = cast(
        "list[Any]", await _run_trigger_event_query(query, "admin list_trigger_events result-count query failed")
    )
    recent: dict[str, int] = dict.fromkeys(CAPACITY_VALIDATION_RESULTS, 0)
    previous: dict[str, int] = dict.fromkeys(CAPACITY_VALIDATION_RESULTS, 0)
    for validation_result, recent_count, previous_count in rows:
        key = str(validation_result)
        if key in recent:
            recent[key] = int(recent_count or 0)
            previous[key] = int(previous_count or 0)
    total_recent = sum(recent.values())
    total_previous = sum(previous.values())
    return TriggerEventResultCounts(
        window_hours=_RESULT_COUNT_WINDOW_HOURS,
        recent=recent,
        previous=previous,
        total_recent=total_recent,
        total_previous=total_previous,
        direction=_trend_direction(total_recent, total_previous),
    )


@router.get("")
@handle_db_errors("admin.triggers.list_trigger_events")
async def list_trigger_events(
    trigger_type: str | None = Query(None),
    validation_result: str | None = Query(None),
    cursor: str | None = Query(None, description="Cursor: createdAt_id"),
    limit: int = Query(25, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("admin.trigger_events"),
) -> TriggerEventListResponse:
    rows = await _fetch_trigger_event_rows(session, principal, trigger_type, validation_result, cursor, limit)
    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    items = [
        TriggerEventItem(
            id=str(row.event.id),
            trigger_id=str(row.event.trigger_id),
            trigger_type=row.event.trigger_type,
            validation_result=row.event.validation_result,
            received_at=row.event.received_at.isoformat() if row.event.received_at else None,
            created_at=row.event.created_at.isoformat() if row.event.created_at else None,
            run_id=str(row.event.run_id) if row.event.run_id else None,
            pipeline_id=str(row.pipeline_id) if row.pipeline_id else None,
            pipeline_name=row.pipeline_name,
            pipeline_deleted=bool(row.pipeline_deleted),
            error_detail=row.event.error_detail,
        )
        for row in rows
    ]

    next_cursor: str | None = None
    prev_cursor: str | None = None
    if has_more and rows:
        last = rows[-1].event
        next_cursor = f"{last.created_at.isoformat()}_{last.id}"
    if rows:
        first = rows[0].event
        prev_cursor = f"{first.created_at.isoformat()}_{first.id}"

    total = await _count_trigger_events_total(session, principal)
    result_counts = await _fetch_result_counts(session, principal, now=datetime.now(UTC))

    return TriggerEventListResponse(
        items=items,
        next_cursor=next_cursor,
        prev_cursor=prev_cursor,
        total=total,
        result_counts=result_counts,
    )
