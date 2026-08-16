"""Admin cost-components CRUD — org-scoped, soft-delete, validated.

Prefix: ``/api/v1/admin/costs/components``. Gated with
``require_permission("cost.manage")`` + ``require_feature("admin_cost_breakdown")``
on the write routes AND the GET list. Save-time 422 validation is the ONLY
validation path — there is NO validate-formula endpoint (the frontend's inline
UI validation round-trips through the save endpoint's 422).

Layer rule: ``modulo.db`` may not import ``modulo.core``, so ALL
core-dependent validation (formula engine, param registry, constants) and the
audit-event emission live HERE in the route layer; the CRUD module performs
only the DB-level checks (org cap, duplicate pre-check, last-calculated
guards).
"""

from __future__ import annotations

import logging
import re as _re
import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_feature, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.core.audit_logger import append_audit_event
from modulo.core.cost_controller.breakdown.constants import (
    MAX_COMPONENTS_PER_ORG,
    MAX_DISPLAY_NAME_LENGTH,
    MAX_FORMULA_LENGTH,
)
from modulo.core.cost_controller.breakdown.formula import CostFormulaError, validate_formula
from modulo.core.cost_controller.breakdown.params import CALCULATED_ALLOWED_IDENTS, REGISTERED_RATE_FALLBACKS
from modulo.db.crud.cost_component import (
    create_cost_component,
    list_cost_components,
    soft_delete_cost_component,
    update_cost_component,
)
from modulo.db.models.cost_component import CostComponent, CostComponentKind
from modulo.db.rls import set_rls_org
from modulo.settings import get_settings

_RE_SAFE_KEY_NAME = "^[a-z][a-z0-9_]{1,63}$"
_CODE_COST_MANAGE = "cost.manage"


_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/costs/components", tags=["admin", "costs"])

_RATE_COLUMN_CAP = Decimal("999999999999.999999")

RESERVED_NAMES = frozenset({"reported", "rate", "cost_estimate_usd", "model_cost_usd"})
RESERVED_REPORT_KEYS = frozenset({"reported", "rate", "cost_estimate_usd"})


def validate_component_fields(*, name: str | None = None, report_key: str | None = None) -> None:
    """Reserved-key validation (422). ``model_cost_usd`` is NOT reserved as a report_key."""
    if name is not None and name in RESERVED_NAMES:
        raise CostFormulaError(
            "reserved_name",
            f"component name {name!r} is reserved (reserved: {sorted(RESERVED_NAMES)})",
        )
    if report_key is not None and report_key in RESERVED_REPORT_KEYS:
        raise CostFormulaError(
            "reserved_report_key",
            f"report_key {report_key!r} is reserved (reserved: {sorted(RESERVED_REPORT_KEYS)})",
        )


def validate_rate_fallback(rate_fallback: str | None) -> None:
    """rate_fallback REGISTRY validation (422, fail-closed)."""
    if rate_fallback is not None and rate_fallback not in REGISTERED_RATE_FALLBACKS:
        raise CostFormulaError(
            "unknown_rate_fallback",
            f"rate_fallback {rate_fallback!r} is not registered (valid: {sorted(REGISTERED_RATE_FALLBACKS)})",
        )


def _referenced_idents(formula: str) -> set[str]:
    return set(_re.findall(r"[A-Za-z_][A-Za-z0-9_]*", formula))


def validate_component_formula(
    *,
    kind: str,
    formula: str | None,
    rate_usd: Any,
    rate_fallback: str | None,
) -> None:
    """Cross-field formula validation (422)."""
    if kind == CostComponentKind.SELF_REPORTED.value:
        if formula is not None:
            raise CostFormulaError(
                "self_reported_formula",
                "self_reported components must have formula NULL (implicit reported)",
            )
        return
    # calculated
    if not formula or not formula.strip():
        raise CostFormulaError("missing_formula", "calculated components require a non-empty formula")
    validate_formula(formula, CALCULATED_ALLOWED_IDENTS)
    if rate_usd is None and rate_fallback is None and "rate" in _referenced_idents(formula):
        raise CostFormulaError(
            "rate_without_source",
            "formula references 'rate' but neither rate_usd nor a registered rate_fallback is set",
        )


class CostComponentBase(BaseModel):
    name: str | None = Field(None, pattern=_RE_SAFE_KEY_NAME)
    display_name: str | None = Field(None, min_length=1, max_length=MAX_DISPLAY_NAME_LENGTH)
    kind: CostComponentKind | None = None
    rate_usd: Decimal | None = Field(None, ge=0)
    rate_fallback: str | None = Field(None, max_length=32)
    formula: str | None = Field(None, max_length=MAX_FORMULA_LENGTH)
    report_key: str | None = Field(None, max_length=64)
    enabled: bool | None = None
    sort_order: int | None = None


class CostComponentCreate(CostComponentBase):
    name: str = Field(..., pattern=_RE_SAFE_KEY_NAME)
    display_name: str = Field(..., min_length=1, max_length=MAX_DISPLAY_NAME_LENGTH)
    kind: CostComponentKind = Field(...)
    enabled: bool = True
    sort_order: int = 0

    @model_validator(mode="after")
    def _validate_cross_field(self) -> CostComponentCreate:
        _validate_rate_usd_bound(self.rate_usd)
        _cross_field_validation(
            kind=self.kind.value,
            rate_usd=self.rate_usd,
            rate_fallback=self.rate_fallback,
            formula=self.formula,
            report_key=self.report_key,
        )
        return self


class CostComponentUpdate(BaseModel):
    """Partial update via ``model_fields_set`` (exclude_unset).

    Explicit ``rate_usd: None`` clears the field back to NULL (env fallback) —
    do NOT use exclude_none.
    """

    name: str | None = Field(None, pattern=_RE_SAFE_KEY_NAME)
    display_name: str | None = Field(None, min_length=1, max_length=MAX_DISPLAY_NAME_LENGTH)
    kind: CostComponentKind | None = None
    rate_usd: Decimal | None = Field(None, ge=0)
    rate_fallback: str | None = Field(None, max_length=32)
    formula: str | None = Field(None, max_length=MAX_FORMULA_LENGTH)
    report_key: str | None = Field(None, max_length=64)
    enabled: bool | None = None
    sort_order: int | None = None

    @model_validator(mode="after")
    def _validate_rate(self) -> CostComponentUpdate:
        if self.rate_usd is not None:
            _validate_rate_usd_bound(self.rate_usd)
        return self


class CostComponentResponse(BaseModel):
    id: uuid.UUID
    name: str
    display_name: str
    kind: str
    rate_usd: Decimal | None
    rate_fallback: str | None
    formula: str | None
    report_key: str | None
    enabled: bool
    sort_order: int
    deleted_at: Any = None

    model_config = {"from_attributes": True}


def _validate_rate_usd_bound(rate_usd: Decimal | None) -> None:
    """DYNAMIC upper bound — the env knob moves the write-path boundary; the
    Numeric(18,6) column cap is the hard ceiling. A Settings-unavailable
    context raises ValueError -> FastAPI maps to 422 (never an implicit 500).
    """
    if rate_usd is None:
        return
    try:
        knob = Decimal(str(get_settings().max_rate_usd))
    except Exception:
        raise ValueError("rate knob unavailable: cannot validate rate_usd") from None
    cap = min(knob, _RATE_COLUMN_CAP)
    if rate_usd > cap:
        raise ValueError(f"rate_usd exceeds max {cap}")


def _cross_field_validation(
    *,
    kind: str,
    rate_usd: Decimal | None,
    rate_fallback: str | None,
    formula: str | None,
    report_key: str | None,
) -> None:
    validate_component_fields(name=None, report_key=report_key)
    validate_rate_fallback(rate_fallback)
    if kind == CostComponentKind.SELF_REPORTED.value and report_key is None:
        raise ValueError("self_reported components require report_key")
    if kind == CostComponentKind.CALCULATED.value and report_key is not None:
        raise ValueError("calculated components must have report_key None")
    validate_component_formula(kind=kind, formula=formula, rate_usd=rate_usd, rate_fallback=rate_fallback)


def _map_validation_error(exc: Exception) -> HTTPException:
    if isinstance(exc, CostFormulaError):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))
    message = str(exc)
    if message == "duplicate_component":
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A cost component with this name/report_key already exists.",
        )
    if message.startswith("last_calculated"):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The last enabled calculated cost component cannot be deleted, disabled, or kind-changed.",
        )
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=message)


def _to_response(component: CostComponent) -> CostComponentResponse:
    return CostComponentResponse.model_validate(component)


@router.get("", response_model=list[CostComponentResponse])
@handle_db_errors("costs.components.list")
async def get_components(
    _: object = require_feature("admin_cost_breakdown"),
    current_user: TenantPrincipal = require_permission(_CODE_COST_MANAGE),
    session: AsyncSession = Depends(get_db_session),
) -> list[CostComponentResponse]:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            rows = await list_cost_components(session)
        return [_to_response(c) for c in rows]
    except CostFormulaError as exc:
        raise _map_validation_error(exc) from None
    except ValueError as exc:
        raise _map_validation_error(exc) from None


@router.post("", response_model=CostComponentResponse, status_code=status.HTTP_201_CREATED)
@handle_db_errors("costs.components.create")
async def create_component(
    req: CostComponentCreate,
    _: object = require_feature("admin_cost_breakdown"),
    current_user: TenantPrincipal = require_permission(_CODE_COST_MANAGE),
    session: AsyncSession = Depends(get_db_session),
) -> CostComponentResponse:
    try:
        validate_component_fields(name=req.name, report_key=req.report_key)
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            component = await create_cost_component(
                session,
                org_id=current_user.organisation_id,
                name=req.name,
                display_name=req.display_name,
                kind=req.kind.value,
                rate_usd=req.rate_usd,
                rate_fallback=req.rate_fallback,
                formula=req.formula,
                report_key=req.report_key,
                enabled=req.enabled,
                sort_order=req.sort_order,
                max_components=MAX_COMPONENTS_PER_ORG,
            )
            await append_audit_event(
                session,
                org_id=current_user.organisation_id,
                event_type="cost_component_created",
                actor_user_id=current_user.user_id,
                resource_type="cost_component",
                resource_id=component.id,
                payload_json={"name": component.name, "kind": component.kind},
            )
        return _to_response(component)
    except CostFormulaError as exc:
        raise _map_validation_error(exc) from None
    except ValueError as exc:
        raise _map_validation_error(exc) from None


@router.put("/{component_id}", response_model=CostComponentResponse)
@handle_db_errors("costs.components.update")
async def update_component(
    component_id: uuid.UUID,
    req: CostComponentUpdate,
    _: object = require_feature("admin_cost_breakdown"),
    current_user: TenantPrincipal = require_permission(_CODE_COST_MANAGE),
    session: AsyncSession = Depends(get_db_session),
) -> CostComponentResponse:
    updates = dict(req.model_dump(exclude_unset=True))
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            component = await update_cost_component(
                session,
                component_id=component_id,
                updates=updates,
            )
            if component is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cost component not found.")
            # Validate the MERGED state (partial update) against the same rules.
            validate_component_fields(name=component.name, report_key=component.report_key)
            validate_rate_fallback(component.rate_fallback)
            _cross_field_validation(
                kind=component.kind,
                rate_usd=component.rate_usd,
                rate_fallback=component.rate_fallback,
                formula=component.formula,
                report_key=component.report_key,
            )
            await append_audit_event(
                session,
                org_id=current_user.organisation_id,
                event_type="cost_component_updated",
                actor_user_id=current_user.user_id,
                resource_type="cost_component",
                resource_id=component.id,
                payload_json={"name": component.name, "kind": component.kind},
            )
        return _to_response(component)
    except CostFormulaError as exc:
        raise _map_validation_error(exc) from None
    except ValueError as exc:
        raise _map_validation_error(exc) from None


@router.delete("/{component_id}", status_code=status.HTTP_204_NO_CONTENT)
@handle_db_errors("costs.components.delete")
async def delete_component(
    component_id: uuid.UUID,
    _: object = require_feature("admin_cost_breakdown"),
    current_user: TenantPrincipal = require_permission(_CODE_COST_MANAGE),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            component = await soft_delete_cost_component(
                session,
                component_id=component_id,
            )
            if component is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cost component not found.")
            await append_audit_event(
                session,
                org_id=current_user.organisation_id,
                event_type="cost_component_deleted",
                actor_user_id=current_user.user_id,
                resource_type="cost_component",
                resource_id=component.id,
                payload_json={"name": component.name},
            )
    except CostFormulaError as exc:
        raise _map_validation_error(exc) from None
    except ValueError as exc:
        raise _map_validation_error(exc) from None
