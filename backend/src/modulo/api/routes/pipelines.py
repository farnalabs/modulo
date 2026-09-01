"""Pipeline CRUD REST API.

Alpha: Graph replacement uses row-level locking (SELECT ... FOR UPDATE) in
replace_pipeline_graph. No advisory lock is deployed; the row lock on the
pipeline row serialises concurrent graph writes within a serialisable transaction.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, ValidationError, WithJsonSchema, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_THIS_FEATURE_NOT_AVAILABLE
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import (
    get_db_session,
    require_feature,
    require_permission,
    require_team_membership_or_admin,
)
from modulo.api.models.team_visibility import TeamVisibilityMixin
from modulo.api.team_scope import resolve_pipeline_team_scope, team_membership_exists
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.team_rbac import org_role_level
from modulo.core.audit_logger import append_audit_event
from modulo.core.capability_scope import (
    ScopeViolationError,
    agent_granted_connector_types,
    validate_allowed_connectors_subset,
)
from modulo.core.graph_validator import GraphValidator
from modulo.core.pipeline_engine.scatter_join import (
    FanOutConfig,
    JoinAggregateSpec,
    JoinCollectSpec,
)
from modulo.core.release_channels import VALID_RELEASE_CHANNELS
from modulo.core.reports.quality_report import (
    deliver_quality_report,
    generate_quality_report,
)
from modulo.core.run_context.autonomy import (
    autonomy_change_payload,
)
from modulo.core.team_visibility import (
    connector_team_mismatch_detail,
    extract_connector_bindings,
    find_connector_team_mismatches,
    find_model_backend_team_mismatches,
    model_backend_team_mismatch_detail,
)
from modulo.db.crud import guardrail_config as _guardrail_config
from modulo.db.crud.composite_template import create_composite_template
from modulo.db.crud.hitl_gate_guard import (
    GuardrailBindingStripDenied,
    HitlGateWeakeningDenied,
    denial_http_status,
)
from modulo.db.crud.pipeline import (
    PipelineHasActiveRunsError,
    archive_pipeline,
    check_pipeline_name_available,
    clone_pipeline,
    create_pipeline,
    get_pipeline,
    get_pipeline_graph,
    list_pipelines,
    replace_pipeline_graph,
    restore_pipeline,
    soft_delete_pipeline,
    unarchive_pipeline,
    update_pipeline,
)
from modulo.db.crud.pipeline_folder import move_pipeline_to_folder
from modulo.db.crud.pipeline_snapshot import create_snapshot_edit
from modulo.db.crud.pipeline_snapshot_versioning import (
    delete_snapshot,
    diff_snapshots,
    get_snapshot_detail,
    list_snapshots,
    rollback_to_snapshot,
    tag_snapshot,
)
from modulo.db.models.agent import Agent
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.notification_endpoint import NotificationEndpoint
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_edge import PipelineEdge
from modulo.db.models.schema import Schema
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.util import sanitise_log_value as _sanitise_log_value

_CODE_PIPELINE_LIST = "pipeline.list"
_CODE_ROUTES_PIPELINES = "routes.pipelines"
_MSG_PIPELINE_NOT_FOUND = "Pipeline not found"
_CODE_PIPELINE_GRAPH_UPDATE = "pipeline.graph.update"
_CODE_PIPELINE_UPDATE = "pipeline.update"
_MSG_SNAPSHOT_NOT_FOUND = "Snapshot not found"


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/pipelines", tags=["pipelines"])

# DoS guard: reject graphs larger than these limits before any DB work.
_MAX_GRAPH_NODES = 500
_MAX_GRAPH_EDGES = 1000

# ADR 017 service-layer backstop: operator+ is "privileged" (privilege is
# required to weaken/remove an existing HITL gate via a graph write).
_OPERATOR_LEVEL = org_role_level("operator")
_ADMIN_LEVEL = org_role_level("admin")


def _is_privileged(role: str | None) -> bool:
    """Resolve the is_privileged flag from an org role (operator+ -> True).

    Uses the flag-independent numeric hierarchy (team_rbac), NOT the
    kill-switched assert_org_role path, so the HITL guard stays live even
    when authz.enforce is disabled.
    """
    if role is None:
        return False
    return org_role_level(role) >= _OPERATOR_LEVEL


def _is_guardrail_admin(principal: TenantPrincipal) -> bool:
    """Resolve whether the caller may strip a guardrail binding from a node.

    Admin-level only (``org_role == "admin"``, the role the ``guardrail.manage``
    permission requires) — the same privilege the admin-only guardrail
    definition / apply / reject endpoints enforce. Uses the flag-independent
    numeric hierarchy so enforcement stays live even when authz.enforce is
    disabled (mirrors ``_is_privileged`` for the HITL guard).

    FAR-309 PR A review: this resolves the caller-supplied admin flag; the
    service-layer guard (``replace_pipeline_graph`` /
    ``rollback_to_snapshot``) re-reads the live role under the row lock for
    REST callers, so a stale role claim cannot slip a strip past the guard.
    """
    if principal.org_role is None:
        return False
    return org_role_level(principal.org_role) >= _ADMIN_LEVEL


async def _set_rls_context(session: AsyncSession, principal: TenantPrincipal) -> None:
    """Establish the RLS org + user context for a request transaction."""
    await set_rls_org(session, principal.organisation_id)
    await set_rls_user_context(session, principal.account_id, principal.org_role)


def _raise_db_migration_error() -> None:
    """Raise the 501 'feature not available' response for a ProgrammingError."""
    logger.exception(_CODE_ROUTES_PIPELINES)
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=MSG_THIS_FEATURE_NOT_AVAILABLE,
    ) from None


def _require_pipeline(pipeline: Pipeline | None) -> Pipeline:
    """Return the pipeline, or raise 404 when it does not exist."""
    if pipeline is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PIPELINE_NOT_FOUND)
    return pipeline


async def _get_pipeline_or_404(session: AsyncSession, pipeline_id: uuid.UUID) -> Pipeline:
    """Fetch a pipeline, raising 404 when it does not exist."""
    return _require_pipeline(await get_pipeline(session, pipeline_id))


@dataclass(frozen=True)
class GraphEdgeData:
    """Serialised edge payload shared by every graph-write call site.

    ``hitl_gate_config_present`` records whether the caller explicitly supplied a
    ``hitl_gate_config`` so the service layer can distinguish "gate removed" from
    "gate not mentioned".
    """

    id: uuid.UUID
    source_node_id: uuid.UUID
    target_node_id: uuid.UUID
    edge_type: str
    condition_expression: str | None
    hitl_gate_config: dict[str, Any] | None
    hitl_gate_config_present: bool
    source_port: str = "out"
    target_port: str = "in"


def _edge_to_data(edge: PipelineGraphEdge) -> GraphEdgeData:
    """Serialize a request edge into the persisted graph-write payload shape."""
    return GraphEdgeData(
        id=edge.id,
        source_node_id=edge.source_node_id,
        target_node_id=edge.target_node_id,
        edge_type=edge.edge_type,
        condition_expression=edge.condition_expression,
        hitl_gate_config=(edge.hitl_gate_config.model_dump(mode="json") if edge.hitl_gate_config is not None else None),
        hitl_gate_config_present="hitl_gate_config" in edge.model_fields_set,
        source_port=edge.source_port,
        target_port=edge.target_port,
    )


def _edge_data_to_dict(edge: GraphEdgeData) -> dict[str, Any]:
    return {
        "id": edge.id,
        "source_node_id": edge.source_node_id,
        "target_node_id": edge.target_node_id,
        "edge_type": edge.edge_type,
        "condition_expression": edge.condition_expression,
        "hitl_gate_config": edge.hitl_gate_config,
        "hitl_gate_config_present": edge.hitl_gate_config_present,
        "source_port": edge.source_port,
        "target_port": edge.target_port,
    }


def _edge_data_to_validator(edge: GraphEdgeData) -> dict[str, Any]:
    """Build the GraphValidator's reduced edge representation."""
    return {
        "source": str(edge.source_node_id),
        "target": str(edge.target_node_id),
        "type": edge.edge_type,
        "condition_expression": edge.condition_expression,
        "hitl_gate_config": edge.hitl_gate_config,
        "source_port": edge.source_port,
        "target_port": edge.target_port,
    }


def _reject_graph_validation_issues(issues: list[Any]) -> None:
    """Raise 422 for graph-save issues that must block authoring."""
    for issue in issues:
        if issue.code in ("GUARDRAIL_CAP_EXCEEDED", "REDACT_CORRECT_BLOCKED"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=issue.message,
            )


def _graph_validation_issue(severity: str, code: str, message: str, node_id: str | None = None) -> GraphValidationIssue:
    return GraphValidationIssue(
        severity=severity,
        code=code,
        message=message,
        node_id=node_id,
    )


async def _deny_hitl_gate(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    exc: HitlGateWeakeningDenied,
    request_id: str | None = None,
) -> None:
    """Append the denial audit event and translate to HTTP (hitl-gate-removal-guard-plan.md v19 §5).

    The guarded write already rolled back (guard-runs-before-delete), so the
    denial audit event is written in a fresh transaction immediately after the
    denial — it must never be lost with the rolled-back write.
    """
    try:
        async with session.begin():
            await set_rls_org(session, org_id)
            payload = exc.payload_json or {
                "caller_type": "rest",
                "reason_code": exc.reason_code,
                "denied": True,
                "affected_edges": [
                    {
                        "source_node_id": k[0],
                        "target_node_id": k[1],
                        "edge_type": k[2],
                    }
                    for k in exc.correlation_keys
                ],
                "weakening_types": exc.weakening_types,
            }
            await append_audit_event(
                session,
                org_id=org_id,
                event_type="hitl_gate_removal_denied",
                actor_user_id=account_id,
                resource_type="pipeline",
                resource_id=pipeline_id,
                payload_json=payload,
                request_id=request_id,
            )
    except Exception:
        logger.exception("routes.pipelines.hitl_denial_audit_failed")
    detail = f"Gate weakening denied ({exc.reason_code})."
    if exc.detail:
        detail += f" Affected edges: {exc.detail}"
    raise HTTPException(
        status_code=denial_http_status(exc.reason_code),
        detail=detail,
    ) from None


async def _handle_graph_write_denials(
    session: AsyncSession,
    *,
    principal: TenantPrincipal,
    pipeline_id: uuid.UUID,
    exc: HitlGateWeakeningDenied | GuardrailBindingStripDenied,
) -> None:
    """Translate a graph-write denial into its HTTP response.

    ``HitlGateWeakeningDenied`` is audited then re-raised as HTTP by
    ``_deny_hitl_gate``; ``GuardrailBindingStripDenied`` maps directly to its
    denial status. Shared by the graph-replace, pipeline-update, snapshot-
    rollback, and node-conversion save paths.
    """
    if isinstance(exc, HitlGateWeakeningDenied):
        await _deny_hitl_gate(
            session,
            org_id=principal.organisation_id,
            account_id=principal.account_id,
            pipeline_id=pipeline_id,
            exc=exc,
            request_id=getattr(principal, "request_id", None),
        )
        return
    raise HTTPException(
        status_code=denial_http_status(exc.reason_code),
        detail=exc.detail,
    ) from None


_RETRY_POLICY_EVENTS = frozenset({"stall", "timeout", "failure", "eval_failed"})
_RETRY_POLICY_MAX_RETRIES = 5


def _validate_retry_policy(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate a ``retry_policy`` payload, returning it unchanged.

    ``None`` is accepted (treated as "no retry policy"). Raises ValueError with
    a clear message when the policy shape is malformed.
    """
    if value is None:
        return value
    if not isinstance(value, dict):
        raise ValueError(
            "retry_policy must be an object like "
            "{'on': ['stall','timeout','failure','eval_failed'], 'max_retries': 0-5}"
        )
    on = value.get("on", [])
    if not isinstance(on, list) or any(not isinstance(e, str) for e in on):
        raise ValueError("retry_policy 'on' must be a list of strings from ['stall','timeout','failure','eval_failed']")
    unknown = set(on) - _RETRY_POLICY_EVENTS
    if unknown:
        raise ValueError(
            f"retry_policy 'on' contains unknown values {sorted(unknown)}; "
            "allowed values are ['stall','timeout','failure','eval_failed']"
        )
    max_retries = value.get("max_retries", 0)
    if isinstance(max_retries, bool) or not isinstance(max_retries, int):
        raise ValueError("retry_policy 'max_retries' must be an integer between 0 and 5")
    if not 0 <= max_retries <= _RETRY_POLICY_MAX_RETRIES:
        raise ValueError("retry_policy 'max_retries' must be an integer between 0 and 5")
    return value


class PipelineCreate(TeamVisibilityMixin):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    visibility: str = Field(default="org", pattern=r"^(org|team)$")
    owner_team_id: uuid.UUID | None = None
    max_concurrent_runs: int = Field(default=5, ge=1)
    lock_wait_timeout_seconds: int = Field(default=300, ge=30, le=3600)
    node_timeout_seconds: int = Field(default=300, ge=1)
    run_context_defaults: dict[str, Any] = Field(default_factory=dict)
    default_autonomy_level: str = "manual_approval"
    max_duration_seconds: int = Field(3600, ge=1)
    stale_run_timeout_minutes: int = Field(
        30,
        ge=1,
        description="Max minutes a run can stay in pending/running without progress before being killed.",
    )
    folder_id: uuid.UUID | None = None
    rate_limit_config: dict[str, Any] | None = Field(
        None,
        description=(
            "Rate limit: {max_triggers: int, window_seconds: int, key_fields: [str], match_mode: 'exact'|'presence'}"
        ),
    )
    retry_policy: dict[str, Any] | None = Field(
        None,
        description=(
            "Retry policy: {on: [stall|timeout|failure|eval_failed], max_retries: 0-5}. "
            "When a run ends in a configured state and retries remain, the run is "
            "re-dispatched automatically instead of terminal-failing."
        ),
    )

    @field_validator("retry_policy")
    @classmethod
    def _validate_retry_policy_field(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _validate_retry_policy(value)


class PipelineUpdate(TeamVisibilityMixin):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    visibility: str | None = Field(None, pattern=r"^(org|team)$")
    owner_team_id: uuid.UUID | None = None
    max_concurrent_runs: int | None = Field(None, ge=1)
    lock_wait_timeout_seconds: int | None = Field(None, ge=30, le=3600)
    node_timeout_seconds: int | None = Field(None, ge=1)
    run_context_defaults: dict[str, Any] | None = None
    default_autonomy_level: str | None = None
    max_duration_seconds: int | None = Field(None, ge=1)
    stale_run_timeout_minutes: Annotated[
        int | None,
        Field(
            None,
            ge=1,
            description="Override the stale-run timeout for this pipeline.",
        ),
        WithJsonSchema({"type": "integer", "minimum": 1}),
    ] = None

    @field_validator("stale_run_timeout_minutes", mode="before")
    @classmethod
    def reject_null_stale_timeout(cls, v: int | None) -> int | None:
        if v is None:
            raise ValueError("stale_run_timeout_minutes cannot be set to null. Use a value >= 1.")
        return v

    rate_limit_config: dict[str, Any] | None = Field(
        None,
        description="Rate limit config. Set to {} to clear.",
    )
    retry_policy: dict[str, Any] | None = Field(
        None,
        description="Retry policy: {on: [stall|timeout|failure|eval_failed], max_retries: 0-5}. Set to {} to clear.",
    )
    graph_json: PipelineGraphUpdate | None = Field(
        None,
        description="Replace the pipeline graph (nodes + edges). Creates a new snapshot.",
    )

    @field_validator("retry_policy", mode="before")
    @classmethod
    def _validate_retry_policy_field(cls, value: dict[str, Any] | None) -> dict[str, Any]:
        # None clears the policy (empty dict) — the column is non-nullable.
        return _validate_retry_policy(value) or {}

    @field_validator("max_duration_seconds", mode="before")
    @classmethod
    def reject_null_max_duration(cls, v: int | None) -> int | None:
        if v is None:
            raise ValueError("max_duration_seconds cannot be set to null. Use a value >= 1.")
        return v

    @field_validator("node_timeout_seconds", mode="before")
    @classmethod
    def reject_null_node_timeout(cls, v: int | None) -> int | None:
        if v is None:
            raise ValueError("node_timeout_seconds cannot be set to null. Use a value >= 1.")
        return v

    @field_validator("lock_wait_timeout_seconds", mode="before")
    @classmethod
    def reject_null_lock_wait_timeout(cls, v: int | None) -> int | None:
        if v is None:
            raise ValueError("lock_wait_timeout_seconds cannot be set to null. Use a value >= 30.")
        return v


class PipelineResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    description: str | None
    visibility: str
    max_concurrent_runs: int
    lock_wait_timeout_seconds: int
    node_timeout_seconds: int
    run_context_defaults: dict[str, Any]
    default_autonomy_level: str | None = None
    # Intentionally nullable: legacy rows and rollbacks of pre-migration data
    # can still expose a NULL value until the max_duration pipeline migration
    # has run on all production DBs.
    max_duration_seconds: int | None = None
    stale_run_timeout_minutes: int = 30
    rate_limit_config: dict[str, Any] | None = None
    retry_policy: dict[str, Any] = Field(default_factory=dict, json_schema_extra={"default": {}})
    snapshot_count: int = 0
    archived_at: datetime | None = None
    owner_team_id: uuid.UUID | None = None
    folder_id: uuid.UUID | None = None
    # Set on PATCH /pipelines/{id} responses when owner_team_id changed: the
    # UI warns the user to re-save the graph so connectors/model backends are
    # rebound for the new team (PRD §9.3 ownership transfer).
    connector_rebind_required: bool = False
    created_by: uuid.UUID = Field(validation_alias="account_id")
    created_at: datetime
    updated_at: datetime

    @field_validator("retry_policy", mode="before")
    @classmethod
    def _coerce_retry_policy(cls, value: Any) -> dict[str, Any]:
        # The column is non-nullable with a {} default, but legacy rows and
        # partial ORM objects may expose None — the no-policy default is {}.
        return value if isinstance(value, dict) else {}

    model_config = {"from_attributes": True, "populate_by_name": True}


class PipelineListResponse(BaseModel):
    items: list[PipelineResponse]
    total: int
    page: int
    page_size: int
    next_cursor: str | None = None
    has_more: bool = False


class GraphPosition(BaseModel):
    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)


class ConnectorBinding(BaseModel):
    type: str = Field(min_length=1, max_length=100)
    instance_id: uuid.UUID


class SchemaPin(BaseModel):
    """A pinned schema version reference for a pipeline node."""

    schema_id: uuid.UUID
    schema_version: str

    @field_validator("schema_version")
    @classmethod
    def version_must_be_concrete(cls, v: str) -> str:
        if v in ("latest", "*", "") or len(v) > 50:
            raise ValueError(f"schema_version must be a concrete version, got '{v}'")
        return v


_RESERVED_ENV_PREFIXES = ("MODULO_", "OPENCODE_API_KEY")


class CapabilityScope(BaseModel):
    """Node-level least-privilege contract (FAR-402 P4 / FAR-418).

    A node may NARROW (never widen) what its referenced Agent is granted:

    * ``allowed_connectors``: connector instance-ids and/or connector types the
      node may resolve from the ConnectorHub. Each connector-TYPE entry must be
      within the Agent's ``connector_type_refs`` (compile-time check); the
      ConnectorHub is fetched with ONLY these connectors (deny-by-default).
    * ``allowed_tools``: MCP/runtime tools the node's agent may invoke — an
      additional narrowing filter wired through ``check_tool_scope``.
    * ``context_scope``: allowlist of ``run_context`` keys the node may read
      (need-to-know boundary).

    Default is UNRESTRICTED: an absent ``capability_scope`` leaves behaviour
    unchanged (the node may use all of its Agent's grants).
    """

    allowed_connectors: list[str] | None = Field(
        default=None,
        description="Connector instance-ids and/or connector types the node may "
        "resolve. Absent/empty = UNRESTRICTED (Agent grants).",
    )
    allowed_tools: list[str] | None = Field(
        default=None,
        description="MCP/runtime tools the node's agent may invoke. Absent = NOT "
        "narrowed (additional role check still applies).",
    )
    context_scope: list[str] | None = Field(
        default=None,
        description="Allowlist of run_context keys the node may read. Absent = UNRESTRICTED (full run_context).",
    )


class PipelineGraphNode(BaseModel):
    id: uuid.UUID
    node_type: Literal["agent", "manual", "composite", "sandbox_agent", "router", "hitl", "join"] = "agent"
    agent_id: uuid.UUID | None = None
    position: GraphPosition
    connector_binding: ConnectorBinding | None = None
    output_schema_id: uuid.UUID | None = None
    input_schema_pin: SchemaPin | None = None
    output_schema_pin: SchemaPin | None = None
    # FAR-418: node-level capability_scope (least-privilege). A node narrows (never
    # widens) its Agent's grants: allowed_connectors / allowed_tools / context_scope.
    # Absent = UNRESTRICTED (behaviour unchanged). Validated in _resolve_graph_references.
    capability_scope: CapabilityScope | None = None
    label: str | None = Field(default=None, max_length=255)
    role: str | None = None
    autonomy_recommendation: str | None = None
    # FAR-295: is this node logically safe to re-run? Applies to EVERY executor
    # type (agent, manual, composite, sandbox_agent). Defaults to true. A node
    # marked idempotent=false (e.g. one with an external side effect like
    # creating a PR or charging a card) suppresses BOTH the run-level
    # retry_policy re-dispatch and the node-level transient retry for any graph
    # that contains it — re-running would double-execute the side effect.
    idempotent: bool = Field(
        default=True,
        description="Whether the node is logically safe to re-run. When false, "
        "retries of any run containing this node are suppressed.",
    )
    composite_ref: uuid.UUID | None = None
    composite_parameter_values: dict[str, Any] | None = None
    composite_input_mapping: dict[str, Any] | None = None
    composite_output_mapping: dict[str, Any] | None = None
    parameter_set_id: uuid.UUID | None = None
    parameter_overrides: dict[str, Any] | None = None
    template_id: str | None = None
    # FAR-296: sandbox_agent mode — "llm" (default, dispatches an LLM agent with
    # agent_command + rendered prompt) or "script" (runs script_command verbatim
    # with the full run input at /home/user/input.json).
    mode: Literal["llm", "script"] = "llm"
    agent_command: str | None = None
    agent_prompt: str | None = None
    script_command: str | None = None
    # FAR-296 Phase 3: egress control + resource-limit config surface.
    # egress_policy: "default" (internet allowed, e2b default), "deny_all"
    # (allow_internet_access=False), or "selected" (allow_internet_access=False
    # + a host:port egress_allowlist carried as metadata — FAR-296 Phase 3b-3).
    # resource_limits: a known-subset dict carried as sandbox metadata so a
    # server-side template/config can enforce them.
    egress_policy: Literal["default", "deny_all", "selected"] | None = None
    egress_allowlist: list[dict[str, Any]] | None = None
    resource_limits: dict[str, Any] | None = None
    # FAR-212 PR B: sandbox write/egress mediation surface. ``read_only`` mounts
    # / chmods the workspace read-only at runtime (so writes are impossible for
    # the agent's non-root user — write_files derives False) and
    # ``git_credentials`` scopes the provisioned git credential (``scoped`` =
    # limited to the allowlisted github.com host via an enforced helper;
    # ``unscoped`` = full access, the default; ``none`` = no git credentials are
    # provisioned). Both are validated (``_validate_sandbox_read_only_config`` /
    # ``_validate_sandbox_git_credentials_config``) and ENFORCED (node_runner
    # applies the sandbox policy step), so the capability derivation can certify
    # them mechanically. Only sandbox_agent nodes may set them.
    read_only: bool = False
    git_credentials: Literal["scoped", "unscoped", "none"] | None = None
    # FAR-296 Phase 4a: wall-clock spend budget (seconds). When set, the
    # node's sandbox is killed by the platform-side runtime killer once the
    # wall-clock elapsed time exceeds this budget — a tighter spend bound than
    # the node timeout. Must be a positive int (validated at save-time).
    wallclock_budget_seconds: int | None = None
    # FAR-228: opt-in idempotency gate for side-effecting sandbox nodes. When
    # non-empty, a FULL-LINE occurrence of this literal in the sandbox output
    # marks the run's delivery as done (raw-output marker ``delivery_done``),
    # and transient retries of that node are suppressed by the idempotency gate.
    delivery_sentinel: str | None = None
    env_vars: dict[str, str] | None = None
    context_files: dict[str, str] | None = None
    timeout_seconds: int | None = Field(
        default=None,
        ge=60,
        le=604800,
        description="Per-node timeout override (60-604800s). Overrides pipeline node_timeout_seconds.",
    )
    output_schema_json: dict[str, Any] | None = Field(
        default=None,
        description="Inline JSON Schema defining the node's output shape.",
    )
    description: str | None = Field(default=None, max_length=2000)
    # FAR-306: opt-in stall detectors for sandbox_agent nodes. The heartbeat
    # (connection liveness) is enabled by default; the log-growth / stdout-delta
    # / filesystem detectors are OFF unless configured.
    stall_timeout_seconds: int | None = Field(
        default=None,
        ge=60,
        le=604800,
        description="Stall window (seconds) before the idle watchdog treats the agent as stalled.",
    )
    enable_heartbeat: bool = Field(
        default=True,
        description="Enable the connection-liveness (heartbeat) stall channel.",
    )
    watch_log_path: str | None = Field(
        default=None,
        description="Log-growth detector: a path inside the sandbox whose growth counts as activity.",
    )
    stdout_percentage_delta: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="stdout-delta detector: fraction of new stdout that must differ to count as activity.",
    )
    watch_globs: list[str] = Field(
        default_factory=list,
        description="Filesystem detector: globs of sandbox paths whose change counts as activity.",
    )
    # FAR-402 P1 (F2-A Router / F2-D HITL): per-node configuration carried on
    # the node and compiled by the pipeline engine.
    router_config: dict[str, Any] | None = Field(
        default=None,
        description="Router node config: {mode, rules:[{guard (JMESPath), target|target_port}|{default, target}]}. "
        "Required for node_type='router'.",
    )
    hitl_config: dict[str, Any] | None = Field(
        default=None,
        description="HITL node config (mode, form_schema_ref, reject_target, claim_team_id, claim_expiry_min, "
        "human_only, eval_before_interrupt, required_team_id, overdue_threshold_minutes, eval_condition, "
        "condition). Compiles to the existing synthetic-gate path. Required for node_type='hitl'.",
    )
    # FAR-402 P3 / FAR-417: scatter (fan-out). A property on an agent /
    # sandbox_agent / composite node (NOT a new node_type). When set, the node
    # splits its `split` source port into N parallel branches. The compile step
    # expands this into N distinct child node identities with unique ids.
    fan_out: FanOutConfig | None = None
    # FAR-402 P3 / FAR-417: join (fan-in). Only valid on a `join` node_type.
    # `collect` lists the upstream branches to gather; `aggregate` declares how
    # to fold them.
    collect: list[JoinCollectSpec] | None = None
    aggregate: JoinAggregateSpec | None = None
    # Policy when some collected branches failed (non-empty, non-timeout).
    # "collect_and_proceed" (default) marks failed branches in the output;
    # "fail" raises. Deadline (seconds) for waiting on slow/partial branches.
    join_partial_policy: Literal["collect_and_proceed", "fail"] = "collect_and_proceed"
    # FAR-416 (FAR-402 F1): ports are ADDITIVE metadata over the flat
    # run_context/artifact dict. A port name maps 1:1 to the flat-state key the
    # node already uses (identity mapping). When absent, the lazy backfill
    # synthesizes a single {port:"out"} output and {port:"in"} input. Declaring
    # ports enables compile-time fan-in safety + typed port validation.
    inputs: list[dict[str, Any]] | None = Field(
        default=None,
        description="Input ports. Each entry: {port: str, schema_ref?: str}. "
        "None => backfilled with a single default 'in' port at compile time.",
    )
    outputs: list[dict[str, Any]] | None = Field(
        default=None,
        description="Output ports. Each entry: {port: str, schema_ref?: str}. "
        "None => backfilled with a single default 'out' port at compile time.",
    )

    @model_validator(mode="after")
    def validate_node_type(self) -> PipelineGraphNode:
        node_validators = {
            "manual": self._validate_manual_node,
            "composite": self._validate_composite_node,
            "sandbox_agent": self._validate_sandbox_agent_node,
            "agent": self._validate_agent_node,
            "router": self._validate_router_node,
            "hitl": self._validate_hitl_node,
            "join": self._validate_join_node,
        }
        node_validators[self.node_type]()
        # FAR-402 P3 / FAR-417: fan_out / collect / aggregate cross-checks.
        if self.fan_out is not None and self.node_type == "join":
            raise ValueError("A join node cannot also declare fan_out")
        if self.fan_out is not None and self.node_type not in (
            "agent",
            "sandbox_agent",
        ):
            raise ValueError("fan_out is only allowed on agent / sandbox_agent nodes")
        # FAR-212 PR B: read_only / git_credentials are sandbox_agent-only fields.
        # A non-sandbox node that sets them is rejected — the enforcement surface
        # (read-only workspace, git-credential scope) only exists for sandbox
        # agents, and a declared-but-unenforced field on another node type would
        # be a silent no-op.
        if self.node_type != "sandbox_agent":
            if self.read_only:
                raise ValueError("Only sandbox_agent nodes can set read_only=True")
            if self.git_credentials is not None:
                raise ValueError("Only sandbox_agent nodes can set git_credentials")
        if self.node_type != "agent" and self.parameter_set_id is not None:
            raise ValueError("Only agent nodes can have parameter_set_id")
        if (
            self.output_schema_pin is not None
            and self.output_schema_id is not None
            and self.output_schema_pin.schema_id != self.output_schema_id
        ):
            raise ValueError(
                f"output_schema_pin.schema_id ({self.output_schema_pin.schema_id}) "
                f"does not match output_schema_id ({self.output_schema_id})"
            )
        return self

    def _validate_manual_node(self) -> None:
        if self.agent_id is not None:
            raise ValueError("Manual nodes cannot reference an agent")
        if self.connector_binding is not None:
            raise ValueError("Manual nodes cannot have connector bindings")
        has_output = self.output_schema_pin is not None or self.output_schema_id is not None
        if not has_output:
            raise ValueError("Manual nodes require an output schema")
        if self.label is None:
            raise ValueError("Manual nodes require a label")

    def _validate_router_node(self) -> None:
        # The heavy validation (default-rule enforcement) happens at compile
        # time in the pipeline engine; here we just require a router_config.
        if self.router_config is None:
            raise ValueError("Router nodes require a router_config")
        rules = (self.router_config or {}).get("rules") or []
        if not rules:
            raise ValueError("Router nodes require at least one rule")
        for rule in rules:
            if rule.get("default"):
                continue
            if not (rule.get("target") or rule.get("target_port")):
                raise ValueError("Each non-default Router rule requires a target/target_port")

    def _validate_hitl_node(self) -> None:
        # HITL nodes produce output like a normal (manual/agent) node and gate
        # their outgoing edges. Require a hitl_config describing the gate.
        if self.hitl_config is None:
            raise ValueError("HITL nodes require a hitl_config")

    def _validate_composite_node(self) -> None:
        if self.composite_ref is None:
            raise ValueError("Composite nodes require a composite_ref")
        if self.agent_id is not None:
            raise ValueError("Composite nodes cannot reference an agent")
        if self.connector_binding is not None:
            raise ValueError("Composite nodes cannot have connector bindings")

    def _validate_sandbox_agent_node(self) -> None:
        # FAR-296 mode-aware validation — ONE shared helper used by every
        # sandbox_agent gate (Pydantic model, node runner, GraphValidator,
        # MCP update_pipeline_graph, config linter) so save-time and run-time
        # agreement is guaranteed. Imported from the lightweight sandbox_mode
        # module (no LangGraph) to keep the API layer import-linter-clean.
        from modulo.core.pipeline_engine.sandbox_mode import (
            _validate_sandbox_egress_allowlist_config,
            _validate_sandbox_egress_config,
            _validate_sandbox_git_credentials_config,
            _validate_sandbox_mode_config,
            _validate_sandbox_read_only_config,
            _validate_sandbox_resource_limits_config,
            _validate_sandbox_wallclock_budget_config,
        )

        _validate_sandbox_mode_config(self.model_dump())
        _validate_sandbox_egress_config(self.model_dump())
        _validate_sandbox_egress_allowlist_config(
            self.egress_policy,
            self.egress_allowlist,
            str(self.id),
        )
        _validate_sandbox_resource_limits_config(self.model_dump())
        # FAR-212 PR B: read_only / git_credentials are sandbox-only fields.
        # Validated here (and by the graph validator / MCP / node runner via
        # the shared helpers) so a non-boolean read_only or an unrecognised
        # git_credentials scope can never reach the capability derivation
        # (which fails CLOSED on an unvalidated key).
        _validate_sandbox_read_only_config(self.model_dump())
        _validate_sandbox_git_credentials_config(self.model_dump())
        _validate_sandbox_wallclock_budget_config(
            self.wallclock_budget_seconds,
            self.timeout_seconds,
            str(self.id),
        )
        if not self.template_id:
            raise ValueError("Sandbox agent nodes require a template_id (e.g. 'opencode')")
        self._validate_sandbox_env_vars()
        self._validate_sandbox_context_files()

    def _validate_agent_node(self) -> None:
        if self.agent_id is None:
            raise ValueError("Agent nodes require an agent")

    def _validate_join_node(self) -> None:
        # A join node is a pure convergence node — it has no agent, no connector
        # binding, and no manual output schema of its own.
        if self.agent_id is not None:
            raise ValueError("Join nodes cannot reference an agent")
        if self.connector_binding is not None:
            raise ValueError("Join nodes cannot have connector bindings")
        if not self.collect:
            raise ValueError("Join nodes require a non-empty 'collect' list")
        if self.aggregate is None:
            raise ValueError("Join nodes require an 'aggregate'")
        if self.aggregate.kind == "merge_by_key" and not self.aggregate.key:
            raise ValueError("Join aggregate merge_by_key requires an explicit 'key'")
        if self.aggregate.kind == "map" and not self.aggregate.map_expression:
            raise ValueError("Join aggregate map requires a 'map_expression'")

    def _validate_sandbox_env_vars(self) -> None:
        if not self.env_vars:
            return
        for key in self.env_vars:
            for prefix in _RESERVED_ENV_PREFIXES:
                if key.startswith(prefix):
                    raise ValueError(
                        f"Sandbox agent env var '{key}' uses reserved prefix '{prefix}'. "
                        "System-reserved env vars are set automatically."
                    )

    def _validate_sandbox_context_files(self) -> None:
        if not self.context_files:
            return
        for source_path in self.context_files:
            if not source_path.startswith("/"):
                raise ValueError(
                    f"Sandbox agent context_files source '{source_path}' must be an absolute path (starting with /)"
                )


class EvalCondition(BaseModel):
    eval_name: str = Field(
        min_length=1,
        max_length=255,
        description="Name of the eval definition to reference.",
    )
    threshold: float = Field(
        ge=0.0,
        le=1.0,
        description="Score threshold for the condition.",
    )
    operator: str = Field(
        pattern="^(lt|gt|lte|gte|eq|neq)$",
        description="Comparison operator: lt (score < threshold), gt (score > threshold), "
        "lte (score <= threshold), gte (score >= threshold), eq (score == threshold), "
        "neq (score != threshold).",
    )


class HitlGateConfig(BaseModel):
    label: str = Field(min_length=1, max_length=255)
    description: str = Field(max_length=2000)
    reject_target: uuid.UUID | None = None
    correction_target: uuid.UUID | None = Field(
        default=None,
        description="Node ID routed to on HITL rejection for the FAR-210 single-node "
        "correction path. Accepted and persisted through the graph contract; the "
        "reject→correction dispatch seam is tracked as a follow-up (the graph "
        "compiler currently kicks a rejection back to reject_target).",
    )
    claim_expiry_minutes: int = Field(gt=0, le=1440)
    human_only: bool
    required_team_id: uuid.UUID | None = None
    condition: str | None = Field(
        default=None,
        max_length=500,
        description="JMESPath expression evaluated against the upstream node output. "
        "If it returns true, gate activates. If false/empty/null, gate is skipped.",
    )
    eval_condition: EvalCondition | None = Field(
        default=None,
        description="Eval-reference condition: references an eval definition by name "
        "with threshold and operator. Evaluated after eval-before-interrupt runs. "
        "If the condition evaluates to true (e.g., score < threshold with operator lt), "
        "the gate fires. If false, execution continues without interrupting.",
    )


class PipelineGraphEdge(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    source_node_id: uuid.UUID
    target_node_id: uuid.UUID
    edge_type: str = Field(pattern="^(normal|reject|conditional|loop)$")
    hitl_gate_config: HitlGateConfig | None = None
    condition_expression: str | None = Field(
        default=None,
        max_length=500,
        description="JMESPath expression for conditional edge routing. "
        "Evaluated against pipeline state; if truthy, routes to target.",
    )
    # FAR-416 (FAR-402 F1): port addressing. Defaults mirror the flat-state keys
    # used before ports existed, so legacy edges route identically.
    source_port: str = Field(
        default="out",
        description="Output port on the source node this edge originates from.",
    )
    target_port: str = Field(
        default="in",
        description="Input port on the target node this edge delivers into.",
    )

    model_config = {"from_attributes": True}


class GraphValidationIssue(BaseModel):
    severity: str
    code: str
    message: str
    node_id: str | None = None


class PipelineGraphUpdate(BaseModel):
    nodes: list[PipelineGraphNode]
    edges: list[PipelineGraphEdge]

    @model_validator(mode="after")
    def reject_database_conflicts(self) -> PipelineGraphUpdate:
        if len(self.nodes) > _MAX_GRAPH_NODES:
            raise ValueError(f"Graph exceeds maximum of {_MAX_GRAPH_NODES} nodes")
        if len(self.edges) > _MAX_GRAPH_EDGES:
            raise ValueError(f"Graph exceeds maximum of {_MAX_GRAPH_EDGES} edges")
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("Graph node IDs must be unique")
        edge_ids = [edge.id for edge in self.edges]
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("Graph edge IDs must be unique")
        paths = [(edge.source_node_id, edge.target_node_id, edge.edge_type) for edge in self.edges]
        if len(paths) != len(set(paths)):
            raise ValueError("Graph edge paths must be unique")
        return self


class PipelineGraphResponse(PipelineGraphUpdate):
    validation_issues: list[GraphValidationIssue] = Field(default_factory=list)


def _graph_response(
    nodes: list[dict[str, Any]],
    edges: list[Any],
    *,
    validation_issues: list[GraphValidationIssue] | None = None,
) -> PipelineGraphResponse:
    try:
        return PipelineGraphResponse(
            nodes=[PipelineGraphNode.model_validate(node) for node in nodes],
            edges=[PipelineGraphEdge.model_validate(edge) for edge in edges],
            validation_issues=validation_issues or [],
        )
    except ValidationError as e:
        logger.exception("Pipeline graph data validation failed: %s", e.errors())
        detail = "Pipeline graph contains invalid data. This may be caused by a schema migration."
        detail += f" Validation errors: {e.errors()}"
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=detail,
        ) from e


async def _enforce_connector_team_bindings(
    session: AsyncSession,
    org_id: uuid.UUID,
    pipeline_owner_team_id: uuid.UUID | None,
    connector_bindings: list[dict[str, Any]],
) -> None:
    """Block graph saves that bind a team-private connector to a different team's pipeline.

    PRD §9.3: a connector with ``visibility: team`` is only usable within pipelines
    owned by the same team. Violations raise 409 ``connector_team_mismatch`` at the
    pipeline-save command layer.
    """
    mismatches = await find_connector_team_mismatches(
        session,
        org_id=org_id,
        pipeline_owner_team_id=pipeline_owner_team_id,
        connector_bindings=connector_bindings,
    )
    if mismatches:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=connector_team_mismatch_detail(mismatches),
        )


async def _enforce_model_backend_team_bindings(
    session: AsyncSession,
    org_id: uuid.UUID,
    pipeline_owner_team_id: uuid.UUID | None,
    model_backend_pins: list[dict[str, Any]],
) -> None:
    """Block graph saves that pin a team-private model backend from another team.

    PRD §9.3: a model backend with ``visibility: team`` is only usable within
    pipelines owned by the same team, mirroring the connector rule. Violations
    raise 409 ``model_backend_team_mismatch`` at the pipeline-save command layer.
    """
    mismatches = await find_model_backend_team_mismatches(
        session,
        org_id=org_id,
        pipeline_owner_team_id=pipeline_owner_team_id,
        model_backend_pins=model_backend_pins,
    )
    if mismatches:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=model_backend_team_mismatch_detail(mismatches),
        )


async def _load_agents_by_ids(
    session: AsyncSession,
    org_id: uuid.UUID,
    agent_ids: set[uuid.UUID],
) -> dict[uuid.UUID, Agent]:
    """Load tenant-owned agents by ID, raising 422 for unknown IDs."""
    agents = (
        list(
            (
                await session.execute(
                    select(Agent).where(
                        Agent.organisation_id == org_id,
                        Agent.id.in_(agent_ids),
                    )
                )
            ).scalars()
        )
        if agent_ids
        else []
    )
    agents_by_id = {agent.id: agent for agent in agents}
    missing_agent_ids = sorted(agent_ids - agents_by_id.keys(), key=str)
    if missing_agent_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown agent IDs for this organisation: {missing_agent_ids}",
        )
    return agents_by_id


def _collect_schema_ids(nodes: list[PipelineGraphNode]) -> set[uuid.UUID]:
    """Collect the schema IDs referenced by a graph's nodes."""
    schema_ids_to_check: set[uuid.UUID] = set()
    for node in nodes:
        if node.node_type == "manual":
            if node.output_schema_id is not None:
                schema_ids_to_check.add(node.output_schema_id)
            if node.output_schema_pin is not None:
                schema_ids_to_check.add(node.output_schema_pin.schema_id)
        if node.input_schema_pin is not None:
            schema_ids_to_check.add(node.input_schema_pin.schema_id)
        if node.output_schema_pin is not None:
            schema_ids_to_check.add(node.output_schema_pin.schema_id)
    return schema_ids_to_check


async def _load_existing_schema_ids(
    session: AsyncSession,
    org_id: uuid.UUID,
    schema_ids_to_check: set[uuid.UUID],
) -> set[uuid.UUID]:
    """Load the tenant-owned schema IDs, raising 422 for unknown IDs."""
    existing_schema_ids = (
        set(
            (
                await session.execute(
                    select(Schema.id).where(
                        Schema.organisation_id == org_id,
                        Schema.id.in_(schema_ids_to_check),
                    )
                )
            ).scalars()
        )
        if schema_ids_to_check
        else set()
    )
    missing_schema_ids = sorted(schema_ids_to_check - existing_schema_ids, key=str)
    if missing_schema_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown schema IDs for this organisation: {missing_schema_ids}",
        )
    return existing_schema_ids


def _build_schema_and_backend_pins(
    nodes: list[PipelineGraphNode],
    agents_by_id: dict[uuid.UUID, Agent],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build schema + model-backend pins for a graph's nodes."""
    schema_pins: list[dict[str, Any]] = []
    model_backend_pins: list[dict[str, Any]] = []
    for node in nodes:
        if node.agent_id is not None:
            agent = agents_by_id[node.agent_id]
            schema_pins.extend(
                [
                    {
                        "node_id": str(node.id),
                        "direction": "input",
                        "schema_id": str(agent.input_schema_id),
                    },
                    {
                        "node_id": str(node.id),
                        "direction": "output",
                        "schema_id": str(agent.output_schema_id),
                    },
                ]
            )
            model_backend_pins.append(
                {
                    "node_id": str(node.id),
                    "model_backend_id": str(agent.model_backend_id),
                }
            )
        else:
            if node.input_schema_pin is not None:
                schema_pins.append(
                    {
                        "node_id": str(node.id),
                        "direction": "input",
                        "schema_id": str(node.input_schema_pin.schema_id),
                        "schema_version": node.input_schema_pin.schema_version,
                    }
                )
            if node.output_schema_pin is not None:
                schema_pins.append(
                    {
                        "node_id": str(node.id),
                        "direction": "output",
                        "schema_id": str(node.output_schema_pin.schema_id),
                        "schema_version": node.output_schema_pin.schema_version,
                    }
                )
            elif node.output_schema_id is not None:
                schema_pins.append(
                    {
                        "node_id": str(node.id),
                        "direction": "output",
                        "schema_id": str(node.output_schema_id),
                    }
                )
    return schema_pins, model_backend_pins


def _validate_capability_scopes(
    nodes: list[PipelineGraphNode],
    agents_by_id: dict[uuid.UUID, Agent],
) -> None:
    """Compile-time narrow-not-widen check for node capability_scope (FAR-418).

    A node may NARROW its referenced Agent's connector grants, never WIDEN them.
    For every node that declares ``capability_scope.allowed_connectors``, each
    connector-TYPE entry must be within the Agent's ``connector_type_refs``; a
    widen attempt raises a typed ``ScopeViolationError`` (422 at the route). A
    node with no scope (UNRESTRICTED default) or no Agent reference is skipped.
    Connector instance-id entries are opaque here and enforced at run time by the
    ConnectorHub deny-by-default fetch scope.
    """
    for node in nodes:
        scope = node.capability_scope
        if scope is None or not scope.allowed_connectors:
            continue
        if node.agent_id is None:
            # A connector node may not reference an Agent; its allowed_connectors
            # are instance/type constrained only, so nothing is checked here.
            continue
        agent = agents_by_id.get(node.agent_id)
        if agent is None:
            continue
        granted = agent_granted_connector_types(agent.connector_type_refs)
        validate_allowed_connectors_subset(
            node_id=str(node.id),
            allowed_connectors=scope.allowed_connectors,
            granted_types=granted,
        )


async def _resolve_graph_references(
    session: AsyncSession,
    nodes: list[PipelineGraphNode],
    org_id: uuid.UUID,
    pipeline_owner_team_id: uuid.UUID | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate tenant-owned graph references and derive validator pins.

    Whenever the graph resolves model-backend pins, they are checked against
    the pipeline's team: a team-private model backend pinned by a pipeline owned
    by a different team (or by no team at all) raises 409
    ``model_backend_team_mismatch`` (PRD §9.3), mirroring the connector rule
    which is also enforced unconditionally. The mismatch rule itself decides
    whether an org-owned pipeline (``owner_team_id=None``) may pin a team-private
    backend.
    """
    agent_ids = {node.agent_id for node in nodes if node.agent_id is not None}
    agents_by_id = await _load_agents_by_ids(session, org_id, agent_ids)
    try:
        _validate_capability_scopes(nodes, agents_by_id)
    except ScopeViolationError as exc:
        # FAR-418: a node that widens (never narrows) its Agent's connector grants
        # is rejected — the save is refused, not silently accepted.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    await _load_existing_schema_ids(session, org_id, _collect_schema_ids(nodes))
    schema_pins, model_backend_pins = _build_schema_and_backend_pins(nodes, agents_by_id)
    if model_backend_pins:
        await _enforce_model_backend_team_bindings(
            session,
            org_id=org_id,
            pipeline_owner_team_id=pipeline_owner_team_id,
            model_backend_pins=model_backend_pins,
        )
    return schema_pins, model_backend_pins


@router.get("", responses={401: {"description": "Unauthorized"}})
@handle_db_errors("pipelines.list")
async def list_pipelines_endpoint(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query()] = None,
    include_archived: Annotated[bool, Query()] = False,
    folder_id: Annotated[uuid.UUID | None, Query()] = None,
    principal: TenantPrincipal = require_permission(_CODE_PIPELINE_LIST),
) -> PipelineListResponse:
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            result = await list_pipelines(
                session,
                page=page,
                page_size=page_size,
                cursor=cursor,
                include_archived=include_archived,
                folder_id=folder_id,
            )
    except ProgrammingError:
        _raise_db_migration_error()

    return PipelineListResponse(
        items=[PipelineResponse.model_validate(p) for p in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
        next_cursor=result.next_cursor,
        has_more=result.has_more,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
@handle_db_errors("pipelines.create")
async def create_pipeline_endpoint(
    req: PipelineCreate,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission("pipeline.create"),
) -> PipelineResponse:
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            pipeline = await create_pipeline(
                session,
                org_id=principal.organisation_id,
                name=req.name,
                account_id=principal.account_id,
                description=req.description,
                visibility=req.visibility,
                owner_team_id=req.owner_team_id,
                max_concurrent_runs=req.max_concurrent_runs,
                lock_wait_timeout_seconds=req.lock_wait_timeout_seconds,
                node_timeout_seconds=req.node_timeout_seconds,
                run_context_defaults=req.run_context_defaults,
                default_autonomy_level=req.default_autonomy_level,
                max_duration_seconds=req.max_duration_seconds,
                stale_run_timeout_minutes=req.stale_run_timeout_minutes,
                folder_id=req.folder_id,
            )
            if req.retry_policy is not None:
                # The model default ({}) applies when omitted; an explicit value
                # is persisted on the returned ORM row within this transaction.
                pipeline.retry_policy = req.retry_policy
    except ProgrammingError:
        _raise_db_migration_error()

    return PipelineResponse.model_validate(pipeline)


@router.get("/{pipeline_id}")
@handle_db_errors("pipelines.get")
async def get_pipeline_endpoint(
    pipeline_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_CODE_PIPELINE_LIST),
    _: TenantPrincipal = require_team_membership_or_admin(resolve_pipeline_team_scope),
) -> PipelineResponse:
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            pipeline = await get_pipeline(session, pipeline_id, organisation_id=principal.organisation_id)
    except ProgrammingError:
        _raise_db_migration_error()

    if pipeline is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PIPELINE_NOT_FOUND)
    return PipelineResponse.model_validate(pipeline)


@router.get("/{pipeline_id}/graph")
@handle_db_errors("pipelines.get_graph")
async def get_pipeline_graph_endpoint(
    pipeline_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission("pipeline.graph.read"),
) -> PipelineGraphResponse:
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            graph = await get_pipeline_graph(session, pipeline_id)
    except ProgrammingError:
        _raise_db_migration_error()

    if graph is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PIPELINE_NOT_FOUND)
    nodes, edges = graph
    return _graph_response(nodes, edges)


def _prepare_graph_write(
    req: PipelineGraphUpdate,
) -> tuple[list[dict[str, Any]], list[GraphEdgeData], dict[str, Any], list[dict[str, Any]]]:
    """Serialise a graph update into its write + validator representations."""
    node_data = [node.model_dump(mode="json") for node in req.nodes]
    edge_data = [_edge_to_data(edge) for edge in req.edges]
    validator_graph = {
        "nodes": node_data,
        "edges": [_edge_data_to_validator(edge) for edge in edge_data],
    }
    connector_bindings = extract_connector_bindings(node_data)
    return node_data, edge_data, validator_graph, connector_bindings


async def _validate_graph_save(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    validator_graph: dict[str, Any],
    connector_bindings: list[dict[str, Any]],
    model_backend_pins: list[dict[str, Any]],
) -> list[GraphValidationIssue]:
    """Run save-time graph validation, returning the advisory issue list.

    Loads the pipeline's guardrail eval rows so the graph-save validation can
    reject a per-node guardrail-cap violation (FAR-223 item 7) — the
    authoring-time rejection that the create_run fail-closed backstop also
    enforces at run start.
    """
    guardrail_rows = await _guardrail_config.load_pipeline_guardrail_rows(
        session,
        pipeline_id=pipeline_id,
        organisation_id=org_id,
    )
    validation = await GraphValidator().validate_definition(
        validator_graph,
        session,
        connector_bindings=connector_bindings,
        model_backend_pins=model_backend_pins,
        guardrail_definitions=list(guardrail_rows),
    )
    _reject_graph_validation_issues(validation.issues)
    return [
        _graph_validation_issue(
            severity=issue.severity,
            code=issue.code,
            message=issue.message,
            node_id=issue.node_id,
        )
        for issue in validation.issues
    ]


def _extract_agent_command_sync_updates(nodes: list[dict[str, Any]]) -> dict[uuid.UUID, str]:
    """FAR-488a: first-carried ``agent_command`` per distinct bound agent.

    A node WITHOUT an ``agent_id`` needs no sync (its node-level command always
    stands at snapshot time — ``_apply_agent_fields`` only materializes bound
    agents). A node carrying no usable ``agent_command`` value has nothing to
    sync. When several nodes bind the SAME agent, the FIRST node's command wins
    (deterministic; snapshot materialization applies one row value to every
    node bound to that agent, so per-node divergence is not representable).
    """
    updates: dict[uuid.UUID, str] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        raw_agent_id = node.get("agent_id")
        command = node.get("agent_command")
        if raw_agent_id is None or not isinstance(command, str) or not command:
            continue
        try:
            agent_id = uuid.UUID(str(raw_agent_id))
        except (TypeError, ValueError):
            continue
        updates.setdefault(agent_id, command)
    return updates


async def _sync_agent_row_commands(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    nodes: list[dict[str, Any]],
) -> int:
    """FAR-488a: sync a PATCHed node ``agent_command`` into the bound Agent row.

    At snapshot time ``_apply_agent_fields`` overwrites a bound node's
    ``agent_command`` with the Agent row's non-NULL value, while the graph PATCH
    used to persist node-level commands to ``graph_nodes_json`` only — so an
    operator's PATCH read back correctly but every run silently executed the
    stale Agent-row command (FAR-488 incident, 2026-08-29). Syncing the row
    inside the SAME transaction as the graph write makes "what you PATCH is
    what runs" hold.

    Deliberate skip cases: a node without ``agent_id`` (nothing bound — the
    node value already stands); an Agent row with a NULL ``agent_command``
    (the node value already stands at snapshot time); an incoming command
    equal to the row value (no-op). Returns the number of Agent rows updated.
    """
    updates = _extract_agent_command_sync_updates(nodes)
    if not updates:
        return 0
    result = await session.execute(select(Agent).where(Agent.id.in_(list(updates)), Agent.organisation_id == org_id))
    changed = 0
    for agent in result.scalars():
        incoming = updates.get(agent.id)
        if incoming is None or agent.agent_command is None or agent.agent_command == incoming:
            continue
        logger.info(
            "pipeline.graph.agent_command_synced",
            extra={"agent_id": str(agent.id), "organisation_id": str(org_id)},
        )
        agent.agent_command = incoming
        changed += 1
    return changed


@router.patch("/{pipeline_id}/graph")
@handle_db_errors("pipelines.replace_graph")
async def replace_pipeline_graph_endpoint(
    pipeline_id: uuid.UUID,
    req: PipelineGraphUpdate,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_CODE_PIPELINE_GRAPH_UPDATE),
    _: TenantPrincipal = require_team_membership_or_admin(resolve_pipeline_team_scope),
) -> PipelineGraphResponse:
    # Route layer carries the operator baseline ("pipeline.graph.update") for
    # defense-in-depth breadth; actual gate-weakening enforcement is the
    # service-layer backstop (operator+ privileged under the row lock, non-
    # privileged callers denied — hitl-gate-removal-guard-plan.md v19 §3 item
    # 5). There is deliberately no admin-only route gate here: operators are
    # "privileged" for weakening by design, and equivalent weakening remains
    # reachable via update_pipeline / convert_to_agent / revert_to_manual, so an
    # admin-only gate would only block the operator's primary graph-edit path.
    node_data, edge_data, validator_graph, connector_bindings = _prepare_graph_write(req)
    issues: list[GraphValidationIssue] = []

    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            pipeline = await _get_pipeline_or_404(session, pipeline_id)
            await _enforce_connector_team_bindings(
                session,
                principal.organisation_id,
                pipeline.owner_team_id,
                connector_bindings,
            )
            # FAR-309 PR A review: the guardrail-binding strip guard now lives
            # in the SERVICE LAYER (replace_pipeline_graph, under the row lock)
            # so every graph-mutation caller inherits it — including the
            # PATCH /{id} graph_json path and snapshot rollback. The admin flag
            # is resolved here and the service layer re-reads the live role
            # under the lock for REST callers.
            _schema_pins, model_backend_pins = await _resolve_graph_references(
                session,
                req.nodes,
                principal.organisation_id,
                pipeline_owner_team_id=pipeline.owner_team_id,
            )
            graph = await replace_pipeline_graph(
                session,
                pipeline_id=pipeline_id,
                org_id=principal.organisation_id,
                nodes=node_data,
                edges=[_edge_data_to_dict(edge) for edge in edge_data],
                is_privileged=_is_privileged(principal.org_role),
                caller_type="rest",
                account_id=principal.account_id,
                is_guardrail_admin=_is_guardrail_admin(principal),
            )
            if graph is not None:
                # FAR-488a: keep the bound Agent rows in step with node-level
                # agent_command PATCHes INSIDE the same transaction, so the
                # next snapshot materializes the command the operator saved.
                await _sync_agent_row_commands(session, org_id=principal.organisation_id, nodes=node_data)
                issues = await _validate_graph_save(
                    session,
                    org_id=principal.organisation_id,
                    pipeline_id=pipeline_id,
                    validator_graph=validator_graph,
                    connector_bindings=connector_bindings,
                    model_backend_pins=model_backend_pins,
                )
    except (HitlGateWeakeningDenied, GuardrailBindingStripDenied) as exc:
        await _handle_graph_write_denials(
            session,
            principal=principal,
            pipeline_id=pipeline_id,
            exc=exc,
        )
    except ProgrammingError:
        _raise_db_migration_error()

    if graph is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PIPELINE_NOT_FOUND)
    nodes, edges = graph
    return _graph_response(nodes, edges, validation_issues=issues)


def _is_admin(principal: TenantPrincipal) -> bool:
    return principal.org_role == "admin"


def _is_team_private(visibility: str | None, owner_team_id: uuid.UUID | None) -> bool:
    """True when a pipeline is currently team-private (has a team owner)."""
    return visibility not in ("org", None) and owner_team_id is not None


def _is_owner_reassignment(new_owner_team_id: uuid.UUID | None, current_owner_team_id: uuid.UUID | None) -> bool:
    """True when the update hands the pipeline to a different team."""
    return new_owner_team_id is not None and new_owner_team_id != current_owner_team_id


async def _require_team_membership(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    team_id: uuid.UUID,
    denial_detail: str,
) -> None:
    """Raise 403 unless the caller is a member of the given team."""
    is_member = await team_membership_exists(session, account_id=account_id, team_id=team_id)
    if not is_member:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=denial_detail)


async def _assert_team_transition_allowed(
    session: AsyncSession,
    principal: TenantPrincipal,
    current: Pipeline,
    update_payload: dict[str, Any],
) -> None:
    """Re-validate the team gate against the NEW visibility/owner_team_id values.

    The endpoint's ``require_team_membership_or_admin`` dependency checks the
    CURRENT ``owner_team_id`` at request time, but the update can change
    ``visibility`` or reassign/clear ``owner_team_id`` without re-checking the
    team gate against the NEW values — a member could downgrade a team-private
    pipeline to org-visible or hand it to a team they don't belong to
    (task-authz-b-visibility-guard). Re-runs the RLS-parity membership-or-admin
    gate inside the same transaction (RLS context is transaction-scoped).
    """
    # Org-admin bypass applies throughout (RLS parity).
    if _is_admin(principal):
        return

    changes_visibility = "visibility" in update_payload
    changes_owner_team = "owner_team_id" in update_payload
    if not changes_visibility and not changes_owner_team:
        return  # No-op: the team boundary is unchanged.

    new_visibility = update_payload.get("visibility", current.visibility)
    new_owner_team_id = update_payload.get("owner_team_id", current.owner_team_id)

    # A team-visible pipeline must keep an owner team.
    if new_visibility == "team" and new_owner_team_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="owner_team_id is required when visibility is 'team'",
        )

    # Current team gate (if the pipeline is currently team-private): the caller
    # must be a member of the CURRENT team (or admin). ``_is_team_private`` is
    # only True when an owner team is set, so this never skips a real gate.
    current_team_id = current.owner_team_id
    if current_team_id is not None and _is_team_private(current.visibility, current_team_id):
        await _require_team_membership(
            session,
            account_id=principal.account_id,
            team_id=current_team_id,
            denial_detail="Not a member of the team that owns this resource",
        )

    # New team gate: reassigning to a team requires membership of the NEW team.
    if new_owner_team_id is not None and _is_owner_reassignment(new_owner_team_id, current.owner_team_id):
        await _require_team_membership(
            session,
            account_id=principal.account_id,
            team_id=new_owner_team_id,
            denial_detail="Cannot reassign a pipeline to a team you are not a member of",
        )


async def _maybe_audit_autonomy_change(
    session: AsyncSession,
    *,
    principal: TenantPrincipal,
    pipeline_id: uuid.UUID,
    updates: dict[str, Any],
) -> None:
    """Append the autonomy-level-change audit event when the level changed."""
    if "default_autonomy_level" not in updates:
        return
    previous = await get_pipeline(session, pipeline_id)
    prev_level = previous.default_autonomy_level if previous else None
    if prev_level == updates["default_autonomy_level"]:
        return
    await append_audit_event(
        session,
        org_id=principal.organisation_id,
        event_type="pipeline.autonomy_level_changed",
        actor_user_id=principal.account_id,
        resource_type="pipeline",
        resource_id=pipeline_id,
        payload_json=autonomy_change_payload(
            previous=prev_level,
            current=updates["default_autonomy_level"],
        ),
        request_id=getattr(principal, "request_id", None),
    )


async def _apply_graph_update(
    session: AsyncSession,
    *,
    pipeline_id: uuid.UUID,
    org_id: uuid.UUID,
    principal: TenantPrincipal,
    graph_json: PipelineGraphUpdate,
    updates: dict[str, Any],
) -> None:
    """Apply a graph replacement shipped inside a PATCH update payload."""
    node_data = [node.model_dump(mode="json") for node in graph_json.nodes]
    edge_data = [_edge_to_data(edge) for edge in graph_json.edges]
    graph_bindings = extract_connector_bindings(node_data)
    existing = await get_pipeline(session, pipeline_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PIPELINE_NOT_FOUND)
    effective_owner_team_id = updates.get("owner_team_id", existing.owner_team_id)
    await _enforce_connector_team_bindings(
        session,
        org_id,
        effective_owner_team_id,
        graph_bindings,
    )
    await _resolve_graph_references(
        session,
        graph_json.nodes,
        org_id,
        pipeline_owner_team_id=effective_owner_team_id,
    )
    graph = await replace_pipeline_graph(
        session,
        pipeline_id=pipeline_id,
        org_id=org_id,
        nodes=node_data,
        edges=[_edge_data_to_dict(edge) for edge in edge_data],
        is_privileged=_is_privileged(principal.org_role),
        caller_type="rest",
        account_id=principal.account_id,
        is_guardrail_admin=_is_guardrail_admin(principal),
    )
    if graph is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PIPELINE_NOT_FOUND)
    # FAR-488a: same Agent-row sync as the PATCH /graph endpoint — a graph
    # replacement shipped inside a PATCH update payload must also run.
    await _sync_agent_row_commands(session, org_id=org_id, nodes=node_data)


def _raise_active_runs_conflict(exc: PipelineHasActiveRunsError) -> None:
    """Raise the 409 for an ownership transfer blocked by active runs (PRD §9.3)."""
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"pipeline_has_active_runs: {exc.active_run_count} run(s) still in progress; "
        "cannot change ownership while any run is active",
    ) from None


@router.patch("/{pipeline_id}")
@handle_db_errors("pipelines.update")
async def update_pipeline_endpoint(
    pipeline_id: uuid.UUID,
    req: PipelineUpdate,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_CODE_PIPELINE_UPDATE),
    _: TenantPrincipal = require_team_membership_or_admin(resolve_pipeline_team_scope),
) -> PipelineResponse:
    updates = req.model_dump(exclude_unset=True)
    has_graph = "graph_json" in updates
    updates.pop("graph_json", None)
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            current = await _get_pipeline_or_404(session, pipeline_id)
            await _assert_team_transition_allowed(session, principal, current, updates)
            ownership_changed = "owner_team_id" in updates and updates["owner_team_id"] != current.owner_team_id
            await _maybe_audit_autonomy_change(
                session,
                principal=principal,
                pipeline_id=pipeline_id,
                updates=updates,
            )
            if has_graph and req.graph_json is not None:
                await _apply_graph_update(
                    session,
                    pipeline_id=pipeline_id,
                    org_id=principal.organisation_id,
                    principal=principal,
                    graph_json=req.graph_json,
                    updates=updates,
                )
            pipeline = await update_pipeline(
                session,
                pipeline_id,
                updates,
                org_id=principal.organisation_id,
                account_id=principal.account_id,
                request_id=getattr(principal, "request_id", None),
            )
            # Refresh the ORM row inside the transaction so the DB-computed
            # `updated_at` (onupdate=func.current_timestamp()) is loaded while
            # the transaction is active. Accessing it after commit with
            # autobegin=False raises InvalidRequestError -> 422 silent-success.
            if pipeline is not None:
                await session.refresh(pipeline)
    except (HitlGateWeakeningDenied, GuardrailBindingStripDenied) as exc:
        await _handle_graph_write_denials(
            session,
            principal=principal,
            pipeline_id=pipeline_id,
            exc=exc,
        )
    except PipelineHasActiveRunsError as exc:
        _raise_active_runs_conflict(exc)
    except ProgrammingError:
        _raise_db_migration_error()

    if pipeline is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PIPELINE_NOT_FOUND)
    response = PipelineResponse.model_validate(pipeline)
    response.connector_rebind_required = ownership_changed
    return response


@router.delete("/{pipeline_id}", status_code=status.HTTP_204_NO_CONTENT)
@handle_db_errors("pipelines.delete")
async def delete_pipeline_endpoint(
    pipeline_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission("pipeline.delete"),
    _: TenantPrincipal = require_team_membership_or_admin(resolve_pipeline_team_scope),
) -> None:
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            deleted = await soft_delete_pipeline(session, pipeline_id)
    except ProgrammingError:
        _raise_db_migration_error()

    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PIPELINE_NOT_FOUND)


@router.post("/{pipeline_id}/restore")
@handle_db_errors("pipelines.restore")
async def restore_pipeline_endpoint(
    pipeline_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_CODE_PIPELINE_UPDATE),
) -> PipelineResponse:
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            existing = await get_pipeline(
                session, pipeline_id, include_deleted=True, organisation_id=principal.organisation_id
            )
            if existing is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PIPELINE_NOT_FOUND)
            pipeline = await restore_pipeline(session, pipeline_id)
    except ProgrammingError:
        _raise_db_migration_error()
    if pipeline is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PIPELINE_NOT_FOUND)
    return PipelineResponse.model_validate(pipeline)


@router.post("/{pipeline_id}/archive")
@handle_db_errors("pipelines.archive")
async def archive_pipeline_endpoint(
    pipeline_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_CODE_PIPELINE_UPDATE),
) -> PipelineResponse:
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            existing = await get_pipeline(session, pipeline_id, organisation_id=principal.organisation_id)
            if existing is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PIPELINE_NOT_FOUND)
            pipeline = await archive_pipeline(session, pipeline_id)
    except ProgrammingError:
        _raise_db_migration_error()
    if pipeline is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PIPELINE_NOT_FOUND)
    return PipelineResponse.model_validate(pipeline)


@router.post("/{pipeline_id}/unarchive")
@handle_db_errors("pipelines.unarchive")
async def unarchive_pipeline_endpoint(
    pipeline_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_CODE_PIPELINE_UPDATE),
) -> PipelineResponse:
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            existing = await get_pipeline(session, pipeline_id, organisation_id=principal.organisation_id)
            if existing is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PIPELINE_NOT_FOUND)
            pipeline = await unarchive_pipeline(session, pipeline_id)
    except ProgrammingError:
        _raise_db_migration_error()
    if pipeline is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PIPELINE_NOT_FOUND)
    return PipelineResponse.model_validate(pipeline)


# ---------------------------------------------------------------------------
# Clone
# ---------------------------------------------------------------------------


class PipelineCloneRequest(BaseModel):
    name: str | None = Field(
        None,
        min_length=1,
        max_length=255,
        description="Overrides the default 'Copy of {original_name}' name",
    )


async def _clone_pipeline_into_org(
    session: AsyncSession,
    *,
    pipeline_id: uuid.UUID,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    org_role: str | None,
    requested_name: str | None,
) -> tuple[Any, str]:
    """Clone a pipeline within an org, validating the source and target name.

    Returns ``(cloned_row, target_name)``. Raises ``HTTPException`` for a missing
    source or an already-used name.
    """
    source = await get_pipeline(session, pipeline_id)
    if source is None:
        logger.warning("Copy aborted: source pipeline %s not found", pipeline_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"pipeline_copy_failed: Source pipeline not found [pipeline_id: {pipeline_id}]",
        )

    target_name = requested_name or f"Copy of {source.name}"
    if not await check_pipeline_name_available(session, org_id, target_name):
        logger.warning(
            "Copy aborted: name '%s' already exists in org %s",
            _sanitise_log_value(target_name),
            org_id,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(f"pipeline_copy_failed: A pipeline named '{target_name}' already exists in this organisation"),
        )

    cloned = await clone_pipeline(
        session,
        org_id=org_id,
        pipeline_id=pipeline_id,
        account_id=account_id,
        org_role=org_role,
        new_name=requested_name,
    )
    if cloned is None:
        logger.warning("Copy aborted: source pipeline %s disappeared during copy", pipeline_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(f"pipeline_copy_failed: Source pipeline disappeared during copy [pipeline_id: {pipeline_id}]"),
        )

    await append_audit_event(
        session,
        org_id=org_id,
        event_type="pipeline.cloned",
        actor_user_id=account_id,
        resource_type="pipeline",
        resource_id=pipeline_id,
        payload_json={
            "cloned_pipeline_id": str(cloned.id),
            "target_name": target_name,
        },
    )
    return cloned, target_name


@router.post("/{pipeline_id}/clone", status_code=status.HTTP_201_CREATED)
@handle_db_errors("pipelines.clone")
async def clone_pipeline_endpoint(
    pipeline_id: uuid.UUID,
    req: PipelineCloneRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission("pipeline.create"),
) -> PipelineResponse:
    logger.info(
        "Copy request: pipeline=%s org=%s user=%s",
        pipeline_id,
        principal.organisation_id,
        principal.account_id,
    )

    if principal.org_role == "viewer":
        logger.warning(
            "Copy denied: user %s has role '%s' (requires admin)",
            principal.account_id,
            principal.org_role,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only organisation members and admins can clone pipelines",
        )

    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            cloned, target_name = await _clone_pipeline_into_org(
                session,
                pipeline_id=pipeline_id,
                org_id=principal.organisation_id,
                account_id=principal.account_id,
                org_role=principal.org_role,
                requested_name=req.name,
            )
    except ProgrammingError:
        _raise_db_migration_error()

    logger.info("Copy complete: %s -> %s (%s)", pipeline_id, cloned.id, _sanitise_log_value(target_name))
    return PipelineResponse.model_validate(cloned)


# ---------------------------------------------------------------------------
# Save as composite
# ---------------------------------------------------------------------------


class SaveAsCompositeRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    selected_node_ids: list[uuid.UUID] = Field(min_length=1)


_PARAM_PATTERN = re.compile(r"\{\{parameter\.(\w+)\}\}")


async def _detect_parameter_ports(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    agent_ids: set[Any],
) -> list[dict[str, Any]]:
    """Auto-detect ``{{parameter.<name>}}`` placeholders in the selected agents' prompts.

    Returns one parameter port dict per unique placeholder name, ordered by the
    first agent that references it.
    """
    detected_ports: list[dict[str, Any]] = []
    if not agent_ids:
        return detected_ports
    agents_result = await session.execute(select(Agent).where(Agent.id.in_(agent_ids), Agent.organisation_id == org_id))
    for agent in agents_result.scalars().all():
        matches = _PARAM_PATTERN.findall(agent.prompt_template or "")
        for param_name in matches:
            if any(p.get("name") == param_name for p in detected_ports):
                continue
            detected_ports.append(
                {
                    "id": str(uuid.uuid4()),
                    "name": param_name,
                    "label": param_name.replace("_", " ").title(),
                    "description": None,
                    "type": "string",
                    "required": False,
                    "default_value": None,
                    "options": None,
                    "target_injection": {
                        "mode": "prompt_replace",
                        "node_id": str(agent.id),
                        "injection_point": "prompt_template",
                    },
                }
            )
    return detected_ports


@router.post("/{pipeline_id}/save-as-composite", status_code=status.HTTP_201_CREATED)
@handle_db_errors("pipelines.save_as_composite")
async def save_as_composite_endpoint(
    pipeline_id: uuid.UUID,
    req: SaveAsCompositeRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: Annotated[TenantPrincipal, Depends(get_current_tenant_user)],
) -> dict[str, Any]:
    try:
        async with session.begin():
            await _set_rls_context(session, principal)

            pipeline = await get_pipeline(session, pipeline_id)
            if pipeline is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PIPELINE_NOT_FOUND)

            all_nodes = pipeline.graph_nodes_json
            selected_ids_str = {str(nid) for nid in req.selected_node_ids}
            sub_nodes = [n for n in all_nodes if str(n.get("id")) in selected_ids_str]
            if not sub_nodes:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="No valid nodes selected",
                )

            sub_node_ids_str = {str(n.get("id")) for n in sub_nodes}

            # Auto-detect parameter placeholders: scan all agent prompts referenced by selected nodes
            agent_ids = {n.get("agent_id") for n in sub_nodes if n.get("agent_id") is not None}
            detected_ports = await _detect_parameter_ports(
                session,
                org_id=principal.organisation_id,
                agent_ids=agent_ids,
            )

            # Extract edges that connect selected nodes
            all_edges_raw = await session.execute(select(PipelineEdge).where(PipelineEdge.pipeline_id == pipeline_id))
            sub_edges = [
                {
                    "id": str(edge.id),
                    "source_node_id": str(edge.source_node_id),
                    "target_node_id": str(edge.target_node_id),
                    "edge_type": edge.edge_type,
                    "condition_expression": edge.condition_expression,
                    "hitl_gate_config": edge.hitl_gate_config,
                }
                for edge in all_edges_raw.scalars().all()
                if str(edge.source_node_id) in sub_node_ids_str and str(edge.target_node_id) in sub_node_ids_str
            ]

            # Create the composite template
            template = await create_composite_template(
                session,
                org_id=principal.organisation_id,
                account_id=principal.account_id,
                name=req.name,
                description=req.description,
                sub_pipeline_graph_json={"nodes": [dict(n) for n in sub_nodes], "edges": sub_edges},
                parameter_ports_json=detected_ports,
                version="0.1.0",
            )

    except ProgrammingError:
        _raise_db_migration_error()

    return {
        "id": str(template.id),
        "name": template.name,
        "version": template.version,
        "parameter_ports": detected_ports,
    }


# ---------------------------------------------------------------------------
# Quality Report
# ---------------------------------------------------------------------------


class QualityReportResponse(BaseModel):
    period: dict[str, str]
    summary: dict[str, Any]
    week_over_week: dict[str, Any]
    trend: list[dict[str, Any]]
    eval_breakdown: dict[str, Any]
    deliveries: list[dict[str, Any]]


def _endpoint_events(raw_events: object) -> list[Any]:
    """Normalise an endpoint's ``events`` column (JSON list or raw list)."""
    if isinstance(raw_events, list):
        return raw_events
    if isinstance(raw_events, str):
        try:
            parsed = json.loads(raw_events)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    return []


async def _quality_report_recipient_urls(
    session: AsyncSession,
    org_id: uuid.UUID,
) -> list[str]:
    """Collect webhook URLs subscribed to the ``quality_report`` event.

    ``events`` may be stored as a JSON list or a raw list; both shapes are
    normalised before the membership check.
    """
    endpoints = (
        await session.execute(
            select(NotificationEndpoint).where(
                NotificationEndpoint.organisation_id == org_id,
            )
        )
    ).scalars()

    return [ep.url for ep in endpoints if "quality_report" in _endpoint_events(ep.events)]


@router.post(
    "/{pipeline_id}/quality-report",
)
@handle_db_errors("pipelines.trigger_quality_report")
async def trigger_quality_report(
    pipeline_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_CODE_PIPELINE_UPDATE),
) -> QualityReportResponse:
    try:
        async with session.begin():
            await _set_rls_context(session, principal)

            pipeline = await get_pipeline(session, pipeline_id)
            if pipeline is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PIPELINE_NOT_FOUND)

            report = await generate_quality_report(session, principal.organisation_id)

            recipient_urls = await _quality_report_recipient_urls(session, principal.organisation_id)

            deliveries: list[dict[str, Any]] = []
            if recipient_urls:
                deliveries = await deliver_quality_report(report, {"webhook_urls": recipient_urls})
    except ProgrammingError:
        _raise_db_migration_error()

    return QualityReportResponse(
        period=report["period"],
        summary=report["summary"],
        week_over_week=report["week_over_week"],
        trend=report["trend"],
        eval_breakdown=report["eval_breakdown"],
        deliveries=deliveries,
    )


# ---------------------------------------------------------------------------
# Snapshot Versioning
# ---------------------------------------------------------------------------


class SnapshotResponse(BaseModel):
    id: uuid.UUID
    pipeline_id: uuid.UUID
    snapshot_version: int
    tag: str | None
    notes: str | None
    created_at: datetime | None
    created_by: uuid.UUID | None = Field(default=None, validation_alias="account_id")
    # FAR-402 P6: live-edit history + release-channel discriminator. Additive
    # fields default to the legacy run-kind/none-channel snapshot so older
    # clients that ignore them keep working.
    version_kind: str = "run"
    created_kind: str = "run"
    draft: bool = False
    channel: str = "none"

    model_config = {"from_attributes": True, "populate_by_name": True}


class SnapshotDetailResponse(SnapshotResponse):
    graph_json: dict[str, Any] | None = None
    connector_bindings_json: list[dict[str, Any]] | None = None
    schema_pins_json: list[dict[str, Any]] | None = None
    prompt_pins_json: list[dict[str, Any]] | None = None
    model_backend_pins_json: list[dict[str, Any]] | None = None
    default_autonomy_level: str | None = None
    run_context_defaults: dict[str, Any] | None = None


class SnapshotTagUpdate(BaseModel):
    tag: str | None = None
    notes: str | None = None


class SnapshotListResponse(BaseModel):
    items: list[SnapshotResponse]
    total: int


class SnapshotCreateEdit(BaseModel):
    """Body for a live-edit save (FAR-402 P6).

    ``draft`` marks an in-progress editor auto-save (``False`` = a committed
    edit). ``channel`` optionally tags the edit's release channel (default
    ``none`` — the live-edit chain is not channel-routed unless set).
    """

    draft: bool = False
    channel: str | None = None


class SnapshotDiffQuery(BaseModel):
    snapshot_a_id: uuid.UUID
    snapshot_b_id: uuid.UUID


class SnapshotDiffResponse(BaseModel):
    snapshot_a: dict[str, Any]
    snapshot_b: dict[str, Any]
    nodes_added: list[dict[str, Any]]
    nodes_removed: list[dict[str, Any]]
    nodes_modified: list[dict[str, Any]]
    edges_added: list[dict[str, Any]]
    edges_removed: list[dict[str, Any]]
    edges_modified: list[dict[str, Any]]
    # FAR-402 P6: semantic-diff + impact layer (port-signature deltas,
    # downstream-impact oracle, save-time breaking-change warnings).
    semantic: dict[str, Any] = Field(default_factory=dict)


def _snapshot_to_response(s: Any) -> SnapshotResponse:
    return SnapshotResponse(
        id=s.id,
        pipeline_id=s.pipeline_id,
        snapshot_version=s.snapshot_version,
        tag=s.tag,
        notes=s.notes,
        created_at=s.created_at,
        created_by=s.account_id,
        version_kind=s.version_kind,
        created_kind=s.created_kind,
        draft=s.draft,
        channel=s.channel,
    )


def _snapshot_to_detail_response(s: Any) -> SnapshotDetailResponse:
    return SnapshotDetailResponse(
        id=s.id,
        pipeline_id=s.pipeline_id,
        snapshot_version=s.snapshot_version,
        tag=s.tag,
        notes=s.notes,
        created_at=s.created_at,
        created_by=s.account_id,
        version_kind=s.version_kind,
        created_kind=s.created_kind,
        draft=s.draft,
        channel=s.channel,
        graph_json=s.graph_json,
        connector_bindings_json=s.connector_bindings_json,
        schema_pins_json=s.schema_pins_json,
        prompt_pins_json=s.prompt_pins_json,
        model_backend_pins_json=s.model_backend_pins_json,
        default_autonomy_level=s.default_autonomy_level,
        run_context_defaults=s.run_context_defaults,
    )


@router.get("/{pipeline_id}/snapshots")
@handle_db_errors("pipelines.list_snapshots")
async def list_snapshot_endpoint(
    pipeline_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    principal: TenantPrincipal = require_permission(_CODE_PIPELINE_LIST),
    _: TenantPrincipal = require_team_membership_or_admin(resolve_pipeline_team_scope),
) -> SnapshotListResponse:
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            pipeline = await get_pipeline(session, pipeline_id)
            if pipeline is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pipeline not found")
            snapshots, total = await list_snapshots(session, pipeline_id, page=page, page_size=page_size)
    except ProgrammingError:
        _raise_db_migration_error()

    return SnapshotListResponse(
        items=[_snapshot_to_response(s) for s in snapshots],
        total=total,
    )


@router.post(
    "/{pipeline_id}/snapshots",
    dependencies=[require_feature("pipeline_diff_rollback")],
)
@handle_db_errors("pipelines.save_edit_snapshot")
async def save_edit_snapshot_endpoint(
    pipeline_id: uuid.UUID,
    req: SnapshotCreateEdit,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_CODE_PIPELINE_GRAPH_UPDATE),
    _: TenantPrincipal = require_team_membership_or_admin(resolve_pipeline_team_scope),
) -> SnapshotResponse:
    """Save a LIVE-EDIT snapshot of the current graph (FAR-402 P6).

    Creates a new snapshot row tagged ``version_kind='edit'`` so the editor's
    save history is distinct from run-frozen snapshots; the prior snapshot row
    stays immutable, so rollback remains a pointer swap to a prior version. A
    ``draft`` save marks an in-progress editor auto-save; ``channel`` optionally
    tags the edit's release channel.
    """
    channel = str(req.channel or "none").strip().lower()
    if channel not in VALID_RELEASE_CHANNELS:
        valid = ", ".join(sorted(VALID_RELEASE_CHANNELS))
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid release channel: {req.channel!r}. Must be one of {valid}.",
        )
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            pipeline = await get_pipeline(session, pipeline_id)
            if pipeline is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PIPELINE_NOT_FOUND)
            snapshot = await create_snapshot_edit(
                session,
                pipeline_id=pipeline_id,
                account_id=principal.account_id,
                draft=req.draft,
                channel=channel,
            )
    except ProgrammingError:
        _raise_db_migration_error()

    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to save edit snapshot")
    return _snapshot_to_response(snapshot)


@router.get("/{pipeline_id}/snapshots/{snapshot_id}")
@handle_db_errors("pipelines.get_snapshot_detail")
async def get_snapshot_detail_endpoint(
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_CODE_PIPELINE_LIST),
    _: TenantPrincipal = require_team_membership_or_admin(resolve_pipeline_team_scope),
) -> SnapshotDetailResponse:
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            snapshot = await get_snapshot_detail(
                session,
                snapshot_id,
                organisation_id=principal.organisation_id,
                pipeline_id=pipeline_id,
            )
    except ProgrammingError:
        _raise_db_migration_error()

    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_SNAPSHOT_NOT_FOUND)
    return _snapshot_to_detail_response(snapshot)


@router.patch("/{pipeline_id}/snapshots/{snapshot_id}")
@handle_db_errors("pipelines.tag_snapshot")
async def tag_snapshot_endpoint(
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    req: SnapshotTagUpdate,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_CODE_PIPELINE_UPDATE),
    _: TenantPrincipal = require_team_membership_or_admin(resolve_pipeline_team_scope),
) -> SnapshotResponse:
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            snapshot = await tag_snapshot(session, snapshot_id, tag=req.tag, notes=req.notes)
    except ProgrammingError:
        _raise_db_migration_error()

    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_SNAPSHOT_NOT_FOUND)
    return _snapshot_to_response(snapshot)


@router.post(
    "/{pipeline_id}/snapshots/{snapshot_id}/rollback",
    dependencies=[require_feature("pipeline_diff_rollback")],
)
@handle_db_errors("pipelines.rollback_snapshot")
async def rollback_snapshot_endpoint(
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_CODE_PIPELINE_GRAPH_UPDATE),
    _: TenantPrincipal = require_team_membership_or_admin(resolve_pipeline_team_scope),
) -> SnapshotResponse:
    # Route layer carries the operator baseline ("pipeline.graph.update") for
    # defense-in-depth breadth; actual gate-weakening enforcement is the
    # service-layer backstop (operator+ privileged under the row lock, non-
    # privileged callers denied — hitl-gate-removal-guard-plan.md v19 §3 item
    # 5). There is deliberately no admin-only route gate here, matching the
    # graph-replace endpoint: operators are "privileged" for weakening by
    # design (equivalent weakening stays reachable via update_pipeline).
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            new_snapshot = await rollback_to_snapshot(
                session,
                pipeline_id,
                snapshot_id,
                account_id=principal.account_id,
                is_privileged=_is_privileged(principal.org_role),
                caller_type="rest",
                is_guardrail_admin=_is_guardrail_admin(principal),
            )
    except (HitlGateWeakeningDenied, GuardrailBindingStripDenied) as exc:
        await _handle_graph_write_denials(
            session,
            principal=principal,
            pipeline_id=pipeline_id,
            exc=exc,
        )
    except ProgrammingError:
        _raise_db_migration_error()

    if new_snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Snapshot or pipeline not found",
        )
    return _snapshot_to_response(new_snapshot)


@router.delete("/{pipeline_id}/snapshots/{snapshot_id}", status_code=status.HTTP_204_NO_CONTENT)
@handle_db_errors("pipelines.delete_snapshot")
async def delete_snapshot_endpoint(
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission("pipeline.delete"),
    _: TenantPrincipal = require_team_membership_or_admin(resolve_pipeline_team_scope),
) -> None:
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can delete snapshots",
        )
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            snapshot = await get_snapshot_detail(
                session,
                snapshot_id,
                organisation_id=principal.organisation_id,
                pipeline_id=pipeline_id,
            )
            if snapshot is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Snapshot not found")
            deleted = await delete_snapshot(session, snapshot_id)
    except ProgrammingError:
        _raise_db_migration_error()

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete the latest snapshot",
        )


@router.post(
    "/{pipeline_id}/snapshots/diff",
    dependencies=[require_feature("pipeline_diff_rollback")],
)
@handle_db_errors("pipelines.diff_snapshots")
async def diff_snapshot_endpoint(
    pipeline_id: uuid.UUID,
    req: SnapshotDiffQuery,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_CODE_PIPELINE_LIST),
    _: TenantPrincipal = require_team_membership_or_admin(resolve_pipeline_team_scope),
) -> SnapshotDiffResponse:
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            result = await diff_snapshots(session, req.snapshot_a_id, req.snapshot_b_id)
    except ProgrammingError:
        _raise_db_migration_error()

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="One or both snapshots not found",
        )
    return SnapshotDiffResponse(**result)


# ---------------------------------------------------------------------------
# Folder assignment
# ---------------------------------------------------------------------------


class PipelineFolderMoveRequest(BaseModel):
    folder_id: uuid.UUID | None = None


@router.patch("/{pipeline_id}/folder")
@handle_db_errors("pipelines.move_to_folder")
async def move_pipeline_to_folder_endpoint(
    pipeline_id: uuid.UUID,
    req: PipelineFolderMoveRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_CODE_PIPELINE_UPDATE),
) -> PipelineResponse:
    try:
        async with session.begin():
            await _set_rls_context(session, principal)
            pipeline = await move_pipeline_to_folder(session, pipeline_id, req.folder_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(e),
        ) from None
    except ProgrammingError:
        _raise_db_migration_error()
    if pipeline is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PIPELINE_NOT_FOUND)
    return PipelineResponse.model_validate(pipeline)


# ---------------------------------------------------------------------------
# Node conversion: manual <-> agent
# ---------------------------------------------------------------------------


class ConvertToAgentRequest(BaseModel):
    agent_id: uuid.UUID
    connector_binding: ConnectorBinding
    model_backend_id: uuid.UUID


async def _load_locked_pipeline_graph(
    session: AsyncSession,
    pipeline_id: uuid.UUID,
) -> tuple[list[dict[str, Any]], list[Any]]:
    """Load and row-lock a pipeline, returning its graph nodes + edge rows.

    Raises 404 when the pipeline does not exist.
    """
    pipeline_row = (
        await session.execute(select(Pipeline).where(Pipeline.id == pipeline_id).with_for_update())
    ).scalar_one_or_none()
    if pipeline_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PIPELINE_NOT_FOUND)
    nodes = list(pipeline_row.graph_nodes_json) if pipeline_row.graph_nodes_json else []
    edges = list((await session.execute(select(PipelineEdge).where(PipelineEdge.pipeline_id == pipeline_id))).scalars())
    return nodes, edges


async def _save_locked_graph(
    session: AsyncSession,
    *,
    pipeline_id: uuid.UUID,
    org_id: uuid.UUID,
    principal: TenantPrincipal,
    nodes: list[dict[str, Any]],
    edges: list[Any],
) -> tuple[list[dict[str, Any]], list[Any]] | None:
    """Persist a locked node-conversion graph via the shared save path."""
    return await _save_graph(
        session,
        pipeline_id,
        org_id,
        nodes,
        edges,
        is_privileged=_is_privileged(principal.org_role),
        caller_type="rest",
        account_id=principal.account_id,
        is_guardrail_admin=_is_guardrail_admin(principal),
    )


async def _finalize_locked_graph_save(
    exc: Exception,
    session: AsyncSession,
    *,
    principal: TenantPrincipal,
    pipeline_id: uuid.UUID,
) -> None:
    """Translate a locked-graph save error into the correct HTTP response.

    ``HitlGateWeakeningDenied`` is recorded (the guarded write already rolled
    back) and control returns to the caller, which then raises the 404
    saved-graph response. The other two errors are translated directly into an
    ``HTTPException``. Shared by the convert-to-agent and revert-to-manual
    endpoints, which only differ in how they prepare ``nodes``/``edges``.
    """
    if isinstance(exc, HitlGateWeakeningDenied):
        await _deny_hitl_gate(
            session,
            org_id=principal.organisation_id,
            account_id=principal.account_id,
            pipeline_id=pipeline_id,
            exc=exc,
            request_id=getattr(principal, "request_id", None),
        )
        return
    if isinstance(exc, GuardrailBindingStripDenied):
        raise HTTPException(
            status_code=denial_http_status(exc.reason_code),
            detail=exc.detail,
        ) from exc
    if isinstance(exc, ProgrammingError):
        logger.exception(_CODE_ROUTES_PIPELINES)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_THIS_FEATURE_NOT_AVAILABLE,
        ) from exc
    raise exc


@router.post(
    "/{pipeline_id}/nodes/{node_id}/convert-to-agent",
)
@handle_db_errors("pipelines.convert_node_to_agent")
async def convert_node_to_agent_endpoint(
    pipeline_id: uuid.UUID,
    node_id: uuid.UUID,
    req: ConvertToAgentRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_CODE_PIPELINE_GRAPH_UPDATE),
) -> PipelineGraphResponse:
    try:
        async with session.begin():
            await _set_rls_context(session, principal)

            nodes, edges = await _load_locked_pipeline_graph(session, pipeline_id)

            target = _find_node_in_list(nodes, node_id)
            if target is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
            if target.get("node_type") != "manual":
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Only manual nodes can be converted to agent",
                )

            agent = (
                await session.execute(
                    select(Agent).where(
                        Agent.id == req.agent_id,
                        Agent.organisation_id == principal.organisation_id,
                    )
                )
            ).scalar_one_or_none()
            if agent is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

            connector = (
                await session.execute(
                    select(ConnectorInstance).where(
                        ConnectorInstance.id == req.connector_binding.instance_id,
                        ConnectorInstance.organisation_id == principal.organisation_id,
                    )
                )
            ).scalar_one_or_none()
            if connector is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found")
            if connector.connector_type_id != req.connector_binding.type:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Connector type mismatch",
                )

            model_backend = (
                await session.execute(
                    select(ModelBackend).where(
                        ModelBackend.id == req.model_backend_id,
                        ModelBackend.organisation_id == principal.organisation_id,
                    )
                )
            ).scalar_one_or_none()
            if model_backend is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model backend not found")

            target["node_type"] = "agent"
            target["agent_id"] = str(req.agent_id)
            target["connector_binding"] = {
                "type": req.connector_binding.type,
                "instance_id": str(req.connector_binding.instance_id),
            }
            target.pop("output_schema_id", None)

            await append_audit_event(
                session,
                org_id=principal.organisation_id,
                actor_user_id=principal.account_id,
                event_type="pipeline.node.convert_to_agent",
                resource_type="pipeline",
                resource_id=pipeline_id,
                payload_json={
                    "node_id": str(node_id),
                    "agent_id": str(req.agent_id),
                },
            )

            saved = await _save_locked_graph(
                session,
                pipeline_id=pipeline_id,
                org_id=principal.organisation_id,
                principal=principal,
                nodes=nodes,
                edges=edges,
            )
    except (HitlGateWeakeningDenied, GuardrailBindingStripDenied, ProgrammingError) as exc:
        await _finalize_locked_graph_save(exc, session, principal=principal, pipeline_id=pipeline_id)

    if saved is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PIPELINE_NOT_FOUND)
    saved_nodes, saved_edges = saved
    return _graph_response(saved_nodes, saved_edges)


@router.post(
    "/{pipeline_id}/nodes/{node_id}/revert-to-manual",
)
@handle_db_errors("pipelines.revert_node_to_manual")
async def revert_node_to_manual_endpoint(
    pipeline_id: uuid.UUID,
    node_id: uuid.UUID,
    snapshot_id: Annotated[uuid.UUID, Query()],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_permission(_CODE_PIPELINE_GRAPH_UPDATE),
) -> PipelineGraphResponse:
    try:
        async with session.begin():
            await _set_rls_context(session, principal)

            nodes, edges = await _load_locked_pipeline_graph(session, pipeline_id)

            target = _find_node_in_list(nodes, node_id)
            if target is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
            if target.get("node_type") != "agent":
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Only agent nodes can be reverted to manual",
                )

            snapshot = await get_snapshot_detail(
                session,
                snapshot_id,
                organisation_id=principal.organisation_id,
                pipeline_id=pipeline_id,
            )
            if snapshot is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_SNAPSHOT_NOT_FOUND)

            snapshot_nodes = snapshot.graph_json.get("nodes", [])
            snapshot_node = _find_node_in_list(snapshot_nodes, node_id)
            if snapshot_node is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Snapshot does not contain this node",
                )
            if snapshot_node.get("node_type") != "manual":
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Snapshot node was not a manual node",
                )

            output_schema_id = snapshot_node.get("output_schema_id")
            if output_schema_id is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Snapshot node has no output schema",
                )

            target["node_type"] = "manual"
            sid = str(output_schema_id) if not isinstance(output_schema_id, str) else output_schema_id
            target["output_schema_id"] = sid
            target.pop("agent_id", None)
            target.pop("connector_binding", None)
            if not target.get("label"):
                target["label"] = snapshot_node.get("label") or f"Manual {node_id}"

            await append_audit_event(
                session,
                org_id=principal.organisation_id,
                actor_user_id=principal.account_id,
                event_type="pipeline.node.revert_to_manual",
                resource_type="pipeline",
                resource_id=pipeline_id,
                payload_json={
                    "node_id": str(node_id),
                    "snapshot_id": str(snapshot_id),
                },
            )

            saved = await _save_locked_graph(
                session,
                pipeline_id=pipeline_id,
                org_id=principal.organisation_id,
                principal=principal,
                nodes=nodes,
                edges=edges,
            )
    except (HitlGateWeakeningDenied, GuardrailBindingStripDenied, ProgrammingError) as exc:
        await _finalize_locked_graph_save(exc, session, principal=principal, pipeline_id=pipeline_id)

    if saved is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_PIPELINE_NOT_FOUND)
    saved_nodes, saved_edges = saved
    return _graph_response(saved_nodes, saved_edges)


def _find_node_in_list(nodes: list[dict[str, Any]], node_id: uuid.UUID) -> dict[str, Any] | None:
    """Find a node dict by ID within a list of node dicts."""
    node_id_str = str(node_id)
    for n in nodes:
        raw_id = n.get("id")
        if raw_id is None:
            continue
        if (isinstance(raw_id, uuid.UUID) and raw_id == node_id) or str(raw_id) == node_id_str:
            return n
    return None


def _edge_to_dict(e: Any) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "source_node_id": str(e.source_node_id),
        "target_node_id": str(e.target_node_id),
        "edge_type": e.edge_type,
        "condition_expression": getattr(e, "condition_expression", None),
        "hitl_gate_config": dict(e.hitl_gate_config) if isinstance(e.hitl_gate_config, dict) else e.hitl_gate_config,
        "hitl_gate_config_present": True,
        "source_port": getattr(e, "source_port", "out"),
        "target_port": getattr(e, "target_port", "in"),
    }


async def _save_graph(
    session: AsyncSession,
    pipeline_id: uuid.UUID,
    org_id: uuid.UUID,
    nodes: list[dict[str, Any]],
    edges: list[Any],
    is_privileged: bool,
    caller_type: Literal["rest", "mcp"],
    account_id: uuid.UUID | None = None,
    is_guardrail_admin: bool = False,
) -> tuple[list[dict[str, Any]], list[Any]] | None:
    """Persist updated nodes + edges via replace_pipeline_graph.

    Accepts edges as either ORM model instances (PipelineEdge) or plain dicts.
    Forwards is_privileged + caller_type + account_id + is_guardrail_admin to
    the underlying graph write (ADR 017 backstop /
    hitl-gate-removal-guard-plan.md v19 / FAR-309 PR A review).
    """
    edge_dicts = [_edge_to_dict(e) if hasattr(e, "source_node_id") else dict(e) for e in edges]
    graph = await replace_pipeline_graph(
        session,
        pipeline_id=pipeline_id,
        org_id=org_id,
        nodes=nodes,
        edges=edge_dicts,
        is_privileged=is_privileged,
        caller_type=caller_type,
        account_id=account_id,
        is_guardrail_admin=is_guardrail_admin,
    )
    if graph is not None:
        # FAR-488a: node-conversion saves go through here too — keep the same
        # "what you save is what runs" Agent-row sync (a no-op when the node
        # commands already mirror the bound Agent rows).
        await _sync_agent_row_commands(session, org_id=org_id, nodes=nodes)
    return graph
