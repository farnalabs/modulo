"""Add node_telemetry_json to runs

Revision ID: 0074_run_node_telemetry_json
Revises: 0073_run_node_attempt_count
Create Date: 2026-08-10

Adds ``node_telemetry_json`` to the ``runs`` table. This column stores the
per-node telemetry dict (status, wall_clock_time_ms, exit_code, agent_stdout,
changed_files, ...) split OUT of ``outputs_json`` by the Agent Return Contract
(FAR-125) so each node's output return value and its telemetry can be persisted
separately. NULL for pre-split runs; written atomically alongside
``outputs_json`` by the run-status/outputs CRUD writers. JSON, nullable, no
index.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0074_run_node_telemetry_json"
down_revision: str | None = "0073_run_node_attempt_count"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("node_telemetry_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "node_telemetry_json")
