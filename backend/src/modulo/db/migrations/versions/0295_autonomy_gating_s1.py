"""Autonomy gating S1 — earned autonomy level + run_daily_facts.autonomy_level (FAR-1175).

Revision ID: 0295_autonomy_gating_s1
Revises: 0294_eval_results_org_fk
Create Date: 2026-10-10

ADR 043 slice S1 (evidence-driven autonomy gating). Two additive, nullable
columns — both NULL on existing rows, so behaviour is byte-identical until an
operator promotes/demotes a pipeline or the facts writer records an applied
level:

* ``pipelines.earned_autonomy_level`` — the runtime *earned* level, stored
  OUTSIDE the snapshot (like the circuit-breaker state) because it must change
  between runs without a pipeline edit. Read LIVE at every HITL gate when the
  ``autonomy_gating`` feature flag is on; in-flight runs apply
  ``min(pinned-at-run-start, live)`` so a demotion bites mid-run but a
  promotion never loosens an in-flight run (ADR 043 §3). CHECK
  ``ck_pipelines_earned_autonomy_level``. ``earned_autonomy_updated_at`` records
  the last mutation instant.

* ``run_daily_facts.autonomy_level`` — the effective autonomy the run resolved
  under, sourced from the run's ``run.autonomy_level_applied`` audit event at
  fact-write time so the analytics surface can bucket by autonomy without
  re-joining the audit chain (autonomy-study.md §3.3).
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0295_autonomy_gating_s1"
down_revision: str | None = "0294_eval_results_org_fk"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_LEVEL_VOCAB = "('manual_approval', 'notify_on_complete', 'fully_autonomous')"


def upgrade() -> None:
    op.add_column(
        "pipelines",
        sa.Column("earned_autonomy_level", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "pipelines",
        sa.Column("earned_autonomy_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_pipelines_earned_autonomy_level",
        "pipelines",
        f"earned_autonomy_level IS NULL OR earned_autonomy_level IN {_LEVEL_VOCAB}",
    )
    op.add_column(
        "run_daily_facts",
        sa.Column("autonomy_level", sa.String(length=30), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("run_daily_facts", "autonomy_level")
    op.drop_constraint("ck_pipelines_earned_autonomy_level", "pipelines", type_="check")
    op.drop_column("pipelines", "earned_autonomy_updated_at")
    op.drop_column("pipelines", "earned_autonomy_level")
