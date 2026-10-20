"""FAR-1962 (chunk 13, slices 3+4): purge the legacy ``eval_definitions`` table.

**THIS MIGRATION IS IRREVERSIBLE. ROLLBACK IS A DATABASE RESTORE, NOT A CODE
REVERT.** See :func:`downgrade`.

Context — why the table is provably redundant at this point in the chain:

* ``0254_eval_backfill_cutover`` backfilled every ``eval_definitions`` row into
  ``evals`` with 1:1 UUID reuse and repointed the ``eval_results.eval_id`` FK
  and the ``trg_eval_results_eval_id_tenant`` trigger onto ``evals``.
* every read and write path was then cut over to ``evals`` (the frozen legacy
  table stopped being written at the cutover).
* ``0294_eval_results_org_fk`` replaced the single-column
  ``eval_results_eval_id_fkey`` with the composite ``fk_eval_results_eval_org``
  on ``(eval_id, organisation_id)`` → ``evals(id, organisation_id)``. A
  migration that dropped the *old* constraint name would abort; this one never
  assumes it — it enumerates inbound foreign keys from the catalogue and fails
  closed if ANY still reference the legacy table.
* FAR-1961 deleted the legacy ORM entity, leaving the physical table as the
  last residue of the frozen snapshot.

The owner's recorded decision, implemented verbatim and **all inside one
transaction** (Alembic's transactional DDL — a failure at any step rolls the
whole migration back, so no partial purge is ever observable):

1. **ASSERT** — every ``eval_definitions.id`` must exist in ``evals`` with the
   same id. If ANY row has no live twin the migration FAILS CLOSED: nothing is
   deleted and nothing is dropped. The assertion is a general property, never a
   hard-coded row count.
2. **DELETE** — the proven-redundant rows are removed by a logged statement
   that records the deleted count, so the data loss is a named, auditable act
   rather than an invisible side effect of DDL.
3. **DROP** — the now-empty table is dropped, with the drop set ENUMERATED FROM
   THE LIVE CATALOGUE at migration time (constraints, indexes, triggers,
   policies). No object name is assumed: the inventory is read from
   ``pg_constraint`` / ``pg_index`` / ``pg_trigger`` / ``pg_policy``, logged,
   and re-verified as gone afterwards.

Non-Postgres backends are a no-op (SQLite/MariaDB schemas come from the ORM
``create_all`` and no longer model this table).

Production rollout preconditions that live OUTSIDE this migration:

* a pre-purge ``pg_dump`` verified with ``pg_restore --list`` (FAR-1963), and
* a passed staging rehearsal of the purge.

Revision ID: 0324_purge_eval_definitions
Revises: 0323_pipeline_snapshots_substantive_version_index
Create Date: 2026-10-20
"""

from __future__ import annotations

import logging
from typing import Any

from alembic import op
from sqlalchemy import text

revision: str = "0324_purge_eval_definitions"
down_revision: str | None = "0323_pipeline_snapshots_substantive_version_index"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

logger = logging.getLogger(f"alembic.{revision}")

# Fixed identifiers of the migrated schema (never caller input), so every DDL
# statement below is a static literal — no identifier is interpolated.
_LEGACY_TABLE = "eval_definitions"
_LIVE_TABLE = "evals"
_EVAL_RESULTS_TABLE = "eval_results"

# ---------------------------------------------------------------------------
# Static SQL (no f-strings: op.execute cannot bind, and the FAR-915 rule forbids
# interpolating identifiers into DDL)
# ---------------------------------------------------------------------------

_TABLE_OID_SQL = "SELECT to_regclass('public.\"eval_definitions\"')::oid"

# Rows whose id has NO live ``evals`` twin — the fail-closed gate of step 1.
_ORPHAN_COUNT_SQL = (
    "SELECT COUNT(*) FROM public.eval_definitions legacy "
    "LEFT JOIN public.evals live ON live.id = legacy.id "
    "WHERE live.id IS NULL"
)
_ORPHAN_SAMPLE_SQL = (
    "SELECT legacy.id FROM public.eval_definitions legacy "
    "LEFT JOIN public.evals live ON live.id = legacy.id "
    "WHERE live.id IS NULL "
    "ORDER BY legacy.id LIMIT 10"
)

# Inbound foreign keys — enumerated, never name-assumed (0294 renamed the old
# ``eval_results_eval_id_fkey`` to the composite ``fk_eval_results_eval_org``).
_INBOUND_FK_SQL = (
    "SELECT c.conname, c.conrelid::regclass::text "
    "FROM pg_constraint c "
    "WHERE c.contype = 'f' AND c.confrelid = :legacy_oid "
    "ORDER BY c.conname"
)

# Triggers on OTHER tables whose definition still resolves the legacy table.
_LEGACY_POINTING_TRIGGER_SQL = (
    "SELECT t.tgname, c.relname "
    "FROM pg_trigger t "
    "JOIN pg_class c ON c.oid = t.tgrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND NOT t.tgisinternal "
    "  AND t.tgrelid <> :legacy_oid "
    "  AND pg_get_triggerdef(t.oid) LIKE '%eval_definitions%' "
    "ORDER BY t.tgname"
)

# Ordered drop-and-recreate for any trigger that still targets the legacy table.
# A DO block keeps the trigger name dynamic INSIDE the database (``format %I``)
# without interpolating an identifier into Python-side DDL. It takes no bind
# parameters: a placeholder inside a dollar-quoted block body is not a
# server-side parameter, so the legacy table is resolved with ``to_regclass``.
_REPOINT_LEGACY_TRIGGERS_SQL = """
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT t.tgname
        FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND NOT t.tgisinternal
          AND t.tgrelid <> COALESCE(to_regclass('public."eval_definitions"'), 0)
          AND pg_get_triggerdef(t.oid) LIKE '%eval_definitions%'
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON public.eval_results', r.tgname);
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE INSERT OR UPDATE OF eval_id, organisation_id '
            'ON public.eval_results FOR EACH ROW '
            'EXECUTE FUNCTION public.enforce_same_organisation(''evals'', ''eval_id'')',
            r.tgname
        );
    END LOOP;
END $$;
"""

# The eval_results tenant trigger must resolve ``evals`` before the drop.
_EVAL_RESULTS_TRIGGER_RESOLVES_EVALS_SQL = (
    "SELECT COUNT(*) FROM pg_trigger t "
    "JOIN pg_class c ON c.oid = t.tgrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND NOT t.tgisinternal "
    "  AND c.relname = 'eval_results' "
    "  AND pg_get_triggerdef(t.oid) LIKE '%enforce_same_organisation(''evals''%'"
)

# Catalogue enumerations of the drop set (all keyed on the legacy table's oid).
_ATTACHED_CONSTRAINTS_SQL = "SELECT conname, contype FROM pg_constraint WHERE conrelid = :legacy_oid ORDER BY conname"
_ATTACHED_INDEXES_SQL = (
    "SELECT c.relname FROM pg_index i "
    "JOIN pg_class c ON c.oid = i.indexrelid "
    "WHERE i.indrelid = :legacy_oid "
    "ORDER BY c.relname"
)
_ATTACHED_TRIGGERS_SQL = (
    "SELECT tgname FROM pg_trigger WHERE tgrelid = :legacy_oid AND NOT tgisinternal ORDER BY tgname"
)
_ATTACHED_POLICIES_SQL = "SELECT polname FROM pg_policy WHERE polrelid = :legacy_oid ORDER BY polname"

# Post-drop verification. Scoped by the legacy table's oid, never by object
# name: several of the enumerated names are shared across tables (every
# org-scoped table carries a policy literally named ``rls_org_isolation``), so a
# global name match would report another table's object as a leftover. The oid
# is the only identity that is unique to the dropped relation.
_CONSTRAINT_ON_OID_SQL = "SELECT COUNT(*) FROM pg_constraint WHERE conrelid = :legacy_oid"
_INDEX_ON_OID_SQL = "SELECT COUNT(*) FROM pg_index WHERE indrelid = :legacy_oid"
_TRIGGER_ON_OID_SQL = "SELECT COUNT(*) FROM pg_trigger WHERE tgrelid = :legacy_oid"
_POLICY_ON_OID_SQL = "SELECT COUNT(*) FROM pg_policy WHERE polrelid = :legacy_oid"
_TABLE_BY_NAME_SQL = "SELECT COUNT(*) FROM pg_class WHERE relkind = 'r' AND relname = 'eval_definitions'"

_COUNT_SQL = 'SELECT COUNT(*) FROM public."eval_definitions"'
_DELETE_SQL = 'DELETE FROM public."eval_definitions"'
_DROP_SQL = 'DROP TABLE public."eval_definitions"'


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def _scalar(sql: str, params: dict[str, Any] | None = None) -> Any:
    return op.get_bind().execute(text(sql), params or {}).scalar()


def _rows(sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    return list(op.get_bind().execute(text(sql), params or {}).fetchall())


def _legacy_oid() -> int | None:
    """The legacy table's oid, or None when it is already gone."""
    oid = _scalar(_TABLE_OID_SQL)
    return int(oid) if oid is not None else None


def _assert_every_row_has_a_live_twin() -> None:
    """Step 1 — FAIL CLOSED unless every legacy row exists in ``evals``."""
    orphans = _scalar(_ORPHAN_COUNT_SQL)
    if orphans:
        sample = [str(row[0]) for row in _rows(_ORPHAN_SAMPLE_SQL)]
        msg = (
            f"FAR-1962 purge abort: {orphans} {_LEGACY_TABLE} row(s) have no "
            f"{_LIVE_TABLE} twin with the same id — nothing deleted, nothing dropped. "
            f"Sample ids: {sample}"
        )
        logger.error(msg)
        raise RuntimeError(msg)
    logger.info(
        "eval_definitions purge: assertion passed — every legacy row has a live evals twin with the same id.",
    )


def _assert_no_inbound_foreign_keys(legacy_oid: int) -> None:
    """FAIL CLOSED if anything still references the legacy table by FK."""
    inbound = _rows(_INBOUND_FK_SQL, {"legacy_oid": legacy_oid})
    if inbound:
        detail = ", ".join(f"{name} on {table}" for name, table in inbound)
        msg = (
            f"FAR-1962 purge abort: {len(inbound)} foreign key(s) still reference "
            f"{_LEGACY_TABLE} ({detail}). Repoint them onto {_LIVE_TABLE} in an earlier "
            "slice; this migration never assumes a constraint name and never drops a "
            "referenced table."
        )
        logger.error(msg)
        raise RuntimeError(msg)
    logger.info("eval_definitions purge: no inbound foreign keys reference the legacy table.")


def _repoint_and_verify_legacy_triggers(legacy_oid: int) -> None:
    """Ordered drop-and-recreate for any trigger still pointing at the legacy table.

    Observed at the pre-purge head: nothing points at it (0254 repointed
    ``trg_eval_results_eval_id_tenant`` onto ``evals``). This branch exists for an
    out-of-band deployment whose trigger was recreated against the legacy table.

    Only triggers on ``eval_results`` are repairable here — the repair knows that
    table's columns. A trigger on any OTHER table still resolving the legacy
    table is an unexpected dependent and FAILS CLOSED (a human must decide how to
    repoint it). After the repair the general property is re-verified.
    """
    pointing = _rows(_LEGACY_POINTING_TRIGGER_SQL, {"legacy_oid": legacy_oid})
    unrepairable = [(name, table) for name, table in pointing if table != _EVAL_RESULTS_TABLE]
    if unrepairable:
        msg = (
            f"FAR-1962 purge abort: {len(unrepairable)} trigger(s) on table(s) other than "
            f"{_EVAL_RESULTS_TABLE} still resolve {_LEGACY_TABLE} "
            f"({', '.join(f'{name} on {table}' for name, table in unrepairable)}). "
            "Repoint them in an earlier slice."
        )
        logger.error(msg)
        raise RuntimeError(msg)

    if pointing:
        logger.warning(
            "eval_definitions purge: %d trigger(s) still resolve the legacy table (%s) — "
            "dropping and recreating them against %s, in that order.",
            len(pointing),
            ", ".join(f"{name} on {table}" for name, table in pointing),
            _LIVE_TABLE,
        )
        op.execute(text(_REPOINT_LEGACY_TRIGGERS_SQL))

    still_pointing = _rows(_LEGACY_POINTING_TRIGGER_SQL, {"legacy_oid": legacy_oid})
    if still_pointing:
        msg = (
            f"FAR-1962 purge abort: {len(still_pointing)} trigger(s) still resolve "
            f"{_LEGACY_TABLE} after the repoint attempt "
            f"({', '.join(name for name, _ in still_pointing)})."
        )
        logger.error(msg)
        raise RuntimeError(msg)

    resolving = _scalar(_EVAL_RESULTS_TRIGGER_RESOLVES_EVALS_SQL)
    if not resolving:
        msg = (
            "FAR-1962 purge abort: no eval_results tenant trigger resolves "
            f"{_LIVE_TABLE} — the same-organisation guard would be lost by the drop."
        )
        logger.error(msg)
        raise RuntimeError(msg)
    logger.info("eval_definitions purge: the eval_results tenant trigger resolves evals.")


def _enumerate_drop_set(legacy_oid: int) -> dict[str, list[str]]:
    """Read the drop set from the live catalogue — never assume object names."""
    inventory = {
        "constraints": [str(row[0]) for row in _rows(_ATTACHED_CONSTRAINTS_SQL, {"legacy_oid": legacy_oid})],
        "indexes": [str(row[0]) for row in _rows(_ATTACHED_INDEXES_SQL, {"legacy_oid": legacy_oid})],
        "triggers": [str(row[0]) for row in _rows(_ATTACHED_TRIGGERS_SQL, {"legacy_oid": legacy_oid})],
        "policies": [str(row[0]) for row in _rows(_ATTACHED_POLICIES_SQL, {"legacy_oid": legacy_oid})],
    }
    logger.info(
        "eval_definitions purge: drop set enumerated from the catalogue — "
        "constraints=%s indexes=%s triggers=%s policies=%s",
        inventory["constraints"],
        inventory["indexes"],
        inventory["triggers"],
        inventory["policies"],
    )
    return inventory


def _delete_proven_redundant_rows() -> int:
    """Step 2 — a logged, auditable DELETE that records the count."""
    before = _scalar(_COUNT_SQL)
    result = op.get_bind().execute(text(_DELETE_SQL))
    deleted = int(result.rowcount or 0)
    if deleted != before:
        # A concurrent writer would mean the cutover was incomplete — fail
        # closed rather than report a count that does not describe the act.
        msg = (
            f"FAR-1962 purge abort: deleted {deleted} row(s) but the pre-delete count "
            f"was {before} — a concurrent writer reached the legacy table."
        )
        logger.error(msg)
        raise RuntimeError(msg)
    logger.info(
        "eval_definitions purge: deleted %d proven-redundant row(s) (each verified to have "
        "a live evals twin); the legacy table is now empty.",
        deleted,
    )
    return deleted


def _assert_inventory_gone(inventory: dict[str, list[str]], legacy_oid: int) -> None:
    """Step 3 verification — every enumerated catalogue object is gone.

    Checked by the legacy relation's oid rather than by object name: several
    enumerated names are shared across tables (``rls_org_isolation`` exists on
    every org-scoped table), so only the oid identifies the dropped table's own
    objects. ``DROP TABLE`` removes them atomically; this proves it did.
    """
    checks = (
        ("constraints", _CONSTRAINT_ON_OID_SQL),
        ("indexes", _INDEX_ON_OID_SQL),
        ("triggers", _TRIGGER_ON_OID_SQL),
        ("policies", _POLICY_ON_OID_SQL),
    )
    leftovers: list[str] = []
    for kind, sql in checks:
        if not inventory[kind]:
            continue
        remaining = _scalar(sql, {"legacy_oid": legacy_oid})
        if remaining:
            leftovers.append(f"{kind}: {remaining} still attached to the legacy oid ({inventory[kind]})")
    if not leftovers and _scalar(_TABLE_BY_NAME_SQL):
        leftovers.append(f"table: a relation named {_LEGACY_TABLE!r} still exists")
    if leftovers:
        msg = "FAR-1962 purge abort: catalogue objects still present after the drop — " + "; ".join(leftovers)
        logger.error(msg)
        raise RuntimeError(msg)


def upgrade() -> None:
    if not _is_postgres():
        return

    legacy_oid = _legacy_oid()
    if legacy_oid is None:
        logger.info(
            "eval_definitions purge: table already absent — nothing to assert, delete or drop.",
        )
        return

    # 1. ASSERT (fail closed: a missing twin aborts before any deletion).
    _assert_every_row_has_a_live_twin()
    _assert_no_inbound_foreign_keys(legacy_oid)
    _repoint_and_verify_legacy_triggers(legacy_oid)
    inventory = _enumerate_drop_set(legacy_oid)

    # 2. DELETE (logged, counted).
    _delete_proven_redundant_rows()

    # 3. DROP + verify the whole enumerated drop set is gone.
    op.execute(text(_DROP_SQL))
    _assert_inventory_gone(inventory, legacy_oid)
    if _legacy_oid() is not None:
        msg = "FAR-1962 purge abort: the legacy table is still present after DROP TABLE."
        logger.error(msg)
        raise RuntimeError(msg)
    logger.info(
        "eval_definitions purge: table dropped; its %d catalogue object(s) are gone.",
        sum(len(names) for names in inventory.values()),
    )


def downgrade() -> None:
    """IRREVERSIBLE — rollback requires the pre-purge database backup.

    The dropped rows were proven-redundant copies of live ``evals`` rows (step 1's
    assertion), and every reader/writer has used ``evals`` since
    ``0254_eval_backfill_cutover``; but the *drop itself* cannot be reconstructed
    by a chain rewind. There is deliberately NO code path here that pretends to
    resurrect the data: a schema-only rewind would recreate an EMPTY legacy table
    that no production path reads or writes, which is not a rollback.

    The rollback procedure is a RESTORE, owned by the pre-purge ``pg_dump``
    (FAR-1963 — verified with ``pg_restore --list`` and rehearsed on staging
    before the rollout):

    1. stop the application (web + worker) so nothing writes during the restore;
    2. restore the pre-purge dump into the database;
    3. stamp ``alembic_version`` back to ``0323_pipeline_snapshots_substantive_version_index`` so the
       chain is consistent with the restored schema.

    After a restore, deploying the previously running image also redeploys the
    application code that predates the purge.
    """
    raise RuntimeError(
        "FAR-1962 purge: migration 0324 downgrade is a DOCUMENTED NO-OP — the DROP is "
        "irreversible and rollback is a database restore from the pre-purge backup "
        "(FAR-1963), never a code revert. See this function's docstring for the procedure."
    )
