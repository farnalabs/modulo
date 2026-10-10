"""Eval management endpoints.

URLs:
    POST   /api/v1/evals              — create an eval definition (admin only)
    GET    /api/v1/runs/{run_id}/evals — list eval results for a run
    POST   /api/v1/evals/compare      — side-by-side comparison of two runs
    GET    /api/v1/evals/coverage     — eval coverage map for a pipeline
    POST   /api/v1/evals/from-run     — create eval definition from run data
    PUT    /api/v1/evals/suites/{suite_id}/alerting — configure regression alerting (admin only)
    POST   /api/v1/eval-datasets      — create an eval dataset (admin only)
    GET    /api/v1/eval-datasets      — list eval datasets
    GET    /api/v1/eval-datasets/{id} — get eval dataset (team-scoped)
    PATCH  /api/v1/eval-datasets/{id} — update eval dataset (team-scoped, admin only)
    DELETE /api/v1/eval-datasets/{id} — soft-delete eval dataset (team-scoped, admin only)
    POST   /api/v1/eval-suites        — create an eval suite (admin only)
    GET    /api/v1/eval-suites        — list eval suites
    GET    /api/v1/eval-suites/{id}   — get eval suite (team-scoped)
    PATCH  /api/v1/eval-suites/{id}   — update eval suite (team-scoped, admin only)
    DELETE /api/v1/eval-suites/{id}   — delete eval suite (team-scoped, admin only)
    POST   /api/v1/evals/{eval_id}/policy-gate       — create/replace a policy gate
    PUT    /api/v1/evals/{eval_id}/policy-gate       — update a policy gate's action
    DELETE /api/v1/evals/{eval_id}/policy-gate       — soft-delete a policy gate
    GET    /api/v1/evals/{eval_id}/policy-gate       — read a policy gate
    PATCH  /api/v1/evals/{eval_id}/policy-gate/toggle — enable/disable a policy gate
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import (
    MSG_DB_OPERATION_FAILED,
    MSG_FEATURE_NOT_AVAILABLE,
    MSG_PIPELINE_NOT_FOUND,
    MSG_RESOURCE_ALREADY_EXISTS,
)
from modulo.api.db_error_handling import handle_db_errors, raise_session_contract_error
from modulo.api.dependencies import (
    deny_break_glass_mint,
    get_db_session,
    require_permission,
    require_team_membership_or_admin,
)
from modulo.api.team_scope import (
    resolve_eval_dataset_team_scope,
    resolve_eval_suite_team_scope,
    validate_owner_team_for_create,
)
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.audit_coverage import audited
from modulo.core.audit_logger import append_audit_event
from modulo.core.eval_engine.author_warnings import check_author_warnings
from modulo.core.eval_engine.coverage_gap import (
    DEFAULT_DIVERGENCE_THRESHOLD,
    DEFAULT_MIN_RUNS,
    compute_coverage_gap,
)
from modulo.core.eval_engine.eval_definition_write import (
    create_or_update_eval,
    validate_guardrail_request,
)
from modulo.core.eval_engine.policy_gate import (
    PolicyGateBindingViolationError,
    validate_binding,
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
from modulo.db.crud.run_node_outputs import read_run_blobs
from modulo.db.models.eval import Eval
from modulo.db.models.eval_dataset import EvalDataset
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.eval_suite import EvalSuite
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.policy_gate import PolicyGate
from modulo.db.models.run import Run
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.db.soft_delete import include_soft_deleted
from modulo.db.sqlstates import sqlstate_of

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
_CODE_EVALS_POLICY_GATE_CREATE = "evals.policy_gate.create"
_CODE_EVALS_POLICY_GATE_UPDATE = "evals.policy_gate.update"
_CODE_EVALS_POLICY_GATE_DELETE = "evals.policy_gate.delete"
_CODE_EVALS_POLICY_GATE_GET = "evals.policy_gate.get"
_EVAL_TYPE_PATTERN = r"^(llm_judge|regex|json_schema|custom_function|guardrail|human_set)$"
_POLICY_GATE_ACTION_PATTERN = r"^(warn|block)$"
_MSG_EVAL_SUITE_NOT_FOUND = "Eval suite not found"
_MSG_POLICY_GATE_NOT_FOUND = "Policy gate not found for this eval"
_MSG_POLICY_GATE_CONFLICT = "A policy gate for this eval was created concurrently. Please retry."
_MSG_POLICY_GATE_LOCK_TIMEOUT = "Gate save is temporarily unavailable due to high contention. Please retry."
_CODE_EVAL_DEFINITION_UPDATE = "eval.definition.update"
_CODE_EVAL_DEFINITION_DELETE = "eval.definition.delete"
_CODE_EVAL_DEFINITION_CREATE = "eval.definition.create"
_VISIBILITY_PATTERN = "^(org|team)$"

_MSG_POLICY_GATE_CASCADE_CONFLICT = (
    "Cannot delete this eval: it has policy gate decision records. "
    "Remove the associated pipeline run(s) or wait for decision retention "
    "cleanup before deleting."
)
_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["evals"])


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class CreateEvalRequest(BaseModel):
    model_config = {"extra": "forbid"}

    pipeline_id: uuid.UUID
    node_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=255)
    eval_type: str = Field(pattern=_EVAL_TYPE_PATTERN)
    config_json: dict[str, Any] = Field(default_factory=dict)
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


def _eval_def_to_dict(
    eval_row: Eval,
    *,
    policy_gate: PolicyGate | None = None,
) -> dict[str, Any]:
    """Convert an ``Eval`` row (and optional ``PolicyGate``) to the response dict.

    Returns the public fields only; ``failure_behaviour`` is excluded from the
    response shape (it is an internal column, not exposed to callers).
    """
    return {
        "id": str(eval_row.id),
        "pipeline_id": str(eval_row.pipeline_id),
        "node_id": str(eval_row.node_id) if eval_row.node_id else None,
        "name": eval_row.name,
        "eval_type": eval_row.eval_type,
        "config_json": eval_row.config_json,
        "pass_threshold": float(eval_row.pass_threshold) if eval_row.pass_threshold is not None else None,
        "suite_id": eval_row.suite_id,
        "account_id": str(eval_row.account_id),
        "version": getattr(eval_row, "version", 1),
        "pre_version_raw": getattr(eval_row, "pre_version_raw", None),
    }


def _validate_guardrail_request(
    *,
    eval_type: str,
    config_json: dict[str, Any] | None,
) -> None:
    """Graph-save validation for guardrail definitions (FAR-208 item 5).

    Validates the config vocabulary and detection type, delegating to the
    consolidated validator in ``eval_definition_write``.
    """
    validate_guardrail_request(
        eval_type=eval_type,
        config_json=config_json,
    )


class UpdateEvalRequest(BaseModel):
    model_config = {"extra": "forbid"}

    node_id: uuid.UUID | None = None
    name: str | None = Field(None, min_length=1, max_length=255)
    eval_type: str | None = Field(None, pattern=_EVAL_TYPE_PATTERN)
    config_json: dict[str, Any] | None = None
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


# ---------------------------------------------------------------------------
# Policy Gate request / response schemas (FAR-1106, chunk 6)
# ---------------------------------------------------------------------------


class PolicyGateCreateRequest(BaseModel):
    """Request body for POST /api/v1/evals/{eval_id}/policy-gate."""

    model_config = {"extra": "forbid"}

    action: str = Field(pattern=_POLICY_GATE_ACTION_PATTERN)


class PolicyGateUpdateRequest(BaseModel):
    """Request body for PUT /api/v1/evals/{eval_id}/policy-gate."""

    model_config = {"extra": "forbid"}

    action: str = Field(pattern=_POLICY_GATE_ACTION_PATTERN)


class PolicyGateToggleRequest(BaseModel):
    """Request body for PATCH /api/v1/evals/{eval_id}/policy-gate/toggle.

    Toggles the operator safety control (CO-5).  Setting ``enabled``
    atomically stamps the corresponding timestamp so the symmetric CHECK
    constraint (ck_policy_gates_enabled_timestamps) is always satisfied.
    """

    model_config = {"extra": "forbid"}

    enabled: bool


class PolicyGateResponse(BaseModel):
    """Response body for GET /api/v1/evals/{eval_id}/policy-gate."""

    id: uuid.UUID
    eval_id: uuid.UUID
    action: str
    version: int
    pre_version_raw: dict[str, Any] | None = None
    warnings: list[dict[str, str]] = Field(default_factory=list)
    # FAR-967 operator safety control (CO-5): surfaced on every gate read.
    enabled: bool
    enabled_at: datetime | None = None
    disabled_at: datetime | None = None


# ---------------------------------------------------------------------------
# Policy Gate helpers — advisory lock and create-or-replace
# ---------------------------------------------------------------------------

_GATE_LOCK_TIMEOUT_SQL = "SET LOCAL statement_timeout = '5s'"
_GATE_ADVISORY_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext(:key))"
_GATE_LOCK_KEY_PREFIX = "policy_gate:"


async def _with_gate_advisory_lock[T](
    eval_id: uuid.UUID,
    session: AsyncSession,
    fn: Callable[..., Awaitable[T]],
) -> T:
    """Execute *fn* while holding a transaction-scoped advisory lock keyed on *eval_id*.

    The lock is released when the transaction commits or rolls back.

    PostgreSQL advisory locks are transaction-scoped when called inside a
    transaction.  The lock key is derived from *eval_id* via ``hashtext()``
    — the established pattern for string-keyed advisory locks in this
    codebase (see ``gate_coalescing.py:107``).

    ``SET LOCAL statement_timeout`` limits how long the session blocks on
    ``pg_advisory_xact_lock``.  5 seconds is long enough for normal
    acquisition (the lock is held only for the check-delete-insert sequence)
    and short enough to surface contention to the caller quickly.
    On timeout, PostgreSQL raises ``statement_timeout`` (SQLSTATE 57014)
    which surfaces as a 503 Service Unavailable.

    Generic over the wrapped callable's return type (FAR-967 F6): the
    toggle handler wraps its load-and-mutate under the SAME lock, so the
    helper must be able to return whatever the callback produces — not only
    a ``PolicyGate``.
    """
    lock_key = f"{_GATE_LOCK_KEY_PREFIX}{eval_id}"
    await session.execute(text(_GATE_LOCK_TIMEOUT_SQL))
    await session.execute(text(_GATE_ADVISORY_LOCK_SQL), {"key": lock_key})
    return await fn()


async def _create_or_replace_gate(
    eval_id: uuid.UUID,
    gate_fields: dict[str, Any],
    session: AsyncSession,
    principal: "TenantPrincipal",
) -> PolicyGate:
    """Create or replace a PolicyGate under an advisory lock.

    Re-checks for a live gate (the advisory lock serialises concurrent
    requests), soft-deletes any conflicting gate, then inserts the new one.
    A second ``UniqueViolation`` while holding the lock is a true race
    that could not be resolved → 409 Conflict.

    FAR-967 F6: when a live gate is replaced, the new row INHERITS its
    ``enabled``/``enabled_at``/``disabled_at`` state — a replace never
    silently re-enables a gate the operator disabled.
    """

    async def _do_insert() -> PolicyGate:
        # Re-check for a live gate (the advisory lock serialises concurrent requests)
        existing = await session.execute(
            select(PolicyGate).where(
                PolicyGate.eval_id == eval_id,
                PolicyGate.deleted_at.is_(None),
            )
        )
        live_gate = existing.scalar_one_or_none()
        now = datetime.now(UTC)
        if live_gate is not None:
            # Soft-delete the conflicting gate
            live_gate.deleted_at = func.now()
            live_gate.deleted_by = principal.account_id
            # FAR-967 F6: the replacement INHERITS the operator's enabled
            # state (and its timestamp pair).  A create-or-replace must
            # never silently re-enable a gate the operator deliberately
            # disabled — enforcement state is operator product state (CO-5),
            # not something a re-POST should reset.
            enabled = live_gate.enabled
            enabled_at = live_gate.enabled_at
            disabled_at = live_gate.disabled_at
            # Normalise so the symmetric CHECK constraint
            # (ck_policy_gates_enabled_timestamps) is satisfied on insert
            # even if the carried row somehow arrived with a missing stamp.
            if enabled and enabled_at is None:
                enabled_at = now
            if not enabled and disabled_at is None:
                disabled_at = now
        else:
            # Fresh create — enabled at creation time so the symmetric CHECK
            # constraint is satisfied at insert (CO-2).
            enabled = True
            enabled_at = now
            disabled_at = None
        new_gate = PolicyGate(
            organisation_id=principal.organisation_id,
            eval_id=eval_id,
            node_id=gate_fields["node_id"],
            action=gate_fields["action"],
            version=1,
            enabled=enabled,
            enabled_at=enabled_at,
            disabled_at=disabled_at,
        )
        session.add(new_gate)
        await session.flush()
        return new_gate

    return await _with_gate_advisory_lock(eval_id, session, _do_insert)


async def _load_eval_or_404(
    session: AsyncSession,
    eval_id: uuid.UUID,
    principal: TenantPrincipal,
) -> Eval:
    """Load an eval scoped to the principal's org, or raise 404.

    Shared by every policy-gate handler so the org-scoped lookup and its
    404 mapping are defined once (also keeps SonarCloud's copy-paste gate
    quiet: four identical inline blocks previously counted as new-code
    duplication).
    """
    result = await session.execute(
        select(Eval).where(
            Eval.id == eval_id,
            Eval.organisation_id == principal.organisation_id,
        )
    )
    eval_row = result.scalar_one_or_none()
    if eval_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_MSG_EVAL_DEFINITION_NOT_FOUND,
        )
    return eval_row


async def _load_live_gate_or_404(
    session: AsyncSession,
    eval_id: uuid.UUID,
    principal: TenantPrincipal,
) -> PolicyGate:
    """Load the live policy gate for an eval, or raise 404.

    Shared by the update / delete / read handlers (see ``_load_eval_or_404``
    for the duplication rationale).
    """
    result = await session.execute(
        select(PolicyGate).where(
            PolicyGate.eval_id == eval_id,
            PolicyGate.organisation_id == principal.organisation_id,
            PolicyGate.deleted_at.is_(None),
        )
    )
    gate = result.scalar_one_or_none()
    if gate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_MSG_POLICY_GATE_NOT_FOUND,
        )
    return gate


async def _collect_author_warnings(
    session: AsyncSession,
    principal: TenantPrincipal,
    eval_row: Eval,
    eval_id: uuid.UUID,
) -> list[dict[str, str]]:
    """Run the FAR-957 §3.2 author-warning checks for an eval's evidence keys.

    Advisory only — the checks never block binding. Only ``evidence_key`` is
    recognised (top-level or under ``detection``); a bare ``key`` is too
    ambiguous and would suppress genuine no_producer warnings. A check failure
    is logged and swallowed so telemetry/advisory trouble can never break the
    gate write.
    """
    ev_config = eval_row.config_json or {}
    evidence_key_candidates: set[str] = set()
    if "evidence_key" in ev_config:
        evidence_key_candidates.add(str(ev_config["evidence_key"]))
    detection = ev_config.get("detection")
    if isinstance(detection, dict) and "evidence_key" in detection:
        evidence_key_candidates.add(str(detection["evidence_key"]))

    author_warnings: list[dict[str, str]] = []
    for ek in evidence_key_candidates:
        try:
            warnings_list = await check_author_warnings(
                session,
                org_id=principal.organisation_id,
                pipeline_id=eval_row.pipeline_id,
                evidence_key=ek,
                binding_node_id=eval_row.node_id,
            )
            author_warnings.extend(w.to_dict() for w in warnings_list)
        except Exception:
            _log.warning(
                "policy_gate.author_warnings_check_failed",
                extra={
                    "org_id": str(principal.organisation_id),
                    "eval_id": str(eval_id),
                    "evidence_key": ek,
                },
                exc_info=True,
            )
    return author_warnings


# ---------------------------------------------------------------------------
# Policy Gate endpoints (FAR-1106, chunk 6)
# ---------------------------------------------------------------------------


@router.post(
    "/evals/{eval_id}/policy-gate",
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(audited("policy_gate_created", "policy_gate", principal_dep=get_current_tenant_user)),
        Depends(deny_break_glass_mint),
    ],
    responses={
        400: {"description": "Bad Request — binding validation failed"},
        404: {"description": "Eval not found"},
        409: {"description": "Conflict — concurrent gate creation"},
        503: {"description": "Service Unavailable — lock timeout"},
    },
)
@handle_db_errors(_CODE_EVALS_POLICY_GATE_CREATE)
async def create_policy_gate(
    eval_id: uuid.UUID,
    req: PolicyGateCreateRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_DEFINITION_UPDATE),
) -> PolicyGateResponse:
    """Create a PolicyGate for an eval (admin only).

    The gate inherits the eval's edit permission — no separate permission
    check is performed (criterion 14).

    ``validate_binding`` is called to verify the gate-to-eval binding is
    valid (cross-tenancy, guardrail-typed, suite-scoped, node_id mismatch).
    Violations are logged at WARNING with structured context but the caller
    receives a generic 400 -- never the violation list or org identifiers
    (criteria 5-9).

    Concurrent creates are serialised via a transaction-scoped advisory lock
    (section 4.4/5).  A ``UniqueViolation`` while holding the lock → 409.
    A lock-acquisition timeout (SQLSTATE 57014) → 503.
    """
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can create policy gates",
        )

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            # Load the eval to verify it exists and belongs to this org
            eval_row = await _load_eval_or_404(session, eval_id, principal)

            # Validate binding (cross-tenancy, guardrail, suite-scoped, node_id mismatch)
            pg_fields = {
                "id": uuid.uuid4(),  # placeholder — real id assigned on insert
                "organisation_id": principal.organisation_id,
                "node_id": eval_row.node_id,
            }
            ev_fields = {
                "id": eval_row.id,
                "organisation_id": eval_row.organisation_id,
                "node_id": eval_row.node_id,
                "eval_type": eval_row.eval_type,
            }
            try:
                validate_binding(pg_fields, ev_fields)
            except PolicyGateBindingViolationError as exc:
                _log.warning(
                    "Policy gate binding violation",
                    extra={"violations": exc.violations},
                )
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Policy gate binding is invalid. Check the eval configuration.",
                ) from exc

            # Author-warning checks (FAR-957 §3.2): advisory only, never
            # blocks binding.
            author_warnings = await _collect_author_warnings(session, principal, eval_row, eval_id)

            gate_fields = {
                "action": req.action,
                "node_id": eval_row.node_id,
            }

            try:
                gate = await _create_or_replace_gate(eval_id, gate_fields, session, principal)
            except IntegrityError:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=_MSG_POLICY_GATE_CONFLICT,
                ) from None

            # Audit log (best-effort — a failed audit never blocks the create)
            try:
                await append_audit_event(
                    session,
                    org_id=principal.organisation_id,
                    event_type="policy_gate.created",
                    actor_user_id=principal.account_id,
                    resource_type="policy_gate",
                    resource_id=gate.id,
                    payload_json={
                        "eval_id": str(eval_id),
                        "action": gate.action,
                        "version": gate.version,
                        # FAR-967 F6: enforcement state is part of what the
                        # event records — an auditor must see whether the
                        # gate this event created/replaced is active.
                        "enabled": gate.enabled,
                    },
                )
            except Exception:
                _log.exception(
                    "policy_gate.create_audit_failed",
                    extra={
                        "org_id": str(principal.organisation_id),
                        "eval_id": str(eval_id),
                    },
                )
    except HTTPException:
        raise
    except ProgrammingError:
        _log.exception(_CODE_EVALS_POLICY_GATE_CREATE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals.create_policy_gate")
        # Detect lock-acquisition timeout (SQLSTATE 57014)
        if sqlstate_of(exc) == "57014":
            _log.warning(
                "policy_gate.create_lock_timeout",
                extra={"org_id": str(principal.organisation_id), "eval_id": str(eval_id)},
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=_MSG_POLICY_GATE_LOCK_TIMEOUT,
            ) from None
        _log.exception(_CODE_EVALS_POLICY_GATE_CREATE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except Exception:
        _log.exception(
            "policy_gate.create_error",
            extra={"org_id": str(principal.organisation_id), "eval_id": str(eval_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while creating the policy gate.",
        ) from None

    return PolicyGateResponse(
        id=gate.id,
        eval_id=gate.eval_id,
        action=gate.action,
        version=gate.version,
        pre_version_raw=gate.pre_version_raw,
        warnings=author_warnings,
        enabled=gate.enabled,
        enabled_at=gate.enabled_at,
        disabled_at=gate.disabled_at,
    )


@router.put(
    "/evals/{eval_id}/policy-gate",
    dependencies=[
        Depends(audited("policy_gate_updated", "policy_gate", principal_dep=get_current_tenant_user)),
        Depends(deny_break_glass_mint),
    ],
    responses={
        400: {"description": "Bad Request — binding validation failed"},
        404: {"description": "Policy gate not found"},
        409: {"description": "Conflict — concurrent gate update"},
        503: {"description": "Service Unavailable — lock timeout"},
    },
)
@handle_db_errors(_CODE_EVALS_POLICY_GATE_UPDATE)
async def update_policy_gate(
    eval_id: uuid.UUID,
    req: PolicyGateUpdateRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_DEFINITION_UPDATE),
) -> PolicyGateResponse:
    """Update a PolicyGate's action (admin only).

    The gate inherits the eval's edit permission — no separate permission
    check is performed (criterion 14).

    Version increments on each edit (1→2→3).  ``pre_version_raw`` captures
    ALL currently-mutable fields as ``{"action": <value>}`` — the snapshot
    key set equals the set of mutable fields (criteria 17/18).

    ``validate_binding`` is called to verify the gate-to-eval binding is
    still valid after the update.  Violations are logged at WARNING with
    structured context but the caller receives a generic 400 (criteria 5-9).
    """
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can update policy gates",
        )

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            # Load the eval and the existing live gate (404 when either is missing)
            eval_row = await _load_eval_or_404(session, eval_id, principal)
            gate = await _load_live_gate_or_404(session, eval_id, principal)

            # Validate binding (cross-tenancy, guardrail, suite-scoped, node_id mismatch)
            pg_fields = {
                "id": gate.id,
                "organisation_id": principal.organisation_id,
                "node_id": gate.node_id,
            }
            ev_fields = {
                "id": eval_row.id,
                "organisation_id": eval_row.organisation_id,
                "node_id": eval_row.node_id,
                "eval_type": eval_row.eval_type,
            }
            try:
                validate_binding(pg_fields, ev_fields)
            except PolicyGateBindingViolationError as exc:
                _log.warning(
                    "Policy gate binding violation",
                    extra={"violations": exc.violations},
                )
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Policy gate binding is invalid. Check the eval configuration.",
                ) from exc

            # Author-warning checks (FAR-957 §3.2): advisory only, never
            # blocks binding.  Wired into the update path (F4) because a
            # re-bind to a newly-racy key must surface the same advisory
            # warnings as a fresh create.
            author_warnings = await _collect_author_warnings(session, principal, eval_row, eval_id)

            # Snapshot pre_version_raw: KEY SET must equal the set of mutable
            # fields (currently `action` only) — not a hardcoded list (criteria 17/18).
            mutable_fields = {"action"}
            snapshot = {field: getattr(gate, field) for field in mutable_fields}
            gate.pre_version_raw = snapshot
            gate.version = (gate.version or 1) + 1
            gate.action = req.action

            # Audit log (best-effort)
            try:
                await append_audit_event(
                    session,
                    org_id=principal.organisation_id,
                    event_type="policy_gate.updated",
                    actor_user_id=principal.account_id,
                    resource_type="policy_gate",
                    resource_id=gate.id,
                    payload_json={
                        "eval_id": str(eval_id),
                        "action": gate.action,
                        "version": gate.version,
                        "pre_version_raw": snapshot,
                        # FAR-967 F6: record whether the gate this event
                        # updated is actively enforcing (see create event).
                        "enabled": gate.enabled,
                    },
                )
            except Exception:
                _log.exception(
                    "policy_gate.update_audit_failed",
                    extra={
                        "org_id": str(principal.organisation_id),
                        "eval_id": str(eval_id),
                    },
                )
    except HTTPException:
        raise
    except ProgrammingError:
        _log.exception(_CODE_EVALS_POLICY_GATE_UPDATE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals.update_policy_gate")
        _log.exception(_CODE_EVALS_POLICY_GATE_UPDATE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except Exception:
        _log.exception(
            "policy_gate.update_error",
            extra={"org_id": str(principal.organisation_id), "eval_id": str(eval_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while updating the policy gate.",
        ) from None

    return PolicyGateResponse(
        id=gate.id,
        eval_id=gate.eval_id,
        action=gate.action,
        version=gate.version,
        pre_version_raw=gate.pre_version_raw,
        warnings=author_warnings,
        enabled=gate.enabled,
        enabled_at=gate.enabled_at,
        disabled_at=gate.disabled_at,
    )


@router.delete(
    "/evals/{eval_id}/policy-gate",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        Depends(
            audited("policy_gate_deleted", "policy_gate", principal_dep=get_current_tenant_user, fail_closed=True),
            scope="function",  # NOSONAR python:S930 - valid FastAPI Depends() kwarg; bundled signature is stale
        ),
        Depends(deny_break_glass_mint),
    ],
    responses={
        404: {"description": "Policy gate not found"},
        409: {"description": "Conflict — gate has decision records"},
    },
)
@handle_db_errors(_CODE_EVALS_POLICY_GATE_DELETE)
async def delete_policy_gate(
    eval_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_DEFINITION_DELETE),
) -> None:
    """Soft-delete a PolicyGate (admin only).

    The gate inherits the eval's edit permission — no separate permission
    check is performed (criterion 14).

    Sets ``deleted_at`` / ``deleted_by`` on the gate row.  The row remains
    (soft-deleted) but is no longer live.

    If the gate has existing ``PolicyGateDecision`` rows, the FK RESTRICT
    raises ``IntegrityError`` → mapped to a typed 409 naming the
    remediation (criterion 4a / §4.3a).
    """
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can delete policy gates",
        )

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            # Load the eval and the existing live gate (404 when either is missing)
            await _load_eval_or_404(session, eval_id, principal)
            gate = await _load_live_gate_or_404(session, eval_id, principal)

            gate_id = gate.id
            gate_action = gate.action

            # Soft-delete the gate
            gate.deleted_at = func.now()
            gate.deleted_by = principal.account_id

            # Audit log (best-effort)
            try:
                await append_audit_event(
                    session,
                    org_id=principal.organisation_id,
                    event_type="policy_gate.deleted",
                    actor_user_id=principal.account_id,
                    resource_type="policy_gate",
                    resource_id=gate_id,
                    payload_json={
                        "eval_id": str(eval_id),
                        "action": gate_action,
                    },
                )
            except Exception:
                _log.exception(
                    "policy_gate.delete_audit_failed",
                    extra={
                        "org_id": str(principal.organisation_id),
                        "eval_id": str(eval_id),
                    },
                )
    except HTTPException:
        raise
    except IntegrityError:
        # FK RESTRICT on PolicyGateDecision rows — gate has decision records
        _log.exception(_CODE_EVALS_POLICY_GATE_DELETE)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_MSG_POLICY_GATE_CASCADE_CONFLICT,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_EVALS_POLICY_GATE_DELETE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals.delete_policy_gate")
        _log.exception(_CODE_EVALS_POLICY_GATE_DELETE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except Exception:
        _log.exception(
            "policy_gate.delete_error",
            extra={"org_id": str(principal.organisation_id), "eval_id": str(eval_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while deleting the policy gate.",
        ) from None


# ---------------------------------------------------------------------------
# Policy Gate toggle endpoint (FAR-967, CO-5 / CO-3)
# ---------------------------------------------------------------------------

_CODE_EVALS_POLICY_GATE_TOGGLE = "evals.policy_gate.toggle"
_MSG_POLICY_GATE_TOGGLE_CHECK_VIOLATION = (
    "Toggle would violate the enabled/disabled timestamp constraint. This is a server-side bug — please report it."
)


@router.patch(
    "/evals/{eval_id}/policy-gate/toggle",
    # FAR-967 F2: a break-glass principal must not be able to flip
    # enforcement — the toggle carries the same mint deny as create /
    # update / delete.
    dependencies=[
        Depends(
            audited("policy_gate_toggled", "policy_gate", principal_dep=get_current_tenant_user, fail_closed=True),
            scope="function",  # NOSONAR python:S930 - valid FastAPI Depends() kwarg; bundled signature is stale
        ),
        Depends(deny_break_glass_mint),
    ],
    responses={
        404: {"description": "Policy gate not found"},
        503: {"description": "Service Unavailable — lock timeout"},
        500: {"description": "Internal Server Error"},
    },
)
@handle_db_errors(_CODE_EVALS_POLICY_GATE_TOGGLE)
async def toggle_policy_gate(
    eval_id: uuid.UUID,
    req: PolicyGateToggleRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_DEFINITION_UPDATE),
) -> PolicyGateResponse:
    """Enable or disable a PolicyGate (admin only).

    Toggles the ``enabled`` boolean and atomically stamps the corresponding
    timestamp so the symmetric CHECK constraint is always satisfied:

    - ``enabled=true``  → ``enabled_at=now(), disabled_at=NULL``
    - ``enabled=false`` → ``disabled_at=now(), enabled_at=NULL``

    The gate inherits the eval's edit permission (OQ-3).  ``enabled`` is
    product state toggled via API/UI only — NEVER declarative / config-as-code
    (CO-3).

    The load-and-mutate runs under the same transaction-scoped advisory lock
    as create-or-replace (FAR-967 F6) so a concurrent replace cannot
    interleave with this read-modify-write.  A lock-acquisition timeout
    (SQLSTATE 57014) → 503.

    An audit-log entry is emitted (best-effort, same path as create/update).
    """
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can toggle policy gates",
        )

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            # Load eval (404 when missing); the gate load happens under the
            # advisory lock below so it serialises against a concurrent
            # create-or-replace (FAR-967 F6).
            await _load_eval_or_404(session, eval_id, principal)

            now = datetime.now(UTC)

            async def _do_toggle() -> tuple[PolicyGate, bool]:
                gate = await _load_live_gate_or_404(session, eval_id, principal)
                pre_enabled = gate.enabled

                # Atomic toggle with timestamp stamping.
                # The CHECK constraint will reject invalid states at the DB layer;
                # we set the fields correctly so that never fires.
                if req.enabled:
                    gate.enabled = True
                    gate.enabled_at = now
                    gate.disabled_at = None
                else:
                    gate.enabled = False
                    gate.disabled_at = now
                    gate.enabled_at = None
                return gate, pre_enabled

            gate, pre_enabled = await _with_gate_advisory_lock(eval_id, session, _do_toggle)

            # Audit log (best-effort — a failed audit never blocks the toggle)
            try:
                await append_audit_event(
                    session,
                    org_id=principal.organisation_id,
                    event_type="policy_gate.toggled",
                    actor_user_id=principal.account_id,
                    resource_type="policy_gate",
                    resource_id=gate.id,
                    payload_json={
                        "eval_id": str(eval_id),
                        "enabled": req.enabled,
                        "pre_enabled": pre_enabled,
                        "action": gate.action,
                    },
                )
            except Exception:
                _log.exception(
                    "policy_gate.toggle_audit_failed",
                    extra={
                        "org_id": str(principal.organisation_id),
                        "eval_id": str(eval_id),
                    },
                )
    except HTTPException:
        raise
    except ProgrammingError:
        _log.exception(_CODE_EVALS_POLICY_GATE_TOGGLE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals.toggle_policy_gate")
        # Lock-acquisition timeout (SQLSTATE 57014) — same mapping as create.
        if sqlstate_of(exc) == "57014":
            _log.warning(
                "policy_gate.toggle_lock_timeout",
                extra={"org_id": str(principal.organisation_id), "eval_id": str(eval_id)},
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=_MSG_POLICY_GATE_LOCK_TIMEOUT,
            ) from None
        # Detect CHECK-constraint violation (symmetric timestamp invariant)
        if sqlstate_of(exc) == "23514":
            _log.warning(
                "policy_gate.toggle_check_violation",
                extra={
                    "org_id": str(principal.organisation_id),
                    "eval_id": str(eval_id),
                    "enabled": req.enabled,
                },
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=_MSG_POLICY_GATE_TOGGLE_CHECK_VIOLATION,
            ) from None
        _log.exception(_CODE_EVALS_POLICY_GATE_TOGGLE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except Exception:
        _log.exception(
            "policy_gate.toggle_error",
            extra={
                "org_id": str(principal.organisation_id),
                "eval_id": str(eval_id),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while toggling the policy gate.",
        ) from None

    return PolicyGateResponse(
        id=gate.id,
        eval_id=gate.eval_id,
        action=gate.action,
        version=gate.version,
        pre_version_raw=gate.pre_version_raw,
        enabled=gate.enabled,
        enabled_at=gate.enabled_at,
        disabled_at=gate.disabled_at,
    )


@router.get(
    "/evals/{eval_id}/policy-gate",
    responses={
        404: {"description": "Policy gate not found"},
    },
)
@handle_db_errors(_CODE_EVALS_POLICY_GATE_GET)
async def get_policy_gate(
    eval_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_LIST),
) -> PolicyGateResponse:
    """Read a PolicyGate for an eval.

    Returns the gate's action and version.  Returns 404 when no gate exists
    (criterion 13).  The gate inherits the eval's edit permission — no
    separate permission check is performed (criterion 14).
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            # Load the eval and the live gate (404 when either is missing)
            await _load_eval_or_404(session, eval_id, principal)
            gate = await _load_live_gate_or_404(session, eval_id, principal)
    except HTTPException:
        raise
    except ProgrammingError:
        _log.exception(_CODE_EVALS_POLICY_GATE_GET)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals.get_policy_gate")
        _log.exception(_CODE_EVALS_POLICY_GATE_GET)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None
    except Exception:
        _log.exception(
            "policy_gate.get_error",
            extra={"org_id": str(principal.organisation_id), "eval_id": str(eval_id)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while fetching the policy gate.",
        ) from None

    return PolicyGateResponse(
        id=gate.id,
        eval_id=gate.eval_id,
        action=gate.action,
        version=gate.version,
        pre_version_raw=gate.pre_version_raw,
        enabled=gate.enabled,
        enabled_at=gate.enabled_at,
        disabled_at=gate.disabled_at,
    )


@router.post(
    "/evals",
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(audited("eval_definition_created", "eval_definition", principal_dep=get_current_tenant_user)),
        Depends(deny_break_glass_mint),
    ],
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
    principal: TenantPrincipal = require_permission(_CODE_EVAL_DEFINITION_CREATE),
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
                raise HTTPException(status_code=404, detail=MSG_PIPELINE_NOT_FOUND)

            try:
                eval_row = await create_or_update_eval(
                    session,
                    org_id=principal.organisation_id,
                    account_id=principal.account_id,
                    pipeline_id=req.pipeline_id,
                    node_id=req.node_id,
                    name=req.name,
                    eval_type=req.eval_type,
                    config_json=req.config_json,
                    failure_behaviour="warn",
                    pass_threshold=req.pass_threshold,
                    suite_id=req.suite_id,
                )
            except PolicyGateBindingViolationError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"PolicyGate binding violation: {exc}",
                ) from exc
            # Map Eval row to the legacy response shape
            eval_def = _eval_def_to_dict(eval_row)
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
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals.create_eval_definition")
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

    return eval_def


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
    """List eval definitions for the caller's organisation.

    Reads from the ``evals`` table.  The ``failure_behaviour`` field is not
    returned in the response — it is an internal column used by the pipeline
    engine only.
    """
    from sqlalchemy import func as sa_func

    conditions = [
        Eval.organisation_id == principal.organisation_id,
        Eval.deleted_at.is_(None),
    ]
    if pipeline_id:
        conditions.append(Eval.pipeline_id == pipeline_id)
    if eval_type:
        conditions.append(Eval.eval_type == eval_type)

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            total_q = select(sa_func.count(Eval.id)).where(*conditions)
            total = (await session.execute(total_q)).scalar() or 0

            q = (
                select(Eval)
                .where(*conditions)
                .order_by(Eval.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            rows = (await session.execute(q)).scalars().all()

            # Batch-load PolicyGates for the page (one query, not N+1).
            gate_map: dict[uuid.UUID, PolicyGate] = {}
            if rows:
                eval_ids = [r.id for r in rows]
                gates = (
                    (
                        await session.execute(
                            select(PolicyGate).where(
                                PolicyGate.eval_id.in_(eval_ids),
                                PolicyGate.organisation_id == principal.organisation_id,
                                PolicyGate.deleted_at.is_(None),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                gate_map = {g.eval_id: g for g in gates}
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
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals.list_eval_definitions")
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
        items=[EvalDefinitionResponse(**_eval_def_to_dict(d, policy_gate=gate_map.get(d.id))) for d in rows],
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
                raise HTTPException(status_code=404, detail=MSG_PIPELINE_NOT_FOUND)

            nodes_raw = pipeline.graph_nodes_json or []
            node_ids = [str(n.get("id")) for n in nodes_raw if n.get("id")]

            eval_defs_rows = (
                (
                    await session.execute(
                        select(EvalDefinition).where(
                            EvalDefinition.pipeline_id == pipeline_id,
                            EvalDefinition.organisation_id == principal.organisation_id,
                            EvalDefinition.node_id.in_([uuid.UUID(nid) for nid in node_ids if nid]),
                            EvalDefinition.deleted_at.is_(None),
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
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals.eval_coverage")
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
        nid = str(n.get("id") or "")
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
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals.eval_leaderboard")
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
                    include_soft_deleted(
                        select(EvalDefinition).where(
                            EvalDefinition.id == eval_id,
                            EvalDefinition.organisation_id == principal.organisation_id,
                        )
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
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals.eval_timeseries")
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
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals.eval_coverage_gap")
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
    dependencies=[Depends(audited("eval_suite_alerting_updated", "eval_suite", principal_dep=get_current_tenant_user))],
)
@handle_db_errors(_CODE_EVALS_SUITE_ALERTING)
async def update_suite_alerting(
    suite_id: uuid.UUID,
    req: EvalSuiteAlertingRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_DEFINITION_UPDATE),
    _: TenantPrincipal = require_team_membership_or_admin(resolve_eval_suite_team_scope),
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
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals.update_suite_alerting")
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


# ---------------------------------------------------------------------------
# Eval Dataset CRUD (FAR-947: team-scope gates)
# ---------------------------------------------------------------------------


class CreateEvalDatasetRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    owner_team_id: uuid.UUID | None = None
    visibility: str = Field(default="org", pattern=_VISIBILITY_PATTERN)


class UpdateEvalDatasetRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    owner_team_id: uuid.UUID | None = None
    visibility: str | None = Field(None, pattern=_VISIBILITY_PATTERN)


class EvalDatasetResponse(BaseModel):
    id: uuid.UUID
    name: str
    version: int
    owner_team_id: uuid.UUID | None = None
    visibility: str = "org"
    organisation_id: uuid.UUID
    created_at: Any = None
    updated_at: Any = None


class EvalDatasetListResponse(BaseModel):
    items: list[EvalDatasetResponse]
    total: int
    page: int
    page_size: int


def _eval_dataset_response(dataset: EvalDataset) -> EvalDatasetResponse:
    """Serialise an :class:`EvalDataset` row for API responses."""
    return EvalDatasetResponse(
        id=dataset.id,
        name=dataset.name,
        version=dataset.version,
        owner_team_id=dataset.owner_team_id,
        visibility=dataset.visibility,
        organisation_id=dataset.organisation_id,
        created_at=_iso_or_none(dataset.created_at),
        updated_at=_iso_or_none(dataset.updated_at),
    )


_CODE_EVAL_DATASETS_CREATE = "evals.create_eval_dataset"
_CODE_EVAL_DATASETS_LIST = "evals.list_eval_datasets"
_CODE_EVAL_DATASETS_GET = "evals.get_eval_dataset"
_CODE_EVAL_DATASETS_UPDATE = "evals.update_eval_dataset"
_CODE_EVAL_DATASETS_DELETE = "evals.delete_eval_dataset"
_MSG_EVAL_DATASET_NOT_FOUND = "Eval dataset not found"


@router.post(
    "/eval-datasets",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(audited("eval_dataset_created", "eval_dataset", principal_dep=get_current_tenant_user))],
)
@handle_db_errors(_CODE_EVAL_DATASETS_CREATE)
async def create_eval_dataset(
    req: CreateEvalDatasetRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_DEFINITION_CREATE),
) -> EvalDatasetResponse:
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can create eval datasets",
        )
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            await validate_owner_team_for_create(session, principal, req.owner_team_id)
            dataset = EvalDataset(
                organisation_id=principal.organisation_id,
                name=req.name,
                owner_team_id=req.owner_team_id,
                visibility=req.visibility,
                version=1,
            )
            session.add(dataset)
            await session.flush()
    except HTTPException:
        raise
    except IntegrityError:
        _log.exception(_CODE_EVAL_DATASETS_CREATE)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Eval dataset name already exists in this organisation.",
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals.create_eval_dataset")
        _log.exception(_CODE_EVAL_DATASETS_CREATE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None

    return _eval_dataset_response(dataset)


@router.get("/eval-datasets")
@handle_db_errors(_CODE_EVAL_DATASETS_LIST)
async def list_eval_datasets(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_LIST),
) -> EvalDatasetListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            from sqlalchemy import func as sa_func

            total_q = select(sa_func.count(EvalDataset.id)).where(
                EvalDataset.organisation_id == principal.organisation_id,
                EvalDataset.deleted_at.is_(None),
            )
            total = (await session.execute(total_q)).scalar() or 0

            q = (
                select(EvalDataset)
                .where(
                    EvalDataset.organisation_id == principal.organisation_id,
                    EvalDataset.deleted_at.is_(None),
                )
                .order_by(EvalDataset.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            rows = (await session.execute(q)).scalars().all()
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals.list_eval_datasets")
        _log.exception(_CODE_EVAL_DATASETS_LIST)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None

    return EvalDatasetListResponse(
        items=[_eval_dataset_response(d) for d in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/eval-datasets/{dataset_id}")
@handle_db_errors(_CODE_EVAL_DATASETS_GET)
async def get_eval_dataset(
    dataset_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_LIST),
    _: TenantPrincipal = require_team_membership_or_admin(resolve_eval_dataset_team_scope),
) -> EvalDatasetResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await session.execute(
                select(EvalDataset).where(
                    EvalDataset.id == dataset_id,
                    EvalDataset.organisation_id == principal.organisation_id,
                    EvalDataset.deleted_at.is_(None),
                )
            )
            dataset = result.scalar_one_or_none()
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals.get_eval_dataset")
        _log.exception(_CODE_EVAL_DATASETS_GET)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None

    if dataset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_EVAL_DATASET_NOT_FOUND)
    return _eval_dataset_response(dataset)


@router.patch(
    "/eval-datasets/{dataset_id}",
    dependencies=[Depends(audited("eval_dataset_updated", "eval_dataset", principal_dep=get_current_tenant_user))],
)
@handle_db_errors(_CODE_EVAL_DATASETS_UPDATE)
async def update_eval_dataset(
    dataset_id: uuid.UUID,
    req: UpdateEvalDatasetRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_DEFINITION_UPDATE),
    _: TenantPrincipal = require_team_membership_or_admin(resolve_eval_dataset_team_scope),
) -> EvalDatasetResponse:
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can update eval datasets",
        )
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await session.execute(
                select(EvalDataset).where(
                    EvalDataset.id == dataset_id,
                    EvalDataset.organisation_id == principal.organisation_id,
                    EvalDataset.deleted_at.is_(None),
                )
            )
            dataset = result.scalar_one_or_none()
            if dataset is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_EVAL_DATASET_NOT_FOUND)

            updates = req.model_dump(exclude_unset=True)
            for key, value in updates.items():
                setattr(dataset, key, value)
            await session.flush()
    except HTTPException:
        raise
    except IntegrityError:
        _log.exception(_CODE_EVAL_DATASETS_UPDATE)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Eval dataset name already exists in this organisation.",
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals.update_eval_dataset")
        _log.exception(_CODE_EVAL_DATASETS_UPDATE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None

    return _eval_dataset_response(dataset)


@router.delete(
    "/eval-datasets/{dataset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        Depends(
            audited("eval_dataset_deleted", "eval_dataset", principal_dep=get_current_tenant_user, fail_closed=True),
            scope="function",  # NOSONAR python:S930 - valid FastAPI Depends() kwarg; bundled signature is stale
        )
    ],
)
@handle_db_errors(_CODE_EVAL_DATASETS_DELETE)
async def delete_eval_dataset(
    dataset_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_DEFINITION_DELETE),
    _: TenantPrincipal = require_team_membership_or_admin(resolve_eval_dataset_team_scope),
) -> None:
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can delete eval datasets",
        )
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await session.execute(
                select(EvalDataset).where(
                    EvalDataset.id == dataset_id,
                    EvalDataset.organisation_id == principal.organisation_id,
                    EvalDataset.deleted_at.is_(None),
                )
            )
            dataset = result.scalar_one_or_none()
            if dataset is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_EVAL_DATASET_NOT_FOUND)
            dataset.deleted_at = datetime.now(UTC)
            dataset.deleted_by = principal.account_id
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals.delete_eval_dataset")
        _log.exception(_CODE_EVAL_DATASETS_DELETE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None


# ---------------------------------------------------------------------------
# Eval Suite CRUD (FAR-947: team-scope gates)
# ---------------------------------------------------------------------------


class CreateEvalSuiteRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    owner_team_id: uuid.UUID | None = None
    visibility: str = Field(default="org", pattern=_VISIBILITY_PATTERN)


class UpdateEvalSuiteRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    owner_team_id: uuid.UUID | None = None
    visibility: str | None = Field(None, pattern=_VISIBILITY_PATTERN)


class EvalSuiteResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    version: int = 1
    owner_team_id: uuid.UUID | None = None
    visibility: str = "org"
    organisation_id: uuid.UUID
    eval_definition_ids: list[uuid.UUID] = Field(default_factory=list)
    created_at: Any = None
    updated_at: Any = None


class EvalSuiteListResponse(BaseModel):
    items: list[EvalSuiteResponse]
    total: int
    page: int
    page_size: int


def _eval_suite_response(suite: EvalSuite) -> EvalSuiteResponse:
    """Serialise an :class:`EvalSuite` row for API responses."""
    return EvalSuiteResponse(
        id=suite.id,
        name=suite.name,
        description=suite.description,
        version=suite.version,
        owner_team_id=suite.owner_team_id,
        visibility=suite.visibility,
        organisation_id=suite.organisation_id,
        eval_definition_ids=suite.eval_definition_ids or [],
        created_at=_iso_or_none(suite.created_at),
        updated_at=_iso_or_none(suite.updated_at),
    )


_CODE_EVAL_SUITES_CREATE = "evals.create_eval_suite"
_CODE_EVAL_SUITES_LIST = "evals.list_eval_suites"
_CODE_EVAL_SUITES_GET = "evals.get_eval_suite"
_CODE_EVAL_SUITES_UPDATE = "evals.update_eval_suite"
_CODE_EVAL_SUITES_DELETE = "evals.delete_eval_suite"
_MSG_EVAL_SUITE_NOT_FOUND_DETAIL = "Eval suite not found"


@router.post(
    "/eval-suites",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(audited("eval_suite_created", "eval_suite", principal_dep=get_current_tenant_user))],
)
@handle_db_errors(_CODE_EVAL_SUITES_CREATE)
async def create_eval_suite(
    req: CreateEvalSuiteRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_DEFINITION_CREATE),
) -> EvalSuiteResponse:
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can create eval suites",
        )
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            await validate_owner_team_for_create(session, principal, req.owner_team_id)
            suite = EvalSuite(
                organisation_id=principal.organisation_id,
                name=req.name,
                description=req.description,
                owner_team_id=req.owner_team_id,
                visibility=req.visibility,
                version=1,
            )
            session.add(suite)
            await session.flush()
    except HTTPException:
        raise
    except IntegrityError:
        _log.exception(_CODE_EVAL_SUITES_CREATE)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Eval suite with this name already exists in this organisation.",
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals.create_eval_suite")
        _log.exception(_CODE_EVAL_SUITES_CREATE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None

    return _eval_suite_response(suite)


@router.get("/eval-suites")
@handle_db_errors(_CODE_EVAL_SUITES_LIST)
async def list_eval_suites(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_LIST),
) -> EvalSuiteListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            from sqlalchemy import func as sa_func

            total_q = select(sa_func.count(EvalSuite.id)).where(
                EvalSuite.organisation_id == principal.organisation_id,
            )
            total = (await session.execute(total_q)).scalar() or 0

            q = (
                select(EvalSuite)
                .where(EvalSuite.organisation_id == principal.organisation_id)
                .order_by(EvalSuite.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            rows = (await session.execute(q)).scalars().all()
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals.list_eval_suites")
        _log.exception(_CODE_EVAL_SUITES_LIST)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None

    return EvalSuiteListResponse(
        items=[_eval_suite_response(s) for s in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/eval-suites/{suite_id}")
@handle_db_errors(_CODE_EVAL_SUITES_GET)
async def get_eval_suite(
    suite_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_LIST),
    _: TenantPrincipal = require_team_membership_or_admin(resolve_eval_suite_team_scope),
) -> EvalSuiteResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await session.execute(
                select(EvalSuite).where(
                    EvalSuite.id == suite_id,
                    EvalSuite.organisation_id == principal.organisation_id,
                )
            )
            suite = result.scalar_one_or_none()
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals.get_eval_suite")
        _log.exception(_CODE_EVAL_SUITES_GET)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None

    if suite is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_EVAL_SUITE_NOT_FOUND_DETAIL)
    return _eval_suite_response(suite)


@router.patch(
    "/eval-suites/{suite_id}",
    dependencies=[Depends(audited("eval_suite_updated", "eval_suite", principal_dep=get_current_tenant_user))],
)
@handle_db_errors(_CODE_EVAL_SUITES_UPDATE)
async def update_eval_suite(
    suite_id: uuid.UUID,
    req: UpdateEvalSuiteRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_DEFINITION_UPDATE),
    _: TenantPrincipal = require_team_membership_or_admin(resolve_eval_suite_team_scope),
) -> EvalSuiteResponse:
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can update eval suites",
        )
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await session.execute(
                select(EvalSuite).where(
                    EvalSuite.id == suite_id,
                    EvalSuite.organisation_id == principal.organisation_id,
                )
            )
            suite = result.scalar_one_or_none()
            if suite is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_EVAL_SUITE_NOT_FOUND_DETAIL)

            updates = req.model_dump(exclude_unset=True)
            for key, value in updates.items():
                setattr(suite, key, value)
            await session.flush()
    except HTTPException:
        raise
    except IntegrityError:
        _log.exception(_CODE_EVAL_SUITES_UPDATE)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Eval suite with this name already exists in this organisation.",
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals.update_eval_suite")
        _log.exception(_CODE_EVAL_SUITES_UPDATE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None

    return _eval_suite_response(suite)


@router.delete(
    "/eval-suites/{suite_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        Depends(
            audited("eval_suite_deleted", "eval_suite", principal_dep=get_current_tenant_user, fail_closed=True),
            scope="function",  # NOSONAR python:S930 - valid FastAPI Depends() kwarg; bundled signature is stale
        )
    ],
)
@handle_db_errors(_CODE_EVAL_SUITES_DELETE)
async def delete_eval_suite(
    suite_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_DEFINITION_DELETE),
    _: TenantPrincipal = require_team_membership_or_admin(resolve_eval_suite_team_scope),
) -> None:
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can delete eval suites",
        )
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await session.execute(
                select(EvalSuite).where(
                    EvalSuite.id == suite_id,
                    EvalSuite.organisation_id == principal.organisation_id,
                )
            )
            suite = result.scalar_one_or_none()
            if suite is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_EVAL_SUITE_NOT_FOUND_DETAIL)
            await session.delete(suite)
    except HTTPException:
        raise
    except IntegrityError:
        _log.exception(_CODE_EVAL_SUITES_DELETE)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete eval suite: it is referenced by other resources.",
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals.delete_eval_suite")
        _log.exception(_CODE_EVAL_SUITES_DELETE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DB_OPERATION_FAILED,
        ) from None


@router.get("/evals/{eval_id}")
@handle_db_errors(_CODE_EVALS_GET_EVAL_DEFINITION)
async def get_eval_definition(
    eval_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_LIST),
) -> dict[str, Any]:
    """Get a single eval definition by ID.

    Reads from the ``evals`` table (chunk 3b cutover).  Includes
    soft-deleted rows for historical lookups.  The associated
    ``PolicyGate`` (if any) is loaded for the response mapping.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await session.execute(
                include_soft_deleted(
                    select(Eval).where(
                        Eval.id == eval_id,
                        Eval.organisation_id == principal.organisation_id,
                    )
                )
            )
            eval_row = result.scalar_one_or_none()

            # Load the associated PolicyGate (if any) for the response mapping.
            policy_gate: PolicyGate | None = None
            if eval_row is not None:
                gate_result = await session.execute(
                    include_soft_deleted(
                        select(PolicyGate).where(
                            PolicyGate.eval_id == eval_row.id,
                            PolicyGate.organisation_id == principal.organisation_id,
                        )
                    )
                )
                policy_gate = gate_result.scalar_one_or_none()
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
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals.get_eval_definition")
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
    if eval_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_EVAL_DEFINITION_NOT_FOUND)
    return _eval_def_to_dict(eval_row, policy_gate=policy_gate)


@router.put(
    "/evals/{eval_id}",
    dependencies=[
        Depends(audited("eval_definition_updated", "eval_definition", principal_dep=get_current_tenant_user)),
        Depends(deny_break_glass_mint),
    ],
)
@handle_db_errors(_CODE_EVALS_UPDATE_EVAL_DEFINITION)
async def update_eval_definition(
    eval_id: uuid.UUID,
    req: UpdateEvalRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_DEFINITION_UPDATE),
) -> dict[str, Any]:
    """Update an eval definition. Admin only.

    Reads from the ``evals`` table (chunk 3b cutover).  The update is
    persisted via ``create_or_update_eval`` which handles version stamping
    and PolicyGate management internally.
    """
    if principal.org_role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can update eval definitions")

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await session.execute(
                include_soft_deleted(
                    select(Eval).where(
                        Eval.id == eval_id,
                        Eval.organisation_id == principal.organisation_id,
                    )
                )
            )
            eval_row = result.scalar_one_or_none()
            if eval_row is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_EVAL_DEFINITION_NOT_FOUND)

            updates = req.model_dump(exclude_unset=True)
            new_type = updates.get("eval_type", eval_row.eval_type)

            # Resolve current failure_behaviour from the PolicyGate (if any),
            # falling back to "warn" for guardrail-typed / suite-scoped evals.
            # failure_behaviour was retired from the public surface (FAR-1103
            # chunk 5a) — always use the current gate value.
            current_gate_result = await session.execute(
                select(PolicyGate).where(
                    PolicyGate.eval_id == eval_row.id,
                    PolicyGate.organisation_id == principal.organisation_id,
                    PolicyGate.deleted_at.is_(None),
                )
            )
            current_gate = current_gate_result.scalar_one_or_none()
            current_failure_behaviour = current_gate.action if current_gate is not None else "warn"

            new_config = updates.get("config_json", eval_row.config_json)
            _validate_guardrail_request(
                eval_type=new_type,
                config_json=new_config,
            )
            # Redirect to Eval+PolicyGate via the shared helper — version
            # stamping and PolicyGate management are handled internally.
            try:
                eval_row = await create_or_update_eval(
                    session,
                    org_id=principal.organisation_id,
                    account_id=principal.account_id,
                    pipeline_id=eval_row.pipeline_id,
                    node_id=updates.get("node_id", eval_row.node_id),
                    name=updates.get("name") or eval_row.name or "",
                    eval_type=new_type,
                    config_json=new_config,
                    failure_behaviour=current_failure_behaviour,
                    pass_threshold=updates.get("pass_threshold", eval_row.pass_threshold),
                    suite_id=updates.get("suite_id", eval_row.suite_id),
                    eval_suite_id=updates.get("eval_suite_id", getattr(eval_row, "eval_suite_id", None)),
                    existing_eval_id=eval_row.id,
                )
            except PolicyGateBindingViolationError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"PolicyGate binding violation: {exc}",
                ) from exc

            # Reload the PolicyGate for the response mapping (it may have
            # been created/updated by the helper).
            gate_result = await session.execute(
                select(PolicyGate).where(
                    PolicyGate.eval_id == eval_row.id,
                    PolicyGate.organisation_id == principal.organisation_id,
                    PolicyGate.deleted_at.is_(None),
                )
            )
            policy_gate = gate_result.scalar_one_or_none()
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
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals.update_eval_definition")
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

    return _eval_def_to_dict(eval_row, policy_gate=policy_gate)


@router.delete(
    "/evals/{eval_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        Depends(
            audited(
                "eval_definition_deleted", "eval_definition", principal_dep=get_current_tenant_user, fail_closed=True
            ),
            scope="function",  # NOSONAR python:S930 - valid FastAPI Depends() kwarg; bundled signature is stale
        ),
        Depends(deny_break_glass_mint),
    ],
)
@handle_db_errors(_CODE_EVALS_DELETE_EVAL_DEFINITION)
async def delete_eval_definition(
    eval_id: uuid.UUID,
    purge: bool = Query(False, description="Hard-remove a soft-deleted guardrail eval definition (step 2)"),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_DEFINITION_DELETE),
) -> None:
    """Delete an eval definition. Admin only.

    Reads from the ``evals`` table (chunk 3b cutover). Two-step soft-delete
    (FAR-309 PR B): a guardrail eval is SOFT-deleted (``deleted_at`` /
    ``deleted_by`` stamped on ``Eval`` and its live ``PolicyGate``, if any)
    instead of hard-removed. A second admin step (``?purge=true``) hard-removes
    soft-deleted rows — the ``PolicyGate`` cascades via ``ON DELETE CASCADE``,
    but ``PolicyGateDecision`` rows block hard-delete via RESTRICT (mapped to
    409). Non-guardrail evals keep their existing hard delete. Every
    soft-delete and purge writes an org-scoped audit event (best-effort
    fail-open-with-log — a failed audit never rolls back the delete).
    """
    if principal.org_role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can delete eval definitions")

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await session.execute(
                include_soft_deleted(
                    select(Eval).where(
                        Eval.id == eval_id,
                        Eval.organisation_id == principal.organisation_id,
                    )
                )
            )
            eval_row = result.scalar_one_or_none()
            if eval_row is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_EVAL_DEFINITION_NOT_FOUND)
            # Capture identity BEFORE any mutation — a hard-deleted ORM
            # instance no longer exposes attributes.
            eval_id_str = str(eval_row.id)
            eval_name = eval_row.name
            is_guardrail = eval_row.eval_type == "guardrail"
            soft = is_guardrail and not purge

            if soft:
                now = datetime.now(UTC)
                eval_row.deleted_at = now
                eval_row.deleted_by = principal.account_id
                # Soft-delete the live PolicyGate (if any) in the same
                # transaction — no orphaned gate enforcing silently.
                gate_result = await session.execute(
                    select(PolicyGate).where(
                        PolicyGate.eval_id == eval_row.id,
                        PolicyGate.organisation_id == principal.organisation_id,
                        PolicyGate.deleted_at.is_(None),
                    )
                )
                gate = gate_result.scalar_one_or_none()
                if gate is not None:
                    gate.deleted_at = now
                    gate.deleted_by = principal.account_id
            else:
                # Hard-delete: PolicyGate cascades via ON DELETE CASCADE;
                # PolicyGateDecision rows block via RESTRICT (→ 409).
                await session.delete(eval_row)

            # The two-step soft-delete audit applies to guardrail rows only —
            # a non-guardrail eval keeps its hard delete (no audit event).
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
            detail="Cannot delete eval: existing decision rows prevent removal (RESTRICT)",
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_EVALS_DELETE_EVAL_DEFINITION)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals.delete_eval_definition")
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
    node_id: uuid.UUID | None = Query(None),
    eval_id: uuid.UUID | None = Query(None),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVAL_LIST),
) -> dict[str, Any]:
    """List all eval results for a given run.

    Returns a paginated list of eval results with the eval definition name
    included for convenience. Requires the run to belong to the caller's
    organisation.

    Optional query parameters ``node_id`` and ``eval_id`` filter results
    by the evaluation's target node or eval definition respectively. When
    omitted the response is identical to the unfiltered call.
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

            base_filters = [
                EvalResult.run_id == run_id,
                EvalResult.organisation_id == principal.organisation_id,
                non_guardrail_eval_results_clause(),
            ]
            if node_id is not None:
                base_filters.append(EvalResult.node_id == node_id)
            if eval_id is not None:
                base_filters.append(EvalResult.eval_id == eval_id)

            total_q = select(sa_func.count(EvalResult.id)).where(*base_filters)
            total = (await session.execute(total_q)).scalar() or 0

            offset = (page - 1) * page_size
            q = (
                select(EvalResult)
                .where(*base_filters)
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
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals.list_run_evals")
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


# FAR-1472 exemption (read-only POST): compares two runs' stored eval results
# side by side and writes nothing. Deliberately left in
# audit_coverage_baseline.txt.
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
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals._fetch_compare_evals")
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
                        include_soft_deleted(
                            select(EvalDefinition).where(
                                EvalDefinition.id.in_(eval_ids),
                                EvalDefinition.organisation_id == principal.organisation_id,
                            )
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
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals._fetch_eval_definitions")
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


async def _load_eval_source_run(session: AsyncSession, principal: TenantPrincipal, run_id: uuid.UUID) -> Run:
    """Fetch the source run scoped to the caller's org (404 when missing)."""
    run = (
        await session.execute(
            select(Run).where(
                Run.id == run_id,
                Run.organisation_id == principal.organisation_id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


async def _load_eval_source_pipeline(session: AsyncSession, principal: TenantPrincipal, pipeline_id: Any) -> Pipeline:
    """Fetch the source run's pipeline scoped to the caller's org (404 when missing)."""
    pipeline = (
        await session.execute(
            select(Pipeline).where(
                Pipeline.id == pipeline_id,
                Pipeline.organisation_id == principal.organisation_id,
            )
        )
    ).scalar_one_or_none()
    if pipeline is None:
        raise HTTPException(status_code=404, detail=MSG_PIPELINE_NOT_FOUND)
    return pipeline


async def _load_node_sample_output(
    session: AsyncSession,
    principal: TenantPrincipal,
    run_id: uuid.UUID,
    node_id: uuid.UUID,
) -> dict[str, Any]:
    """Read the flagged node's output, wrapped as a dict sample for the eval.

    FAR-583 read-switch: the blobs reassemble from run_node_outputs
    (new-table-only reader) inside the caller's transaction.
    """
    blobs = await read_run_blobs(session, run_id=run_id, organisation_id=principal.organisation_id)
    outputs = blobs.outputs or {}
    node_output = (
        node_return(outputs, blobs.telemetry, str(node_id)) or node_return(outputs, blobs.telemetry, node_id.hex) or {}
    )
    return node_output if isinstance(node_output, dict) else {"output": str(node_output)}


async def _eval_from_run_source(
    session: AsyncSession,
    principal: TenantPrincipal,
    req: CreateEvalFromRunRequest,
) -> tuple[Run, dict[str, Any]]:
    """Load the from-run eval source (run, pipeline, node output) in one transaction."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            run = await _load_eval_source_run(session, principal, req.run_id)
            await _load_eval_source_pipeline(session, principal, run.pipeline_id)
            sample_output = await _load_node_sample_output(session, principal, req.run_id, req.node_id)
            return run, sample_output
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
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals._eval_from_run_source")
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


def _build_eval_config_json(eval_type: str, sample_output: dict[str, Any]) -> dict[str, Any]:
    """Build the eval definition's stub config from the sample output's first field."""
    field = next(iter(sample_output.keys())) if sample_output else ""
    if eval_type == "regex":
        return {"field": field, "pattern": ""}
    if eval_type == "json_schema":
        return {"field": field, "schema": {}}
    if eval_type == "llm_judge":
        return {"field": field, "instructions": ""}
    if eval_type == "custom_function":
        return {"field": field, "function": ""}
    return {}


async def _insert_eval_definition(
    session: AsyncSession,
    principal: TenantPrincipal,
    req: CreateEvalFromRunRequest,
    run: Run,
    config_json: dict[str, Any],
) -> dict[str, Any]:
    """Persist the new eval definition in its own transaction.

    Redirected to write to Eval+PolicyGate via the shared helper (FAR-1101 chunk 3b).
    ``failure_behaviour`` is hardcoded to the ``"warn"`` default on this path,
    so callers never set it directly.  Returns a response dict for the caller.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            try:
                eval_row = await create_or_update_eval(
                    session,
                    org_id=principal.organisation_id,
                    account_id=principal.account_id,
                    pipeline_id=run.pipeline_id,
                    node_id=req.node_id,
                    name=req.name,
                    eval_type=req.eval_type,
                    config_json=config_json,
                    failure_behaviour="warn",
                    pass_threshold=None,
                    suite_id=None,
                )
            except PolicyGateBindingViolationError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"PolicyGate binding violation: {exc}",
                ) from exc
            # Build a legacy-compatible dict for the caller
            return _eval_def_to_dict(eval_row)
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
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evals._insert_eval_definition")
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


@router.post(
    "/evals/from-run",
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(audited("eval_definition_created", "eval_definition", principal_dep=get_current_tenant_user)),
        Depends(deny_break_glass_mint),
    ],
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
    principal: TenantPrincipal = require_permission(_CODE_EVAL_DEFINITION_CREATE),
) -> dict[str, Any]:
    """Create an eval definition pre-populated from run output."""
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can create eval definitions",
        )
    run, sample_output = await _eval_from_run_source(session, principal, req)
    config_json = _build_eval_config_json(req.eval_type, sample_output)
    eval_dict = await _insert_eval_definition(session, principal, req, run, config_json)
    # eval_dict is already a legacy-compatible dict from the redirect helper
    eval_dict["sample_output"] = sample_output
    return eval_dict
