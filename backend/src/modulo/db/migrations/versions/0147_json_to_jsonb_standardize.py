"""Promote remaining ``json`` columns to ``jsonb`` (dist db-jsonb-standardize).

Revision ID: 0147_json_to_jsonb_standardize
Revises: 0146_extend_runs_status_cost_ceiling
Create Date: 2026-08-24

The codebase adopted ``jsonb`` as its JSON standard (see 0129_runs_json_to_jsonb
and the many columns created as ``jsonb`` directly), but ~67 columns are still
typed plain ``json`` in Postgres. ``jsonb`` gives binary storage, faster
containment/``@>`` operators, and GIN-indexability, so this migration brings the
remaining columns up to the same standard.

Columns that are already ``jsonb`` (e.g. ``runs.*``,
``feedback_records.correction_state``, ``metrics_staging.payload``,
``eval_suites.eval_definition_ids``) are excluded. The ``USING col::jsonb`` cast
is lossless (NULL stays NULL; well-formed ``json`` re-parses identically as
``jsonb``).

This is a Postgres-only change: SQLite/MariaDB use the ORM's generic ``JSON``
type, so the migration is skipped on non-Postgres dialects.

Downgrade reverts the same columns from ``jsonb`` back to ``json``.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0147_json_to_jsonb_standardize"
down_revision: str | None = "0146_extend_runs_status_cost_ceiling"
branch_labels: str | None = None
depends_on: str | None = None

# (table, column) pairs still typed ``json`` in Postgres that this migration
# promotes to ``jsonb``. Constants only - no user input reaches the DDL.
_JSON_TO_JSONB: tuple[tuple[str, str], ...] = (
    ("accounts", "preferences"),
    ("agents", "agent_commands"),
    ("agents", "prompt_version_history"),
    ("agents", "connector_type_refs"),
    ("agents", "required_environment_capabilities"),
    ("agents", "evals"),
    ("agents", "retry_policy"),
    ("audit_events", "payload_json"),
    ("composite_templates", "sub_pipeline_graph_json"),
    ("composite_templates", "parameter_ports_json"),
    ("connector_instances", "config_json"),
    ("connector_instances", "allowed_operations"),
    ("environment_profiles", "capabilities_json"),
    ("environment_profiles", "config_json"),
    ("environment_profiles", "secret_refs_json"),
    ("error_events", "context_json"),
    ("error_forwarder_configs", "config_json"),
    ("eval_cases", "input_payload"),
    ("eval_cases", "expected_output"),
    ("eval_definitions", "config_json"),
    ("feedback_records", "rejected_output"),
    ("hitl_claims", "decision_payload"),
    ("library_primitives", "tags"),
    ("library_primitives", "content_json"),
    ("library_sync_state", "manifest_json"),
    ("library_sync_state", "catalog_json"),
    ("lifecycle_maps", "content_json"),
    ("model_backends", "default_params"),
    ("model_backends", "fallback_backend_ids"),
    ("notification_endpoints", "events"),
    ("organisations", "settings_json"),
    ("organisations", "otel_config_json"),
    ("organisations", "export_bundle_json"),
    ("organisations", "guardrail_pins_json"),
    ("parameter_schemas", "parameters"),
    ("parameter_sets", "values"),
    ("pipelines", "run_context_defaults"),
    ("pipelines", "rate_limit_config"),
    ("pipeline_snapshots", "graph_json"),
    ("pipeline_snapshots", "connector_bindings_json"),
    ("pipeline_snapshots", "schema_pins_json"),
    ("pipeline_snapshots", "prompt_pins_json"),
    ("pipeline_snapshots", "model_backend_pins_json"),
    ("pipeline_snapshots", "composite_bindings_json"),
    ("pipeline_snapshots", "parameter_bindings_json"),
    ("pipeline_snapshots", "guardrail_pins_json"),
    ("pipeline_snapshots", "config_json"),
    ("pipeline_snapshots", "run_context_defaults"),
    ("chat_messages", "tool_calls_json"),
    ("chat_messages", "tool_results_json"),
    ("remy_skills", "triggers"),
    ("scheduled_reports", "config_json"),
    ("scheduled_reports", "recipient_config"),
    ("schema_versions", "definition_json"),
    ("sso_providers", "group_mappings"),
    ("system_config", "value"),
    ("teams", "notification_endpoints"),
    ("teams", "settings"),
    ("triggers", "config_json"),
    ("variant_groups", "variants"),
    ("saved_views", "filters"),
    ("saved_views", "columns"),
    ("webhook_payloads", "raw_payload"),
    ("workspace_leases", "resource_usage_json"),
    ("workspace_leases", "output_artifact_refs_json"),
    ("feature_flag_catalog", "depends_on"),
)


def _promote(target: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table, column in _JSON_TO_JSONB:
        op.execute(
            text(f'ALTER TABLE public."{table}" ALTER COLUMN "{column}" TYPE {target} USING "{column}"::{target};')
        )


def upgrade() -> None:
    _promote("jsonb")


def downgrade() -> None:
    _promote("json")
