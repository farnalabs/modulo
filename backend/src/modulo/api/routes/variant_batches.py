"""Variant batch API — list, detail, soft-delete, re-fire (FAR-775).

Routes match the TypeScript contract in frontend/src/lib/api/variantBatches.ts.
"""

import logging
import uuid
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, select

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.core.node_output_split import node_return
from modulo.db.crud.eval_run import non_guardrail_eval_results_clause
from modulo.db.crud.run_node_outputs import read_run_node_outputs_raw
from modulo.db.crud.variant_group import (
    get_all_state_batch_ids,
    get_batch_runs,
    get_batch_state,
    get_variant_group,
    list_batch_runs_for_batch_ids,
    list_batch_states,
    run_variant_batch,
    soft_delete_batch_state,
)
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.run import Run
from modulo.db.models.run import Run as RunModel
from modulo.db.rls import set_rls_org, set_rls_user_context

router = APIRouter(prefix="/api/v1/variant-batches", tags=["variant-batches"])

_log = logging.getLogger(__name__)

_CODE_LIST = "variant_batches.list"
_CODE_DETAIL = "variant_batches.detail"
_CODE_DELETE = "variant_batches.delete"
_CODE_RE_FIRE = "variant_batches.refire"

MSG_BATCH_NOT_FOUND = "Variant batch not found"


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------

# Statuses treated as terminal for batch completion calculation.
_COMPLETE = {"complete"}
_FAILED = {"failed", "eval_failed"}
_CANCELLED = {"cancelled"}


def _compute_batch_status(run_statuses: list[str]) -> str:
    """Derive VariantBatchStatus from the set of run statuses."""
    if not run_statuses:
        return "pending"
    statuses = set(run_statuses)
    if statuses <= _COMPLETE:
        return "complete"
    if statuses <= _CANCELLED:
        return "cancelled"
    if statuses & _FAILED:
        return "failed"
    if statuses & _COMPLETE:
        return "partial"
    if statuses <= {"pending"}:
        return "pending"
    return "running"


async def _load_run_blobs(
    session: Any,
    run: Run,
    *,
    org_id: uuid.UUID,
) -> dict[str, Any] | None:
    """Read per-node return dicts from the node-output blob store.

    Returns ``{node_id: return_value}`` or ``None`` when the run has no
    stored outputs.
    """
    blobs = await read_run_node_outputs_raw(
        session,
        run_id=run.id,
        organisation_id=org_id,
    )
    outputs = blobs.outputs
    if not isinstance(outputs, dict) or not outputs:
        return None
    result: dict[str, Any] = {}
    for node_id in outputs:
        val = node_return(outputs, blobs.telemetry, node_id)
        if val is not None:
            result[node_id] = val
    return result or None


async def _batch_load_eval_results(
    session: Any,
    run_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[dict[str, Any]]]:
    """Batch-load eval results for multiple runs in ONE query.

    Excludes guardrail rows per the eval_results consumer contract.
    Returns ``{run_id: [{eval_id, node_id, passed, score, detail}, ...]}``.
    """
    if not run_ids:
        return {}

    er_result = await session.execute(
        select(EvalResult)
        .where(
            EvalResult.run_id.in_(run_ids),
            non_guardrail_eval_results_clause(),
        )
        .order_by(EvalResult.run_id, EvalResult.evaluated_at)
    )
    eval_results_by_run: dict[uuid.UUID, list[dict[str, Any]]] = defaultdict(list)
    for er in er_result.scalars().all():
        run_id = uuid.UUID(str(er.run_id))
        eval_results_by_run[run_id].append(
            {
                "eval_id": str(er.eval_id),
                "node_id": str(er.node_id) if er.node_id is not None else None,
                "passed": er.passed,
                "score": er.score,
                "detail": er.detail,
            }
        )
    return dict(eval_results_by_run)


def _run_to_variant_run(
    run: Run,
    *,
    eval_stats: dict[uuid.UUID, tuple[int, int]],
    eval_results: list[dict[str, Any]],
    node_outputs: dict[str, Any] | None,
) -> dict[str, Any]:
    """Map a Run ORM object to the frontend VariantBatchRun shape."""
    frozen: dict[str, Any] = {}
    raw = run.variant_config_snapshot
    if isinstance(raw, dict):
        frozen = raw
    snapshot_label = frozen.get("snapshot_id") or frozen.get("variant_name")
    overrides = frozen.get("run_context_overrides") or {}
    input_label = str(overrides) if overrides else None

    total, passed = eval_stats.get(run.id, (0, 0))

    return {
        "run_id": str(run.id),
        "variant_name": frozen.get("variant_name") or "unknown",
        "snapshot_label": str(snapshot_label) if snapshot_label else None,
        "input_label": input_label,
        "run_status": run.status,
        "pass_rate": round(passed / total, 4) if total else None,
        "total_cost_usd": run.total_cost_usd,
        "total_tokens": run.total_tokens,
        "eval_results": eval_results,
        "node_outputs": node_outputs,
    }


async def _batch_load_eval_stats(
    session: Any,
    run_ids: list[uuid.UUID],
) -> dict[uuid.UUID, tuple[int, int]]:
    """Batch-load eval pass-rate stats for multiple runs in ONE query.

    Excludes guardrail rows per the eval_results consumer contract.
    Returns ``{run_id: (total, passed)}``.
    """
    if not run_ids:
        return {}

    er_result = await session.execute(
        select(
            EvalResult.run_id,
            func.count(EvalResult.id),
            func.sum(case((EvalResult.passed, 1), else_=0)),
        )
        .where(
            EvalResult.run_id.in_(run_ids),
            non_guardrail_eval_results_clause(),
        )
        .group_by(EvalResult.run_id)
    )
    eval_stats: dict[uuid.UUID, tuple[int, int]] = {}
    for run_id, total, passed in er_result.all():
        eval_stats[uuid.UUID(str(run_id))] = (int(total or 0), int(passed or 0))
    return eval_stats


async def _load_batch_detail(
    session: Any,
    *,
    batch_id: uuid.UUID,
    org_id: uuid.UUID,
) -> dict[str, Any]:
    """Load batch detail from the variant_batch_state row + runs table.

    Falls back to synthesizing from runs when no state row exists (legacy
    batches created before FAR-775).
    """
    state = await get_batch_state(session, batch_id=batch_id, org_id=org_id)
    runs = await get_batch_runs(session, org_id=org_id, batch_id=batch_id)

    if not runs and state is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MSG_BATCH_NOT_FOUND)

    run_statuses = [r.status for r in runs]

    # Batch name: prefer state row; fall back to first run's variant_name.
    batch_name = ""
    if state and state.name:
        batch_name = state.name
    elif runs:
        frozen = runs[0].variant_config_snapshot or {}
        batch_name = f"{frozen.get('variant_name', 'unknown')} comparison"

    # Pipeline name: state row has pipeline_id but no name column — resolve
    # from pipeline if available.
    pipeline_name = None
    pipeline_id = state.pipeline_id if state else None
    if pipeline_id is None and runs:
        pipeline_id = runs[0].pipeline_id

    # Batch-level timestamps: use state row when present, else first/last run.
    if state:
        created_at = state.created_at
        updated_at = state.updated_at
    elif runs:
        created_at = runs[0].created_at
        updated_at = runs[-1].completed_at or runs[-1].created_at
    else:
        created_at = None
        updated_at = None

    # Eval stats: one grouped query across the whole batch (no N+1).
    run_ids = [r.id for r in runs]
    eval_stats = await _batch_load_eval_stats(session, run_ids)

    # Batch-load eval results for all runs in one query (no N+1).
    eval_results_by_run = await _batch_load_eval_results(session, run_ids)

    # Load node outputs for each run (N queries but each is a simple blob read).
    variant_runs: list[dict[str, Any]] = []
    for run in runs:
        node_outputs = await _load_run_blobs(session, run, org_id=org_id)
        variant_runs.append(
            _run_to_variant_run(
                run,
                eval_stats=eval_stats,
                eval_results=eval_results_by_run.get(run.id, []),
                node_outputs=node_outputs,
            )
        )

    return {
        "batch_id": str(batch_id),
        "name": batch_name,
        "pipeline_id": str(pipeline_id) if pipeline_id else "",
        "pipeline_name": pipeline_name,
        "status": _compute_batch_status(run_statuses),
        "created_at": created_at.isoformat() if created_at else "",
        "updated_at": updated_at.isoformat() if updated_at else "",
        "runs": variant_runs,
    }


def _summarise_batch_runs(
    run_statuses: list[str],
    *,
    runs: list[Run],
    run_count: int | None = None,
) -> tuple[str, int]:
    """Derive batch status and run_count from run data."""
    return _compute_batch_status(run_statuses), run_count if run_count is not None else len(runs)


# ---------------------------------------------------------------------------
# GET /api/v1/variant-batches — paginated list
# ---------------------------------------------------------------------------


@router.get("", response_model=None)
@handle_db_errors(_CODE_LIST)
async def list_batches(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _session: Any = Depends(get_db_session),
    _principal: TenantPrincipal = require_permission("variant.list"),
) -> dict[str, Any]:
    """List variant batches for the current org ("My comparisons").

    Items come from variant_batch_state rows (FAR-775) when present, and
    legacy batches are synthesised from the runs table. The response shape
    matches the frontend VariantBatchListResponse.
    """
    async with _session.begin():
        await set_rls_org(_session, _principal.organisation_id)
        await set_rls_user_context(_session, _principal.account_id, _principal.org_role)
        org_id = _principal.organisation_id

        # Phase 1: known batches from the state table.
        states_items, states_total = await list_batch_states(_session, org_id=org_id, page=page, page_size=page_size)

        # Collect batch_ids from ALL state rows (org-wide) for legacy exclusion.
        # Using only the current page's IDs would double-count state batches
        # on pages 2+ in the legacy scan (FAR-775).
        all_state_ids = await get_all_state_batch_ids(_session, org_id=org_id)

        # Batch-load runs for the current page's state-row batch_ids (M7).
        known_ids: list[uuid.UUID] = [st.batch_id for st in states_items]
        all_runs_by_batch = await list_batch_runs_for_batch_ids(_session, org_id=org_id, batch_ids=known_ids)

        # Build summaries from state rows.
        summaries: list[dict[str, Any]] = []
        for st in states_items:
            batch_runs = all_runs_by_batch.get(st.batch_id, [])
            run_statuses = [r.status for r in batch_runs]

            batch_name = st.name or ""
            pipeline_name = None
            pipeline_id = st.pipeline_id
            if batch_runs and not pipeline_name:
                pipeline_id = pipeline_id or batch_runs[0].pipeline_id

            batch_status, run_count = _summarise_batch_runs(run_statuses, runs=batch_runs)
            summaries.append(
                {
                    "batch_id": str(st.batch_id),
                    "name": batch_name,
                    "pipeline_name": pipeline_name,
                    "status": batch_status,
                    "run_count": run_count,
                    "created_at": st.created_at.isoformat() if st.created_at else "",
                }
            )

        # Phase 2: legacy batches not in the state table.
        # Scan runs for batch_ids not yet known — these predate FAR-775.

        # Count legacy batches for the true total.
        # Exclude ALL state-table batch_ids (org-wide), not just the current
        # page's — page 2+ state rows would otherwise be double-counted.
        legacy_count_result = await _session.execute(
            select(func.count(func.distinct(RunModel.batch_id))).where(
                RunModel.organisation_id == org_id,
                RunModel.batch_id.isnot(None),
                RunModel.batch_id.notin_(all_state_ids) if all_state_ids else RunModel.batch_id.isnot(None),
            )
        )
        legacy_total_count = legacy_count_result.scalar_one() or 0

        legacy_result = await _session.execute(
            select(RunModel.batch_id, func.count(RunModel.id))
            .where(
                RunModel.organisation_id == org_id,
                RunModel.batch_id.isnot(None),
                RunModel.batch_id.notin_(all_state_ids) if all_state_ids else RunModel.batch_id.isnot(None),
            )
            .group_by(RunModel.batch_id)
            .order_by(func.min(RunModel.created_at).desc())
            .limit(max(0, page_size - len(summaries)))
        )
        legacy_batch_ids = [uuid.UUID(str(bid)) for bid, _ in legacy_result.all()]

        # Batch-load runs for all legacy batch_ids in ONE query (M7).
        legacy_runs_by_batch = await list_batch_runs_for_batch_ids(_session, org_id=org_id, batch_ids=legacy_batch_ids)

        for bid in legacy_batch_ids:
            if bid in all_state_ids:
                continue
            legacy_runs = legacy_runs_by_batch.get(bid, [])
            run_statuses = [r.status for r in legacy_runs]
            frozen_first: dict[str, Any] = {}
            if legacy_runs:
                raw = legacy_runs[0].variant_config_snapshot
                if isinstance(raw, dict):
                    frozen_first = raw
            batch_name = f"{frozen_first.get('variant_name', 'unknown')} comparison"
            created_at = legacy_runs[0].created_at if legacy_runs else None
            batch_status, run_count = _summarise_batch_runs(run_statuses, runs=legacy_runs)
            summaries.append(
                {
                    "batch_id": str(bid),
                    "name": batch_name,
                    "pipeline_name": None,
                    "status": batch_status,
                    "run_count": run_count,
                    "created_at": created_at.isoformat() if created_at else "",
                }
            )

        total_count = states_total + legacy_total_count

        return {
            "items": summaries,
            "total": total_count,
        }


# ---------------------------------------------------------------------------
# GET /api/v1/variant-batches/{batch_id} — full detail + runs
# ---------------------------------------------------------------------------


@router.get("/{batch_id}", response_model=None)
@handle_db_errors(_CODE_DETAIL)
async def get_batch(
    batch_id: uuid.UUID,
    _session: Any = Depends(get_db_session),
    _principal: TenantPrincipal = require_permission("variant.list"),
) -> dict[str, Any]:
    """Fetch a single batch's detail + runs by batch_id."""
    async with _session.begin():
        await set_rls_org(_session, _principal.organisation_id)
        await set_rls_user_context(_session, _principal.account_id, _principal.org_role)
        return await _load_batch_detail(
            _session,
            batch_id=batch_id,
            org_id=_principal.organisation_id,
        )


# ---------------------------------------------------------------------------
# DELETE /api/v1/variant-batches/{batch_id} — soft-delete
# ---------------------------------------------------------------------------


@router.delete("/{batch_id}", response_model=None)
@handle_db_errors(_CODE_DELETE)
async def delete_batch(
    batch_id: uuid.UUID,
    _session: Any = Depends(get_db_session),
    _principal: TenantPrincipal = require_permission("variant.delete"),
) -> dict[str, str]:
    """Soft-delete a batch: hides it from 'My comparisons' but keeps
    the compare URL + run links working.
    """
    async with _session.begin():
        await set_rls_org(_session, _principal.organisation_id)
        await set_rls_user_context(_session, _principal.account_id, _principal.org_role)
        org_id = _principal.organisation_id
        found = await soft_delete_batch_state(_session, batch_id=batch_id, org_id=org_id)
        if not found:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MSG_BATCH_NOT_FOUND)
        return {}


# ---------------------------------------------------------------------------
# POST /api/v1/variant-batches/{batch_id}/re-fire — re-fire batch
# ---------------------------------------------------------------------------


@router.post("/{batch_id}/re-fire", response_model=None)
@handle_db_errors(_CODE_RE_FIRE)
async def re_fire_batch(
    batch_id: uuid.UUID,
    _session: Any = Depends(get_db_session),
    _principal: TenantPrincipal = require_permission("variant.run"),
) -> dict[str, Any]:
    """Re-fire a batch from its frozen definition.

    Loads the original batch state, resolves the variant group + pipeline,
    and fires a fresh batch with the same input payload. Returns the new
    batch detail (with new batch_id).
    """
    async with _session.begin():
        await set_rls_org(_session, _principal.organisation_id)
        await set_rls_user_context(_session, _principal.account_id, _principal.org_role)
        org_id = _principal.organisation_id

        state = await get_batch_state(_session, batch_id=batch_id, org_id=org_id)
        if state is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MSG_BATCH_NOT_FOUND)

        # Re-resolve the variant group from the original snapshot.
        group_id = state.variant_group_id
        if group_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Batch has no source variant group — cannot re-fire",
            )

        group = await get_variant_group(_session, group_id=group_id)
        if group is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Source variant group no longer exists",
            )

        # M9: org defense-in-depth — verify group belongs to this org.
        if group.organisation_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=MSG_BATCH_NOT_FOUND,
            )

        results = await run_variant_batch(
            _session,
            org_id=org_id,
            group=group,
            input_payload=state.input_payload or {},
            account_id=_principal.account_id,
            trigger_type="manual",
        )

        if results is None or not results:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="variant_group_quota_exceeded",
            )

        # C2: Extract new batch_id from the first run's snapshot — hard fail
        # if extraction fails (never fall back to old batch_id).
        first_run_snap: dict[str, Any] = {}
        raw_first = results[0].get("frozen_snapshot") or results[0].get("variant")
        if isinstance(raw_first, dict):
            first_run_snap = raw_first
        new_batch_id_raw = first_run_snap.get("batch_id")
        if not new_batch_id_raw:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="re-fire could not determine the new batch id",
            )
        new_batch_id = uuid.UUID(str(new_batch_id_raw))

        return await _load_batch_detail(
            _session,
            batch_id=new_batch_id,
            org_id=org_id,
        )
