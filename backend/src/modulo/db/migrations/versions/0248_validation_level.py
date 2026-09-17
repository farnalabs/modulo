"""FAR-935: add validation_level to connector_instances and model_backends.

Adds ``validation_level`` (String(30), nullable) to both tables. The column is
populated by the health/canary sweep (``health_sweep.py``), which mirrors the
``last_health_check_at`` pattern: no default, nullable, written on the next
sweep pass.

No data backfill: existing rows get ``NULL`` until the next sweep run computes
and writes the level.

Downgrade: drops both columns.

Revision ID: 0248_validation_level
Revises: 0247_sso_presets
Create Date: 2026-09-17
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0248_validation_level"
down_revision = "0247_sso_presets"
branch_labels = None
depends_on = None

_TABLES = ("connector_instances", "model_backends")


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    columns = {c["name"] for c in inspect(bind).get_columns(table)}
    return column in columns


def upgrade() -> None:
    if not _is_postgres():
        return
    for table in _TABLES:
        if not _has_column(table, "validation_level"):
            op.add_column(table, sa.Column("validation_level", sa.String(30), nullable=True))


def downgrade() -> None:
    if not _is_postgres():
        return
    for table in _TABLES:
        if _has_column(table, "validation_level"):
            op.drop_column(table, "validation_level")
