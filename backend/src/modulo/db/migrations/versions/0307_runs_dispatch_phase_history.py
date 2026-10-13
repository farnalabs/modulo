"""FAR-1714: runs.dispatch_phase_history + runs.dispatch_phase_write_drops.

Revision ID: 0307_runs_dispatch_phase_history
Revises: 0306_trigger_secret_encryption
Create Date: 2026-10-13

Two additive columns on ``runs`` making the dispatch-phase story reconstructable
FROM THE DATABASE after the fact (the 10-08→10-12 claimed-but-never-dispatched
window could not be reconstructed at all: only the LAST phase survived in
``dispatch_phase`` / ``dispatch_phase_entered_at``, and the per-phase detail
lived in worker stdout, of which Fly retains ~100 lines):

* ``dispatch_phase_history`` (JSONB on Postgres / generic JSON on other
  backends — the repo-wide multi-backend parity convention) — append-only
  array of ``{"phase": ..., "entered_at": <ISO-8601 timestamptz>}``. One
  entry per durable phase write (``core/pipeline_execution``'s
  ``_PHASE_UPDATE_SQL``, token-fenced and monotonic-guarded like the scalar
  columns), PLUS one ``"claimed"`` entry appended by each of the four claim
  statements (execute/resume × plain/token) — so the ``claimed`` entries mark
  ATTEMPT boundaries and every attempt's claim time survives (the scalar
  floor is overwritten by later phases). Cumulative across re-claims: never
  reset, because the wedged prior attempt is exactly what forensics need.
  NULL for rows that predate this migration.

* ``dispatch_phase_write_drops`` (integer NOT NULL DEFAULT 0) — how many
  durable phase writes the run abandoned at the 2 s
  ``PHASE_WRITE_TIMEOUT_SECONDS`` bound (timeout / DB error). The durable
  FAR-1511 saturation signal (10-13 drops/min observed on prod, previously
  log-only). Cumulative across re-claims; additive-only (no attempt can
  stomp another's count).

Both are METADATA-ONLY adds — a nullable ``ADD COLUMN`` and an ``ADD COLUMN``
with a constant default (Postgres 11+ fast default): no table rewrite, no
ACCESS EXCLUSIVE held for a backfill, safe on the hot ``runs`` table (the
FAR-403 ``ALTER ... TYPE`` lesson applies to type rewrites, not adds).
INTERNAL ONLY — neither column is API-projected (absent from ``RunResponse`` /
``_build_list_item`` / MCP payloads), so no response-shape change accompanies
this, and the ORM model (``db/models/run.py``) maps both in the SAME change
(model/migration parity, the database.md rule).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0307_runs_dispatch_phase_history"
down_revision: str | None = "0306_trigger_secret_encryption"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "dispatch_phase_history",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )
    op.add_column(
        "runs",
        sa.Column("dispatch_phase_write_drops", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("runs", "dispatch_phase_write_drops")
    op.drop_column("runs", "dispatch_phase_history")
