"""Promote cost_breakdown and node_token_usage from JSON to JSONB.

Revision ID: 0199_runs_json_to_jsonb
Revises: 0198_runs_add_missing_indexes
Create Date: 2026-09-08
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0199_runs_json_to_jsonb"
down_revision = "0198_runs_add_missing_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Promote cost_breakdown and node_token_usage from JSON to JSONB.
    # JSONB enables GIN indexing, containment (@>) queries, and path extraction
    # without loading the full document. These columns are queried by application
    # code (cost breakdown aggregation, node token usage analytics).
    # NOTE: outputs_json, input_payload, node_telemetry_json, raw_output_markers,
    # run_classification, blocked_partial_summary, guardrail_summary_json, work_item_refs,
    # variant_config_snapshot remain JSON for SQLite/MariaDB parity (document-shaped data
    # not queried at the DB level).
    op.execute("ALTER TABLE runs ALTER COLUMN cost_breakdown TYPE jsonb USING cost_breakdown::jsonb")
    op.execute("ALTER TABLE runs ALTER COLUMN node_token_usage TYPE jsonb USING node_token_usage::jsonb")

    # Promote error_detail from String(5000) to Text.
    # Error payloads (stack traces, diffs) can exceed 5000 chars; String truncates silently.
    op.execute("ALTER TABLE runs ALTER COLUMN error_detail TYPE text")


def downgrade() -> None:
    op.execute("ALTER TABLE runs ALTER COLUMN error_detail TYPE varchar(5000) USING error_detail::varchar(5000)")
    op.execute("ALTER TABLE runs ALTER COLUMN node_token_usage TYPE json USING node_token_usage::json")
    op.execute("ALTER TABLE runs ALTER COLUMN cost_breakdown TYPE json USING cost_breakdown::json")
