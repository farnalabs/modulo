"""Add telemetry_bytes to run_daily_facts (FAR-125 telemetry sizing).

Revision ID: 0078_run_daily_facts_telemetry_bytes
Revises: 0077_add_stalled_status
Create Date: 2026-08-10

Adds ``telemetry_bytes`` to the analytics fact table: the serialised size of
``Run.node_telemetry_json`` (json.dumps length) when present. The ORM model
maps the column, but migration 0071 (which added ``output_bytes``) predates the
FAR-125 split of telemetry out of ``outputs_json`` — the sibling column was
never migrated. Without this, ``record_run_facts`` (and the analytics
maintenance backfill) hit ``UndefinedColumnError`` on every write carrying a
``telemetry_bytes`` value.

Only ADD COLUMN — no RLS policy, role, or index changes: the new column
inherits the table's owner and the existing ``rls_org_isolation`` policy applies
to it automatically (matching migrations 0071 / 0076). Nullable, BIGINT, no
index: it is written once per terminal run alongside the other size/denormalised
columns.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0078_run_daily_facts_telemetry_bytes"
down_revision: str | None = "0077_add_stalled_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "run_daily_facts",
        sa.Column(
            "telemetry_bytes",
            sa.BigInteger(),
            nullable=True,
            comment="serialised size of Run.node_telemetry_json (json.dumps length) when present",
        ),
    )


def downgrade() -> None:
    op.drop_column("run_daily_facts", "telemetry_bytes")
