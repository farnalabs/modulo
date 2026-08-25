"""Eval management endpoints.

URLs:
    POST   /api/v1/evals              — create an eval definition (admin only)
    GET    /api/v1/runs/{run_id}/evals — list eval results for a run
    POST   /api/v1/evals/compare      — side-by-side comparison of two runs
    GET    /api/v1/evals/coverage     — eval coverage map for a pipeline
    POST   /api/v1/evals/from-run     — create eval definition from run data
    PUT    /api/v1/evals/suites/{suite_id}/alerting — configure regression alerting (admin only)
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_DB_OPERATION_FAILED, MSG_FEATURE_NOT_AVAILABLE, MSG_RESOURCE_ALREADY_EXISTS
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import deny_break_glass_mint, get_db_session, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.core.audit_logger import append_audit_event
from modulo.core.eval_engine.coverage_gap import (
    DEFAULT_DIVERGENCE_THRESHOLD,
    DEFAULT_MIN_RUNS,
    compute_coverage_gap,
)
from modulo.core.eval_engine.suite_run import (
    EVAL_LEADERBOARD_DEFAULT_DAYS,
    EVAL_LEADERBOARD_MAX_DAYS,
    aggregate_eval_leaderboard,
    bucket_eval_timeseries,
    build_eval_leaderboard_query,
    build_eval_pipelines_query,
    build_eval_timeseries_query,
    summarise_eval_timeseries,
)
from modulo.core.node_output_split import node_return
from modulo.db.crud.eval_run import non_guardrail_eval_results_clause
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.eval_suite import EvalSuite
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import Run
from modulo.db.rls import set_rls_org, set_rls_user_context

_CODE_EVALS_CREATE_EVAL_DEFINITION = "evals.create_eval_definition"
_CODE_EVAL_LIST = "eval.list"
_CODE_EVALS_LIST_EVAL_DEFINITIONS = "evals.list_eval_definitions"
_CODE_EVALS_EVAL_COVERAGE = "evals.eval_coverage"
_CODE_EVALS_GET_EVAL_DEFINITION = "evals.get_eval_definition"
_MSG_EVAL_DEFINITION_NOT_FOUND = "Eval definition not found"
_CODE_EVALS_UPDATE_EVAL_DEFINITION = "evals.update_eval_definition"
_CODE_EVALS_DELETE_EVAL_DEFINITION = "evals.delete_eval_definition"
_CODE_EVALS_LIST_RUN_EVALS = "evals.list_run_evals"
_CODE_EVALS_COMPARE_EVALS = "evals.compare_evals"
_CODE_EVALS_CREATE_EVAL_RUN = "evals.create_eval_from_run"
_CODE_EVALS_LEADERBOARD = "evals.leaderboard"
_CODE_EVALS_TIMESERIES = "evals.timeseries"
_CODE_EVALS_SUITE_ALERTING = "evals.suite_alerting"
_CODE_EVALS_COVERAGE_GAP = "evals.coverage_gap"
_EVAL_TYPE_PATTERN = r"^(llm_judge|regex|json_schema|custom_function|guardrail|human_set)$"
_MSG_PIPELINE_NOT_FOUND = "Pipeline not found"
_MSG_EVAL_SUITE_NOT_FOUND = "Eval suite not found"


_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["evals"])


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class CreateEvalRequest(BaseModel):
    pipeline_id: uuid.UUID
    node_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=255)
    eval_type: str = Field(pattern=_EVAL_TYPE_PATTERN)
    config_json: dict[str, Any] = Field(default_factory=dict)
    failure_behaviour: str = "warn"
    pass_threshold: float | None = Field(None, ge=0.0, le=1.0)
    suite_id: str | None = None


class EvalDefinitionResponse(BaseModel):
    model_config = {"populate_by_name": True}
    id: uuid.UUID
    pipeline_id: uuid.UUID
    node_id: uuid.UUID | None
    name: str
    eval_type: str
    config_json: dict[str, Any]
    failure_behaviour: str
    pass_threshold: float | None = None
    suite_id: str | None = None
    # Eval-definition version (FAR-382): additive/optional, defaults to 1 so
    # existing clients that don't read it keep working unchanged.
    version: int = 1
    pre_version_raw: dict[str, Any] | None = None
    created_by: uuid.UUID = Field(validation_alias="account_id")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _eval_def_to_dict(eval_def: EvalDefinition) -> dict[str, Any]:
    return {
        "id": str(eval_def.id),
        "pipeline_id": str(eval_def.pipeline_id),
        "node_id": str(eval_def.node_id) if eval_def.node_id else None,
        "name": eval_def.name,
        "eval_type": eval_def.eval_type,
        "config_json": eval_def.config_json,
        "failure_behaviour": eval_def.failure_behaviour,
        "pass_threshold": eval_def.pass_threshold,
        "suite_id": eval_def.suite_id,
        "account_id": str(eval_def.account_id),
        "version": getattr(eval_def, "version", 1),
        "pre_version_raw": getattr(eval_def, "pre_version_raw", None),
    }


def _stamp_eval_definition_version(eval_def: EvalDefinition) -> None:
    """Bump the eval-definition version and snapshot the pre-edit config.

    FAR-382: an edit to an eval definition is a version-scoped event. The prior
    config is captured into ``pre_version_raw`` before mutation so a reversal is
    reconstructable, then ``version`` is incremented. A v1->v2 rubric change is
    therefore explicit — an ``EvalResult`` stamped with v1 never looks like a
    regression against a v2-scoped result.
    """
    eval_def.pre_version_raw = {"config_json": eval_def.config_json}
    eval_def.version = (eval_def.version or 1) + 1


def _validate_guardrail_request(
    *,
    eval_type: str,
    failure_behaviour: str | None,
    config_json: dict[str, Any] | None,
) -> None:
    """Graph-save validation for guardrail definitions (FAR-208 item 5).

    A guardrail binding never carries ``failure_behaviour='retry'`` — a
    guardrail block is TERMINAL (eval_failed) and run-level retries are
    excluded by design. Rejected at the API edge so an invalid binding can
    never reach the graph or the engine.
    """
    if eval_type != "guardrail":
        return
    if failure_behaviour == "retry":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A guardrail may never use failure_behaviour='retry' — guardrail blocks are terminal.",
        )
    if failure_behaviour not in (None, "warn", "block"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Guardrail failure_behaviour must be 'warn' or 'block'.",
        )
    if config_json is None:
        return
    action = config_json.get("action")
    if action is not None and action not in ("observe", "warn", "block", "redact"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Guardrail action must be one of observe|warn|block|redact (got {action!r}).",
        )
    detection_type = config_json.get("type")
    if detection_type is not None and detection_type not in ("regex", "json_schema"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Guardrail detection must be regex|json_schema (got {detection_type!r}).",
        )
    # The ``detection`` envelope (PRD §8.17) is an alternative declaration form;
    # when present, its ``type`` is authoritative and must be deterministic pure
    # detection too — reject a forbidden envelope type at the API edge rather
    # than at run time (where it would fail closed as a mechanism error).
    envelope = config_json.get("detection")
    if isinstance(envelope, dict):
        env_type = envelope.get("type")
        if env_type is not None and env_type not in ("regex", "json_schema"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Guardrail detection envelope type must be regex|json_schema (got {env_type!r}).",
            )


class UpdateEvalRequest(BaseModel):
    node_id: uuid.UUID | None = None
    name: str | None = Field(None, min_length=1, max_length=255)
    eval_type: str | None = Field(None, pattern=_EVAL_TYPE_PATTERN)
    config_json: dict[str, Any] | None = None
    failure_behaviour: str | None = None
    pass_threshold: float | None = Field(None, ge=0.0, le=1.0)
    suite_id: str | None = None


class EvalDefinitionListResponse(BaseModel):
    items: list[EvalDefinitionResponse]
    total: int
    page: int
    page_size: int


class EvalSuiteAlertingRequest(BaseModel):
    """Per-suite regression alerting configuration (FAR-379)."""

    # Rolling N-run baseline window used when resolving the comparison baseline:
    # ``N`` forms the baseline from the N most-recent completed same-tuple prior
    # runs; NULL keeps the single-latest baseline. The window controls HOW MANY
    # prior runs form the baseline, never WHETHER to compare or alert — a NULL
    # window does NOT disable alerting.
    baseline_window: int | None = Field(None, ge=1)
    # Pass-rate drop threshold (fraction 0..1) the observed drop must exceed.
    # NULL defers entirely to the Phase 3 ``regressed`` detection flag.
    minimum_delta: float | None = Field(None, ge=0.0, le=1.0)
    # Silence window (minutes) between regression alerts for a suite. NULL = no
    # time-based rate limit (idempotency on suite_run_id still applies).
    cooldown: int | None = Field(None, ge=0)


class EvalSuiteAlertingResponse(BaseModel):
    suite_id: uuid.UUID
    baseline_window: int | None
    minimum_delta: float | None
    cooldown: int | None


# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/evals",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(deny_break_glass_mint)],
    responses={
        403: {"description": "Forbidden"},
        404: {"description": "Not Found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal Server Error"},
        501: {"description": "Not Implemented"},
        503: {"description": "Service Unavailable"},
    },
)
@handle_db_errors(_CODE_EVALS_CREATE_EVAL_DEFINITION)
async def create_eval_definition(
    req: CreateEvalRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("eval.definition.create"),
) -> dict[str, Any]:
    """Create a new eval definition.

    Admin only. The eval definition is scoped to the caller's organisation.
    """
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can create eval definitions",
        )

    _validate_guardrail_request(
        eval_type=req.eval_type,
        failure_behaviour=req.failure_behaviour,
        config_json=req.config_json,
    )

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            pipeline = (
                await session.execute(
                    select(Pipeline).where(
                        Pipeline.id == req.pipeline_id,
                        Pipeline.organisation_id == principal.organisation_id,
                    )
                )
            ).scalar_one_or_none()
            if pipeline is None:
                raise HTTPException(status_code=404, detail=_MSG_PIPELINE_NOT_FOUND)

            eval_def = EvalDefinition(
                organisation_id=principal.organisation_id,
                pipeline_id=req.pipeline_id,
                node_id=req.node_id,
                name=req.name,
                eval_type=req.eval_type,
                config_json=req.config_json,
                failure_behaviour=req.failure_behaviour,
                pass_threshold=req.pass_threshold,
                suite_id=req.suite_id,
                account_id=principal.account_id,
                version=1,
            )
            session.add(eval_def)
            await session.flush()
    except HTTPException:
        raise
    except IntegrityError:
        _log.exception(_CODE_EVALS_CREATE_EVAL_DEFINITION)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Eval definition references a resource that does not exist.",
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_EVALS_CREATE_EVAL_DEFINITION)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_EVALS_CREATE_EVAL_DEFINITION)
        _log.warning("evals.create_eval_definition_db_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except Exception:
        _log.exception("evals.create_eval_definition_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while creating the eval definition.",
        ) from None

    return _eval_def_to_dict(eval_def)


# ---------------------------------------------------------------------------
# Eval Definition CRUD
# ---------------------------------------------------------------------------


@router.get("/evals")
@handle_db_errors(_CODE_EVALS_LIST_EVAL_DEFINITIONS)
async def list_eval_definitions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    pipeline_id: uuid.UUID | None = None,
    eval_type: str | None = Query(None, pattern=_EVAL_TYPE_PATTERN),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_LIST),
) -> EvalDefinitionListResponse:
    """List eval definitions for the caller's organisation."""
    from sqlalchemy import func as sa_func

    conditions = [EvalDefinition.organisation_id == principal.organisation_id]
    if pipeline_id:
        conditions.append(EvalDefinition.pipeline_id == pipeline_id)
    if eval_type:
        conditions.append(EvalDefinition.eval_type == eval_type)

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            total_q = select(sa_func.count(EvalDefinition.id)).where(*conditions)
            total = (await session.execute(total_q)).scalar() or 0

            q = (
                select(EvalDefinition)
                .where(*conditions)
                .order_by(EvalDefinition.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            rows = (await session.execute(q)).scalars().all()
    except HTTPException:
        raise
    except IntegrityError:
        _log.exception(_CODE_EVALS_LIST_EVAL_DEFINITIONS)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_EVALS_LIST_EVAL_DEFINITIONS)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_EVALS_LIST_EVAL_DEFINITIONS)
        _log.warning("evals.list_eval_definitions_db_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except Exception:
        _log.exception("evals.list_eval_definitions_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while listing eval definitions.",
        ) from None

    return EvalDefinitionListResponse(
        items=[EvalDefinitionResponse(**_eval_def_to_dict(d)) for d in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/evals/coverage  (must be before /evals/{eval_id} to avoid conflict)
# ---------------------------------------------------------------------------


@router.get(
    "/evals/coverage",
    status_code=status.HTTP_200_OK,
    responses={
        404: {"description": "Not Found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal Server Error"},
        501: {"description": "Not Implemented"},
        503: {"description": "Service Unavailable"},
    },
)
@handle_db_errors(_CODE_EVALS_EVAL_COVERAGE)
async def eval_coverage(
    pipeline_id: uuid.UUID = Query(..., description="Pipeline ID"),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_LIST),
) -> dict[str, Any]:
    """Return eval coverage map for a pipeline."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            pipeline = (
                await session.execute(
                    select(Pipeline).where(
                        Pipeline.id == pipeline_id,
                        Pipeline.organisation_id == principal.organisation_id,
                    )
                )
            ).scalar_one_or_none()
            if pipeline is None:
                raise HTTPException(status_code=404, detail=_MSG_PIPELINE_NOT_FOUND)

            nodes_raw = pipeline.graph_nodes_json or []
            node_ids = [str(n.get("id")) for n in nodes_raw if n.get("id")]

            eval_defs_rows = (
                (
                    await session.execute(
                        select(EvalDefinition).where(
                            EvalDefinition.pipeline_id == pipeline_id,
                            EvalDefinition.organisation_id == principal.organisation_id,
                            EvalDefinition.node_id.in_([uuid.UUID(nid) for nid in node_ids if nid]),
                        )
                    )
                )
                .scalars()
                .all()
            )
    except HTTPException:
        raise
    except IntegrityError:
        _log.exception(_CODE_EVALS_EVAL_COVERAGE)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_EVALS_EVAL_COVERAGE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_EVALS_EVAL_COVERAGE)
        _log.warning("evals.eval_coverage_db_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except Exception:
        _log.exception("evals.eval_coverage_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while computing eval coverage.",
        ) from None

    eval_count_by_node: dict[str, int] = {}
    for ed in eval_defs_rows:
        nid = str(ed.node_id)
        eval_count_by_node[nid] = eval_count_by_node.get(nid, 0) + 1

    covered_count = 0
    nodes_result: list[dict[str, Any]] = []
    for n in nodes_raw:
        nid = str(n.get("id", ""))
        name = n.get("name") or n.get("label", "") or nid
        count = eval_count_by_node.get(nid, 0)
        has_evals = count > 0
        if has_evals:
            covered_count += 1
        nodes_result.append(
            {
                "node_id": nid,
                "name": name,
                "has_evals": has_evals,
                "eval_count": count,
            }
        )

    total = len(nodes_result)
    pct = round(covered_count / total * 100, 1) if total else 0.0

    return {
        "nodes": nodes_result,
        "summary": {
            "total_nodes": total,
            "covered_nodes": covered_count,
            "uncovered_nodes": total - covered_count,
            "coverage_pct": pct,
        },
    }


# ---------------------------------------------------------------------------
# GET /api/v1/evals/leaderboard  (must be before /evals/{eval_id} to avoid
# the literal "leaderboard" segment being parsed as a {eval_id} uuid)
# ---------------------------------------------------------------------------


@router.get(
    "/evals/leaderboard",
    status_code=status.HTTP_200_OK,
    responses={
        404: {"description": "Not Found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal Server Error"},
        501: {"description": "Not Implemented"},
        503: {"description": "Service Unavailable"},
    },
)
@handle_db_errors(_CODE_EVALS_LEADERBOARD)
async def eval_leaderboard(
    group_by: str = Query("pipeline", pattern="^(pipeline|node|agent)$"),
    days: int = Query(EVAL_LEADERBOARD_DEFAULT_DAYS, ge=1, le=EVAL_LEADERBOARD_MAX_DAYS),
    eval_id: uuid.UUID | None = None,
    pipeline_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
    model_backend_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_LIST),
) -> dict[str, Any]:
    """Return a per-axis leaderboard ranked by aggregate pass-rate (FAR-378).

    A pure read-model over the ``SuiteRun``/``eval_results`` data. The axis is
    ``pipeline`` | ``node`` | ``agent`` (the model backend that produced the
    output). Pass-rate is computed from the ``passed`` boolean ONLY — raw
    ``score`` is never compared across differing ``eval_type``; each axis entry
    carries a per-``eval_type`` partition (``by_type``) so a mixed-type suite is
    never ranked on a raw score. Org-scoped: every query carries the explicit
    ``organisation_id`` predicate.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            statement, params = build_eval_leaderboard_query(
                org_id=principal.organisation_id,
                group_by=group_by,
                days=days,
                eval_id=eval_id,
                pipeline_id=pipeline_id,
                node_id=node_id,
                model_backend_id=model_backend_id,
            )
            rows = (await session.execute(text(statement), params)).all()
    except HTTPException:
        raise
    except IntegrityError:
        _log.exception(_CODE_EVALS_LEADERBOARD)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_EVALS_LEADERBOARD)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_EVALS_LEADERBOARD)
        _log.warning("evals.leaderboard_db_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except Exception:
        _log.exception("evals.leaderboard_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while computing the eval leaderboard.",
        ) from None

    entries = aggregate_eval_leaderboard(rows, group_by=group_by)
    return {"group_by": group_by, "days": days, "entries": entries}


# ---------------------------------------------------------------------------
# GET /api/v1/evals/{eval_id}/timeseries
# ---------------------------------------------------------------------------


@router.get(
    "/evals/{eval_id}/timeseries",
    status_code=status.HTTP_200_OK,
    responses={
        404: {"description": "Not Found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal Server Error"},
        501: {"description": "Not Implemented"},
        503: {"description": "Service Unavailable"},
    },
)
@handle_db_errors(_CODE_EVALS_TIMESERIES)
async def eval_timeseries(
    eval_id: uuid.UUID,
    days: int = Query(EVAL_LEADERBOARD_DEFAULT_DAYS, ge=1, le=EVAL_LEADERBOARD_MAX_DAYS),
    pipeline_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
    model_backend_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_LIST),
) -> dict[str, Any]:
    """Return a day-bucketed pass-rate time-series for a single eval (FAR-378).

    Zeros the day grid from the window start through today so the series is
    continuous; an absent day is emitted with ``total=0`` and ``pass_rate=None``
    (never ``0.0``). Carries a cross-pipeline rollup (``pipelines``) and a
    window ``summary``. Pass-rate is computed from ``passed`` only, partitioned
    by ``eval_type``.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            eval_def = (
                await session.execute(
                    select(EvalDefinition).where(
                        EvalDefinition.id == eval_id,
                        EvalDefinition.organisation_id == principal.organisation_id,
                    )
                )
            ).scalar_one_or_none()
            if eval_def is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_EVAL_DEFINITION_NOT_FOUND)

            statement, params = build_eval_timeseries_query(
                org_id=principal.organisation_id,
                eval_id=eval_id,
                days=days,
                pipeline_id=pipeline_id,
                node_id=node_id,
                model_backend_id=model_backend_id,
            )
            rows = (await session.execute(text(statement), params)).all()

            pipeline_statement, pipeline_params = build_eval_pipelines_query(
                org_id=principal.organisation_id,
                eval_id=eval_id,
                days=days,
                node_id=node_id,
                model_backend_id=model_backend_id,
            )
            pipeline_rows = (await session.execute(text(pipeline_statement), pipeline_params)).all()
    except HTTPException:
        raise
    except IntegrityError:
        _log.exception(_CODE_EVALS_TIMESERIES)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_EVALS_TIMESERIES)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_EVALS_TIMESERIES)
        _log.warning("evals.timeseries_db_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except Exception:
        _log.exception("evals.timeseries_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while computing the eval time-series.",
        ) from None

    since = datetime.now(UTC) - timedelta(days=days)
    buckets = bucket_eval_timeseries(rows, since=since)
    summary = summarise_eval_timeseries(buckets)
    pipelines = [
        {"pipeline_id": str(r.pipeline_id), "pipeline_name": r.pipeline_name}
        for r in pipeline_rows
        if r.pipeline_id is not None
    ]
    return {
        "eval_id": str(eval_id),
        "eval_name": eval_def.name,
        "days": days,
        "buckets": buckets,
        "summary": summary,
        "pipelines": pipelines,
    }


# ---------------------------------------------------------------------------
# GET /api/v1/eval-coverage-gap  (FAR-381) — the eval-suite insufficiency signal
# ---------------------------------------------------------------------------


@router.get(
    "/eval-coverage-gap",
    status_code=status.HTTP_200_OK,
    responses={
        409: {"description": "Conflict"},
        422: {"description": "Unprocessable Entity"},
        500: {"description": "Internal Server Error"},
        501: {"description": "Not Implemented"},
        503: {"description": "Service Unavailable"},
    },
)
@handle_db_errors(_CODE_EVALS_COVERAGE_GAP)
async def eval_coverage_gap(
    variant_group_id: uuid.UUID | None = Query(None, description="Scope to a variant group"),
    batch_id: uuid.UUID | None = Query(None, description="Scope to a single fired batch"),
    min_runs: int = Query(DEFAULT_MIN_RUNS, ge=1, description="Minimum run count before a signal is emitted"),
    threshold: float = Query(
        DEFAULT_DIVERGENCE_THRESHOLD,
        ge=0.0,
        le=1.0,
        description="Variant-divergence threshold above which variants count as genuinely differing",
    ),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_LIST),
) -> dict[str, Any]:
    """Return the eval coverage-gap signal for a batch or variant group (FAR-381).

    A pure read-model over the ``VariantGroup -> Run -> EvalResult`` lineage.
    Emits a per-eval verdict only once at least ``min_runs`` terminal runs carry
    eval data (statistical significance — llm-judge scores are high-variance),
    and only when variant outputs diverged past ``threshold`` while that eval
    could not differentiate them. A gap routes to ``recommended_action =
    "improve_evals"`` (the eval suite is the problem, not the variants); all
    other cases are ``"ok"``. Org-scoped (explicit ``organisation_id`` predicate
    on every query; ``set_rls_org`` remains defense-in-depth).
    """
    if variant_group_id is None and batch_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Provide a variant_group_id or batch_id to scope the coverage-gap signal.",
        )

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            summary = await compute_coverage_gap(
                session,
                org_id=principal.organisation_id,
                batch_id=batch_id,
                variant_group_id=variant_group_id,
                min_runs=min_runs,
                divergence_threshold=threshold,
            )
    except HTTPException:
        raise
    except IntegrityError:
        _log.exception(_CODE_EVALS_COVERAGE_GAP)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_EVALS_COVERAGE_GAP)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_EVALS_COVERAGE_GAP)
        _log.warning("evals.coverage_gap_db_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except Exception:
        _log.exception("evals.coverage_gap_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while computing the eval coverage-gap signal.",
        ) from None

    return summary.to_dict()


@router.put(
    "/evals/suites/{suite_id}/alerting",
    status_code=status.HTTP_200_OK,
    responses={
        403: {"description": "Forbidden"},
        404: {"description": "Not Found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal Server Error"},
        501: {"description": "Not Implemented"},
        503: {"description": "Service Unavailable"},
    },
)
@handle_db_errors(_CODE_EVALS_SUITE_ALERTING)
async def update_suite_alerting(
    suite_id: uuid.UUID,
    req: EvalSuiteAlertingRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("eval.definition.update"),
) -> EvalSuiteAlertingResponse:
    """Configure regression alerting for an eval suite (FAR-379).

    Admin only. Sets the suite's ``baseline_window`` / ``minimum_delta`` /
    ``cooldown`` so the Alerting layer knows WHEN and HOW OFTEN to page: a
    regression must exceed ``minimum_delta``, and after it fires the suite is
    silent for ``cooldown`` minutes. ``baseline_window`` controls how many of
    the most-recent completed same-tuple prior runs form the comparison baseline
    (``N`` — a rolling N-run baseline; NULL — single-latest). A NULL
    ``baseline_window`` does NOT disable alerting: the window widens the baseline
    but whether to alert is always governed by the comparison result and
    ``minimum_delta``. Additive/non-breaking — every field is optional, and NULL
    clears that field.

    The suite is looked up org-scoped (a cross-org suite can never be
    configured). NULL values are persisted as NULL, wiping the config.
    """
    if principal.org_role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can update eval suites")

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            suite = (
                await session.execute(
                    select(EvalSuite).where(
                        EvalSuite.id == suite_id,
                        EvalSuite.organisation_id == principal.organisation_id,
                    )
                )
            ).scalar_one_or_none()
            if suite is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_EVAL_SUITE_NOT_FOUND)

            updates = req.model_dump(exclude_unset=True)
            for key in ("baseline_window", "minimum_delta", "cooldown"):
                if key in updates:
                    setattr(suite, key, updates[key])
            await session.flush()
    except HTTPException:
        raise
    except IntegrityError:
        _log.exception(_CODE_EVALS_SUITE_ALERTING)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Update would violate a constraint.",
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_EVALS_SUITE_ALERTING)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_EVALS_SUITE_ALERTING)
        _log.warning("evals.suite_alerting_db_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except Exception:
        _log.exception("evals.suite_alerting_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while updating eval suite alerting.",
        ) from None

    return EvalSuiteAlertingResponse(
        suite_id=suite.id,
        baseline_window=suite.baseline_window,
        minimum_delta=float(suite.minimum_delta) if suite.minimum_delta is not None else None,
        cooldown=suite.cooldown,
    )


@router.get("/evals/{eval_id}")
@handle_db_errors(_CODE_EVALS_GET_EVAL_DEFINITION)
async def get_eval_definition(
    eval_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_LIST),
) -> dict[str, Any]:
    """Get a single eval definition by ID."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await session.execute(
                select(EvalDefinition).where(
                    EvalDefinition.id == eval_id,
                    EvalDefinition.organisation_id == principal.organisation_id,
                )
            )
            eval_def = result.scalar_one_or_none()
    except HTTPException:
        raise
    except IntegrityError:
        _log.exception(_CODE_EVALS_GET_EVAL_DEFINITION)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_EVALS_GET_EVAL_DEFINITION)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_EVALS_GET_EVAL_DEFINITION)
        _log.warning("evals.get_eval_definition_db_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except Exception:
        _log.exception("evals.get_eval_definition_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while fetching the eval definition.",
        ) from None
    if eval_def is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_EVAL_DEFINITION_NOT_FOUND)
    return _eval_def_to_dict(eval_def)


@router.put("/evals/{eval_id}", dependencies=[Depends(deny_break_glass_mint)])
@handle_db_errors(_CODE_EVALS_UPDATE_EVAL_DEFINITION)
async def update_eval_definition(
    eval_id: uuid.UUID,
    req: UpdateEvalRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("eval.definition.update"),
) -> dict[str, Any]:
    """Update an eval definition. Admin only."""
    if principal.org_role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can update eval definitions")

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await session.execute(
                select(EvalDefinition).where(
                    EvalDefinition.id == eval_id,
                    EvalDefinition.organisation_id == principal.organisation_id,
                )
            )
            eval_def = result.scalar_one_or_none()
            if eval_def is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_EVAL_DEFINITION_NOT_FOUND)

            updates = req.model_dump(exclude_unset=True)
            new_type = updates.get("eval_type", eval_def.eval_type)
            new_behaviour = updates.get("failure_behaviour")
            if new_behaviour is None:
                new_behaviour = eval_def.failure_behaviour
            new_config = updates.get("config_json", eval_def.config_json)
            _validate_guardrail_request(
                eval_type=new_type,
                failure_behaviour=new_behaviour,
                config_json=new_config,
            )
            # FAR-382 versioning: snapshot the raw pre-edit config so a reversal
            # is reconstructable, then bump the version — a rubric/config change
            # is an explicitly version-scoped event, never a silent regression.
            _stamp_eval_definition_version(eval_def)
            for key, value in updates.items():
                setattr(eval_def, key, value)
            await session.flush()
    except HTTPException:
        raise
    except IntegrityError:
        _log.exception(_CODE_EVALS_UPDATE_EVAL_DEFINITION)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Update would violate a constraint. Check that the referenced pipeline or suite exists.",
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_EVALS_UPDATE_EVAL_DEFINITION)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_EVALS_UPDATE_EVAL_DEFINITION)
        _log.warning("evals.update_eval_definition_db_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except Exception:
        _log.exception("evals.update_eval_definition_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while updating the eval definition.",
        ) from None

    return _eval_def_to_dict(eval_def)


@router.delete(
    "/evals/{eval_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(deny_break_glass_mint)],
)
@handle_db_errors(_CODE_EVALS_DELETE_EVAL_DEFINITION)
async def delete_eval_definition(
    eval_id: uuid.UUID,
    purge: bool = Query(False, description="Hard-remove a soft-deleted guardrail eval definition (step 2)"),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("eval.definition.delete"),
) -> None:
    """Delete an eval definition. Admin only.

    Two-step soft-delete (FAR-309 PR B): a GUARDRAIL eval definition is
    SOFT-deleted (``deleted_at``/``deleted_by`` stamped) instead of hard
    removed, so snapshot pins that reference it keep resolving to the
    skipped-with-audit path rather than a dangling row. A second admin step
    (``?purge=true``) hard-removes soft-deleted rows. Non-guardrail evals
    keep their existing hard delete. Every soft-delete and purge writes an
    org-scoped audit event (best-effort fail-open-with-log, matching the
    admin_orgs audit pattern — a failed audit never rolls back the delete).
    """
    if principal.org_role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can delete eval definitions")

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await session.execute(
                select(EvalDefinition).where(
                    EvalDefinition.id == eval_id,
                    EvalDefinition.organisation_id == principal.organisation_id,
                )
            )
            eval_def = result.scalar_one_or_none()
            if eval_def is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_EVAL_DEFINITION_NOT_FOUND)
            # Capture identity BEFORE any mutation — a hard-deleted ORM
            # instance no longer exposes attributes.
            eval_id_str = str(eval_def.id)
            eval_name = eval_def.name
            is_guardrail = eval_def.eval_type == "guardrail"
            soft = is_guardrail and not purge
            if soft:
                eval_def.deleted_at = datetime.now(UTC)
                eval_def.deleted_by = principal.account_id
            else:
                await session.delete(eval_def)
            # The two-step soft-delete audit applies to GUARDRAIL rows only —
            # a non-guardrail eval keeps its pre-PR-B hard delete (no audit
            # event). ``eval_definition.soft_deleted`` / ``eval_definition.purged``
            # are the only two event types this seam emits.
            if is_guardrail:
                try:
                    await append_audit_event(
                        session,
                        org_id=principal.organisation_id,
                        event_type="eval_definition.soft_deleted" if soft else "eval_definition.purged",
                        actor_user_id=principal.account_id,
                        resource_type="eval_definition",
                        resource_id=eval_id,
                        payload_json={"eval_id": eval_id_str, "name": eval_name, "purge": purge},
                    )
                except Exception:
                    _log.exception(
                        "evals.delete_eval_definition_audit_failed",
                        extra={"org_id": str(principal.organisation_id), "eval_id": eval_id_str},
                    )
    except HTTPException:
        raise
    except IntegrityError:
        _log.exception(_CODE_EVALS_DELETE_EVAL_DEFINITION)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_EVALS_DELETE_EVAL_DEFINITION)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_EVALS_DELETE_EVAL_DEFINITION)
        _log.warning("evals.delete_eval_definition_db_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except Exception:
        _log.exception("evals.delete_eval_definition_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while deleting the eval definition.",
        ) from None


@router.get("/runs/{run_id}/evals", status_code=status.HTTP_200_OK)
@handle_db_errors(_CODE_EVALS_LIST_RUN_EVALS)
async def list_run_evals(
    run_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_LIST),
) -> dict[str, Any]:
    """List all eval results for a given run.

    Returns a paginated list of eval results with the eval definition name
    included for convenience. Requires the run to belong to the caller's
    organisation.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            run_result = await session.execute(
                select(Run).where(
                    Run.id == run_id,
                    Run.organisation_id == principal.organisation_id,
                )
            )
            run = run_result.scalar_one_or_none()
            if run is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

            from sqlalchemy import func as sa_func

            total_q = select(sa_func.count(EvalResult.id)).where(
                EvalResult.run_id == run_id,
                EvalResult.organisation_id == principal.organisation_id,
                non_guardrail_eval_results_clause(),
            )
            total = (await session.execute(total_q)).scalar() or 0

            offset = (page - 1) * page_size
            q = (
                select(EvalResult)
                .where(
                    EvalResult.run_id == run_id,
                    EvalResult.organisation_id == principal.organisation_id,
                    non_guardrail_eval_results_clause(),
                )
                .order_by(EvalResult.evaluated_at.desc())
                .offset(offset)
                .limit(page_size)
            )
            rows = (await session.execute(q)).scalars().all()
    except HTTPException:
        raise
    except IntegrityError:
        _log.exception(_CODE_EVALS_LIST_RUN_EVALS)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_EVALS_LIST_RUN_EVALS)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_EVALS_LIST_RUN_EVALS)
        _log.warning("evals.list_run_evals_db_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except Exception:
        _log.exception("evals.list_run_evals_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while listing run eval results.",
        ) from None

    return {
        "items": [
            {
                "id": str(r.id),
                "run_id": str(r.run_id),
                "node_id": str(r.node_id) if r.node_id else None,
                "eval_id": str(r.eval_id),
                "passed": r.passed,
                "score": r.score,
                "detail": r.detail,
                "evaluated_at": r.evaluated_at.isoformat() if r.evaluated_at else None,
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Request / response schemas for new endpoints
# ---------------------------------------------------------------------------


class CompareEvalsRequest(BaseModel):
    run_id_a: uuid.UUID
    run_id_b: uuid.UUID


class CreateEvalFromRunRequest(BaseModel):
    run_id: uuid.UUID
    node_id: uuid.UUID
    # NOTE: ``guardrail`` is deliberately absent from the from-run vocabulary.
    # The from-run endpoint pre-populates a definition from run OUTPUT — a
    # guardrail is a deny-rule (regex pattern / json_schema) that cannot be
    # derived from a sample, and a stub config would be silently-inert
    # (fail-open) for a data-safety control. Guardrails are authored directly.
    eval_type: str = Field(pattern=r"^(llm_judge|regex|json_schema|custom_function)$")
    name: str = Field(min_length=1, max_length=255)


# ---------------------------------------------------------------------------
# POST /api/v1/evals/compare
# ---------------------------------------------------------------------------


@router.post(
    "/evals/compare",
    status_code=status.HTTP_200_OK,
    responses={
        404: {"description": "Not Found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal Server Error"},
        501: {"description": "Not Implemented"},
        503: {"description": "Service Unavailable"},
    },
)
@handle_db_errors(_CODE_EVALS_COMPARE_EVALS)
async def compare_evals(
    req: CompareEvalsRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_LIST),
) -> dict[str, Any]:
    """Compare eval results between two runs side by side."""
    run_a, run_b, results_a, results_b = await _fetch_compare_evals(req, session, principal)

    eval_ids = {r.eval_id for r in results_a} | {r.eval_id for r in results_b}
    eval_defs = {}
    if eval_ids:
        eval_defs = await _fetch_eval_definitions(eval_ids, session, principal)

    results_by_eval_a: dict[uuid.UUID, Any] = {r.eval_id: r for r in results_a}
    results_by_eval_b: dict[uuid.UUID, Any] = {r.eval_id: r for r in results_b}

    compared: list[dict[str, Any]] = []
    for eid in sorted(eval_ids):
        ra = results_by_eval_a.get(eid)
        rb = results_by_eval_b.get(eid)
        edef = eval_defs.get(eid)
        result_a = _compare_result_payload(ra)
        result_b = _compare_result_payload(rb)
        compared.append(
            {
                "eval_id": str(eid),
                "eval_name": edef.name if edef else "unknown",
                "node_id": _compare_node_id(ra, rb),
                "result_a": result_a,
                "result_b": result_b,
                "delta": round(_compare_score(result_a) - _compare_score(result_b), 4),
            }
        )

    return {
        "run_a": {
            "id": str(run_a.id),
            "created_at": _iso_or_none(run_a.created_at),
            "variant_name": "A",
        },
        "run_b": {
            "id": str(run_b.id),
            "created_at": _iso_or_none(run_b.created_at),
            "variant_name": "B",
        },
        "results": compared,
    }


async def _fetch_compare_evals(
    req: "CompareEvalsRequest",
    session: AsyncSession,
    principal: TenantPrincipal,
) -> tuple[Any, Any, Any, Any]:
    """Load both comparison runs plus their non-guardrail eval results."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            run_a = (
                await session.execute(
                    select(Run).where(
                        Run.id == req.run_id_a,
                        Run.organisation_id == principal.organisation_id,
                    )
                )
            ).scalar_one_or_none()
            if run_a is None:
                raise HTTPException(status_code=404, detail="Run A not found")

            run_b = (
                await session.execute(
                    select(Run).where(
                        Run.id == req.run_id_b,
                        Run.organisation_id == principal.organisation_id,
                    )
                )
            ).scalar_one_or_none()
            if run_b is None:
                raise HTTPException(status_code=404, detail="Run B not found")

            results_a = (
                (
                    await session.execute(
                        select(EvalResult).where(
                            EvalResult.run_id == req.run_id_a,
                            EvalResult.organisation_id == principal.organisation_id,
                            non_guardrail_eval_results_clause(),
                        )
                    )
                )
                .scalars()
                .all()
            )

            results_b = (
                (
                    await session.execute(
                        select(EvalResult).where(
                            EvalResult.run_id == req.run_id_b,
                            EvalResult.organisation_id == principal.organisation_id,
                            non_guardrail_eval_results_clause(),
                        )
                    )
                )
                .scalars()
                .all()
            )
            return run_a, run_b, results_a, results_b
    except HTTPException:
        raise
    except IntegrityError:
        _log.exception(_CODE_EVALS_COMPARE_EVALS)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_EVALS_COMPARE_EVALS)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_EVALS_COMPARE_EVALS)
        _log.warning("evals.compare_evals_first_block_db_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except Exception:
        _log.exception("evals.compare_evals_first_block_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while comparing eval results.",
        ) from None


async def _fetch_eval_definitions(
    eval_ids: set[uuid.UUID],
    session: AsyncSession,
    principal: TenantPrincipal,
) -> dict[uuid.UUID, Any]:
    """Load the eval definitions referenced by the compared results."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            defs_rows = (
                (
                    await session.execute(
                        select(EvalDefinition).where(
                            EvalDefinition.id.in_(eval_ids),
                            EvalDefinition.organisation_id == principal.organisation_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            return {d.id: d for d in defs_rows}
    except HTTPException:
        raise
    except IntegrityError:
        _log.exception(_CODE_EVALS_COMPARE_EVALS)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_EVALS_COMPARE_EVALS)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_EVALS_COMPARE_EVALS)
        _log.warning("evals.compare_evals_second_block_db_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except Exception:
        _log.exception("evals.compare_evals_second_block_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while comparing eval results.",
        ) from None


def _compare_result_payload(result: Any) -> dict[str, Any] | None:
    """Serialise one eval result, or ``None`` when the run has no result for the eval."""
    if result is None:
        return None
    return {
        "passed": result.passed,
        "score": result.score,
        "detail": result.detail,
    }


def _compare_score(result: dict[str, Any] | None) -> float:
    """Return the numeric score of a serialised eval result, defaulting to zero."""
    if result is not None and result.get("score") is not None:
        return float(result["score"])
    return 0.0


def _compare_node_id(ra: Any, rb: Any) -> str | None:
    """Return the first available node id across the A/B results."""
    if ra is not None and ra.node_id:
        return str(ra.node_id)
    if rb is not None and rb.node_id:
        return str(rb.node_id)
    return None


def _iso_or_none(value: Any) -> str | None:
    """Return an ISO-formatted timestamp, or ``None`` when absent."""
    return value.isoformat() if value else None


# ---------------------------------------------------------------------------
# POST /api/v1/evals/from-run
# ---------------------------------------------------------------------------


@router.post(
    "/evals/from-run",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(deny_break_glass_mint)],
    responses={
        403: {"description": "Forbidden"},
        404: {"description": "Not Found"},
        409: {"description": "Conflict"},
        500: {"description": "Internal Server Error"},
        501: {"description": "Not Implemented"},
        503: {"description": "Service Unavailable"},
    },
)
@handle_db_errors(_CODE_EVALS_CREATE_EVAL_RUN)
async def create_eval_from_run(
    req: CreateEvalFromRunRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("eval.definition.create"),
) -> dict[str, Any]:
    """Create an eval definition pre-populated from run output."""
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can create eval definitions",
        )

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            run = (
                await session.execute(
                    select(Run).where(
                        Run.id == req.run_id,
                        Run.organisation_id == principal.organisation_id,
                    )
                )
            ).scalar_one_or_none()
            if run is None:
                raise HTTPException(status_code=404, detail="Run not found")

            pipeline = (
                await session.execute(
                    select(Pipeline).where(
                        Pipeline.id == run.pipeline_id,
                        Pipeline.organisation_id == principal.organisation_id,
                    )
                )
            ).scalar_one_or_none()
            if pipeline is None:
                raise HTTPException(status_code=404, detail=_MSG_PIPELINE_NOT_FOUND)

            outputs = run.outputs_json or {}
            node_output = (
                node_return(outputs, run.node_telemetry_json, str(req.node_id))
                or node_return(outputs, run.node_telemetry_json, req.node_id.hex)
                or {}
            )

            sample_output = node_output if isinstance(node_output, dict) else {"output": str(node_output)}
    except HTTPException:
        raise
    except IntegrityError:
        _log.exception(_CODE_EVALS_CREATE_EVAL_RUN)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_EVALS_CREATE_EVAL_RUN)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_EVALS_CREATE_EVAL_RUN)
        _log.warning(
            "evals.create_eval_from_run_first_block_db_error", extra={"org_id": str(principal.organisation_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except Exception:
        _log.exception("evals.create_eval_from_run_first_block_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while creating an eval from run output.",
        ) from None

    config_json: dict[str, Any] = {}
    if req.eval_type == "regex":
        config_json = {
            "field": next(iter(sample_output.keys())) if sample_output else "",
            "pattern": "",
        }
    elif req.eval_type == "json_schema":
        config_json = {
            "field": next(iter(sample_output.keys())) if sample_output else "",
            "schema": {},
        }
    elif req.eval_type == "llm_judge":
        config_json = {
            "field": next(iter(sample_output.keys())) if sample_output else "",
            "instructions": "",
        }
    elif req.eval_type == "custom_function":
        config_json = {
            "field": next(iter(sample_output.keys())) if sample_output else "",
            "function": "",
        }

    try:
        async with session.begin():
            eval_def = EvalDefinition(
                organisation_id=principal.organisation_id,
                pipeline_id=run.pipeline_id,
                node_id=req.node_id,
                name=req.name,
                eval_type=req.eval_type,
                config_json=config_json,
                failure_behaviour="warn",
                account_id=principal.account_id,
                version=1,
            )
            session.add(eval_def)
            await session.flush()
    except HTTPException:
        raise
    except IntegrityError:
        _log.exception(_CODE_EVALS_CREATE_EVAL_RUN)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Eval definition references a resource that does not exist.",
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_EVALS_CREATE_EVAL_RUN)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_EVALS_CREATE_EVAL_RUN)
        _log.warning(
            "evals.create_eval_from_run_second_block_db_error", extra={"org_id": str(principal.organisation_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except Exception:
        _log.exception(
            "evals.create_eval_from_run_second_block_error", extra={"org_id": str(principal.organisation_id)}
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while creating an eval from run output.",
        ) from None

    result = _eval_def_to_dict(eval_def)
    result["sample_output"] = sample_output
    return result
