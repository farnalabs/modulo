"""POST /api/v1/runs — manual pipeline trigger and run lifecycle endpoints."""

import asyncio
import difflib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as SA_TimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import (
    _get_engine,
    _get_session_factory,
    get_db_session,
    pg_connection_string,
)
from modulo.api.middleware.sensitive_mask import is_sensitive_key, mask_sensitive_value
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.background_pipeline_worker import BackgroundPipelineWorker
from modulo.core.pipeline_engine.executor import PipelineExecutor
from modulo.core.pipeline_engine.recovery import (
    ConcurrentRecoveryError,
    NodeAlreadyCompletedError,
    NodeNotFoundInGraphError,
    RecoveryNotAllowedError,
    recover_node,
)
from modulo.db.crud.node_observation import observe_node
from modulo.db.crud.pipeline import get_pipeline
from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph
from modulo.db.crud.run import (
    create_run,
    get_run,
    get_run_heatmap,
    get_run_io,
    get_run_stats,
    request_cancellation,
)
from modulo.db.crud.run import (
    list_runs as db_list_runs,
)
from modulo.db.models.agent import Agent
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.rls import set_rls_org
from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)

_bg_worker: BackgroundPipelineWorker | None = None


def set_background_worker(worker: BackgroundPipelineWorker) -> None:
    global _bg_worker
    _bg_worker = worker


router = APIRouter(prefix="/api/v1/runs", tags=["runs"])

_TERMINAL_STATUSES = frozenset({"complete", "failed", "cancelled", "eval_failed"})


class RunNotFoundError(KeyError):
    """Raised when a run is not found."""


async def _run_with_retry[R](
    fn: Callable[[], Awaitable[R]],
    max_retries: int = 2,
    base_delay: float = 0.5,
) -> R:
    """Execute fn with retry on transient connection errors."""
    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except (TimeoutError, ConnectionResetError, OSError, SA_TimeoutError, OperationalError) as exc:
            last_exc = exc
            if attempt < max_retries:
                _log.warning("route.db_retry", extra={"attempt": attempt + 1, "error": str(exc)})
                await asyncio.sleep(base_delay * (2**attempt))
            else:
                _log.warning("route.db_retry_exhausted", extra={"error": str(exc)})
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Retry exhausted without exception")


async def _do_get_run(
    factory: async_sessionmaker[AsyncSession],
    principal: TenantPrincipal,
    run_id: uuid.UUID,
) -> Run:
    async with factory() as session, session.begin():
        await set_rls_org(session, principal.organisation_id)
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from modulo.db.models.run import Run

        stmt = select(Run).options(selectinload(Run.pipeline)).where(Run.id == run_id)
        run = (await session.execute(stmt)).scalar_one_or_none()
        if run is None:
            raise RunNotFoundError(run_id)
        return run


async def _do_list_runs(
    factory: async_sessionmaker[AsyncSession],
    user: TenantPrincipal,
    pipeline_id: uuid.UUID | None,
    run_status: str | None,
    trigger_type: str | None,
    search: str | None,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    async with factory() as session, session.begin():
        await set_rls_org(session, user.organisation_id)
        result = await db_list_runs(
            session,
            pipeline_id=pipeline_id,
            status=run_status,
            trigger_type=trigger_type,
            search=search,
            page=page,
            page_size=page_size,
        )
        items = []
        for run in result.items:
            pipeline_name = run.pipeline.name if run.pipeline else None
            items.append(
                {
                    "run_id": str(run.id),
                    "pipeline_id": str(run.pipeline_id),
                    "pipeline_name": pipeline_name,
                    "status": run.status,
                    "trigger_type": run.trigger_type,
                    "run_number": run.run_number,
                    "created_at": run.created_at.isoformat() if run.created_at else None,
                    "started_at": run.started_at.isoformat() if run.started_at else None,
                    "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                    "error_code": run.error_code,
                    "total_cost_usd": run.total_cost_usd,
                    "account_id": str(run.account_id) if run.account_id else None,
                }
            )
    return {
        "items": items,
        "total": result.total,
        "page": result.page,
        "page_size": result.page_size,
        "next_cursor": result.next_cursor,
        "has_more": result.has_more,
    }


@handle_db_errors("runs.list_runs_endpoint")
@router.get("")
async def list_runs_endpoint(
    pipeline_id: uuid.UUID | None = Query(None),
    run_status: str | None = Query(None, alias="status"),
    trigger_type: str | None = Query(None),
    search: str | None = Query(None, max_length=200),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    factory: async_sessionmaker[AsyncSession] = Depends(_get_session_factory),
    user: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, Any]:
    try:
        return await _run_with_retry(
            lambda: _do_list_runs(factory, user, pipeline_id, run_status, trigger_type, search, page, page_size)
        )
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        _log.exception("route.programming_error")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. This feature requires a database update. Please contact support.",
        ) from None
    except SQLAlchemyError:
        _log.exception("route.db_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("runs_list.unexpected_error", extra={"type": type(exc).__name__})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None


_NAMESPACE_TRACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


class TriggerRunRequest(BaseModel):
    pipeline_id: uuid.UUID
    input_payload: dict[str, Any] = Field(default_factory=dict)


class RunResponse(BaseModel):
    run_id: uuid.UUID
    status: str
    pipeline_id: uuid.UUID
    run_number: int | None = None
    pipeline_name: str | None = None
    langgraph_thread_id: str
    error_detail: str | None = None
    error_code: str | None = None
    total_cost_usd: Decimal | None = None
    token_consumption: dict[str, Any] | None = None
    trace_id: str | None = None
    node_token_usage: dict[str, Any] | None = None


def _build_run_response(run: Any) -> RunResponse:
    """Build a RunResponse from a Run ORM entity, populating derived fields."""
    token_consumption: dict[str, Any] | None = None
    if run.total_tokens is not None:
        token_consumption = {"total_tokens": run.total_tokens}

    trace_id: str | None = None
    if run.langgraph_thread_id:
        trace_id = str(uuid.uuid5(_NAMESPACE_TRACE, run.langgraph_thread_id))

    pipeline_name: str | None = None
    if run.pipeline is not None:
        pipeline_name = run.pipeline.name

    return RunResponse(
        run_id=run.id,
        status=run.status,
        pipeline_id=run.pipeline_id,
        run_number=run.run_number,
        pipeline_name=pipeline_name,
        langgraph_thread_id=run.langgraph_thread_id,
        error_detail=run.error_detail,
        error_code=run.error_code,
        total_cost_usd=run.total_cost_usd,
        token_consumption=token_consumption,
        trace_id=trace_id,
        node_token_usage=run.node_token_usage,
    )


async def _validate_run_input_basics(
    session: AsyncSession,
    graph_json: dict[str, Any],
    snapshot: PipelineSnapshot,
    input_payload: dict[str, Any],
) -> None:
    """Basic pre-run input health checks (not full schema validation).

    Verifies the entry node exists, its agent is valid, and input is a dict.
    Full schema-definition validation is delegated to graph_validator at
    run time after snapshot creation.
    """
    nodes = graph_json.get("nodes", [])
    edges = graph_json.get("edges", [])
    if not nodes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Pipeline graph has no nodes",
        )

    target_ids: set[str] = set()
    for edge in edges:
        target_id = edge.get("target_node_id")
        if target_id is None:
            target_id = edge.get("target")
        if target_id is not None:
            target_ids.add(str(target_id))
    entry_candidates = [n for n in nodes if str(n.get("id")) not in target_ids]
    if not entry_candidates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Pipeline graph has no entry node (cycle detected)",
        )

    entry_node = entry_candidates[0]
    agent_id_str = entry_node.get("agent_id")
    if agent_id_str is None:
        return

    agent_result = await session.execute(select(Agent).where(Agent.id == uuid.UUID(str(agent_id_str))))
    agent = agent_result.scalar_one_or_none()
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Entry agent {agent_id_str} not found",
        )

    if not isinstance(input_payload, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Input payload must be a JSON object",
        )


@router.post("", response_model=RunResponse, status_code=status.HTTP_202_ACCEPTED)
async def trigger_run(
    req: TriggerRunRequest,
    session: AsyncSession = Depends(get_db_session),
    engine: AsyncEngine = Depends(_get_engine),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> RunResponse:
    """Manually trigger a pipeline run.

    Returns 202 immediately; execution happens in a background task.
    The run status can be polled via GET /api/v1/runs/{run_id}.
    """
    org_id = principal.organisation_id

    try:
        async with session.begin():
            await set_rls_org(session, org_id)
            pipeline = await get_pipeline(session, req.pipeline_id)
            if pipeline is None:
                raise HTTPException(status_code=404, detail=f"Pipeline {req.pipeline_id} not found")
            snapshot = await create_snapshot_from_live_graph(
                session,
                pipeline_id=pipeline.id,
                account_id=principal.account_id,
            )
            if snapshot is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Pipeline {req.pipeline_id} not found",
                )
            await _validate_run_input_basics(session, snapshot.graph_json, snapshot, req.input_payload)
            run = await create_run(
                session,
                org_id=org_id,
                pipeline_id=pipeline.id,
                snapshot_id=snapshot.id,
                trigger_type="manual",
                input_payload=req.input_payload,
            )
            run_id = run.id
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. This feature requires a database update. Please contact support.",
        ) from None

    except SQLAlchemyError:
        _log.warning("route.db_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception("pipeline_execution.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    if _bg_worker is not None:
        _bg_worker.submit(run_id, org_id, req.input_payload)
    else:
        _log.warning("Background worker not initialized — run %s will not execute", run_id)

    return _build_run_response(run)


# ---------------------------------------------------------------------------
# Run stats / analytics
# ---------------------------------------------------------------------------


@handle_db_errors("runs.get_run_stats_endpoint")
@router.get("/stats", response_model=dict[str, Any])
async def get_run_stats_endpoint(
    period: str = Query(default="30d", pattern=r"^(7d|30d|90d)$"),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, Any]:
    """Aggregated run stats for a period (7d|30d|90d)."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            return await get_run_stats(session, period)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. This feature requires a database update. Please contact support.",
        ) from None

    except SQLAlchemyError:
        _log.warning("route.db_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception("pipeline_execution.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None


@handle_db_errors("runs.get_run_heatmap_endpoint")
@router.get("/stats/heatmap", response_model=list[dict[str, Any]])
async def get_run_heatmap_endpoint(
    year: int = Query(default=2026, ge=2020, le=2100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> list[dict[str, Any]]:
    """Run counts per day for the given year (calendar heatmap)."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            return await get_run_heatmap(session, year)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. This feature requires a database update. Please contact support.",
        ) from None

    except SQLAlchemyError:
        _log.warning("route.db_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception("pipeline_execution.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None


@router.get("/{run_id}", response_model=RunResponse)
async def get_run_status(
    run_id: uuid.UUID,
    factory: async_sessionmaker[AsyncSession] = Depends(_get_session_factory),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> RunResponse:
    try:
        run = await _run_with_retry(lambda: _do_get_run(factory, principal, run_id))
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None

    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. This feature requires a database update. Please contact support.",
        ) from None

    except SQLAlchemyError:
        _log.warning("route.db_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None
    except RunNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Run not found",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("pipeline_execution.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None

    return _build_run_response(run)


@router.post("/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, str]:
    """Request cancellation of a run.

    Returns 202 immediately. The run may transition to cancelled asynchronously.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            run = await get_run(session, run_id)

            if run is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

            if run.status in _TERMINAL_STATUSES:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Run is already in terminal status: {run.status}",
                )

            await request_cancellation(session, run_id)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. This feature requires a database update. Please contact support.",
        ) from None

    except SQLAlchemyError:
        _log.warning("route.db_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception("pipeline_execution.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    return {"status": "accepted"}


# ---------------------------------------------------------------------------
# Run IO inspection
# ---------------------------------------------------------------------------


class RunIOResponse(BaseModel):
    run_id: uuid.UUID
    run_number: int | None = None
    status: str
    input_payload: dict[str, Any] | None = None
    outputs_json: dict[str, Any] | None = None
    fixture_map: dict[str, str] | None = None

    def build_fixture_map(self) -> dict[str, str]:
        return _build_fixture_map(self.input_payload, self.outputs_json)


def _build_fixture_map(
    input_payload: dict[str, Any] | None,
    outputs_json: dict[str, Any] | None,
) -> dict[str, str]:
    """Generate a StubModelBackend fixture_map from run IO.

    If outputs_json is structured per-node (each value a dict with
    ``input`` and ``output`` keys), each node's mapping becomes a
    fixture_map entry.  Otherwise a single entry maps the full
    input_payload to the serialised outputs.
    """
    fixture: dict[str, str] = {}
    inp = input_payload or {}
    out = outputs_json or {}

    if isinstance(out, dict) and any(isinstance(v, dict) and "input" in v and "output" in v for v in out.values()):
        for _node_id, node_io in out.items():
            if isinstance(node_io, dict):
                node_input = node_io.get("input", json.dumps(inp, sort_keys=True))
                node_output = node_io.get("output", "")
                key = " ".join(str(node_input).split())
                fixture[key] = str(node_output)
    else:
        key = " ".join(str(inp).split())
        fixture[key] = str(out)

    return fixture


class FixtureExportResponse(BaseModel):
    fixture_name: str
    run_id: uuid.UUID
    pipeline_id: uuid.UUID
    status: str
    snapshot_graph_json: dict[str, Any] = {}
    input_payload: dict[str, Any] | None = None
    outputs_json: dict[str, Any] | None = None
    fixture_map: dict[str, str]


@handle_db_errors("runs.get_run_io_endpoint")
@router.get("/{run_id}/io", response_model=RunIOResponse)
async def get_run_io_endpoint(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> RunIOResponse:
    """Return per-node IO for a completed run, plus generated fixture_map."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            result = await get_run_io(session, run_id)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. This feature requires a database update. Please contact support.",
        ) from None

    except SQLAlchemyError:
        _log.warning("route.db_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception("pipeline_execution.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    if result.get("outputs_json"):
        result["outputs_json"] = _mask_output_value(result["outputs_json"])

    resp = RunIOResponse(**result)
    resp.fixture_map = resp.build_fixture_map()
    return resp


@handle_db_errors("runs.export_run_fixture")
@router.get("/{run_id}/export-fixture", response_model=FixtureExportResponse)
async def export_run_fixture(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> FixtureExportResponse:
    """Export run IO data as a StubModelBackend-compatible fixture.

    Returns the input payload, per-node outputs, snapshot graph, and
    a ``fixture_map`` that can be loaded directly into
    ``StubModelBackend(fixture_map=...)`` for regression testing.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            run = await get_run(session, run_id)
            if run is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

            from modulo.db.models.pipeline_snapshot import PipelineSnapshot as SnapModel

            snap_result = await session.execute(select(SnapModel).where(SnapModel.id == run.snapshot_id))
            snapshot = snap_result.scalar_one_or_none()
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. This feature requires a database update. Please contact support.",
        ) from None

    except SQLAlchemyError:
        _log.warning("route.db_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception("pipeline_execution.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    graph_json = snapshot.graph_json if snapshot else {}

    masked_input = _mask_output_value(run.input_payload) if run.input_payload else None
    masked_outputs = _mask_output_value(run.outputs_json) if run.outputs_json else None
    fixture_map = _build_fixture_map(masked_input, masked_outputs)
    short_id = str(run.id)[:8]

    return FixtureExportResponse(
        fixture_name=f"run_{short_id}_io",
        run_id=run.id,
        pipeline_id=run.pipeline_id,
        status=run.status,
        snapshot_graph_json=graph_json,
        input_payload=masked_input,
        outputs_json=masked_outputs,
        fixture_map=fixture_map,
    )


# ---------------------------------------------------------------------------
# Workspace lease inspection
# ---------------------------------------------------------------------------


@handle_db_errors("runs.get_run_workspace_lease")
@router.get("/{run_id}/workspace-lease")
async def get_run_workspace_lease(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, Any] | None:
    """Return the WorkspaceLease associated with a run, if any."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            from modulo.db.models.workspace_lease import WorkspaceLease

            result = await session.execute(select(WorkspaceLease).where(WorkspaceLease.run_id == run_id))
            lease = result.scalar_one_or_none()
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. This feature requires a database update. Please contact support.",
        ) from None

    except SQLAlchemyError:
        _log.warning("route.db_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception("pipeline_execution.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    if lease is None:
        return None
    return {
        "id": str(lease.id),
        "organisation_id": str(lease.organisation_id),
        "environment_profile_id": str(lease.environment_profile_id),
        "run_id": str(lease.run_id) if lease.run_id else None,
        "provider_ref": lease.provider_ref,
        "status": lease.status,
        "started_at": lease.lease_started_at.isoformat() if lease.lease_started_at else None,
        "expires_at": lease.lease_expires_at.isoformat() if lease.lease_expires_at else None,
        "resource_usage": lease.resource_usage_json,
    }


@handle_db_errors("runs.get_run_workspace_events")
@router.get("/{run_id}/workspace-events")
async def get_run_workspace_events(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> list[dict[str, str]]:
    """Return workspace lifecycle events for a run as a timeline."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            from modulo.db.models.audit_event import AuditEvent

            result = await session.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.resource_type == "workspace",
                    AuditEvent.resource_id == run_id,
                )
                .order_by(AuditEvent.created_at)
            )
            events = result.scalars().all()
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. This feature requires a database update. Please contact support.",
        ) from None

    except SQLAlchemyError:
        _log.warning("route.db_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception("pipeline_execution.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    return [
        {
            "event": evt.event_type.replace("workspace_", ""),
            "detail": (evt.payload_json or {}).get("detail", ""),
            "timestamp": evt.created_at.isoformat(),
        }
        for evt in events
    ]


# ---------------------------------------------------------------------------
# Node output inspection
# ---------------------------------------------------------------------------


def _mask_output_value(value: Any, *, _depth: int = 0) -> Any:
    """Recursively mask sensitive string fields in *value*.

    Traverses dicts, lists, and simple values.  String values whose keys
    match :func:`is_sensitive_key` are replaced with the standard mask.
    Nones and non-string atomic values pass through unchanged.
    """
    if _depth > 20:
        return value
    if isinstance(value, dict):
        return {
            k: (
                mask_sensitive_value(v)
                if isinstance(v, str) and is_sensitive_key(k)
                else _mask_output_value(v, _depth=_depth + 1)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_mask_output_value(item, _depth=_depth + 1) for item in value]
    return value


class NodeOutputResponse(BaseModel):
    run_id: uuid.UUID
    node_id: str
    output: Any = None


@handle_db_errors("runs.get_run_node_output")
@router.get("/{run_id}/nodes/{node_id}/output", response_model=NodeOutputResponse)
async def get_run_node_output(
    run_id: uuid.UUID,
    node_id: str,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> NodeOutputResponse:
    """Return a specific node's output from a completed pipeline run.

    Sensitive fields (keys matching *token*, *secret*, *api_key*,
    *password*, *key*, *credential*) in the output are masked with
    bullet characters.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            run = await get_run(session, run_id)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. This feature requires a database update. Please contact support.",
        ) from None

    except SQLAlchemyError:
        _log.warning("route.db_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception("pipeline_execution.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    outputs = run.outputs_json or {}
    node_output = outputs.get(node_id)
    if node_output is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Node {node_id} not found in run outputs",
        )

    masked = _mask_output_value(node_output)
    return NodeOutputResponse(run_id=run_id, node_id=node_id, output=masked)


# ---------------------------------------------------------------------------
# Node observation (task-nv24-node-observed-human)
# ---------------------------------------------------------------------------


class ObserveNodeResponse(BaseModel):
    run_id: uuid.UUID
    node_id: str
    human_observed_at: str | None = None
    human_observed_by: str | None = None


@handle_db_errors("runs.observe_run_node")
@router.post("/{run_id}/nodes/{node_id}/observe", response_model=ObserveNodeResponse)
async def observe_run_node(
    run_id: uuid.UUID,
    node_id: str,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> ObserveNodeResponse:
    """Mark a node as observed by a human.

    Requires operator or admin role.  Idempotent — observing the same
    node multiple times returns the original observation timestamp.
    """
    if principal.org_role not in ("admin", "operator"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only operators and admins can observe nodes",
        )

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            run = await get_run(session, run_id)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. This feature requires a database update. Please contact support.",
        ) from None

    except SQLAlchemyError:
        _log.warning("route.db_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception("pipeline_execution.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            obs = await observe_node(
                session,
                organisation_id=principal.organisation_id,
                run_id=run_id,
                node_id=node_id,
                observed_by=principal.account_id,
            )
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. This feature requires a database update. Please contact support.",
        ) from None

    except SQLAlchemyError:
        _log.warning("route.db_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception("pipeline_execution.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    return ObserveNodeResponse(
        run_id=run_id,
        node_id=node_id,
        human_observed_at=obs.human_observed_at.isoformat() if obs.human_observed_at else None,
        human_observed_by=str(obs.account_id) if obs.account_id else None,
    )


# ---------------------------------------------------------------------------
# Node recovery (task-prd-recovery-manual-input)
# ---------------------------------------------------------------------------


class NodeRecoverRequest(BaseModel):
    input_data: dict[str, Any] | None = None


class NodeRecoverResponse(BaseModel):
    run_id: uuid.UUID
    node_id: str
    action: str
    status: str


@handle_db_errors("runs.recover_run_node")
@router.post(
    "/{run_id}/nodes/{node_id}/recover",
    response_model=NodeRecoverResponse,
    status_code=status.HTTP_200_OK,
)
async def recover_run_node(
    run_id: uuid.UUID,
    node_id: str,
    req: NodeRecoverRequest,
    session: AsyncSession = Depends(get_db_session),
    engine: AsyncEngine = Depends(_get_engine),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> NodeRecoverResponse:
    """Recover a failed manual-input node.

    Two modes:
      * **Re-run** — provide ``input_data`` with the new manual output.
      * **Skip** — omit ``input_data`` (or set ``null``); the node is marked
        completed with no output and the run resumes.

    Requires operator or admin role.
    """
    if principal.org_role not in ("admin", "operator"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only operators and admins can recover nodes",
        )

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            try:
                run = await recover_node(
                    session,
                    org_id=principal.organisation_id,
                    run_id=run_id,
                    node_id=node_id,
                    input_data=req.input_data,
                    actor_id=principal.account_id,
                )
            except RecoveryNotAllowedError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)[:200]) from exc
            except NodeNotFoundInGraphError as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
            except NodeAlreadyCompletedError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            except ConcurrentRecoveryError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. This feature requires a database update. Please contact support.",
        ) from None

    except SQLAlchemyError:
        _log.warning("route.db_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception("pipeline_execution.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    action = "skip" if req.input_data is None else "replay"

    # Resume the graph with the recovery data.
    resume_data: dict[str, Any] = {"action": action, "output": req.input_data}

    executor = PipelineExecutor(
        engine,
        checkpointer_conn_string=pg_connection_string(str(engine.url)),
    )
    try:
        await executor.resume(
            run_id=run_id,
            org_id=principal.organisation_id,
            resume_data=resume_data,
        )
    except Exception as exc:
        _log.exception("run.recover_node.resume_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to resume pipeline after node recovery",
        ) from exc

    return NodeRecoverResponse(
        run_id=run_id,
        node_id=node_id,
        action=action,
        status=run.status,
    )


# ---------------------------------------------------------------------------
# Prompt reveal (PRD §8.9)
# ---------------------------------------------------------------------------


class PromptRevealResponse(BaseModel):
    prompt: str
    messages: list[dict[str, str]]
    token_count: int
    prompt_always_visible: bool = False


def _mask_prompt_text(text: str) -> str:
    """Mask sensitive credential-like values in prompt text.

    Replaces values following sensitive keys (token, secret, api_key,
    password, key, credential) with bullet characters.
    """
    import re

    masked = text
    patterns = [
        (r'(api_key["\']?\s*[:=]\s*["\']?)[^"\'}\s,]+', r"\1" + "\u2022\u2022\u2022\u2022\u2022\u2022"),
        (r'(secret["\']?\s*[:=]\s*["\']?)[^"\'}\s,]+', r"\1" + "\u2022\u2022\u2022\u2022\u2022\u2022"),
        (r'(token["\']?\s*[:=]\s*["\']?)[^"\'}\s,]+', r"\1" + "\u2022\u2022\u2022\u2022\u2022\u2022"),
        (r'(password["\']?\s*[:=]\s*["\']?)[^"\'}\s,]+', r"\1" + "\u2022\u2022\u2022\u2022\u2022\u2022"),
        (r'(credential["\']?\s*[:=]\s*["\']?)[^"\'}\s,]+', r"\1" + "\u2022\u2022\u2022\u2022\u2022\u2022"),
        (r'(passwd["\']?\s*[:=]\s*["\']?)[^"\'}\s,]+', r"\1" + "\u2022\u2022\u2022\u2022\u2022\u2022"),
    ]
    for pattern, replacement in patterns:
        masked = re.sub(pattern, replacement, masked, flags=re.IGNORECASE)
    return masked


def _mask_message_list(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Apply sensitive masking to all message content."""
    return [{"role": m["role"], "content": _mask_prompt_text(m["content"])} for m in messages]


def _estimate_tokens(text: str) -> int:
    """Estimate token count using a 4-char-per-token heuristic."""
    return max(1, len(text) // 4)


async def _get_checkpoint_state(
    session: AsyncSession,
    thread_id: str,
    organisation_id: uuid.UUID,
    fernet_key: str | None = None,
) -> dict[str, Any] | None:
    """Fetch the latest checkpoint state for a thread, decrypting if needed."""
    from cryptography.fernet import Fernet

    result = await session.execute(
        text("""
            SELECT checkpoint, checkpoint_id
            FROM checkpoints
            WHERE organisation_id = :org_id
              AND thread_id = :thread_id
              AND checkpoint_ns = ''
            ORDER BY checkpoint_id DESC
            LIMIT 1
        """),
        {"org_id": organisation_id, "thread_id": thread_id},
    )
    row = result.fetchone()
    if row is None:
        return None

    raw_checkpoint = row[0]
    if isinstance(raw_checkpoint, str):
        try:
            parsed = json.loads(raw_checkpoint)
            if isinstance(parsed, dict):
                if parsed.get("__encrypted__") and fernet_key:
                    f = Fernet(fernet_key.encode())
                    decrypted = f.decrypt(parsed["data"].encode())
                    raw_checkpoint = json.loads(decrypted.decode())
                else:
                    raw_checkpoint = parsed
        except (json.JSONDecodeError, Exception) as exc:
            _log.warning("checkpoint.decrypt_skip", extra={"error": str(exc)[:200]})
    elif isinstance(raw_checkpoint, dict) and raw_checkpoint.get("__encrypted__") and fernet_key:
        try:
            f = Fernet(fernet_key.encode())
            decrypted = f.decrypt(raw_checkpoint["data"].encode())
            raw_checkpoint = json.loads(decrypted.decode())
        except Exception as exc:
            _log.warning("checkpoint.decrypt_skip", extra={"error": str(exc)[:200]})

    if isinstance(raw_checkpoint, dict):
        return raw_checkpoint.get("channel_values")
    return None


def _build_messages_from_agent_and_state(
    agent: Agent | None,
    input_payload: dict[str, Any] | None,
    outputs_json: dict[str, Any] | None,
    checkpoint_state: dict[str, Any] | None,
    node_id: str,
) -> list[dict[str, str]]:
    """Reconstruct the LLM messages for a node from agent + run data.

    Builds system message from the agent's prompt_template, user message
    from the input payload or checkpoint state, and assistant messages
    from previous node outputs.
    """
    messages: list[dict[str, str]] = []

    if agent is not None:
        system_content = agent.prompt_template or ""
        if system_content:
            messages.append({"role": "system", "content": system_content})

    # Build conversation history from previous node outputs.
    if outputs_json:
        for prev_node_id, output in outputs_json.items():
            if prev_node_id == node_id:
                continue
            if isinstance(output, str):
                messages.append({"role": "assistant", "content": output})
            elif isinstance(output, dict):
                content = json.dumps(output, default=str)
                messages.append({"role": "assistant", "content": content})

    # Current user input — prefer checkpoint state, fall back to run input_payload.
    user_input: dict[str, Any] | None = None
    if checkpoint_state:
        run_ctx = checkpoint_state.get("run_context") or {}
        user_input = run_ctx.get("input")
    if user_input is None and input_payload:
        user_input = input_payload

    if user_input is not None:
        if isinstance(user_input, str):
            messages.append({"role": "user", "content": user_input})
        else:
            messages.append({"role": "user", "content": json.dumps(user_input, default=str)})

    return messages


def _lookup_agent_for_node(
    graph_json: dict[str, Any],
    node_id: str,
) -> uuid.UUID | None:
    """Find the agent_id for a node in the graph definition."""
    nodes = graph_json.get("nodes", [])
    for node in nodes:
        if str(node.get("id")) == node_id:
            agent_id = node.get("agent_id")
            if agent_id is not None:
                return uuid.UUID(str(agent_id))
            return None
    return None


@handle_db_errors("runs.reveal_node_prompt")
@router.post("/{run_id}/nodes/{node_id}/prompt/reveal", response_model=PromptRevealResponse)
async def reveal_node_prompt(
    run_id: uuid.UUID,
    node_id: str,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
    settings: Settings = Depends(get_settings),
) -> PromptRevealResponse:
    """Reconstruct and reveal the exact prompt sent to the LLM for a node.

    Returns the full prompt text, structured messages (system, user,
    assistant), and an estimated token count. Sensitive credential-like
    values are masked.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            run = await get_run(session, run_id)

            if run is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

            # Load snapshot to get graph definition.
            snapshot_id = run.snapshot_id
            snapshot_result = await session.execute(select(PipelineSnapshot).where(PipelineSnapshot.id == snapshot_id))
            snapshot = snapshot_result.scalar_one_or_none()

            if snapshot is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Snapshot {snapshot_id} not found for run",
                )

            graph_json: dict[str, Any] = snapshot.graph_json

            # Verify node exists in the graph.
            agent_id = _lookup_agent_for_node(graph_json, node_id)
            if agent_id is None:
                # Check if node exists at all (even non-agent nodes).
                node_ids = {str(n.get("id")) for n in graph_json.get("nodes", [])}
                if node_id not in node_ids:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Node {node_id} not found in pipeline graph",
                    )

            # Load agent for prompt template (if this is an agent node).
            agent: Agent | None = None
            prompt_always_visible = False
            if agent_id is not None:
                agent_result = await session.execute(select(Agent).where(Agent.id == agent_id))
                agent = agent_result.scalar_one_or_none()
                if agent is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Agent {agent_id} not found for node {node_id}",
                    )
                prompt_always_visible = bool(agent.prompt_always_visible)

            # Try to load checkpoint state for richer prompt reconstruction.
            thread_id = run.langgraph_thread_id
            checkpoint_state = await _get_checkpoint_state(
                session,
                thread_id,
                principal.organisation_id,
                fernet_key=settings.fernet_key,
            )

        messages = _build_messages_from_agent_and_state(
            agent=agent,
            input_payload=run.input_payload,
            outputs_json=run.outputs_json,
            checkpoint_state=checkpoint_state,
            node_id=node_id,
        )

        # Apply masking to protect sensitive values.
        masked_messages = _mask_message_list(messages)
        full_prompt = "\n\n".join(
            f"<{m['role'].upper()}>\n{m['content']}\n</{m['role'].upper()}>" for m in masked_messages
        )
        token_count = _estimate_tokens(full_prompt)

        return PromptRevealResponse(
            prompt=full_prompt,
            messages=masked_messages,
            token_count=token_count,
            prompt_always_visible=prompt_always_visible,
        )
    except asyncio.CancelledError:
        raise
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. This feature requires a database update. Please contact support.",
        ) from None
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        _log.warning("prompt_reveal.db_error", extra={"error": str(exc)[:200]})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Feature is temporarily unavailable. Please try again.",
        ) from None
    except Exception:
        _log.exception("prompt_reveal.error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while revealing the prompt.",
        ) from None


# ---------------------------------------------------------------------------
# Node output diff across runs (task-agent-output-diff)
# ---------------------------------------------------------------------------


class NodeOutputDiffLine(BaseModel):
    type: Literal["unchanged", "removed", "added"]
    content: str
    line_a: int | None = None
    line_b: int | None = None


class NodeOutputDiffRequest(BaseModel):
    run_id_a: uuid.UUID
    node_id_a: str
    run_id_b: uuid.UUID
    node_id_b: str


class NodeOutputDiffResponse(BaseModel):
    run_id_a: uuid.UUID
    run_id_b: uuid.UUID
    node_output_a: Any = None
    node_output_b: Any = None
    diff_lines: list[NodeOutputDiffLine]
    has_diff: bool


@handle_db_errors("runs.diff_node_output")
@router.post("/diff", response_model=NodeOutputDiffResponse)
async def diff_node_output(
    req: NodeOutputDiffRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> NodeOutputDiffResponse:
    """Diff a specific node's output across two runs.

    Accepts two (run_id, node_id) pairs, fetches each node's output,
    applies sensitive masking, and returns a structured line-level diff
    using difflib.SequenceMatcher.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            run_a = await get_run(session, req.run_id_a)
            run_b = await get_run(session, req.run_id_b)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None

    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. This feature requires a database update. Please contact support.",
        ) from None

    except SQLAlchemyError:
        _log.warning("route.db_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception("pipeline_execution.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    if run_a is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {req.run_id_a} not found",
        )
    if run_b is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {req.run_id_b} not found",
        )

    outputs_a = run_a.outputs_json or {}
    outputs_b = run_b.outputs_json or {}

    node_output_a = outputs_a.get(req.node_id_a)
    node_output_b = outputs_b.get(req.node_id_b)

    if node_output_a is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Node {req.node_id_a} not found in run {req.run_id_a} outputs",
        )
    if node_output_b is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Node {req.node_id_b} not found in run {req.run_id_b} outputs",
        )

    masked_a = _mask_output_value(node_output_a)
    masked_b = _mask_output_value(node_output_b)

    text_a = json.dumps(masked_a, indent=2)
    text_b = json.dumps(masked_b, indent=2)

    lines_a = text_a.splitlines(keepends=True)
    lines_b = text_b.splitlines(keepends=True)

    differ = difflib.SequenceMatcher(None, lines_a, lines_b)
    diff_lines: list[NodeOutputDiffLine] = []
    line_a = 1
    line_b = 1

    for op, i1, i2, j1, j2 in differ.get_opcodes():
        if op == "equal":
            for idx in range(i1, i2):
                diff_lines.append(
                    NodeOutputDiffLine(
                        type="unchanged",
                        content=lines_a[idx].rstrip("\n"),
                        line_a=line_a,
                        line_b=line_b,
                    )
                )
                line_a += 1
                line_b += 1
        elif op == "replace":
            for _ in range(i2 - i1):
                diff_lines.append(
                    NodeOutputDiffLine(
                        type="removed",
                        content=lines_a[i1].rstrip("\n"),
                        line_a=line_a,
                        line_b=None,
                    )
                )
                line_a += 1
                i1 += 1
            for _ in range(j2 - j1):
                diff_lines.append(
                    NodeOutputDiffLine(
                        type="added",
                        content=lines_b[j1].rstrip("\n"),
                        line_a=None,
                        line_b=line_b,
                    )
                )
                line_b += 1
                j1 += 1
        elif op == "delete":
            for _ in range(i2 - i1):
                diff_lines.append(
                    NodeOutputDiffLine(
                        type="removed",
                        content=lines_a[i1].rstrip("\n"),
                        line_a=line_a,
                        line_b=None,
                    )
                )
                line_a += 1
                i1 += 1
        elif op == "insert":
            for _ in range(j2 - j1):
                diff_lines.append(
                    NodeOutputDiffLine(
                        type="added",
                        content=lines_b[j1].rstrip("\n"),
                        line_a=None,
                        line_b=line_b,
                    )
                )
                line_b += 1
                j1 += 1

    has_diff = any(d.type != "unchanged" for d in diff_lines)

    return NodeOutputDiffResponse(
        run_id_a=req.run_id_a,
        run_id_b=req.run_id_b,
        node_output_a=masked_a,
        node_output_b=masked_b,
        diff_lines=diff_lines,
        has_diff=has_diff,
    )
