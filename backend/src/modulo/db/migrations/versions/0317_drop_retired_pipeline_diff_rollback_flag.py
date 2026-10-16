"""Delete every retired flag's stale ``feature_flag_catalog`` row (FAR-1875).

Revision ID: 0317_drop_retired_pipeline_diff_rollback_flag
Revises: 0316_policy_gate_evaluation_mode
Create Date: 2026-10-16

The revision id is kept as ``0317_drop_retired_pipeline_diff_rollback_flag``
(FAR-1819 retired that flag first and it is this migration's namesake), but the
sweep covers the whole systemically-stale set below.

FAR-1819 (PR #1870) retired the ``pipeline_diff_rollback`` tier gate: the
``require_feature("pipeline_diff_rollback")`` call sites and the entry in
``modulo.core.feature_flags._KNOWN_FLAGS`` were removed, and the key was
dropped from ``modulo.core.seed_data.catalog.FLAGS`` (which is DERIVED from
``_KNOWN_FLAGS``). Retiring a flag is a code-only change: the boot seeder
``_seed_tier_catalog`` (``modulo.api.main``) only upserts catalog rows with
``INSERT ... ON CONFLICT (name) DO NOTHING`` — it never deletes — so every
database seeded while a flag existed (staging, production) keeps a
``feature_flag_catalog`` row for it. The admin Feature Flags listing is
DB-backed (``admin_feature_flags._build_registry`` ->
``FeatureFlagRegistry.from_db`` -> ``_load_catalog`` -> ``list_feature_flags``),
so that stale row stays user-visible as a flag that gates nothing. The same
defect applies to EVERY flag ever removed from ``_KNOWN_FLAGS``, so this
migration fixes it as one systematic sweep rather than per-flag.

``RETIRED_FLAG_NAMES`` is the verified-stale set: every name is (a) ABSENT from
the current ``_KNOWN_FLAGS`` code registry and (b) has zero production readers
in ``backend/src`` / ``frontend/src`` (no ``require_feature(...)`` /
``feature_enabled(...)`` / ``featureEnabled(...)`` call site). Deleting a
catalog row for a name that is STILL in the code registry would gate that
feature off (``FeatureFlagRegistry.get_flag`` returns ``None`` ->
``feature_enabled`` is ``False``), so the set must never contain a live flag;
the integration test asserts that invariant against the live registry.

``remy`` / ``remy_ui_driving`` are deliberately NOT in the set: migration
``0257_rename_remy_to_assistant`` already renamed those two rows, so they are
handled.

This is a DATA-only migration — NO schema change. The ``DO $$ ... $$`` block is
guarded by ``information_schema`` existence checks for BOTH the table and its
``name`` column, so (a) a second run matches no row and is a no-op, and (b) a
fresh/unseeded database where the table or the row is absent completes without
error. ``tier_catalog`` is not modified by the delete — its ``team``/``community``
rows (the deleted flags' former ``tier_id`` values) are left in place.

Non-Postgres (SQLite) parity: the portable test schema has no
``feature_flag_catalog`` table, so both directions return early; the ``DO $$``
block is Postgres-only syntax. ``downgrade`` restores the exact rows the sweep
removed (their historical name/description/tier, captured from the last
``_KNOWN_FLAGS`` revision that declared each) with ``ON CONFLICT (name) DO
NOTHING``, so it is idempotent and never clobbers a live row. Every identifier
is schema-qualified as ``public."..."`` (the same convention as migration
``0109``) so the guard and the DML cannot disagree under a non-default
``search_path``, and ``downgrade`` additionally guards the ``tier_catalog``
TABLE so a present-``feature_flag_catalog``/absent-``tier_catalog`` database
cannot raise ``42P01``.
"""

from __future__ import annotations

from alembic import op

revision: str = "0317_drop_retired_pipeline_diff_rollback_flag"
down_revision: str | None = "0316_policy_gate_evaluation_mode"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

#: Every flag whose boot-seeded catalog row this migration removes. Each name
#: is absent from ``modulo.core.feature_flags._KNOWN_FLAGS`` and has no
#: production reader (verified per-name; see the module docstring).
RETIRED_FLAG_NAMES: tuple[str, ...] = (
    "agent_signal_trigger",
    "api_changelog",
    "audit_crypto_chain",
    "checkpoint_encryption",
    "community_library",
    "community_registry",
    "cron_trigger",
    "eval_maturity",
    "migration_cli",
    "notification_log",
    "ongoing_trigger",
    "parallel_branches",
    "pipeline_diff_rollback",
    "polling_trigger",
    "prompt_optimization",
    "schema_union_types",
)

#: Historical ``(name, description, tier_id)`` for each retired flag, captured
#: from the last ``_KNOWN_FLAGS`` revision that still declared it. ``downgrade``
#: restores these rows with ``is_active=true`` and ``depends_on=NULL`` (each
#: stale flag declared no dependencies).
RETIRED_FLAG_ROWS: tuple[tuple[str, str, str], ...] = (
    (
        "agent_signal_trigger",
        "Trigger pipelines via agent-to-agent signals",
        "community",
    ),
    ("api_changelog", "API changelog and version history", "community"),
    (
        "audit_crypto_chain",
        "Cryptographic chaining of audit events for tamper evidence",
        "team",
    ),
    ("checkpoint_encryption", "Encrypt pipeline checkpoints at rest", "team"),
    (
        "community_library",
        "Browse and import community-contributed pipeline primitives",
        "community",
    ),
    (
        "community_registry",
        "Publish and discover community pipeline primitives",
        "team",
    ),
    (
        "cron_trigger",
        "Schedule pipeline runs on a cron expression",
        "community",
    ),
    (
        "eval_maturity",
        (
            "Generic eval suite/dataset maturity model (FAR-374). Gates the new "
            "EvalSuite entity, endpoints, and UI behind a flag so the legacy "
            "suite_id behaviour is untouched until explicitly enabled."
        ),
        "community",
    ),
    ("migration_cli", "CLI tool for migrating pipelines across instances", "team"),
    ("notification_log", "In-app notification delivery log", "community"),
    (
        "ongoing_trigger",
        "Keep a pipeline topped up to a target number of in-flight runs",
        "community",
    ),
    (
        "parallel_branches",
        "Run branching logic in parallel within a pipeline",
        "community",
    ),
    (
        "pipeline_diff_rollback",
        "Diff-based pipeline version comparison and rollback",
        "team",
    ),
    (
        "polling_trigger",
        "Trigger pipelines by polling external endpoints",
        "community",
    ),
    (
        "prompt_optimization",
        "Automated prompt tuning and optimisation",
        "team",
    ),
    (
        "schema_union_types",
        "Union types and polymorphic schemas",
        "team",
    ),
)


def _single_quoted(value: str) -> str:
    """Quote a frozen module-constant string as a SQL literal.

    Doubling embedded single quotes is the standard SQL escape; the values are
    module constants, never caller data, so no identifier validation is needed.
    """
    return "'" + value.replace("'", "''") + "'"


# The SQL below is assembled ONLY from the frozen module constants above — no
# caller data is interpolated, and nothing is built inside an ``op.execute(...)``
# f-string, so the migration f-string-SQL semgrep rules do not apply.
_FLAG_NAME_LITERALS = ", ".join(_single_quoted(name) for name in RETIRED_FLAG_NAMES)

_FLAG_VALUE_ROWS = ", ".join(
    "(" + _single_quoted(name) + ", " + _single_quoted(description) + ", " + _single_quoted(tier_id) + ")"
    for name, description, tier_id in RETIRED_FLAG_ROWS
)

# The information_schema guard is scoped to ``table_schema='public'`` and the
# DML is schema-qualified ``public."..."``, so the two cannot disagree under a
# non-default ``search_path``.
_CATALOG_GUARD = (
    "EXISTS (SELECT 1 FROM information_schema.tables "
    "WHERE table_schema='public' AND table_name='feature_flag_catalog') "
    "AND EXISTS (SELECT 1 FROM information_schema.columns "
    "WHERE table_schema='public' AND table_name='feature_flag_catalog' AND column_name='name')"
)

# DATA-only delete of every retired catalog row. Guarded on the table AND its
# ``name`` column existing, so a re-run and a fresh/unseeded database are both
# no-ops.
_DELETE_RETIRED_FLAGS_SQL = (
    # SQL assembled from frozen module constants only, never caller data.
    "DO $$ BEGIN IF " + _CATALOG_GUARD + " THEN "  # noqa: S608  # nosec B608
    'DELETE FROM public."feature_flag_catalog" WHERE name IN (' + _FLAG_NAME_LITERALS + "); "
    "END IF; END $$;"
)

# Downgrade: restore the rows the sweep removed. Idempotent via
# ``ON CONFLICT (name) DO NOTHING``. Guarded on the catalog table + name column
# AND the ``tier_catalog`` TABLE, and restricted to rows whose ``tier_id``
# actually exists, so a database where ``tier_catalog`` was never seeded (or is
# absent) cannot raise ``42P01`` or an FK violation.
_RESTORE_RETIRED_FLAGS_SQL = (
    # SQL assembled from frozen module constants only, never caller data.
    "DO $$ BEGIN IF " + _CATALOG_GUARD + " "  # noqa: S608  # nosec B608
    "AND EXISTS (SELECT 1 FROM information_schema.tables "
    "WHERE table_schema='public' AND table_name='tier_catalog') THEN "
    'INSERT INTO public."feature_flag_catalog" (name, description, tier_id, depends_on, is_active) '
    "SELECT v.name, v.description, v.tier_id, NULL, true "
    "FROM (VALUES " + _FLAG_VALUE_ROWS + ") AS v(name, description, tier_id) "  # nosec B608
    "WHERE v.tier_id IN (SELECT tier_id FROM public.tier_catalog) "
    "ON CONFLICT (name) DO NOTHING; "
    "END IF; END $$;"
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(_DELETE_RETIRED_FLAGS_SQL)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(_RESTORE_RETIRED_FLAGS_SQL)
