"""Variant batch API — list, detail, soft-delete, re-fire (FAR-775).

Routes match the TypeScript contract in frontend/src/lib/api/variantBatches.ts.
"""

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_FEATURE_NOT_AVAILABLE
from modulo.api.dependencies import get_db_session, require_permission
from modulo.core.node_output_split import node_return
from modulo.db.crud.run_node_outputs import read_run_node_outputs_raw
from modulo.db.crud.variant_group import (
    get_batch_runs,
    get_batch_state,
    list_batch_states,
    soft_delete_batch_state,
)
from modulo.db.models.run import Run

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

# Map Run.status → VariantRunStatus (the frontend's VariantRunStatus union).
_RUN_STATUS_MAP: dict[str, str] = {
    "pending": "pending",
    "running": "running",
    "awaiting_human": "awaiting_human",
    "claimed": "claimed",
    "hitl_parked": "hitl_parked",
    "complete": "complete",
    "failed": "failed",
    "cancelled": "cancelled",
    "eval_failed": "eval_failed",
    "stalled": "stalled",
    "budget_exceeded": "budget_exceeded",
}

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
    session: AsyncSession,
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


def _run_to_variant_run(
    run: Run,
    *,
    eval_stats: dict[uuid.UUID, tuple[int, int]],
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
    status_str = _RUN_STATUS_MAP.get(run.status, run.status)

    return {
        "run_id": str(run.id),
        "variant_name": frozen.get("variant_name") or "unknown",
        "snapshot_label": str(snapshot_label) if snapshot_label else None,
        "input_label": input_label,
        "run_status": status_str,
        "pass_rate": round(passed / total, 4) if total else None,
        "total_cost_usd": run.total_cost_usd,
        "total_tokens": run.total_tokens,
        "eval_results": [
            {
                "eval_id": str(er.eval_id),
                "node_id": er.node_id,
                "passed": er.passed,
                "score": er.score,
                "detail": er.detail,
            }
            for er in run._eval_results
        ]
        if hasattr(run, "_eval_results") and run._eval_results
        else [],
        "node_outputs": node_outputs,
    }


async def _load_batch_detail(
    session: AsyncSession,
    *,
    batch_id: uuid.UUID,
    org_id: uuid.UUID,
) -> dict[str, Any]:
    """Load batch detail from the variant_batch_state row + runs table.

    Falls back to synthesizing from runs when no state row exists (legacy
    batches created before FAR-775).
    """
    from sqlalchemy import case, func, select

    from modulo.db.models.eval_result import EvalResult

    state = await get_batch_state(session, batch_id=batch_id, org_id=org_id)
    runs = await get_batch_runs(session, org_id=org_id, batch_id=batch_id)

    if not runs and state is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MSG_BATCH_NOT_FOUND)

    run_statuses = [_RUN_STATUS_MAP.get(r.status, r.status) for r in runs]

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
    eval_stats: dict[uuid.UUID, tuple[int, int]] = {}
    if run_ids:
        er_result = await session.execute(
            select(
                EvalResult.run_id,
                func.count(EvalResult.id),
                func.sum(case((EvalResult.passed, 1), else_=0)),
            )
            .where(EvalResult.run_id.in_(run_ids))
            .group_by(EvalResult.run_id)
        )
        for run_id, total, passed in er_result.all():
            eval_stats[uuid.UUID(str(run_id))] = (int(total or 0), int(passed or 0))

    # Load node outputs for each run (N queries but each is a simple blob read).
    variant_runs: list[dict[str, Any]] = []
    for run in runs:
        node_outputs = await _load_run_blobs(session, run, org_id=org_id)
        variant_runs.append(
            _run_to_variant_run(
                run,
                eval_stats=eval_stats,
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


# ---------------------------------------------------------------------------
# GET /api/v1/variant-batches — paginated list
# ---------------------------------------------------------------------------


@router.get("", response_model=None)
async def list_batches(
    request: Request,
    page: int = 1,
    page_size: int = 20,
    _session: AsyncSession = Depends(get_db_session),
    _principal: Any = require_permission("variant.list"),
) -> dict[str, Any]:
    """List variant batches for the current org ("My comparisons").

    Items come from variant_batch_state rows (FAR-775) when present, and
    legacy batches are synthesised from the runs table. The response shape
    matches the frontend VariantBatchListResponse.
    """
    try:
        async with _session.begin():
            org_id = _principal.organisation_id

            from sqlalchemy import func, select

            # Phase 1: known batches from the state table.
            states_items, states_total = await list_batch_states(
                _session, org_id=org_id, page=page, page_size=page_size
            )

            # Build summaries from state rows.
            summaries: list[dict[str, Any]] = []
            known_ids: set[uuid.UUID] = set()
            for st in states_items:
                known_ids.add(st.batch_id)
                runs = await get_batch_runs(_session, org_id=org_id, batch_id=st.batch_id)
                run_statuses = [_RUN_STATUS_MAP.get(r.status, r.status) for r in runs]

                batch_name = st.name or ""
                pipeline_name = None
                pipeline_id = st.pipeline_id
                if runs and not pipeline_name:
                    pipeline_id = pipeline_id or runs[0].pipeline_id

                summaries.append(
                    {
                        "batch_id": str(st.batch_id),
                        "name": batch_name,
                        "pipeline_name": pipeline_name,
                        "status": _compute_batch_status(run_statuses),
                        "run_count": len(runs),
                        "created_at": st.created_at.isoformat() if st.created_at else "",
                    }
                )

            # Phase 2: legacy batches not in the state table.
            # Scan runs for batch_ids not yet known — these predate FAR-775.
            from modulo.db.models.run import Run as RunModel

            legacy_result = await _session.execute(
                select(RunModel.batch_id, func.count(RunModel.id))
                .where(
                    RunModel.organisation_id == org_id,
                    RunModel.batch_id.isnot(None),
                    RunModel.batch_id.notin_(known_ids) if known_ids else RunModel.batch_id.isnot(None),
                )
                .group_by(RunModel.batch_id)
                .order_by(func.min(RunModel.created_at).desc())
                .limit(max(0, page_size - len(summaries)))
            )
            for bid, run_count in legacy_result.all():
                bid_uuid = uuid.UUID(str(bid))
                if bid_uuid in known_ids:
                    continue
                legacy_runs = await get_batch_runs(_session, org_id=org_id, batch_id=bid_uuid)
                run_statuses = [_RUN_STATUS_MAP.get(r.status, r.status) for r in legacy_runs]
                frozen_first: dict[str, Any] = {}
                if legacy_runs:
                    raw = legacy_runs[0].variant_config_snapshot
                    if isinstance(raw, dict):
                        frozen_first = raw
                batch_name = f"{frozen_first.get('variant_name', 'unknown')} comparison"
                created_at = legacy_runs[0].created_at if legacy_runs else None
                summaries.append(
                    {
                        "batch_id": str(bid_uuid),
                        "name": batch_name,
                        "pipeline_name": None,
                        "status": _compute_batch_status(run_statuses),
                        "run_count": run_count,
                        "created_at": created_at.isoformat() if created_at else "",
                    }
                )

            total_count = states_total  # legacy scan is best-effort on top of page

            return {
                "items": summaries,
                "total": total_count,
            }

    except IntegrityError:
        _log.exception(_CODE_LIST)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Database integrity error",
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_LIST)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_LIST)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in variant batch list")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None


# ---------------------------------------------------------------------------
# GET /api/v1/variant-batches/{batch_id} — full detail + runs
# ---------------------------------------------------------------------------


@router.get("/{batch_id}", response_model=None)
async def get_batch(
    batch_id: uuid.UUID,
    request: Request,
    _session: AsyncSession = Depends(get_db_session),
    _principal: Any = require_permission("variant.list"),
) -> dict[str, Any]:
    """Fetch a single batch's detail + runs by batch_id."""
    try:
        async with _session.begin():
            return await _load_batch_detail(
                _session,
                batch_id=batch_id,
                org_id=_principal.organisation_id,
            )
    except HTTPException:
        raise
    except IntegrityError:
        _log.exception(_CODE_DETAIL)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Database integrity error",
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_DETAIL)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_DETAIL)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None
    except Exception:
        _log.exception("Unexpected error in variant batch detail")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None


# ---------------------------------------------------------------------------
# DELETE /api/v1/variant-batches/{batch_id} — soft-delete
# ---------------------------------------------------------------------------


@router.delete("/{batch_id}", response_model=None)
async def delete_batch(
    batch_id: uuid.UUID,
    request: Request,
    _session: AsyncSession = Depends(get_db_session),
    _principal: Any = require_permission("variant.delete"),
) -> dict[str, str]:
    """Soft-delete a batch: hides it from 'My comparisons' but keeps
    the compare URL + run links working.
    """
    try:
        async with _session.begin():
            org_id = _principal.organisation_id
            found = await soft_delete_batch_state(_session, batch_id=batch_id, org_id=org_id)
            if not found:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MSG_BATCH_NOT_FOUND)
            return {}
    except HTTPException:
        raise
    except IntegrityError:
        _log.exception(_CODE_DELETE)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Database integrity error",
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_DELETE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_DELETE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None
    except Exception:
        _log.exception("Unexpected error in variant batch delete")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None


# ---------------------------------------------------------------------------
# POST /api/v1/variant-batches/{batch_id}/re-fire — re-fire batch
# ---------------------------------------------------------------------------


@router.post("/{batch_id}/re-fire", response_model=None)
async def re_fire_batch(
    batch_id: uuid.UUID,
    request: Request,
    _session: AsyncSession = Depends(get_db_session),
    _principal: Any = require_permission("variant.run"),
) -> dict[str, Any]:
    """Re-fire a batch from its frozen definition.

    Loads the original batch state, resolves the variant group + pipeline,
    and fires a fresh batch with the same input payload. Returns the new
    batch detail (with new batch_id).
    """
    try:
        async with _session.begin():
            from modulo.db.crud.variant_group import (
                get_variant_group,
                run_variant_batch,
            )

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

            # Collect the new batch_id from the first run's frozen snapshot.
            first_run_snap: dict[str, Any] = {}
            raw_first = results[0].get("frozen_snapshot") or results[0].get("variant")
            if isinstance(raw_first, dict):
                first_run_snap = raw_first
            new_batch_id = first_run_snap.get("batch_id") or batch_id

            return await _load_batch_detail(
                _session,
                batch_id=uuid.UUID(str(new_batch_id)),
                org_id=org_id,
            )
    except HTTPException:
        raise
    except IntegrityError:
        _log.exception(_CODE_RE_FIRE)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Database integrity error",
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_RE_FIRE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_RE_FIRE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None
    except Exception:
        _log.exception("Unexpected error in variant batch re-fire")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None
