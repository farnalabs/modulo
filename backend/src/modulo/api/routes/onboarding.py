"""First-run onboarding wizard REST API — status, step tracking, and step data."""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.db.crud.pipeline import create_pipeline, replace_pipeline_graph
from modulo.db.crud.schema import create_schema
from modulo.db.models.pipeline import Pipeline
from modulo.db.rls import set_rls_org, set_rls_user_context

logger = logging.getLogger(__name__)

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/onboarding", tags=["onboarding"])

_ONBOARDING_STEPS: list[dict[str, Any]] = [
    {"id": "connect_tools", "label": "Connect Tooling", "order": 1},
    {"id": "select_template", "label": "Select Starter Template", "order": 2},
    {"id": "configure_agent", "label": "Configure First Agent", "order": 3},
    {"id": "run_demo", "label": "Run Demo", "order": 4},
]


@dataclass
class _OnboardingState:
    is_first_run: bool
    completed_steps: list[str]


def _load_onboarding_state() -> _OnboardingState:
    stored_raw = _load_onboarding_json()
    if stored_raw is None:
        return _OnboardingState(is_first_run=True, completed_steps=[])
    return _OnboardingState(
        is_first_run=stored_raw.get("is_first_run", False),
        completed_steps=stored_raw.get("completed_steps", []),
    )


def _load_onboarding_json() -> dict[str, Any] | None:
    import json
    import os

    path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".onboarding-state.json")
    try:
        with open(path) as f:
            return cast("dict[str, Any]", json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_onboarding_state(state: _OnboardingState) -> None:
    import json
    import os

    path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".onboarding-state.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"is_first_run": state.is_first_run, "completed_steps": state.completed_steps}, f)


class OnboardingStatusResponse(BaseModel):
    is_first_run: bool
    completed_steps: list[str]
    current_step: int | None = None
    total_steps: int = 4

    model_config = {"from_attributes": True}


class MarkStepRequest(BaseModel):
    step_id: str


class MarkStepResponse(BaseModel):
    step_id: str
    completed: bool
    completed_steps: list[str]


class OnboardingStepDataResponse(BaseModel):
    step_id: str
    label: str
    order: int
    data: dict[str, Any]


_STEP_DATA: dict[str, dict[str, Any]] = {
    "connect_tools": {
        "title": "Connect Your Tools",
        "description": "Link GitHub, Jira, or Linear to get started.",
        "connectors": [
            {"id": "github", "name": "GitHub", "type": "oauth", "connected": False},
            {"id": "jira", "name": "Jira", "type": "token", "connected": False},
            {"id": "linear", "name": "Linear", "type": "token", "connected": False},
        ],
    },
    "select_template": {
        "title": "Select a Starter Template",
        "description": "Pick a template to kickstart your first pipeline.",
    },
    "configure_agent": {
        "title": "Configure Your First Agent",
        "description": "Tweak the prompt and model for your first agent.",
    },
    "run_demo": {
        "title": "Run the Demo Pipeline",
        "description": "Trigger a demo run and watch it complete step by step.",
    },
}


class StarterPipelineResponse(BaseModel):
    pipeline_id: uuid.UUID
    name: str


@handle_db_errors("onboarding.get_onboarding_status")
@router.get("/status", response_model=OnboardingStatusResponse)
async def get_onboarding_status(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> OnboardingStatusResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            result = await session.execute(
                select(Pipeline).where(Pipeline.organisation_id == principal.organisation_id).limit(1)
            )
            has_pipelines = result.scalar_one_or_none() is not None
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error. Please try again.",
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("onboarding.get_onboarding_status.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        ) from e

    state = _load_onboarding_state()

    if state.is_first_run and has_pipelines:
        state.is_first_run = False
        _save_onboarding_state(state)

    current_step = None
    if state.is_first_run:
        completed_ids = set(state.completed_steps)
        for step in _ONBOARDING_STEPS:
            if step["id"] not in completed_ids:
                current_step = step["order"]
                break

    return OnboardingStatusResponse(
        is_first_run=state.is_first_run,
        completed_steps=state.completed_steps,
        current_step=current_step,
        total_steps=len(_ONBOARDING_STEPS),
    )


@handle_db_errors("onboarding.mark_step_completed")
@router.post("/step", response_model=MarkStepResponse)
async def mark_step_completed(
    req: MarkStepRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> MarkStepResponse:
    try:
        valid_ids: set[str] = {str(s["id"]) for s in _ONBOARDING_STEPS}
        if req.step_id not in valid_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(f"Invalid step_id '{req.step_id}'. Must be one of: {', '.join(sorted(valid_ids))}"),
            )

        state = _load_onboarding_state()
        if req.step_id not in state.completed_steps:
            state.completed_steps.append(req.step_id)

        all_completed = len(state.completed_steps) >= len(_ONBOARDING_STEPS)
        if all_completed:
            state.is_first_run = False

        _save_onboarding_state(state)

        return MarkStepResponse(
            step_id=req.step_id,
            completed=True,
            completed_steps=state.completed_steps,
        )
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("onboarding.mark_step_completed.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        ) from e


@handle_db_errors("onboarding.get_step_data")
@router.get("/step/{step_id}", response_model=OnboardingStepDataResponse)
async def get_step_data(
    step_id: str,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> OnboardingStepDataResponse:
    try:
        step = None
        for s in _ONBOARDING_STEPS:
            if s["id"] == step_id:
                step = s
                break

        if step is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Step '{step_id}' not found",
            )

        data = _STEP_DATA.get(step_id, {})

        if step_id == "select_template":
            try:
                async with session.begin():
                    await set_rls_org(session, principal.organisation_id)
                    from modulo.db.models.library_primitive import LibraryPrimitive

                    templates_result = await session.execute(
                        select(LibraryPrimitive)
                        .where(
                            LibraryPrimitive.organisation_id == principal.organisation_id,
                            LibraryPrimitive.primitive_type == "pipeline_template",
                        )
                        .limit(3)
                    )
                    templates = templates_result.scalars().all()
                    data["templates"] = [
                        {
                            "id": str(t.id),
                            "name": t.name,
                            "description": t.description,
                            "category": t.category,
                            "tags": t.tags or [],
                        }
                        for t in templates
                    ]
            except ProgrammingError:
                logger.warning("onboarding.get_step_data.select_template_missing", exc_info=True)
                raise HTTPException(
                    status_code=status.HTTP_501_NOT_IMPLEMENTED,
                    detail="Feature is not available. Run database migrations to enable it.",
                ) from None

        return OnboardingStepDataResponse(
            step_id=step["id"],
            label=step["label"],
            order=step["order"],
            data=data,
        )
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("onboarding.get_step_data.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        ) from e


@handle_db_errors("onboarding.create_starter_pipeline")
@router.post("/starter-pipeline", response_model=StarterPipelineResponse, status_code=status.HTTP_201_CREATED)
async def create_starter_pipeline(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> StarterPipelineResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            schema = await create_schema(
                session,
                org_id=principal.organisation_id,
                name="Starter Pipeline Schema",
                account_id=principal.account_id,
                description="Auto-generated schema for the SDLC starter pipeline.",
            )

            pipeline = await create_pipeline(
                session,
                org_id=principal.organisation_id,
                name="SDLC Starter Pipeline",
                account_id=principal.account_id,
                description=(
                    "A starter pipeline mapping your SDLC workflow. Customise each stage to match your team's process."
                ),
            )

            schema_id = schema.id
            node_defs = [
                ("Task Review", 50, 200),
                ("Development", 350, 200),
                ("Review & QA", 650, 200),
                ("Promote to Staging", 950, 200),
                ("Promote to Prod", 1250, 200),
            ]

            nodes = []
            edges = []
            prev_id = None
            for label, x, y in node_defs:
                node_id = uuid.uuid4()
                nodes.append(
                    {
                        "id": str(node_id),
                        "node_type": "manual",
                        "position": {"x": x, "y": y},
                        "label": label,
                        "output_schema_id": str(schema_id),
                        "agent_id": None,
                        "connector_binding": None,
                        "role": None,
                        "autonomy_recommendation": None,
                        "composite_ref": None,
                        "composite_parameter_values": None,
                        "composite_input_mapping": None,
                        "composite_output_mapping": None,
                    }
                )
                if prev_id is not None:
                    edges.append(
                        {
                            "id": str(uuid.uuid4()),
                            "source_node_id": str(prev_id),
                            "target_node_id": str(node_id),
                            "edge_type": "normal",
                            "condition_expression": None,
                            "hitl_gate_config": None,
                        }
                    )
                prev_id = node_id

            await replace_pipeline_graph(
                session,
                pipeline_id=pipeline.id,
                org_id=principal.organisation_id,
                nodes=nodes,
                edges=edges,
            )

        return StarterPipelineResponse(
            pipeline_id=pipeline.id,
            name=pipeline.name,
        )
    except ProgrammingError:
        logger.warning("onboarding.get_onboarding_status.table_missing", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        logger.warning("onboarding.get_onboarding_status.db_error", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error. Please try again.",
        ) from None
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("onboarding.create_starter_pipeline.unexpected_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from e
