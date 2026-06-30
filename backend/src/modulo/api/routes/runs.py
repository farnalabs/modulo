"""POST /api/v1/runs — manual pipeline trigger and run lifecycle endpoints."""

import difflib
import json
import logging
import uuid
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.api.dependencies import (
    _get_engine,
    get_db_session,
    get_or_create_engine,
    pg_connection_string,
)
from modulo.api.middleware.sensitive_mask import is_sensitive_key, mask_sensitive_value
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
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
    update_run_status,
)
from modulo.db.models.agent import Agent
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.rls import set_rls_org
from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/runs", tags=["runs"])

_TERMINAL_STATUSES = frozenset({"complete", "failed", "cancelled", "eval_failed"})
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
    node_token_usage: dict[str, Any] | None = None


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
        await _validate_run_input_basics(session, snapshot.graph_json, snapshot, body.input_payload)

        run = await create_run(
            session,
            org_id=org_id,
            pipeline_id=pipeline.id,
            snapshot_id=snapshot.id,
            trigger_type="manual",
            input_payload=body.input_payload,
        )
        run_id = run.id

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
                    await update_run_status(session, run_id, "failed", error_code="internal_error")
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


class FixtureExportResponse(BaseModel):
    fixture_name: str
    run_id: uuid.UUID
    pipeline_id: uuid.UUID
    status: str
    snapshot_graph_json: dict[str, Any] = {}
    input_payload: dict[str, Any] | None = None
    outputs_json: dict[str, Any] | None = None
    fixture_map: dict[str, str]


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

    if result.get("outputs_json"):
        result["outputs_json"] = _mask_output_value(result["outputs_json"])

    resp = RunIOResponse(**result)
    resp.fixture_map = resp.build_fixture_map()
    return resp


@router.get("/{run_id}/export-fixture", response_model=FixtureExportResponse)
async def export_run_fixture(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> FixtureExportResponse:
    """Export run IO data as a StubModelBackend-compatible fixture.

    Returns the input payload, per-node outputs, snapshot graph, and
    a ``fixture_map`` that can be loaded directly into
    ``StubModelBackend(fixture_map=...)`` for regression testing.
    """
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        run = await get_run(session, run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

        from modulo.db.models.pipeline_snapshot import PipelineSnapshot as SnapModel
        snap_result = await session.execute(
            select(SnapModel).where(SnapModel.id == run.snapshot_id)
        )
        snapshot = snap_result.scalar_one_or_none()

    graph_json = snapshot.graph_json if snapshot else {}

    masked_input = _mask_output_value(run.input_payload) if run.input_payload else None
    masked_outputs = _mask_output_value(run.outputs_json) if run.outputs_json else None
    fixture_map = _build_fixture_map(masked_input, masked_outputs)
    short_id = str(run.id).split("-")[0]

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

        result = await session.execute(select(WorkspaceLease).where(WorkspaceLease.run_id == run_id))
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
            select(AuditEvent)
            .where(
                AuditEvent.resource_type == "workspace",
                AuditEvent.resource_id == run_id,
            )
            .order_by(AuditEvent.created_at)
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


@router.get("/{run_id}/nodes/{node_id}/output", response_model=NodeOutputResponse)
async def get_run_node_output(
    run_id: uuid.UUID,
    node_id: str,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> NodeOutputResponse:
    """Return a specific node's output from a completed pipeline run.

    Sensitive fields (keys matching *token*, *secret*, *api_key*,
    *password*, *key*, *credential*) in the output are masked with
    bullet characters.
    """
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        run = await get_run(session, run_id)

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


@router.post("/{run_id}/nodes/{node_id}/observe", response_model=ObserveNodeResponse)
async def observe_run_node(
    run_id: uuid.UUID,
    node_id: str,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
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

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        run = await get_run(session, run_id)

    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        obs = await observe_node(
            session,
            organisation_id=principal.organisation_id,
            run_id=run_id,
            node_id=node_id,
            observed_by=principal.user_id,
        )

    return ObserveNodeResponse(
        run_id=run_id,
        node_id=node_id,
        human_observed_at=obs.human_observed_at.isoformat() if obs.human_observed_at else None,
        human_observed_by=str(obs.human_observed_by) if obs.human_observed_by else None,
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


@router.post(
    "/{run_id}/nodes/{node_id}/recover",
    response_model=NodeRecoverResponse,
    status_code=status.HTTP_200_OK,
)
async def recover_run_node(
    run_id: uuid.UUID,
    node_id: str,
    body: NodeRecoverRequest,
    session: AsyncSession = Depends(get_db_session),
    engine: AsyncEngine = Depends(_get_engine),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
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

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        try:
            run = await recover_node(
                session,
                org_id=principal.organisation_id,
                run_id=run_id,
                node_id=node_id,
                input_data=body.input_data,
                actor_id=principal.user_id,
            )
        except RecoveryNotAllowedError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except NodeNotFoundInGraphError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except NodeAlreadyCompletedError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ConcurrentRecoveryError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    action = "skip" if body.input_data is None else "replay"

    # Resume the graph with the recovery data.
    resume_data: dict[str, Any] = {"output": body.input_data}
    if action == "skip":
        resume_data = {"action": "skip", "output": None}

    executor = PipelineExecutor(engine)
    await executor.resume(
        run_id=run_id,
        org_id=principal.organisation_id,
        resume_data=resume_data,
    )

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
        (r'(api_key["\']?\s*[:=]\s*["\']?)[^"\'}\s,]+', r'\1' + "\u2022\u2022\u2022\u2022\u2022\u2022"),
        (r'(secret["\']?\s*[:=]\s*["\']?)[^"\'}\s,]+', r'\1' + "\u2022\u2022\u2022\u2022\u2022\u2022"),
        (r'(token["\']?\s*[:=]\s*["\']?)[^"\'}\s,]+', r'\1' + "\u2022\u2022\u2022\u2022\u2022\u2022"),
        (r'(password["\']?\s*[:=]\s*["\']?)[^"\'}\s,]+', r'\1' + "\u2022\u2022\u2022\u2022\u2022\u2022"),
        (r'(credential["\']?\s*[:=]\s*["\']?)[^"\'}\s,]+', r'\1' + "\u2022\u2022\u2022\u2022\u2022\u2022"),
        (r'(passwd["\']?\s*[:=]\s*["\']?)[^"\'}\s,]+', r'\1' + "\u2022\u2022\u2022\u2022\u2022\u2022"),
    ]
    for pattern, replacement in patterns:
        masked = re.sub(pattern, replacement, masked, flags=re.IGNORECASE)
    return masked


def _mask_message_list(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Apply sensitive masking to all message content."""
    return [
        {"role": m["role"], "content": _mask_prompt_text(m["content"])}
        for m in messages
    ]


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
    elif isinstance(raw_checkpoint, dict):
        if raw_checkpoint.get("__encrypted__") and fernet_key:
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


@router.post("/{run_id}/nodes/{node_id}/prompt/reveal", response_model=PromptRevealResponse)
async def reveal_node_prompt(
    run_id: uuid.UUID,
    node_id: str,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> PromptRevealResponse:
    """Reconstruct and reveal the exact prompt sent to the LLM for a node.

    Returns the full prompt text, structured messages (system, user,
    assistant), and an estimated token count. Sensitive credential-like
    values are masked.
    """
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        run = await get_run(session, run_id)

    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    # Load snapshot to get graph definition.
    snapshot_id = run.snapshot_id
    snapshot_result = await session.execute(
        select(PipelineSnapshot).where(PipelineSnapshot.id == snapshot_id)
    )
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
        f"<{m['role'].upper()}>\n{m['content']}\n</{m['role'].upper()}>"
        for m in masked_messages
    )
    token_count = _estimate_tokens(full_prompt)

    return PromptRevealResponse(
        prompt=full_prompt,
        messages=masked_messages,
        token_count=token_count,
        prompt_always_visible=prompt_always_visible,
    )


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


@router.post("/diff", response_model=NodeOutputDiffResponse)
async def diff_node_output(
    body: NodeOutputDiffRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> NodeOutputDiffResponse:
    """Diff a specific node's output across two runs.

    Accepts two (run_id, node_id) pairs, fetches each node's output,
    applies sensitive masking, and returns a structured line-level diff
    using difflib.SequenceMatcher.
    """
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        run_a = await get_run(session, body.run_id_a)
        run_b = await get_run(session, body.run_id_b)

    if run_a is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {body.run_id_a} not found",
        )
    if run_b is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {body.run_id_b} not found",
        )

    outputs_a = run_a.outputs_json or {}
    outputs_b = run_b.outputs_json or {}

    node_output_a = outputs_a.get(body.node_id_a)
    node_output_b = outputs_b.get(body.node_id_b)

    if node_output_a is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Node {body.node_id_a} not found in run {body.run_id_a} outputs",
        )
    if node_output_b is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Node {body.node_id_b} not found in run {body.run_id_b} outputs",
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
            for _ in range(i2 - i1):
                diff_lines.append(
                    NodeOutputDiffLine(
                        type="unchanged",
                        content=lines_a[i1].rstrip("\n"),
                        line_a=line_a,
                        line_b=line_b,
                    )
                )
                line_a += 1
                line_b += 1
            i1, j1 = i2, j2
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
        run_id_a=body.run_id_a,
        run_id_b=body.run_id_b,
        node_output_a=masked_a,
        node_output_b=masked_b,
        diff_lines=diff_lines,
        has_diff=has_diff,
    )
