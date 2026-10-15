"""CRUD for Organisation records."""

import json
import logging
import uuid
from typing import Any

from sqlalchemy import ARRAY, Text, bindparam, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key

from modulo.db.crud.base import apply_updates
from modulo.db.models.organisation import (
    MODULO_REGISTRY_ORG_ID,
    SYSTEM_ORG_ID,
    Organisation,
)
from modulo.db.seed import seed_system_schemas

_log = logging.getLogger(__name__)

# The RLS tenant key generic backends (SQLite/MariaDB) store in ``session.info``
# — mirrors ``modulo.db.rls._TENANT_KEY`` (kept local so the strict read here
# does not depend on the module-private constant).
_RLS_TENANT_KEY = "org_id"


class OrgSettingsPatchOrgMismatch(RuntimeError):  # noqa: N818  # matches the OutputsRlsMismatch sibling naming
    """The session's bound RLS org disagrees with the patch target organisation.

    ``organisations`` is not itself RLS-scoped, so a caller bound to one org
    could otherwise issue a settings patch for another. This is a programming
    error (wrong org bound, or a route that failed to set ``set_rls_org``): the
    write is refused rather than silently mutating the wrong tenant.
    """


class OrgSettingsPatchContextError(RuntimeError):
    """Base class for a settings patch whose RLS org context cannot be trusted.

    FAR-1830: the tenant guard must FAIL CLOSED. ``db.rls.read_rls_org``
    degrades every failure to ``None`` (a deliberate public contract with many
    callers), so it cannot distinguish "no org bound" from "the bound context
    could not be read". Either way the tenant boundary is unknown, so the patch
    refuses to write. The two cases are distinct subclasses so a caller can tell
    them apart.
    """


class OrgSettingsPatchContextAbsentError(OrgSettingsPatchContextError):
    """No org RLS context is bound and the caller did not opt out.

    A user-facing writer that forgot ``set_rls_org`` lands here rather than
    silently writing across the tenant boundary.
    """


class OrgSettingsPatchContextUnreadableError(OrgSettingsPatchContextError):
    """The session's bound org context could not be read.

    The context READ failed (e.g. the GUC read raised) — not the same as an
    unbound context, and never treated as "unbound / allowed".
    """


# Sentinel org IDs that must never resolve a login page.
_SENTINEL_ORG_IDS: frozenset[uuid.UUID] = frozenset({SYSTEM_ORG_ID, MODULO_REGISTRY_ORG_ID})


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
        #
        # FAR-1624 org-lock audit: intentionally plain `FOR UPDATE`, NOT `FOR NO
        # KEY UPDATE` — callers take the org lock FIRST (no pre-held FK `KEY
        # SHARE` on the org), so they cannot hit the KEY-SHARE->FOR-UPDATE
        # upgrade cycle.
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


async def update_org_settings_patch(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    patch: dict[str, Any] | None = None,
    delete_keys: list[str] | None = None,
    *,
    allow_unbound_context: bool = False,
) -> dict[str, Any]:
    """Atomically merge (and/or remove) top-level keys of ``settings_json`` (FAR-1830).

    Several independent concerns persist their state in
    ``organisations.settings_json`` under their own top-level key (email config,
    ``logo_url``, ``cost_controls``, ``run_concurrency_limit``, ``demo``,
    ``license_key``, ``feature_overrides``, ``retention_days``, ``evidence_retention``...).
    The old write pattern — read the whole column, merge one key in Python,
    write the WHOLE column back via :func:`update_organisation` — takes no lock,
    so any two writers overlapping in their read→write window silently drop each
    other's keys. This helper replaces that read-modify-write with a single
    atomic statement:

    - Postgres (the supported backend): ``UPDATE organisations SET
      settings_json = (COALESCE(settings_json, '{}'::jsonb) || :patch::jsonb) -
      :delete_keys::text[] WHERE id = :org_id AND deleted_at IS NULL RETURNING
      settings_json`` — the read and write are one statement, so no interleaving
      writer can be lost.
    - Generic backends (SQLite / MariaDB, tests + legacy): an equivalent
      read-merge-write under ``SELECT ... FOR UPDATE``. Dialect-gated so the
      JSONB operators never reach a backend that lacks them.

    Contract:

    - A soft-deleted org (``deleted_at IS NOT NULL``) is refused with
      :class:`LookupError` on BOTH paths — the raw Postgres statement carries the
      same ``deleted_at IS NULL`` predicate the generic ORM read applies via the
      global soft-delete filter. (A raw statement does not pass through the ORM
      listener, so the predicate is explicit.)
    - The caller owns the transaction and MUST have set the org RLS context
      (``set_rls_org``) for this org — the same contract as every other
      ``settings_json`` writer. The tenant guard FAILS CLOSED: a session bound to
      a DIFFERENT org is refused (:class:`OrgSettingsPatchOrgMismatch`), and a
      session with NO readable org context is refused too —
      :class:`OrgSettingsPatchContextAbsentError` when genuinely unbound,
      :class:`OrgSettingsPatchContextUnreadableError` when the context read itself
      failed. Only a genuine boot-seed / system caller may pass
      ``allow_unbound_context=True`` to permit a genuinely-unbound (but
      readable) context; a failed read is ALWAYS refused.
    - The lock wait is bounded via ``set_mutation_row_lock_timeout``.
    - Any in-session ``Organisation`` for this id is refreshed to the merged
      value WITHOUT dirtying it (``set_committed_value``), so callers read the
      patched settings without emitting a follow-up whole-column write.
    - The in-process settings-derived flag cache is invalidated so a patch that
      changes a cached flag cannot serve a stale value.

    Returns the refreshed ``settings_json`` mapping.
    """
    patch = patch or {}
    delete_keys = list(delete_keys or [])

    # Normalise the target to a ``UUID``: callers occasionally pass the id as a
    # string, and both the tenant-mismatch comparison and the bound SQL /
    # ORM predicate must compare like-for-like (a ``UUID`` never equals its
    # ``str`` form).
    target_org_id = _coerce_org_uuid(organisation_id)
    if target_org_id is None:
        raise ValueError(f"update_org_settings_patch requires a UUID organisation id, got {organisation_id!r}")

    await _take_settings_patch_lock_wait(session)
    dialect = await _settings_patch_dialect(session)
    bound_org = await _strict_read_rls_org(session, dialect)
    if bound_org is None:
        if not allow_unbound_context:
            raise OrgSettingsPatchContextAbsentError(
                "no org RLS context bound for a settings_json patch; call set_rls_org() first "
                "(only genuine boot-seed/system callers may pass allow_unbound_context=True)"
            )
    elif bound_org != target_org_id:
        raise OrgSettingsPatchOrgMismatch(
            f"settings_json patch org mismatch: session org {bound_org} != target org {target_org_id}"
        )

    if dialect == "postgresql":
        settings = await _apply_settings_patch_postgres(session, target_org_id, patch, delete_keys)
    else:
        settings = await _apply_settings_patch_generic(session, target_org_id, patch, delete_keys)

    _invalidate_settings_derived_caches()
    return settings


async def _strict_read_rls_org(session: AsyncSession, dialect: str) -> uuid.UUID | None:
    """Read the session's bound RLS org, NEVER swallowing a genuine read failure.

    ``db.rls.read_rls_org`` degrades every failure to ``None`` (its public
    contract, relied on by many callers). The tenancy guard here needs to tell
    "no org bound" apart from "the bound context could not be read", so it uses
    this strict local read instead: a real failure raises
    :class:`OrgSettingsPatchContextUnreadableError`; only a genuinely-absent context
    returns ``None``.

    Generic backends store the org in ``session.info['org_id']`` and have no
    ``app.organisation_id`` GUC, so on a non-Postgres dialect the info lookup is
    authoritative and its absence is genuinely-unbound (not a read error). On
    Postgres the ``current_setting`` read is authoritative.
    """
    info = getattr(session, "info", None)
    if isinstance(info, dict):
        value = info.get(_RLS_TENANT_KEY)
        if value is not None:
            parsed = _coerce_org_uuid(value)
            if parsed is not None:
                return parsed
    if dialect != "postgresql":
        return None
    try:
        result = await session.execute(text("SELECT current_setting('app.organisation_id', true)"))
    except Exception as exc:
        raise OrgSettingsPatchContextUnreadableError(
            "could not read the session's app.organisation_id RLS context"
        ) from exc
    raw: object = result.scalar()
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip() == "":
        return None
    return _coerce_org_uuid(raw)


def _coerce_org_uuid(value: object) -> uuid.UUID | None:
    """Return a ``UUID`` when *value* is a valid UUID/str, else ``None``."""
    try:
        return uuid.UUID(str(value).strip())
    except (ValueError, AttributeError, TypeError):
        return None


async def _take_settings_patch_lock_wait(session: AsyncSession) -> None:
    """Bound the row-lock wait for the rest of the transaction (FAR-1313)."""
    from modulo.db.crud.row_lock import set_mutation_row_lock_timeout

    await set_mutation_row_lock_timeout(session)


async def _settings_patch_dialect(session: AsyncSession) -> str:
    from modulo.db.crud.run import get_dialect_name

    return await get_dialect_name(session)


async def _apply_settings_patch_postgres(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    patch: dict[str, Any],
    delete_keys: list[str],
) -> dict[str, Any]:
    stmt = text(
        "UPDATE organisations "
        "SET settings_json = "
        "(COALESCE(settings_json, '{}'::jsonb) || CAST(:patch AS jsonb)) "
        "- CAST(:delete_keys AS text[]) "
        "WHERE id = :org_id AND deleted_at IS NULL "
        "RETURNING settings_json"
    ).bindparams(
        bindparam("patch", value=json.dumps(patch)),
        bindparam("delete_keys", value=delete_keys, type_=ARRAY(Text)),
    )
    result = await session.execute(stmt, {"org_id": str(organisation_id)})
    row = result.first()
    if row is None:
        # No row matched: either the org does not exist OR it is soft-deleted.
        # The generic path (get_organisation) applies the global soft-delete
        # filter, so this matches its behaviour (both refuse a soft-deleted org).
        raise LookupError(f"organisation not found: {organisation_id}")
    raw = row[0]
    settings = json.loads(raw) if isinstance(raw, str) else dict(raw)
    _sync_identity_map(session, organisation_id, settings)
    return settings


async def _apply_settings_patch_generic(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    patch: dict[str, Any],
    delete_keys: list[str],
) -> dict[str, Any]:
    org = await get_organisation(session, organisation_id, for_update=True)
    if org is None:
        raise LookupError(f"organisation not found: {organisation_id}")
    settings = dict(org.settings_json) if isinstance(org.settings_json, dict) else {}
    settings.update(patch)
    for key in delete_keys:
        settings.pop(key, None)
    org.settings_json = settings
    await session.flush()
    return settings


def _sync_identity_map(session: AsyncSession, organisation_id: uuid.UUID, settings: dict[str, Any]) -> None:
    """Refresh an already-loaded org row to the merged value without dirtying it."""
    identity_map = getattr(session, "identity_map", None)
    getter = getattr(identity_map, "get", None)
    if getter is None:
        return
    org = getter(identity_key(Organisation, organisation_id))
    if isinstance(org, Organisation):
        set_committed_value(org, "settings_json", settings)


def _invalidate_settings_derived_caches() -> None:
    """Drop in-process caches derived from ``settings_json``.

    ``db.crud.run`` mirrors the org agent-minting flag into a short-TTL
    in-process cache (it cannot import ``core`` under the db→core import
    contract), so a settings patch must clear it or a flag toggled here keeps
    serving the stale value for up to the TTL. Lazy import: ``db.crud.run``
    imports ``get_organisation`` from this module, so a module-level import
    would be circular.
    """
    from modulo.db.crud.run import clear_agent_mint_flag_cache

    clear_agent_mint_flag_cache()


# ---------------------------------------------------------------------------
# Login-active predicate (FAR-856)
# ---------------------------------------------------------------------------

# An org counts for login when ALL of:
#   - status == 'active'
#   - deleted_at IS NULL
#   - id is not a sentinel (SYSTEM_ORG_ID, MODULO_REGISTRY_ORG_ID)


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
