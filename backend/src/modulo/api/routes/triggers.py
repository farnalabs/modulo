"""Trigger management endpoints — cron, polling, and listing.

URLs:
    GET    /api/v1/triggers                    — list all triggers (paginated)
    PATCH  /api/v1/triggers/{id}/cron          — update cron config
    GET    /api/v1/triggers/{id}/cron/preview   — preview next N fire times
    PATCH  /api/v1/triggers/{id}/polling        — update polling config
    POST   /api/v1/triggers/{id}/polling/test   — test polling query/condition
"""

import datetime
import hashlib
import json
import logging
import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import deny_break_glass_mint, get_db_session, require_permission
from modulo.api.middleware.sensitive_mask import mask_config_json
from modulo.auth.jwt import TenantPrincipal
from modulo.core.cron_helpers import compute_next_fire, validate_cron_expression
from modulo.core.exceptions import OrgDeletedError
from modulo.core.trigger_engine import TriggerEngine
from modulo.db.models.organisation import Organisation
from modulo.db.models.trigger import Trigger
from modulo.db.models.trigger_event import TriggerEvent
from modulo.db.rls import set_rls_org
from modulo.db.settings_resolver import org_row_is_paused

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["triggers"])


def _serialize_spend_limit(value: Decimal | None) -> float | None:
    """Serialize the trigger-level ``daily_spend_limit`` Numeric column to JSON.

    Returns ``None`` when no limit is configured so callers can distinguish
    "unlimited" from a zero budget.
    """
    if value is None:
        return None
    return float(value)


@router.get("/triggers", status_code=status.HTTP_200_OK)
@handle_db_errors("triggers.list_triggers")
async def list_triggers(
    pipeline_id: uuid.UUID | None = Query(None),
    trigger_type: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("trigger.list"),
) -> dict[str, Any]:
    """List all triggers, optionally filtered by pipeline or type."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            base_filter = Trigger.organisation_id == principal.organisation_id
            q = select(Trigger).where(base_filter, Trigger.deleted_at.is_(None))

            if pipeline_id is not None:
                q = q.where(Trigger.pipeline_id == pipeline_id)
            if trigger_type is not None:
                q = q.where(Trigger.trigger_type == trigger_type)

            count_q = select(func.count()).select_from(Trigger).where(base_filter, Trigger.deleted_at.is_(None))
            if pipeline_id is not None:
                count_q = count_q.where(Trigger.pipeline_id == pipeline_id)
            if trigger_type is not None:
                count_q = count_q.where(Trigger.trigger_type == trigger_type)
            total_raw = (await session.execute(count_q)).scalar_one()
            total = int(total_raw) if total_raw is not None else 0
            offset = (page - 1) * page_size
            q = q.order_by(Trigger.created_at.desc()).offset(offset).limit(page_size)
            rows = (await session.execute(q)).scalars().all()

            # Org-wide trigger pause state — the SAME predicate the create_run
            # gate applies (org_row_is_paused: triggers_paused column OR a
            # non-active org status). A suspended/deleted org therefore shows the
            # paused banner and the toggle state matches the server truth. Fresh
            # column-level read (never the ORM identity map).
            org_state = (
                await session.execute(
                    select(
                        Organisation.triggers_paused,
                        Organisation.triggers_paused_at,
                        Organisation.status,
                    ).where(Organisation.id == principal.organisation_id)
                )
            ).one_or_none()
            if org_state is None:
                org_triggers_paused = False
                org_paused_at = None
            else:
                triggers_paused_col, paused_at, status = org_state
                org_triggers_paused = org_row_is_paused(status, triggers_paused_col)
                org_paused_at = paused_at.isoformat() if paused_at else None
    except ProgrammingError:
        _log.exception("triggers.list_triggers")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("triggers.list_triggers")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("list_triggers failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None

    return {
        "items": [
            {
                "id": str(r.id),
                "pipeline_id": str(r.pipeline_id),
                "trigger_type": r.trigger_type,
                "active": r.active,
                "max_concurrent_runs": r.max_concurrent_runs,
                "daily_spend_limit": _serialize_spend_limit(r.daily_spend_limit),
                "config_json": mask_config_json(r.config_json),
                "cron_expression": r.cron_expression,
                "cron_timezone": r.cron_timezone,
                "last_fired_at": r.last_fired_at.isoformat() if r.last_fired_at else None,
                "next_fire_at": r.next_fire_at.isoformat() if r.next_fire_at else None,
                "created_by": str(r.account_id),
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "triggers_paused": org_triggers_paused,
        "paused_at": org_paused_at,
    }


class CronConfigUpdate(BaseModel):
    """Request body for PATCH /triggers/{id}/cron."""

    cron_expression: str | None = None
    cron_timezone: str | None = None
    active: bool | None = None
    snapshot_id: str | None = None
    input_template: dict[str, Any] | None = None


def _validated_next_fire(cron_expression: str | None, cron_timezone: str | None) -> datetime.datetime:
    """Validate a complete cron configuration and return its next UTC fire time."""
    if cron_expression is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Cron expression is required",
        )
    timezone = cron_timezone or "UTC"
    error = validate_cron_expression(cron_expression, timezone)
    if error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid cron expression: {error}",
        )
    return compute_next_fire(cron_expression, timezone=timezone)


@router.patch(
    "/triggers/{trigger_id}/cron",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(deny_break_glass_mint)],
)
@handle_db_errors("triggers.update_cron_config")
async def update_cron_config(
    trigger_id: uuid.UUID,
    req: CronConfigUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("trigger.update"),
) -> dict[str, Any]:
    """Update cron configuration for a trigger.

    Validates the cron expression before saving. Computes ``next_fire_at``
    when the expression or timezone changes.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            result = await session.execute(
                select(Trigger).where(
                    Trigger.id == trigger_id,
                    Trigger.organisation_id == principal.organisation_id,
                    Trigger.deleted_at.is_(None),
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

            next_fire_at: datetime.datetime | None = None
            if req.cron_expression is not None or req.cron_timezone is not None:
                next_fire_at = _validated_next_fire(
                    req.cron_expression if req.cron_expression is not None else trigger.cron_expression,
                    req.cron_timezone if req.cron_timezone is not None else trigger.cron_timezone,
                )

            if req.active is not None:
                trigger.active = req.active
            if req.cron_expression is not None:
                trigger.cron_expression = req.cron_expression
            if req.cron_timezone is not None:
                trigger.cron_timezone = req.cron_timezone
            if next_fire_at is not None:
                trigger.next_fire_at = next_fire_at

            if req.snapshot_id is not None:
                trigger.config_json = {**(trigger.config_json or {}), "snapshot_id": req.snapshot_id}

            if req.input_template is not None:
                trigger.config_json = {**(trigger.config_json or {}), "input_template": req.input_template}

            await session.flush()
    except ProgrammingError:
        _log.exception("triggers.update_cron_config")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("triggers.update_cron_config")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("update_cron_config failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None

    return {
        "id": str(trigger.id),
        "cron_expression": trigger.cron_expression,
        "cron_timezone": trigger.cron_timezone,
        "active": trigger.active,
        "next_fire_at": trigger.next_fire_at.isoformat() if trigger.next_fire_at else None,
        "input_template": trigger.config_json.get("input_template") if trigger.config_json else None,
    }


@router.get("/triggers/{trigger_id}/cron/preview", status_code=status.HTTP_200_OK)
@handle_db_errors("triggers.preview_cron_schedule")
async def preview_cron_schedule(
    trigger_id: uuid.UUID,
    count: int = Query(5, ge=1, le=50),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("trigger.list"),
) -> dict[str, Any]:
    """Preview the next *count* fire times for a cron trigger."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            result = await session.execute(
                select(Trigger).where(
                    Trigger.id == trigger_id,
                    Trigger.organisation_id == principal.organisation_id,
                    Trigger.deleted_at.is_(None),
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

            times: list[str] = []
            next_fire = datetime.datetime.now(datetime.UTC)
            for _ in range(count):
                next_fire = compute_next_fire(
                    trigger.cron_expression,
                    after=next_fire,
                    timezone=trigger.cron_timezone or "UTC",
                )
                times.append(next_fire.isoformat())
    except ProgrammingError:
        _log.exception("triggers.preview_cron_schedule")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("triggers.preview_cron_schedule")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("preview_cron_schedule failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None

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
    poll_interval_seconds: int | None = Field(None, ge=60)
    snapshot_id: str | None = None
    daily_spend_limit: Decimal | None = Field(
        None, ge=0, description="Daily spend ceiling in USD; null clears, None unchanged"
    )


@router.patch(
    "/triggers/{trigger_id}/polling", status_code=status.HTTP_200_OK, dependencies=[Depends(deny_break_glass_mint)]
)
@handle_db_errors("triggers.update_polling_config")
async def update_polling_config(
    trigger_id: uuid.UUID,
    req: PollingConfigUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("trigger.update"),
) -> dict[str, Any]:
    """Update polling configuration for a trigger.

    Validates that the trigger is of type ``polling`` before applying changes.
    Recomputes ``next_fire_at`` when the interval or config changes.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            result = await session.execute(
                select(Trigger).where(
                    Trigger.id == trigger_id,
                    Trigger.organisation_id == principal.organisation_id,
                    Trigger.deleted_at.is_(None),
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

            if req.active is not None:
                trigger.active = req.active
            if "daily_spend_limit" in req.model_fields_set:
                trigger.daily_spend_limit = req.daily_spend_limit

            config = dict(trigger.config_json or {})

            if req.connector_instance_id is not None:
                config["connector_instance_id"] = req.connector_instance_id
            if req.poll_query is not None:
                config["poll_query"] = req.poll_query
            if req.condition_expression is not None:
                config["condition_expression"] = req.condition_expression
            if req.poll_interval_seconds is not None:
                config["poll_interval_seconds"] = req.poll_interval_seconds
            if req.snapshot_id is not None:
                config["snapshot_id"] = req.snapshot_id

            trigger.config_json = config

            # Recompute next_fire_at when interval or config changes
            if any(
                x is not None
                for x in [
                    req.poll_interval_seconds,
                    req.connector_instance_id,
                    req.poll_query,
                ]
            ):
                trigger_engine = TriggerEngine()
                await trigger_engine.schedule_polling_trigger(
                    session, trigger=trigger, org_id=principal.organisation_id
                )

            await session.flush()
    except ProgrammingError:
        _log.exception("triggers.update_polling_config")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("triggers.update_polling_config")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("update_polling_config failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None

    return {
        "id": str(trigger.id),
        "active": trigger.active,
        "daily_spend_limit": _serialize_spend_limit(trigger.daily_spend_limit),
        "config_json": mask_config_json(trigger.config_json),
        "next_fire_at": trigger.next_fire_at.isoformat() if trigger.next_fire_at else None,
    }


class PollingTestRequest(BaseModel):
    """Request body for POST /triggers/{id}/polling/test."""

    connector_instance_id: str
    poll_query: str
    condition_expression: str | None = None


@router.post("/triggers/{trigger_id}/polling/test", status_code=status.HTTP_200_OK)
@handle_db_errors("triggers.test_polling_condition")
async def test_polling_condition(
    trigger_id: uuid.UUID,
    req: PollingTestRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("trigger.update"),
) -> dict[str, Any]:
    """Test a polling trigger's query and condition expression without firing a run.

    Runs the connector query and JMESPath evaluation, returning the result
    status and matching records. Does not create a Run or TriggerEvent.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            result = await session.execute(
                select(Trigger).where(
                    Trigger.id == trigger_id,
                    Trigger.organisation_id == principal.organisation_id,
                    Trigger.deleted_at.is_(None),
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
    except ProgrammingError:
        _log.exception("triggers.test_polling_condition")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("triggers.test_polling_condition")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("test_polling_condition failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None

    # Evaluate outside the transaction (connector ops are I/O, not DB)
    trigger_engine = TriggerEngine()
    return await trigger_engine.evaluate_condition(
        session,
        trigger=trigger,
        org_id=principal.organisation_id,
        connector_instance_id=uuid.UUID(req.connector_instance_id),
        poll_query=req.poll_query,
        condition_expression=req.condition_expression,
    )


# ---------------------------------------------------------------------------
# Trigger CRUD
# ---------------------------------------------------------------------------


class TriggerCreate(BaseModel):
    trigger_type: str = Field(..., pattern=r"^(manual|webhook|cron|polling)$")
    active: bool = True
    max_concurrent_runs: int = Field(default=1, ge=1)
    daily_spend_limit: Decimal | None = Field(None, ge=0, description="Daily spend ceiling in USD; None = unlimited")
    config_json: dict[str, Any] = Field(default_factory=dict)
    cron_expression: str | None = None
    cron_timezone: str | None = None


@router.post(
    "/pipelines/{pipeline_id}/triggers",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(deny_break_glass_mint)],
)
@handle_db_errors("triggers.create_trigger")
async def create_trigger(
    pipeline_id: uuid.UUID,
    req: TriggerCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("trigger.create"),
) -> dict[str, Any]:
    """Create a new trigger for a pipeline."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            next_fire_at = None
            if req.cron_expression is not None or req.cron_timezone is not None:
                if req.trigger_type != "cron":
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Only cron triggers can have cron configuration",
                    )
                next_fire_at = _validated_next_fire(req.cron_expression, req.cron_timezone)
            trigger = Trigger(
                organisation_id=principal.organisation_id,
                pipeline_id=pipeline_id,
                trigger_type=req.trigger_type,
                active=req.active,
                max_concurrent_runs=req.max_concurrent_runs,
                daily_spend_limit=req.daily_spend_limit,
                config_json=req.config_json,
                cron_expression=req.cron_expression,
                cron_timezone=req.cron_timezone,
                account_id=principal.account_id,
                next_fire_at=next_fire_at,
            )
            session.add(trigger)
            await session.flush()
    except ProgrammingError:
        _log.exception("triggers.create_trigger")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("triggers.create_trigger")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("create_trigger failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None

    return {
        "id": str(trigger.id),
        "pipeline_id": str(trigger.pipeline_id),
        "trigger_type": trigger.trigger_type,
        "active": trigger.active,
        "max_concurrent_runs": trigger.max_concurrent_runs,
        "daily_spend_limit": _serialize_spend_limit(trigger.daily_spend_limit),
        "config_json": mask_config_json(trigger.config_json),
        "cron_expression": trigger.cron_expression,
        "cron_timezone": trigger.cron_timezone,
        "last_fired_at": trigger.last_fired_at.isoformat() if trigger.last_fired_at else None,
        "next_fire_at": trigger.next_fire_at.isoformat() if trigger.next_fire_at else None,
        "input_template": trigger.config_json.get("input_template") if trigger.config_json else None,
    }


class TriggerUpdate(BaseModel):
    active: bool | None = None
    max_concurrent_runs: int | None = Field(None, ge=1)
    daily_spend_limit: Decimal | None = Field(
        None, ge=0, description="Daily spend ceiling in USD; null clears, None unchanged"
    )
    config_json: dict[str, Any] | None = None
    cron_expression: str | None = None
    cron_timezone: str | None = None


@router.put("/triggers/{trigger_id}", status_code=status.HTTP_200_OK, dependencies=[Depends(deny_break_glass_mint)])
@handle_db_errors("triggers.update_trigger")
async def update_trigger(
    trigger_id: uuid.UUID,
    req: TriggerUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("trigger.update"),
) -> dict[str, Any]:
    """Update a trigger's general configuration."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            result = await session.execute(
                select(Trigger).where(
                    Trigger.id == trigger_id,
                    Trigger.organisation_id == principal.organisation_id,
                    Trigger.deleted_at.is_(None),
                )
            )
            trigger = result.scalar_one_or_none()
            if trigger is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trigger not found")

            if (req.cron_expression is not None or req.cron_timezone is not None) and trigger.trigger_type != "cron":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Only cron triggers can have cron configuration",
                )

            next_fire_at: datetime.datetime | None = None
            if req.cron_expression is not None or req.cron_timezone is not None:
                next_fire_at = _validated_next_fire(
                    req.cron_expression if req.cron_expression is not None else trigger.cron_expression,
                    req.cron_timezone if req.cron_timezone is not None else trigger.cron_timezone,
                )

            if req.active is not None:
                trigger.active = req.active
            if req.max_concurrent_runs is not None:
                trigger.max_concurrent_runs = req.max_concurrent_runs
            if "daily_spend_limit" in req.model_fields_set:
                trigger.daily_spend_limit = req.daily_spend_limit
            if req.config_json is not None:
                trigger.config_json = req.config_json
            if req.cron_expression is not None:
                trigger.cron_expression = req.cron_expression
            if req.cron_timezone is not None:
                trigger.cron_timezone = req.cron_timezone
            if next_fire_at is not None:
                trigger.next_fire_at = next_fire_at

            await session.flush()
    except ProgrammingError:
        _log.exception("triggers.update_trigger")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("triggers.update_trigger")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("update_trigger failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None

    return {
        "id": str(trigger.id),
        "pipeline_id": str(trigger.pipeline_id),
        "trigger_type": trigger.trigger_type,
        "active": trigger.active,
        "max_concurrent_runs": trigger.max_concurrent_runs,
        "daily_spend_limit": _serialize_spend_limit(trigger.daily_spend_limit),
        "config_json": mask_config_json(trigger.config_json),
        "cron_expression": trigger.cron_expression,
        "cron_timezone": trigger.cron_timezone,
        "last_fired_at": trigger.last_fired_at.isoformat() if trigger.last_fired_at else None,
        "next_fire_at": trigger.next_fire_at.isoformat() if trigger.next_fire_at else None,
    }


@router.delete(
    "/triggers/{trigger_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(deny_break_glass_mint)],
)
@handle_db_errors("triggers.delete_trigger")
async def delete_trigger(
    trigger_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("trigger.delete"),
) -> None:
    """Soft-delete a trigger."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            from modulo.db.crud.trigger import soft_delete_trigger

            deleted = await soft_delete_trigger(session, trigger_id)
            if deleted is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trigger not found")
    except ProgrammingError:
        _log.exception("triggers.delete_trigger")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("triggers.delete_trigger")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("delete_trigger failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None


@router.post(
    "/triggers/{trigger_id}/restore",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(deny_break_glass_mint)],
)
@handle_db_errors("triggers.restore_trigger")
async def restore_trigger(
    trigger_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("trigger.update"),
) -> dict[str, Any]:
    """Restore a soft-deleted trigger."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            from modulo.db.crud.trigger import restore_trigger as _restore_trigger

            trigger = await _restore_trigger(session, trigger_id)
            if trigger is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trigger not found")
    except ProgrammingError:
        _log.exception("triggers.restore_trigger")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("triggers.restore_trigger")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("restore_trigger failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None

    return {
        "id": str(trigger.id),
        "pipeline_id": str(trigger.pipeline_id),
        "trigger_type": trigger.trigger_type,
        "active": trigger.active,
        "max_concurrent_runs": trigger.max_concurrent_runs,
        "daily_spend_limit": _serialize_spend_limit(trigger.daily_spend_limit),
        "config_json": mask_config_json(trigger.config_json),
        "cron_expression": trigger.cron_expression,
        "cron_timezone": trigger.cron_timezone,
        "last_fired_at": trigger.last_fired_at.isoformat() if trigger.last_fired_at else None,
        "next_fire_at": trigger.next_fire_at.isoformat() if trigger.next_fire_at else None,
    }


@router.post(
    "/triggers/{trigger_id}/toggle",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(deny_break_glass_mint)],
)
@handle_db_errors("triggers.toggle_trigger")
async def toggle_trigger(
    trigger_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("trigger.update"),
) -> dict[str, Any]:
    """Toggle a trigger's active state."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            result = await session.execute(
                select(Trigger).where(
                    Trigger.id == trigger_id,
                    Trigger.organisation_id == principal.organisation_id,
                    Trigger.deleted_at.is_(None),
                )
            )
            trigger = result.scalar_one_or_none()
            if trigger is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trigger not found")

            trigger.active = not trigger.active
            await session.flush()
    except ProgrammingError:
        _log.exception("triggers.toggle_trigger")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("triggers.toggle_trigger")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("toggle_trigger failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None

    return {"id": str(trigger.id), "active": trigger.active}


class TestTriggerRequest(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


@router.post("/triggers/{trigger_id}/test", status_code=status.HTTP_200_OK)
@handle_db_errors("triggers.test_trigger")
async def test_trigger(
    trigger_id: uuid.UUID,
    req: TestTriggerRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("trigger.update"),
) -> dict[str, Any]:
    """Fire a test event for a trigger.

    For manual triggers this also creates a Run. For all trigger types
    a TriggerEvent is recorded.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            result = await session.execute(
                select(Trigger).where(
                    Trigger.id == trigger_id,
                    Trigger.organisation_id == principal.organisation_id,
                    Trigger.deleted_at.is_(None),
                )
            )
            trigger = result.scalar_one_or_none()
            if trigger is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trigger not found")

            raw_body = json.dumps(req.payload, sort_keys=True).encode()
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
                    session, pipeline_id=trigger.pipeline_id, account_id=principal.account_id
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
                    input_payload=req.payload,
                    account_id=principal.account_id,
                    trigger_id=trigger.id,
                )
                run_id = str(run.id)
                event.run_id = run.id

            await session.flush()
    except ProgrammingError:
        _log.exception("triggers.test_trigger")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("triggers.test_trigger")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except OrgDeletedError as exc:
        _log.exception("triggers.test_trigger")
        if exc.deleted:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot create run: organisation {exc.org_id} is deleted",
            ) from None
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cannot create run: organisation {exc.org_id} not found",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("test_trigger failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None

    return {
        "event_id": str(event.id),
        "run_id": run_id,
        "status": "test_event_created",
    }


@router.get("/triggers/{trigger_id}/events", status_code=status.HTTP_200_OK)
@handle_db_errors("triggers.list_trigger_events")
async def list_trigger_events(
    trigger_id: uuid.UUID,
    event_status: str | None = Query(None, alias="status"),
    cursor: str | None = Query(None, description="Cursor: createdAt_eventId"),
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("trigger.events.list"),
) -> dict[str, Any]:
    """List trigger events with cursor-based pagination.

    Supports filtering by status (validation_result). Returns a ``next_cursor``
    value that can be passed as ``cursor`` on the next request.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            trigger_result = await session.execute(
                select(Trigger).where(
                    Trigger.id == trigger_id,
                    Trigger.organisation_id == principal.organisation_id,
                    Trigger.deleted_at.is_(None),
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
                    _log.warning("Malformed cursor ignored: %s", cursor, exc_info=True)

            q = q.order_by(TriggerEvent.created_at.desc(), TriggerEvent.id.desc()).limit(limit + 1)
            rows = (await session.execute(q)).scalars().all()
    except ProgrammingError:
        _log.exception("triggers.list_trigger_events")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("triggers.list_trigger_events")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("list_trigger_events failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None

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
    principal: TenantPrincipal = require_permission("trigger.list"),
) -> dict[str, Any]:
    """List triggers for a specific pipeline."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            q = select(Trigger).where(
                Trigger.pipeline_id == pipeline_id,
                Trigger.organisation_id == principal.organisation_id,
                Trigger.deleted_at.is_(None),
            )
            if trigger_type is not None:
                q = q.where(Trigger.trigger_type == trigger_type)
            q = q.order_by(Trigger.created_at.desc())
            rows = (await session.execute(q)).scalars().all()
    except ProgrammingError:
        _log.exception("triggers.list_pipeline_triggers")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("triggers.list_pipeline_triggers")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database operation failed. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("list_pipeline_triggers failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None

    return {
        "items": [
            {
                "id": str(r.id),
                "pipeline_id": str(r.pipeline_id),
                "trigger_type": r.trigger_type,
                "active": r.active,
                "max_concurrent_runs": r.max_concurrent_runs,
                "daily_spend_limit": _serialize_spend_limit(r.daily_spend_limit),
                "config_json": mask_config_json(r.config_json),
                "cron_expression": r.cron_expression,
                "cron_timezone": r.cron_timezone,
                "last_fired_at": r.last_fired_at.isoformat() if r.last_fired_at else None,
                "next_fire_at": r.next_fire_at.isoformat() if r.next_fire_at else None,
                "created_by": str(r.account_id),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }
