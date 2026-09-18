"""HITL decision briefing (FAR-613): ``hitl_claims.context_json``.

Revision ID: 0190_hitl_claim_context_json
Revises: 0189_agent_runner_bindings
Create Date: 2026-09-06

The executor's interrupt handler (``_handle_graph_interrupt``) now captures a
BOUNDED, failure-isolated context bundle at gate-fire time — the resolved gate
config description, the JMESPath condition expression, the trigger kind
(edge-condition gate vs HITL-node gate), the source node id/label, excerpted
artifacts relevant to the condition, the raising output's ``reason`` (node
gates), and the pipeline name. Persisting it on the claim row means the
reviewer's briefing is available from ``GET /api/v1/runs/{run_id}/hitl/pending``
and ``GET /api/v1/hitl/pending`` WITHOUT a per-gate snapshot walk, and the
briefing reflects the graph state at FIRE time even if the pipeline is edited
afterwards.

The column is nullable (legacy gates that fired before this column existed
carry NULL context — the UI renders a muted "no description" fallback).
Postgres creates it as ``jsonb`` (the repo's JSON storage standard — the
``blocked_partial_summary`` precedent, migration 0111); non-Postgres dialects
use the ORM's generic ``JSON`` for SQLite/MariaDB parity (the model maps the
column as generic JSON for the same reason). ``op.add_column`` with an inline
type is portable DDL (plain ``ADD COLUMN`` — no table-level constraint), so
the unit round-trip harness runs it on SQLite and Postgres alike.

Downgrade drops exactly the column the upgrade added (additive, nullable,
never backfilled — safe).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0190_hitl_claim_context_json"
down_revision: str | None = "0189_agent_runner_bindings"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.add_column("hitl_claims", sa.Column("context_json", JSONB(), nullable=True))
    else:
        op.add_column("hitl_claims", sa.Column("context_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("hitl_claims", "context_json")
