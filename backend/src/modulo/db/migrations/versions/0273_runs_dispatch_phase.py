"""FAR-1088: runs.dispatch_phase / dispatch_phase_entered_at instrumentation columns.

Revision ID: 0273_runs_dispatch_phase
Revises: 0272_oauth_client_revoke_lookup_indexes
Create Date: 2026-10-01

Two additive, nullable columns on ``runs`` recording which dispatch phase a run
last entered — the durable trace for diagnosing claimed-but-nodeless runs:

* ``dispatch_phase`` (Text) — the phase label. The guaranteed floor is written
  by the claim itself: ``core/pipeline_execution`` stamps ``'claimed'`` inside
  the SAME atomic ``UPDATE ... RETURNING`` that claims the row, so a run that
  was claimed can never lack a phase even if no node ever executed, and every
  re-dispatch/re-claim resets it (a re-woken run never reports a previous
  attempt's phase). Later workstreams add the loading_setup / setup_complete /
  streaming writes and the terminaliser read.
* ``dispatch_phase_entered_at`` (timestamptz) — when that phase was entered.

Both are NULL for rows that predate this migration. INTERNAL ONLY — neither
column is API-projected (absent from ``RunResponse``, ``_build_list_item`` and
the MCP run payloads), so no response-shape change accompanies this.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0273_runs_dispatch_phase"
down_revision: str | None = "0272_oauth_client_revoke_lookup_indexes"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("dispatch_phase", sa.Text(), nullable=True))
    op.add_column(
        "runs",
        sa.Column("dispatch_phase_entered_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runs", "dispatch_phase_entered_at")
    op.drop_column("runs", "dispatch_phase")
