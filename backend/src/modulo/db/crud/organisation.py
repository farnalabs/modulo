"""CRUD for Organisation records."""

import json
import logging
import uuid
from typing import Any

from sqlalchemy import ARRAY, Text, TextClause, bindparam, select, text
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

# The (column, require_live_row) POLICY TABLE (FAR-1832 QA F13a) — ONE source
# of truth shared by both dialects. ``settings_json`` MUST refuse a soft-deleted
# org (require_live_row=True); the FAR-1025 OTel column MUST stay writable
# during the pending-deletion window (require_live_row=False). Validating the
# pair here means the generic path can no longer accept the forbidden
# ``("settings_json", False)`` combination that writes through
# ``include_soft_deleted`` — exactly the soft-deleted-org settings write
# FAR-1830 forbids.
_VALID_JSON_COLUMN_LIVE_ROW: frozenset[tuple[str, bool]] = frozenset(
    {("settings_json", True), ("otel_config_json", False)}
)


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
    """Apply whole-column ORM updates to an organisation row.

    FAR-1832: ``settings_json`` is FORBIDDEN here, and (QA F14) so is the twin
    tenant JSON column ``otel_config_json``. Writing a whole JSON column through
    this generic helper is the lost-update shape the atomic patch replaced (read
    the column, merge one key in Python, write the WHOLE column back — dropping
    every concurrent writer's keys committed in the window). Settings concerns
    write their own top-level key through :func:`update_org_settings_patch` and
    the OTel writer through ``db.crud.observability.update_otel_config``.
    Raising here rather than silently routing the write makes the dumper shape
    impossible to reintroduce by copy-paste; legitimate non-JSON callers
    (``name``, ``plan_id``, ...) are unaffected.
    """
    if "settings_json" in updates:
        raise ValueError(
            "update_organisation cannot write settings_json (whole-column settings writes lose "
            "concurrent writers' keys); use update_org_settings_patch(session, org_id, {key: value}, "
            "delete_keys=[...]) for atomic per-key settings writes instead (FAR-1830/FAR-1832)"
        )
    if "otel_config_json" in updates:
        # FAR-1832 QA F14: the twin tenant JSON column. No caller writes it here
        # today (only ``name``/``plan_id``), but leaving it writable is a
        # copy-paste hazard that reintroduces the same whole-column dumper the
        # settings_json guard above exists to prevent.
        raise ValueError(
            "update_organisation cannot write otel_config_json (whole-column writes lose concurrent "
            "writers' keys); use db.crud.observability.update_otel_config(session, org_id, {key: value}) "
            "for atomic per-key OTel writes instead (FAR-1025/FAR-1832)"
        )
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
    nested_patch: dict[str, dict[str, Any]] | None = None,
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

    ``nested_patch`` (FAR-1832 QA F1) composes ONE top-level key at the
    SUB-FIELD level: ``settings_json = jsonb_set(…, '{key}',
    COALESCE(settings_json->'key','{}') || :subpatch)``. A top-level ``patch``
    entry REPLACES the whole value of its key; a ``nested_patch`` entry MERGES
    into the stored value's sub-fields, so two writers of DIFFERENT sub-fields
    of the same block (e.g. the ``product_analytics`` consent writer and the
    admin level toggle) both survive. At most ONE nested key per call, and a
    key must not appear in BOTH ``patch`` and ``nested_patch`` (the ordering
    would be ambiguous — refused rather than silently resolved).

    FAR-1832 QA F3: the implementation is SHARED with the OTel-config writer —
    this function is a thin wrapper over
    :func:`update_org_json_column_patch` pinned to ``column="settings_json"``
    and ``require_live_row=True`` — so there is exactly one implementation of
    the guard / lock-bound / dialect-split / identity-map machinery.

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
    settings = await update_org_json_column_patch(
        session,
        organisation_id,
        patch=patch or {},
        delete_keys=list(delete_keys or []),
        column="settings_json",
        require_live_row=True,
        allow_unbound_context=allow_unbound_context,
        nested_patch=nested_patch,
    )
    _invalidate_settings_derived_caches()
    return settings


async def update_org_json_column_patch(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    *,
    patch: dict[str, Any],
    delete_keys: list[str],
    column: str,
    require_live_row: bool,
    allow_unbound_context: bool = False,
    nested_patch: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Atomically merge (and/or remove) top-level keys of ONE Organisation JSON column.

    The single implementation (FAR-1832 QA F3) behind both
    :func:`update_org_settings_patch` (``settings_json``; soft-deleted orgs
    refused) and ``db.crud.observability.update_otel_config``
    (``otel_config_json``; soft-deleted orgs deliberately still writable per
    FAR-1025). The legitimate differences are ARGUMENTS, not a fork:

    - ``column`` — which JSONB column to patch (validated against the
      allowlist below; never interpolated from user input).
    - ``require_live_row`` — whether the write carries the
      ``deleted_at IS NULL`` predicate / the generic path uses the
      soft-delete-filtered read.
    - ``delete_keys`` — callers that never delete keys pass ``[]``.

    Everything else is shared: the bounded lock wait
    (``set_mutation_row_lock_timeout``), the fail-closed tenant guard
    (:func:`_strict_read_rls_org`), the single-statement Postgres merge, the
    locked generic-dialect fallback, the ``LookupError`` for a missing /
    soft-deleted (when ``require_live_row``) org, and the identity-map refresh.

    Contract: same as :func:`update_org_settings_patch` except where the
    arguments above deliberately diverge; in particular the tenant guard is
    NOT opt-out-able per column — every writer must bind the org RLS context
    (or pass ``allow_unbound_context`` for genuine boot-seed/system callers).

    WARNING: ``allow_unbound_context=True`` is for boot-seed / system callers
    ONLY — an organisation must normally have ``set_rls_org`` bound before its
    settings are patched. It never masks a *corrupt* context: a non-empty
    ``app.organisation_id`` that fails UUID coercion fails closed
    (:class:`OrgSettingsPatchContextUnreadableError`) regardless of this flag.
    """
    if column not in ("settings_json", "otel_config_json"):
        raise ValueError(f"unsupported organisation JSON column: {column!r}")
    if (column, require_live_row) not in _VALID_JSON_COLUMN_LIVE_ROW:
        # QA F13a: the (column, require_live_row) pairing is a fixed policy, not
        # a free cross-product. Validating it HERE — before any dialect branch —
        # closes the generic path's acceptance of ("settings_json", False),
        # which would write a soft-deleted org's settings via
        # ``include_soft_deleted``: the exact write FAR-1830 forbids.
        raise ValueError(
            f"unsupported org JSON patch configuration: column={column!r} "
            f"require_live_row={require_live_row!r} — settings_json requires "
            f"require_live_row=True and otel_config_json requires require_live_row=False"
        )
    nested = dict(nested_patch or {})
    if len(nested) > 1:
        raise ValueError(f"nested_patch supports at most one top-level key per call, got {sorted(nested)!r}")
    overlap = set(nested) & set(patch)
    if overlap:
        raise ValueError(
            f"nested_patch key(s) {sorted(overlap)!r} also appear in patch — the compose order would be ambiguous"
        )
    # QA F13c: a key in BOTH delete_keys and patch/nested_patch is equally
    # ambiguous (delete-wins was never a documented contract), so it is refused
    # for the same reason the patch/nested overlap is — rather than silently
    # letting the delete win.
    delete_overlap = set(delete_keys) & (set(patch) | set(nested))
    if delete_overlap:
        raise ValueError(
            f"key(s) {sorted(delete_overlap)!r} appear in both delete_keys and patch/nested_patch — "
            "the compose order would be ambiguous"
        )

    # Normalise the target to a ``UUID``: callers occasionally pass the id as a
    # string, and both the tenant-mismatch comparison and the bound SQL /
    # ORM predicate must compare like-for-like (a ``UUID`` never equals its
    # ``str`` form).
    target_org_id = _coerce_org_uuid(organisation_id)
    if target_org_id is None:
        raise ValueError(f"org JSON column patch requires a UUID organisation id, got {organisation_id!r}")

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
        value = await _apply_json_column_patch_postgres(
            session,
            target_org_id,
            patch,
            delete_keys,
            column=column,
            require_live_row=require_live_row,
            nested_patch=nested or None,
        )
    else:
        value = await _apply_json_column_patch_generic(
            session,
            target_org_id,
            patch,
            delete_keys,
            column=column,
            require_live_row=require_live_row,
            nested_patch=nested or None,
        )
    return value


async def _strict_read_rls_org(session: AsyncSession, dialect: str) -> uuid.UUID | None:
    """Read the session's bound RLS org, NEVER swallowing a genuine read failure.

    ``db.rls.read_rls_org`` degrades every failure to ``None`` (its public
    contract, relied on by many callers). The tenancy guard here needs to tell
    "no org bound" apart from "the bound context could not be read", so it uses
    this strict local read instead: a real failure raises
    :class:`OrgSettingsPatchContextUnreadableError`; only a genuinely-absent context
    returns ``None``.

    FAR-1832 QA F4: the branch is on the DIALECT FIRST. On Postgres the
    ``current_setting('app.organisation_id', true)`` GUC read is the ONLY
    authoritative source — ``set_rls_org`` sets the GUC, never
    ``session.info``, so an ``info['org_id']`` value on a Postgres session is
    unrelated to the transaction's actual RLS context and must not satisfy the
    guard (a session carrying a stale/planted info value would otherwise pass
    the tenant check while the GUC — the real boundary — is unset or different,
    defeating :class:`OrgSettingsPatchContextUnreadableError` entirely). A
    failed GUC read always raises (fail closed); ``session.info`` is consulted
    ONLY on non-Postgres dialects, where it is what ``set_rls_org`` writes and
    its absence is genuinely-unbound (not a read error).
    """
    if dialect == "postgresql":
        return await _read_postgres_rls_org(session)
    return _read_session_info_rls_org(session)


async def _read_postgres_rls_org(session: AsyncSession) -> uuid.UUID | None:
    """Read the bound org from the Postgres ``app.organisation_id`` GUC.

    Split out of :func:`_strict_read_rls_org` (FAR-1832) so the dialect
    dispatch stays a single branch. A failed GUC read raises (fail closed); an
    absent/blank GUC is genuinely unbound and returns ``None``; a NON-EMPTY
    value that fails UUID coercion is a CORRUPT context and raises
    (:class:`OrgSettingsPatchContextUnreadableError`) rather than being treated
    as unbound — otherwise ``allow_unbound_context=True`` would permit a write
    against an unknown tenant boundary.
    """
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
    coerced = _coerce_org_uuid(raw)
    if coerced is None:
        raise OrgSettingsPatchContextUnreadableError(
            f"the session's app.organisation_id RLS context is not a valid UUID: {raw!r}"
        )
    return coerced


def _read_session_info_rls_org(session: AsyncSession) -> uuid.UUID | None:
    """Read the bound org from ``session.info`` (non-Postgres dialects only).

    ``set_rls_org`` writes the tenant id to ``session.info`` on generic
    backends; its absence is genuinely-unbound (not a read error), so an
    absent key or a non-``dict`` ``info`` both return ``None``.
    """
    info = getattr(session, "info", None)
    if not isinstance(info, dict):
        return None
    value = info.get(_RLS_TENANT_KEY)
    if value is None:
        return None
    return _coerce_org_uuid(value)


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


def _pg_patch_statement(column: str, require_live_row: bool, *, nested: bool = False) -> TextClause:
    """The single-statement Postgres merge for one (column, live-row) configuration.

    FAR-1832 QA F3: the supported configurations are selected from CONSTANT
    statements rather than interpolating the column name into SQL — the
    ``raw-sql-fstring`` semgrep rule (correctly) forbids identifier
    interpolation into ``text()``, and the cross-product of the two allowlisted
    columns, the live-row flag and the nested-sub-field form is a small,
    audited set. Anything else raises rather than falling through to an
    unaudited shape.

    The ``nested=True`` variants (QA F1) wrap the base merge in a
    ``jsonb_set`` that composes the single nested key at the SUB-FIELD level:
    ``COALESCE(col->key, '{}') || :nested_patch`` — so a writer of one
    sub-field of a shared block never replaces the sibling sub-fields another
    writer committed. QA F13b: the key's stored value is healed through
    ``jsonb_typeof(...) = 'object'`` (a CASE), not a bare ``COALESCE`` — a bare
    ``COALESCE`` catches only SQL NULL, so a stored JSON ``null`` or any
    non-object block fell through to ``jsonb || jsonb`` and corrupted into an
    array. The heal now matches the generic path's ``isinstance(existing, dict)``.
    The base expression is repeated inside ``jsonb_set`` because every reference
    on the right-hand side of an UPDATE sees the OLD row value; the nested
    compose therefore starts from the same base document the top-level ``||``
    produces.
    """
    if column == "settings_json" and require_live_row:
        if nested:
            # Full literal (no concatenation/f-string — bandit B608 and the
            # semgrep raw-sql-fstring rule both reject constructed SQL; every
            # dynamic piece is a bound parameter). The base merge expression
            # appears twice because every reference on the right-hand side of
            # an UPDATE sees the OLD row value: the nested compose starts from
            # the same base document the top-level || produces.
            return text(
                "UPDATE organisations "
                "SET settings_json = jsonb_set("
                "(COALESCE(settings_json, '{}'::jsonb) || CAST(:patch AS jsonb)), "
                "ARRAY[:nested_key]::text[], "
                "CASE WHEN jsonb_typeof("
                "(COALESCE(settings_json, '{}'::jsonb) || CAST(:patch AS jsonb)) "
                "-> CAST(:nested_key AS text)) = 'object' "
                "THEN (COALESCE(settings_json, '{}'::jsonb) || CAST(:patch AS jsonb)) "
                "-> CAST(:nested_key AS text) "
                "ELSE '{}'::jsonb END "
                "|| CAST(:nested_patch AS jsonb)) "
                "- CAST(:delete_keys AS text[]) "
                "WHERE id = :org_id AND deleted_at IS NULL "
                "RETURNING settings_json"
            )
        return text(
            "UPDATE organisations "
            "SET settings_json = "
            "(COALESCE(settings_json, '{}'::jsonb) || CAST(:patch AS jsonb)) "
            "- CAST(:delete_keys AS text[]) "
            "WHERE id = :org_id AND deleted_at IS NULL "
            "RETURNING settings_json"
        )
    if column == "otel_config_json" and not require_live_row and not nested:
        # No ``deleted_at IS NULL`` predicate (FAR-1025, see
        # ``update_org_json_column_patch``); the empty ``delete_keys`` clear is
        # a no-op and keeps ONE statement shape across both columns. The OTel
        # writer never uses a nested sub-patch.
        return text(
            "UPDATE organisations "
            "SET otel_config_json = "
            "(COALESCE(otel_config_json, '{}'::jsonb) || CAST(:patch AS jsonb)) "
            "- CAST(:delete_keys AS text[]) "
            "WHERE id = :org_id "
            "RETURNING otel_config_json"
        )
    raise ValueError(
        f"unsupported org JSON patch configuration: column={column!r} "
        f"require_live_row={require_live_row!r} nested={nested!r}"
    )


async def _apply_json_column_patch_postgres(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    patch: dict[str, Any],
    delete_keys: list[str],
    *,
    column: str = "settings_json",
    require_live_row: bool = True,
    nested_patch: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    stmt = _pg_patch_statement(column, require_live_row, nested=bool(nested_patch))
    params: dict[str, Any] = {"org_id": str(organisation_id)}
    stmt = stmt.bindparams(
        bindparam("patch", value=json.dumps(patch)),
        bindparam("delete_keys", value=delete_keys, type_=ARRAY(Text)),
    )
    if nested_patch:
        ((nested_key, nested_sub),) = nested_patch.items()
        stmt = stmt.bindparams(
            bindparam("nested_key", value=nested_key),
            bindparam("nested_patch", value=json.dumps(nested_sub)),
        )
    result = await session.execute(stmt, params)
    row = result.first()
    if row is None:
        # No row matched: either the org does not exist OR (when
        # ``require_live_row``) it is soft-deleted. The generic path
        # (get_organisation) applies the global soft-delete filter, so this
        # matches its behaviour (both refuse a soft-deleted org).
        raise LookupError(f"organisation not found: {organisation_id}")
    raw = row[0]
    value = json.loads(raw) if isinstance(raw, str) else dict(raw)
    _sync_identity_map(session, organisation_id, value, column=column)
    return value


async def _apply_json_column_patch_generic(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    patch: dict[str, Any],
    delete_keys: list[str],
    *,
    column: str = "settings_json",
    require_live_row: bool = True,
    nested_patch: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if require_live_row:
        org = await get_organisation(session, organisation_id, for_update=True)
    else:
        # FAR-1025 / OTel path: a pending-deletion org stays writable, so the
        # read opts out of the global soft-delete filter (same include_
        # soft_deleted contract the observability reader uses).
        from modulo.db.soft_delete import include_soft_deleted

        result = await session.execute(
            include_soft_deleted(select(Organisation).where(Organisation.id == organisation_id).with_for_update())
        )
        org = result.scalar_one_or_none()
    if org is None:
        raise LookupError(f"organisation not found: {organisation_id}")
    current = getattr(org, column)
    value = dict(current) if isinstance(current, dict) else {}
    value.update(patch)
    # Nested sub-field compose (QA F1): merge into the CURRENT value of the
    # key (post top-level patch — the overlap check above makes that
    # unambiguous), exactly like the Postgres jsonb_set || does.
    for nested_key, nested_sub in (nested_patch or {}).items():
        existing = value.get(nested_key)
        merged_sub = dict(existing) if isinstance(existing, dict) else {}
        merged_sub.update(nested_sub)
        value[nested_key] = merged_sub
    for key in delete_keys:
        value.pop(key, None)
    setattr(org, column, value)
    await session.flush()
    return value


def _sync_identity_map(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    value: dict[str, Any],
    *,
    column: str = "settings_json",
) -> None:
    """Refresh an already-loaded org row to the merged value without dirtying it."""
    identity_map = getattr(session, "identity_map", None)
    getter = getattr(identity_map, "get", None)
    if getter is None:
        return
    org = getter(identity_key(Organisation, organisation_id))
    if isinstance(org, Organisation):
        set_committed_value(org, column, value)


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
