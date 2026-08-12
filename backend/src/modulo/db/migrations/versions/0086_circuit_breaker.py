"""Add per-pipeline cost-control circuit breaker columns (FAR-105, spec §8.10)

Revision ID: 0086_circuit_breaker
Revises: 0085_journey_facts
Create Date: 2026-08-12

Three additive columns on ``pipelines``:

- ``circuit_breaker_threshold`` (Numeric) — per-pipeline monthly spend
  threshold in USD. NULL = no breaker configured for this pipeline.
- ``circuit_breaker_tripped`` (Boolean, server_default false) — once set, the
  pipeline's triggers are paused and no new runs are allowed until an admin
  re-enables the pipeline (which clears the flag).
- ``circuit_breaker_tripped_at`` (DateTime) — when the breaker tripped, for
  operator observability.

Tenant model mirrors ``pipelines`` (existing RLS + ``enforce_same_organisation``
trigger already cover the table) — no new policy DDL is needed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0086_circuit_breaker"
down_revision: str | None = "0085_journey_facts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "pipelines"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("circuit_breaker_threshold", sa.Numeric(14, 6), nullable=True))
    op.add_column(
        _TABLE,
        sa.Column("circuit_breaker_tripped", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(_TABLE, sa.Column("circuit_breaker_tripped_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, "circuit_breaker_tripped_at")
    op.drop_column(_TABLE, "circuit_breaker_tripped")
    op.drop_column(_TABLE, "circuit_breaker_threshold")
