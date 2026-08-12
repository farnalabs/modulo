"""Create modulo_journey_facts table (FAR-143 per-writer journey denominators)

Revision ID: 0085_journey_facts
Revises: 0084_journeys_table
Create Date: 2026-08-12

One row per ``(run_id, writer)`` — the self-report parse-failure /
finalise-attempt denominators written by the journey finalise hook (fail-open),
enough to compute a 7d self-report parse-failure ratio after the source
``runs`` rows are swept by retention.

``run_id`` is deliberately NOT a FK — like ``run_daily_facts`` and
``journeys.latest_terminal_run_id``, the fact must survive the 90-day run
purge (a future "fix" into an FK breaks retention).

Tenant model mirrors ``journeys`` (FAR-142): strict org RLS
(``rls_org_isolation``). There is no ``owner_team_id`` column here (the
counters are org-scoped diagnostics), so no ``enforce_same_organisation``
tenant trigger is needed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0085_journey_facts"
down_revision: str | None = "0084_journeys_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "modulo_journey_facts"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("writer", sa.String(length=30), nullable=False),
        sa.Column("parse_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("finalise_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "writer", name="uq_modulo_journey_facts_run_writer"),
    )
    op.create_index(op.f("ix_modulo_journey_facts_organisation_id"), _TABLE, ["organisation_id"], unique=False)
    op.create_index("ix_modulo_journey_facts_org_created", _TABLE, ["organisation_id", "created_at"], unique=False)
    # Literal DDL so the RLS-coverage architecture test can detect this table
    # (it scans for `ALTER TABLE "<table>" ENABLE ROW LEVEL SECURITY`).
    strict = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"
    op.execute(sa.text('ALTER TABLE "modulo_journey_facts" ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'CREATE POLICY rls_org_isolation ON "{_TABLE}" USING ({strict})'))


def downgrade() -> None:
    op.execute(sa.text(f'DROP POLICY IF EXISTS rls_org_isolation ON "{_TABLE}"'))
    op.execute(sa.text(f'ALTER TABLE "{_TABLE}" DISABLE ROW LEVEL SECURITY'))
    op.drop_index("ix_modulo_journey_facts_org_created", table_name=_TABLE)
    op.drop_index(op.f("ix_modulo_journey_facts_organisation_id"), table_name=_TABLE)
    op.drop_table(_TABLE)
