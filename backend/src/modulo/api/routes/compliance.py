"""Compliance control-mapping pack and per-instance evidence index API (FAR-2036).

Endpoints
---------
GET /api/v1/compliance/evidence-index   the org's evidence artefacts, keyed to
                                        the mapping rows they satisfy

The organisation is always taken from the authenticated principal, never from
query input. The route runs inside ``session.begin()`` with the RLS org and
user context set, and the generator itself also filters every query by
``organisation_id`` - the tenant boundary does not depend on either layer
alone.

DB failures are translated by ``@handle_db_errors`` (ProgrammingError to 501,
SQLAlchemyError to 503, IntegrityError to 409, HTTPException re-raised,
Exception to 500).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.compliance.evidence_index import EvidenceIndex, build_evidence_index
from modulo.db.rls import set_rls_org, set_rls_user_context

router = APIRouter(prefix="/api/v1/compliance", tags=["compliance"])

#: Admin-only: the index aggregates org-wide audit evidence. See the
#: registration comment in ``modulo.auth.permissions``.
_CODE_EVIDENCE_INDEX_READ = "compliance.evidence_index.read"


@router.get("/evidence-index", response_model=EvidenceIndex)
@handle_db_errors("compliance.evidence_index")
async def get_evidence_index_endpoint(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_EVIDENCE_INDEX_READ),
) -> EvidenceIndex:
    """Return this organisation's compliance evidence index.

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

    async with session.begin():
        await set_rls_org(session, organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        return await build_evidence_index(session, organisation_id)
