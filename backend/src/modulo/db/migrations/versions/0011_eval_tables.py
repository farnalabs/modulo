"""Create eval_definitions and eval_results tables.

Revision ID: 0011_eval_tables
Revises: 0010_user_last_login_token_families
Create Date: 2026-06-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_eval_tables"
down_revision: str | None = "0010_user_last_login_token_families"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "eval_definitions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("pipeline_id", sa.UUID(), nullable=False),
        sa.Column("node_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("eval_type", sa.String(30), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("failure_behaviour", sa.String(10), nullable=False, server_default=sa.text("'warn'")),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pipeline_id"], ["pipelines.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_eval_definitions_organisation_id"),
        "eval_definitions",
        ["organisation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_eval_definitions_pipeline_id"),
        "eval_definitions",
        ["pipeline_id"],
        unique=False,
    )
    op.create_check_constraint(
        "ck_eval_definitions_type",
        "eval_definitions",
        "eval_type IN ('llm_judge', 'regex', 'json_schema', 'custom_function')",
    )
    op.create_check_constraint(
        "ck_eval_definitions_failure_behaviour",
        "eval_definitions",
        "failure_behaviour IN ('warn', 'block')",
    )

    op.create_table(
        "eval_results",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("node_id", sa.UUID(), nullable=True),
        sa.Column("eval_id", sa.UUID(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("detail", sa.String(2000), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["eval_id"], ["eval_definitions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_eval_results_organisation_id"),
        "eval_results",
        ["organisation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_eval_results_run_id"),
        "eval_results",
        ["run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_eval_results_run_id"), table_name="eval_results")
    op.drop_index(op.f("ix_eval_results_organisation_id"), table_name="eval_results")
    op.drop_table("eval_results")
    op.drop_index(op.f("ix_eval_definitions_pipeline_id"), table_name="eval_definitions")
    op.drop_index(op.f("ix_eval_definitions_organisation_id"), table_name="eval_definitions")
    op.drop_table("eval_definitions")
