"""Add parameter_schemas and parameter_sets tables.

Implements RFC §10 Phase 1: Parameter Schema + Parameter Sets data model.

Changes:
  - Create parameter_schemas table
  - Create parameter_sets table
  - Add parameter_schema_id column to agents table
  - Add indexes on FK columns

Revision ID: 0012_parameter_schemas
Revises: 0011_database_review_fixes
Create Date: 2026-07-15

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_parameter_schemas"
down_revision: str | None = "0011_database_review_fixes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _create_parameter_schemas_table()
    _create_parameter_sets_table()
    _add_agent_column()
    _add_indexes()


def downgrade() -> None:
    _remove_indexes()
    _remove_agent_column()
    _drop_parameter_sets_table()
    _drop_parameter_schemas_table()


def _create_parameter_schemas_table() -> None:
    op.create_table(
        "parameter_schemas",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), sa.ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("parameters", sa.JSON(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def _drop_parameter_schemas_table() -> None:
    op.drop_table("parameter_schemas")


def _create_parameter_sets_table() -> None:
    op.create_table(
        "parameter_sets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "parameter_schema_id", sa.Uuid(), sa.ForeignKey("parameter_schemas.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("organisation_id", sa.Uuid(), sa.ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("values", sa.JSON(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("parameter_schema_id", "name", name="uq_parameter_sets_schema_name"),
        sa.PrimaryKeyConstraint("id"),
    )


def _drop_parameter_sets_table() -> None:
    op.drop_table("parameter_sets")


def _add_agent_column() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "parameter_schema_id",
            sa.Uuid(),
            sa.ForeignKey("parameter_schemas.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )


def _remove_agent_column() -> None:
    op.drop_constraint(op.f("fk_agents_parameter_schema_id"), "agents", type_="foreignkey")
    op.drop_column("agents", "parameter_schema_id")


def _add_indexes() -> None:
    op.create_index(
        op.f("ix_parameter_schemas_organisation_id"),
        "parameter_schemas",
        ["organisation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_parameter_sets_parameter_schema_id"),
        "parameter_sets",
        ["parameter_schema_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_parameter_sets_organisation_id"),
        "parameter_sets",
        ["organisation_id"],
        unique=False,
    )


def _remove_indexes() -> None:
    op.drop_index(op.f("ix_parameter_sets_organisation_id"), table_name="parameter_sets")
    op.drop_index(op.f("ix_parameter_sets_parameter_schema_id"), table_name="parameter_sets")
    op.drop_index(op.f("ix_parameter_schemas_organisation_id"), table_name="parameter_schemas")
