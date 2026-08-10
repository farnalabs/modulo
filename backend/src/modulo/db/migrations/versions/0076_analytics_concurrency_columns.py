"""Add concurrency/slot-utilization columns to run_daily_facts (FAR-134).

Revision ID: 0076_analytics_concurrency_columns
Revises: 0075_runtime_hardening_columns
Create Date: 2026-08-10

Adds the absolute run-lifecycle instants and the full queue wait to the facts
table so the analytics surface can reconstruct slot utilization ("how many
runs were running / queued at any instant") WITHOUT reading live ``runs``
(which is purged after 90 days):

- ``dispatched_at`` / ``started_at`` / ``completed_at`` — absolute UTC instants
  copied from the source run. Deliberately NOT foreign keys — facts must
  survive the run purge (ADR 020).
- ``total_queue_wait_ms`` — ``Run.started_at - Run.created_at`` in ms: the FULL
  wait from run creation to execution start, covering capacity-deferral +
  the SAQ queue. NULL when either side is missing.

Also fixes the stale ``queue_wait_ms`` column comment shipped in migration
0071 (which documented the pre-FAR-133 ``dispatched_at - started_at`` sign).
0071 itself is NOT edited — a shipped migration must never be changed in
place; the corrected comment is applied here.

Only ADD COLUMN / COMMENT statements — no RLS policy, role, or index changes:
the new columns inherit the table's owner and the existing
``rls_org_isolation`` policy applies to them automatically. Non-Postgres dev
backends (SQLite / MariaDB) render the columns via ``sa.DateTime(timezone=True)``
the same way the surrounding migrations do.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0076_analytics_concurrency_columns"
down_revision: str | None = "0075_runtime_hardening_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (name, type, comment) — comments kept in sync with the ORM mapped_column.
_NEW_COLUMNS: list[tuple[str, sa.types.TypeEngine[object], str]] = [
    (
        "dispatched_at",
        sa.DateTime(timezone=True),
        "absolute UTC instant the run was dispatched to the queue — from Run.dispatched_at",
    ),
    (
        "started_at",
        sa.DateTime(timezone=True),
        "absolute UTC instant the run started executing — from Run.started_at",
    ),
    (
        "completed_at",
        sa.DateTime(timezone=True),
        "absolute UTC instant the run completed — from Run.completed_at",
    ),
    (
        "total_queue_wait_ms",
        sa.BigInteger(),
        "Run.started_at - Run.created_at (full wait from creation to start, capacity deferral + SAQ queue), else NULL",
    ),
]


def upgrade() -> None:
    for name, type_, comment in _NEW_COLUMNS:
        op.add_column("run_daily_facts", sa.Column(name, type_, nullable=True, comment=comment))
    # Correct the stale comment shipped in 0071 (pre-FAR-133 sign fix). The
    # shipped migration is untouched — this is the safe additive correction.
    # COMMENT ON COLUMN is Postgres DDL; guard so non-Postgres dev backends
    # (SQLite / MariaDB) still migrate cleanly (column comments are ignored
    # there, matching the docstring below).
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "COMMENT ON COLUMN run_daily_facts.queue_wait_ms IS "
            "'Run.started_at - Run.dispatched_at when both present, else NULL'"
        )


def downgrade() -> None:
    for name, _type, _comment in reversed(_NEW_COLUMNS):
        op.drop_column("run_daily_facts", name)
