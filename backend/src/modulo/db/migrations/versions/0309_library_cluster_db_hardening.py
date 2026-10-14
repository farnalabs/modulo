"""Library / community-catalogue cluster DB hardening.

Revision ID: 0309_library_cluster_db_hardening
Revises: 0308_runs_dispatch_phase_history
Create Date: 2026-10-13

Consolidates the MAJOR/CRITICAL findings of an 8-lens schema review of the
library-primitive / collection-install cluster. Plain-English rationale:

* **Missing indexes (M1-M3).** ``library_primitives.version_group_id`` is
  filtered on by the version-group listing, ``find_newer_version`` and the
  auto-update recipient scan, but has no index; the per-pin slug lookup
  (``primitive_type = ? AND slug = ?``) has no index because the existing
  partial unique starts with ``organisation_id``; and the community-catalogue
  listing filter (published + community visibility) has no supporting index.
* **``collection_install`` destroyed its own provenance (C1).** The model
  docstring calls the rows append-only history, but uninstall hard-deleted the
  install row and CASCADEed away its ``collection_install_entity`` children.
  The table gains ``deleted_at`` and uninstall now soft-deletes; the
  ``0207`` unique constraint on ``(organisation_id, collection_id)`` becomes a
  PARTIAL unique index (same name) so an uninstall no longer blocks a reinstall.
* **No actor recorded (M4).** ``collection_install`` never recorded which
  account performed the install; it gains a nullable ``created_by`` FK.
* **Redundant CHECK constraints (M8).** ``0165_add_check_constraints`` added
  ``ck_library_primitive_download_count`` / ``ck_library_primitive_review_count``
  on top of ``0151``'s already-equivalent ``ck_library_primitives_dl_count`` /
  ``ck_library_primitives_review_count``; the 0165 pair is dropped and the 0151
  pair (which the ORM model declares) is kept.

MODEL-ONLY drift fixed in the same change (NO DDL here, the DB is already
correct): the four model files align their JSON columns to jsonb
(``JSON().with_variant(JSONB(), "postgresql")``) and ``composite_templates``
declares the partial unique 0156 already created.

TABLE-LOCKING HAZARD (the repo's standard caveat, not a new one): this
migration runs inside Alembic's single ``engine.begin()`` transaction
(``env.py`` wraps every revision in one transaction), so
``CREATE INDEX CONCURRENTLY`` — which cannot run inside a transaction block at
all, and which needs a connection that can see the already-committed schema —
is unavailable here. Every index below is therefore a plain (blocking)
``CREATE INDEX IF NOT EXISTS``, which holds a lock on the table for the
duration of the build. ``library_primitives`` can be a large table on a busy
deployment; schedule this migration in a low-traffic window, and on very large
tables consider a post-deploy ``CREATE INDEX CONCURRENTLY`` plus a follow-up
migration that only asserts the index exists. This is the same caveat
documented in ``0128_add_fk_lookup_indexes.py``.

``IF NOT EXISTS`` / ``DROP CONSTRAINT IF EXISTS`` keep the upgrade idempotent
and re-runnable; the two ``op.add_column`` calls, the ``created_by`` FK and its
index are each guarded by an inspector existence check of their OWN (column, FK
and index are independent — a column-present-but-constraint-absent schema is
repaired rather than skipped). The DDL is Postgres-flavoured
(the CHECK DROP / partial-index WHERE clause); the chain has been
Postgres-only in practice since ``0165`` (its bare ``ALTER TABLE ... ADD
CONSTRAINT`` cannot run on SQLite).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0309_library_cluster_db_hardening"
down_revision: str | None = "0308_runs_dispatch_phase_history"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# ---------------------------------------------------------------------------
# Index DDL — literal statements only (an f-string/format-built SQL statement
# is blocked by the migration-fstring-sql semgrep rule in this directory).
# ---------------------------------------------------------------------------

# M1: version_group_id lookups (version listing, find_newer_version, update
# recipients). M2: per-pin slug resolution. M3: community-catalogue listing.
_UPGRADE_INDEXES: tuple[str, ...] = (
    ("CREATE INDEX IF NOT EXISTS ix_library_primitives_version_group_id ON library_primitives (version_group_id)"),
    ("CREATE INDEX IF NOT EXISTS ix_library_primitives_type_slug ON library_primitives (primitive_type, slug)"),
    (
        "CREATE INDEX IF NOT EXISTS ix_library_primitives_visibility_contribution "
        "ON library_primitives (organisation_id, visibility, contribution_status) "
        "WHERE deleted_at IS NULL"
    ),
)

_DOWNGRADE_INDEX_DROPS: tuple[str, ...] = (
    # NOTE: ``ix_collection_install_created_by`` is deliberately NOT listed
    # here — downgrade() drops it explicitly (right before dropping the
    # column), and listing it twice would make the reversal order ambiguous.
    "DROP INDEX IF EXISTS ix_library_primitives_visibility_contribution",
    "DROP INDEX IF EXISTS ix_library_primitives_type_slug",
    "DROP INDEX IF EXISTS ix_library_primitives_version_group_id",
)

# M8: drop the redundant 0165 duplicates; the 0151 pair (same predicates,
# different names, declared by the ORM model) is kept.
_DROP_0165_DUPLICATE_CHECKS: tuple[str, ...] = (
    "ALTER TABLE library_primitives DROP CONSTRAINT IF EXISTS ck_library_primitive_download_count",
    "ALTER TABLE library_primitives DROP CONSTRAINT IF EXISTS ck_library_primitive_review_count",
)

_RESTORE_0165_CHECKS: tuple[str, ...] = (
    ("ALTER TABLE library_primitives ADD CONSTRAINT ck_library_primitive_download_count CHECK (download_count >= 0)"),
    ("ALTER TABLE library_primitives ADD CONSTRAINT ck_library_primitive_review_count CHECK (review_count >= 0)"),
)

# C1: the 0207 UniqueConstraint becomes a partial unique index of the SAME
# name so a soft-deleted (uninstalled) row no longer blocks a reinstall. The
# DROP must come after the deleted_at column exists (see upgrade()).
_DROP_INSTALL_UNIQUE_CONSTRAINT = (
    "ALTER TABLE collection_install DROP CONSTRAINT IF EXISTS uq_collection_install_org_collection"
)
_CREATE_INSTALL_PARTIAL_UNIQUE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_collection_install_org_collection "
    "ON collection_install (organisation_id, collection_id) WHERE deleted_at IS NULL"
)
_RESTORE_INSTALL_UNIQUE_CONSTRAINT = (
    "DROP INDEX IF EXISTS uq_collection_install_org_collection",
    (
        "ALTER TABLE collection_install ADD CONSTRAINT uq_collection_install_org_collection "
        "UNIQUE (organisation_id, collection_id)"
    ),
)

# M4: the created_by FK, added by explicit DDL so the constraint carries a
# deterministic name the downgrade can drop.
_CREATE_CREATED_BY_FK = (
    "ALTER TABLE collection_install ADD CONSTRAINT fk_collection_install_created_by "
    "FOREIGN KEY (created_by) REFERENCES accounts (id) ON DELETE SET NULL"
)
_DROP_CREATED_BY_FK = "ALTER TABLE collection_install DROP CONSTRAINT IF EXISTS fk_collection_install_created_by"


def _column_exists(table: str, column: str) -> bool:
    """Inspector existence check so the add_column calls stay re-runnable."""
    inspector = sa.inspect(op.get_bind())
    return column in {col["name"] for col in inspector.get_columns(table)}


def _foreign_key_exists(table: str, name: str) -> bool:
    """Inspector existence check for a named FK (Postgres has no
    ``ADD CONSTRAINT IF NOT EXISTS``)."""
    inspector = sa.inspect(op.get_bind())
    return name in {fk["name"] for fk in inspector.get_foreign_keys(table)}


def _index_exists(table: str, name: str) -> bool:
    """Inspector existence check so the index DDL stays re-runnable."""
    inspector = sa.inspect(op.get_bind())
    return name in {ix["name"] for ix in inspector.get_indexes(table)}


def upgrade() -> None:
    # M1 / M2 / M3 — plain CREATE INDEX IF NOT EXISTS (see the docstring's
    # table-locking caveat).
    for statement in _UPGRADE_INDEXES:
        op.execute(sa.text(statement))

    # M8 — drop the redundant 0165 CHECK duplicates (no-ops when absent).
    for statement in _DROP_0165_DUPLICATE_CHECKS:
        op.execute(sa.text(statement))

    # C1 — soft-delete column. ``collection_install`` is NOT OrgScoped-derived
    # (explicit columns, PK is install_id), so the column is added directly.
    if not _column_exists("collection_install", "deleted_at"):
        op.add_column(
            "collection_install",
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        )
    # Convert the 0207 unique constraint to a partial unique index (same name).
    op.execute(sa.text(_DROP_INSTALL_UNIQUE_CONSTRAINT))
    op.execute(sa.text(_CREATE_INSTALL_PARTIAL_UNIQUE_INDEX))

    # M4 — actor provenance: nullable created_by FK + its lookup index.
    # Column, FK and index are each gated on their OWN existence: a schema
    # where the column already landed but the constraint/index did not (a
    # partially-applied earlier run) is REPAIRED, never silently skipped —
    # nesting all three under the column check would leave it incomplete.
    if not _column_exists("collection_install", "created_by"):
        op.add_column(
            "collection_install",
            sa.Column("created_by", sa.Uuid(), nullable=True),
        )
    if not _foreign_key_exists("collection_install", "fk_collection_install_created_by"):
        op.execute(sa.text(_CREATE_CREATED_BY_FK))
    if not _index_exists("collection_install", "ix_collection_install_created_by"):
        op.execute(
            sa.text("CREATE INDEX IF NOT EXISTS ix_collection_install_created_by ON collection_install (created_by)")
        )


def downgrade() -> None:
    # M4 — drop the FK, the index, then the column (dropping the column alone
    # would take the FK and index with it, but the explicit order keeps the
    # reversal auditable and dialect-independent).
    op.execute(sa.text(_DROP_CREATED_BY_FK))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_collection_install_created_by"))
    op.drop_column("collection_install", "created_by")

    # C1 — restore the 0207 UniqueConstraint and drop the partial unique index.
    # NOTE: if an install -> uninstall -> reinstall cycle left a soft-deleted row
    # beside a live one for the same (organisation_id, collection_id), the
    # unconditional UNIQUE re-add FAILS LOUDLY — the correct behaviour: a
    # downgrade must never silently weaken the constraint the code relies on.
    for statement in _RESTORE_INSTALL_UNIQUE_CONSTRAINT:
        op.execute(sa.text(statement))
    op.drop_column("collection_install", "deleted_at")

    # M8 — restore the 0165 CHECK pair.
    for statement in _RESTORE_0165_CHECKS:
        op.execute(sa.text(statement))

    # M1 / M2 / M3 — drop the new indexes.
    for statement in _DOWNGRADE_INDEX_DROPS:
        op.execute(sa.text(statement))
