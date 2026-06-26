"""Eval management endpoints.

URLs:
    POST   /api/v1/evals              â€” create an eval definition (admin only)
    GET    /api/v1/runs/{run_id}/evals â€” list eval results for a run
    POST   /api/v1/evals/compare      â€” side-by-side comparison of two runs
    GET    /api/v1/evals/coverage     â€” eval coverage map for a pipeline
    POST   /api/v1/evals/from-run     â€” create eval definition from run data
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import Run
from modulo.db.rls import set_rls_org, set_rls_user_context

router = APIRouter(prefix="/api/v1", tags=["evals"])


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class CreateEvalRequest(BaseModel):
    pipeline_id: uuid.UUID
    node_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=255)
    eval_type: str = Field(pattern=r"^(llm_judge|regex|json_schema|custom_function)$")
    config_json: dict[str, Any] = {}
    failure_behaviour: str = "warn"
    pass_threshold: float | None = None
    suite_id: str | None = None


class EvalDefinitionResponse(BaseModel):
    id: uuid.UUID
    pipeline_id: uuid.UUID
    node_id: uuid.UUID | None
    name: str
    eval_type: str
    config_json: dict[str, Any]
    failure_behaviour: str
    pass_threshold: float | None = None
    suite_id: str | None = None
    created_by: uuid.UUID


class EvalResultResponse(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    node_id: uuid.UUID | None
    eval_id: uuid.UUID
    passed: bool
    score: float | None
    detail: str | None
    evaluated_at: str


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
        "created_by": str(eval_def.created_by),
    }


class UpdateEvalRequest(BaseModel):
    node_id: uuid.UUID | None = None
    name: str | None = Field(None, min_length=1, max_length=255)
    eval_type: str | None = Field(None, pattern=r"^(llm_judge|regex|json_schema|custom_function)$")
    config_json: dict[str, Any] | None = None
    failure_behaviour: str | None = None
    pass_threshold: float | None = None
    suite_id: str | None = None


class EvalDefinitionListResponse(BaseModel):
    items: list[EvalDefinitionResponse]
    total: int
    page: int
    page_size: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/evals", response_model=dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_eval_definition(
    body: CreateEvalRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """Create a new eval definition.

    Admin only. The eval definition is scoped to the caller's organisation.
    """
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can create eval definitions",
        )

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.user_id, principal.org_role)
        eval_def = EvalDefinition(
            organisation_id=principal.organisation_id,
            pipeline_id=body.pipeline_id,
            node_id=body.node_id,
            name=body.name,
            eval_type=body.eval_type,
            config_json=body.config_json,
            failure_behaviour=body.failure_behaviour,
            pass_threshold=body.pass_threshold,
            suite_id=body.suite_id,
            created_by=principal.user_id,
        )
        session.add(eval_def)
        await session.flush()

    return _eval_def_to_dict(eval_def)


# ---------------------------------------------------------------------------
# Eval Definition CRUD
# ---------------------------------------------------------------------------


@router.get("/evals", response_model=EvalDefinitionListResponse)
async def list_eval_definitions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    pipeline_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> EvalDefinitionListResponse:
    """List eval definitions for the caller's organisation."""
    from sqlalchemy import func as sa_func

    conditions = [EvalDefinition.organisation_id == principal.organisation_id]
    if pipeline_id:
        conditions.append(EvalDefinition.pipeline_id == pipeline_id)

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.user_id, principal.org_role)

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

    return EvalDefinitionListResponse(
        items=[EvalDefinitionResponse(**_eval_def_to_dict(d)) for d in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/evals/coverage  (must be before /evals/{eval_id} to avoid conflict)
# ---------------------------------------------------------------------------


@router.get("/evals/coverage", status_code=status.HTTP_200_OK)
async def eval_coverage(
    pipeline_id: uuid.UUID = Query(..., description="Pipeline ID"),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """Return eval coverage map for a pipeline."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.user_id, principal.org_role)

        pipeline = (
            await session.execute(
                select(Pipeline).where(
                    Pipeline.id == pipeline_id,
                    Pipeline.organisation_id == principal.organisation_id,
                )
            )
        ).scalar_one_or_none()
        if pipeline is None:
            raise HTTPException(status_code=404, detail="Pipeline not found")

        nodes_raw = pipeline.graph_nodes_json or []
        node_ids = [str(n.get("id")) for n in nodes_raw if n.get("id")]

        eval_defs_rows = (
            (
                await session.execute(
                    select(EvalDefinition).where(
                        EvalDefinition.pipeline_id == pipeline_id,
                        EvalDefinition.node_id.in_([uuid.UUID(nid) for nid in node_ids if nid]),
                    )
                )
            )
            .scalars()
            .all()
        )

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


@router.get("/evals/{eval_id}", response_model=dict[str, Any])
async def get_eval_definition(
    eval_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """Get a single eval definition by ID."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.user_id, principal.org_role)
        result = await session.execute(
            select(EvalDefinition).where(
                EvalDefinition.id == eval_id,
                EvalDefinition.organisation_id == principal.organisation_id,
            )
        )
        eval_def = result.scalar_one_or_none()
    if eval_def is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Eval definition not found")
    return _eval_def_to_dict(eval_def)


@router.put("/evals/{eval_id}", response_model=dict[str, Any])
async def update_eval_definition(
    eval_id: uuid.UUID,
    body: UpdateEvalRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """Update an eval definition. Admin only."""
    if principal.org_role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can update eval definitions")

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.user_id, principal.org_role)
        result = await session.execute(
            select(EvalDefinition).where(
                EvalDefinition.id == eval_id,
                EvalDefinition.organisation_id == principal.organisation_id,
            )
        )
        eval_def = result.scalar_one_or_none()
        if eval_def is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Eval definition not found")

        updates = body.model_dump(exclude_unset=True)
        for key, value in updates.items():
            setattr(eval_def, key, value)
        await session.flush()

    return _eval_def_to_dict(eval_def)


@router.delete("/evals/{eval_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_eval_definition(
    eval_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> None:
    """Delete an eval definition. Admin only."""
    if principal.org_role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can delete eval definitions")

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.user_id, principal.org_role)
        result = await session.execute(
            select(EvalDefinition).where(
                EvalDefinition.id == eval_id,
                EvalDefinition.organisation_id == principal.organisation_id,
            )
        )
        eval_def = result.scalar_one_or_none()
        if eval_def is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Eval definition not found")
        await session.delete(eval_def)


@router.get("/runs/{run_id}/evals", response_model=dict[str, Any], status_code=status.HTTP_200_OK)
async def list_run_evals(
    run_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """List all eval results for a given run.

    Returns a paginated list of eval results with the eval definition name
    included for convenience. Requires the run to belong to the caller's
    organisation.
    """
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.user_id, principal.org_role)

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
        )
        total = (await session.execute(total_q)).scalar() or 0

        offset = (page - 1) * page_size
        q = (
            select(EvalResult)
            .where(
                EvalResult.run_id == run_id,
                EvalResult.organisation_id == principal.organisation_id,
            )
            .order_by(EvalResult.evaluated_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        rows = (await session.execute(q)).scalars().all()

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


class CoverageQueryParams(BaseModel):
    pipeline_id: uuid.UUID


class CreateEvalFromRunRequest(BaseModel):
    run_id: uuid.UUID
    node_id: uuid.UUID
    eval_type: str = Field(pattern=r"^(llm_judge|regex|json_schema|custom_function)$")
    name: str = Field(min_length=1, max_length=255)


# ---------------------------------------------------------------------------
# POST /api/v1/evals/compare
# ---------------------------------------------------------------------------


@router.post("/evals/compare", status_code=status.HTTP_200_OK)
async def compare_evals(
    body: CompareEvalsRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """Compare eval results between two runs side by side."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.user_id, principal.org_role)

        run_a = (
            await session.execute(
                select(Run).where(
                    Run.id == body.run_id_a,
                    Run.organisation_id == principal.organisation_id,
                )
            )
        ).scalar_one_or_none()
        if run_a is None:
            raise HTTPException(status_code=404, detail="Run A not found")

        run_b = (
            await session.execute(
                select(Run).where(
                    Run.id == body.run_id_b,
                    Run.organisation_id == principal.organisation_id,
                )
            )
        ).scalar_one_or_none()
        if run_b is None:
            raise HTTPException(status_code=404, detail="Run B not found")

        results_a = (
            (await session.execute(select(EvalResult).where(EvalResult.run_id == body.run_id_a))).scalars().all()
        )

        results_b = (
            (await session.execute(select(EvalResult).where(EvalResult.run_id == body.run_id_b))).scalars().all()
        )

    eval_ids = {r.eval_id for r in results_a} | {r.eval_id for r in results_b}
    eval_defs = {}
    if eval_ids:
        async with session.begin():
            defs_rows = (
                (await session.execute(select(EvalDefinition).where(EvalDefinition.id.in_(eval_ids)))).scalars().all()
            )
            for d in defs_rows:
                eval_defs[d.id] = d

    results_by_eval_a: dict[uuid.UUID, Any] = {}
    for r in results_a:
        results_by_eval_a[r.eval_id] = r

    results_by_eval_b: dict[uuid.UUID, Any] = {}
    for r in results_b:
        results_by_eval_b[r.eval_id] = r

    compared: list[dict[str, Any]] = []
    all_eval_ids = sorted(eval_ids)
    for eid in all_eval_ids:
        ra = results_by_eval_a.get(eid)
        rb = results_by_eval_b.get(eid)
        edef = eval_defs.get(eid)
        result_a = (
            {
                "passed": ra.passed if ra else False,
                "score": ra.score if ra else None,
                "detail": ra.detail if ra else None,
            }
            if ra
            else None
        )
        result_b = (
            {
                "passed": rb.passed if rb else False,
                "score": rb.score if rb else None,
                "detail": rb.detail if rb else None,
            }
            if rb
            else None
        )
        score_a = result_a["score"] if result_a and result_a["score"] is not None else 0.0
        score_b = result_b["score"] if result_b and result_b["score"] is not None else 0.0
        delta = round(score_a - score_b, 4)
        compared.append(
            {
                "eval_id": str(eid),
                "eval_name": edef.name if edef else "unknown",
                "node_id": str(ra.node_id) if ra and ra.node_id else str(rb.node_id) if rb and rb.node_id else None,
                "result_a": result_a,
                "result_b": result_b,
                "delta": delta,
            }
        )

    return {
        "run_a": {
            "id": str(run_a.id),
            "created_at": run_a.created_at.isoformat() if run_a.created_at else None,
            "variant_name": "A",
        },
        "run_b": {
            "id": str(run_b.id),
            "created_at": run_b.created_at.isoformat() if run_b.created_at else None,
            "variant_name": "B",
        },
        "results": compared,
    }


# ---------------------------------------------------------------------------
# POST /api/v1/evals/from-run
# ---------------------------------------------------------------------------


@router.post("/evals/from-run", status_code=status.HTTP_201_CREATED)
async def create_eval_from_run(
    body: CreateEvalFromRunRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """Create an eval definition pre-populated from run output."""
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can create eval definitions",
        )

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.user_id, principal.org_role)

        run = (
            await session.execute(
                select(Run).where(
                    Run.id == body.run_id,
                    Run.organisation_id == principal.organisation_id,
                )
            )
        ).scalar_one_or_none()
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")

        outputs = run.outputs_json or {}
        node_output = outputs.get(str(body.node_id)) or outputs.get(body.node_id.hex) or {}

        sample_output = node_output if isinstance(node_output, dict) else {"output": str(node_output)}

    config_json: dict[str, Any] = {}
    if body.eval_type == "regex":
        config_json = {
            "field": next(iter(sample_output.keys())) if sample_output else "",
            "pattern": "",
        }
    elif body.eval_type == "json_schema":
        config_json = {
            "field": next(iter(sample_output.keys())) if sample_output else "",
            "schema": {},
        }
    elif body.eval_type == "llm_judge":
        config_json = {
            "field": next(iter(sample_output.keys())) if sample_output else "",
            "instructions": "",
        }
    elif body.eval_type == "custom_function":
        config_json = {
            "field": next(iter(sample_output.keys())) if sample_output else "",
            "function": "",
        }

    async with session.begin():
        eval_def = EvalDefinition(
            organisation_id=principal.organisation_id,
            pipeline_id=run.pipeline_id,
            node_id=body.node_id,
            name=body.name,
            eval_type=body.eval_type,
            config_json=config_json,
            failure_behaviour="warn",
            created_by=principal.user_id,
        )
        session.add(eval_def)
        await session.flush()

    result = _eval_def_to_dict(eval_def)
    result["sample_output"] = sample_output
    return result
