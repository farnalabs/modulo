"""Delete every stale ``feature_flag_catalog`` row (FAR-1875).

Revision ID: 0317_drop_retired_pipeline_diff_rollback_flag
Revises: 0316_policy_gate_evaluation_mode
Create Date: 2026-10-16

The revision id is kept as ``0317_drop_retired_pipeline_diff_rollback_flag``
(FAR-1819 retired that flag first and it is this migration's namesake), but the
sweep is systemic rather than a list: ``upgrade`` deletes EVERY catalog row
whose ``name`` is not in ``LIVE_FLAG_NAMES_SNAPSHOT`` — the code registry frozen
at the revision that authors this migration. A migration must stay
deterministic, so the snapshot is a module constant and the live registry is
never imported at run time; any row whose name is absent from the snapshot is by
definition unreadable by the code at this revision and therefore stale.

FAR-1819 (PR #1870) retired the ``pipeline_diff_rollback`` tier gate: the
``require_feature`` guard for that flag and its entry in
``modulo.core.feature_flags._KNOWN_FLAGS`` were removed, and the key was dropped
from ``modulo.core.seed_data.catalog.FLAGS`` (which is DERIVED from
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

Deriving the set from the frozen registry (rather than enumerating retired
names) is what makes the sweep COMPLETE: a name the author did not think to
enumerate is still removed. ``RETIRED_FLAG_NAMES`` / ``RETIRED_FLAG_ROWS`` are
kept only for ``downgrade``, which restores those known rows. Every name in that
explicit list is (a) ABSENT from the current ``_KNOWN_FLAGS`` code registry and
(b) has zero production readers in ``backend/src`` / ``frontend/src`` (no
``require_feature`` / ``feature_enabled`` / ``featureEnabled`` gate call site).
Restoring a row for a name that is STILL in the code registry would be
harmless (the row matches the registry), but the list must never contain a name
whose tier is missing; the integration test pins the retired names against the
live registry as a safety precondition.

``remy`` / ``remy_ui_driving`` are deliberately NOT restored by ``downgrade``:
migration ``0257_rename_remy_to_assistant`` already renamed those two rows, so
they are handled.

This is a DATA-only migration — NO schema change, and ``tier_catalog`` is never
mutated. Both directions guard on ``information_schema`` probes via a bound
``op.get_bind()`` query (the catalog's ``name`` column for the sweep, plus the
``tier_catalog`` table for the restore), so (a) a second run matches no row and
is a no-op, and (b) a fresh/unseeded database where a table or column is absent
completes without error. Provision of the ``DELETE``/``INSERT`` values is via
bound parameters — no string-concatenated SQL and no quoting helper — so no
``S608``/``B608`` suppression is needed.

Non-Postgres (SQLite) parity: the portable test schema has no
``feature_flag_catalog`` table, and both directions return early on a
non-Postgres dialect (the ``<> ALL(...)::text[]`` and ``ON CONFLICT`` shapes are
Postgres-only). ``downgrade`` restores the exact rows the sweep removed (their
historical name/description/tier, captured from the last ``_KNOWN_FLAGS``
revision that declared each) with ``ON CONFLICT (name) DO NOTHING``, restricted
to rows whose ``tier_id`` actually exists, so it is idempotent and never
clobbers a live row.
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

_log = logging.getLogger(__name__)

revision: str = "0317_drop_retired_pipeline_diff_rollback_flag"
down_revision: str | None = "0316_policy_gate_evaluation_mode"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

#: The code registry's flag names frozen at the revision that authors this
#: migration (``modulo.core.seed_data.catalog.FLAGS``, itself derived from
#: ``modulo.core.feature_flags._KNOWN_FLAGS``). ``upgrade`` deletes every
#: ``feature_flag_catalog`` row whose ``name`` is not in this snapshot. Frozen
#: as a module constant deliberately: a migration must be deterministic and
#: must not read the live registry at run time.
LIVE_FLAG_NAMES_SNAPSHOT: tuple[str, ...] = (
    "admin_cost_breakdown",
    "admin_cost_controls",
    "admin_run_retention",
    "admin_spend_limits",
    "analytics_page",
    "api_key_grants",
    "assistant",
    "assistant_ui_driving",
    "audit_viewer",
    "autonomy_gating",
    "dashboard_charts",
    "email_config",
    "environment_profiles",
    "error_forwarders",
    "error_tracking",
    "eval_system",
    "external_secrets",
    "library_collection",
    "lifecycle_map_journeys",
    "mcp_server",
    "mobile_sidebar_rail",
    "model_backend_management",
    "observability",
    "pipeline_delete",
    "plugin_management",
    "rate_limits",
    "runtime_config",
    "saved_views",
    "schema_version_history",
    "scim",
    "sso",
    "sso_unrestricted_provisioning",
    "team_rbac",
    "user_management",
    "user_scoped_mcp_keys",
    "view_modes",
    "web_vitals_analytics",
    "webhook_notification_log",
    "webhook_trigger",
)

#: Known-retired flag names, used ONLY by ``downgrade`` to restore the rows the
#: sweep removed. Each name is absent from ``_KNOWN_FLAGS`` and has no
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


def _catalog_has_name_column(bind: sa.Connection) -> bool:
    """True when ``public.feature_flag_catalog.name`` exists.

    A column cannot exist without its table, so this single probe covers both
    the "catalog table absent" and "name column absent" cases.
    """
    return (
        bind.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'feature_flag_catalog' "
                "AND column_name = 'name'"
            )
        ).first()
        is not None
    )


def _has_tier_catalog_table(bind: sa.Connection) -> bool:
    """True when ``public.tier_catalog`` exists; the restore needs it for its FK."""
    return (
        bind.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'tier_catalog'"
            )
        ).first()
        is not None
    )


def sweep_stale_catalog_rows(bind: sa.Connection) -> int:
    """Delete every catalog row whose ``name`` is not in the frozen registry snapshot.

    Returns the number of rows deleted, or 0 when the catalog/name column is
    absent (a fresh or unseeded database). The name list is a bound parameter
    (a Python list bound to a Postgres ``text[]``) — no string concatenation.
    """
    if not _catalog_has_name_column(bind):
        return 0
    result = bind.execute(
        sa.text('DELETE FROM public."feature_flag_catalog" WHERE name <> ALL(:live_names)'),
        {"live_names": list(LIVE_FLAG_NAMES_SNAPSHOT)},
    )
    return result.rowcount or 0


def restore_retired_catalog_rows(bind: sa.Connection) -> int:
    """Restore the known-retired rows removed by the sweep (``downgrade``).

    Idempotent via ``ON CONFLICT (name) DO NOTHING`` and restricted to rows
    whose ``tier_id`` actually exists in ``tier_catalog``, so a database where
    the tier table was never seeded (or is absent) cannot raise ``42P01`` or an
    FK violation. Returns the number of rows inserted.
    """
    if not _catalog_has_name_column(bind) or not _has_tier_catalog_table(bind):
        return 0
    rows = [
        {
            "name": name,
            "description": description,
            "tier_id": tier_id,
            # A distinct bind name for the existence check: reusing ``tier_id``
            # in both the SELECT list and the WHERE makes psycopg deduce two
            # inconsistent types for one parameter ("text versus character
            # varying") under executemany.
            "tier_check": tier_id,
        }
        for name, description, tier_id in RETIRED_FLAG_ROWS
    ]
    result = bind.execute(
        sa.text(
            'INSERT INTO public."feature_flag_catalog" (name, description, tier_id, depends_on, is_active) '
            "SELECT :name, :description, :tier_id, NULL, true "
            "WHERE EXISTS (SELECT 1 FROM public.tier_catalog WHERE tier_id = :tier_check) "
            "ON CONFLICT (name) DO NOTHING"
        ),
        rows,
    )
    return result.rowcount or 0


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    deleted = sweep_stale_catalog_rows(bind)
    # Observability: a 0-row sweep is distinguishable in the deploy log from a
    # wrong database, so the count is always reported.
    _log.info("migration 0317: swept %d stale feature_flag_catalog row(s)", deleted)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    restored = restore_retired_catalog_rows(bind)
    _log.info("migration 0317: restored %d retired feature_flag_catalog row(s)", restored)
