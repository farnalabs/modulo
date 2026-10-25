"""Evidence read surface + server-side field-level redaction boundary (FAR-944).

Implements chunk 9b §2 sub-scope A: a read-only view over the generic
``evidence`` store (chunk 7) with an admin-editable redaction boundary
(§2.2-§2.4). Redaction is applied SERVER-SIDE before serialisation — the
frontend renders the payload as-is and never filters fields itself.

Also implements sub-scope B (FAR-941): Risk / Assessment as a first-class
navigation entry — a filtered, faceted projection over the evidence store.

Endpoints
---------
GET  /api/v1/evidence                    list run evidence rows (redacted)
GET  /api/v1/evidence/{evidence_id}      one evidence row (redacted)
GET  /api/v1/evidence/redaction-rules    list the org's rules (admin)
PUT  /api/v1/evidence/redaction-rules    upsert a rule (admin)
GET  /api/v1/evidence/risk-assessment    risk/assessment faceted projection (FAR-941)

All reads are org-scoped through RLS (``set_rls_org`` +
``set_rls_user_context`` inside the transaction); the organisation is always
taken from the principal, never from client input.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status  # nosemgrep: loopvar-shadows-import
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Text, and_, cast, func, or_, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import (
    get_db_session,
    require_permission,
    require_system_or_org_admin,
)
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.audit_coverage import audited
from modulo.core.evidence_redaction import (
    REDACTION_CLASS_FIELDS,
    apply_redaction,
    load_redaction_rules,
    resolve_redaction_classes_from,
)
from modulo.db.models.evidence import Evidence
from modulo.db.models.evidence_redaction_rule import EvidenceRedactionRule
from modulo.db.models.policy_gate_decision import PolicyGateDecision
from modulo.db.models.run import Run
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.db.soft_delete import include_soft_deleted

router = APIRouter(prefix="/api/v1", tags=["evidence"])

_CODE_EVIDENCE_READ = "evidence.read"
_CODE_EVIDENCE_REDACTION_MANAGE = "evidence.redaction.manage"
_CODE_READ_REDACTION_RULES = "evidence.read_redaction_rules"
_CODE_UPSERT_REDACTION_RULE = "evidence.upsert_redaction_rule"
_CODE_LIST_EVIDENCE = "evidence.list"
_CODE_GET_EVIDENCE = "evidence.get"
_CODE_RISK_ASSESSMENT = "evidence.risk_assessment"

_MSG_EVIDENCE_NOT_FOUND = "Evidence row not found"

#: Hard cap on rows returned by the list endpoint, so a run with a very large
#: evidence history cannot force one unbounded response. Ordered by
#: ``created_at`` then ``id`` (stable), so the cap truncates a deterministic tail.
_EVIDENCE_LIST_LIMIT = 500

#: Gate-action vocabulary for the action facet. Single source for validation,
#: per-row resolution and the summary counts — never re-list these values.
_RISK_ACTIONS: tuple[str, ...] = ("block", "escalate", "warn", "continue")
_VALID_ACTIONS = frozenset(_RISK_ACTIONS)
#: Severity ordering used when a run has decisions with several actions.
_ACTION_SEVERITY = {action: rank for rank, action in enumerate(_RISK_ACTIONS)}


# ── Request / response models ────────────────────────────────────────────


class RedactionRuleUpsert(BaseModel):
    """Upsert payload for one ``(key_pattern, role)`` redaction rule (§2.4)."""

    key_pattern: str = Field(min_length=1, max_length=255)
    role: str = Field(pattern=r"^(admin|operator|viewer)$")
    redacted_fields: list[str] = Field(default_factory=list)

    @field_validator("redacted_fields")
    @classmethod
    def _known_classes_only(cls, value: list[str]) -> list[str]:
        """Reject unknown redaction classes so a rule can never be a silent no-op."""
        unknown = sorted({item for item in value if item not in REDACTION_CLASS_FIELDS})
        if unknown:
            raise ValueError(f"Unknown redaction class(es): {', '.join(unknown)}")
        return sorted(set(value))


class RedactionRuleResponse(BaseModel):
    """A stored redaction rule."""

    id: uuid.UUID
    key_pattern: str
    role: str
    redacted_fields: list[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class EvidenceRowResponse(BaseModel):
    """One evidence row with its ``value`` already redacted for the caller's role.

    ``action`` is the gate action resolved from ``policy_gate_decisions`` for the
    row's run (and node, for ``node_execution`` rows). It is only populated by
    the risk/assessment projection; the plain evidence reads leave it ``None``
    because they do not join the decision record.
    """

    id: uuid.UUID
    organisation_id: uuid.UUID
    key: str
    subject_type: str
    subject_id: str
    value: Any
    observed_at: datetime
    producer_type: str
    producer_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    action: str | None = None


class RiskAssessmentSummary(BaseModel):
    """Summary counts for the risk/assessment view."""

    total_rows: int
    action_counts: dict[str, int]
    facet_degraded: bool = False


class RiskAssessmentResponse(BaseModel):
    """Risk/assessment projection response."""

    summary: RiskAssessmentSummary
    rows: list[EvidenceRowResponse]


# ── DB helpers (module-level so route tests can patch them individually) ──


async def _load_evidence_rows(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    run_id: uuid.UUID,
    node_id: str | None,
    key: str | None,
) -> list[Evidence]:
    """Load the run's evidence rows, optionally narrowed to a node / key.

    With no ``node_id`` this selects the run-level rows (``subject_type`` ==
    ``"run"`` AND ``subject_id`` == the run id). The explicit ``subject_type``
    guard matters: without it a future non-run subject whose ``subject_id``
    happened to equal the run id would be returned here. With a ``node_id`` it
    additionally includes the composite ``node_execution`` rows for that node
    (``subject_id`` == ``"{run_id}:{node_id}"``).

    Capped at :data:`_EVIDENCE_LIST_LIMIT` rows.
    """
    if node_id is None:
        subject_predicate = and_(
            Evidence.subject_type == "run",
            Evidence.subject_id == str(run_id),
        )
    else:
        subject_predicate = or_(
            and_(Evidence.subject_type == "run", Evidence.subject_id == str(run_id)),
            and_(
                Evidence.subject_type == "node_execution",
                Evidence.subject_id == f"{run_id}:{node_id}",
            ),
        )

    stmt = select(Evidence).where(
        Evidence.organisation_id == organisation_id,
        subject_predicate,
    )
    if key is not None:
        stmt = stmt.where(Evidence.key == key)

    stmt = stmt.order_by(Evidence.created_at.asc(), Evidence.id.asc()).limit(_EVIDENCE_LIST_LIMIT)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _load_evidence_row(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    evidence_id: uuid.UUID,
) -> Evidence | None:
    """Load a single evidence row scoped to the caller's organisation."""
    result = await session.execute(
        select(Evidence).where(
            Evidence.organisation_id == organisation_id,
            Evidence.id == evidence_id,
        )
    )
    return result.scalar_one_or_none()


async def _load_redaction_rules(
    session: AsyncSession,
    organisation_id: uuid.UUID,
) -> list[EvidenceRedactionRule]:
    """List the org's live redaction rules, deterministically ordered."""
    result = await session.execute(
        select(EvidenceRedactionRule)
        .where(EvidenceRedactionRule.organisation_id == organisation_id)
        .order_by(EvidenceRedactionRule.key_pattern.asc(), EvidenceRedactionRule.role.asc())
    )
    return list(result.scalars().all())


async def _upsert_redaction_rule(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    key_pattern: str,
    role: str,
    redacted_fields: list[str],
) -> EvidenceRedactionRule:
    """Create or update the rule for ``(org, key_pattern, role)``.

    A soft-deleted rule with the same key is restored in place rather than
    inserted, so the table's unique constraint is never violated by a
    previously-retired rule.
    """
    existing = (
        await session.execute(
            include_soft_deleted(
                select(EvidenceRedactionRule).where(
                    EvidenceRedactionRule.organisation_id == organisation_id,
                    EvidenceRedactionRule.key_pattern == key_pattern,
                    EvidenceRedactionRule.role == role,
                )
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        rule = EvidenceRedactionRule(
            organisation_id=organisation_id,
            key_pattern=key_pattern,
            role=role,
            redacted_fields=redacted_fields,
        )
        session.add(rule)
    else:
        existing.redacted_fields = redacted_fields
        # Restore a previously soft-deleted rule (the unique constraint covers
        # org+key_pattern+role regardless of deleted_at).
        existing.deleted_at = None
        rule = existing

    await session.flush()
    return rule


def _redacted_row(
    row: Evidence,
    role: str,
    rules: list[EvidenceRedactionRule],
    action: str | None = None,
) -> EvidenceRowResponse:
    """Serialise one evidence row, resolving its classes from pre-loaded *rules*.

    Pure/synchronous: the caller loads the org's rules ONCE per request
    (:func:`load_redaction_rules`) and resolves every row from that in-memory
    list, so a page of N rows costs one rule query, not N.
    """
    classes = resolve_redaction_classes_from(rules, role, row.key)
    return EvidenceRowResponse(
        id=row.id,
        organisation_id=row.organisation_id,
        key=row.key,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        value=apply_redaction(row.value, classes),
        observed_at=row.observed_at,
        producer_type=row.producer_type,
        producer_id=row.producer_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        action=action,
    )


# ── Admin: redaction rules ────────────────────────────────────────────────


@router.get("/evidence/redaction-rules")
@handle_db_errors(_CODE_READ_REDACTION_RULES)
async def list_redaction_rules_endpoint(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_system_or_org_admin(_CODE_EVIDENCE_REDACTION_MANAGE),
) -> list[RedactionRuleResponse]:
    """List the caller org's redaction rules (admin only).

    DB failures are translated by ``@handle_db_errors``; a read cannot produce a
    rule conflict, so no local ``IntegrityError`` arm re-labels one as such.
    """
    org_id = principal.organisation_id

    async with session.begin():
        await set_rls_org(session, org_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        rules = await _load_redaction_rules(session, org_id)

    return [RedactionRuleResponse.model_validate(rule) for rule in rules]


@router.put(
    "/evidence/redaction-rules",
    dependencies=[
        Depends(
            audited(
                "evidence_redaction_rule_upserted",
                "evidence_redaction_rule",
                principal_dep=get_current_tenant_user,
            )
        )
    ],
    status_code=http_status.HTTP_200_OK,
)
@handle_db_errors(_CODE_UPSERT_REDACTION_RULE)
async def upsert_redaction_rule_endpoint(
    req: RedactionRuleUpsert,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_system_or_org_admin(_CODE_EVIDENCE_REDACTION_MANAGE),
) -> RedactionRuleResponse:
    """Create or update one redaction rule for the caller org (admin only).

    DB failures (including the unique-constraint ``IntegrityError`` that a
    concurrent upsert of the same ``(key_pattern, role)`` can raise) are
    translated to their status codes by ``@handle_db_errors`` — one shared
    mapping instead of a duplicated per-route chain.
    """
    org_id = principal.organisation_id

    async with session.begin():
        await set_rls_org(session, org_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        rule = await _upsert_redaction_rule(
            session,
            org_id,
            req.key_pattern,
            req.role,
            req.redacted_fields,
        )

    return RedactionRuleResponse.model_validate(rule)


# ── Risk / Assessment projection (FAR-941, sub-scope B) ────────────────────
# Declared BEFORE /evidence/{evidence_id} so the static path wins over the
# parameterized one (FastAPI matches routes in declaration order).


@router.get("/evidence/risk-assessment")
@handle_db_errors(_CODE_RISK_ASSESSMENT)
async def risk_assessment_endpoint(
    key_prefix: str | None = Query(
        default=None, description="Optional evidence key prefix filter (e.g. 'risk.' or 'assessment.')"
    ),
    pipeline_id: uuid.UUID | None = Query(default=None, description="Optional pipeline id filter"),
    days: int = Query(default=7, ge=1, le=365, description="Rolling time window in days (default 7)"),
    action: str | None = Query(
        default=None, description="Optional gate action filter (continue, warn, block, escalate)"
    ),
    limit: int = Query(default=100, ge=1, le=500, description="Maximum rows to return (default 100)"),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVIDENCE_READ),
) -> RiskAssessmentResponse:
    """Return a filtered, faceted projection of evidence rows for risk/assessment.

    This is a read-only derived view over the evidence store (chunk 7) — no new
    storage, no schema change, no data migration.

    Filters:
    - key_prefix: evidence keys starting with this prefix (e.g. "risk." or "assessment.")
    - pipeline_id: filter to evidence from runs belonging to this pipeline
    - days: rolling time window on evidence.created_at (default 7, max 365)
    - action: gate action parsed from subject_id via policy_gate_decisions
      (best-effort; requires policy_gate_decisions.run_id column; ignored if absent)

    The action facet degrades to absent when the run_id column does not exist
    on policy_gate_decisions — the response includes "facet_degraded": true
    and the UI should hide the action facet control.

    No outcome facet — the evidence table has no outcome column. Outcome facet
    returns with the predicate unit.
    """
    org_id = principal.organisation_id

    # Validate action parameter early so FastAPI returns 422 before DB work
    if action is not None and action not in _VALID_ACTIONS:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid action: {action}. Valid actions: {', '.join(sorted(_VALID_ACTIONS))}",
        )

    async with session.begin():
        await set_rls_org(session, org_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)

        # Load rows with redaction
        rows, facet_degraded = await _load_risk_assessment_rows(
            session,
            org_id,
            key_prefix=key_prefix,
            pipeline_id=pipeline_id,
            days=days,
            action=action,
            limit=limit,
        )

        # Load redaction rules ONCE and apply to all rows. Per-row gate actions
        # come from one decision-record query, never one query per row.
        rules = await load_redaction_rules(session, org_id)
        row_actions, actions_degraded = await _load_risk_assessment_row_actions(session, org_id, rows)
        redacted_rows = [_redacted_row(row, principal.org_role, rules, row_actions.get(row.id)) for row in rows]

        # Count total rows matching filters (without limit)
        total_rows = await _count_risk_assessment_rows(
            session,
            org_id,
            key_prefix=key_prefix,
            pipeline_id=pipeline_id,
            days=days,
            action=action,
        )

        # Count by action for summary card
        action_counts, counts_degraded = await _count_risk_assessment_by_action(
            session,
            org_id,
            key_prefix=key_prefix,
            pipeline_id=pipeline_id,
            days=days,
        )

    return RiskAssessmentResponse(
        summary=RiskAssessmentSummary(
            total_rows=total_rows,
            action_counts=action_counts,
            facet_degraded=facet_degraded or actions_degraded or counts_degraded,
        ),
        rows=redacted_rows,
    )


# ── Reads (redacted per the caller's role) ───────────────────────────────


@router.get("/evidence/{evidence_id}")
@handle_db_errors(_CODE_GET_EVIDENCE)
async def get_evidence_endpoint(
    evidence_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVIDENCE_READ),
) -> EvidenceRowResponse:
    """Return one evidence row for the caller org, redacted for their role.

    The 404 for a missing row is raised inside the body and passed through
    untouched by ``@handle_db_errors``; DB failures use the shared mapping.
    """
    org_id = principal.organisation_id

    async with session.begin():
        await set_rls_org(session, org_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        row = await _load_evidence_row(session, org_id, evidence_id)
        if row is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=_MSG_EVIDENCE_NOT_FOUND,
            )
        rules = await load_redaction_rules(session, org_id)
        return _redacted_row(row, principal.org_role, rules)


@router.get("/evidence")
@handle_db_errors(_CODE_LIST_EVIDENCE)
async def list_evidence_endpoint(
    run_id: uuid.UUID = Query(..., description="Run id whose evidence rows to list"),
    node_id: str | None = Query(default=None, description="Optional node id to include node-level evidence"),
    key: str | None = Query(default=None, description="Optional exact evidence key filter"),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVIDENCE_READ),
) -> list[EvidenceRowResponse]:
    """List a run's evidence rows for the caller org, redacted for their role.

    At most :data:`_EVIDENCE_LIST_LIMIT` rows are returned.

    The org's redaction rules are loaded ONCE per request and each row resolved
    in memory from that list — never one rule query per row. DB failures use the
    shared ``@handle_db_errors`` mapping; a read cannot produce a rule conflict.
    """
    org_id = principal.organisation_id

    async with session.begin():
        await set_rls_org(session, org_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        rows = await _load_evidence_rows(session, org_id, run_id, node_id, key)
        # Load the org's rules ONCE, then resolve each row in memory (N+1 fix).
        rules = await load_redaction_rules(session, org_id)
        return [_redacted_row(row, principal.org_role, rules) for row in rows]


def _risk_scope_clause(
    organisation_id: uuid.UUID,
    *,
    key_prefix: str | None,
    pipeline_id: uuid.UUID | None,
    days: int,
) -> Any:
    """Build the shared WHERE clause for the risk/assessment projection.

    One builder for the rows, the total and the per-action counts — the three
    queries must never drift apart (a drift shows up as a summary that
    disagrees with its own table).
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)
    clauses: list[Any] = [
        Evidence.organisation_id == organisation_id,
        Evidence.created_at >= cutoff,
    ]

    if key_prefix is not None:
        clauses.append(Evidence.key.like(f"{key_prefix}%"))

    if pipeline_id is not None:
        run_ids_subq = (
            select(Run.id)
            .where(
                Run.organisation_id == organisation_id,
                Run.pipeline_id == pipeline_id,
            )
            .subquery()
        )
        # ``evidence.subject_id`` is TEXT while ``runs.id`` is UUID, so the
        # comparison must be text-to-text (a bare ``text = uuid`` raises on
        # Postgres). Cast the run id rather than the subject id: malformed
        # subject ids must not turn a read into a 500.
        run_id_text = cast(run_ids_subq.c.id, Text)
        clauses.append(
            or_(
                and_(
                    Evidence.subject_type == "run",
                    Evidence.subject_id.in_(select(run_id_text)),
                ),
                and_(
                    Evidence.subject_type == "node_execution",
                    # Extract run_id from subject_id ("{run_id}:{node_id}")
                    func.split_part(Evidence.subject_id, ":", 1).in_(select(run_id_text)),
                ),
            )
        )

    return and_(*clauses)


def _risk_action_clause(organisation_id: uuid.UUID, action: str) -> Any:
    """WHERE clause matching evidence rows whose run resolved to *action*.

    Requires the ``policy_gate_decisions.run_id`` column (chunk 4); callers
    wrap the execute in a savepoint and degrade when that column is absent.
    """
    decision_run_ids_subq = (
        select(PolicyGateDecision.run_id)
        .where(
            PolicyGateDecision.organisation_id == organisation_id,
            PolicyGateDecision.resolved_action == action,
        )
        .distinct()
        .subquery()
    )
    # Same TEXT/UUID asymmetry as ``_risk_scope_clause``: compare text to text.
    decision_run_id_text = cast(decision_run_ids_subq.c.run_id, Text)
    return or_(
        and_(
            Evidence.subject_type == "run",
            Evidence.subject_id.in_(select(decision_run_id_text)),
        ),
        and_(
            Evidence.subject_type == "node_execution",
            func.split_part(Evidence.subject_id, ":", 1).in_(select(decision_run_id_text)),
        ),
    )


def _split_run_and_node(subject_type: str, subject_id: str) -> tuple[uuid.UUID | None, str | None]:
    """Resolve a row's run id and (for node rows) node id from its subject.

    Returns ``(None, None)`` for a malformed subject id — a data anomaly must
    not turn a read into a 500.
    """
    if subject_type == "run":
        try:
            return uuid.UUID(subject_id), None
        except ValueError:
            return None, None
    if subject_type == "node_execution":
        run_part, separator, node_part = subject_id.partition(":")
        if not separator:
            return None, None
        try:
            return uuid.UUID(run_part), node_part
        except ValueError:
            return None, None
    return None, None


async def _load_risk_assessment_rows(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    *,
    key_prefix: str | None = None,
    pipeline_id: uuid.UUID | None = None,
    days: int = 7,
    action: str | None = None,
    limit: int = _EVIDENCE_LIST_LIMIT,
) -> tuple[list[Evidence], bool]:
    """Load evidence rows for the risk/assessment projection.

    Returns (rows, facet_degraded) where facet_degraded is True if the action
    filter was requested but the policy_gate_decisions.run_id column does not
    exist (or the query failed due to a schema mismatch).

    The action-filtered execute runs inside a SAVEPOINT: on Postgres a failed
    statement aborts the enclosing transaction, so catching ``ProgrammingError``
    without a savepoint would poison the fallback query.
    """
    stmt = (
        select(Evidence)
        .where(
            _risk_scope_clause(
                organisation_id,
                key_prefix=key_prefix,
                pipeline_id=pipeline_id,
                days=days,
            )
        )
        .order_by(Evidence.created_at.desc(), Evidence.id.desc())
        .limit(limit)
    )

    if action is not None:
        try:
            async with session.begin_nested():
                result = await session.execute(stmt.where(_risk_action_clause(organisation_id, action)))
                return list(result.scalars().all()), False
        except ProgrammingError:
            # The run_id column doesn't exist on policy_gate_decisions (chunk 4
            # hasn't landed). Degrade gracefully: ignore the action filter and
            # signal the degradation to the caller.
            pass

    result = await session.execute(stmt)
    return list(result.scalars().all()), action is not None


async def _count_risk_assessment_rows(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    *,
    key_prefix: str | None = None,
    pipeline_id: uuid.UUID | None = None,
    days: int = 7,
    action: str | None = None,
) -> int:
    """Count evidence rows matching the risk/assessment filters (without limit)."""
    stmt = select(func.count(Evidence.id)).where(
        _risk_scope_clause(
            organisation_id,
            key_prefix=key_prefix,
            pipeline_id=pipeline_id,
            days=days,
        )
    )

    if action is not None:
        try:
            async with session.begin_nested():
                result = await session.execute(stmt.where(_risk_action_clause(organisation_id, action)))
                return result.scalar_one_or_none() or 0
        except ProgrammingError:
            # run_id column doesn't exist — ignore the action filter for counting
            pass

    result = await session.execute(stmt)
    return result.scalar_one_or_none() or 0


async def _count_risk_assessment_by_action(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    *,
    key_prefix: str | None = None,
    pipeline_id: uuid.UUID | None = None,
    days: int = 7,
) -> tuple[dict[str, int], bool]:
    """Count evidence rows grouped by gate action.

    Returns ``(counts, facet_degraded)``. Every action's execute is guarded
    like the other two helpers (savepoint + ``ProgrammingError``); a missing
    ``run_id`` column returns zero counts and ``facet_degraded=True``.
    """
    base_stmt = select(func.count(Evidence.id)).where(
        _risk_scope_clause(
            organisation_id,
            key_prefix=key_prefix,
            pipeline_id=pipeline_id,
            days=days,
        )
    )

    counts = dict.fromkeys(_RISK_ACTIONS, 0)
    for action in _RISK_ACTIONS:
        try:
            async with session.begin_nested():
                result = await session.execute(base_stmt.where(_risk_action_clause(organisation_id, action)))
                counts[action] = result.scalar_one_or_none() or 0
        except ProgrammingError:
            return counts, True

    return counts, False


def _collect_row_run_ids(rows: list[Evidence]) -> set[uuid.UUID]:
    """Collect the distinct run ids referenced by *rows* (malformed ids skipped)."""
    run_ids: set[uuid.UUID] = set()
    for row in rows:
        run_id, _ = _split_run_and_node(row.subject_type, row.subject_id)
        if run_id is not None:
            run_ids.add(run_id)
    return run_ids


def _aggregate_decision_actions(
    decisions: Sequence[Any],
) -> tuple[dict[uuid.UUID, str], dict[tuple[uuid.UUID, str], str]]:
    """Fold decision rows into run-level and node-level most-severe action maps."""
    run_actions: dict[uuid.UUID, str] = {}
    node_actions: dict[tuple[uuid.UUID, str], str] = {}
    for decision_run_id, decision_node_id, resolved_action in decisions:
        if decision_run_id is None or resolved_action not in _VALID_ACTIONS:
            continue
        if decision_node_id is None:
            run_actions[decision_run_id] = _most_severe(run_actions.get(decision_run_id), resolved_action)
        else:
            node_key = (decision_run_id, str(decision_node_id))
            node_actions[node_key] = _most_severe(node_actions.get(node_key), resolved_action)
    return run_actions, node_actions


def _resolve_row_actions(
    rows: list[Evidence],
    run_actions: dict[uuid.UUID, str],
    node_actions: dict[tuple[uuid.UUID, str], str],
) -> dict[uuid.UUID, str]:
    """Map each row id to its node action (when present) else its run action."""
    resolved: dict[uuid.UUID, str] = {}
    for row in rows:
        run_id, node_id = _split_run_and_node(row.subject_type, row.subject_id)
        if run_id is None:
            continue
        action = node_actions.get((run_id, node_id)) if node_id is not None else None
        action = action or run_actions.get(run_id)
        if action is not None:
            resolved[row.id] = action
    return resolved


async def _load_risk_assessment_row_actions(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    rows: list[Evidence],
) -> tuple[dict[uuid.UUID, str], bool]:
    """Resolve each row's gate action from ``policy_gate_decisions``.

    One query for the whole page (never one per row). Returns
    ``(row_id -> action, facet_degraded)``; a row with no decision is absent
    from the map so the UI renders an explicit unknown rather than a
    fabricated value.
    """
    run_ids = _collect_row_run_ids(rows)
    if not run_ids:
        return {}, False

    stmt = select(
        PolicyGateDecision.run_id,
        PolicyGateDecision.node_id,
        PolicyGateDecision.resolved_action,
    ).where(
        PolicyGateDecision.organisation_id == organisation_id,
        PolicyGateDecision.run_id.in_(run_ids),
    )

    try:
        async with session.begin_nested():
            result = await session.execute(stmt)
            decisions = result.all()
    except ProgrammingError:
        return {}, True

    run_actions, node_actions = _aggregate_decision_actions(decisions)
    return _resolve_row_actions(rows, run_actions, node_actions), False


def _most_severe(current: str | None, candidate: str) -> str:
    """Return the more severe of two gate actions (block > escalate > warn > continue)."""
    if current is None:
        return candidate
    return current if _ACTION_SEVERITY[current] <= _ACTION_SEVERITY[candidate] else candidate
