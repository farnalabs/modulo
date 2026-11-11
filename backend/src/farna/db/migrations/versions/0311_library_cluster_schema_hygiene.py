"""Library-cluster schema hygiene follow-ups (FAR-1758).

Revision ID: 0311_library_cluster_schema_hygiene
Revises: 0310_library_cluster_db_hardening
Create Date: 2026-10-14

The four MINOR findings from the improve-database 8-lens review of the
library / community-catalogue cluster that PR #1778 deliberately left behind.
Plain-English rationale:

* **Two redundant indexes.** ``ix_collection_install_entity_install_id``
  duplicates the leading column of ``collection_install_entity``'s primary key
  ``(install_id, entity_type, entity_id)`` — a B-tree on the leading column is
  already the PK index, so the standalone one costs write amplification and
  serves nothing. ``ix_publishers_public_key_hex`` has exactly one consumer,
  ``crud/publisher.py::get_publisher_by_key``, which always filters
  ``organisation_id = ? AND public_key_hex = ?`` — already served by
  ``uq_publishers_org_key (organisation_id, public_key_hex)``. A single-column
  index on the SECOND column of a composite unique cannot serve that lookup
  anyway (B-trees do not skip-scan from the middle), so it was dead weight on
  both counts. Neither index is declared by the ORM, so no model edit is
  needed for these two (``publisher.py`` does drop ``index=True`` from
  ``public_key_hex`` so ``create_all`` stops rebuilding it).
* **``publishers.trust_tier`` had no DB CHECK.** The ``{green, amber}``
  vocabulary lived only in app code — ``VALID_TIERS`` in
  ``crud/publisher.py`` and the admin route's ``_RE_GREEN_OR_AMBER`` Pydantic
  pattern — while every other enum-ish column in this cluster carries a DB
  guard. ``ck_publishers_trust_tier`` closes that: the model declares the same
  constraint, so ORM metadata and the migrated schema stay in lockstep.
* **``library_primitives.version_group_id`` had no self-FK.** It always points
  at an existing ``library_primitives.id`` in the SAME organisation (the
  service derives it from ``version_group_id or existing.id``), yet unlike its
  siblings ``forked_from`` and ``update_available_version_id`` it had neither a
  foreign key nor the ``enforce_same_organisation`` tenant trigger — a dangling
  or cross-org value was silently accepted. Both are added here, mirroring the
  sibling pair from ``0109_schema_teams_library`` exactly.

Deploy-safety: the CHECK and the FK are each added ``NOT VALID`` then
``VALIDATE``-d — the house pattern from ``0151_fix_constraints`` /
``0164_add_missing_foreign_keys`` / ``0165_add_check_constraints``. ``NOT
VALID`` takes only a brief ``AccessExclusive`` lock and scans no rows; the
``VALIDATE`` is online and **fails loudly** if a historical row already
carries an out-of-set ``trust_tier`` or a dangling/cross-org
``version_group_id``. That failure is the intended behaviour: this migration
must never quietly leave the constraint unenforced for pre-existing rows.
Each add is guarded by an inspector existence check so the upgrade is
re-runnable, matching ``0310``'s style (Postgres has no ``ADD CONSTRAINT IF
NOT EXISTS``).

TABLE-LOCKING HAZARD (the repo's standard caveat, carried forward from
``0310``): everything runs inside Alembic's single ``engine.begin()``
transaction, so ``CREATE INDEX CONCURRENTLY`` is unavailable — the two index
recreations in ``downgrade()`` are plain blocking ``CREATE INDEX IF NOT
EXISTS`` statements. Both indexes are tiny (small tables, leading column of a
PK / a 128-char hex column), so this is a non-issue in practice.

MODEL-ONLY parity: ``publisher.py`` (drops ``index=True``, adds the CHECK) and
``library_primitive.py`` (adds the self-FK) are edited in the SAME change as
this migration — model/migration parity is a hard rule, enforced by
``tests/integration/test_initial_migration.py``'s
``test_migrated_schema_matches_orm_metadata``.

The DDL is Postgres-flavoured (``NOT VALID`` / ``DO $$`` trigger blocks); the
chain has been Postgres-only in practice since ``0165``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0311_library_cluster_schema_hygiene"
down_revision: str | None = "0310_library_cluster_db_hardening"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# ---------------------------------------------------------------------------
# Statements are LITERAL strings only — the migration-fstring-sql semgrep rule
# (paths include **/db/migrations/versions/**, exclude window ends at 0247)
# blocks f-string / format-built DDL in this directory.
# ---------------------------------------------------------------------------

# (1) + (2) — redundant single-column indexes, dropped unconditionally
# (``IF EXISTS`` keeps the upgrade re-runnable and a create_all'd DB safe).
_DROP_REDUNDANT_INDEXES: tuple[str, ...] = (
    "DROP INDEX IF EXISTS ix_collection_install_entity_install_id",
    "DROP INDEX IF EXISTS ix_publishers_public_key_hex",
)

# downgrade() recreates both, byte-identical to the DDL that first made them
# (0207 for the install-entity index, 0005/0109 for the publisher one).
_RECREATE_REDUNDANT_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS ix_collection_install_entity_install_id ON collection_install_entity (install_id)",
    "CREATE INDEX IF NOT EXISTS ix_publishers_public_key_hex ON publishers (public_key_hex)",
)

# (3) — trust_tier vocabulary. The allowed set is exactly VALID_TIERS from
# crud/publisher.py ({"green", "amber"}), mirrored by the admin route's
# _RE_GREEN_OR_AMBER pattern.
_ADD_TRUST_TIER_CHECK = (
    "ALTER TABLE publishers ADD CONSTRAINT ck_publishers_trust_tier CHECK (trust_tier IN ('green', 'amber')) NOT VALID"
)
_VALIDATE_TRUST_TIER_CHECK = "ALTER TABLE publishers VALIDATE CONSTRAINT ck_publishers_trust_tier"
_DROP_TRUST_TIER_CHECK = "ALTER TABLE publishers DROP CONSTRAINT IF EXISTS ck_publishers_trust_tier"

# (4a) — the missing self-FK. Constraint name follows the Postgres default
# ``<table>_<column>_fkey`` the sibling self-references already carry
# (library_primitives_forked_from_fkey, ..._update_available_version_id_fkey),
# so the ORM's unnamed ``ForeignKey(...)`` and the migrated DB agree.
_ADD_VERSION_GROUP_FK = (
    "ALTER TABLE library_primitives ADD CONSTRAINT library_primitives_version_group_id_fkey "
    "FOREIGN KEY (version_group_id) REFERENCES library_primitives(id) "
    "ON DELETE SET NULL NOT VALID"
)
_VALIDATE_VERSION_GROUP_FK = (
    "ALTER TABLE library_primitives VALIDATE CONSTRAINT library_primitives_version_group_id_fkey"
)
_DROP_VERSION_GROUP_FK = (
    "ALTER TABLE library_primitives DROP CONSTRAINT IF EXISTS library_primitives_version_group_id_fkey"
)

# (4b) — the same-organisation tenant trigger the siblings carry (0109),
# copied verbatim in shape: idempotent pg_trigger guard, column-scoped
# INSERT/UPDATE, and public.enforce_same_organisation(target_table, column).
# That function RAISES when the referenced row is missing OR belongs to a
# different organisation, so a dangling/cross-org value fails loudly rather
# than being skipped.
_CREATE_VERSION_GROUP_TENANT_TRIGGER = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_library_primitives_version_group_id_tenant') "
    "THEN CREATE TRIGGER trg_library_primitives_version_group_id_tenant "
    "BEFORE INSERT OR UPDATE OF version_group_id, organisation_id ON public.library_primitives "
    "FOR EACH ROW EXECUTE FUNCTION public.enforce_same_organisation('library_primitives', 'version_group_id'); "
    "END IF; END $$;"
)
_DROP_VERSION_GROUP_TENANT_TRIGGER = (
    "DROP TRIGGER IF EXISTS trg_library_primitives_version_group_id_tenant ON public.library_primitives"
)


def _constraint_exists(table: str, name: str) -> bool:
    """Inspector existence check for a named constraint.

    Postgres has no ``ADD CONSTRAINT IF NOT EXISTS``, so the CHECK and the FK
    are gated on their OWN existence — the same repair-not-skip shape 0310
    uses for its column/FK/index guards. Looks in the foreign-key list first
    (the common case) then the CHECK list, so one helper serves both.
    """
    inspector = sa.inspect(op.get_bind())
    if name in {fk["name"] for fk in inspector.get_foreign_keys(table)}:
        return True
    return name in {check.get("name") for check in inspector.get_check_constraints(table)}


def upgrade() -> None:
    # (1) + (2) — drop the two redundant indexes (no-ops when already absent).
    for statement in _DROP_REDUNDANT_INDEXES:
        op.execute(sa.text(statement))

    # (3) — trust_tier CHECK, NOT VALID then VALIDATE (loud on bad history).
    if not _constraint_exists("publishers", "ck_publishers_trust_tier"):
        op.execute(sa.text(_ADD_TRUST_TIER_CHECK))
        op.execute(sa.text(_VALIDATE_TRUST_TIER_CHECK))

    # (4a) — version_group_id self-FK, NOT VALID then VALIDATE.
    if not _constraint_exists("library_primitives", "library_primitives_version_group_id_fkey"):
        op.execute(sa.text(_ADD_VERSION_GROUP_FK))
        op.execute(sa.text(_VALIDATE_VERSION_GROUP_FK))

    # (4b) — same-org tenant trigger. Idempotent by its own pg_trigger guard,
    # so no inspector check is needed.
    op.execute(sa.text(_CREATE_VERSION_GROUP_TENANT_TRIGGER))


def downgrade() -> None:
    # (4b) then (4a) — trigger first, so the trigger never fires against a
    # constraint that is about to vanish (exact reverse of upgrade()).
    op.execute(sa.text(_DROP_VERSION_GROUP_TENANT_TRIGGER))
    op.execute(sa.text(_DROP_VERSION_GROUP_FK))

    # (3)
    op.execute(sa.text(_DROP_TRUST_TIER_CHECK))

    # (1) + (2) — recreate the two indexes.
    for statement in _RECREATE_REDUNDANT_INDEXES:
        op.execute(sa.text(statement))
