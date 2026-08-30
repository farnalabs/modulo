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

from modulo.api.constants import MSG_DB_OPERATION_FAILED, MSG_FEATURE_NOT_AVAILABLE, MSG_INTERNAL_SERVER_ERROR
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import deny_break_glass_mint, get_db_session, require_permission
from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK, mask_config_json
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.secret_storage import _is_encrypted_token, encrypt_stored_secret
from modulo.core.cron_helpers import (
    _count_ongoing_runs,
    compute_next_fire,
    validate_cron_expression,
)
from modulo.core.exceptions import OrgDeletedError
from modulo.core.trigger_engine import TriggerEngine
from modulo.core.trigger_streak import (
    _streak_config,
    anchor_trigger_streak_epoch,
    clear_trigger_streak_after_reenable,
    get_trigger_streak_status,
)
from modulo.core.trigger_validation import validate_ongoing_config
from modulo.db.capacity import StorageExhaustedError
from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph
from modulo.db.crud.run import create_run
from modulo.db.crud.trigger import apply_trigger_event_cursor
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.trigger import Trigger
from modulo.db.models.trigger_event import TriggerEvent
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.db.settings_resolver import org_row_is_paused
from modulo.settings import Settings, get_settings

_CODE_TRIGGER_LIST = "trigger.list"
_CODE_TRIGGER_UPDATE = "trigger.update"
_MSG_TRIGGER_NOT_FOUND = "Trigger not found"
_MSG_ONLY_CRON_TRIGGERS_CAN = "Only cron triggers can have cron configuration"
_CODE_TRIGGERS_TEST_TRIGGER = "triggers.test_trigger"
_MAX_PREVIEW_COUNT = 50


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


async def _ongoing_in_flight(session: AsyncSession, trigger: Trigger) -> int:
    """Fresh in-flight count for an ``ongoing`` trigger (0 for other types).

    FAR-158: the trigger detail/list responses carry ``in_flight`` for ongoing
    triggers so the UI can show the current pool size. Cheap on the
    migration-0086 ``(trigger_id, status)`` index. Runs inside the request's
    transaction where the RLS context is already set.
    """
    if trigger.trigger_type != "ongoing":
        return 0
    return await _count_ongoing_runs(session, trigger.id)


async def _streak_status_for(session: AsyncSession, trigger: Trigger) -> dict[str, Any]:
    """FAR-191 — ``streak_status`` for a trigger serializer.

    Returns the UNIFORM 6-key shape for every trigger (FIX 5): always delegates
    to ``get_trigger_streak_status``, whose base handles non-ongoing triggers
    (``{enabled: false, streak: 0, threshold: 0, state: 'unconfigured',
    deactivated_reason: null, last_outcomes: []}``) without issuing any query.
    The threshold is resolved here and passed in so the reader is
    self-contained. Best-effort — ``get_trigger_streak_status`` never raises,
    so a read failure degrades to the unconfigured base instead of 500ing the
    list.
    """
    threshold, _ = _streak_config(trigger.config_json)
    return await get_trigger_streak_status(session, trigger, config_threshold=threshold)


def _trigger_type_and_pipeline_filters(
    base_filter: Any,
    pipeline_id: uuid.UUID | None,
    trigger_type: str | None,
) -> list[Any]:
    """Build the shared list/count WHERE conditions for the optional filters."""
    filters: list[Any] = [base_filter]
    if pipeline_id is not None:
        filters.append(Trigger.pipeline_id == pipeline_id)
    if trigger_type is not None:
        filters.append(Trigger.trigger_type == trigger_type)
    return filters


async def _read_org_pause_state(session: AsyncSession, organisation_id: uuid.UUID) -> tuple[bool, str | None]:
    """Read the org-wide trigger pause state with the SAME predicate create_run uses."""
    org_state = (
        await session.execute(
            select(
                Organisation.triggers_paused,
                Organisation.triggers_paused_at,
                Organisation.status,
            ).where(Organisation.id == organisation_id)
        )
    ).one_or_none()
    if org_state is None:
        return False, None
    triggers_paused_col, paused_at, status = org_state
    org_triggers_paused = org_row_is_paused(status, triggers_paused_col)
    org_paused_at = paused_at.isoformat() if paused_at else None
    return org_triggers_paused, org_paused_at


def _trigger_to_dict(trigger: Trigger, *, in_flight: int, streak_status: dict[str, Any]) -> dict[str, Any]:
    """Build the shared trigger API dict from precomputed ``in_flight``/``streak_status``.

    Single source of truth for the trigger shape — both ``_serialize_trigger``
    (list paths) and ``_serialize_trigger_detail`` (create/update/restore
    responses) delegate here so the field set cannot drift between them.
    """
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
        "in_flight": in_flight,
        "streak_status": streak_status,
    }


async def _serialize_trigger(session: AsyncSession, trigger: Trigger) -> dict[str, Any]:
    """Serialize one trigger row to the API shape (runs INSIDE the RLS tx)."""
    data = _trigger_to_dict(
        trigger,
        in_flight=await _ongoing_in_flight(session, trigger),
        streak_status=await _streak_status_for(session, trigger),
    )
    data["created_by"] = str(trigger.account_id)
    return data


async def _load_trigger_for_update(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    trigger_id: uuid.UUID,
) -> Trigger:
    """Load a trigger for an update path, 404 when missing/soft-deleted."""
    result = await session.execute(
        select(Trigger).where(
            Trigger.id == trigger_id,
            Trigger.organisation_id == organisation_id,
            Trigger.deleted_at.is_(None),
        )
    )
    trigger = result.scalar_one_or_none()
    if trigger is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trigger not found")
    return trigger


def _merge_trigger_config(current: dict[str, Any] | None, update: dict[str, Any]) -> dict[str, Any]:
    """MERGE config fields — never wholesale replace (drops unmanaged keys).

    A masked placeholder must never clobber the stored secret (read-modify-write
    round-trip guard); an explicit ``None`` clears the key; a missing key leaves
    it intact.
    """
    merged_cfg = dict(current or {})
    for k, v in update.items():
        if isinstance(v, str) and v == SENSITIVE_VALUE_MASK:
            continue
        if v is None:
            merged_cfg.pop(k, None)
        else:
            merged_cfg[k] = v
    return merged_cfg


_SECRET_CONFIG_KEYS = frozenset({"hmac_secret", "signing_secret"})


def _encrypt_trigger_config_secrets(config: dict[str, Any] | None, fernet_key: str) -> dict[str, Any]:
    """Encrypt known secret fields in a trigger config_json before storage.

    Only encrypts values that are plaintext strings (not already encrypted
    bytes/base64 strings or masked placeholders). Existing encrypted values
    are left unchanged so updates are idempotent.
    """
    if not config:
        return {}
    result = dict(config)
    for key in _SECRET_CONFIG_KEYS:
        val = result.get(key)
        if isinstance(val, str) and val and val != SENSITIVE_VALUE_MASK and not _is_encrypted_token(val):
            try:
                result[key] = encrypt_stored_secret(val, fernet_key).decode()
            except Exception:
                _log.exception("trigger_config_encrypt_failed key=%s", key)
    return result


async def _guard_and_resolve_ongoing_changes(
    session: AsyncSession,
    trigger: Trigger,
    req: "TriggerUpdate",
) -> bool:
    """FAR-158 ongoing guards; returns True when the scan interval changed."""
    _guard_ongoing_spend_limit(req)
    if _ongoing_changes_triggered(req):
        await _validate_ongoing_changes(session, trigger, req)
    if req.config_json is not None:
        return _scan_interval_changed(trigger, req.config_json)
    return False


def _guard_ongoing_spend_limit(req: "TriggerUpdate") -> None:
    """Reject updates that would clear the (required) ongoing spend limit."""
    if "daily_spend_limit" in req.model_fields_set and req.daily_spend_limit is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="ongoing triggers require daily_spend_limit; clearing it is not allowed",
        )


def _ongoing_changes_triggered(req: "TriggerUpdate") -> bool:
    """True when any pool-affecting field is being changed."""
    return any(x is not None for x in [req.max_concurrent_runs, req.config_json, req.active]) or (
        "daily_spend_limit" in req.model_fields_set
    )


async def _validate_ongoing_changes(session: AsyncSession, trigger: Trigger, req: "TriggerUpdate") -> None:
    """Re-run the shared ongoing validator against the MERGED post-update values."""
    pipeline = await session.get(Pipeline, trigger.pipeline_id)
    pipeline_cap = pipeline.max_concurrent_runs if pipeline is not None else 0
    validate_ongoing_config(
        trigger.trigger_type,
        max_concurrent_runs=(
            req.max_concurrent_runs if req.max_concurrent_runs is not None else trigger.max_concurrent_runs
        ),
        daily_spend_limit=(
            req.daily_spend_limit if "daily_spend_limit" in req.model_fields_set else trigger.daily_spend_limit
        ),
        config_json=(req.config_json if req.config_json is not None else trigger.config_json),
        pipeline_max_concurrent_runs=pipeline_cap,
    )


def _scan_interval_changed(trigger: Trigger, new_config: dict[str, Any]) -> bool:
    """True when the new config's scan interval differs from the stored one."""
    old_scan = int((trigger.config_json or {}).get("scan_interval_seconds") or 60)
    new_scan = int(new_config.get("scan_interval_seconds") or 60)
    return new_scan != old_scan


def _require_trigger_type(trigger: Trigger, expected: str, detail: str) -> None:
    """Raise 400 unless the trigger's type matches ``expected``."""
    if trigger.trigger_type != expected:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


async def _apply_active_state(session: AsyncSession, trigger: Trigger, new_active: bool | None) -> bool:
    """Apply an ``active`` transition; returns the trigger's PREVIOUS ``active`` value.

    FAR-190: any ``active=True`` transition re-anchors the no-delivery streak
    epoch so pre-existing history can never count. Returns ``False`` when the
    field was not supplied so callers can skip the post-commit counter clear.
    """
    if new_active is None:
        return False
    prev_active = trigger.active
    trigger.active = new_active
    if trigger.active and not prev_active:
        await anchor_trigger_streak_epoch(session, trigger_id=trigger.id)
    return prev_active


def _merge_if_set(config: dict[str, Any], key: str, value: Any) -> None:
    """Set ``config[key]`` from ``value`` when it is not ``None`` (merge semantics)."""
    if value is not None:
        config[key] = value


def _bump_ongoing_next_fire(trigger: Trigger, changed: bool) -> None:
    """Push an ongoing trigger's ``next_fire_at`` to now when its pool/cadence changed.

    The scheduler tick selects ``next_fire_at IS NULL OR due``, so the reset
    makes a freshly configured ongoing trigger fire promptly.
    """
    if trigger.trigger_type == "ongoing" and changed:
        trigger.next_fire_at = datetime.datetime.now(datetime.UTC)


def _serialize_trigger_detail(trigger: Trigger, *, in_flight: int, streak_status: dict[str, Any]) -> dict[str, Any]:
    """The full single-trigger API shape shared by the create/update/restore responses."""
    return _trigger_to_dict(trigger, in_flight=in_flight, streak_status=streak_status)


def _resolve_cron_next_fire(
    trigger_type: str,
    cron_expression: str | None,
    cron_timezone: str | None,
) -> datetime.datetime | None:
    """Validate cron fields when supplied; returns the next UTC fire time or ``None``.

    Raises 400 when cron fields are supplied for a non-cron trigger.
    """
    if cron_expression is None and cron_timezone is None:
        return None
    if trigger_type != "cron":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_MSG_ONLY_CRON_TRIGGERS_CAN)
    return _validated_next_fire(cron_expression, cron_timezone)


@router.get("/triggers", status_code=status.HTTP_200_OK)
@handle_db_errors("triggers.list_triggers")
async def list_triggers(
    pipeline_id: uuid.UUID | None = Query(None),
    trigger_type: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_TRIGGER_LIST),
) -> dict[str, Any]:
    """List all triggers, optionally filtered by pipeline or type."""
    items: list[dict[str, Any]] = []
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            base_filter = Trigger.organisation_id == principal.organisation_id
            trigger_filter = _trigger_type_and_pipeline_filters(base_filter, pipeline_id, trigger_type)

            q = select(Trigger).where(*trigger_filter, Trigger.deleted_at.is_(None))
            count_q = select(func.count()).select_from(Trigger).where(*trigger_filter, Trigger.deleted_at.is_(None))
            total_raw = (await session.execute(count_q)).scalar_one()
            total = int(total_raw) if total_raw is not None else 0
            offset = (page - 1) * page_size
            q = q.order_by(Trigger.created_at.desc()).offset(offset).limit(page_size)
            rows = (await session.execute(q)).scalars().all()

            org_triggers_paused, org_paused_at = await _read_org_pause_state(session, principal.organisation_id)

            # Serialize the items INSIDE the RLS transaction (FIX 2): the
            # in-flight + streak-status reads rely on the ``SET LOCAL
            # app.organisation_id`` context, which is transaction-scoped — a read
            # after commit sees zero rows on strict-RLS Postgres, silently
            # showing a deactivated trigger as state 'ok'.
            items.extend([await _serialize_trigger(session, r) for r in rows])
    except ProgrammingError:
        _log.exception("triggers.list_triggers")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("triggers.list_triggers")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("list_triggers failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    return {
        "items": items,
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


async def _apply_cron_update(session: AsyncSession, trigger: Trigger, req: "CronConfigUpdate") -> bool:
    """Mutate a cron trigger per the update; returns the PREVIOUS ``active`` value."""
    _require_trigger_type(trigger, "cron", _MSG_ONLY_CRON_TRIGGERS_CAN)

    if req.cron_expression is not None or req.cron_timezone is not None:
        trigger.next_fire_at = _validated_next_fire(
            req.cron_expression if req.cron_expression is not None else trigger.cron_expression,
            req.cron_timezone if req.cron_timezone is not None else trigger.cron_timezone,
        )

    prev_active = await _apply_active_state(session, trigger, req.active)
    if req.cron_expression is not None:
        trigger.cron_expression = req.cron_expression
    if req.cron_timezone is not None:
        trigger.cron_timezone = req.cron_timezone

    if req.snapshot_id is not None:
        trigger.config_json = {**(trigger.config_json or {}), "snapshot_id": req.snapshot_id}

    if req.input_template is not None:
        trigger.config_json = {**(trigger.config_json or {}), "input_template": req.input_template}

    return prev_active


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
    principal: TenantPrincipal = require_permission(_CODE_TRIGGER_UPDATE),
) -> dict[str, Any]:
    """Update cron configuration for a trigger.

    Validates the cron expression before saving. Computes ``next_fire_at``
    when the expression or timezone changes.
    """
    prev_active = False
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            trigger = await _load_trigger_for_update(session, principal.organisation_id, trigger_id)
            prev_active = await _apply_cron_update(session, trigger, req)
            await session.flush()
    except ProgrammingError:
        _log.exception("triggers.update_cron_config")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("triggers.update_cron_config")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("update_cron_config failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    # FAR-190: clear the config-failure Redis counter only AFTER the commit
    # (over-clearing safe, under-clearing not); best-effort.
    if req.active is True and not prev_active:
        await clear_trigger_streak_after_reenable(trigger.id)

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
    principal: TenantPrincipal = require_permission(_CODE_TRIGGER_LIST),
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
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_TRIGGER_NOT_FOUND)

            if not trigger.cron_expression:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Trigger has no cron expression configured",
                )

            times: list[str] = []
            next_fire = datetime.datetime.now(datetime.UTC)
            preview_count = max(1, min(count, _MAX_PREVIEW_COUNT))
            for _ in range(preview_count):
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
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("triggers.preview_cron_schedule")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("preview_cron_schedule failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
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
    principal: TenantPrincipal = require_permission(_CODE_TRIGGER_UPDATE),
) -> dict[str, Any]:
    """Update polling configuration for a trigger.

    Validates that the trigger is of type ``polling`` before applying changes.
    Recomputes ``next_fire_at`` when the interval or config changes.
    """
    prev_active = False
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            trigger = await _load_trigger_for_update(session, principal.organisation_id, trigger_id)

            _require_trigger_type(trigger, "polling", "Only polling triggers can have polling configuration")

            prev_active = await _apply_active_state(session, trigger, req.active)
            if "daily_spend_limit" in req.model_fields_set:
                trigger.daily_spend_limit = req.daily_spend_limit

            config = dict(trigger.config_json or {})

            _merge_if_set(config, "connector_instance_id", req.connector_instance_id)
            _merge_if_set(config, "poll_query", req.poll_query)
            _merge_if_set(config, "condition_expression", req.condition_expression)
            _merge_if_set(config, "poll_interval_seconds", req.poll_interval_seconds)
            _merge_if_set(config, "snapshot_id", req.snapshot_id)

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
                    session, trigger=trigger, _org_id=principal.organisation_id
                )

            await session.flush()
    except ProgrammingError:
        _log.exception("triggers.update_polling_config")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("triggers.update_polling_config")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("update_polling_config failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    # FAR-190: clear the config-failure Redis counter only AFTER the commit.
    if req.active is True and not prev_active:
        await clear_trigger_streak_after_reenable(trigger.id)

    return {
        "id": str(trigger.id),
        "active": trigger.active,
        "daily_spend_limit": _serialize_spend_limit(trigger.daily_spend_limit),
        "config_json": mask_config_json(trigger.config_json),
        "next_fire_at": trigger.next_fire_at.isoformat() if trigger.next_fire_at else None,
    }


class OngoingConfigUpdate(BaseModel):
    """Request body for PATCH /triggers/{id}/ongoing (FAR-158)."""

    active: bool | None = None
    scan_interval_seconds: int | None = Field(None, ge=60)
    input_template: dict[str, Any] | None = None
    snapshot_id: str | None = None
    target_runs: int | None = Field(None, ge=1, le=20, description="Ongoing pool target (max_concurrent_runs)")


@handle_db_errors("triggers.update_ongoing_config")
@router.patch(
    "/triggers/{trigger_id}/ongoing",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(deny_break_glass_mint)],
)
async def update_ongoing_config(
    trigger_id: uuid.UUID,
    req: OngoingConfigUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_TRIGGER_UPDATE),
) -> dict[str, Any]:
    """Update the ongoing configuration for an ``ongoing`` trigger.

    Mirrors ``update_polling_config``. Only ``ongoing`` triggers accept this
    config surface. Sets the ``config_json`` keys ``scan_interval_seconds`` /
    ``input_template`` / ``snapshot_id`` (merging, never wholesale replacing),
    updates ``target_runs`` -> ``max_concurrent_runs``, and recomputes
    ``next_fire_at = now`` when the scan cadence changes or the trigger is
    turned on so the new configuration is picked up on the next tick.
    """
    prev_active = False
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            trigger = await _load_trigger_for_update(session, principal.organisation_id, trigger_id)

            _require_trigger_type(trigger, "ongoing", "Only ongoing triggers can have ongoing configuration")

            config = dict(trigger.config_json or {})
            old_scan = int(config.get("scan_interval_seconds") or 60)
            scan_changed = False
            if req.scan_interval_seconds is not None:
                config["scan_interval_seconds"] = req.scan_interval_seconds
                scan_changed = req.scan_interval_seconds != old_scan
            _merge_if_set(config, "input_template", req.input_template)
            _merge_if_set(config, "snapshot_id", req.snapshot_id)
            trigger.config_json = config

            prev_max = trigger.max_concurrent_runs
            if req.target_runs is not None:
                trigger.max_concurrent_runs = req.target_runs
            prev_active = await _apply_active_state(session, trigger, req.active)

            activated = req.active is not None and trigger.active
            target_changed = req.target_runs is not None and req.target_runs != prev_max
            _bump_ongoing_next_fire(trigger, scan_changed or activated or target_changed)

            await session.flush()
            updated_in_flight = await _ongoing_in_flight(session, trigger)
            ongoing_streak_status = await _streak_status_for(session, trigger)
    except ProgrammingError:
        _log.exception("triggers.update_ongoing_config")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("triggers.update_ongoing_config")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("update_ongoing_config failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    # FAR-190: clear the config-failure Redis counter only AFTER the commit.
    if req.active is True and not prev_active:
        await clear_trigger_streak_after_reenable(trigger.id)

    return {
        "id": str(trigger.id),
        "active": trigger.active,
        "max_concurrent_runs": trigger.max_concurrent_runs,
        "daily_spend_limit": _serialize_spend_limit(trigger.daily_spend_limit),
        "config_json": mask_config_json(trigger.config_json),
        "next_fire_at": trigger.next_fire_at.isoformat() if trigger.next_fire_at else None,
        "in_flight": updated_in_flight,
        "streak_status": ongoing_streak_status,
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
    principal: TenantPrincipal = require_permission(_CODE_TRIGGER_UPDATE),
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
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_TRIGGER_NOT_FOUND)

            _require_trigger_type(trigger, "polling", "Only polling triggers can be tested")
    except ProgrammingError:
        _log.exception("triggers.test_polling_condition")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("triggers.test_polling_condition")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("test_polling_condition failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    # Evaluate outside the transaction (connector ops are I/O, not DB)
    trigger_engine = TriggerEngine()
    return await trigger_engine.evaluate_condition(
        session,
        _trigger=trigger,
        org_id=principal.organisation_id,
        connector_instance_id=uuid.UUID(req.connector_instance_id),
        poll_query=req.poll_query,
        condition_expression=req.condition_expression,
    )


# ---------------------------------------------------------------------------
# Trigger CRUD
# ---------------------------------------------------------------------------


class TriggerCreate(BaseModel):
    trigger_type: str = Field(..., pattern=r"^(manual|webhook|cron|polling|agent_signal|ongoing|slack_app_mention)$")
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
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Create a new trigger for a pipeline."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            next_fire_at = _resolve_cron_next_fire(req.trigger_type, req.cron_expression, req.cron_timezone)
            if req.trigger_type == "ongoing":
                # FAR-158 ongoing guard: validated BEFORE creating (the shared
                # validator also loads the pipeline cap for the target check).
                pipeline = await session.get(Pipeline, pipeline_id)
                pipeline_cap = pipeline.max_concurrent_runs if pipeline is not None else 0
                validate_ongoing_config(
                    req.trigger_type,
                    max_concurrent_runs=req.max_concurrent_runs,
                    daily_spend_limit=req.daily_spend_limit,
                    config_json=req.config_json,
                    pipeline_max_concurrent_runs=pipeline_cap,
                )
                # A fresh ongoing trigger must fire on the first scheduler tick
                # (the scan selects next_fire_at IS NULL OR due).
                next_fire_at = datetime.datetime.now(datetime.UTC)
            encrypted_config = _encrypt_trigger_config_secrets(req.config_json, settings.fernet_key)
            trigger = Trigger(
                organisation_id=principal.organisation_id,
                pipeline_id=pipeline_id,
                trigger_type=req.trigger_type,
                active=req.active,
                max_concurrent_runs=req.max_concurrent_runs,
                daily_spend_limit=req.daily_spend_limit,
                config_json=encrypted_config,
                cron_expression=req.cron_expression,
                cron_timezone=req.cron_timezone,
                account_id=principal.account_id,
                next_fire_at=next_fire_at,
                # FAR-190: creation anchors the no-delivery streak epoch (the
                # streak boundary) so pre-existing history can never count.
                streak_epoch=datetime.datetime.now(datetime.UTC),
            )
            session.add(trigger)
            await session.flush()
            # Computed INSIDE the transaction (RLS context set) — after commit
            # the SET LOCAL is gone and the count could not be scoped.
            created_in_flight = await _ongoing_in_flight(session, trigger)
            created_streak_status = await _streak_status_for(session, trigger)
    except ProgrammingError:
        _log.exception("triggers.create_trigger")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("triggers.create_trigger")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("create_trigger failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    response = _serialize_trigger_detail(trigger, in_flight=created_in_flight, streak_status=created_streak_status)
    response["input_template"] = trigger.config_json.get("input_template") if trigger.config_json else None
    return response


class TriggerUpdate(BaseModel):
    active: bool | None = None
    max_concurrent_runs: int | None = Field(None, ge=1)
    daily_spend_limit: Decimal | None = Field(
        None, ge=0, description="Daily spend ceiling in USD; null clears, None unchanged"
    )
    config_json: dict[str, Any] | None = None
    cron_expression: str | None = None
    cron_timezone: str | None = None


async def _apply_trigger_update(
    session: AsyncSession,
    settings: Settings,
    trigger: Trigger,
    req: TriggerUpdate,
) -> tuple[bool, bool]:
    """Mutate a trigger per the update; returns (ongoing_scan_interval_changed, prev_active)."""
    cron_changed = req.cron_expression is not None or req.cron_timezone is not None
    if cron_changed:
        _require_trigger_type(trigger, "cron", _MSG_ONLY_CRON_TRIGGERS_CAN)
        trigger.next_fire_at = _validated_next_fire(
            req.cron_expression if req.cron_expression is not None else trigger.cron_expression,
            req.cron_timezone if req.cron_timezone is not None else trigger.cron_timezone,
        )

    # FAR-158 ongoing guards. The ongoing spend limit is REQUIRED — it
    # can never be cleared to None (the DB partial CHECK would also reject the
    # row). When any pool-affecting field changes, re-run the shared validator
    # against the MERGED (post-update) values.
    ongoing_scan_interval_changed = False
    if trigger.trigger_type == "ongoing":
        ongoing_scan_interval_changed = await _guard_and_resolve_ongoing_changes(session, trigger, req)

    # Pre-mutation snapshot for the ongoing next_fire_at reset decision
    # (reset only when the pool/cadence/active actually CHANGES).
    prev_max = trigger.max_concurrent_runs
    prev_active = await _apply_active_state(session, trigger, req.active)
    if req.max_concurrent_runs is not None:
        trigger.max_concurrent_runs = req.max_concurrent_runs
    if "daily_spend_limit" in req.model_fields_set:
        trigger.daily_spend_limit = req.daily_spend_limit
    if req.config_json is not None:
        merged = _merge_trigger_config(trigger.config_json, req.config_json)
        trigger.config_json = _encrypt_trigger_config_secrets(merged, settings.fernet_key)
    if req.cron_expression is not None:
        trigger.cron_expression = req.cron_expression
    if req.cron_timezone is not None:
        trigger.cron_timezone = req.cron_timezone

    # Ongoing triggers recompute next_fire_at when the pool or cadence
    # actually changes (NOT on metadata-only edits) so the new config
    # takes effect promptly.
    target_changed = req.max_concurrent_runs is not None and req.max_concurrent_runs != prev_max
    activated = req.active is not None and trigger.active and not prev_active
    _bump_ongoing_next_fire(trigger, target_changed or ongoing_scan_interval_changed or activated)
    return ongoing_scan_interval_changed, prev_active


@router.put("/triggers/{trigger_id}", status_code=status.HTTP_200_OK, dependencies=[Depends(deny_break_glass_mint)])
@handle_db_errors("triggers.update_trigger")
async def update_trigger(
    trigger_id: uuid.UUID,
    req: TriggerUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_TRIGGER_UPDATE),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Update a trigger's general configuration."""
    prev_active = False
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            trigger = await _load_trigger_for_update(session, principal.organisation_id, trigger_id)

            _ongoing_changed, prev_active = await _apply_trigger_update(session, settings, trigger, req)

            await session.flush()
            updated_in_flight = await _ongoing_in_flight(session, trigger)
            updated_streak_status = await _streak_status_for(session, trigger)
    except ProgrammingError:
        _log.exception("triggers.update_trigger")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("triggers.update_trigger")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("update_trigger failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    # FAR-190: clear the config-failure Redis counter only AFTER the commit.
    if req.active is True and not prev_active:
        await clear_trigger_streak_after_reenable(trigger.id)

    return _serialize_trigger_detail(trigger, in_flight=updated_in_flight, streak_status=updated_streak_status)


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
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_TRIGGER_NOT_FOUND)
    except ProgrammingError:
        _log.exception("triggers.delete_trigger")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("triggers.delete_trigger")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("delete_trigger failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
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
    principal: TenantPrincipal = require_permission(_CODE_TRIGGER_UPDATE),
) -> dict[str, Any]:
    """Restore a soft-deleted trigger."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            from modulo.db.crud.trigger import restore_trigger as _restore_trigger

            trigger = await _restore_trigger(session, trigger_id)
            if trigger is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_TRIGGER_NOT_FOUND)
            # An ongoing trigger restored back into service must fire on the
            # next tick (its next_fire_at was advanced while it was deleted).
            if trigger.trigger_type == "ongoing":
                trigger.next_fire_at = datetime.datetime.now(datetime.UTC)
            # FAR-190: a restored trigger back in service re-anchors its
            # no-delivery streak epoch (no un-epoch'd re-enable path).
            if trigger.active:
                await anchor_trigger_streak_epoch(session, trigger_id=trigger.id)
            restored_in_flight = await _ongoing_in_flight(session, trigger)
            restored_streak_status = await _streak_status_for(session, trigger)
    except ProgrammingError:
        _log.exception("triggers.restore_trigger")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("triggers.restore_trigger")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("restore_trigger failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    # FAR-190: clear the config-failure Redis counter only AFTER the commit.
    if trigger.active:
        await clear_trigger_streak_after_reenable(trigger.id)

    return _serialize_trigger_detail(trigger, in_flight=restored_in_flight, streak_status=restored_streak_status)


@router.post(
    "/triggers/{trigger_id}/toggle",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(deny_break_glass_mint)],
)
@handle_db_errors("triggers.toggle_trigger")
async def toggle_trigger(
    trigger_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_TRIGGER_UPDATE),
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
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_TRIGGER_NOT_FOUND)

            trigger.active = not trigger.active
            # An ongoing trigger being turned back ON must fire on the next
            # tick (its next_fire_at was advanced while it was inactive).
            if trigger.trigger_type == "ongoing" and trigger.active:
                trigger.next_fire_at = datetime.datetime.now(datetime.UTC)
            # FAR-190: re-anchor the no-delivery streak epoch on any active=True
            # transition (no un-epoch'd re-enable path).
            if trigger.active:
                await anchor_trigger_streak_epoch(session, trigger_id=trigger.id)
            await session.flush()
            toggled_streak_status = await _streak_status_for(session, trigger)
    except ProgrammingError:
        _log.exception("triggers.toggle_trigger")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("triggers.toggle_trigger")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("toggle_trigger failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    # FAR-190: clear the config-failure Redis counter only AFTER the commit.
    if trigger.active:
        await clear_trigger_streak_after_reenable(trigger.id)

    return {
        "id": str(trigger.id),
        "active": trigger.active,
        "streak_status": toggled_streak_status,
    }


class TestTriggerRequest(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


async def _record_manual_test_run(
    session: AsyncSession,
    principal: TenantPrincipal,
    trigger: Trigger,
    payload: dict[str, Any],
    event: TriggerEvent,
) -> str | None:
    """Create a snapshot + run for a manual test trigger; returns the run id (or None).

    Raised as part of the ``manual`` dispatch branch in ``test_trigger``.
    """
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
        input_payload=payload,
        account_id=principal.account_id,
        trigger_id=trigger.id,
    )
    event.run_id = run.id
    return str(run.id)


@router.post("/triggers/{trigger_id}/test", status_code=status.HTTP_200_OK)
@handle_db_errors(_CODE_TRIGGERS_TEST_TRIGGER)
async def test_trigger(
    trigger_id: uuid.UUID,
    req: TestTriggerRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_TRIGGER_UPDATE),
) -> dict[str, Any]:
    """Fire a test event for a trigger.

    For manual triggers this also creates a Run. For all trigger types
    a TriggerEvent is recorded.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await session.execute(
                select(Trigger).where(
                    Trigger.id == trigger_id,
                    Trigger.organisation_id == principal.organisation_id,
                    Trigger.deleted_at.is_(None),
                )
            )
            trigger = result.scalar_one_or_none()
            if trigger is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_TRIGGER_NOT_FOUND)

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
                run_id = await _record_manual_test_run(session, principal, trigger, req.payload, event)

            await session.flush()
    except ProgrammingError:
        _log.exception(_CODE_TRIGGERS_TEST_TRIGGER)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_TRIGGERS_TEST_TRIGGER)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except OrgDeletedError as exc:
        _log.exception(_CODE_TRIGGERS_TEST_TRIGGER)
        if exc.deleted:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot create run: organisation {exc.org_id} is deleted",
            ) from None
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cannot create run: organisation {exc.org_id} not found",
        ) from None
    except StorageExhaustedError:
        raise
    except HTTPException:
        raise
    except Exception:
        _log.exception("test_trigger failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
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
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_TRIGGER_NOT_FOUND)

            q = select(TriggerEvent).where(
                TriggerEvent.trigger_id == trigger_id,
                TriggerEvent.organisation_id == principal.organisation_id,
            )
            if event_status is not None:
                q = q.where(TriggerEvent.validation_result == event_status)

            if cursor:
                q = apply_trigger_event_cursor(q, cursor)

            q = q.order_by(TriggerEvent.created_at.desc(), TriggerEvent.id.desc()).limit(limit + 1)
            rows = (await session.execute(q)).scalars().all()
    except ProgrammingError:
        _log.exception("triggers.list_trigger_events")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("triggers.list_trigger_events")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("list_trigger_events failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
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
    principal: TenantPrincipal = require_permission(_CODE_TRIGGER_LIST),
) -> dict[str, Any]:
    """List triggers for a specific pipeline."""
    pipeline_items: list[dict[str, Any]] = []
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

            # Serialize the items INSIDE the RLS transaction (FIX 2, sibling of
            # list_triggers): the in-flight + streak-status reads rely on the
            # ``SET LOCAL app.organisation_id`` context, which is
            # transaction-scoped — a read after commit sees zero rows on
            # strict-RLS Postgres, silently showing a deactivated trigger as
            # state 'ok' with no Re-enable button.
            for r in rows:
                item = await _serialize_trigger(session, r)
                item["created_at"] = r.created_at.isoformat() if r.created_at else None
                pipeline_items.append(item)
    except ProgrammingError:
        _log.exception("triggers.list_pipeline_triggers")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("triggers.list_pipeline_triggers")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("list_pipeline_triggers failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    return {
        "items": pipeline_items,
    }
