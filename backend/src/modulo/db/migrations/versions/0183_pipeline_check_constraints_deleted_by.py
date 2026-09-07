"""Add missing CHECK constraints on pipelines and deleted_by audit column.

Revision ID: 0183_pipeline_check_constraints_deleted_by
Revises: 0182_hitl_claims_active_sweep_indexes
Create Date: 2026-09-07

Findings from improve-database lens analysis of the pipeline model cluster:

1. ``pipelines.max_duration_seconds`` — NOT NULL with default 3600 but no
   positivity check; negative values silently accepted.
2. ``pipelines.stale_run_timeout_minutes`` — NOT NULL with default 30 but
   no positivity check.
3. ``pipelines.max_steps`` — nullable; allows zero or negative step limits
   when set.
4. ``pipelines.token_budget`` — nullable; allows zero or negative budgets
   when set.
5. ``pipelines.circuit_breaker_threshold`` — nullable; allows zero or
   negative spend thresholds when set.
6. ``pipelines`` lacks ``deleted_by`` column — inconsistent with
   ``eval_definitions``, ``eval_datasets``, and ``scheduled_reports`` which
   pair ``deleted_at`` with ``deleted_by`` for audit trail completeness.

All CHECK constraints use ``NOT VALID`` + ``VALIDATE CONSTRAINT`` pattern
(consistent with 0165/0170) for online-safe deployment.  ``deleted_by`` is
added as nullable with no backfill (legacy rows have no creator info
available) — same pattern as 0175.

Deploy-safety: CHECK validation acquires SHARE UPDATE EXCLUSIVE lock (not
ACCESS EXCLUSIVE) when using NOT VALID + VALIDATE, so concurrent writes are
not blocked.  The ADD COLUMN for ``deleted_by`` is metadata-only (PG 11+)
for a nullable column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0183_pipeline_check_constraints_deleted_by"
down_revision: str | None = "0182_hitl_claims_active_sweep_indexes"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "pipelines"

_CONSTRAINTS: list[tuple[str, str]] = [
    ("ck_pipelines_max_duration_positive", "max_duration_seconds > 0"),
    ("ck_pipelines_stale_timeout_positive", "stale_run_timeout_minutes > 0"),
    (
        "ck_pipelines_max_steps_positive",
        "max_steps IS NULL OR max_steps > 0",
    ),
    (
        "ck_pipelines_token_budget_positive",
        "token_budget IS NULL OR token_budget > 0",
    ),
    (
        "ck_pipelines_circuit_breaker_threshold_positive",
        "circuit_breaker_threshold IS NULL OR circuit_breaker_threshold > 0",
    ),
]


def upgrade() -> None:
    # --- CHECK constraints (NOT VALID for online-safe add) ---
    for name, expr in _CONSTRAINTS:
        op.execute(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {name} CHECK ({expr}) NOT VALID")
        op.execute(f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {name}")

    # --- deleted_by audit column ---
    op.add_column(
        _TABLE,
        sa.Column(
            "deleted_by",
            sa.Uuid(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "deleted_by")

    for name, _ in reversed(_CONSTRAINTS):
        op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {name}")
