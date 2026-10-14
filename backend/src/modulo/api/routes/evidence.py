"""Evidence read surface + server-side field-level redaction boundary (FAR-944).

Implements chunk 9b §2 sub-scope A: a read-only view over the generic
``evidence`` store (chunk 7) with an admin-editable redaction boundary
(§2.2-§2.4). Redaction is applied SERVER-SIDE before serialisation — the
frontend renders the payload as-is and never filters fields itself.

Endpoints
---------
GET  /api/v1/evidence                    list run evidence rows (redacted)
GET  /api/v1/evidence/{evidence_id}      one evidence row (redacted)
GET  /api/v1/evidence/redaction-rules    list the org's rules (admin)
PUT  /api/v1/evidence/redaction-rules    upsert a rule (admin)

All reads are org-scoped through RLS (``set_rls_org`` +
``set_rls_user_context`` inside the transaction); the organisation is always
taken from the principal, never from client input.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status  # nosemgrep: loopvar-shadows-import
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_UNEXPECTED_ERROR
from modulo.api.db_error_handling import raise_session_contract_error
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
    resolve_redaction_classes,
)
from modulo.db.models.evidence import Evidence
from modulo.db.models.evidence_redaction_rule import EvidenceRedactionRule
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.db.soft_delete import include_soft_deleted

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["evidence"])

_CODE_EVIDENCE_READ = "evidence.read"
_CODE_EVIDENCE_REDACTION_MANAGE = "evidence.redaction.manage"
_CODE_READ_REDACTION_RULES = "evidence.read_redaction_rules"
_CODE_UPSERT_REDACTION_RULE = "evidence.upsert_redaction_rule"
_CODE_LIST_EVIDENCE = "evidence.list"
_CODE_GET_EVIDENCE = "evidence.get"

_MSG_MIGRATIONS_REQUIRED = "Evidence is not available. Run database migrations to enable it."
_MSG_DB_UNAVAILABLE = "Evidence is temporarily unavailable."
_MSG_EVIDENCE_NOT_FOUND = "Evidence row not found"
_MSG_RULE_CONFLICT = "A redaction rule for this key pattern and role already exists."


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
    """One evidence row with its ``value`` already redacted for the caller's role."""

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


# ── DB helpers (module-level so route tests can patch them individually) ──


async def _load_evidence_rows(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    run_id: uuid.UUID,
    node_id: str | None,
    key: str | None,
) -> list[Evidence]:
    """Load the run's evidence rows, optionally narrowed to a node / key.

    With no ``node_id`` this selects the run-level rows (``subject_id`` == the
    run id). With a ``node_id`` it additionally includes the composite
    ``node_execution`` rows for that node (``subject_id`` == ``"{run_id}:{node_id}"``).
    """
    if node_id is None:
        subject_predicate = Evidence.subject_id == str(run_id)
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

    stmt = stmt.order_by(Evidence.created_at.asc(), Evidence.id.asc())
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


async def _redacted_row(
    session: AsyncSession,
    row: Evidence,
    organisation_id: uuid.UUID,
    role: str,
) -> EvidenceRowResponse:
    """Serialise one evidence row with its ``value`` redacted for *role*."""
    classes = await resolve_redaction_classes(session, organisation_id, role, row.key)
    return EvidenceRowResponse(
        id=row.id,
        organisation_id=row.organisation_id,
        key=row.key,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        value=apply_redaction(row.value, role, classes),
        observed_at=row.observed_at,
        producer_type=row.producer_type,
        producer_id=row.producer_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# ── Admin: redaction rules (declared BEFORE /evidence/{evidence_id}) ──────


@router.get("/evidence/redaction-rules")
async def list_redaction_rules_endpoint(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_system_or_org_admin(_CODE_EVIDENCE_REDACTION_MANAGE),
) -> list[RedactionRuleResponse]:
    """List the caller org's redaction rules (admin only)."""
    org_id = principal.organisation_id

    try:
        async with session.begin():
            await set_rls_org(session, org_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            rules = await _load_redaction_rules(session, org_id)
    except IntegrityError:
        logger.exception(_CODE_READ_REDACTION_RULES)
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=_MSG_RULE_CONFLICT,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_READ_REDACTION_RULES)
        raise HTTPException(
            status_code=http_status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_MIGRATIONS_REQUIRED,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evidence.list_redaction_rules_endpoint")
        logger.exception(_CODE_READ_REDACTION_RULES)
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DB_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(_CODE_READ_REDACTION_RULES)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

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
async def upsert_redaction_rule_endpoint(
    req: RedactionRuleUpsert,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_system_or_org_admin(_CODE_EVIDENCE_REDACTION_MANAGE),
) -> RedactionRuleResponse:
    """Create or update one redaction rule for the caller org (admin only)."""
    org_id = principal.organisation_id

    try:
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
    except IntegrityError:
        logger.exception(_CODE_UPSERT_REDACTION_RULE)
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=_MSG_RULE_CONFLICT,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_UPSERT_REDACTION_RULE)
        raise HTTPException(
            status_code=http_status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_MIGRATIONS_REQUIRED,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evidence.upsert_redaction_rule_endpoint")
        logger.exception(_CODE_UPSERT_REDACTION_RULE)
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DB_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(_CODE_UPSERT_REDACTION_RULE)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    return RedactionRuleResponse.model_validate(rule)


# ── Reads (redacted per the caller's role) ───────────────────────────────


@router.get("/evidence/{evidence_id}")
async def get_evidence_endpoint(
    evidence_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVIDENCE_READ),
) -> EvidenceRowResponse:
    """Return one evidence row for the caller org, redacted for their role."""
    org_id = principal.organisation_id

    try:
        async with session.begin():
            await set_rls_org(session, org_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            row = await _load_evidence_row(session, org_id, evidence_id)
            if row is None:
                raise HTTPException(
                    status_code=http_status.HTTP_404_NOT_FOUND,
                    detail=_MSG_EVIDENCE_NOT_FOUND,
                )
            response = await _redacted_row(session, row, org_id, principal.org_role)
    except IntegrityError:
        logger.exception(_CODE_GET_EVIDENCE)
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=_MSG_RULE_CONFLICT,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_GET_EVIDENCE)
        raise HTTPException(
            status_code=http_status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_MIGRATIONS_REQUIRED,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evidence.get_evidence_endpoint")
        logger.exception(_CODE_GET_EVIDENCE)
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DB_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(_CODE_GET_EVIDENCE)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    return response


@router.get("/evidence")
async def list_evidence_endpoint(
    run_id: uuid.UUID = Query(..., description="Run id whose evidence rows to list"),
    node_id: str | None = Query(default=None, description="Optional node id to include node-level evidence"),
    key: str | None = Query(default=None, description="Optional exact evidence key filter"),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVIDENCE_READ),
) -> list[EvidenceRowResponse]:
    """List a run's evidence rows for the caller org, redacted for their role."""
    org_id = principal.organisation_id

    try:
        async with session.begin():
            await set_rls_org(session, org_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            rows = await _load_evidence_rows(session, org_id, run_id, node_id, key)
            responses = [await _redacted_row(session, row, org_id, principal.org_role) for row in rows]
    except IntegrityError:
        logger.exception(_CODE_LIST_EVIDENCE)
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=_MSG_RULE_CONFLICT,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_LIST_EVIDENCE)
        raise HTTPException(
            status_code=http_status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_MIGRATIONS_REQUIRED,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "evidence.list_evidence_endpoint")
        logger.exception(_CODE_LIST_EVIDENCE)
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DB_UNAVAILABLE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(_CODE_LIST_EVIDENCE)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    return responses
