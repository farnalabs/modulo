"""CRUD for Organisation records."""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import apply_updates
from modulo.db.models.organisation import (
    MODULO_REGISTRY_ORG_ID,
    ORPHAN_ORG_ID,
    Organisation,
)
from modulo.db.seed import seed_system_schemas

_log = logging.getLogger(__name__)

# Sentinel org IDs that must never resolve a login page.
_SENTINEL_ORG_IDS: frozenset[uuid.UUID] = frozenset({ORPHAN_ORG_ID, MODULO_REGISTRY_ORG_ID})


async def get_organisation(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> Organisation | None:
    stmt = select(Organisation).where(Organisation.id == org_id)
    if for_update:
        # Caller is about to read-modify-write this row (e.g. license rotation).
        # Lock it for the remainder of the transaction so a concurrent writer
        # cannot interleave between the read and the write (TOCTOU). No-op on
        # dialects without SELECT ... FOR UPDATE (SQLite).
        stmt = stmt.with_for_update()
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def create_organisation(
    session: AsyncSession,
    *,
    name: str,
    slug: str,
    plan_id: str | None = None,
    created_by: uuid.UUID | None = None,
) -> Organisation:
    org = Organisation(
        name=name,
        slug=slug,
        plan_id=plan_id,
        created_by=created_by,
    )
    session.add(org)
    await session.flush()
    await session.refresh(org)

    # Seed system schemas for the new organisation.
    if created_by is not None:
        try:
            await seed_system_schemas(session, org.id, created_by)
        except Exception:
            _log.warning("seed.system_schemas_failed_for_new_org", exc_info=True)
        try:
            from modulo.db.seed import seed_bundled_runner_profile

            await seed_bundled_runner_profile(session, org.id, created_by)
        except Exception:
            _log.warning("seed.bundled_runner_profile_failed_for_new_org", exc_info=True)

    return org


async def get_organisation_by_slug(
    session: AsyncSession,
    slug: str,
) -> Organisation | None:
    # ``organisations.slug`` is a partial UNIQUE index (``WHERE deleted_at IS
    # NULL``) so a soft-deleted org's slug may be reused. A live lookup must
    # ignore soft-deleted rows; otherwise a create would 409 against a slug that
    # is free to reuse, and a duplicate slug materialising would make
    # ``scalar_one_or_none`` raise ``MultipleResultsFound`` -> 500.
    stmt = select(Organisation).where(Organisation.slug == slug).where(Organisation.deleted_at.is_(None)).limit(1)
    result = await session.execute(stmt)
    return result.scalars().first()


async def list_organisations(
    session: AsyncSession,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[Organisation]:
    try:
        result = await session.execute(
            select(Organisation).order_by(Organisation.created_at.desc()).offset(offset).limit(limit)
        )
        return list(result.scalars().all())
    except ProgrammingError:
        return []


async def delete_organisation(
    session: AsyncSession,
    org_id: uuid.UUID,
) -> bool:
    org = await get_organisation(session, org_id)
    if org is None:
        return False
    # FAR-592 (D6): agent_runner_bindings model_backends FK is ON DELETE
    # RESTRICT; the agent cascade removes most rows, but a binding row that
    # still references a live org backend would abort the hard org delete.
    # Delete the binding rows explicitly FIRST so teardown is unconditional.
    from modulo.db.crud.agent_runner_binding import delete_org_binding_rows
    from modulo.db.crud.policy_gate_decision import purge_org_decision_records
    from modulo.db.rls import set_rls_execution_context, set_rls_org

    await set_rls_org(session, org_id)
    await set_rls_execution_context(session)
    await delete_org_binding_rows(session, org_id)

    # FAR-1102: purge governance decision records BEFORE the hard-delete.
    # The RESTRICT FK chain (pipelines -> evals -> policy_gates ->
    # policy_gate_decisions) means hard-deleting the org with decision rows
    # present raises IntegrityError → raw 500.  Shared helper with Path 3
    # (org_deletion.py confirm_org_deletion).
    await purge_org_decision_records(session, org_id)

    await session.delete(org)
    await session.flush()
    return True


async def update_organisation(
    session: AsyncSession,
    org_id: uuid.UUID,
    updates: dict[str, object],
) -> Organisation | None:
    org = await get_organisation(session, org_id)
    if org is None:
        return None
    apply_updates(org, updates)
    await session.flush()
    return org


# ---------------------------------------------------------------------------
# Login-active predicate (FAR-856)
# ---------------------------------------------------------------------------

# An org counts for login when ALL of:
#   - status == 'active'
#   - deleted_at IS NULL
#   - id is not a sentinel (ORPHAN_ORG_ID, MODULO_REGISTRY_ORG_ID)


def is_login_active_org(org: Organisation) -> bool:
    """Return whether *org* counts for login resolution.

    Reused by the login-context and org-login endpoints so the predicate
    is single-sourced and cannot drift between read sites.
    """
    return org.status == "active" and org.deleted_at is None and org.id not in _SENTINEL_ORG_IDS


async def list_login_active_orgs(session: AsyncSession) -> list[Organisation]:
    """Return every login-active organisation (for login-context resolution).

    The query applies the same predicate as ``is_login_active_org`` at the
    SQL level so the DB does the filtering — the ORM-level predicate is a
    second safety net, not the primary filter.
    """
    stmt = (
        select(Organisation)
        .where(
            Organisation.status == "active",
            Organisation.deleted_at.is_(None),
            Organisation.id.notin_(_SENTINEL_ORG_IDS),
        )
        .order_by(Organisation.created_at)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_login_active_org_by_slug(session: AsyncSession, slug: str) -> Organisation | None:
    """Resolve a single login-active org by exact slug.

    Returns ``None`` for unknown, soft-deleted, suspended, or sentinel orgs.
    The uniform None for all non-login-active cases is deliberate — callers
    must not reveal whether the slug exists.
    """
    stmt = (
        select(Organisation)
        .where(
            Organisation.slug == slug,
            Organisation.status == "active",
            Organisation.deleted_at.is_(None),
            Organisation.id.notin_(_SENTINEL_ORG_IDS),
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalars().first()
