"""FAR-902: schema enforcement aggregate counters on run_daily_facts.

Adds four nullable integer columns to ``run_daily_facts`` — the run-level
aggregate counters derived from per-attempt enforcement records on
``run_node_outputs``:

- ``enforcement_native_count``: attempts with native structured output.
- ``enforcement_verbatim_count``: attempts with verbatim output.
- ``enforcement_repair_count``: total repair loop invocations.
- ``enforcement_wasted_count``: total wasted attempts (schema rejection only).

All nullable — runs without schema enforcement have NULLs.  No data backfill
(existing rows stay NULL until the compensating sweep corrects them).

Downgrade: drops all four columns.

Revision ID: 0250b_enforcement_daily_facts
Revises: 0250_schema_enforcement_telemetry
Create Date: 2026-09-20
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0250b_enforcement_daily_facts"
down_revision = "0250_schema_enforcement_telemetry"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("enforcement_native_count", "count of attempts with native structured output (FAR-902)"),
    ("enforcement_verbatim_count", "count of attempts with verbatim output (FAR-902)"),
    ("enforcement_repair_count", "total repair loop invocations across all attempts (FAR-902)"),
    ("enforcement_wasted_count", "total wasted attempts (schema rejection only, FAR-902)"),
)


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    columns = {c["name"] for c in inspect(bind).get_columns(table)}
    return column in columns


def upgrade() -> None:
    if not _is_postgres():
        return
    for col_name, comment in _COLUMNS:
        if not _has_column("run_daily_facts", col_name):
            op.add_column(
                "run_daily_facts",
                sa.Column(col_name, sa.Integer(), nullable=True, comment=comment),
            )


def downgrade() -> None:
    if not _is_postgres():
        return
    for col_name, _ in _COLUMNS:
        if _has_column("run_daily_facts", col_name):
            op.drop_column("run_daily_facts", col_name)
