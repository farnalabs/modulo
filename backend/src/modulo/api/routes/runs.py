"""POST /api/v1/runs — manual pipeline trigger and run lifecycle endpoints."""

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as SA_TimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tenacity import before_sleep_log, retry, retry_if_exception, stop_after_attempt, wait_exponential

from modulo.api.constants import MSG_RESOURCE_ALREADY_EXISTS, MSG_UNEXPECTED_ERROR
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import (
    _get_engine,
    _get_session_factory,
    get_db_session,
    require_permission,
    require_permission_any_credential,
)
from modulo.api.middleware.sensitive_mask import is_sensitive_key, mask_sensitive_value
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.dispatch import dispatch_run
from modulo.core.exceptions import OrgDeletedError
from modulo.core.guardrails import GuardrailSummary
from modulo.core.line_diff import iter_line_diffs
from modulo.core.node_output_split import node_return, node_telemetry
from modulo.core.pipeline_engine.classify import REASON_DELIVERED_EMAIL, _any_marker_delivery_done
from modulo.core.pipeline_engine.error_codes import map_legacy_code, present_error, sanitize_error_text
from modulo.core.pipeline_engine.event_broker import get_registry
from modulo.core.pipeline_engine.recovery import (
    ConcurrentRecoveryError,
    GuardrailOverrideError,
    GuardrailOverrideRejectedError,
    GuardrailOverrideRequiredError,
    NodeAlreadyCompletedError,
    NodeNotFoundInGraphError,
    RecoveryNotAllowedError,
    guardrail_override,
    recover_node,
)
from modulo.core.rate_limiter import TokenBucketRegistry
from modulo.core.trigger_engine import TriggerEngine
from modulo.db.crud.node_observation import observe_node
from modulo.db.crud.observability import get_otel_config
from modulo.db.crud.pipeline import get_pipeline
from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph
from modulo.db.crud.run import (
    create_run,
    get_child_run_rollup,
    get_run,
    get_run_heatmap,
    get_run_stats,
    request_cancellation,
)
from modulo.db.crud.run import (
    list_runs as db_list_runs,
)
from modulo.db.models.agent import Agent
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import TERMINAL_STATUSES, Run
from modulo.db.rls import set_rls_org
from modulo.otel_bridge import trace_id_for_thread
from modulo.settings import Settings, get_settings

_MSG_FEATURE_NOT_AVAILABLE_FEATURE = (
    "Feature is not available. This feature requires a database update. Please contact support."
)
_CODE_ROUTE_DB_ERROR = "route.db_error"
_MSG_DATABASE_TEMPORARILY_UNAVAILABLE = "Database temporarily unavailable."
_CODE_PIPELINE_EXECUTION_UNEXPECTED_ERROR = "pipeline_execution.unexpected_error"
_MSG_RUN_NOT_FOUND = "Run not found"
_CODE_RUN_OUTPUT = "run.output"
_MASKED_PLACEHOLDER = "••••••"
_CODE_RUNS_OBSERVE_RUN_NODE = "runs.observe_run_node"
_DEFAULT_FLOAT_DISPLAY = "0.000000"
_CODE_RUN_LIST = "run.list"
_CODE_RUNS_TRIGGER_RUN = "runs.trigger_run"
_CODE_RUNS_REVEAL_NODE_PROMPT = "runs.reveal_node_prompt"


_log = logging.getLogger(__name__)

# Guardrail-override rate limit (FAR-223 PR C gap). The override re-runs the
# guardrail pass and re-dispatches the run, so an operator must not be able to
# hammer it. ~10 overrides per 60s window per (org, actor). Uses the in-memory
# TokenBucketRegistry (per-process) which fails open -- the override keeps
# working if Redis is unavailable, which is the established best-effort pattern.
_GUARDRAIL_OVERRIDE_RATE_LIMIT = 10
_GUARDRAIL_OVERRIDE_RATE_PER_SEC = _GUARDRAIL_OVERRIDE_RATE_LIMIT / 60.0
_guardrail_override_rate_limiter = TokenBucketRegistry(
    rate=_GUARDRAIL_OVERRIDE_RATE_PER_SEC,
    burst=_GUARDRAIL_OVERRIDE_RATE_LIMIT,
)

_RETRY_TRANSIENT = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
    retry=retry_if_exception(
        lambda e: isinstance(e, (TimeoutError, ConnectionResetError, OSError, SA_TimeoutError, OperationalError))
    ),
    reraise=True,
    before_sleep=before_sleep_log(_log, logging.WARNING),
)


router = APIRouter(prefix="/api/v1/runs", tags=["runs"])

# Child-run cost rollup. `total_cost_usd` keeps its own-run semantics; the
# aggregate is a derived display value and never mutates the stored field.
_COST_ROLLUP_ZERO = Decimal(_DEFAULT_FLOAT_DISPLAY)
_COST_ROLLUP_QUANTUM = Decimal("0.000001")


def _quantize_cost_rollup(value: Decimal) -> Decimal:
    """Normalise a cost rollup value to 6 decimal places (Numeric(14, 6) scale)."""
    return value.quantize(_COST_ROLLUP_QUANTUM)


class RunNotFoundError(KeyError):
    """Raised when a run is not found."""


@_RETRY_TRANSIENT
async def _run_with_retry[R](
    fn: Callable[[], Awaitable[R]],
) -> R:
    """Execute fn with retry on transient connection errors."""
    return await fn()


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


async def _do_get_child_run_rollup(
    factory: async_sessionmaker[AsyncSession],
    principal: TenantPrincipal,
    run_id: uuid.UUID,
) -> tuple[Decimal, int]:
    """(child cost, child count) rollup for a single run (0.000000, 0 if none)."""
    async with factory() as session, session.begin():
        await set_rls_org(session, principal.organisation_id)
        rollup = await get_child_run_rollup(session, [run_id])
        cost, count = rollup.get(run_id, (_COST_ROLLUP_ZERO, 0))
        return _quantize_cost_rollup(cost), count


async def _do_get_otel_endpoint(
    factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
) -> str:
    """Return the org's configured OTLP endpoint, or ``""`` when unset.

    Best-effort enrichment (FAR-198 trace_url deep-link): a DB failure must
    never turn a run-detail request into an error — the run response is valid
    without a trace_url.
    """
    try:
        async with factory() as session, session.begin():
            await set_rls_org(session, org_id)
            config = await get_otel_config(session, org_id)
        return config.get("otlp_endpoint") or ""
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("runs.otel_endpoint_unavailable", extra={"org_id": str(org_id)}, exc_info=True)
        return ""


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
        # Child-run cost rollup: ONE GROUP BY query for the whole page, joined
        # in Python — never a per-row aggregate (avoids N+1).
        run_ids = [run.id for run in result.items]
        child_rollup: dict[uuid.UUID, tuple[Decimal, int]] = {}
        if run_ids:
            child_rollup = await get_child_run_rollup(session, run_ids)
        items = []
        for run in result.items:
            pipeline_name = run.pipeline.name if run.pipeline else None
            child_cost, child_count = child_rollup.get(run.id, (_COST_ROLLUP_ZERO, 0))
            child_cost = _quantize_cost_rollup(child_cost)
            own_cost = run.total_cost_usd if run.total_cost_usd is not None else _COST_ROLLUP_ZERO
            _error_code, error_detail = present_error(run.error_code, run.error_detail, limit=200)
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
                    "error_code": _error_code,
                    "error_detail": error_detail,
                    "total_cost_usd": run.total_cost_usd,
                    "child_runs_cost_usd": child_cost,
                    "child_runs_count": child_count,
                    "aggregate_cost_usd": _quantize_cost_rollup(own_cost + child_cost),
                    "account_id": str(run.account_id) if run.account_id else None,
                    "input_payload": _mask_output_value(run.input_payload) if run.input_payload else None,
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


@router.get("")
@handle_db_errors("runs.list_runs_endpoint")
async def list_runs_endpoint(
    pipeline_id: uuid.UUID | None = Query(None),
    run_status: str | None = Query(None, alias="status"),
    trigger_type: str | None = Query(None),
    search: str | None = Query(None, max_length=200),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    factory: async_sessionmaker[AsyncSession] = Depends(_get_session_factory),
    user: TenantPrincipal = require_permission(_CODE_RUN_LIST),
) -> dict[str, Any]:
    try:
        return await _run_with_retry(
            lambda: _do_list_runs(factory, user, pipeline_id, run_status, trigger_type, search, page, page_size)
        )
    except IntegrityError:
        _log.exception("runs.list_runs_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception("route.programming_error")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_FEATURE_NOT_AVAILABLE_FEATURE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_ROUTE_DB_ERROR)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("runs_list.unexpected_error", extra={"type": type(exc).__name__})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None


# Union serialization bounds (PR B, plan §6.1): the per-node node_token_usage
# summary is truncated to the NEWEST N nodes on RunResponse, beyond which a
# node_count aggregate is emitted; the full union stays on the run row.
_NODE_TOKEN_USAGE_MAX_NODES = 200
# Union display clamp — a hostile model_cost_raw_usd cannot reach the UI/money
# formatter through the union surface; the raw value stays in the stored union
# for audit. Same clamp value as the breakdown's RAW_REPORTED_DISPLAY_CLAMP.
_UNION_DISPLAY_CLAMP = Decimal("1000000.0")


def _clamp_node_token_usage_union(ntu: dict[str, Any]) -> dict[str, Any]:
    """Union display clamp for serialization surfaces (RunResponse + MCP).

    ``model_cost_raw_usd`` in each per-node dict is magnitude-clamped at 1e6
    for display; every other value is preserved verbatim. The stored union is
    never mutated.
    """
    out: dict[str, Any] = {}
    for nid, node in ntu.items():
        if not isinstance(node, dict):
            out[nid] = node
            continue
        entry = dict(node)
        raw = entry.get("model_cost_raw_usd")
        if raw is not None:
            try:
                d = Decimal(str(raw))
            except (TypeError, ValueError, ArithmeticError):
                d = None
            if d is not None:
                entry["model_cost_raw_usd"] = (
                    float(d) if d.is_finite() and abs(d) <= _UNION_DISPLAY_CLAMP else float(_UNION_DISPLAY_CLAMP)
                )
        out[nid] = entry
    return out


def _serialize_node_token_usage(ntu: dict[str, Any] | None) -> dict[str, Any] | None:
    """RunResponse serialization of ``node_token_usage``.

    Applies the union display clamp then the per-node truncation bound: when
    more than ``_NODE_TOKEN_USAGE_MAX_NODES`` nodes are present, only the
    newest N (dict insertion order — the union appends as nodes complete) are
    emitted and a ``node_count`` aggregate records the full size.
    """
    if not ntu:
        return None
    clamped = _clamp_node_token_usage_union(ntu)
    total = len(clamped)
    if total <= _NODE_TOKEN_USAGE_MAX_NODES:
        return clamped
    kept = dict(list(clamped.items())[-_NODE_TOKEN_USAGE_MAX_NODES:])
    kept["node_count"] = total
    return kept


class TriggerRunRequest(BaseModel):
    pipeline_id: uuid.UUID
    input_payload: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


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
    # Deep-link to the org's configured OTLP backend (Jaeger-style) for this
    # run's trace. Only populated on the detail endpoint when the org has an
    # otlp_endpoint configured — always None on list/trigger responses.
    trace_url: str | None = None
    node_token_usage: dict[str, Any] | None = None
    # Cost breakdown — component snapshots (amounts as strings). NULL for
    # pre-migration runs; amounts ride the breakdown serializer which owns the
    # raw_reported display clamp. UNGATED (Free-tier orgs see their own).
    cost_breakdown: list[dict[str, Any]] | None = None
    # Child-run cost rollup. `total_cost_usd` stays own-run cost; these are
    # derived display fields (0.000000 when no children / all NULL) that never
    # touch the stored column.
    child_runs_cost_usd: Decimal = Decimal(_DEFAULT_FLOAT_DISPLAY)
    child_runs_count: int = 0
    aggregate_cost_usd: Decimal = Decimal(_DEFAULT_FLOAT_DISPLAY)
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    # FAR-228: the stored run-outcome classification record (FAR-189) and the
    # derived gate-fired flag. gate_fired is True when the idempotency gate
    # suppressed a delivery retry (error_code harness.idempotency_gate), or the
    # classification reason is email_delivered, or any raw-output marker carries
    # delivery_done — this makes guard-A completions (error_code=None) API
    # distinguishable from an ordinary complete run.
    run_classification: dict[str, Any] | None = None
    gate_fired: bool = False
    # FAR-213 blocked-partial summary — structured record of run-termination
    # compensation for a guardrail-blocked run (executed nodes, per-node
    # publish status, compensation outcomes). None for non-blocked / pre-column
    # runs.
    blocked_partial_summary: dict[str, Any] | None = None
    # FAR-223 item 11 — per-run guardrail interception snapshot (bound /
    # evaluated / passed / violated / observed / errored / redacted / skipped /
    # expected_skips / unexpected_skips). NULL when the run had no guardrails
    # bound, or on pre-migration runs.
    guardrail_summary: dict[str, int] | None = None


def _run_gate_fired(run: Any) -> bool:
    """Derive whether the FAR-228 idempotency gate fired for a run row.

    True when (a) the run's error_code is ``harness.idempotency_gate`` (guard B
    suppression), (b) the stored classification reason is ``email_delivered``,
    or (c) any raw-output marker carries ``delivery_done is True`` (guard A /
    success-path stamp / cancelled-retention). Never raises on non-dict columns.
    """
    # The DB stores the RAW spelling for legacy rows (``idempotency_gate``) and
    # the dotted registry code (``harness.idempotency_gate``) for new writes, so
    # the read is routed through ``map_legacy_code`` to match both.
    if map_legacy_code(getattr(run, "error_code", None)) == "harness.idempotency_gate":
        return True
    classification = getattr(run, "run_classification", None)
    if isinstance(classification, dict) and classification.get("reason") == REASON_DELIVERED_EMAIL:
        return True
    markers = getattr(run, "raw_output_markers", None)
    return bool(_any_marker_delivery_done(markers))


def _guardrail_summary_from_run(run: Any) -> dict[str, int] | None:
    """Parse the persisted ``guardrail_summary_json`` for run detail (item 11).

    Defensive like ``run_classification``: the JSON column could hold any JSON
    value (or a MagicMock in tests) — a non-dict/malformed value degrades to
    None, never a 500.
    """
    raw = getattr(run, "guardrail_summary_json", None)
    if not isinstance(raw, dict) or not raw:
        return None
    try:
        return GuardrailSummary.from_mapping(raw).to_dict()
    except (TypeError, ValueError):
        _log.warning("runs.guardrail_summary_invalid", extra={"run_id": str(getattr(run, "id", ""))})
        return None


def _build_run_response(
    run: Any,
    child_cost: Decimal | None = None,
    child_count: int = 0,
    otlp_endpoint: str | None = None,
) -> RunResponse:
    """Build a RunResponse from a Run ORM entity, populating derived fields."""
    token_consumption: dict[str, Any] | None = None
    if run.total_tokens is not None:
        token_consumption = {"total_tokens": run.total_tokens}

    trace_id: str | None = None
    trace_url: str | None = None
    if run.langgraph_thread_id:
        trace_id = trace_id_for_thread(run.langgraph_thread_id)
        if otlp_endpoint:
            trace_url = f"{otlp_endpoint.rstrip('/')}/jaeger/ui/trace/{trace_id}"

    pipeline_name: str | None = None
    if run.pipeline is not None:
        pipeline_name = run.pipeline.name

    child_runs_cost_usd = _quantize_cost_rollup(child_cost if child_cost is not None else _COST_ROLLUP_ZERO)
    own_cost = run.total_cost_usd if run.total_cost_usd is not None else _COST_ROLLUP_ZERO

    error_code, error_detail = present_error(run.error_code, run.error_detail, limit=5000)

    # FAR-228: defensive coercion — the run_classification JSON column could
    # hold any JSON value (or a MagicMock in tests); a non-dict is surfaced as
    # None, never a 500. gate_fired is derived in _run_gate_fired (also guarded).
    run_classification = run.run_classification if isinstance(run.run_classification, dict) else None

    # FAR-213: same defensive coercion for the blocked_partial_summary column.
    blocked_partial_summary = run.blocked_partial_summary if isinstance(run.blocked_partial_summary, dict) else None

    return RunResponse(
        run_id=run.id,
        status=run.status,
        pipeline_id=run.pipeline_id,
        run_number=run.run_number,
        pipeline_name=pipeline_name,
        langgraph_thread_id=run.langgraph_thread_id,
        error_detail=error_detail,
        error_code=error_code,
        total_cost_usd=run.total_cost_usd,
        token_consumption=token_consumption,
        trace_id=trace_id,
        trace_url=trace_url,
        node_token_usage=_serialize_node_token_usage(run.node_token_usage),
        cost_breakdown=run.cost_breakdown,
        child_runs_cost_usd=child_runs_cost_usd,
        child_runs_count=child_count,
        aggregate_cost_usd=_quantize_cost_rollup(own_cost + child_runs_cost_usd),
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        run_classification=run_classification,
        gate_fired=_run_gate_fired(run),
        blocked_partial_summary=blocked_partial_summary,
        guardrail_summary=_guardrail_summary_from_run(run),
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
    principal: TenantPrincipal = require_permission_any_credential("run.trigger"),
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

            # Pipeline-level rate limit check
            rl = pipeline.rate_limit_config
            if rl and rl.get("max_triggers"):
                key = TriggerEngine._compute_rate_limit_key(req.input_payload, rl)
                recent_count = await TriggerEngine._count_recent_rate_limited(
                    session, pipeline.id, key, int(rl.get("window_seconds", 3600))
                )
                if recent_count >= int(rl["max_triggers"]):
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail=(
                            f"Rate limit exceeded: {rl['max_triggers']} triggers per {rl.get('window_seconds', 3600)}s"
                        ),
                    )
            else:
                key = None

            run = await create_run(
                session,
                org_id=org_id,
                pipeline_id=pipeline.id,
                snapshot_id=snapshot.id,
                trigger_type="manual",
                input_payload=req.input_payload,
                rate_limit_key=key,
            )
            run_id = run.id
    except IntegrityError:
        _log.exception(_CODE_RUNS_TRIGGER_RUN)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_RUNS_TRIGGER_RUN)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_FEATURE_NOT_AVAILABLE_FEATURE,
        ) from None

    except SQLAlchemyError:
        _log.warning(_CODE_ROUTE_DB_ERROR, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from None

    except OrgDeletedError as exc:
        _log.exception(_CODE_RUNS_TRIGGER_RUN)
        if exc.deleted:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot create run: organisation {exc.org_id} is deleted",
            ) from None
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cannot create run: organisation {exc.org_id} not found",
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception(_CODE_PIPELINE_EXECUTION_UNEXPECTED_ERROR)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    await dispatch_run(str(run_id), str(org_id), queue="runs")

    return _build_run_response(run)


# ---------------------------------------------------------------------------
# Run stats / analytics
# ---------------------------------------------------------------------------


@router.get("/stats", response_model=dict[str, Any])
@handle_db_errors("runs.get_run_stats_endpoint")
async def get_run_stats_endpoint(
    period: str = Query(default="30d", pattern=r"^(7d|30d|90d)$"),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_RUN_LIST),
) -> dict[str, Any]:
    """Aggregated run stats for a period (7d|30d|90d)."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            return await get_run_stats(session, period)
    except ProgrammingError:
        _log.exception("runs.get_run_stats_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_FEATURE_NOT_AVAILABLE_FEATURE,
        ) from None

    except SQLAlchemyError:
        _log.warning(_CODE_ROUTE_DB_ERROR, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception(_CODE_PIPELINE_EXECUTION_UNEXPECTED_ERROR)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None


@router.get("/stats/heatmap", response_model=list[dict[str, Any]])
@handle_db_errors("runs.get_run_heatmap_endpoint")
async def get_run_heatmap_endpoint(
    year: int = Query(default=2026, ge=2020, le=2100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_RUN_LIST),
) -> list[dict[str, Any]]:
    """Run counts per day for the given year (calendar heatmap)."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            return await get_run_heatmap(session, year)
    except ProgrammingError:
        _log.exception("runs.get_run_heatmap_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_FEATURE_NOT_AVAILABLE_FEATURE,
        ) from None

    except SQLAlchemyError:
        _log.warning(_CODE_ROUTE_DB_ERROR, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception(_CODE_PIPELINE_EXECUTION_UNEXPECTED_ERROR)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None


@router.get("/{run_id}", response_model=RunResponse)
async def get_run_status(
    run_id: uuid.UUID,
    factory: async_sessionmaker[AsyncSession] = Depends(_get_session_factory),
    principal: TenantPrincipal = require_permission_any_credential("run.status"),
) -> RunResponse:
    try:
        run = await _run_with_retry(lambda: _do_get_run(factory, principal, run_id))
        child_cost, child_count = await _run_with_retry(lambda: _do_get_child_run_rollup(factory, principal, run_id))
        otlp_endpoint = await _do_get_otel_endpoint(factory, principal.organisation_id)
    except IntegrityError:
        _log.exception("runs.get_run_status")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None

    except ProgrammingError:
        _log.exception("runs.get_run_status")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_FEATURE_NOT_AVAILABLE_FEATURE,
        ) from None

    except SQLAlchemyError:
        _log.warning(_CODE_ROUTE_DB_ERROR, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from None
    except RunNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_MSG_RUN_NOT_FOUND,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception(_CODE_PIPELINE_EXECUTION_UNEXPECTED_ERROR)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    return _build_run_response(run, child_cost, child_count, otlp_endpoint=otlp_endpoint)


@router.post("/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("run.cancel"),
) -> dict[str, str]:
    """Request cancellation of a run.

    Returns 202 immediately. The run may transition to cancelled asynchronously.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            run = await get_run(session, run_id)

            if run is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_RUN_NOT_FOUND)

            if run.status in TERMINAL_STATUSES:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Run is already in terminal status: {run.status}",
                )

            # PAUSED-then-cancelled class (awaiting_human/claimed) runs NO
            # finalize (§4.2). A STREAMED running run cancelled cross-process is
            # routed through finalize_cost, re-reading the STORED cumulative
            # sets; a NEVER-PAUSED in-flight run has none and forfeits its
            # accrued cost (cost_components_partial_spend_lost log).
            was_paused = run.status in ("awaiting_human", "claimed")
            await request_cancellation(session, run_id)
            if not was_paused:
                from modulo.core.cost_controller.finalize import finalize_cancelled_run

                await finalize_cancelled_run(session, run_id=run_id, org_id=principal.organisation_id)
    except IntegrityError:
        _log.exception("runs.cancel_run")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception("runs.cancel_run")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_FEATURE_NOT_AVAILABLE_FEATURE,
        ) from None

    except SQLAlchemyError:
        _log.warning(_CODE_ROUTE_DB_ERROR, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception(_CODE_PIPELINE_EXECUTION_UNEXPECTED_ERROR)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
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
    node_telemetry: dict[str, Any] | None = None
    fixture_map: dict[str, str] | None = None
    #: node_id -> human label from the snapshot graph (frontend UUID hygiene).
    node_labels: dict[str, str] = Field(default_factory=dict)

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

    # Resolve every per-node value through node_return (the legacy-safe pure
    # return accessor). For legacy rows it returns each value verbatim, so the
    # fixture_map is byte-identical to today; once P1 writes pure returns the
    # fixture logic keeps reading the same accessor.
    resolved_out: Any = out
    if isinstance(out, dict):
        resolved_out = {node_id: node_return(out, None, node_id) for node_id in out}

    if isinstance(resolved_out, dict) and any(
        isinstance(v, dict) and "input" in v and "output" in v for v in resolved_out.values()
    ):
        for node_io in resolved_out.values():
            if isinstance(node_io, dict):
                node_input = node_io.get("input", json.dumps(inp, sort_keys=True))
                node_output = node_io.get("output", "")
                key = " ".join(str(node_input).split())
                fixture[key] = str(node_output)
    else:
        key = " ".join(str(inp).split())
        fixture[key] = str(resolved_out)

    return fixture


class FixtureExportResponse(BaseModel):
    fixture_name: str
    run_id: uuid.UUID
    pipeline_id: uuid.UUID
    status: str
    snapshot_graph_json: dict[str, Any] = Field(default_factory=dict)
    input_payload: dict[str, Any] | None = None
    outputs_json: dict[str, Any] | None = None
    fixture_map: dict[str, str]


@router.get("/{run_id}/io", response_model=RunIOResponse)
@handle_db_errors("runs.get_run_io_endpoint")
async def get_run_io_endpoint(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_RUN_OUTPUT),
) -> RunIOResponse:
    """Return per-node IO for a completed run, plus generated fixture_map.

    The response exposes a single NORMALIZED view (FAR-126): ``outputs_json``
    holds each node's pure return and ``node_telemetry`` holds its exhaustive
    telemetry. Both are resolved through the legacy-safe accessors
    (``node_return`` / ``node_telemetry``), so legacy runs (no telemetry
    column) are byte-identical to today's envelope shape, and P1+ runs expose
    the split surfaces. Telemetry-only nodes (e.g. ``skipped`` recovery
    markers without an ``outputs_json`` entry) still appear under
    ``node_telemetry``. All surfaces — input payload, outputs, telemetry —
    are masked for secrets.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            run = await get_run(session, run_id)
            snapshot = None
            if run is not None and run.snapshot_id:
                from modulo.db.models.pipeline_snapshot import PipelineSnapshot as SnapModel

                snap_result = await session.execute(select(SnapModel).where(SnapModel.id == run.snapshot_id))
                snapshot = snap_result.scalar_one_or_none()
    except IntegrityError:
        _log.exception("runs.get_run_io_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception("runs.get_run_io_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_FEATURE_NOT_AVAILABLE_FEATURE,
        ) from None

    except SQLAlchemyError:
        _log.warning(_CODE_ROUTE_DB_ERROR, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception(_CODE_PIPELINE_EXECUTION_UNEXPECTED_ERROR)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_RUN_NOT_FOUND)

    node_labels: dict[str, str] = {}
    if snapshot is not None and isinstance(snapshot.graph_json, dict):
        for n in snapshot.graph_json.get("nodes", []):
            if isinstance(n, dict) and n.get("id"):
                node_labels[str(n["id"])] = str(n.get("label") or n.get("node_type") or n.get("id"))

    outputs_json = run.outputs_json
    telemetry_json = run.node_telemetry_json

    # One shape for the frontend: node_return resolves the pure return (new
    # rows) or the envelope verbatim (legacy rows); node_telemetry resolves
    # the stored telemetry (new rows) or the inner output envelope (legacy).
    normalized_outputs = (
        {nid: node_return(outputs_json, telemetry_json, nid) for nid in outputs_json} if outputs_json else outputs_json
    )
    node_ids = set(outputs_json or {}) | set(telemetry_json or {})
    normalized_telemetry = (
        {nid: node_telemetry(telemetry_json, outputs_json, nid) for nid in node_ids} if node_ids else telemetry_json
    )

    masked_outputs = _mask_output_value(normalized_outputs)
    masked_telemetry = _mask_output_value(normalized_telemetry)
    masked_input = _mask_output_value(run.input_payload) if run.input_payload else None

    resp = RunIOResponse(
        run_id=run.id,
        run_number=run.run_number,
        status=run.status,
        input_payload=masked_input,
        outputs_json=masked_outputs,
        node_telemetry=masked_telemetry,
        node_labels=node_labels,
    )
    resp.fixture_map = resp.build_fixture_map()
    return resp


@router.get("/{run_id}/export-fixture", response_model=FixtureExportResponse)
@handle_db_errors("runs.export_run_fixture")
async def export_run_fixture(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_RUN_OUTPUT),
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
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_RUN_NOT_FOUND)

            from modulo.db.models.pipeline_snapshot import PipelineSnapshot as SnapModel

            snap_result = await session.execute(select(SnapModel).where(SnapModel.id == run.snapshot_id))
            snapshot = snap_result.scalar_one_or_none()
    except IntegrityError:
        _log.exception("runs.export_run_fixture")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception("runs.export_run_fixture")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_FEATURE_NOT_AVAILABLE_FEATURE,
        ) from None

    except SQLAlchemyError:
        _log.warning(_CODE_ROUTE_DB_ERROR, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception(_CODE_PIPELINE_EXECUTION_UNEXPECTED_ERROR)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    graph_json = snapshot.graph_json if snapshot else {}

    # Normalize to the pure return before masking (FAR-126): node_return
    # resolves each node's pure return for new-shape rows (telemetry present)
    # and returns the legacy envelope verbatim otherwise, so the exported
    # outputs_json mirrors GET /runs/{id}/io and legacy runs stay byte-identical.
    outputs_json = run.outputs_json
    telemetry_json = run.node_telemetry_json
    normalized_outputs = (
        {nid: node_return(outputs_json, telemetry_json, nid) for nid in outputs_json} if outputs_json else outputs_json
    )

    masked_input = _mask_output_value(run.input_payload) if run.input_payload else None
    masked_outputs = _mask_output_value(normalized_outputs) if normalized_outputs else None
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


@router.get("/{run_id}/workspace-lease")
@handle_db_errors("runs.get_run_workspace_lease")
async def get_run_workspace_lease(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_RUN_OUTPUT),
) -> dict[str, Any] | None:
    """Return the WorkspaceLease associated with a run, if any."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            from modulo.db.models.workspace_lease import WorkspaceLease

            result = await session.execute(select(WorkspaceLease).where(WorkspaceLease.run_id == run_id))
            lease = result.scalar_one_or_none()
    except IntegrityError:
        _log.exception("runs.get_run_workspace_lease")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception("runs.get_run_workspace_lease")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_FEATURE_NOT_AVAILABLE_FEATURE,
        ) from None

    except SQLAlchemyError:
        _log.warning(_CODE_ROUTE_DB_ERROR, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception(_CODE_PIPELINE_EXECUTION_UNEXPECTED_ERROR)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
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


@router.get("/{run_id}/workspace-events")
@handle_db_errors("runs.get_run_workspace_events")
async def get_run_workspace_events(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_RUN_OUTPUT),
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
        _log.exception("runs.get_run_workspace_events")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception("runs.get_run_workspace_events")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_FEATURE_NOT_AVAILABLE_FEATURE,
        ) from None

    except SQLAlchemyError:
        _log.warning(_CODE_ROUTE_DB_ERROR, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception(_CODE_PIPELINE_EXECUTION_UNEXPECTED_ERROR)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    return [
        {
            "event": evt.event_type.replace("workspace_", ""),
            "detail": sanitize_error_text((evt.payload_json or {}).get("detail", "")),
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
@handle_db_errors("runs.get_run_node_output")
async def get_run_node_output(
    run_id: uuid.UUID,
    node_id: str,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_RUN_OUTPUT),
) -> NodeOutputResponse:
    """Return a specific node's output from a completed pipeline run.

    Sensitive fields (keys matching *token*, *secret*, *api_key*,
    *password*, *key*, *credential*) in the output are masked with
    bullet characters.

    For P1+ (split) rows this returns the node's PURE return. When a node
    has no return (skipped / recovered / failed-no-return) but exists in
    ``node_telemetry_json``, a DERIVED ``{status, summary}`` object is
    returned instead of a 404 — never the raw telemetry (no stdout / log
    tail on this surface).
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            run = await get_run(session, run_id)
    except IntegrityError:
        _log.exception("runs.get_run_node_output")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception("runs.get_run_node_output")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_FEATURE_NOT_AVAILABLE_FEATURE,
        ) from None

    except SQLAlchemyError:
        _log.warning(_CODE_ROUTE_DB_ERROR, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception(_CODE_PIPELINE_EXECUTION_UNEXPECTED_ERROR)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_RUN_NOT_FOUND)

    outputs = run.outputs_json or {}
    telemetry = run.node_telemetry_json or {}
    node_output = node_return(outputs, telemetry, node_id)
    if node_output is None:
        node_meta = node_telemetry(telemetry, outputs, node_id)
        if isinstance(node_meta, dict):
            derived = {key: node_meta[key] for key in ("status", "summary") if key in node_meta}
            masked = _mask_output_value(derived)
            return NodeOutputResponse(run_id=run_id, node_id=node_id, output=masked)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Node {node_id} not found in run outputs",
        )

    masked = _mask_output_value(node_output)
    return NodeOutputResponse(run_id=run_id, node_id=node_id, output=masked)


# ---------------------------------------------------------------------------
# Live run events (live stdout/stderr streaming, FAR-98)
# ---------------------------------------------------------------------------


class RunEventItem(BaseModel):
    seq: int
    event_type: str
    payload: dict[str, Any]
    ts: str


class RunEventsResponse(BaseModel):
    run_id: uuid.UUID
    events: list[RunEventItem]


@router.get("/{run_id}/events", response_model=RunEventsResponse)
@handle_db_errors("runs.get_run_events")
async def get_run_events(
    run_id: uuid.UUID,
    since_seq: int = Query(0, ge=0),
    node_id: str | None = Query(None),
    factory: async_sessionmaker[AsyncSession] = Depends(_get_session_factory),
    principal: TenantPrincipal = require_permission_any_credential("run.status"),
) -> RunEventsResponse:
    """Return live chunk events for a run since a sequence number.

    Only ``node.stdout_chunk`` / ``node.stderr_chunk`` events (the live-output
    surface published by sandbox_agent nodes) are returned. Optionally filter
    to a single ``node_id``. The run's org-scoped existence is validated first
    so callers can never observe another org's run events.
    """
    try:
        run = await _run_with_retry(lambda: _do_get_run(factory, principal, run_id))
    except RunNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_RUN_NOT_FOUND) from None
    broker = get_registry().get(run.id)
    events: list[RunEventItem] = []
    if broker is not None:
        for evt in broker.replay_since(since_seq):
            if evt.event_type not in ("node.stdout_chunk", "node.stderr_chunk"):
                continue
            if node_id is not None and evt.payload.get("node_id") != node_id:
                continue
            events.append(
                RunEventItem(
                    seq=evt.seq,
                    event_type=evt.event_type,
                    payload=evt.payload,
                    ts=evt.timestamp.isoformat(),
                )
            )
    return RunEventsResponse(run_id=run.id, events=events)


# ---------------------------------------------------------------------------
# Node observation (task-nv24-node-observed-human)
# ---------------------------------------------------------------------------


class ObserveNodeResponse(BaseModel):
    run_id: uuid.UUID
    node_id: str
    human_observed_at: str | None = None
    human_observed_by: str | None = None


@router.post("/{run_id}/nodes/{node_id}/observe", response_model=ObserveNodeResponse)
@handle_db_errors(_CODE_RUNS_OBSERVE_RUN_NODE)
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
        _log.exception(_CODE_RUNS_OBSERVE_RUN_NODE)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_RUNS_OBSERVE_RUN_NODE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_FEATURE_NOT_AVAILABLE_FEATURE,
        ) from None

    except SQLAlchemyError:
        _log.warning(_CODE_ROUTE_DB_ERROR, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception(_CODE_PIPELINE_EXECUTION_UNEXPECTED_ERROR)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_RUN_NOT_FOUND)

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
        _log.exception(_CODE_RUNS_OBSERVE_RUN_NODE)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_RUNS_OBSERVE_RUN_NODE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_FEATURE_NOT_AVAILABLE_FEATURE,
        ) from None

    except SQLAlchemyError:
        _log.warning(_CODE_ROUTE_DB_ERROR, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception(_CODE_PIPELINE_EXECUTION_UNEXPECTED_ERROR)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
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


@router.post(
    "/{run_id}/nodes/{node_id}/recover",
    response_model=NodeRecoverResponse,
    status_code=status.HTTP_200_OK,
)
@handle_db_errors("runs.recover_run_node")
async def recover_run_node(
    run_id: uuid.UUID,
    node_id: str,
    req: NodeRecoverRequest,
    session: AsyncSession = Depends(get_db_session),
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
            except GuardrailOverrideRequiredError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)[:200]) from exc
            except NodeNotFoundInGraphError as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
            except NodeAlreadyCompletedError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            except ConcurrentRecoveryError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except IntegrityError:
        _log.exception("runs.recover_run_node")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception("runs.recover_run_node")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_FEATURE_NOT_AVAILABLE_FEATURE,
        ) from None

    except SQLAlchemyError:
        _log.warning(_CODE_ROUTE_DB_ERROR, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception(_CODE_PIPELINE_EXECUTION_UNEXPECTED_ERROR)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None
    action = "skip" if req.input_data is None else "replay"

    # Resume the graph with the recovery data. dispatch_run enqueues resume_run
    # to SAQ (the recover-node path); a resume failure surfaces here as 500
    # rather than fire-and-forget 200.
    resume_data: dict[str, Any] = {"action": action, "output": req.input_data}

    try:
        outcome, _job_id = await dispatch_run(
            str(run_id),
            str(principal.organisation_id),
            queue="runs",
            job_type="resume_run",
            resume_data=resume_data,
        )
    except Exception as exc:
        _log.exception("run.recover_node.resume_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to resume pipeline after node recovery",
        ) from exc

    # 'resumed' (shadow inline) and 'enqueued'/'deduped' (SAQ accepted) both
    # leave the run resuming. 'deferred' (capacity-blocked) and
    # 'enqueue_failed' (final enqueue failure after retries) mean the resume
    # was NOT actually dispatched — surface them instead of silently dropping
    # the recovery: the run is left pending and would later be re-dispatched by
    # dispatcher_reconcile as execute_run with resume_data=None, losing the
    # user's replay/skip recovery and any supplied input_data (the run would
    # re-execute from scratch instead of resuming at the recovered node).
    if outcome in ("deferred", "enqueue_failed"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to enqueue pipeline resume after node recovery",
        )

    return NodeRecoverResponse(
        run_id=run_id,
        node_id=node_id,
        action=action,
        status=run.status,
    )


# ---------------------------------------------------------------------------
# Guardrail override (FAR-208 item 6) — the ONLY remediation for a
# guardrail-blocked terminal run (recover_node refuses eval_blocked runs)
# ---------------------------------------------------------------------------


class GuardrailOverrideRequest(BaseModel):
    input_data: dict[str, Any]


class GuardrailOverrideResponse(BaseModel):
    run_id: uuid.UUID
    status: str
    action: str = "override"


@router.post(
    "/{run_id}/guardrail-override",
    response_model=GuardrailOverrideResponse,
    status_code=status.HTTP_200_OK,
)
@handle_db_errors("runs.guardrail_override")
async def guardrail_override_run(
    run_id: uuid.UUID,
    req: GuardrailOverrideRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> GuardrailOverrideResponse:
    """Remediate a guardrail-blocked run with operator-supplied input.

    A guardrail block is TERMINAL ``eval_failed`` (error_code ``eval_blocked``)
    with NO HITL gate, and the generic recover endpoint refuses such runs. The
    override is the ONLY remediation: it re-runs the guardrail pass on the
    supplied ``input_data`` (re-block safe default — a still-violating input is
    refused with 422 and the run stays terminal), persists the post-redaction
    payload, flips the run to ``pending`` with ``is_replay=True``, and
    re-dispatches it from run start (execute_run — the blocked run never
    executed, so there is no checkpoint to resume).

    Requires operator or admin role.
    """
    rate_key = f"guardrail-override:{principal.organisation_id}:{principal.account_id}"
    if not await _guardrail_override_rate_limiter.consume(rate_key):
        _log.warning(
            "runs.guardrail_override.rate_limited",
            extra={"org_id": str(principal.organisation_id), "account_id": str(principal.account_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many guardrail overrides. Try again later.",
        )

    if principal.org_role not in ("admin", "operator"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only operators and admins can override guardrail blocks",
        )

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            try:
                run = await guardrail_override(
                    session,
                    org_id=principal.organisation_id,
                    run_id=run_id,
                    input_data=req.input_data,
                    actor_id=principal.account_id,
                )
            except GuardrailOverrideRejectedError as exc:
                # Still-violating supplied input — re-block safe default. The
                # run stays terminal eval_failed; 422 = the supplied input is
                # unprocessable for this run.
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)[:200]) from exc
            except GuardrailOverrideError as exc:
                # Not a guardrail-blocked terminal run — nothing to override.
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)[:200]) from exc
            except ConcurrentRecoveryError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except IntegrityError:
        _log.exception("runs.guardrail_override")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception("runs.guardrail_override")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_FEATURE_NOT_AVAILABLE_FEATURE,
        ) from None

    except SQLAlchemyError:
        _log.warning(_CODE_ROUTE_DB_ERROR, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception(_CODE_PIPELINE_EXECUTION_UNEXPECTED_ERROR)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    # Re-dispatch the pending run from run start (execute_run). The blocked run
    # never executed, so there is no checkpoint to resume from — dispatch_run
    # enqueues the default execute_run job with no resume data.
    try:
        outcome, _job_id = await dispatch_run(
            str(run_id),
            str(principal.organisation_id),
            queue="runs",
        )
    except Exception as exc:
        _log.exception("run.guardrail_override_dispatch_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to dispatch pipeline after guardrail override",
        ) from exc

    if outcome in ("deferred", "enqueue_failed"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to enqueue pipeline after guardrail override",
        )

    return GuardrailOverrideResponse(run_id=run_id, status=run.status)


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
        (r'(api_key["\']?\s*[:=]\s*["\']?)[^"\'}\s,]+', r"\1" + _MASKED_PLACEHOLDER),
        (r'(secret["\']?\s*[:=]\s*["\']?)[^"\'}\s,]+', r"\1" + _MASKED_PLACEHOLDER),
        (r'(token["\']?\s*[:=]\s*["\']?)[^"\'}\s,]+', r"\1" + _MASKED_PLACEHOLDER),
        (r'(password["\']?\s*[:=]\s*["\']?)[^"\'}\s,]+', r"\1" + _MASKED_PLACEHOLDER),
        (r'(credential["\']?\s*[:=]\s*["\']?)[^"\'}\s,]+', r"\1" + _MASKED_PLACEHOLDER),
        (r'(passwd["\']?\s*[:=]\s*["\']?)[^"\'}\s,]+', r"\1" + _MASKED_PLACEHOLDER),
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
            _log.exception("runs._get_checkpoint_state")
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
        for prev_node_id in outputs_json:
            if prev_node_id == node_id:
                continue
            output = node_return(outputs_json, None, prev_node_id)
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
@handle_db_errors(_CODE_RUNS_REVEAL_NODE_PROMPT)
async def reveal_node_prompt(
    run_id: uuid.UUID,
    node_id: str,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_RUN_OUTPUT),
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
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_RUN_NOT_FOUND)

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
        _log.exception(_CODE_RUNS_REVEAL_NODE_PROMPT)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_RUNS_REVEAL_NODE_PROMPT)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_FEATURE_NOT_AVAILABLE_FEATURE,
        ) from None
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        _log.exception(_CODE_RUNS_REVEAL_NODE_PROMPT)
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


@router.post("/diff", response_model=NodeOutputDiffResponse)
@handle_db_errors("runs.diff_node_output")
async def diff_node_output(
    req: NodeOutputDiffRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_RUN_OUTPUT),
) -> NodeOutputDiffResponse:
    """Diff a specific node's output across two runs.

    Accepts two (run_id, node_id) pairs, fetches each node's output,
    applies sensitive masking, and returns a structured line-level diff
    via the shared modulo.core.line_diff helper.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            run_a = await get_run(session, req.run_id_a)
            run_b = await get_run(session, req.run_id_b)
    except IntegrityError:
        _log.exception("runs.diff_node_output")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None

    except ProgrammingError:
        _log.exception("runs.diff_node_output")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_FEATURE_NOT_AVAILABLE_FEATURE,
        ) from None

    except SQLAlchemyError:
        _log.warning(_CODE_ROUTE_DB_ERROR, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from None

    except HTTPException:
        raise
    except Exception:
        _log.exception(_CODE_PIPELINE_EXECUTION_UNEXPECTED_ERROR)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
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

    node_output_a = node_return(outputs_a, None, req.node_id_a)
    node_output_b = node_return(outputs_b, None, req.node_id_b)

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

    diff_lines = [
        NodeOutputDiffLine(
            type=kind,
            content=content,
            line_a=line_a,
            line_b=line_b,
        )
        for kind, content, line_a, line_b in iter_line_diffs(lines_a, lines_b)
    ]

    has_diff = any(d.type != "unchanged" for d in diff_lines)

    return NodeOutputDiffResponse(
        run_id_a=req.run_id_a,
        run_id_b=req.run_id_b,
        node_output_a=masked_a,
        node_output_b=masked_b,
        diff_lines=diff_lines,
        has_diff=has_diff,
    )
