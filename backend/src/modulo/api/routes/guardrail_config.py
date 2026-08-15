"""Guardrail config-as-code REST API (FAR-219 T3).

URLs (mounted under ``/api/v1/guardrails/config``):

    GET    /api/v1/guardrails/config         — export the applied config as YAML
    POST   /api/v1/guardrails/config/propose — validate + hash + diff a proposal
    POST   /api/v1/guardrails/config/apply   — apply the pending proposal (approve/merge)
    POST   /api/v1/guardrails/config/reject  — discard the pending proposal
    GET    /api/v1/guardrails/config/drift   — recompute drift vs the applied pin

The workflow is git-style: **propose** → **diff** → **apply**. Apply is the
operator-only "merge" step that reconciles the live ``eval_type='guardrail'``
``EvalDefinition`` rows the shipped interception seam consumes; the
config-as-code layer is an authoring/source-of-truth seam on top, never a
change to the engine's semantics. Every state-changing step emits an audit
event (summary payloads only — never raw config content).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.core.audit_logger import append_audit_event
from modulo.core.eval_engine import EvalDefinition
from modulo.core.guardrails import GuardrailConfigError, to_engine_definition
from modulo.core.guardrails.config import (
    ConfigChange,
    GuardrailConfigSet,
    GuardrailPin,
    build_config_set_from_definitions,
    check_guardrail_drift,
    diff_config_sets,
    dump_config_set,
    hash_config_set,
    load_config_set,
    to_eval_config,
    utc_now_iso,
)
from modulo.db.crud.guardrail_config import get_guardrail_pin, set_guardrail_pin
from modulo.db.models.eval_definition import EvalDefinition as EvalDefinitionRow
from modulo.db.models.pipeline import Pipeline
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/guardrails/config", tags=["guardrails"])


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class ProposeGuardrailConfigRequest(BaseModel):
    config_yaml: str = Field(min_length=1)


class GuardrailConfigResponse(BaseModel):
    config_yaml: str
    hash: str | None = None
    applied_at: str | None = None
    status: str  # clean | proposed | drift


class GuardrailChangeResponse(BaseModel):
    action: str
    id: str
    name: str
    old_hash: str | None = None
    new_hash: str | None = None
    detail: str = ""


class GuardrailProposalResponse(BaseModel):
    proposed: bool
    hash: str
    diff: list[GuardrailChangeResponse]
    status: str = "proposed"


class GuardrailApplyResponse(BaseModel):
    applied: bool
    hash: str
    applied_at: str
    status: str = "clean"


class GuardrailRejectResponse(BaseModel):
    rejected: bool
    status: str = "clean"


class GuardrailDriftResponse(BaseModel):
    status: str  # clean | proposed | drift
    current_hash: str | None = None
    applied_hash: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load_guardrail_definitions(session: AsyncSession, org_id: uuid.UUID) -> list[EvalDefinition]:
    """Load ALL the org's guardrail rows as engine DTOs (across pipelines)."""
    rows = (
        (
            await session.execute(
                select(EvalDefinitionRow).where(
                    EvalDefinitionRow.organisation_id == org_id,
                    EvalDefinitionRow.eval_type == "guardrail",
                )
            )
        )
        .scalars()
        .all()
    )
    return [to_engine_definition(row) for row in rows]


def _applied_config_set(pin: GuardrailPin | None) -> GuardrailConfigSet:
    """The last-applied config set (from the pin snapshot), or the empty set."""
    if pin is None or not pin.serialized_snapshot:
        return GuardrailConfigSet()
    try:
        return load_config_set(pin.serialized_snapshot)
    except GuardrailConfigError:
        _log.exception("guardrail_config.stored_snapshot_invalid")
        return GuardrailConfigSet()


def _diff_summary(changes: list[ConfigChange]) -> dict[str, Any]:
    """Summary-only diff payload for audit events (ids, never config content)."""
    by_action: dict[str, list[str]] = {"add": [], "update": [], "remove": []}
    for change in changes:
        by_action.setdefault(change.action, []).append(change.id)
    return {action: len(ids) for action, ids in by_action.items()}


async def _audit(
    session: AsyncSession,
    org_id: uuid.UUID,
    account_id: uuid.UUID | None,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Best-effort audit append — a failed audit never breaks the operation."""
    try:
        await append_audit_event(
            session,
            org_id=org_id,
            event_type=event_type,
            actor_user_id=account_id,
            resource_type="organisation",
            resource_id=org_id,
            payload_json=payload,
        )
    except Exception:
        _log.exception("guardrail_config.audit_failed")


async def _reconcile_guardrail_rows(
    session: AsyncSession,
    org_id: uuid.UUID,
    config_set: GuardrailConfigSet,
    account_id: uuid.UUID,
) -> None:
    """Reconcile the org's live guardrail rows to match *config_set*.

    The org-level config is bound to EVERY (non-deleted) pipeline so the
    shipped interception seam — which loads ``eval_type='guardrail'`` rows per
    ``pipeline_id`` — enforces it at the ingestion edge of every run. Rows are
    keyed by the stable config ``id`` (stored as the eval ``name``), making
    re-imports idempotent: present ids are upserted, absent ids are deleted.
    """
    proposed_by_id = {item.id: item for item in config_set.guardrails}
    pipelines = (
        (
            await session.execute(
                select(Pipeline).where(
                    Pipeline.organisation_id == org_id,
                    Pipeline.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for pipeline in pipelines:
        rows = (
            (
                await session.execute(
                    select(EvalDefinitionRow).where(
                        EvalDefinitionRow.pipeline_id == pipeline.id,
                        EvalDefinitionRow.organisation_id == org_id,
                        EvalDefinitionRow.eval_type == "guardrail",
                    )
                )
            )
            .scalars()
            .all()
        )
        rows_by_name = {row.name: row for row in rows}
        for gid, item in proposed_by_id.items():
            row = rows_by_name.get(gid)
            config_json = to_eval_config(item)
            if row is None:
                session.add(
                    EvalDefinitionRow(
                        organisation_id=org_id,
                        pipeline_id=pipeline.id,
                        node_id=None,
                        name=gid,
                        eval_type="guardrail",
                        config_json=config_json,
                        failure_behaviour="warn",
                        account_id=account_id,
                    )
                )
            else:
                row.config_json = config_json
        for name, row in rows_by_name.items():
            # Only delete rows the config-as-code layer owns. Node-bound
            # guardrails authored via the graph-save flow (node_id set) are
            # NOT config-as-code's to reconcile — deleting them would silently
            # strip guardrails the evals API bound to pipeline nodes.
            if name not in proposed_by_id and row.node_id is None:
                await session.delete(row)
    await session.flush()


def _current_status(pin: GuardrailPin | None, drifted: bool) -> str:
    if pin is not None and pin.status == "proposed":
        return "proposed"
    return "drift" if drifted else "clean"


async def _load_pin(session: AsyncSession, org_id: uuid.UUID) -> GuardrailPin | None:
    """Load the org's pin, converting the stored dict to a domain object."""
    return GuardrailPin.from_json(org_id, await get_guardrail_pin(session, org_id))


async def _store_pin(session: AsyncSession, org_id: uuid.UUID, pin: GuardrailPin) -> None:
    """Persist the pin as its stored dict (the DB layer is storage-only)."""
    await set_guardrail_pin(session, org_id, pin.to_json())


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=GuardrailConfigResponse)
@handle_db_errors("guardrail_config.get")
async def get_guardrail_config(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("eval.list"),
) -> GuardrailConfigResponse:
    """Export the org's applied guardrail config as YAML + pin metadata."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        pin = await _load_pin(session, principal.organisation_id)
        definitions = await _load_guardrail_definitions(session, principal.organisation_id)
        drifted = check_guardrail_drift(definitions, pin)
        if pin is None:
            return GuardrailConfigResponse(
                config_yaml=dump_config_set(GuardrailConfigSet()),
                hash=None,
                applied_at=None,
                # Live guardrail rows without a pin (e.g. authored via the
                # graph-save flow) mean the layer is out of sync — report
                # drift so this endpoint agrees with GET /drift.
                status="drift" if drifted else "clean",
            )
        return GuardrailConfigResponse(
            config_yaml=pin.serialized_snapshot or dump_config_set(GuardrailConfigSet()),
            hash=pin.applied_hash,
            applied_at=pin.applied_at,
            status=_current_status(pin, drifted),
        )


@router.post("/propose", response_model=GuardrailProposalResponse)
@handle_db_errors("guardrail_config.propose")
async def propose_guardrail_config(
    req: ProposeGuardrailConfigRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("eval.definition.create"),
) -> GuardrailProposalResponse:
    """Validate + hash a proposed config set, diff it, and store the proposal."""
    try:
        proposed = load_config_set(req.config_yaml)
    except GuardrailConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from None

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        pin = await _load_pin(session, principal.organisation_id)
        current = _applied_config_set(pin)
        changes = diff_config_sets(current, proposed)
        proposed_hash = hash_config_set(proposed)
        now = utc_now_iso()

        if pin is None:
            pin = GuardrailPin(
                org_id=principal.organisation_id,
                status="proposed",
                proposed_hash=proposed_hash,
                proposed_at=now,
                serialized_proposal=req.config_yaml,
            )
        else:
            pin.status = "proposed"
            pin.proposed_hash = proposed_hash
            pin.proposed_at = now
            pin.serialized_proposal = req.config_yaml
        await _store_pin(session, principal.organisation_id, pin)

        await _audit(
            session,
            principal.organisation_id,
            principal.account_id,
            "guardrail_config.proposed",
            {
                "hash": proposed_hash,
                "guardrail_count": len(proposed.guardrails),
                "diff": _diff_summary(changes),
            },
        )

    return GuardrailProposalResponse(
        proposed=True,
        hash=proposed_hash,
        diff=[GuardrailChangeResponse(**change.to_dict()) for change in changes],
        status="proposed",
    )


@router.post("/apply", response_model=GuardrailApplyResponse)
@handle_db_errors("guardrail_config.apply")
async def apply_guardrail_config(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("eval.definition.create"),
) -> GuardrailApplyResponse:
    """Apply the pending proposal — the approve/merge step (operator only).

    Reconciles the live ``EvalDefinition`` rows to the proposed set and moves
    the pin to a clean applied state. 409 when there is no proposal to apply.
    """
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        pin = await _load_pin(session, principal.organisation_id)
        if pin is None or pin.status != "proposed" or not pin.serialized_proposal:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="No guardrail config proposal to apply. Propose one first.",
            )
        try:
            proposed = load_config_set(pin.serialized_proposal)
        except GuardrailConfigError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Stored proposal is invalid: {exc}",
            ) from None

        await _reconcile_guardrail_rows(session, principal.organisation_id, proposed, principal.account_id)

        applied_hash = pin.proposed_hash or hash_config_set(proposed)
        now = utc_now_iso()
        pin.applied_hash = applied_hash
        pin.applied_at = now
        pin.serialized_snapshot = pin.serialized_proposal
        pin.proposed_hash = None
        pin.proposed_at = None
        pin.serialized_proposal = None
        pin.status = "clean"
        await _store_pin(session, principal.organisation_id, pin)

        await _audit(
            session,
            principal.organisation_id,
            principal.account_id,
            "guardrail_config.applied",
            {
                "hash": applied_hash,
                "guardrail_count": len(proposed.guardrails),
            },
        )

    return GuardrailApplyResponse(applied=True, hash=applied_hash, applied_at=now, status="clean")


@router.post("/reject", response_model=GuardrailRejectResponse)
@handle_db_errors("guardrail_config.reject")
async def reject_guardrail_config(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("eval.definition.create"),
) -> GuardrailRejectResponse:
    """Discard the pending proposal (operator only). 409 when none exists."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        pin = await _load_pin(session, principal.organisation_id)
        if pin is None or pin.status != "proposed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="No guardrail config proposal to reject.",
            )
        rejected_hash = pin.proposed_hash
        pin.proposed_hash = None
        pin.proposed_at = None
        pin.serialized_proposal = None
        pin.status = "clean"
        await _store_pin(session, principal.organisation_id, pin)

        await _audit(
            session,
            principal.organisation_id,
            principal.account_id,
            "guardrail_config.rejected",
            {"hash": rejected_hash},
        )

    return GuardrailRejectResponse(rejected=True, status="clean")


@router.get("/drift", response_model=GuardrailDriftResponse)
@handle_db_errors("guardrail_config.drift")
async def get_guardrail_drift(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("eval.list"),
) -> GuardrailDriftResponse:
    """Recompute drift between the live rows and the applied pin."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        pin = await _load_pin(session, principal.organisation_id)
        definitions = await _load_guardrail_definitions(session, principal.organisation_id)
        drifted = check_guardrail_drift(definitions, pin)
        current_hash = hash_config_set(build_config_set_from_definitions(definitions))
        applied_hash = pin.applied_hash if pin else None
        # The response reflects the pin's OWNED state: a pending "proposed" pin
        # stays "proposed" even while the live rows drift, so /drift and
        # /config agree.
        status = _current_status(pin, drifted)

        # Persist status transitions on the pin and audit the drift entry so
        # the audit trail records WHEN drift began, not every poll. Only the
        # "clean" <-> "drift" transition is owned by drift polling — a pending
        # proposal ("proposed") is preserved so apply/reject still work.
        if pin is not None:
            if drifted and pin.status == "clean":
                pin.status = "drift"
                await _store_pin(session, principal.organisation_id, pin)
                await _audit(
                    session,
                    principal.organisation_id,
                    principal.account_id,
                    "guardrail_config.drift_detected",
                    {"current_hash": current_hash, "applied_hash": applied_hash},
                )
            elif not drifted and pin.status == "drift":
                pin.status = "clean"
                await _store_pin(session, principal.organisation_id, pin)

    return GuardrailDriftResponse(status=status, current_hash=current_hash, applied_hash=applied_hash)
