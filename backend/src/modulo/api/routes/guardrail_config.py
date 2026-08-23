"""Guardrail config-as-code REST API (FAR-219 T3).

URLs (mounted under ``/api/v1/guardrails/config``):

    GET    /api/v1/guardrails/config         — export the applied config as YAML
    POST   /api/v1/guardrails/config/propose — validate + hash + diff a proposal
    POST   /api/v1/guardrails/config/apply   — apply the pending proposal (approve/merge)
    POST   /api/v1/guardrails/config/reject  — discard the pending proposal
    GET    /api/v1/guardrails/config/drift   — recompute drift vs the applied pin

The workflow is git-style: **propose** → **diff** → **apply**. Apply/reject
are the admin-only "merge"/"discard" steps that reconcile the live
``eval_type='guardrail'`` ``EvalDefinition`` rows the shipped interception seam
consumes — gated by the same admin check as the direct eval-definition API;
the config-as-code layer is an authoring/source-of-truth seam on top, never a
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
from modulo.api.dependencies import deny_break_glass_mint, get_db_session, require_permission
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
    mask_config_set,
    to_eval_config,
    utc_now_iso,
)
from modulo.db.crud.guardrail_config import get_guardrail_pin, load_pipeline_guardrail_rows, set_guardrail_pin
from modulo.db.models.eval_definition import EvalDefinition as EvalDefinitionRow
from modulo.db.models.pipeline import Pipeline
from modulo.db.rls import set_rls_org, set_rls_user_context

_CODE_EVAL_DEFINITION_CREATE = "eval.definition.create"
_CODE_GUARDRAIL_MANAGE = "guardrail.manage"


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
    """Load the org's LAYER-OWNED guardrail rows as engine DTOs.

    Only ``node_id IS NULL`` rows are included — the org-level rows that apply
    creates/updates/deletes. Node-bound rows authored via the graph-save flow
    are deliberately excluded so the drift/export boundary matches the apply
    ownership boundary: a freshly applied config with node-bound guardrails
    present must read ``clean``, never permanent ``drift``. The interception
    seam (``db/crud/run.py``) loads its own rows independently and is
    unaffected — node-bound guardrails are still enforced at the edge.
    """
    rows = (
        (
            await session.execute(
                select(EvalDefinitionRow).where(
                    EvalDefinitionRow.organisation_id == org_id,
                    EvalDefinitionRow.eval_type == "guardrail",
                    EvalDefinitionRow.node_id.is_(None),
                    # FAR-309 PR B: a soft-deleted guardrail is no longer a live
                    # row — it must not appear in the export/drift surface (its
                    # removal IS the drift, surfaced via the interception skip
                    # path instead).
                    EvalDefinitionRow.deleted_at.is_(None),
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


def _effective_config_set(pin: GuardrailPin | None) -> GuardrailConfigSet:
    """The config set a reader sees: the PROPOSED set while a proposal is
    pending (that is what the operator is reviewing), else the last APPLIED
    snapshot. Returns the empty set when nothing is pinned/applied."""
    if pin is None:
        return GuardrailConfigSet()
    if pin.status == "proposed":
        serialized = pin.serialized_proposal or pin.serialized_snapshot
    else:
        serialized = pin.serialized_snapshot
    if not serialized:
        return GuardrailConfigSet()
    try:
        return load_config_set(serialized)
    except GuardrailConfigError:
        _log.exception("guardrail_config.effective_config_invalid")
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
) -> list[str]:
    """Reconcile the org's live guardrail rows to match *config_set*.

    The org-level config is bound to EVERY (non-deleted) pipeline so the
    shipped interception seam — which loads ``eval_type='guardrail'`` rows per
    ``pipeline_id`` — enforces it at the ingestion edge of every run. Rows are
    keyed by the stable config ``id`` (stored as the eval ``name``), making
    re-imports idempotent: present ids are upserted, absent ids are deleted.

    Returns the list of proposed ids that collided with a node-bound row and
    therefore could NOT be materialized. Collisions are detected BEFORE any
    mutation — a single colliding pipeline fails the whole apply (the caller
    turns this into a 409), so a collision is a clean no-op, never a partial
    reconcile that would leave the applied pin instantly reporting drift.
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
    pipelines_rows: list[tuple[Pipeline, dict[str, EvalDefinitionRow]]] = []
    colliding: list[str] = []
    for pipeline in pipelines:
        rows = await load_pipeline_guardrail_rows(
            session,
            pipeline_id=pipeline.id,
            organisation_id=org_id,
        )
        rows_by_name = {row.name: row for row in rows}
        pipelines_rows.append((pipeline, rows_by_name))
        for gid in proposed_by_id:
            row = rows_by_name.get(gid)
            if row is not None and row.node_id is not None and gid not in colliding:
                colliding.append(gid)
    if colliding:
        return colliding
    for pipeline, rows_by_name in pipelines_rows:
        for gid, item in proposed_by_id.items():
            row = rows_by_name.get(gid)
            config_json = to_eval_config(
                item,
                max_guardrails_per_node=config_set.max_guardrails_per_node,
                guardrail_timeout_seconds=config_set.guardrail_timeout_seconds,
            )
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
            elif row.node_id is None:
                # Only upsert rows the config-as-code layer owns. A node-bound
                # row (graph-save flow) that collides on name must not be
                # silently clobbered — mirror the deletion path's ownership
                # check below.
                row.config_json = config_json
        for name, row in rows_by_name.items():
            # Only delete rows the config-as-code layer owns. Node-bound
            # guardrails authored via the graph-save flow (node_id set) are
            # NOT config-as-code's to reconcile — deleting them would silently
            # strip guardrails the evals API bound to pipeline nodes.
            if name not in proposed_by_id and row.node_id is None:
                await session.delete(row)
    await session.flush()
    return []


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


@router.get("")
@handle_db_errors("guardrail_config.get")
async def get_guardrail_config(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("eval.list"),
) -> GuardrailConfigResponse:
    """Export the org's applied guardrail config as YAML + pin metadata.

    The standard (non-admin) read MASKS the deny-rule internals (regex
    patterns, JSON schemas, redaction field paths) — a viewer can see the
    guardrail topology and actions without the sensitive rule bodies. Admins
    use ``GET /elevated`` (``guardrail.manage``) for the full unmasked config.
    """
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        pin = await _load_pin(session, principal.organisation_id)
        definitions = await _load_guardrail_definitions(session, principal.organisation_id)
        try:
            drifted = check_guardrail_drift(definitions, pin)
        except GuardrailConfigError as exc:
            # A legacy org-level guardrail name the config id pattern rejects
            # (spaces, >100 chars) must fail closed with a clear message — not
            # a generic validation 422 and never a 500.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from None
        if pin is None:
            return GuardrailConfigResponse(
                config_yaml=dump_config_set(mask_config_set(GuardrailConfigSet())),
                hash=None,
                applied_at=None,
                # Live guardrail rows without a pin (e.g. authored via the
                # graph-save flow) mean the layer is out of sync — report
                # drift so this endpoint agrees with GET /drift.
                status="drift" if drifted else "clean",
            )
        current_status = _current_status(pin, drifted)
        return GuardrailConfigResponse(
            config_yaml=dump_config_set(mask_config_set(_effective_config_set(pin))),
            hash=pin.applied_hash,
            applied_at=pin.applied_at,
            status=current_status,
        )


@router.get("/elevated", dependencies=[Depends(deny_break_glass_mint)])
@handle_db_errors("guardrail_config.elevated")
async def get_guardrail_config_elevated(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_GUARDRAIL_MANAGE),
) -> GuardrailConfigResponse:
    """Elevated (admin-only) export of the FULL unmasked guardrail config.

    Requires ``guardrail.manage`` (admin). Returns the actual regex patterns,
    JSON schemas, and redaction field paths that the standard read masks —
    the safety-control internals an operator needs to author/audit rules but a
    non-admin viewer must not see (FAR-309 PR A elevated read).
    """
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        pin = await _load_pin(session, principal.organisation_id)
        definitions = await _load_guardrail_definitions(session, principal.organisation_id)
        try:
            drifted = check_guardrail_drift(definitions, pin)
        except GuardrailConfigError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from None
        if pin is None:
            return GuardrailConfigResponse(
                config_yaml=dump_config_set(GuardrailConfigSet()),
                hash=None,
                applied_at=None,
                status="drift" if drifted else "clean",
            )
        current_status = _current_status(pin, drifted)
        return GuardrailConfigResponse(
            config_yaml=dump_config_set(_effective_config_set(pin)),
            hash=pin.applied_hash,
            applied_at=pin.applied_at,
            status=current_status,
        )


@router.post("/propose", dependencies=[Depends(deny_break_glass_mint)])
@handle_db_errors("guardrail_config.propose")
async def propose_guardrail_config(
    req: ProposeGuardrailConfigRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_DEFINITION_CREATE),
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


@router.post("/apply", dependencies=[Depends(deny_break_glass_mint)])
@handle_db_errors("guardrail_config.apply")
async def apply_guardrail_config(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_DEFINITION_CREATE),
) -> GuardrailApplyResponse:
    """Apply the pending proposal — the approve/merge step (admin only).

    Reconciles the live ``EvalDefinition`` rows to the proposed set and moves
    the pin to a clean applied state. 409 when there is no proposal to apply.
    Guardrails are safety controls, so the reconcile is gated by the same
    admin-only check the direct eval-definition API enforces (evals.py) — an
    operator with ``eval.definition.create`` must not be able to mutate these
    rows through the config seam when the direct API denies it.
    """
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can apply guardrail config",
        )
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
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

        colliding = await _reconcile_guardrail_rows(session, principal.organisation_id, proposed, principal.account_id)
        if colliding:
            # Fail closed with an in-band remediation signal: a proposed id
            # collides with a node-bound guardrail row, so the org-level row
            # cannot be materialized and the applied pin would report drift on
            # the very next poll. The reconcile is a clean no-op (collisions are
            # detected before any mutation) and the pin stays "proposed" so the
            # operator can re-propose a renamed id. The conflict audit commits
            # with this transaction; the 409 is raised after it so the trail is
            # not lost to the rollback.
            await _audit(
                session,
                principal.organisation_id,
                principal.account_id,
                "guardrail_config.apply_conflict",
                {"colliding_ids": sorted(colliding)},
            )
            conflict_detail = (
                "Cannot apply guardrail config: id(s) collide with node-bound guardrails: "
                + ", ".join(sorted(colliding))
                + ". Rename the colliding config id(s) and re-propose."
            )
        else:
            conflict_detail = None
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

    if conflict_detail is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=conflict_detail)

    return GuardrailApplyResponse(applied=True, hash=applied_hash, applied_at=now, status="clean")


@router.post("/reject", dependencies=[Depends(deny_break_glass_mint)])
@handle_db_errors("guardrail_config.reject")
async def reject_guardrail_config(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_DEFINITION_CREATE),
) -> GuardrailRejectResponse:
    """Discard the pending proposal (admin only). 409 when none exists.

    Rejecting discards a proposed safety-control change, so it is gated by the
    same admin-only check as apply and the direct eval-definition API.
    """
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can reject guardrail config",
        )
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


@router.get("/drift")
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
        try:
            drifted = check_guardrail_drift(definitions, pin)
            current_hash = hash_config_set(build_config_set_from_definitions(definitions))
        except GuardrailConfigError as exc:
            # Same fail-closed guarantee as GET /config: a legacy org-level
            # name the config id pattern rejects must surface a clear 422, not
            # a generic validation error and never a 500.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from None
        applied_hash = pin.applied_hash if pin else None
        # The response reflects the pin's OWNED state: a pending "proposed" pin
        # stays "proposed" even while the live rows drift, so /drift and
        # /config agree.
        current_status = _current_status(pin, drifted)

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

    return GuardrailDriftResponse(status=current_status, current_hash=current_hash, applied_hash=applied_hash)
