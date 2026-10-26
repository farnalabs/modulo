"""Compliance control-mapping pack and per-instance evidence index API (FAR-2036).

Endpoints
---------
GET /api/v1/compliance/evidence-index   the org's evidence artefacts, keyed to
                                        the mapping rows they satisfy

The endpoint optionally takes ``from`` / ``to`` (ISO datetimes) to scope every
artefact aggregate to an audit period; the response records the period, and an
unscoped request is explicitly the organisation's FULL HISTORY (FAR-2055).

The organisation is always taken from the authenticated principal, never from
query input. The route runs inside ``session.begin()`` with the RLS org and
user context set, and the generator itself also filters every query by
``organisation_id`` - the tenant boundary does not depend on either layer
alone.

On Postgres the transaction also carries a bounded ``statement_timeout``: the
index runs org-wide aggregate scans, and a mature instance must degrade to an
actionable error rather than pin a pooled connection (FAR-2055). The bound is
transaction-local (``set_config(..., true)``), so it cannot leak to the next
request on a pooled connection.

DB failures are translated by ``@handle_db_errors`` (ProgrammingError to 501,
SQLAlchemyError to 503, IntegrityError to 409, HTTPException re-raised,
Exception to 500). A ``statement_timeout`` expiry (SQLSTATE 57014) is answered
by this module's own arm as a 503 carrying the audit-period remedy, preceded by
a structured ``service_unavailable`` record with ``reason=statement_timeout``
so a persistently slow instance is visible in the error trail rather than only
to the caller.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.db_error_reporting import log_service_unavailable
from modulo.api.dependencies import get_db_session, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.compliance.evidence_index import (
    EvidenceIndex,
    build_evidence_index,
    normalise_audit_period,
)

# Imported by its private name because it is the single source of the
# transaction-local statement_timeout SQL fragment; the value has no shared
# home yet and the duplicate-logic baseline is shrink-only (FAR-2055).
from modulo.core.analytics.guardrails import _SQL_SET_STATEMENT_TIMEOUT
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.db.sqlstates import is_statement_timeout

router = APIRouter(prefix="/api/v1/compliance", tags=["compliance"])

#: Wall-clock bound for the evidence-index aggregate scans (FAR-2055). The
#: queries are index-served, but the org row count is still unbounded from the
#: endpoint's point of view; 30s is generous for the indexed shapes and short
#: enough to fail fast instead of holding a connection.
_EVIDENCE_INDEX_STATEMENT_TIMEOUT_MS = 30_000

_MSG_TIMEOUT = (
    "The compliance evidence index exceeded its time bound. "
    "Narrow the audit period with the 'from' and 'to' parameters and retry."
)

#: Structured-log / log-prefix key for this route. One spelling, used by BOTH
#: the ``@handle_db_errors`` backstop and the timeout arm's own record, so the
#: two cannot drift apart in the persisted ``service_unavailable`` trail.
_LOG_PREFIX = "compliance.evidence_index"

#: Admin-only: the index aggregates org-wide audit evidence. See the
#: registration comment in ``modulo.auth.permissions``.
_CODE_EVIDENCE_INDEX_READ = "compliance.evidence_index.read"


async def _bound_statement_timeout(session: AsyncSession) -> None:
    """Cap this transaction's statement execution on Postgres.

    SQLite (unit tests) has no ``statement_timeout``; the guard keeps the
    transaction-local ``set_config`` Postgres-only. ``set_config(..., true)``
    scopes the value to the current transaction, so a pooled connection
    returning to the pool cannot carry it into another request.
    """
    dialect = (await session.connection()).dialect.name
    if dialect != "postgresql":
        return
    await session.execute(
        text(_SQL_SET_STATEMENT_TIMEOUT),
        {"ms": str(_EVIDENCE_INDEX_STATEMENT_TIMEOUT_MS)},
    )


@router.get("/evidence-index")
@handle_db_errors(_LOG_PREFIX)
async def get_evidence_index_endpoint(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: Annotated[TenantPrincipal, require_permission(_CODE_EVIDENCE_INDEX_READ)],
    period_from: Annotated[
        datetime | None,
        Query(
            alias="from",
            description=(
                "Audit period start (inclusive, ISO 8601). Omit for the organisation's full history. "
                "The period is half-open: [from, to)."
            ),
        ),
    ] = None,
    period_to: Annotated[
        datetime | None,
        Query(
            alias="to",
            description=(
                "Audit period end (EXCLUSIVE, ISO 8601). Omit for the organisation's full history. "
                "The period is half-open: [from, to)."
            ),
        ),
    ] = None,
) -> EvidenceIndex:
    """Return this organisation's compliance evidence index.

    ``from`` / ``to`` optionally scope every artefact aggregate to an audit
    period recorded in the response. With neither supplied the index covers the
    organisation's FULL HISTORY - the response then carries
    ``period_from``/``period_to`` of null, so a reader can always tell a scoped
    index from a full-history one. The organisation itself still comes from the
    authenticated principal, never from a parameter.

    The response lists one entry per artefact TYPE THE INSTANCE PRODUCES - the
    audit chain, run snapshots, HITL decisions, eval results, autonomy changes,
    the release SBOM and the build identity. It does not list every artefact the
    pack maps: a row resting on ``customer_records`` (policies, risk assessments,
    internal-audit reports, incident and restore-test records) is the customer's
    own evidence, produced outside Modulo, and the index produces no entry for
    that type - so those rows are not returned here in any form. The shipped
    mapping documentation (``docs/security/compliance-control-mapping.md``)
    lists them for the customer instead.

    An indexed artefact type with no records is returned with
    ``status: absent`` and a reason, and is named in
    ``missing_artifact_types`` so the caller can see the index is incomplete -
    it is never silently dropped. ``pack_version`` and ``pack_content_hash``
    record which pack produced the index, so a later release cannot silently
    change what was asserted for a past audit period.

    Coverage is per row and per framework; the response deliberately carries no
    single compliant/not-compliant verdict.
    """
    organisation_id = principal.organisation_id
    try:
        period_from, period_to = normalise_audit_period(period_from, period_to)
    except ValueError as exc:
        # A start after the end is a malformed request, not a server fault.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None

    try:
        async with session.begin():
            await set_rls_org(session, organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            await _bound_statement_timeout(session)
            return await build_evidence_index(
                session,
                organisation_id,
                period_from=period_from,
                period_to=period_to,
            )
    except DBAPIError as exc:
        if is_statement_timeout(exc):
            # Operationally visible, not just a client-facing 503: an instance
            # whose evidence index routinely exceeds the bound is a capacity
            # signal (an unbounded org, or a missing index), and the generic
            # DB path's ``log_service_unavailable("db_transient", ...)`` never
            # runs for this arm because it answers before that backstop. The
            # distinct reason code keeps a timeout queryable separately from a
            # genuine database outage in the persisted trail.
            log_service_unavailable(
                "statement_timeout",
                exc,
                route=_LOG_PREFIX,
                detail=f"evidence index exceeded its {_EVIDENCE_INDEX_STATEMENT_TIMEOUT_MS} ms statement_timeout bound",
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=_MSG_TIMEOUT,
            ) from None
        raise
