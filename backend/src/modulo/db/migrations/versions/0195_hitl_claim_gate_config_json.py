"""FAR-634: ``hitl_claims.gate_config_json`` — claim-stamped gate config.

Revision ID: 0195_hitl_claim_gate_config_json
Revises: 0200_runs_runner_marker_sweep_index
Create Date: 2026-09-08

The executor's interrupt handler (``_handle_graph_interrupt``) resolves the
fired gate's ``hitl_gate_config`` and stamps it on the claim row, so the
human_only resolver (``db.crud.hitl_gate_config.resolve_hitl_gate_config``)
reads it in ONE claim-row lookup instead of walking snapshot edges -> node
configs -> live edges on every non-browser decision. The stamped config is
the graph state at FIRE time — the authoritative policy for the gate that
actually fired, even if the pipeline is edited afterwards.

The column is nullable: gates that fired before this column existed carry
NULL and the resolver's existing snapshot/live walk covers them (fallback
intact — legacy semantics unchanged, including the fail-closed
``hitl_gate_exists_but_unresolved`` path for rows with no stamp AND an
unresolvable snapshot). A stamp failure at fire time is failure-isolated
(mirrors the FAR-613 ``context_json`` capture): the gate still fires with
NULL config and the resolver falls back to the walk.

Postgres creates it as ``jsonb`` (the repo's JSON storage standard — same
template as migration 0190); non-Postgres dialects use the ORM's generic
``JSON`` for SQLite/MariaDB parity (the model maps the column as generic
JSON for the same reason). ``op.add_column`` with an inline type is portable
DDL (plain ``ADD COLUMN`` — no table-level constraint), so the unit
round-trip harness runs it on SQLite and Postgres alike.

Downgrade drops exactly the column the upgrade added (additive, nullable,
never backfilled — safe).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0195_hitl_claim_gate_config_json"
down_revision: str | None = "0200_runs_runner_marker_sweep_index"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.add_column("hitl_claims", sa.Column("gate_config_json", JSONB(), nullable=True))
    else:
        op.add_column("hitl_claims", sa.Column("gate_config_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("hitl_claims", "gate_config_json")
