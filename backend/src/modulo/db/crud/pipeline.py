"""Org-scoped CRUD for Pipeline.

All functions assume the caller has set the RLS org context via set_rls_org()
before calling. The session must be within an active transaction.
"""

import copy
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

from sqlalchemy import ColumnElement, Connection, delete, func, select, update
from sqlalchemy.exc import InvalidRequestError, ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from modulo.core.audit_logger import append_audit_event
from modulo.db.crud.base import PageResult, apply_updates
from modulo.db.crud.hitl_gate_guard import (
    HitlGateWeakeningDenied,
    apply_gated_edge_diff,
    build_gate_diff_payload,
    denial_detail,
    enforce_guardrail_binding_strip,
    resolve_effective_privilege,
)
from modulo.db.crud.pagination import CursorPaginator
from modulo.db.crud.pipeline_owner import ACCOUNTABILITY_OWNER_FIELDS, validate_accountability_owner
from modulo.db.crud.run import count_active_runs_for_pipeline
from modulo.db.crud.team_scope import team_scope_clause
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_edge import PipelineEdge
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.snapshot_schema_pin import SnapshotSchemaPin
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.db.soft_delete import include_soft_deleted
from modulo.util import sanitise_log_value as _sanitise_log_value

_log = logging.getLogger(__name__)


class PipelineHasActiveRunsError(Exception):
    """Ownership transfer is blocked while the pipeline has non-terminal runs.

    Raised by ``update_pipeline`` when ``owner_team_id`` changes while any run
    is in a non-terminal state. The route maps this to a structured 409
    ``pipeline_has_active_runs`` response (PRD §9.3 / ownership transfer).
    """

    def __init__(self, active_run_count: int) -> None:
        self.active_run_count = active_run_count
        super().__init__(f"{active_run_count} run(s) still in progress")


def validate_max_concurrent_runs(value: int) -> int:
    """Reject ``max_concurrent_runs`` values that permanently wedge admission (FAR-604).

    0 (or a negative) admits nothing forever: the DB CHECK constraint would
    reject it on PostgreSQL with a bare IntegrityError, but internal callers
    (MCP, seeding, non-Postgres backends) bypass that guard. The dispatch gate
    treats ``<= 0`` as "no cap" while every consumer reads the column as a
    binding cap, so a 0 row silently wedges the pipeline. Raise a clear
    validation error instead; pausing admission is the org triggers pause.
    """
    if value < 1:
        raise ValueError(
            f"max_concurrent_runs must be >= 1, got {value}. "
            "0 or negative permanently wedges run admission; to pause admission use the org triggers pause."
        )
    return value


# FAR-1182: ``pipelines.circuit_breaker_threshold`` is Numeric(14, 6) with a
# ``> 0`` CHECK (migration 0186). NULL = breaker disabled.
CIRCUIT_BREAKER_THRESHOLD_MAX = Decimal("99999999.999999")
_CIRCUIT_BREAKER_QUANTUM = Decimal("0.000001")
CIRCUIT_BREAKER_THRESHOLD_CHANGED_EVENT = "pipeline.circuit_breaker_threshold_changed"
# FAR-1161: accountability-owner assignment/change/clear audit events.
BUSINESS_OWNER_CHANGED_EVENT = "pipeline.business_owner_changed"
RELIABILITY_OWNER_CHANGED_EVENT = "pipeline.reliability_owner_changed"
_OWNER_CHANGED_EVENTS: dict[str, str] = {
    "business_owner_id": BUSINESS_OWNER_CHANGED_EVENT,
    "reliability_owner_id": RELIABILITY_OWNER_CHANGED_EVENT,
}
# FAR-1184: audit event written when a raise/clear of the threshold is refused
# because the caller lacks ``cost.manage``.
CIRCUIT_BREAKER_THRESHOLD_CHANGE_DENIED_EVENT = "pipeline.circuit_breaker_threshold_change_denied"
_MSG_THRESHOLD_NOT_A_NUMBER = "circuit_breaker_threshold must be a number (USD) or null"


def normalize_circuit_breaker_threshold(value: Decimal | float | str | None) -> Decimal | None:
    """Validate + quantize a monthly spend circuit-breaker threshold (USD).

    ``None`` disables the breaker. Any set value must be a finite number
    ``> 0`` that fits the column (<= ``CIRCUIT_BREAKER_THRESHOLD_MAX`` at 6dp);
    anything else raises ``ValueError`` with a caller-facing message. Shared by
    the REST models, the CRUD writers, the MCP tools and ``modulo apply`` so
    every surface enforces the same rule the DB CHECK constraint backs.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(_MSG_THRESHOLD_NOT_A_NUMBER)
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(_MSG_THRESHOLD_NOT_A_NUMBER) from None
    if not dec.is_finite():
        raise ValueError(_MSG_THRESHOLD_NOT_A_NUMBER)
    quantized = dec.quantize(_CIRCUIT_BREAKER_QUANTUM, rounding=ROUND_HALF_UP)
    if quantized <= 0:
        raise ValueError("circuit_breaker_threshold must be greater than 0 (USD); use null to disable the breaker")
    if quantized > CIRCUIT_BREAKER_THRESHOLD_MAX:
        raise ValueError(f"circuit_breaker_threshold must be at most {CIRCUIT_BREAKER_THRESHOLD_MAX} (USD)")
    return quantized


class CircuitBreakerThresholdChangeDenied(Exception):  # noqa: N818 — denial-vocabulary name (cf. PermissionDenied)
    """FAR-1184: a raise/clear of ``circuit_breaker_threshold`` without ``cost.manage``.

    Raised by the REST and MCP surfaces only AFTER
    ``normalize_circuit_breaker_threshold`` has validated the new value and
    ``circuit_breaker_threshold_change_allowed`` has refused the change. No
    state change has occurred when this is raised (the guarded write has not
    run / its transaction has rolled back). Carries the (previous, new) pair
    so the surface can write the
    ``pipeline.circuit_breaker_threshold_change_denied`` audit event before
    returning its 403 (REST) / permission-denied result (MCP).
    """

    def __init__(self, *, previous: Decimal | None, new: Decimal | None) -> None:
        self.previous = previous
        self.new = new
        super().__init__(
            "Raising or clearing circuit_breaker_threshold requires the 'cost.manage' permission (org admin)"
        )


def circuit_breaker_threshold_change_allowed(
    previous: Decimal | None,
    new: Decimal | None,
    *,
    may_manage_cost: bool,
) -> bool:
    """FAR-1184: may this principal apply a ``circuit_breaker_threshold`` change?

    ONE shared rule, applied on every surface that can set the threshold
    (REST create + ``PATCH /pipelines/{id}``, the MCP ``create_pipeline`` +
    ``set_pipeline_circuit_breaker`` tools, and ``modulo apply`` — which is
    refused server-side by the REST gate and surfaces the 403 as an apply
    failure):

    * ``may_manage_cost`` (caller holds ``cost.manage``) — always allowed;
    * setting a threshold where none exists, or lowering it — allowed with
      ``pipeline.update`` alone (FAR-1184 rule 1);
    * raising it, or clearing an EXISTING threshold (``new is None`` with
      ``previous`` set) — refused without ``cost.manage`` (rule 2);
    * an unchanged value — including create-without-a-threshold
      (``None -> None``) — is a no-op, never a "clear".

    Callers MUST pass values already through
    ``normalize_circuit_breaker_threshold``: invalid input raises there,
    BEFORE this check, so an unexpected type (e.g. a string) never reaches
    the comparison — the denial path cannot be bypassed by a bad value type.
    Fail-closed: a comparison against an unexpected stored type denies.
    """
    if may_manage_cost:
        return True
    if new is None:
        # Clearing an EXISTING threshold needs cost.manage; nothing to clear
        # (previous also None) is a no-op, not a clear.
        return previous is None
    if previous is None:
        # Setting where none exists — allowed with pipeline.update.
        return True
    try:
        return bool(new <= previous)
    except TypeError:
        # Fail closed: an unexpected stored type can never authorise a raise.
        return False


def _threshold_as_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


async def audit_accountability_owner_change(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    field: str,
    previous: uuid.UUID | None,
    new: uuid.UUID | None,
    request_id: str | None = None,
) -> bool:
    """Append the ``pipeline.*_owner_changed`` audit event when the value changed.

    Covers ALL three transitions (FAR-1161): assignment (previous None ->
    new id), change (id -> id), and clear (id -> None). A no-op write (same
    value) records nothing, mirroring the circuit-breaker/autonomy audits.

    Returns ``True`` when an event was written.
    """
    if previous == new:
        return False
    await append_audit_event(
        session,
        org_id=org_id,
        event_type=_OWNER_CHANGED_EVENTS[field],
        actor_user_id=actor_user_id,
        resource_type="pipeline",
        resource_id=pipeline_id,
        payload_json={
            "field": field,
            "previous_owner_id": str(previous) if previous is not None else None,
            "new_owner_id": str(new) if new is not None else None,
            "changed_by": str(actor_user_id) if actor_user_id is not None else None,
        },
        request_id=request_id,
    )
    return True


async def audit_circuit_breaker_threshold_change(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    previous: Decimal | None,
    new: Decimal | None,
    request_id: str | None = None,
) -> bool:
    """Append ``pipeline.circuit_breaker_threshold_changed`` when the value changed.

    Returns ``True`` when an event was written. A no-op write (same value)
    records nothing, mirroring the autonomy-level audit.
    """
    if previous == new:
        return False
    await append_audit_event(
        session,
        org_id=org_id,
        event_type=CIRCUIT_BREAKER_THRESHOLD_CHANGED_EVENT,
        actor_user_id=actor_user_id,
        resource_type="pipeline",
        resource_id=pipeline_id,
        payload_json={
            "previous_threshold_usd": _threshold_as_float(previous),
            "new_threshold_usd": _threshold_as_float(new),
            "changed_by": str(actor_user_id) if actor_user_id is not None else None,
        },
        request_id=request_id,
    )
    return True


async def create_pipeline(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    account_id: uuid.UUID,
    description: str | None = None,
    visibility: str = "org",
    owner_team_id: uuid.UUID | None = None,
    max_concurrent_runs: int = 5,
    lock_wait_timeout_seconds: int = 300,
    node_timeout_seconds: int = 300,
    run_context_defaults: dict[str, Any] | None = None,
    default_autonomy_level: str = "manual_approval",
    max_autonomy_level: str | None = None,
    max_duration_seconds: int | None = None,
    stale_run_timeout_minutes: int = 30,
    folder_id: uuid.UUID | None = None,
    circuit_breaker_threshold: Decimal | float | None = None,
    business_owner_id: uuid.UUID | None = None,
    reliability_owner_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> Pipeline:
    if folder_id is not None:
        from modulo.db.models.pipeline_folder import PipelineFolder

        folder = await session.execute(
            select(PipelineFolder).where(
                PipelineFolder.id == folder_id,
                PipelineFolder.organisation_id == org_id,
            )
        )
        if folder.scalar_one_or_none() is None:
            raise ValueError(f"Folder not found in this organisation: {folder_id}")
    max_concurrent_runs = validate_max_concurrent_runs(max_concurrent_runs)
    threshold = normalize_circuit_breaker_threshold(circuit_breaker_threshold)
    # FAR-1161: fail-closed eligibility check on every owner assignment
    # (HTTPException 422 propagates through the route's handle_db_errors).
    for _field, _owner in (
        ("business_owner_id", business_owner_id),
        ("reliability_owner_id", reliability_owner_id),
    ):
        await validate_accountability_owner(
            session,
            owner_account_id=_owner,
            field=_field,
            org_id=org_id,
            visibility=visibility,
            owner_team_id=owner_team_id,
        )
    pipeline = Pipeline(
        organisation_id=org_id,
        name=name,
        account_id=account_id,
        description=description,
        visibility=visibility,
        owner_team_id=owner_team_id,
        business_owner_id=business_owner_id,
        reliability_owner_id=reliability_owner_id,
        max_concurrent_runs=max_concurrent_runs,
        lock_wait_timeout_seconds=lock_wait_timeout_seconds,
        node_timeout_seconds=node_timeout_seconds,
        run_context_defaults=run_context_defaults or {},
        default_autonomy_level=default_autonomy_level,
        max_autonomy_level=max_autonomy_level,
        max_duration_seconds=max_duration_seconds,
        stale_run_timeout_minutes=stale_run_timeout_minutes,
        folder_id=folder_id,
        circuit_breaker_threshold=threshold,
    )
    session.add(pipeline)
    await session.flush()
    if threshold is not None:
        await audit_circuit_breaker_threshold_change(
            session,
            org_id=org_id,
            pipeline_id=pipeline.id,
            actor_user_id=account_id,
            previous=None,
            new=threshold,
            request_id=request_id,
        )
    # FAR-1161: assignment on CREATE is an owner change (previous None -> id).
    for _field in ACCOUNTABILITY_OWNER_FIELDS:
        _new_owner = business_owner_id if _field == "business_owner_id" else reliability_owner_id
        if _new_owner is not None:
            await audit_accountability_owner_change(
                session,
                org_id=org_id,
                pipeline_id=pipeline.id,
                actor_user_id=account_id,
                field=_field,
                previous=None,
                new=_new_owner,
                request_id=request_id,
            )
    return pipeline


async def get_pipeline(
    session: AsyncSession,
    pipeline_id: uuid.UUID,
    *,
    include_deleted: bool = False,
    organisation_id: uuid.UUID | None = None,
) -> Pipeline | None:
    """Fetch a single pipeline by ID.

    Defence-in-depth: when *organisation_id* is provided, the query also
    filters on ``organisation_id`` so cross-tenant access is impossible even
    if RLS is misconfigured. RLS-based callers may omit it, but API-facing
    callers SHOULD pass it.
    """
    stmt = select(Pipeline).where(Pipeline.id == pipeline_id)
    stmt = include_soft_deleted(stmt) if include_deleted else stmt.where(Pipeline.deleted_at.is_(None))
    if organisation_id is not None:
        stmt = stmt.where(Pipeline.organisation_id == organisation_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def check_pipeline_name_available(
    session: AsyncSession,
    org_id: uuid.UUID,
    name: str,
) -> bool:
    """Return True if no pipeline with *name* exists in the given org."""
    result = await session.execute(
        select(Pipeline)
        .where(
            Pipeline.organisation_id == org_id,
            Pipeline.name == name,
            Pipeline.deleted_at.is_(None),
        )
        .with_for_update()
    )
    return result.scalar_one_or_none() is None


async def list_pipelines(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
    cursor: str | None = None,
    include_archived: bool = False,
    include_deleted: bool = False,
    folder_id: uuid.UUID | None = None,
    team_id: uuid.UUID | None = None,
) -> PageResult[Pipeline]:
    base = select(Pipeline)
    base = include_soft_deleted(base) if include_deleted else base.where(Pipeline.deleted_at.is_(None))
    if not include_archived:
        base = base.where(Pipeline.archived_at.is_(None))
    if folder_id is not None:
        base = base.where(Pipeline.folder_id == folder_id)
    if team_id is not None:
        # A team-scoped caller sees its own team's pipelines plus org-level
        # pipelines (no owner team) — the same boundary the MCP guard applies.
        base = base.where(team_scope_clause(Pipeline.owner_team_id, team_id))

    if cursor is not None:
        paginator = CursorPaginator()
        cp = await paginator.paginate(
            session,
            base,
            cursor=cursor,
            limit=page_size,
            model=Pipeline,
            compute_total=True,
        )
        return PageResult(
            items=cp.items,
            total=cp.total or 0,
            page=page,
            page_size=page_size,
            next_cursor=cp.next_cursor,
            has_more=cp.has_more,
        )

    offset = (page - 1) * page_size
    try:
        count_where: list[ColumnElement[bool]] = []
        if not include_deleted:
            count_where.append(Pipeline.deleted_at.is_(None))
        if not include_archived:
            count_where.append(Pipeline.archived_at.is_(None))
        if folder_id is not None:
            count_where.append(Pipeline.folder_id == folder_id)
        if team_id is not None:
            count_where.append(team_scope_clause(Pipeline.owner_team_id, team_id))
        count_stmt = select(func.count()).select_from(Pipeline).where(*count_where)
        if include_deleted:
            count_stmt = include_soft_deleted(count_stmt)
        total = (await session.execute(count_stmt)).scalar_one()
    except ProgrammingError:
        return PageResult(items=[], total=0, page=page, page_size=page_size)
    items = list(
        (await session.execute(base.order_by(Pipeline.created_at.desc()).offset(offset).limit(page_size))).scalars()
    )
    return PageResult(items=items, total=total, page=page, page_size=page_size)


async def update_pipeline(
    session: AsyncSession,
    pipeline_id: uuid.UUID,
    updates: dict[str, Any],
    *,
    org_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> Pipeline | None:
    """Update a pipeline, applying the PRD §9.3 ownership-transfer rules.

    When ``owner_team_id`` changes (reassign to a team, or clear back to
    org-wide) AND audit context is supplied (the REST route path), the transfer
    is blocked while any non-terminal run exists (``PipelineHasActiveRunsError``)
    and a ``resource_team_ownership_changed`` audit event is recorded. Callers
    that pass no audit context (internal tooling, MCP) are not affected.

    A ``circuit_breaker_threshold`` key (FAR-1182) is validated/quantized and,
    when the value actually changes, recorded as a
    ``pipeline.circuit_breaker_threshold_changed`` audit event on EVERY path
    (the actor is ``account_id`` when supplied). ``None`` disables the breaker.
    """
    pipeline = await get_pipeline(session, pipeline_id)
    if pipeline is None:
        return None
    if updates.get("max_concurrent_runs") is not None:
        validate_max_concurrent_runs(updates["max_concurrent_runs"])
    threshold_changing = "circuit_breaker_threshold" in updates
    previous_threshold: Decimal | None = None
    if threshold_changing:
        previous_threshold = pipeline.circuit_breaker_threshold
        updates = {
            **updates,
            "circuit_breaker_threshold": normalize_circuit_breaker_threshold(updates["circuit_breaker_threshold"]),
        }
    # FAR-1161: fail-closed owner eligibility against the EFFECTIVE (post-update)
    # visibility/owner-team — a PATCH may change all three in one payload, and
    # the invariant must hold on the resulting pipeline, not the stale one.
    #
    # Scope-change re-validation (review finding): when the update contains
    # ``visibility`` or ``owner_team_id`` — the same presence test the route's
    # team-transition gate uses — the STORED owners are re-validated too, not
    # just the ones the payload carries. A team move or visibility flip can
    # strand an already-stored owner outside the new scope even when the owner
    # keys are omitted; that combination fails CLOSED with the same 422 the
    # owner-assignment path raises (naming the offending field), never a
    # silent auto-clear. All validation runs BEFORE apply_updates so an
    # out-of-scope owner never mutates the row (the 422 rolls the transaction
    # back regardless; this keeps the in-memory object clean for any later
    # code in the same transaction).
    owner_updates = {f: updates[f] for f in ACCOUNTABILITY_OWNER_FIELDS if f in updates}
    scope_changed = "visibility" in updates or "owner_team_id" in updates
    fields_to_validate = set(owner_updates)
    if scope_changed:
        fields_to_validate.update(ACCOUNTABILITY_OWNER_FIELDS)
    # Guard the whole block: an update that touches neither owners nor scope
    # must not read scope attributes off partial stand-in rows at all.
    if fields_to_validate:
        # getattr defaults: real rows always carry both columns; partial
        # test stand-ins (SimpleNamespace) may not, and a scope-less default
        # only reaches the team gate when a real owner is being validated.
        # Non-str scope (absent stand-in attribute, explicit null) behaves as
        # org scope in the gate below — the same as `visibility != "team"`.
        _raw_visibility = updates.get("visibility", getattr(pipeline, "visibility", None))
        effective_visibility = _raw_visibility if isinstance(_raw_visibility, str) else "org"
        # .get is correct here: an explicit owner_team_id=None (clear) is in
        # the dict, so .get returns None rather than the pipeline default.
        effective_owner_team = updates.get("owner_team_id", getattr(pipeline, "owner_team_id", None))
        for _field in sorted(fields_to_validate):
            if _field in owner_updates:
                # Payload-carried owner: same unconditional pass-through as before.
                _candidate = owner_updates[_field]
            else:
                # Stored owner re-checked because the scope changed. Real rows
                # are UUID | None by column type; partial test stand-ins
                # expose non-column attribute children — treat those as
                # "unassigned" rather than feed a non-UUID into the query.
                _stored = getattr(pipeline, _field, None)
                _candidate = _stored if isinstance(_stored, uuid.UUID | None) else None
            await validate_accountability_owner(
                session,
                owner_account_id=_candidate,
                field=_field,
                org_id=org_id if org_id is not None else pipeline.organisation_id,
                visibility=effective_visibility,
                owner_team_id=effective_owner_team,
            )
    previous_owners: dict[str, uuid.UUID | None] = {f: getattr(pipeline, f) for f in owner_updates}
    old_team_id = pipeline.owner_team_id
    apply_updates(pipeline, updates)
    if threshold_changing:
        await audit_circuit_breaker_threshold_change(
            session,
            org_id=org_id if org_id is not None else pipeline.organisation_id,
            pipeline_id=pipeline_id,
            actor_user_id=account_id,
            previous=previous_threshold,
            new=updates["circuit_breaker_threshold"],
            request_id=request_id,
        )
    # FAR-1161: audit assignment/change/clear of each accountability owner.
    for _field, _previous in previous_owners.items():
        await audit_accountability_owner_change(
            session,
            org_id=org_id if org_id is not None else pipeline.organisation_id,
            pipeline_id=pipeline_id,
            actor_user_id=account_id,
            field=_field,
            previous=_previous,
            new=owner_updates[_field],
            request_id=request_id,
        )
    new_team_id = pipeline.owner_team_id
    if org_id is not None and account_id is not None and new_team_id != old_team_id:
        active_runs = await count_active_runs_for_pipeline(session, pipeline_id, include_pending=True)
        if active_runs:
            raise PipelineHasActiveRunsError(active_runs)
        await append_audit_event(
            session,
            org_id=org_id,
            event_type="resource_team_ownership_changed",
            actor_user_id=account_id,
            resource_type="pipeline",
            resource_id=pipeline_id,
            payload_json={
                "resource_type": "pipeline",
                "resource_id": str(pipeline_id),
                "old_team_id": str(old_team_id) if old_team_id is not None else None,
                "new_team_id": str(new_team_id) if new_team_id is not None else None,
                "changed_by": str(account_id),
            },
            request_id=request_id,
        )
    await session.flush()
    return pipeline


async def soft_delete_pipeline(
    session: AsyncSession,
    pipeline_id: uuid.UUID,
    deleted_by: uuid.UUID | None = None,
) -> Pipeline | None:
    """Mark a pipeline as deleted (soft delete). Returns None if not found or already deleted.

    ``deleted_by`` stamps the deleting account onto ``Pipeline.deleted_by`` for
    audit (mirrors the eval models' soft-delete wiring). It is optional so the
    MCP/hard-delete paths that call this without a principal keep working.
    """
    result = await session.execute(
        update(Pipeline)
        .where(Pipeline.id == pipeline_id, Pipeline.deleted_at.is_(None))
        .values(deleted_at=func.now(), deleted_by=deleted_by)
        .returning(Pipeline)
    )
    await session.flush()
    return result.scalar_one_or_none()


async def restore_pipeline(session: AsyncSession, pipeline_id: uuid.UUID) -> Pipeline | None:
    """Restore a soft-deleted pipeline. Returns None if not found.

    Clears both ``deleted_at`` and ``deleted_by`` so a restored row never
    carries a stale ``deleted_by`` stamp with ``deleted_at IS NULL`` (restores
    the audit state to exactly what soft_delete wrote, mirrored by the eval
    models' restore wiring).
    """
    result = await session.execute(
        update(Pipeline)
        .where(Pipeline.id == pipeline_id, Pipeline.deleted_at.is_not(None))
        .values(deleted_at=None, deleted_by=None)
        .returning(Pipeline)
    )
    await session.flush()
    return result.scalar_one_or_none()


async def archive_pipeline(session: AsyncSession, pipeline_id: uuid.UUID) -> Pipeline | None:
    pipeline = await get_pipeline(session, pipeline_id)
    if pipeline is None:
        return None
    pipeline.archived_at = datetime.now(UTC)
    await session.flush()
    return pipeline


async def unarchive_pipeline(session: AsyncSession, pipeline_id: uuid.UUID) -> Pipeline | None:
    pipeline = await get_pipeline(session, pipeline_id)
    if pipeline is None:
        return None
    pipeline.archived_at = None
    await session.flush()
    return pipeline


async def get_pipeline_graph(
    session: AsyncSession,
    pipeline_id: uuid.UUID,
) -> tuple[list[dict[str, Any]], list[PipelineEdge]] | None:
    """Return the editable live graph for an RLS-visible pipeline."""
    pipeline = await get_pipeline(session, pipeline_id)
    if pipeline is None:
        return None
    edges = list(
        (
            await session.execute(
                select(PipelineEdge)
                .where(PipelineEdge.pipeline_id == pipeline_id)
                .order_by(PipelineEdge.created_at, PipelineEdge.id)
            )
        ).scalars()
    )
    return list(pipeline.graph_nodes_json), edges


@dataclass
class _CloneSourceSnapshot:
    """Plain-data snapshot of the source pipeline taken in the clone's short
    step-(a) transaction, so the slower clone work (step b) never depends on a
    lock or on live reads of the source (hitl-gate-removal-guard-plan.md §3 item 3).
    """

    name: str
    description: str | None
    visibility: str
    owner_team_id: uuid.UUID | None
    max_concurrent_runs: int
    lock_wait_timeout_seconds: int
    node_timeout_seconds: int
    run_context_defaults: dict[str, Any]
    graph_nodes_json: list[dict[str, Any]]
    default_autonomy_level: str
    max_autonomy_level: str | None
    stale_run_timeout_minutes: int
    stdout_retention_config: dict[str, Any] | None
    edges: list[dict[str, Any]]
    snapshots: list[dict[str, Any]]
    # FAR-1182: a copy keeps the source's spend safety limit (never its
    # tripped state - the clone starts with a fresh, untripped breaker).
    circuit_breaker_threshold: Decimal | None = None


async def clone_pipeline(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    account_id: uuid.UUID,
    org_role: str | None = None,
    new_name: str | None = None,
    _read_session_factory: Callable[[], AsyncSession] | None = None,
    _on_step_a_held: Callable[[], Awaitable[None]] | None = None,
    _on_step_a_committed: Callable[[], Awaitable[None]] | None = None,
) -> Pipeline | None:
    """Deep-copy a pipeline and its graph (nodes + first-class edges + snapshots).

    Returns the *new* Pipeline, or *None* if the source does not exist.
    Connector bindings are preserved by reference so users can rebind later.
    SnapshotSchemaPins are also copied for each cloned snapshot.

    Torn-read fix (plan §3 item 3): the source reads (``FOR SHARE`` on the
    pipeline row + nodes/edges/snapshots into plain data) run in a short,
    separate transaction on a read session that commits immediately. The
    slower clone work then runs on the caller's session using only the
    plain-data snapshot, with no further lock dependency — a concurrent
    ``replace_pipeline_graph``'s ``FOR UPDATE`` on the same row can proceed as
    soon as the step-(a) transaction commits. All cloned gated edges emit ONE
    batched ``edge_created_with_gate`` audit event.

    The step-(a) read session is a separate connection, so ``set_rls_org`` is
    not enough: pipelines are team-scoped, so their RLS policy also checks the
    caller's ``app.user_id`` / ``app.org_role``, which must be re-applied via
    ``set_rls_user_context`` for the source to be visible. Pass ``org_role``
    (the caller's org role) and the read session re-applies the caller's full
    RLS context; ``account_id`` doubles as the caller's user.
    """
    _log.info(
        "Cloning pipeline %s (org=%s, requested_name=%s)",
        _sanitise_log_value(pipeline_id),
        _sanitise_log_value(org_id),
        _sanitise_log_value(new_name),
    )

    snapshot = await _read_clone_source_snapshot(
        session,
        org_id=org_id,
        pipeline_id=pipeline_id,
        user_id=account_id,
        org_role=org_role,
        read_factory=_read_session_factory,
        on_step_a_held=_on_step_a_held,
    )
    if snapshot is None:
        _log.warning("Clone aborted: source pipeline %s not found", pipeline_id)
        return None

    if _on_step_a_committed is not None:
        await _on_step_a_committed()

    cloned = await _clone_pipeline_config(
        session,
        snapshot,
        org_id=org_id,
        account_id=account_id,
        pipeline_id=pipeline_id,
        new_name=new_name,
    )
    edge_count = await _clone_edges(
        session,
        snapshot.edges,
        source_id=pipeline_id,
        cloned_id=cloned.id,
        org_id=org_id,
        account_id=account_id,
    )
    node_count = len(snapshot.graph_nodes_json)
    snap_count = await _clone_snapshots(
        session,
        snapshot.snapshots,
        source_id=pipeline_id,
        cloned_id=cloned.id,
        org_id=org_id,
    )

    await session.flush()
    _log.info(
        "Clone complete: %s -> %s (%d edges, %d nodes, %d snapshots)",
        pipeline_id,
        cloned.id,
        edge_count,
        node_count,
        snap_count,
    )
    return cloned


async def _clone_pipeline_config(
    session: AsyncSession,
    snapshot: _CloneSourceSnapshot,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    new_name: str | None,
) -> Pipeline:
    """Build and flush the cloned ``Pipeline`` row from the plain-data snapshot.

    The clone deliberately copies the source's config fields (deep-copying the
    mutable JSON blobs so the clone never shares references with the source),
    giving it a fresh id before any dependent rows (edges, snapshots) are added.
    """
    name = new_name or f"Copy of {snapshot.name}"
    _log.info(
        "Copying pipeline config for %s -> '%s'",
        _sanitise_log_value(pipeline_id),
        _sanitise_log_value(name),
    )
    cloned = Pipeline(
        organisation_id=org_id,
        name=name,
        account_id=account_id,
        description=snapshot.description,
        visibility=snapshot.visibility,
        owner_team_id=snapshot.owner_team_id,
        max_concurrent_runs=snapshot.max_concurrent_runs,
        lock_wait_timeout_seconds=snapshot.lock_wait_timeout_seconds,
        node_timeout_seconds=snapshot.node_timeout_seconds,
        run_context_defaults=copy.deepcopy(snapshot.run_context_defaults),
        # FAR-889: copying a stored snapshot replays its graph verbatim.  A
        # snapshot predating the guard may contain schema-less manual nodes, so
        # this duplicate path tolerates them (FAR-874) rather than running
        # enforce_manual_node_output_schemas; new graphs are guarded on write.
        graph_nodes_json=copy.deepcopy(snapshot.graph_nodes_json),
        default_autonomy_level=snapshot.default_autonomy_level,
        max_autonomy_level=snapshot.max_autonomy_level,
        stale_run_timeout_minutes=snapshot.stale_run_timeout_minutes,
        stdout_retention_config=copy.deepcopy(snapshot.stdout_retention_config),
        circuit_breaker_threshold=snapshot.circuit_breaker_threshold,
    )
    session.add(cloned)
    await session.flush()
    _log.info("Pipeline config copied: new id=%s", cloned.id)
    return cloned


async def _clone_edges(
    session: AsyncSession,
    edges: list[dict[str, Any]],
    *,
    source_id: uuid.UUID,
    cloned_id: uuid.UUID,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
) -> int:
    """Copy the source's first-class edges onto the clone.

    Returns the number of edges copied. Any gated edge (non-None
    ``hitl_gate_config``) is collected and, if at least one exists, emitted as a
    single batched ``edge_created_with_gate`` audit event after the flush.
    """
    _log.info("Copying edges for pipeline %s -> %s", source_id, cloned_id)
    gated_cloned_edges: list[PipelineEdge] = []
    for edge in edges:
        cloned_edge = PipelineEdge(
            organisation_id=org_id,
            pipeline_id=cloned_id,
            source_node_id=edge["source_node_id"],
            target_node_id=edge["target_node_id"],
            edge_type=edge["edge_type"],
            condition_expression=edge.get("condition_expression"),
            hitl_gate_config=copy.deepcopy(edge["hitl_gate_config"]),
            source_port=edge.get("source_port") or "out",
            target_port=edge.get("target_port") or "in",
        )
        session.add(cloned_edge)
        if edge["hitl_gate_config"] is not None:
            gated_cloned_edges.append(cloned_edge)
    await session.flush()
    if gated_cloned_edges:
        await append_audit_event(
            session,
            org_id=org_id,
            event_type="edge_created_with_gate",
            actor_user_id=account_id,
            resource_type="pipeline",
            resource_id=cloned_id,
            payload_json={"edge_ids": [str(e.id) for e in gated_cloned_edges]},
        )
    return len(edges)


async def _clone_snapshots(
    session: AsyncSession,
    snapshots: list[dict[str, Any]],
    *,
    source_id: uuid.UUID,
    cloned_id: uuid.UUID,
    org_id: uuid.UUID,
) -> int:
    """Copy the source's snapshots (and their schema pins) onto the clone.

    Returns the number of snapshots copied. The JSON blobs are deep-copied so
    the cloned snapshots are fully independent of the source.
    """
    _log.info("Copying snapshots for pipeline %s -> %s", source_id, cloned_id)
    snap_count = 0
    for snap in snapshots:
        cloned_snap = PipelineSnapshot(
            organisation_id=org_id,
            pipeline_id=cloned_id,
            snapshot_version=snap["snapshot_version"],
            account_id=snap["account_id"],
            environment_profile_id=snap["environment_profile_id"],
            graph_json=copy.deepcopy(snap["graph_json"]),
            connector_bindings_json=copy.deepcopy(snap["connector_bindings_json"]),
            schema_pins_json=copy.deepcopy(snap["schema_pins_json"]),
            prompt_pins_json=copy.deepcopy(snap["prompt_pins_json"]),
            model_backend_pins_json=copy.deepcopy(snap["model_backend_pins_json"]),
            composite_bindings_json=copy.deepcopy(snap["composite_bindings_json"]),
            parameter_bindings_json=copy.deepcopy(snap["parameter_bindings_json"]),
            tag=snap["tag"],
            notes=snap["notes"],
            default_autonomy_level=snap["default_autonomy_level"],
            max_autonomy_level=snap.get("max_autonomy_level"),
            config_json=copy.deepcopy(snap["config_json"]),
            run_context_defaults=copy.deepcopy(snap["run_context_defaults"]),
            stdout_retention_config=copy.deepcopy(snap.get("stdout_retention_config")),
        )
        session.add(cloned_snap)
        await session.flush()

        for pin in snap["pins"]:
            session.add(
                SnapshotSchemaPin(
                    organisation_id=org_id,
                    snapshot_id=cloned_snap.id,
                    node_id=pin["node_id"],
                    direction=pin["direction"],
                    schema_id=pin["schema_id"],
                    schema_version=pin["schema_version"],
                )
            )
        snap_count += 1
    return snap_count


def _edge_to_plain_dict(e: PipelineEdge) -> dict[str, Any]:
    """Flatten a ``PipelineEdge`` row into the plain-data shape used by both
    the clone snapshot and the graph-replace read path."""
    return {
        "source_node_id": e.source_node_id,
        "target_node_id": e.target_node_id,
        "edge_type": e.edge_type,
        "source_port": e.source_port or "out",
        "target_port": e.target_port or "in",
        "hitl_gate_config": copy.deepcopy(e.hitl_gate_config),
        "condition_expression": getattr(e, "condition_expression", None),
    }


def _snapshot_pin_to_dict(p: SnapshotSchemaPin) -> dict[str, Any]:
    """Flatten a ``SnapshotSchemaPin`` row into plain data."""
    return {
        "node_id": p.node_id,
        "direction": p.direction,
        "schema_id": p.schema_id,
        "schema_version": p.schema_version,
    }


def _snapshot_to_dict(snap: PipelineSnapshot, pins: list[dict[str, Any]]) -> dict[str, Any]:
    """Flatten a ``PipelineSnapshot`` row into plain data, deep-copying its JSON
    blobs so the clone never shares references with the source."""
    return {
        "snapshot_version": snap.snapshot_version,
        "account_id": snap.account_id,
        "environment_profile_id": snap.environment_profile_id,
        "graph_json": copy.deepcopy(snap.graph_json),
        "connector_bindings_json": copy.deepcopy(snap.connector_bindings_json),
        "schema_pins_json": copy.deepcopy(snap.schema_pins_json),
        "prompt_pins_json": copy.deepcopy(snap.prompt_pins_json),
        "model_backend_pins_json": copy.deepcopy(snap.model_backend_pins_json),
        "composite_bindings_json": copy.deepcopy(snap.composite_bindings_json),
        "parameter_bindings_json": copy.deepcopy(snap.parameter_bindings_json),
        "tag": snap.tag,
        "notes": snap.notes,
        "default_autonomy_level": snap.default_autonomy_level,
        "max_autonomy_level": getattr(snap, "max_autonomy_level", None),
        "config_json": copy.deepcopy(snap.config_json),
        "run_context_defaults": copy.deepcopy(snap.run_context_defaults),
        "stdout_retention_config": copy.deepcopy(snap.stdout_retention_config),
        "pins": pins,
    }


def _resolve_read_session_factory(
    session: AsyncSession,
    read_factory: Callable[[], AsyncSession] | None,
) -> tuple[Callable[[], AsyncSession], AsyncEngine | None]:
    """Return ``(factory, read_engine)`` for the clone's step-(a) read session.

    When the caller supplies a *read_factory* it is used as-is (no engine to
    dispose). Otherwise one is derived from the caller's session binding:
    ``session.bind`` is the ``AsyncEngine`` the session was created with;
    ``session.get_bind()`` returns the *sync* ``Engine`` (SQLAlchemy 2.0) which
    ``async_sessionmaker`` rejects ("AsyncEngine expected"). If no usable
    binding exists a ``RuntimeError`` is raised and *read_engine* is None.
    """
    if read_factory is not None:
        return read_factory, None

    bind = session.bind
    if isinstance(bind, AsyncEngine):
        return async_sessionmaker(bind, expire_on_commit=False, class_=AsyncSession), None

    if bind is None:
        try:
            raw = session.get_bind()
        except InvalidRequestError:
            raise RuntimeError("cannot derive an async read URL from the clone source session") from None
        async_url: Any = raw.engine.url if isinstance(raw, Connection) else raw.url
    elif isinstance(bind, AsyncConnection):
        conn = bind.sync_connection
        if conn is None:
            raise RuntimeError("AsyncConnection has no bound sync connection; cannot derive read URL")
        async_url = conn.engine.url
    else:
        async_url = None

    if async_url is None:
        raise RuntimeError("cannot derive an async read URL from the clone source session")
    read_engine = create_async_engine(async_url, poolclass=NullPool)
    return async_sessionmaker(read_engine, expire_on_commit=False, class_=AsyncSession), read_engine


async def _read_clone_source_snapshot(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    user_id: uuid.UUID | None,
    org_role: str | None,
    read_factory: Callable[[], AsyncSession] | None,
    on_step_a_held: Callable[[], Awaitable[None]] | None,
) -> _CloneSourceSnapshot | None:
    """Step (a): short transaction that FOR SHARE-locks the source pipeline row,
    reads nodes/edges/snapshots into plain data, and commits immediately.

    The read session is a separate connection/pool checkout, so it must
    re-apply the caller's full RLS context: ``set_rls_org`` scopes the org, and
    ``set_rls_user_context`` (when *org_role* is given) makes team-scoped
    pipelines visible via ``app.user_id`` / ``app.org_role``.
    """
    factory, read_engine = _resolve_read_session_factory(session, read_factory)

    try:
        async with factory() as read_session, read_session.begin():
            await set_rls_org(read_session, org_id)
            # The guard is intentionally more permissive than the endpoint,
            # which always passes a non-None org_role (TenantPrincipal). Non-API
            # callers and unit tests may omit it, so only re-apply the user
            # context when both identity parts are available.
            if user_id is not None and org_role is not None:
                await set_rls_user_context(read_session, user_id, org_role)
            src_result = await read_session.execute(
                select(Pipeline)
                .where(Pipeline.id == pipeline_id, Pipeline.deleted_at.is_(None))
                .with_for_update(read=True)
            )
            source = src_result.scalar_one_or_none()
            if source is None:
                return None
            if on_step_a_held is not None:
                await on_step_a_held()

            edges = [
                _edge_to_plain_dict(e)
                for e in (
                    await read_session.execute(
                        select(PipelineEdge)
                        .where(PipelineEdge.pipeline_id == pipeline_id)
                        .order_by(PipelineEdge.created_at, PipelineEdge.id)
                    )
                ).scalars()
            ]
            snap_rows = list(
                (
                    await read_session.execute(
                        select(PipelineSnapshot)
                        .where(PipelineSnapshot.pipeline_id == pipeline_id)
                        .order_by(PipelineSnapshot.snapshot_version)
                    )
                ).scalars()
            )
            snapshots: list[dict[str, Any]] = []
            for snap in snap_rows:
                pins = [
                    _snapshot_pin_to_dict(p)
                    for p in (
                        await read_session.execute(
                            select(SnapshotSchemaPin).where(SnapshotSchemaPin.snapshot_id == snap.id)
                        )
                    ).scalars()
                ]
                snapshots.append(_snapshot_to_dict(snap, pins))

            return _CloneSourceSnapshot(
                name=source.name,
                description=source.description,
                visibility=source.visibility,
                owner_team_id=source.owner_team_id,
                max_concurrent_runs=source.max_concurrent_runs,
                lock_wait_timeout_seconds=source.lock_wait_timeout_seconds,
                node_timeout_seconds=source.node_timeout_seconds,
                run_context_defaults=copy.deepcopy(source.run_context_defaults),
                # FAR-889: raw source graph for the clone writer; carried
                # verbatim so legacy schema-less manual nodes survive a copy
                # (FAR-874).  This is a read for an existing graph, not a
                # new-node entry point.
                graph_nodes_json=copy.deepcopy(list(source.graph_nodes_json or [])),
                default_autonomy_level=str(source.default_autonomy_level or "manual_approval"),
                # getattr: stand-in rows built by tests (and any pre-0256
                # materialisation) may lack the column — treat as NULL ceiling.
                max_autonomy_level=getattr(source, "max_autonomy_level", None),
                stale_run_timeout_minutes=source.stale_run_timeout_minutes,
                stdout_retention_config=copy.deepcopy(source.stdout_retention_config),
                edges=edges,
                snapshots=snapshots,
                # getattr: tolerate partial row stand-ins (the real source is
                # a full ORM row); a missing value copies as "disabled".
                circuit_breaker_threshold=getattr(source, "circuit_breaker_threshold", None),
            )
    finally:
        if read_engine is not None:
            await read_engine.dispose()


def _preserve_omitted_gate_config(
    edge: dict[str, Any],
    old_by_key: dict[tuple[str, str, str], Any],
) -> Any:
    """Resolve the ``hitl_gate_config`` to persist for a proposed edge.

    Mirrors ``hitl_gate_guard._normalize_edge`` presence semantics: when the
    client omits the ``hitl_gate_config`` key (or sends
    ``hitl_gate_config_present=False``) for an edge whose topology key matches
    a pre-existing gated edge, the stored value is preserved; for an edge with
    no prior gate the omission persists ``None``. Any ``hitl_gate_config`` value
    carried alongside an omission signal is ignored, exactly as the guard
    ignores it — a client cannot sneak a gate value past an explicit
    ``present=False``. The delete+reinsert write path must honour the guard's
    "omission = preserve" contract (hitl-gate-removal-guard-plan.md §3 item 6)
    — otherwise a client that simply omits the key would silently wipe the
    gate with zero audit.
    """
    present = edge.get("hitl_gate_config_present", "hitl_gate_config" in edge)
    if present:
        return edge.get("hitl_gate_config")
    key = (
        str(edge["source_node_id"]),
        str(edge["target_node_id"]),
        str(edge["edge_type"]),
    )
    return old_by_key.get(key)


class ManualNodeOutputSchemaError(Exception):
    """Raised when a manual node is persisted without an output schema.

    Manual nodes MUST carry either ``output_schema_id`` or
    ``output_schema_pin`` so the run-time executor knows what output shape
    to expect.  Storing a manual node without one is a data-integrity
    violation (FAR-889).
    """

    def __init__(self, node_id: str) -> None:
        self.node_id = node_id
        super().__init__(f"Manual node '{node_id}' requires an output schema (output_schema_id or output_schema_pin)")


def enforce_manual_node_output_schemas(nodes: list[dict[str, Any]]) -> None:
    """Reject manual nodes that lack an output schema at write time.

    This is the write-path guard for FAR-889.  It catches the case where a
    template, library primitive, or direct graph update tries to persist a
    manual node without any of ``output_schema_id``, ``output_schema_pin``,
    or ``output_schema_json``.
    """
    for node in nodes:
        if node.get("node_type") != "manual":
            continue
        has_output = (
            node.get("output_schema_id") is not None
            or node.get("output_schema_pin") is not None
            or node.get("output_schema_json") is not None
        )
        if not has_output:
            raw_id = node.get("id")
            raise ManualNodeOutputSchemaError(str(raw_id) if raw_id is not None else "unknown")


async def replace_pipeline_graph(
    session: AsyncSession,
    *,
    pipeline_id: uuid.UUID,
    org_id: uuid.UUID,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    is_privileged: bool,
    caller_type: Literal["rest", "mcp"],
    account_id: uuid.UUID | None = None,
    is_guardrail_admin: bool = False,
    _on_lock_acquired: Callable[[], Awaitable[None]] | None = None,
) -> tuple[list[dict[str, Any]], list[PipelineEdge]] | None:
    """Atomically replace an editable graph while preserving first-class edges.

    ADR 047 service-layer backstop + hitl-gate-removal-guard-plan.md v19: the
    HITL gate guard runs here, under the row lock and BEFORE any delete/insert.
    ``caller_type`` is required (no default); ``"mcp"`` forces ``is_privileged``
    to False with no live-role query. For ``"rest"`` with ``account_id`` the
    caller's live org role is re-read under the lock (fail-closed on DB error,
    no retry). A gate-weakening write by a non-privileged caller raises
    ``HitlGateWeakeningDenied`` before the delete/insert executes.

    FAR-309 PR A review: the guardrail-binding strip guard
    (``enforce_guardrail_binding_strip``) runs here too, under the same row
    lock — a non-admin cannot strip a guardrail binding by removing a
    guardrail-bound node. ``is_guardrail_admin`` is the caller-supplied admin
    flag (admin-level, the ``guardrail.manage`` privilege — distinct from the
    operator+ ``is_privileged``); for ``"rest"`` with ``account_id`` the live
    role is re-read under the lock.
    """
    result = await session.execute(
        select(Pipeline).where(Pipeline.id == pipeline_id, Pipeline.deleted_at.is_(None)).with_for_update()
    )
    pipeline = result.scalar_one_or_none()
    if pipeline is None:
        return None

    if _on_lock_acquired is not None:
        await _on_lock_acquired()

    effective_privileged = await resolve_effective_privilege(
        session,
        org_id=org_id,
        account_id=account_id,
        is_privileged=is_privileged,
        caller_type=caller_type,
    )

    # Snapshot current edges into plain data BEFORE any write (defense in depth).
    old_rows = list(
        (await session.execute(select(PipelineEdge).where(PipelineEdge.pipeline_id == pipeline_id))).scalars()
    )
    old_edges: list[dict[str, Any]] = [
        {
            "source_node_id": str(e.source_node_id),
            "target_node_id": str(e.target_node_id),
            "edge_type": e.edge_type,
            "condition_expression": getattr(e, "condition_expression", None),
            "hitl_gate_config": copy.deepcopy(e.hitl_gate_config),
            "source_port": getattr(e, "source_port", None) or "out",
            "target_port": getattr(e, "target_port", None) or "in",
        }
        for e in old_rows
    ]

    # FAR-309 PR A review: service-layer guardrail-binding strip guard. A
    # non-admin may not remove a guardrail-bound node (that would drop the
    # binding). Runs under the row lock, before any graph mutation.
    await enforce_guardrail_binding_strip(
        session,
        pipeline_id=pipeline_id,
        org_id=org_id,
        incoming_node_ids={str(node.get("id")) for node in nodes if node.get("id")},
        is_guardrail_admin=is_guardrail_admin,
        caller_type=caller_type,
        account_id=account_id,
    )

    diff = await apply_gated_edge_diff(
        session,
        old_edges,
        edges,
        is_privileged=effective_privileged,
        caller_type=caller_type,
        # FAR-609 review: node-level gate weakening (FAR-402 hitl_config) is
        # detected in the same guard pass — the node lists are deep-copied
        # before any write (same defense in depth as old_edges).
        old_nodes=copy.deepcopy(list(pipeline.graph_nodes_json or [])),
        new_nodes=copy.deepcopy(nodes),
    )
    all_weakened = (*diff.weakened_edges, *diff.weakened_nodes)
    if diff.denied:
        raise HitlGateWeakeningDenied(
            reason_code=diff.reason_code or "insufficient-role",
            correlation_keys=[w.correlation_key for w in all_weakened],
            weakening_types=sorted({t for w in all_weakened for t in w.weakening_types}),
            detail=denial_detail(diff),
            payload_json=build_gate_diff_payload(diff, caller_type),
        )
    if diff.has_weakening:
        await append_audit_event(
            session,
            org_id=org_id,
            event_type="hitl_gate_removed",
            actor_user_id=account_id,
            resource_type="pipeline",
            resource_id=pipeline_id,
            payload_json=build_gate_diff_payload(diff, caller_type),
        )

    # FAR-889: reject manual nodes without output schemas at write time.
    enforce_manual_node_output_schemas(nodes)

    pipeline.graph_nodes_json = nodes
    await session.execute(delete(PipelineEdge).where(PipelineEdge.pipeline_id == pipeline_id))
    old_by_key = {
        (str(e["source_node_id"]), str(e["target_node_id"]), str(e["edge_type"])): e["hitl_gate_config"]
        for e in old_edges
        if e.get("hitl_gate_config") is not None
    }
    # Coerce edge id/source/target to uuid.UUID objects. The REST Pydantic path
    # already does this, but MCP passes raw dicts with string ids — and SQLAlchemy's
    # insertmanyvalues sentinel matching (INSERT ... RETURNING) requires UUID
    # objects, not strings, to match the returned sentinel. Without coercion a
    # 2+ edge graph save raises InvalidRequestError (MCP update_pipeline_graph
    # internal_error).
    persisted_edges = [
        PipelineEdge(
            id=uuid.UUID(str(edge["id"])),
            organisation_id=org_id,
            pipeline_id=pipeline_id,
            source_node_id=uuid.UUID(str(edge["source_node_id"])),
            target_node_id=uuid.UUID(str(edge["target_node_id"])),
            edge_type=edge["edge_type"],
            condition_expression=edge.get("condition_expression"),
            hitl_gate_config=_preserve_omitted_gate_config(edge, old_by_key),
            source_port=edge.get("source_port") or "out",
            target_port=edge.get("target_port") or "in",
        )
        for edge in edges
    ]
    session.add_all(persisted_edges)
    await session.flush()
    return list(pipeline.graph_nodes_json), persisted_edges
