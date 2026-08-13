"""Per-org run_number sequence (atomic counter) — FAR-168

Revision ID: 0092_run_number_sequence
Revises: 0091_trigger_events_retention
Create Date: 2026-08-13

FAR-168 replaces the racy ``SELECT MAX(run_number)+1`` allocation in
``create_run`` with a per-org atomic counter: ``run_number_counters`` holds one
row per organisation and ``create_run`` bumps it via
``INSERT ... ON CONFLICT (organisation_id) DO UPDATE SET
next_run_number = next_run_number + 1 RETURNING next_run_number``, so
concurrent creates in the same org serialize on the counter row and can never
collide on ``uq_runs_org_run_number``.

Migration 0025 first introduced this table but the code reverted to MAX+1 and
migration 0035 dropped the orphaned table. This migration recreates it WITH the
two things that were missing then (an ORM model and an RLS policy), and seeds
each existing org's counter from its current ``MAX(run_number)`` so new runs
continue the sequence without collision.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0092_run_number_sequence"
down_revision: str | None = "0091_trigger_events_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "run_number_counters"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("next_run_number", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("organisation_id"),
    )
    # Seed each existing org's counter to continue from its current max, so the
    # sequence never collides with already-assigned run_numbers on migration.
    op.execute(
        sa.text(
            "INSERT INTO run_number_counters (organisation_id, next_run_number) "
            "SELECT organisation_id, COALESCE(MAX(run_number), 0) + 1 "
            "FROM runs GROUP BY organisation_id"
        )
    )
    # Literal DDL so the RLS-coverage architecture test can detect this table
    # (it scans for `ALTER TABLE "<table>" ENABLE ROW LEVEL SECURITY` — an
    # f-string placeholder would not match the regex).
    strict = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"
    op.execute(sa.text('ALTER TABLE "run_number_counters" ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'CREATE POLICY rls_org_isolation ON "run_number_counters" USING ({strict})'))


def downgrade() -> None:
    op.execute(sa.text(f'DROP POLICY IF EXISTS rls_org_isolation ON "{_TABLE}"'))
    op.execute(sa.text(f'ALTER TABLE "{_TABLE}" DISABLE ROW LEVEL SECURITY'))
    op.drop_table(_TABLE)
