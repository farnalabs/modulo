"""FAR-902: schema enforcement metadata on runs.

Adds two nullable columns to ``runs`` — populated at finalization from
per-node enforcement records on ``run_node_outputs``:

- ``schema_validator_mode``: the mode the run actually executed under
  (``lenient``|``strict``).  Derived from enforcement records.
- ``schema_validation_outcome``: the run's overall validation outcome
  (derived from per-attempt outcomes).

All nullable — runs without schema enforcement have NULLs.  No data backfill
(existing rows stay NULL).

Revision ID: 0253_runs_enforcement_mode_outcome
Revises: 0252_enforcement_daily_facts
Create Date: 2026-09-20
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0253_runs_enforcement_mode_outcome"
down_revision = "0252_enforcement_daily_facts"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    columns = {c["name"] for c in inspect(bind).get_columns(table)}
    return column in columns


def upgrade() -> None:
    if not _is_postgres():
        return
    if not _has_column("runs", "schema_validator_mode"):
        op.add_column(
            "runs",
            sa.Column(
                "schema_validator_mode",
                sa.String(30),
                nullable=True,
                comment="schema validator mode the run executed under (lenient|strict, FAR-902)",
            ),
        )
    if not _has_column("runs", "schema_validation_outcome"):
        op.add_column(
            "runs",
            sa.Column(
                "schema_validation_outcome",
                sa.String(40),
                nullable=True,
                comment="run-level schema validation outcome derived from per-node enforcement records (FAR-902)",
            ),
        )


def downgrade() -> None:
    if not _is_postgres():
        return
    if _has_column("runs", "schema_validation_outcome"):
        op.drop_column("runs", "schema_validation_outcome")
    if _has_column("runs", "schema_validator_mode"):
        op.drop_column("runs", "schema_validator_mode")
