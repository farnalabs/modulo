"""Seed tier_catalog and feature_flag_catalog

Revision ID: 0080_seed_tier_catalog
Revises: 0079_add_opencode_provider
Create Date: 2026-07-05 14:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0080_seed_tier_catalog"
down_revision: Union[str, None] = "0079_add_opencode_provider"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    existing = conn.execute(text("SELECT COUNT(*) FROM tier_catalog")).scalar()
    if existing == 0:
        conn.execute(
            text("""
                INSERT INTO tier_catalog (tier_id, label, rank, requires_license, description)
                VALUES
                ('community', 'Community', 0, false, 'Free tier for individual developers'),
                ('team', 'Team', 1, true, 'Team tier for small teams'),
                ('v1', 'V1', 2, true, 'Production tier with enterprise features'),
                ('v2', 'V2', 3, true, 'Full platform with all features')
            """)
        )

    existing = conn.execute(text("SELECT COUNT(*) FROM feature_flag_catalog")).scalar()
    if existing == 0:
        conn.execute(
            text("""
                INSERT INTO feature_flag_catalog (name, description, tier_id, depends_on, is_active)
                VALUES
                ('parallel_branches', 'Run branching logic in parallel within a pipeline', 'community', NULL, true),
                ('eval_system', 'Built-in eval runner for LLM output quality gates', 'community', NULL, true),
                ('webhook_trigger', 'Trigger pipelines via incoming webhooks', 'community', NULL, true),
                ('cron_trigger', 'Schedule pipeline runs on a cron expression', 'community', NULL, true),
                ('mcp_server', 'Expose pipelines as MCP tools', 'community', NULL, true),
                ('community_library', 'Browse and import community-contributed pipeline primitives', 'community', NULL, true),
                ('saved_views', 'Persistent saved views for run and pipeline lists', 'community', NULL, true),
                ('polling_trigger', 'Trigger pipelines by polling external endpoints', 'community', NULL, true),
                ('agent_signal_trigger', 'Trigger pipelines via agent-to-agent signals', 'community', NULL, true),
                ('helm_deployment', 'Helm chart for production Kubernetes deployment', 'community', NULL, true),
                ('remy', 'Remy in-app AI assistant', 'community', NULL, true),
                ('model_backend_management', 'Manage LLM backend connections and credentials', 'community', NULL, true),
                ('sso', 'Single sign-on via OIDC / SAML 2.0 providers', 'team', NULL, true),
                ('team_rbac', 'Team-level role-based access control', 'team', NULL, true),
                ('audit_viewer', 'Tamper-evident audit log viewer', 'team', NULL, true),
                ('admin_spend_limits', 'Per-organisation daily spend limits and budgets', 'team', NULL, true),
                ('admin_cost_controls', 'Budget overview, team budgets, alert thresholds, and billing settings', 'team', NULL, true),
                ('observability', 'OpenTelemetry export and LangSmith integration settings', 'team', NULL, true),
                ('view_modes', 'Multiple named UI views with admin-defined feature visibility per view and user/team/role assignment', 'team', NULL, true),
                ('environment_profiles', 'Sandbox environment profiles for code execution', 'team', NULL, true),
                ('plugin_management', 'Manage plugins, connectors, and node categories', 'team', NULL, true),
                ('admin_cost_breakdown', 'Monthly cost breakdown and anomaly detection across teams', 'team', NULL, true),
                ('admin_run_retention', 'Configure run retention policies and manual purge', 'team', NULL, true),
                ('error_forwarders', 'External error tracking and alerting integrations', 'team', NULL, true),
                ('schema_version_history', 'Version history and diff for schema definitions', 'team', NULL, true),
                ('schema_union_types', 'Union types and polymorphic schemas', 'v1', NULL, true),
                ('migration_cli', 'CLI tool for migrating pipelines across instances', 'v1', NULL, true),
                ('checkpoint_encryption', 'Encrypt pipeline checkpoints at rest', 'v2', NULL, true),
                ('audit_crypto_chain', 'Cryptographic chaining of audit events for tamper evidence', 'v2', NULL, true),
                ('community_registry', 'Publish and discover community pipeline primitives', 'v2', NULL, true),
                ('prompt_optimization', 'Automated prompt tuning and optimisation', 'v2', NULL, true),
                ('pipeline_diff_rollback', 'Diff-based pipeline version comparison and rollback', 'v2', NULL, true)
            """)
        )


def downgrade() -> None:
    pass
