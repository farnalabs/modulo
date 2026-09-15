"""Add stdout_retention_config to pipelines and pipeline_snapshots (FAR-811).

Adds a nullable JSON column ``stdout_retention_config`` to both the
``pipelines`` and ``pipeline_snapshots`` tables.  The column stores the
pipeline-level default for sandbox stdout retention, shaped as:

    {"mode": "tail"|"full", "max_bytes": <positive int>}

NULL means "no pipeline override — inherit from the org ceiling only".

Revision ID: 0241_pipeline_stdout_retention_config
Revises: 0240_reinstate_organisations_audit_columns
Create Date: 2026-09-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0241_pipeline_stdout_retention_config"
down_revision = "0240_reinstate_organisations_audit_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pipelines", sa.Column("stdout_retention_config", sa.JSON, nullable=True))
    op.add_column("pipeline_snapshots", sa.Column("stdout_retention_config", sa.JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("pipeline_snapshots", "stdout_retention_config")
    op.drop_column("pipelines", "stdout_retention_config")
