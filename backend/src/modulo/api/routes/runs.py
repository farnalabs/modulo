"""POST /api/v1/runs — manual pipeline trigger and run lifecycle endpoints."""

import hashlib
import logging
import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.api.dependencies import (
    _get_engine,
    get_db_session,
    get_or_create_engine,
    pg_connection_string,
)
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.pipeline_engine.executor import PipelineExecutor
from modulo.db.crud.pipeline import get_pipeline
from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph
from modulo.db.crud.run import (
    create_run,
    get_run,
    get_run_heatmap,
    get_run_io,
    get_run_stats,
    request_cancellation,
    update_run_status,
)
from modulo.db.models.agent import Agent
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.rls import set_rls_org
from modulo.settings import get_settings

_log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/runs", tags=["runs"])

_TERMINAL_STATUSES = frozenset({"complete", "failed", "cancelled"})
_NAMESPACE_TRACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


class TriggerRunRequest(BaseModel):
    pipeline_id: uuid.UUID
    input_payload: dict[str, Any] = Field(default_factory=dict)


class RunResponse(BaseModel):
    run_id: uuid.UUID
    status: str
    pipeline_id: uuid.UUID
    langgraph_thread_id: str
    error_detail: str | None = None
    error_code: str | None = None
    total_cost_usd: Decimal | None = None
    token_consumption: dict[str, Any] | None = None
    trace_id: str | None = None


def _build_run_response(run: Any) -> RunResponse:
    """Build a RunResponse from a Run ORM entity, populating derived fields."""
    token_consumption: dict[str, Any] | None = None
    if run.total_tokens is not None:
        token_consumption = {"total_tokens": run.total_tokens}

    trace_id: str | None = None
    if run.langgraph_thread_id:
        trace_id = str(uuid.uuid5(_NAMESPACE_TRACE, run.langgraph_thread_id))

    return RunResponse(
        run_id=run.id,
        status=run.status,
        pipeline_id=run.pipeline_id,
        langgraph_thread_id=run.langgraph_thread_id,
        error_detail=run.error_detail,
        error_code=run.error_code,
        total_cost_usd=run.total_cost_usd,
        token_consumption=token_consumption,
        trace_id=trace_id,
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

    target_ids = {str(e.get("source_node_id", e.get("target"))) for e in edges}
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

    agent_result = await session.execute(
        select(Agent).where(Agent.id == uuid.UUID(str(agent_id_str)))
    )
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
    body: TriggerRunRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db_session),
    engine: AsyncEngine = Depends(_get_engine),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> RunResponse:
    """Manually trigger a pipeline run.

    Returns 202 immediately; execution happens in a background task.
    The run status can be polled via GET /api/v1/runs/{run_id}.
    """
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        pipeline = await get_pipeline(session, body.pipeline_id)

    if pipeline is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pipeline {body.pipeline_id} not found",
        )

    org_id = principal.organisation_id

    # Create the run record inside a transaction.
    async with session.begin():
        await set_rls_org(session, org_id)
        snapshot = await create_snapshot_from_live_graph(
            session,
            pipeline_id=pipeline.id,
            created_by=principal.user_id,
        )
        if snapshot is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Pipeline {body.pipeline_id} not found",
            )

        # Pre-run input health checks against entry agent.
        await _validate_run_input_basics(
            session, snapshot.graph_json, snapshot, body.input_payload
        )

        run = await create_run(
            session,
            org_id=org_id,
            pipeline_id=pipeline.id,
            snapshot_id=snapshot.id,
            trigger_type="manual",
            input_payload=body.input_payload,
        )
        run_id = run.id
        thread_id = run.langgraph_thread_id

    executor = PipelineExecutor(
        engine,
        checkpointer_conn_string=pg_connection_string(str(engine.url)),
    )
    background_tasks.add_task(_run_in_background, executor, run_id, org_id, body.input_payload)

    return _build_run_response(run)


# ---------------------------------------------------------------------------
# Run stats / analytics
# ---------------------------------------------------------------------------


@router.get("/stats", response_model=dict[str, Any])
async def get_run_stats_endpoint(
    period: str = Query(default="30d", pattern=r"^(7d|30d|90d)$"),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """Aggregated run stats for a period (7d|30d|90d)."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        return await get_run_stats(session, period)


@router.get("/stats/heatmap", response_model=list[dict[str, Any]])
async def get_run_heatmap_endpoint(
    year: int = Query(default=2026, ge=2020, le=2100),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Run counts per day for the given year (calendar heatmap)."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        return await get_run_heatmap(session, year)


@router.get("/{run_id}", response_model=RunResponse)
async def get_run_status(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> RunResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        run = await get_run(session, run_id)

    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    return _build_run_response(run)


@router.post("/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, str]:
    """Request cancellation of a run.

    Returns 202 immediately. The run may transition to cancelled asynchronously.
    """
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

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await request_cancellation(session, run_id)

    return {"status": "accepted"}


async def _run_in_background(
    executor: PipelineExecutor,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    input_payload: dict[str, Any],
) -> None:
    try:
        await executor.execute(run_id=run_id, org_id=org_id, input_payload=input_payload)
    except Exception:
        _log.exception("run.background_execution_error", extra={"run_id": str(run_id)})
        try:
            settings = get_settings()
            engine = get_or_create_engine(settings)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                async with session.begin():
                    await set_rls_org(session, org_id)
                    await update_run_status(
                        session, run_id, "failed", error_code="internal_error"
                    )
        except Exception:
            _log.exception("run.mark_failed_error", extra={"run_id": str(run_id)})


# ---------------------------------------------------------------------------
# Run IO inspection
# ---------------------------------------------------------------------------


class RunIOResponse(BaseModel):
    run_id: uuid.UUID
    status: str
    input_payload: dict[str, Any] | None = None
    outputs_json: dict[str, Any] | None = None
    fixture_map: dict[str, str] | None = None

    def build_fixture_map(self) -> dict[str, str]:
        """Generate a StubModelBackend fixture_map from run IO.

        If outputs_json is structured per-node, each node's input->output
        mapping becomes a fixture_map entry.  Otherwise, a single entry maps
        the full input_payload to the serialised outputs.
        """
        fixture: dict[str, str] = {}
        inp = self.input_payload or {}
        out = self.outputs_json or {}

        if isinstance(out, dict) and any(
            isinstance(v, dict) and "input" in v and "output" in v for v in out.values()
        ):
            for _node_id, node_io in out.items():
                if isinstance(node_io, dict):
                    node_input = node_io.get("input", str(inp))
                    node_output = node_io.get("output", "")
                    key = " ".join(str(node_input).split())
                    fixture[key] = str(node_output)
        else:
            key = " ".join(str(inp).split())
            fixture[key] = str(out)

        return fixture


@router.get("/{run_id}/io", response_model=RunIOResponse)
async def get_run_io_endpoint(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> RunIOResponse:
    """Return per-node IO for a completed run, plus generated fixture_map."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        result = await get_run_io(session, run_id)

    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    resp = RunIOResponse(**result)
    resp.fixture_map = resp.build_fixture_map()
    return resp


# ---------------------------------------------------------------------------
# Workspace lease inspection
# ---------------------------------------------------------------------------


@router.get("/{run_id}/workspace-lease")
async def get_run_workspace_lease(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any] | None:
    """Return the WorkspaceLease associated with a run, if any."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        from modulo.db.models.workspace_lease import WorkspaceLease
        result = await session.execute(
            select(WorkspaceLease).where(WorkspaceLease.run_id == run_id)
        )
        lease = result.scalar_one_or_none()
    if lease is None:
        return None
    return {
        "id": str(lease.id),
        "organisation_id": str(lease.organisation_id),
        "environment_profile_id": str(lease.environment_profile_id),
        "run_id": str(lease.run_id) if lease.run_id else None,
        "provider_ref": lease.provider_ref,
        "status": lease.status,
        "started_at": lease.started_at.isoformat() if lease.started_at else None,
        "expires_at": lease.expires_at.isoformat() if lease.expires_at else None,
        "resource_usage": lease.resource_usage_json,
    }


@router.get("/{run_id}/workspace-events")
async def get_run_workspace_events(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> list[dict[str, str]]:
    """Return workspace lifecycle events for a run as a timeline."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        from modulo.db.models.audit_event import AuditEvent
        result = await session.execute(
            select(AuditEvent).where(
                AuditEvent.resource_type == "workspace",
                AuditEvent.resource_id == run_id,
            ).order_by(AuditEvent.created_at)
        )
        events = result.scalars().all()
    return [
        {
            "event": evt.event_type.replace("workspace_", ""),
            "detail": (evt.payload_json or {}).get("detail", ""),
            "timestamp": evt.created_at.isoformat(),
        }
        for evt in events
    ]
