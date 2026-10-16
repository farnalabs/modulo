"""Delete the retired ``pipeline_diff_rollback`` row from ``feature_flag_catalog`` (FAR-1875).

Revision ID: 0317_drop_retired_pipeline_diff_rollback_flag
Revises: 0316_policy_gate_evaluation_mode
Create Date: 2026-10-16

FAR-1819 (PR #1870) retired the ``pipeline_diff_rollback`` tier gate: the
``require_feature("pipeline_diff_rollback")`` call sites and the entry in
``modulo.core.feature_flags._KNOWN_FLAGS`` were removed, and the key was
dropped from ``modulo.core.seed_data.catalog.FLAGS``. The boot seeder
``_seed_tier_catalog`` (``modulo.api.main``) only upserts catalog rows with
``INSERT ... ON CONFLICT (name) DO NOTHING`` — it never deletes — so every
database seeded while the flag existed (staging, production) still carries a
``feature_flag_catalog`` row for it. The admin Feature Flags listing is
DB-backed (``admin_feature_flags._build_registry`` ->
``FeatureFlagRegistry.from_db`` -> ``_load_catalog`` -> ``list_feature_flags``),
so that stale row stays user-visible as a flag that gates nothing.

This is a DATA-only migration — NO schema change. It deletes that one stale
row. The ``DO $$ ... $$`` block is guarded by ``information_schema`` existence
checks for BOTH the table and its ``name`` column, so (a) a second run matches
no row and is a no-op, and (b) a fresh/unseeded database where the table or the
row is absent completes without error. ``tier_catalog`` is not modified — its
``team`` row (the deleted flag's former ``tier_id``) is left in place.

Non-Postgres (SQLite) parity: the portable test schema has no
``feature_flag_catalog`` table, so both directions return early; the ``DO $$``
block is Postgres-only syntax. ``downgrade`` mirrors the delete by re-inserting
the row with its pre-FAR-1819 values (``ON CONFLICT (name) DO NOTHING``, so it
is idempotent and never clobbers a live row), guarded so it is skipped when the
``team`` tier row is absent.
"""

from __future__ import annotations

from alembic import op

revision: str = "0317_drop_retired_pipeline_diff_rollback_flag"
down_revision: str | None = "0316_policy_gate_evaluation_mode"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

#: The retired flag whose boot-seeded catalog row this migration removes.
RETIRED_FLAG_NAME = "pipeline_diff_rollback"

# DATA-only delete of the retired catalog row. Guarded on the table AND its
# ``name`` column existing, so a re-run and a fresh/unseeded database are both
# no-ops. Written as a module constant (no interpolation) so no f-string SQL
# enters a migration (FAR-915 / .semgrep/migration-fstring-sql.yml).
_DELETE_RETIRED_FLAG_SQL = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.tables "
    "WHERE table_schema='public' AND table_name='feature_flag_catalog') "
    "AND EXISTS (SELECT 1 FROM information_schema.columns "
    "WHERE table_schema='public' AND table_name='feature_flag_catalog' AND column_name='name') "
    "THEN DELETE FROM feature_flag_catalog WHERE name = 'pipeline_diff_rollback'; END IF; END $$;"
)

# Downgrade: restore the pre-FAR-1819 row (the exact definition removed from
# ``_KNOWN_FLAGS`` in FAR-1819). Idempotent via ``ON CONFLICT (name) DO
# NOTHING``; skipped when the ``team`` tier row is absent so the FK cannot fail
# on a database where tier_catalog was never seeded.
_RESTORE_RETIRED_FLAG_SQL = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.tables "
    "WHERE table_schema='public' AND table_name='feature_flag_catalog') "
    "AND EXISTS (SELECT 1 FROM information_schema.columns "
    "WHERE table_schema='public' AND table_name='feature_flag_catalog' AND column_name='name') THEN "
    "IF EXISTS (SELECT 1 FROM tier_catalog WHERE tier_id = 'team') THEN "
    "INSERT INTO feature_flag_catalog (name, description, tier_id, depends_on, is_active) "
    "VALUES ('pipeline_diff_rollback', 'Diff-based pipeline version comparison and rollback', "
    "'team', NULL, true) ON CONFLICT (name) DO NOTHING; "
    "END IF; END IF; END $$;"
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(_DELETE_RETIRED_FLAG_SQL)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(_RESTORE_RETIRED_FLAG_SQL)
