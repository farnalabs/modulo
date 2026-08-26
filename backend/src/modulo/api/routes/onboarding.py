"""First-run onboarding REST API — action-based recommended actions with DB persistence."""

import logging
import uuid
from operator import itemgetter
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_permission
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.db.crud.agent import create_agent
from modulo.db.crud.pipeline import create_pipeline, replace_pipeline_graph
from modulo.db.crud.schema import create_schema, create_schema_version
from modulo.db.models.agent import Agent
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.onboarding_progress import OnboardingProgress
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import Run
from modulo.db.models.schema import Schema
from modulo.db.rls import set_rls_org, set_rls_user_context

_MSG_TRUTH_CLASSIFIER = "Truth Classifier"


_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/onboarding", tags=["onboarding"])

_ONBOARDING_ACTIONS: list[dict[str, Any]] = [
    {
        "id": "login",
        "title": "Log in",
        "description": "Authenticate to access the platform.",
        "order": 1,
        "icon": "log-in",
        "route": "/login",
        "auto_check": True,
        "check_type": None,
    },
    {
        "id": "add_ai_model",
        "title": "Add an AI Model",
        "description": "Configure an AI model backend for your agents.",
        "order": 2,
        "icon": "brain",
        "route": "/settings/model-backends",
        "auto_check": False,
        "check_type": "has_model_backend",
    },
    {
        "id": "create_first_agent",
        "title": "Create Your First Agent",
        "description": "Build an agent to process your data.",
        "order": 3,
        "icon": "bot",
        "route": "/agents/create",
        "auto_check": False,
        "check_type": "has_agents",
    },
    {
        "id": "create_first_schema",
        "title": "Create Your First Schema",
        "description": "Define structured data for your pipelines.",
        "order": 4,
        "icon": "database",
        "route": "/schemas/create",
        "auto_check": False,
        "check_type": "has_schemas",
    },
    {
        "id": "create_first_pipeline",
        "title": "Create Your First Pipeline",
        "description": "Build a pipeline to automate your workflow.",
        "order": 5,
        "icon": "git-branch",
        "route": "/pipelines/create",
        "auto_check": False,
        "check_type": "has_pipelines",
    },
    {
        "id": "run_first_pipeline",
        "title": "Run Your First Pipeline",
        "description": "Execute a pipeline and watch it complete.",
        "order": 6,
        "icon": "play",
        "route": "/runs",
        "auto_check": False,
        "check_type": "has_runs",
    },
]


class OnboardingStatusResponse(BaseModel):
    is_first_run: bool
    progress_pct: float
    completed_actions: list[str]
    skipped_actions: list[str]
    dismissed: bool
    actions: list[dict[str, Any]]

    model_config = {"from_attributes": True}


class ActionCompletedResponse(BaseModel):
    action_id: str
    completed: bool
    progress_pct: float


class ActionSkippedResponse(BaseModel):
    action_id: str
    skipped: bool
    progress_pct: float


class DismissResponse(BaseModel):
    dismissed: bool


class SeedExamplesResponse(BaseModel):
    agent_id: uuid.UUID | None = None
    schema_id: uuid.UUID | None = None
    pipeline_id: uuid.UUID | None = None


class StarterPipelineResponse(BaseModel):
    pipeline_id: uuid.UUID
    name: str


async def _get_or_create_progress(
    session: AsyncSession,
    org_id: uuid.UUID,
) -> OnboardingProgress:
    result = await session.execute(select(OnboardingProgress).where(OnboardingProgress.organisation_id == org_id))
    progress = result.scalar_one_or_none()
    if progress is None:
        progress = OnboardingProgress(
            organisation_id=org_id,
            completed_actions=[],
            skipped_actions=[],
            dismissed=False,
        )
        session.add(progress)
        await session.flush()
    return progress


def _compute_progress_pct(completed: list[str], skipped: list[str]) -> float:
    total = len(_ONBOARDING_ACTIONS)
    if total == 0:
        return 100.0
    return round((len(completed) + len(skipped)) / total * 100, 1)


async def _check_auto_completion(
    session: AsyncSession,
    org_id: uuid.UUID,
) -> set[str]:
    auto_completed: set[str] = set()

    auto_completed.add("login")

    model_backend_result = await session.execute(
        select(ModelBackend).where(ModelBackend.organisation_id == org_id).limit(1)
    )
    if model_backend_result.scalar_one_or_none() is not None:
        auto_completed.add("add_ai_model")

    agent_result = await session.execute(select(Agent).where(Agent.organisation_id == org_id).limit(1))
    if agent_result.scalar_one_or_none() is not None:
        auto_completed.add("create_first_agent")

    schema_result = await session.execute(select(Schema).where(Schema.organisation_id == org_id).limit(1))
    if schema_result.scalar_one_or_none() is not None:
        auto_completed.add("create_first_schema")

    pipeline_result = await session.execute(select(Pipeline).where(Pipeline.organisation_id == org_id).limit(1))
    if pipeline_result.scalar_one_or_none() is not None:
        auto_completed.add("create_first_pipeline")

    run_result = await session.execute(select(Run).where(Run.organisation_id == org_id).limit(1))
    if run_result.scalar_one_or_none() is not None:
        auto_completed.add("run_first_pipeline")

    return auto_completed


def _build_action_list(
    completed_actions: list[str],
    skipped_actions: list[str],
    auto_completed: set[str],
) -> list[dict[str, Any]]:
    completed_set = set(completed_actions) | auto_completed
    skipped_set = set(skipped_actions)
    result: list[dict[str, Any]] = []
    for action in sorted(_ONBOARDING_ACTIONS, key=itemgetter("order")):
        aid = action["id"]
        result.append(
            {
                "id": aid,
                "title": action["title"],
                "description": action["description"],
                "order": action["order"],
                "icon": action["icon"],
                "route": action["route"],
                "completed": aid in completed_set,
                "skipped": aid in skipped_set and aid not in completed_set,
                "auto_check": action["auto_check"],
            }
        )
    return result


@router.get("/status")
@handle_db_errors("onboarding.get_onboarding_status")
async def get_onboarding_status(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> OnboardingStatusResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        progress = await _get_or_create_progress(session, principal.organisation_id)
        auto_completed = await _check_auto_completion(session, principal.organisation_id)

    is_first_run = (
        len(progress.completed_actions) == 0 and len(progress.skipped_actions) == 0 and not progress.dismissed
    )

    completed = list(set(progress.completed_actions) | auto_completed)
    skipped = list(set(progress.skipped_actions) - auto_completed)
    progress_pct = _compute_progress_pct(completed, skipped)
    actions = _build_action_list(progress.completed_actions, progress.skipped_actions, auto_completed)

    return OnboardingStatusResponse(
        is_first_run=is_first_run,
        progress_pct=progress_pct,
        completed_actions=completed,
        skipped_actions=skipped,
        dismissed=progress.dismissed,
        actions=actions,
    )


@router.post("/actions/{action_id}/complete")
@handle_db_errors("onboarding.mark_action_completed")
async def mark_action_completed(
    action_id: str,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> ActionCompletedResponse:
    valid_ids = {a["id"] for a in _ONBOARDING_ACTIONS}
    if action_id not in valid_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid action_id '{action_id}'. Must be one of: {', '.join(sorted(valid_ids))}",
        )

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        progress = await _get_or_create_progress(session, principal.organisation_id)
        if action_id not in progress.completed_actions:
            progress.completed_actions = list(set(progress.completed_actions) | {action_id})
        if action_id in progress.skipped_actions:
            progress.skipped_actions = [s for s in progress.skipped_actions if s != action_id]

    completed = list(set(progress.completed_actions))
    skipped = list(progress.skipped_actions)
    progress_pct = _compute_progress_pct(completed, skipped)

    return ActionCompletedResponse(
        action_id=action_id,
        completed=True,
        progress_pct=progress_pct,
    )


@router.post("/actions/{action_id}/skip")
@handle_db_errors("onboarding.mark_action_skipped")
async def mark_action_skipped(
    action_id: str,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> ActionSkippedResponse:
    valid_ids = {a["id"] for a in _ONBOARDING_ACTIONS}
    if action_id not in valid_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid action_id '{action_id}'. Must be one of: {', '.join(sorted(valid_ids))}",
        )

    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        progress = await _get_or_create_progress(session, principal.organisation_id)
        if action_id not in progress.skipped_actions:
            progress.skipped_actions = list(set(progress.skipped_actions) | {action_id})

    completed = list(set(progress.completed_actions))
    skipped = list(progress.skipped_actions)
    progress_pct = _compute_progress_pct(completed, skipped)

    return ActionSkippedResponse(
        action_id=action_id,
        skipped=True,
        progress_pct=progress_pct,
    )


@router.post("/dismiss")
@handle_db_errors("onboarding.dismiss")
async def dismiss_onboarding(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> DismissResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        progress = await _get_or_create_progress(session, principal.organisation_id)
        progress.dismissed = True

    return DismissResponse(dismissed=True)


@router.post("/seed-examples", status_code=status.HTTP_201_CREATED)
@handle_db_errors("onboarding.seed_examples")
async def seed_examples(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("pipeline.create"),
    _agent_perm: TenantPrincipal = require_permission("agent.create"),
    _schema_perm: TenantPrincipal = require_permission("schema.create"),
) -> SeedExamplesResponse:
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)

        truth_schema = await create_schema(
            session,
            org_id=principal.organisation_id,
            name=_MSG_TRUTH_CLASSIFIER,
            account_id=principal.account_id,
            description="Classifies statements as TRUE, FALSE, or UNDETERMINED.",
        )

        definition_json: dict[str, Any] = {
            "type": "object",
            "properties": {
                "statement": {"type": "string"},
                "classification": {"type": "string", "enum": ["TRUE", "FALSE", "UNDETERMINED"]},
                "confidence": {"type": "number"},
            },
            "required": ["statement", "classification", "confidence"],
        }
        await create_schema_version(
            session,
            org_id=principal.organisation_id,
            schema_id=truth_schema.id,
            version="1.0",
            version_number=1,
            definition_json=definition_json,
            account_id=principal.account_id,
            published=True,
        )

        statement_schema = await create_schema(
            session,
            org_id=principal.organisation_id,
            name="Statement Input",
            account_id=principal.account_id,
            description="Input schema containing a single statement string.",
        )
        statement_def: dict[str, Any] = {
            "type": "object",
            "properties": {
                "statement": {"type": "string"},
            },
            "required": ["statement"],
        }
        await create_schema_version(
            session,
            org_id=principal.organisation_id,
            schema_id=statement_schema.id,
            version="1.0",
            version_number=1,
            definition_json=statement_def,
            account_id=principal.account_id,
            published=True,
        )

        mb_result = await session.execute(
            select(ModelBackend).where(ModelBackend.organisation_id == principal.organisation_id).limit(1)
        )
        model_backend = mb_result.scalar_one_or_none()

        agent_id: uuid.UUID | None = None
        if model_backend is not None:
            agent = await create_agent(
                session,
                org_id=principal.organisation_id,
                name=_MSG_TRUTH_CLASSIFIER,
                account_id=principal.account_id,
                is_executable=True,
                input_schema_id=statement_schema.id,
                input_schema_version="1.0",
                output_schema_id=truth_schema.id,
                output_schema_version="1.0",
                prompt_template=(
                    "Classify the following statement as TRUE, FALSE, or UNDETERMINED. "
                    "Respond with a JSON object containing 'classification' and 'confidence'."
                ),
                model_backend_id=model_backend.id,
            )
            agent_id = agent.id

        pipeline = await create_pipeline(
            session,
            org_id=principal.organisation_id,
            name="Truth Classifier Pipeline",
            account_id=principal.account_id,
            description="Classifies statements using the Truth Classifier agent.",
        )

        if agent_id is not None:
            node_id = uuid.uuid4()
            nodes = [
                {
                    "id": str(node_id),
                    "node_type": "agent",
                    "position": {"x": 250, "y": 200},
                    "label": _MSG_TRUTH_CLASSIFIER,
                    "output_schema_id": str(truth_schema.id),
                    "agent_id": str(agent_id),
                    "connector_binding": None,
                    "role": None,
                    "autonomy_recommendation": None,
                    "composite_ref": None,
                    "composite_parameter_values": None,
                    "composite_input_mapping": None,
                    "composite_output_mapping": None,
                }
            ]
            await replace_pipeline_graph(
                session,
                pipeline_id=pipeline.id,
                org_id=principal.organisation_id,
                nodes=nodes,
                edges=[],
                is_privileged=True,
                caller_type="rest",
                account_id=principal.account_id,
            )

        progress = await _get_or_create_progress(session, principal.organisation_id)
        newly_completed = {"create_first_schema", "create_first_pipeline"}
        if agent_id is not None:
            newly_completed.add("create_first_agent")
        progress.completed_actions = list(set(progress.completed_actions) | newly_completed)

    return SeedExamplesResponse(
        agent_id=agent_id,
        schema_id=truth_schema.id,
        pipeline_id=pipeline.id,
    )


@router.post("/starter-pipeline", status_code=status.HTTP_201_CREATED)
@handle_db_errors("onboarding.create_starter_pipeline")
async def create_starter_pipeline(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("pipeline.create"),
    _schema_perm: TenantPrincipal = require_permission("schema.create"),
) -> StarterPipelineResponse:
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

        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        prev_id: uuid.UUID | None = None
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
            is_privileged=True,
            caller_type="rest",
            account_id=principal.account_id,
        )

    return StarterPipelineResponse(
        pipeline_id=pipeline.id,
        name=pipeline.name,
    )
