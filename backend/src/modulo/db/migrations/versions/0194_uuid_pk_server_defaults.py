"""Server-side gen_random_uuid() defaults on every uuid primary key (FAR-718).

Revision ID: 0194_uuid_pk_server_defaults
Revises: 0193_run_node_outputs_sweep_index
Create Date: 2026-09-08

Purpose and outage context
--------------------------
Migration 0191's raw-SQL backfill INSERT omitted the ``id`` column from its
column list (FAR-701/702 outage): the table's uuid primary key had NO server
default, so the INSERT failed with a NOT NULL violation and froze the deploy
chain. The ORM never hits this — SQLAlchemy supplies uuid ids client-side —
so only raw-SQL migrations (and any future hand-written INSERT) were exposed.
This revision kills the entire bug class: every uuid primary key gets a
server-side ``gen_random_uuid()`` default (built-in on Postgres 13+; staging
and prod run PG 17), so a raw INSERT that omits the id column now succeeds
with a random v4 uuid instead of failing.

Behavioural note: this is a SAFETY NET, not an id-strategy change. ORM paths
are unchanged — the client-side ``default=uuid.uuid4`` still supplies ids and
always wins. Only an INSERT that omits the id column entirely (raw SQL, psql
sessions, migration backfills) falls through to the server default.

The statement list is FROZEN: it was enumerated from the SQLAlchemy metadata
at authoring time and is committed here as explicit literals. The migration
MUST NOT inspect metadata at runtime (a runtime-enumerated list would silently
change on future model edits). Excluded deliberately: non-uuid primary keys
(``tier_catalog``, ``oauth_authorization_codes``/``oauth_tokens`` string keys),
composite-PK columns that are also foreign keys (``run_evidence.run_id`` /
``run_evidence.node_id``), and ``alembic_version``.

Downgrade mirrors the list exactly, dropping every default (restoring the
pre-0194 state where raw inserts without id fail again).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

revision: str = "0194_uuid_pk_server_defaults"
down_revision: str | None = "0193_run_node_outputs_sweep_index"
branch_labels: str | None = None
depends_on: str | None = None

# Frozen at authoring time from the SQLAlchemy model metadata (uuid PKs, FK
# parents excluded). Alphabetical by table name for reviewability; the order
# is irrelevant to the result — every statement is an independent column-level
# ALTER.
_UPGRADE_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE accounts ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE agent_runner_bindings ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE agents ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE audit_chain_heads ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE audit_events ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE chat_messages ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE chat_sessions ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE composite_templates ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE connector_instances ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE connector_profiles ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE cost_components ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE deleted_defaults ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE dismissals ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE environment_profiles ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE error_events ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE error_forwarder_configs ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE error_groups ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE error_notification_rules ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE eval_cases ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE eval_datasets ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE eval_definitions ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE eval_results ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE eval_suites ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE feedback_records ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE hitl_claims ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE invitations ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE journeys ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE library_primitives ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE library_sync_state ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE lifecycle_map_stages ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE lifecycle_maps ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE mcp_setup_tokens ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE metrics_staging ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE model_backends ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE modulo_journey_facts ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE node_categories ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE node_observations ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE nodes ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE notification_delivery_log ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE notification_endpoints ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE notification_preferences ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE notifications ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE oauth_clients ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE oauth_token_families ALTER COLUMN family_id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE onboarding_progress ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE org_api_keys ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE org_daily_run_counts ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE org_memberships ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE organisations ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE parameter_schemas ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE parameter_sets ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE pipeline_edges ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE pipeline_folders ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE pipeline_snapshots ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE pipelines ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE primitive_abuse_reports ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE primitive_ratings ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE publishers ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE remy_context_sources ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE remy_skills ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE run_daily_facts ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE runs ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE runner_probe_cache ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE saved_views ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE scheduled_reports ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE schema_folders ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE schema_versions ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE schemas ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE secrets ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE snapshot_schema_pins ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE spend_anomalies ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE sso_providers ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE suite_runs ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE system_config ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE team_memberships ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE teams ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE token_families ALTER COLUMN family_id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE trigger_events ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE triggers ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE variant_groups ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE web_vital_events ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE webhook_dedup_hashes ALTER COLUMN id SET DEFAULT gen_random_uuid()",
    "ALTER TABLE webhook_payloads ALTER COLUMN id SET DEFAULT gen_random_uuid()",
)

# Exact mirror of _UPGRADE_STATEMENTS with SET DEFAULT -> DROP DEFAULT.
_DOWNGRADE_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE accounts ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE agent_runner_bindings ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE agents ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE audit_chain_heads ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE audit_events ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE chat_messages ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE chat_sessions ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE composite_templates ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE connector_instances ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE connector_profiles ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE cost_components ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE deleted_defaults ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE dismissals ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE environment_profiles ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE error_events ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE error_forwarder_configs ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE error_groups ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE error_notification_rules ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE eval_cases ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE eval_datasets ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE eval_definitions ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE eval_results ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE eval_suites ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE feedback_records ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE hitl_claims ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE invitations ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE journeys ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE library_primitives ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE library_sync_state ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE lifecycle_map_stages ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE lifecycle_maps ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE mcp_setup_tokens ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE metrics_staging ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE model_backends ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE modulo_journey_facts ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE node_categories ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE node_observations ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE nodes ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE notification_delivery_log ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE notification_endpoints ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE notification_preferences ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE notifications ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE oauth_clients ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE oauth_token_families ALTER COLUMN family_id DROP DEFAULT",
    "ALTER TABLE onboarding_progress ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE org_api_keys ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE org_daily_run_counts ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE org_memberships ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE organisations ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE parameter_schemas ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE parameter_sets ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE pipeline_edges ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE pipeline_folders ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE pipeline_snapshots ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE pipelines ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE primitive_abuse_reports ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE primitive_ratings ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE publishers ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE remy_context_sources ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE remy_skills ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE run_daily_facts ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE runs ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE runner_probe_cache ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE saved_views ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE scheduled_reports ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE schema_folders ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE schema_versions ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE schemas ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE secrets ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE snapshot_schema_pins ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE spend_anomalies ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE sso_providers ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE suite_runs ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE system_config ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE team_memberships ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE teams ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE token_families ALTER COLUMN family_id DROP DEFAULT",
    "ALTER TABLE trigger_events ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE triggers ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE variant_groups ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE web_vital_events ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE webhook_dedup_hashes ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE webhook_payloads ALTER COLUMN id DROP DEFAULT",
)


def upgrade() -> None:
    bind = op.get_bind()
    for stmt in _UPGRADE_STATEMENTS:
        # ``runner_probe_cache`` is created by a LATER migration (0202) that also
        # supplies its uuid-PK server default at CREATE time; guard against the
        # table not yet existing when this frozen enum runs (post-0194 tables).
        try:
            bind.execute(text(stmt))
        except ProgrammingError as exc:
            if getattr(exc.orig, "sqlstate", "") != "42P01":  # undefined_table
                raise


def downgrade() -> None:
    bind = op.get_bind()
    for stmt in _DOWNGRADE_STATEMENTS:
        try:
            bind.execute(text(stmt))
        except ProgrammingError as exc:
            if getattr(exc.orig, "sqlstate", "") != "42P01":  # undefined_table
                raise
