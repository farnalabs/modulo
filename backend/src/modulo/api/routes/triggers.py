"""Trigger management endpoints — cron, polling, and listing.

URLs:
    GET    /api/v1/triggers                    — list all triggers (paginated)
    PATCH  /api/v1/triggers/{id}/cron          — update cron config
    GET    /api/v1/triggers/{id}/cron/preview   — preview next N fire times
    PATCH  /api/v1/triggers/{id}/polling        — update polling config
    POST   /api/v1/triggers/{id}/polling/test   — test polling query/condition
"""

import datetime
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.api.middleware.sensitive_mask import mask_config_json
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.cron_scheduler import compute_next_fire, validate_cron_expression
from modulo.core.trigger_engine import TriggerEngine
from modulo.db.models.trigger import Trigger
from modulo.db.models.trigger_event import TriggerEvent
from modulo.db.rls import set_rls_org

router = APIRouter(prefix="/api/v1", tags=["triggers"])


@router.get("/triggers", status_code=status.HTTP_200_OK)
async def list_triggers(
    pipeline_id: uuid.UUID | None = Query(None),
    trigger_type: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """List all triggers, optionally filtered by pipeline or type."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        q = select(Trigger).where(Trigger.organisation_id == principal.organisation_id)

        if pipeline_id is not None:
            q = q.where(Trigger.pipeline_id == pipeline_id)
        if trigger_type is not None:
            q = q.where(Trigger.trigger_type == trigger_type)

        total = len((await session.execute(q)).scalars().all())
        offset = (page - 1) * page_size
        q = q.order_by(Trigger.created_at.desc()).offset(offset).limit(page_size)
        rows = (await session.execute(q)).scalars().all()

    return {
        "items": [
            {
                "id": str(r.id),
                "pipeline_id": str(r.pipeline_id),
                "trigger_type": r.trigger_type,
                "active": r.active,
                "max_concurrent_runs": r.max_concurrent_runs,
                "config_json": mask_config_json(r.config_json),
                "cron_expression": r.cron_expression,
                "cron_timezone": r.cron_timezone,
                "last_fired_at": r.last_fired_at.isoformat() if r.last_fired_at else None,
                "next_fire_at": r.next_fire_at.isoformat() if r.next_fire_at else None,
                "created_by": str(r.created_by),
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


class CronConfigUpdate(BaseModel):
    """Request body for PATCH /triggers/{id}/cron."""

    cron_expression: str | None = None
    cron_timezone: str | None = None
    active: bool | None = None
    snapshot_id: str | None = None
    input_template: dict[str, Any] | None = None


@router.patch("/triggers/{trigger_id}/cron", status_code=status.HTTP_200_OK)
async def update_cron_config(
    trigger_id: uuid.UUID,
    body: CronConfigUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """Update cron configuration for a trigger.

    Validates the cron expression before saving. Computes ``next_fire_at``
    when the expression or timezone changes.
    """
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        result = await session.execute(
            select(Trigger).where(
                Trigger.id == trigger_id,
                Trigger.organisation_id == principal.organisation_id,
            )
        )
        trigger = result.scalar_one_or_none()
        if trigger is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trigger not found")

        if trigger.trigger_type != "cron":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only cron triggers can have cron configuration",
            )

        if body.active is not None:
            trigger.active = body.active

        if body.cron_expression is not None:
            err = validate_cron_expression(
                body.cron_expression,
                body.cron_timezone or trigger.cron_timezone or "UTC",
            )
            if err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Invalid cron expression: {err}",
                )
            trigger.cron_expression = body.cron_expression

        if body.cron_timezone is not None:
            trigger.cron_timezone = body.cron_timezone

        # Recompute next_fire_at if relevant
        if body.cron_expression is not None or body.cron_timezone is not None:
            if trigger.cron_expression:
                tz = trigger.cron_timezone or "UTC"
                err = validate_cron_expression(trigger.cron_expression, tz)
                if err is None:
                    trigger.next_fire_at = compute_next_fire(trigger.cron_expression)

        if body.snapshot_id is not None:
            trigger.config_json = {**(trigger.config_json or {}), "snapshot_id": body.snapshot_id}

        if body.input_template is not None:
            trigger.config_json = {**(trigger.config_json or {}), "input_template": body.input_template}

        await session.flush()

    return {
        "id": str(trigger.id),
        "cron_expression": trigger.cron_expression,
        "cron_timezone": trigger.cron_timezone,
        "active": trigger.active,
        "next_fire_at": trigger.next_fire_at.isoformat() if trigger.next_fire_at else None,
        "input_template": trigger.config_json.get("input_template") if trigger.config_json else None,
    }


@router.get("/triggers/{trigger_id}/cron/preview", status_code=status.HTTP_200_OK)
async def preview_cron_schedule(
    trigger_id: uuid.UUID,
    count: int = Query(5, ge=1, le=50),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """Preview the next *count* fire times for a cron trigger."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        result = await session.execute(
            select(Trigger).where(
                Trigger.id == trigger_id,
                Trigger.organisation_id == principal.organisation_id,
            )
        )
        trigger = result.scalar_one_or_none()
        if trigger is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trigger not found")

        if not trigger.cron_expression:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Trigger has no cron expression configured",
            )

        from croniter import croniter

        cron = croniter(trigger.cron_expression, datetime.datetime.now(datetime.UTC))
        times: list[str] = []
        for _ in range(count):
            next_dt = cron.get_next(datetime.datetime)
            times.append(next_dt.isoformat())

    return {
        "trigger_id": str(trigger_id),
        "cron_expression": trigger.cron_expression,
        "cron_timezone": trigger.cron_timezone or "UTC",
        "next_fire_times": times,
    }


# ---------------------------------------------------------------------------
# Polling trigger config
# ---------------------------------------------------------------------------


class PollingConfigUpdate(BaseModel):
    """Request body for PATCH /triggers/{id}/polling."""

    active: bool | None = None
    connector_instance_id: str | None = None
    poll_query: str | None = None
    condition_expression: str | None = None
    poll_interval_seconds: int | None = Field(None, ge=10)
    snapshot_id: str | None = None


@router.patch("/triggers/{trigger_id}/polling", status_code=status.HTTP_200_OK)
async def update_polling_config(
    trigger_id: uuid.UUID,
    body: PollingConfigUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """Update polling configuration for a trigger.

    Validates that the trigger is of type ``polling`` before applying changes.
    Recomputes ``next_fire_at`` when the interval or config changes.
    """
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        result = await session.execute(
            select(Trigger).where(
                Trigger.id == trigger_id,
                Trigger.organisation_id == principal.organisation_id,
            )
        )
        trigger = result.scalar_one_or_none()
        if trigger is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trigger not found")

        if trigger.trigger_type != "polling":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only polling triggers can have polling configuration",
            )

        if body.active is not None:
            trigger.active = body.active

        config = dict(trigger.config_json or {})

        if body.connector_instance_id is not None:
            config["connector_instance_id"] = body.connector_instance_id
        if body.poll_query is not None:
            config["poll_query"] = body.poll_query
        if body.condition_expression is not None:
            config["condition_expression"] = body.condition_expression
        if body.poll_interval_seconds is not None:
            config["poll_interval_seconds"] = body.poll_interval_seconds
        if body.snapshot_id is not None:
            config["snapshot_id"] = body.snapshot_id

        trigger.config_json = config

        # Recompute next_fire_at when interval or config changes
        if any(
            x is not None
            for x in [
                body.poll_interval_seconds,
                body.connector_instance_id,
                body.poll_query,
            ]
        ):
            trigger_engine = TriggerEngine()
            await trigger_engine.schedule_polling_trigger(session, trigger=trigger, org_id=principal.organisation_id)

        await session.flush()

    return {
        "id": str(trigger.id),
        "active": trigger.active,
        "config_json": mask_config_json(trigger.config_json),
        "next_fire_at": trigger.next_fire_at.isoformat() if trigger.next_fire_at else None,
    }


class PollingTestRequest(BaseModel):
    """Request body for POST /triggers/{id}/polling/test."""

    connector_instance_id: str
    poll_query: str
    condition_expression: str | None = None


@router.post("/triggers/{trigger_id}/polling/test", status_code=status.HTTP_200_OK)
async def test_polling_condition(
    trigger_id: uuid.UUID,
    body: PollingTestRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """Test a polling trigger's query and condition expression without firing a run.

    Runs the connector query and JMESPath evaluation, returning the result
    status and matching records. Does not create a Run or TriggerEvent.
    """
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        result = await session.execute(
            select(Trigger).where(
                Trigger.id == trigger_id,
                Trigger.organisation_id == principal.organisation_id,
            )
        )
        trigger = result.scalar_one_or_none()
        if trigger is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trigger not found")

        if trigger.trigger_type != "polling":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only polling triggers can be tested",
            )

    # Evaluate outside the transaction (connector ops are I/O, not DB)
    trigger_engine = TriggerEngine()
    eval_result = await trigger_engine.evaluate_condition(
        session,
        trigger=trigger,
        org_id=principal.organisation_id,
        connector_instance_id=uuid.UUID(body.connector_instance_id),
        poll_query=body.poll_query,
        condition_expression=body.condition_expression,
    )

    return eval_result


# ---------------------------------------------------------------------------
# Trigger CRUD
# ---------------------------------------------------------------------------


class TriggerCreate(BaseModel):
    trigger_type: str = Field(..., pattern=r"^(manual|webhook|cron|polling)$")
    active: bool = True
    max_concurrent_runs: int = 1
    config_json: dict[str, Any] = Field(default_factory=dict)
    cron_expression: str | None = None
    cron_timezone: str | None = None


@router.post("/pipelines/{pipeline_id}/triggers", status_code=status.HTTP_201_CREATED)
async def create_trigger(
    pipeline_id: uuid.UUID,
    body: TriggerCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """Create a new trigger for a pipeline."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        trigger = Trigger(
            organisation_id=principal.organisation_id,
            pipeline_id=pipeline_id,
            trigger_type=body.trigger_type,
            active=body.active,
            max_concurrent_runs=body.max_concurrent_runs,
            config_json=body.config_json,
            cron_expression=body.cron_expression,
            cron_timezone=body.cron_timezone,
            created_by=principal.user_id,
        )
        if body.cron_expression:
            err = validate_cron_expression(body.cron_expression, body.cron_timezone or "UTC")
            if err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Invalid cron expression: {err}",
                )
            trigger.next_fire_at = compute_next_fire(body.cron_expression)
        session.add(trigger)
        await session.flush()

    return {
        "id": str(trigger.id),
        "pipeline_id": str(trigger.pipeline_id),
        "trigger_type": trigger.trigger_type,
        "active": trigger.active,
        "max_concurrent_runs": trigger.max_concurrent_runs,
        "config_json": mask_config_json(trigger.config_json),
        "cron_expression": trigger.cron_expression,
        "cron_timezone": trigger.cron_timezone,
        "last_fired_at": trigger.last_fired_at.isoformat() if trigger.last_fired_at else None,
        "next_fire_at": trigger.next_fire_at.isoformat() if trigger.next_fire_at else None,
        "input_template": trigger.config_json.get("input_template") if trigger.config_json else None,
    }


class TriggerUpdate(BaseModel):
    active: bool | None = None
    max_concurrent_runs: int | None = None
    config_json: dict[str, Any] | None = None
    cron_expression: str | None = None
    cron_timezone: str | None = None


@router.put("/triggers/{trigger_id}", status_code=status.HTTP_200_OK)
async def update_trigger(
    trigger_id: uuid.UUID,
    body: TriggerUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """Update a trigger's general configuration."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        result = await session.execute(
            select(Trigger).where(
                Trigger.id == trigger_id,
                Trigger.organisation_id == principal.organisation_id,
            )
        )
        trigger = result.scalar_one_or_none()
        if trigger is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trigger not found")

        if body.active is not None:
            trigger.active = body.active
        if body.max_concurrent_runs is not None:
            trigger.max_concurrent_runs = body.max_concurrent_runs
        if body.config_json is not None:
            trigger.config_json = body.config_json
        if body.cron_expression is not None:
            if trigger.trigger_type not in ("cron",):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Only cron triggers can have cron expressions",
                )
            tz = body.cron_timezone or trigger.cron_timezone or "UTC"
            err = validate_cron_expression(body.cron_expression, tz)
            if err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Invalid cron expression: {err}",
                )
            trigger.cron_expression = body.cron_expression
        if body.cron_timezone is not None:
            trigger.cron_timezone = body.cron_timezone
        if body.cron_expression is not None or body.cron_timezone is not None:
            if trigger.cron_expression:
                tz = trigger.cron_timezone or "UTC"
                err = validate_cron_expression(trigger.cron_expression, tz)
                if err is None:
                    trigger.next_fire_at = compute_next_fire(trigger.cron_expression)

        await session.flush()

    return {
        "id": str(trigger.id),
        "pipeline_id": str(trigger.pipeline_id),
        "trigger_type": trigger.trigger_type,
        "active": trigger.active,
        "max_concurrent_runs": trigger.max_concurrent_runs,
        "config_json": mask_config_json(trigger.config_json),
        "cron_expression": trigger.cron_expression,
        "cron_timezone": trigger.cron_timezone,
        "last_fired_at": trigger.last_fired_at.isoformat() if trigger.last_fired_at else None,
        "next_fire_at": trigger.next_fire_at.isoformat() if trigger.next_fire_at else None,
    }


@router.delete("/triggers/{trigger_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_trigger(
    trigger_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> None:
    """Delete a trigger and its associated events (cascade)."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        result = await session.execute(
            select(Trigger).where(
                Trigger.id == trigger_id,
                Trigger.organisation_id == principal.organisation_id,
            )
        )
        trigger = result.scalar_one_or_none()
        if trigger is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trigger not found")
        await session.delete(trigger)


@router.post("/triggers/{trigger_id}/toggle", status_code=status.HTTP_200_OK)
async def toggle_trigger(
    trigger_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """Toggle a trigger's active state."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        result = await session.execute(
            select(Trigger).where(
                Trigger.id == trigger_id,
                Trigger.organisation_id == principal.organisation_id,
            )
        )
        trigger = result.scalar_one_or_none()
        if trigger is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trigger not found")

        trigger.active = not trigger.active
        await session.flush()

    return {"id": str(trigger.id), "active": trigger.active}


class TestTriggerRequest(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


@router.post("/triggers/{trigger_id}/test", status_code=status.HTTP_200_OK)
async def test_trigger(
    trigger_id: uuid.UUID,
    body: TestTriggerRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """Fire a test event for a trigger.

    For manual triggers this also creates a Run. For all trigger types
    a TriggerEvent is recorded.
    """
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        result = await session.execute(
            select(Trigger).where(
                Trigger.id == trigger_id,
                Trigger.organisation_id == principal.organisation_id,
            )
        )
        trigger = result.scalar_one_or_none()
        if trigger is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trigger not found")

        import hashlib
        import json

        raw_body = json.dumps(body.payload, sort_keys=True).encode()
        payload_hash = hashlib.sha256(raw_body).hexdigest()

        event = TriggerEvent(
            organisation_id=principal.organisation_id,
            trigger_id=trigger.id,
            trigger_type=trigger.trigger_type,
            raw_payload_hash=payload_hash,
            validation_result="test",
            error_detail=None,
        )
        session.add(event)

        run_id: str | None = None
        if trigger.trigger_type == "manual":
            from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph
            from modulo.db.crud.run import create_run

            snapshot = await create_snapshot_from_live_graph(
                session, pipeline_id=trigger.pipeline_id, created_by=principal.user_id
            )
            if snapshot is None:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to create pipeline snapshot for test trigger",
                )
            run = await create_run(
                session,
                org_id=principal.organisation_id,
                pipeline_id=trigger.pipeline_id,
                snapshot_id=snapshot.id,
                trigger_type="manual",
                input_payload=body.payload,
                created_by=principal.user_id,
                trigger_id=trigger.id,
            )
            run_id = str(run.id)
            event.run_id = run.id

        await session.flush()

    return {
        "event_id": str(event.id),
        "run_id": run_id,
        "status": "test_event_created",
    }


@router.get("/triggers/{trigger_id}/events", status_code=status.HTTP_200_OK)
async def list_trigger_events(
    trigger_id: uuid.UUID,
    event_status: str | None = Query(None, alias="status"),
    cursor: str | None = Query(None, description="Cursor: createdAt_eventId"),
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """List trigger events with cursor-based pagination.

    Supports filtering by status (validation_result). Returns a ``next_cursor``
    value that can be passed as ``cursor`` on the next request.
    """
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        trigger_result = await session.execute(
            select(Trigger).where(
                Trigger.id == trigger_id,
                Trigger.organisation_id == principal.organisation_id,
            )
        )
        trigger = trigger_result.scalar_one_or_none()
        if trigger is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trigger not found")

        q = select(TriggerEvent).where(
            TriggerEvent.trigger_id == trigger_id,
            TriggerEvent.organisation_id == principal.organisation_id,
        )
        if event_status is not None:
            q = q.where(TriggerEvent.validation_result == event_status)

        if cursor:
            try:
                cursor_created_at_str, cursor_id = cursor.split("_", 1)
                cursor_dt = datetime.datetime.fromisoformat(cursor_created_at_str)
                cursor_uuid = uuid.UUID(cursor_id)
                q = q.where(
                    (TriggerEvent.created_at < cursor_dt)
                    | ((TriggerEvent.created_at == cursor_dt) & (TriggerEvent.id < cursor_uuid))
                )
            except (ValueError, AttributeError):
                pass

        q = q.order_by(TriggerEvent.created_at.desc(), TriggerEvent.id.desc()).limit(limit + 1)
        rows = (await session.execute(q)).scalars().all()

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    items = [
        {
            "id": str(e.id),
            "trigger_id": str(e.trigger_id),
            "status": e.validation_result,
            "received_at": e.received_at.isoformat() if e.received_at else None,
            "created_at": e.created_at.isoformat() if e.created_at else None,
            "run_id": str(e.run_id) if e.run_id else None,
            "error_detail": e.error_detail,
        }
        for e in rows
    ]

    next_cursor: str | None = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = f"{last.created_at.isoformat()}_{last.id}"

    return {
        "items": items,
        "next_cursor": next_cursor,
        "limit": limit,
    }


# ---------------------------------------------------------------------------
# Pipeline-scoped trigger router
# ---------------------------------------------------------------------------

pipeline_triggers_router = APIRouter(prefix="/api/v1/pipelines", tags=["pipeline-triggers"])


@pipeline_triggers_router.get("/{pipeline_id}/triggers", status_code=status.HTTP_200_OK)
async def list_pipeline_triggers(
    pipeline_id: uuid.UUID,
    trigger_type: str | None = Query(None),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """List triggers for a specific pipeline."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        q = select(Trigger).where(
            Trigger.pipeline_id == pipeline_id,
            Trigger.organisation_id == principal.organisation_id,
        )
        if trigger_type is not None:
            q = q.where(Trigger.trigger_type == trigger_type)
        q = q.order_by(Trigger.created_at.desc())
        rows = (await session.execute(q)).scalars().all()

    return {
        "items": [
            {
                "id": str(r.id),
                "pipeline_id": str(r.pipeline_id),
                "trigger_type": r.trigger_type,
                "active": r.active,
                "max_concurrent_runs": r.max_concurrent_runs,
                "config_json": mask_config_json(r.config_json),
                "cron_expression": r.cron_expression,
                "cron_timezone": r.cron_timezone,
                "last_fired_at": r.last_fired_at.isoformat() if r.last_fired_at else None,
                "next_fire_at": r.next_fire_at.isoformat() if r.next_fire_at else None,
                "created_by": str(r.created_by),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }
