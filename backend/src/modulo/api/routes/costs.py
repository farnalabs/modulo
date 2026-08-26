"""Admin cost management routes — spend limits, cost reports, export, anomalies, scheduled reports."""

import asyncio
import csv
import io
import logging
import math
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_FEATURE_NOT_AVAILABLE, MSG_INTERNAL_SERVER_ERROR
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_feature, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.core.cost_controller import build_cost_report_buckets, get_cost_report, reset_pipeline_circuit_breaker
from modulo.core.cost_settings import (
    COST_CONTROLS_KEY,
    DEFAULT_ALERT_THRESHOLDS,
    DEFAULT_BILLING_PERIOD,
    DEFAULT_CIRCUIT_BREAKER_ENABLED,
    DEFAULT_CURRENCY,
    SUPPORTED_BILLING_PERIODS,
    SUPPORTED_CURRENCIES,
)
from modulo.core.spend_ceiling import cents_from_usd
from modulo.db.crud.organisation import get_organisation
from modulo.db.crud.scheduled_report import (
    create_scheduled_report,
    delete_scheduled_report,
    list_scheduled_reports,
)
from modulo.db.crud.spend_anomaly import dismiss_anomaly, list_anomalies
from modulo.db.crud.team import get_team, list_teams
from modulo.db.models.daily_run_count import OrgDailyRunCount
from modulo.db.models.organisation import Organisation
from modulo.db.models.scheduled_report import ScheduledReport
from modulo.db.rls import set_rls_org, set_rls_user_context

_CODE_COST_MANAGE = "cost.manage"
_MSG_DATABASE_ERROR_OCCURRED_PLEASE = "A database error occurred. Please try again."


_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/costs", tags=["admin", "costs"])


def _coerce_spend_limit_usd(value: Decimal | None) -> float | None:
    """Convert a stored spend-limit value to float USD, or None when absent/invalid.

    Returns None for NULL and non-numeric values so an empty/fresh database
    serialises as ``null`` instead of raising (which would 500 the endpoint).
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_cents_usd(value: int | None) -> float | None:
    """Convert an integer-cents column to float USD, or None when absent.

    Mirrors ``_coerce_spend_limit_usd`` for the FAR-391 cents columns
    (``max_run_cost_cents`` / ``spend_ceiling_cents`` / ``org_cumulative_spend_cents``).
    """
    if value is None:
        return None
    try:
        return value / 100.0
    except (TypeError, ValueError):
        return None


def _cost_controls(org: object) -> dict[str, Any]:
    """Return the org's persisted ``cost_controls`` settings dict (may be empty).

    ``settings_json`` is a JSON column that may be ``None`` or hold any shape;
    only dict values are treated as cost-control settings.
    """
    settings = getattr(org, "settings_json", None)
    if not isinstance(settings, dict):
        return {}
    cc = settings.get(COST_CONTROLS_KEY)
    return cc if isinstance(cc, dict) else {}


def _read_cost_control(org: object | None, key: str, default: Any) -> Any:
    """Read a single persisted cost-control field, falling back to ``default``."""
    if org is None:
        return default
    value = _cost_controls(org).get(key, default)
    return value if value is not None else default


def _read_currency(org: object | None) -> str:
    value = _read_cost_control(org, "currency", DEFAULT_CURRENCY)
    return value if isinstance(value, str) and value in SUPPORTED_CURRENCIES else DEFAULT_CURRENCY


def _read_billing_period(org: object | None) -> str:
    value = _read_cost_control(org, "billing_period", DEFAULT_BILLING_PERIOD)
    return value if isinstance(value, str) and value in SUPPORTED_BILLING_PERIODS else DEFAULT_BILLING_PERIOD


def _read_circuit_breaker(org: object | None) -> bool:
    value = _read_cost_control(org, "circuit_breaker_enabled", DEFAULT_CIRCUIT_BREAKER_ENABLED)
    return value if isinstance(value, bool) else DEFAULT_CIRCUIT_BREAKER_ENABLED


def _apply_cost_control_updates(org: Organisation, req: "UpdateCostControlsRequest") -> None:
    """Persist the budget and cost-control settings from ``req`` onto ``org``.

    ``settings_json`` is a JSON column that may hold arbitrary shapes, so the
    persisted cost-control settings are re-read defensively before merging. The
    FAR-391 hard spend ceilings (``max_run_cost`` / ``spend_ceiling``) are stored
    as integer cents on dedicated columns (exact, allocation-free comparison at
    the gate).
    """
    if req.budget is not None:
        org.daily_spend_limit = Decimal(str(req.budget))
    # ``exclude_unset`` lets an explicit ``null`` CLEAR a ceiling (back to
    # unlimited) while an omitted field leaves the existing value untouched — so
    # the two ceilings can be managed independently and "Empty = no limit" is
    # honoured by the frontend.
    provided = req.model_dump(exclude_unset=True)
    if "max_run_cost" in provided:
        org.max_run_cost_cents = cents_from_usd(req.max_run_cost)
    if "spend_ceiling" in provided:
        org.spend_ceiling_cents = cents_from_usd(req.spend_ceiling)

    updates: dict[str, Any] = {
        "currency": req.currency,
        "billing_period": req.billing_period,
        "circuit_breaker_enabled": req.circuit_breaker_enabled,
        "alert_thresholds": req.alert_thresholds,
    }
    if not any(v is not None for v in updates.values()):
        return
    settings_raw = org.settings_json if isinstance(org.settings_json, dict) else {}
    settings_dict = dict(settings_raw)
    cc = dict(_cost_controls(org))
    for key, value in updates.items():
        if value is not None:
            cc[key] = [float(t) for t in value] if key == "alert_thresholds" else value
    settings_dict[COST_CONTROLS_KEY] = cc
    org.settings_json = settings_dict


def _read_alert_thresholds(org: object | None) -> list[float]:
    """Read persisted alert thresholds, degrading to the default when corrupted.

    ``settings_json`` is persisted JSON and may hold arbitrary shapes. Anything
    that is not a non-empty list of whole numbers within 1..100 is rejected so a
    corrupted persisted value degrades to the default instead of raising (which
    would 500 the endpoint). Non-finite floats (``NaN``/``Infinity``) are also
    rejected since ``int()`` cannot coerce them and ``json.loads`` accepts them.
    Out-of-range ints are checked before any float conversion because
    ``math.isfinite`` raises ``OverflowError`` for ints too large to fit a
    float (e.g. a persisted ``[1000000000000000000000000000000]``). Normal
    writes are validated by
    ``UpdateCostControlsRequest._validate_alert_thresholds``; this read path is
    what keeps defensiveness against previously-corrupted data.
    """
    value = _read_cost_control(org, "alert_thresholds", [])
    if not isinstance(value, list) or not value:
        return list(DEFAULT_ALERT_THRESHOLDS)
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return list(DEFAULT_ALERT_THRESHOLDS)
        if isinstance(item, int):
            if not 1 <= item <= 100:
                return list(DEFAULT_ALERT_THRESHOLDS)
        elif not math.isfinite(item) or int(item) != item or not 1 <= item <= 100:
            return list(DEFAULT_ALERT_THRESHOLDS)
    return [float(v) for v in value]


class CostReportComponent(BaseModel):
    name: str
    amount_usd: str


class CostReportAnnotations(BaseModel):
    refused_total_usd: float | None = None
    clamped_total_usd: float | None = None


class CostReportRow(BaseModel):
    entity_id: str
    entity_name: str
    total_spend_usd: float
    total_runs: int
    components: list[CostReportComponent] = Field(default_factory=list)
    annotations: CostReportAnnotations = Field(default_factory=CostReportAnnotations)


class CostReportResponse(BaseModel):
    period: str
    group_by: str
    items: list[CostReportRow]
    # PR B reporting buckets — Decimal STRINGS (the NEW buckets are strings;
    # total_spend_usd stays FLOAT). REPORTING only, never a health gate.
    org_unassigned_components: str | None = None
    legacy_total: str | None = None
    org_total: str | None = None
    org_run_count: int | None = None
    has_more: bool = False


class SpendLimitResponse(BaseModel):
    organisation_id: str
    org_daily_spend_limit: float | None
    team_limits: list[dict[str, Any]]


class SetSpendLimitRequest(BaseModel):
    daily_spend_limit: float | None = Field(None, ge=0)


@router.get("")
@handle_db_errors("costs.get_costs")
async def get_costs(
    group_by: str = Query("team", pattern=r"^(team|org)$"),
    period: str = Query("month", pattern=r"^(day|week|month|year)$"),
    current_user: TenantPrincipal = require_permission(_CODE_COST_MANAGE),
    session: AsyncSession = Depends(get_db_session),
) -> CostReportResponse:

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            rows = await get_cost_report(
                session,
                org_id=current_user.organisation_id,
                group_by=group_by,
                period=period,
            )
            buckets = {}
            # REPORTING fields only — the ledger lines are the period-total
            # source; a failure in the runs-based detail aggregation
            # degrades to empty buckets, never to a 500. DB/HTTP/cancel errors
            # still propagate to the outer handlers below.
            try:
                buckets = await build_cost_report_buckets(
                    session,
                    org_id=current_user.organisation_id,
                    period=period,
                )
            except (SQLAlchemyError, HTTPException, asyncio.CancelledError):
                raise  # outer handlers map these to the canonical 501/503/4xx responses
            except Exception:
                _log.exception("get_costs buckets aggregation failed (org_id=%s)", current_user.organisation_id)
    except ProgrammingError:
        _log.exception("get_costs ProgrammingError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("get_costs SQLAlchemyError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("Unexpected error in get_costs")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    if not isinstance(buckets, dict):
        buckets = {}
    components_by_team = buckets.get("components_by_team", {})
    annotations_by_team = buckets.get("annotations_by_team", {})
    items = []
    for r in rows:
        bucket_key = "__org__" if group_by == "org" else r["entity_id"]
        items.append(
            CostReportRow(
                entity_id=r["entity_id"],
                entity_name=r["entity_name"],
                total_spend_usd=r["total_spend_usd"],
                total_runs=r["total_runs"],
                components=[CostReportComponent(**c) for c in components_by_team.get(bucket_key, [])],
                annotations=CostReportAnnotations(**(annotations_by_team.get(bucket_key, {}))),
            )
        )

    return CostReportResponse(
        period=period,
        group_by=group_by,
        items=items,
        org_unassigned_components=buckets.get("org_unassigned_components"),
        legacy_total=buckets.get("legacy_total"),
        org_total=buckets.get("org_total"),
        org_run_count=buckets.get("org_run_count"),
        has_more=bool(buckets.get("has_more", False)),
    )


@router.get("/limits")
@handle_db_errors("costs.get_spend_limits")
async def get_spend_limits(
    _: object = require_feature("admin_spend_limits"),
    __: object = require_feature("admin_cost_controls"),
    current_user: TenantPrincipal = require_permission(_CODE_COST_MANAGE),
    session: AsyncSession = Depends(get_db_session),
) -> SpendLimitResponse:

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            org = await get_organisation(session, current_user.organisation_id)

            teams_result = await list_teams(session, org_id=current_user.organisation_id, page=1, page_size=1000)
    except ProgrammingError:
        _log.exception("get_spend_limits ProgrammingError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("get_spend_limits SQLAlchemyError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("Unexpected error in get_spend_limits")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    return SpendLimitResponse(
        organisation_id=str(current_user.organisation_id),
        org_daily_spend_limit=(float(org.daily_spend_limit) if org and org.daily_spend_limit is not None else None),
        team_limits=[
            {
                "team_id": str(t.id),
                "team_name": t.name,
                "daily_spend_limit": (float(t.daily_spend_limit) if t.daily_spend_limit is not None else None),
            }
            for t in teams_result.items
        ],
    )


@router.put("/limits/org")
@handle_db_errors("costs.set_org_spend_limit")
async def set_org_spend_limit(
    req: SetSpendLimitRequest,
    _: object = require_feature("admin_spend_limits"),
    __: object = require_feature("admin_cost_controls"),
    current_user: TenantPrincipal = require_permission(_CODE_COST_MANAGE),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            org = await get_organisation(session, current_user.organisation_id)
            if org is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")
            org.daily_spend_limit = Decimal(str(req.daily_spend_limit)) if req.daily_spend_limit is not None else None
            await session.flush()
    except ProgrammingError:
        _log.exception("set_org_spend_limit ProgrammingError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("set_org_spend_limit SQLAlchemyError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("Unexpected error in set_org_spend_limit")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    return {
        "organisation_id": str(org.id),
        "daily_spend_limit": req.daily_spend_limit,
    }


@router.put("/limits/teams/{team_id}")
@handle_db_errors("costs.set_team_spend_limit")
async def set_team_spend_limit(
    team_id: uuid.UUID,
    req: SetSpendLimitRequest,
    _: object = require_feature("admin_spend_limits"),
    __: object = require_feature("admin_cost_controls"),
    current_user: TenantPrincipal = require_permission(_CODE_COST_MANAGE),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            team = await get_team(session, team_id)
            if team is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
            team.daily_spend_limit = Decimal(str(req.daily_spend_limit)) if req.daily_spend_limit is not None else None
            await session.flush()
    except ProgrammingError:
        _log.exception("set_team_spend_limit ProgrammingError (team_id=%s)", team_id)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("set_team_spend_limit SQLAlchemyError (team_id=%s)", team_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("Unexpected error in set_team_spend_limit")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    return {
        "team_id": team_id,
        "daily_spend_limit": req.daily_spend_limit,
    }


class CostControlsResponse(BaseModel):
    teams: list[dict[str, object]]
    budget: float | None = None
    # FAR-391 — hard spend ceilings (USD at the API boundary; stored as cents).
    max_run_cost: float | None = None
    spend_ceiling: float | None = None
    org_cumulative_spend_usd: float = 0.0
    alert_thresholds: list[float] = Field(default_factory=lambda: list(DEFAULT_ALERT_THRESHOLDS))
    circuit_breaker_enabled: bool = False
    currency: str = "USD"
    billing_period: str = "monthly"


class UpdateCostControlsRequest(BaseModel):
    budget: float | None = None
    # FAR-391 — hard spend ceilings in USD. ``None`` = clear this ceiling to no
    # limit (explicit null in the body); omitting the field leaves the existing
    # value unchanged so the two ceilings can be managed independently. 0 =
    # kill-switch (block all runs).
    max_run_cost: float | None = None
    spend_ceiling: float | None = None
    alert_thresholds: list[float] | None = None
    circuit_breaker_enabled: bool | None = None
    currency: Literal["USD", "EUR", "GBP"] | None = None
    billing_period: Literal["monthly", "quarterly", "annual"] | None = None

    @field_validator("alert_thresholds")
    @classmethod
    def _validate_alert_thresholds(cls, value: list[float] | None) -> list[float] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("alert_thresholds must be a non-empty list of integers in 1..100")
        for threshold in value:
            if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
                raise ValueError("alert_thresholds values must be integers in 1..100")
            if int(threshold) != threshold or not 1 <= int(threshold) <= 100:
                raise ValueError("alert_thresholds values must be integers in 1..100")
        return list(value)


@router.get("/controls")
@handle_db_errors("costs.get_cost_controls")
async def get_cost_controls(
    _: object = require_feature("admin_cost_controls"),
    current_user: TenantPrincipal = require_permission(_CODE_COST_MANAGE),
    session: AsyncSession = Depends(get_db_session),
) -> CostControlsResponse:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            teams_result = await list_teams(session, org_id=current_user.organisation_id, page=1, page_size=1000)
            org = await get_organisation(session, current_user.organisation_id)
    except ProgrammingError:
        _log.exception("get_cost_controls ProgrammingError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("get_cost_controls SQLAlchemyError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("Unexpected error in get_cost_controls")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    return CostControlsResponse(
        teams=[
            {
                "id": str(t.id),
                "name": t.name,
                "daily_limit_usd": _coerce_spend_limit_usd(t.daily_spend_limit),
            }
            for t in teams_result.items
        ],
        budget=_coerce_spend_limit_usd(org.daily_spend_limit) if org is not None else None,
        max_run_cost=_coerce_cents_usd(org.max_run_cost_cents) if org is not None else None,
        spend_ceiling=_coerce_cents_usd(org.spend_ceiling_cents) if org is not None else None,
        org_cumulative_spend_usd=_coerce_cents_usd(org.org_cumulative_spend_cents or 0) or 0.0
        if org is not None
        else 0.0,
        alert_thresholds=_read_alert_thresholds(org),
        circuit_breaker_enabled=_read_circuit_breaker(org),
        currency=_read_currency(org),
        billing_period=_read_billing_period(org),
    )


@router.put("/controls")
@handle_db_errors("costs.update_cost_controls")
async def update_cost_controls(
    req: UpdateCostControlsRequest,
    _: object = require_feature("admin_cost_controls"),
    current_user: TenantPrincipal = require_permission(_CODE_COST_MANAGE),
    session: AsyncSession = Depends(get_db_session),
) -> CostControlsResponse:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            org = await get_organisation(session, current_user.organisation_id)
            if org is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")

            _apply_cost_control_updates(org, req)

            await session.flush()
            teams_result = await list_teams(session, org_id=current_user.organisation_id, page=1, page_size=1000)
    except ProgrammingError:
        _log.exception("update_cost_controls ProgrammingError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("update_cost_controls SQLAlchemyError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("Unexpected error in update_cost_controls")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    return CostControlsResponse(
        teams=[
            {
                "id": str(t.id),
                "name": t.name,
                "daily_limit_usd": _coerce_spend_limit_usd(t.daily_spend_limit),
            }
            for t in teams_result.items
        ],
        budget=_coerce_spend_limit_usd(org.daily_spend_limit),
        max_run_cost=_coerce_cents_usd(org.max_run_cost_cents),
        spend_ceiling=_coerce_cents_usd(org.spend_ceiling_cents),
        org_cumulative_spend_usd=_coerce_cents_usd(org.org_cumulative_spend_cents or 0) or 0.0,
        alert_thresholds=_read_alert_thresholds(org),
        circuit_breaker_enabled=_read_circuit_breaker(org),
        currency=_read_currency(org),
        billing_period=_read_billing_period(org),
    )


# ── FAR-391: dedicated hard spend-ceiling endpoints ───────────────────────────
#
# A focused surface for the per-run / per-org hard ceilings so the org-settings
# frontend (and any admin tooling) can read + set them without the full
# cost-controls payload. Ceilings are USD at the API boundary and stored as
# integer cents.


class SpendCeilingResponse(BaseModel):
    max_run_cost: float | None = None
    spend_ceiling: float | None = None
    org_cumulative_spend_usd: float = 0.0
    remaining_budget_usd: float | None = None


class SetSpendCeilingRequest(BaseModel):
    max_run_cost: float | None = Field(
        None,
        ge=0,
        description="Per-run hard ceiling in USD. 0 = block all runs. null = no limit (clears an existing ceiling).",
    )
    spend_ceiling: float | None = Field(
        None,
        ge=0,
        description="Org lifetime budget in USD. 0 = block all runs. null = no limit (clears an existing ceiling).",
    )


@router.get("/ceiling")
@handle_db_errors("costs.get_spend_ceiling")
async def get_spend_ceiling(
    _: object = require_feature("admin_cost_controls"),
    current_user: TenantPrincipal = require_permission(_CODE_COST_MANAGE),
    session: AsyncSession = Depends(get_db_session),
) -> SpendCeilingResponse:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            org = await get_organisation(session, current_user.organisation_id)
    except ProgrammingError:
        _log.exception("get_spend_ceiling ProgrammingError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("get_spend_ceiling SQLAlchemyError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("Unexpected error in get_spend_ceiling")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    max_run_cost = _coerce_cents_usd(org.max_run_cost_cents) if org is not None else None
    spend_ceiling = _coerce_cents_usd(org.spend_ceiling_cents) if org is not None else None
    cumulative = _coerce_cents_usd(org.org_cumulative_spend_cents or 0) or 0.0 if org is not None else 0.0
    remaining = None
    if spend_ceiling is not None:
        remaining = max(spend_ceiling - cumulative, 0.0)
    return SpendCeilingResponse(
        max_run_cost=max_run_cost,
        spend_ceiling=spend_ceiling,
        org_cumulative_spend_usd=cumulative,
        remaining_budget_usd=remaining,
    )


@router.put("/ceiling")
@handle_db_errors("costs.set_spend_ceiling")
async def set_spend_ceiling(
    req: SetSpendCeilingRequest,
    _: object = require_feature("admin_cost_controls"),
    current_user: TenantPrincipal = require_permission(_CODE_COST_MANAGE),
    session: AsyncSession = Depends(get_db_session),
) -> SpendCeilingResponse:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            org = await get_organisation(session, current_user.organisation_id)
            if org is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")
            # ``exclude_unset`` distinguishes "field not sent" (leave unchanged,
            # so a partial update never clobbers the other ceiling) from an
            # explicit ``null`` (clear this ceiling back to unlimited). An empty
            # frontend input maps to ``null``, so "Empty = no limit" is honoured.
            provided = req.model_dump(exclude_unset=True)
            if "max_run_cost" in provided:
                org.max_run_cost_cents = cents_from_usd(req.max_run_cost)
            if "spend_ceiling" in provided:
                org.spend_ceiling_cents = cents_from_usd(req.spend_ceiling)
            await session.flush()
    except ProgrammingError:
        _log.exception("set_spend_ceiling ProgrammingError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("set_spend_ceiling SQLAlchemyError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("Unexpected error in set_spend_ceiling")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    max_run_cost = _coerce_cents_usd(org.max_run_cost_cents)
    spend_ceiling = _coerce_cents_usd(org.spend_ceiling_cents)
    cumulative = _coerce_cents_usd(org.org_cumulative_spend_cents or 0) or 0.0
    remaining = None
    if spend_ceiling is not None:
        remaining = max(spend_ceiling - cumulative, 0.0)
    return SpendCeilingResponse(
        max_run_cost=max_run_cost,
        spend_ceiling=spend_ceiling,
        org_cumulative_spend_usd=cumulative,
        remaining_budget_usd=remaining,
    )


class CircuitBreakerResetResponse(BaseModel):
    pipeline_id: str
    circuit_breaker_tripped: bool
    triggers_reactivated: int


@router.post("/circuit-breaker/{pipeline_id}/reset")
@handle_db_errors("costs.reset_circuit_breaker")
async def reset_circuit_breaker(
    pipeline_id: uuid.UUID,
    _: object = require_feature("admin_cost_controls"),
    current_user: TenantPrincipal = require_permission(_CODE_COST_MANAGE),
    session: AsyncSession = Depends(get_db_session),
) -> CircuitBreakerResetResponse:
    """Admin re-enable: clear a tripped pipeline circuit breaker.

    Sets ``circuit_breaker_tripped = False`` on the pipeline and re-activates
    all of its (non-deleted) triggers so new runs are allowed again (spec §8.10
    ``circuit_breaker``: "Permanently pauses trigger until admin re-enables").
    """
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)
            reset = await reset_pipeline_circuit_breaker(
                session,
                org_id=current_user.organisation_id,
                pipeline_id=pipeline_id,
            )
            if not reset:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline not found")
    except ProgrammingError:
        _log.exception("reset_circuit_breaker ProgrammingError (pipeline_id=%s)", pipeline_id)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("reset_circuit_breaker SQLAlchemyError (pipeline_id=%s)", pipeline_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("Unexpected error in reset_circuit_breaker")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    return CircuitBreakerResetResponse(
        pipeline_id=str(pipeline_id),
        circuit_breaker_tripped=False,
        triggers_reactivated=0,
    )


# ── Export ────────────────────────────────────────────────────────────────────


@router.get("/export")
@handle_db_errors("costs.export_costs")
async def export_costs(
    period: str = Query("this_month", pattern=r"^(this_month|last_month|7d|30d|90d)$"),
    group_by: str = Query("team", pattern=r"^(team|pipeline|model)$"),
    format: str = Query("csv", pattern=r"^(csv)$"),
    _: object = require_feature("admin_cost_breakdown"),
    current_user: TenantPrincipal = require_permission(_CODE_COST_MANAGE),
    session: AsyncSession = Depends(get_db_session),
) -> Response:

    period_map: dict[str, str] = {
        "this_month": "month",
        "last_month": "month",
        "7d": "week",
        "30d": "month",
        "90d": "year",
    }

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            rows = await get_cost_report(
                session,
                org_id=current_user.organisation_id,
                group_by=group_by if group_by != "model" else "team",
                period=period_map.get(period, "month"),
            )
    except ProgrammingError:
        _log.exception("export_costs ProgrammingError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("export_costs SQLAlchemyError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("Unexpected error in export_costs")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["entity_id", "entity_name", "total_spend_usd", "total_runs"])
    for r in rows:
        writer.writerow([r["entity_id"], r["entity_name"], r["total_spend_usd"], r["total_runs"]])
    csv_content = output.getvalue()

    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="costs-export-{period}.csv"'},
    )


# ── Scheduled Reports ────────────────────────────────────────────────────────


class CreateReportRequest(BaseModel):
    period: str = Field(pattern=r"^(daily|weekly|monthly)$")
    group_by: str = Field(pattern=r"^(team|org)$")
    format: str = Field(default="csv", pattern=r"^(csv|json)$")
    recipients: list[str] = Field(min_length=1)
    schedule_type: str = Field(default="one_time", pattern=r"^(one_time|recurring)$")


class ReportResponse(BaseModel):
    id: str
    period: str
    group_by: str
    format: str
    recipients: list[str]
    schedule_type: str
    created_at: str


def _report_response(report: ScheduledReport) -> ReportResponse:
    period = report.period
    group_by = report.group_by
    report_format = report.format
    schedule_type = report.schedule_type
    if period is None or group_by is None or report_format is None or schedule_type is None:
        raise ValueError(f"Scheduled cost report {report.id} has invalid configuration")
    return ReportResponse(
        id=str(report.id),
        period=period,
        group_by=group_by,
        format=report_format,
        recipients=report.recipients,
        schedule_type=schedule_type,
        created_at=report.created_at.isoformat(),
    )


@router.post("/reports", status_code=201)
@handle_db_errors("costs.create_report")
async def create_report(
    req: CreateReportRequest,
    _: object = require_feature("admin_cost_controls"),
    current_user: TenantPrincipal = require_permission(_CODE_COST_MANAGE),
    session: AsyncSession = Depends(get_db_session),
) -> ReportResponse:

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            report = await create_scheduled_report(
                session,
                organisation_id=current_user.organisation_id,
                period=req.period,
                group_by=req.group_by,
                format=req.format,
                recipients=req.recipients,
                schedule_type=req.schedule_type,
                account_id=current_user.account_id,
            )
    except ProgrammingError:
        _log.exception("create_report ProgrammingError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("create_report SQLAlchemyError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("Unexpected error in create_report")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    return _report_response(report)


@router.get("/reports")
@handle_db_errors("costs.list_reports")
async def list_reports(
    _: object = require_feature("admin_cost_controls"),
    current_user: TenantPrincipal = require_permission(_CODE_COST_MANAGE),
    session: AsyncSession = Depends(get_db_session),
) -> list[ReportResponse]:

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            reports = await list_scheduled_reports(
                session,
                organisation_id=current_user.organisation_id,
            )
    except ProgrammingError:
        _log.exception("list_reports ProgrammingError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("list_reports SQLAlchemyError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("Unexpected error in list_reports")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    return [_report_response(report) for report in reports]


@router.delete("/reports/{report_id}", status_code=204)
@handle_db_errors("costs.delete_report")
async def delete_report(
    report_id: uuid.UUID,
    _: object = require_feature("admin_cost_controls"),
    current_user: TenantPrincipal = require_permission(_CODE_COST_MANAGE),
    session: AsyncSession = Depends(get_db_session),
) -> None:

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            deleted = await delete_scheduled_report(
                session,
                report_id=report_id,
                organisation_id=current_user.organisation_id,
            )
    except ProgrammingError:
        _log.exception("delete_report ProgrammingError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("delete_report SQLAlchemyError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("Unexpected error in delete_report")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")


# ── Anomalies ──────────────────────────────────────────────────────────────────


class AnomalyResponse(BaseModel):
    id: str
    anomaly_date: str
    pipeline_id: str | None
    amount: float
    baseline: float
    percent_above: float
    dismissed: bool


def _build_rolling_anomalies(daily_spends: list[tuple[Any, float]]) -> list[dict[str, Any]]:
    """Detect days whose spend exceeds 2x the rolling 7-day average."""
    anomalies: list[dict[str, Any]] = []
    for i, (run_date, spend) in enumerate(daily_spends[7:], start=7):
        window = [s for _, s in daily_spends[i - 7 : i]]
        avg = (sum(window) / len(window)) if window else 0.0
        if avg and spend / avg > 2.0:
            anomalies.append(
                {
                    "id": "",
                    "anomaly_date": str(run_date),
                    "pipeline_id": None,
                    "amount": spend,
                    "baseline": avg,
                    "percent_above": round((spend / avg - 1.0) * 100, 2),
                    "dismissed": False,
                }
            )
    return anomalies


def _merge_anomalies(anomalies: list[dict[str, Any]], stored: Any) -> list[dict[str, Any]]:
    """Merge freshly detected anomalies with previously stored ones, keeping stored dismissals."""
    stored_dict: dict[str, Any] = {}
    for a in stored:
        key = str(a.anomaly_date)
        if key not in stored_dict:
            stored_dict[key] = {
                "id": str(a.id),
                "anomaly_date": str(a.anomaly_date),
                "pipeline_id": str(a.pipeline_id) if a.pipeline_id else None,
                "amount": float(a.amount),
                "baseline": float(a.baseline),
                "percent_above": float(a.percent_above),
                "dismissed": a.dismissed,
            }

    for a in anomalies:
        key = a["anomaly_date"]
        if key in stored_dict:
            a["dismissed"] = stored_dict[key]["dismissed"]

    seen_dates = {a["anomaly_date"] for a in anomalies}
    for key, sa in stored_dict.items():
        if key not in seen_dates:
            anomalies.append(sa)
    return anomalies


@router.get("/anomalies")
@handle_db_errors("costs.get_anomalies")
async def get_anomalies(
    _: object = require_feature("admin_cost_breakdown"),
    current_user: TenantPrincipal = require_permission(_CODE_COST_MANAGE),
    session: AsyncSession = Depends(get_db_session),
) -> list[AnomalyResponse]:

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)

            # Detect anomalies: daily org spend > 2x rolling 7-day avg
            today = datetime.now(UTC).date()
            lookback = today - timedelta(days=30)

            counts_q = (
                select(
                    OrgDailyRunCount.run_date,
                    func.sum(OrgDailyRunCount.total_spend_usd).label("daily_spend"),
                )
                .where(
                    OrgDailyRunCount.organisation_id == current_user.organisation_id,
                    OrgDailyRunCount.run_date >= lookback,
                    OrgDailyRunCount.team_id.is_(None),
                )
                .group_by(OrgDailyRunCount.run_date)
                .order_by(OrgDailyRunCount.run_date)
            )

            counts_result = await session.execute(counts_q)
            raw_rows = counts_result.all()
            daily_spends = [(r.run_date, float(str(r.daily_spend))) for r in raw_rows if r.daily_spend is not None]

            detected = _build_rolling_anomalies(daily_spends)

            # Also merge previously stored anomalies (keeper of dismissals)
            stored = await list_anomalies(session, organisation_id=current_user.organisation_id, dismissed=False)
            return [AnomalyResponse(**a) for a in _merge_anomalies(detected, stored)]
    except ProgrammingError:
        _log.exception("get_anomalies ProgrammingError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("get_anomalies SQLAlchemyError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("Unexpected error in get_anomalies")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None


@router.post("/anomalies/dismiss/{anomaly_id}", status_code=204)
@handle_db_errors("costs.dismiss_anomaly_endpoint")
async def dismiss_anomaly_endpoint(
    anomaly_id: uuid.UUID,
    _: object = require_feature("admin_cost_breakdown"),
    current_user: TenantPrincipal = require_permission(_CODE_COST_MANAGE),
    session: AsyncSession = Depends(get_db_session),
) -> None:

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            dismissed = await dismiss_anomaly(
                session,
                anomaly_id=anomaly_id,
                organisation_id=current_user.organisation_id,
            )
    except ProgrammingError:
        _log.exception("dismiss_anomaly ProgrammingError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception("dismiss_anomaly SQLAlchemyError (org_id=%s)", current_user.organisation_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("Unexpected error in dismiss_anomaly_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None

    if not dismissed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Anomaly not found")
