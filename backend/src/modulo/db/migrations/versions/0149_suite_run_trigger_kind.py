"""Trigger run_kind discriminator + eval_suite_id binding (FAR-377).

Revision ID: 0149_suite_run_trigger_kind
Revises: 0148_pipeline_snapshot_versioning_far420
Create Date: 2026-08-25

Renumbered from the former ``0138_trigger_run_kind_suite`` to resolve the
collision with main's merged ``0138_eval_versioning`` (FAR-382, PR 1947). The
branch's former ``eval_results.eval_definition_version`` stamp was dropped here
because ``0138_eval_versioning`` (now the ancestor) already owns that column;
this migration adds only the FAR-377 trigger columns.

Renumbered a second time from ``0143_suite_run_trigger_kind`` to
``0144_suite_run_trigger_kind`` to resolve the collision with main's merged
``0143_rest_connector_profile`` (FAR-412, PR 2009's merge base). It now chains
off the real main head ``0143_rest_connector_profile``.

Renumbered a third time from ``0144_suite_run_trigger_kind`` to
``0147_suite_run_trigger_kind`` to resolve the collision with main's merged
``0144_broaden_notification_status_in_app`` (PR 2009's second merge base). It now
chains off the real main head ``0144_broaden_notification_status_in_app`` and is
the single head of the merged tree.

Renumbered a fourth time from ``0147_suite_run_trigger_kind`` to
``0147_suite_run_trigger_kind`` to resolve the collision with main's merged
``0145_spend_ceiling`` and ``0146_extend_runs_status_cost_ceiling`` (PR 2009's
third merge base). It now chains off the real main head
``0146_extend_runs_status_cost_ceiling`` and is the single head of the merged
tree.

Renumbered a fifth time from ``0147_suite_run_trigger_kind`` to
``0148_suite_run_trigger_kind`` to resolve the collision with main's merged
``0147_json_to_jsonb_standardize`` (PR 2009's fourth merge base). It now chains
off the real main head ``0147_json_to_jsonb_standardize`` and is the single head
of the merged tree.

Renumbered a sixth time from ``0148_suite_run_trigger_kind`` to
``0149_suite_run_trigger_kind`` to resolve the collision with main's merged
``0148_pipeline_snapshot_versioning_far420`` (PR 2009's fifth/merge-queue merge
base). It now chains off the real main head
``0148_pipeline_snapshot_versioning_far420`` and is the single head of the
merged tree.

Scheduled / event-driven eval execution (FAR-377). A ``trigger`` already owns a
``pipeline_id`` (the suite's owning/placeholder pipeline, satisfying the existing
FK + constraints) but has no way to say "this scheduled/event-driven trigger
fires a *SuiteRun* rather than a *Run*". This migration adds:

* ``triggers.run_kind`` — a run-kind discriminator: ``'run'`` (DEFAULT, the
  existing behaviour) or ``'suite_run'``. When ``run_kind = 'suite_run'`` the
  cron/ongoing/event dispatch path enqueues a **SuiteRun** execution instead of a
  pipeline ``Run``.
* ``triggers.eval_suite_id`` — nullable FK to ``eval_suites``. The eval
  dataset + schedule config + model backend + scenario inputs live in the
  trigger's ``config_json``; this column pins the suite the trigger runs.
  ``pipeline_id`` remains NOT NULL (it is the suite's owning/placeholder
  pipeline).

Both columns are ADDITIVE and nullable-or-defaulted: an existing trigger row is
a ``run_kind = 'run'`` trigger with ``eval_suite_id = NULL`` and behaves exactly
as before. No shipped migration is modified.

Reversible: downgrade drops the index, FK + both columns.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0149_suite_run_trigger_kind"
down_revision: str | None = "0148_pipeline_snapshot_versioning_far420"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    if is_pg:
        op.execute("SET search_path TO public")

    op.add_column("triggers", sa.Column("run_kind", sa.String(length=20), nullable=False, server_default="run"))
    op.add_column("triggers", sa.Column("eval_suite_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_triggers_eval_suite_id",
        "triggers",
        "eval_suites",
        ["eval_suite_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_triggers_eval_suite_id", "triggers", ["eval_suite_id"])
    op.create_check_constraint(
        "ck_triggers_run_kind",
        "triggers",
        "run_kind IN ('run', 'suite_run')",
    )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    if is_pg:
        op.execute("SET search_path TO public")

    op.drop_constraint("ck_triggers_run_kind", "triggers", type_="check")
    op.drop_index("ix_triggers_eval_suite_id", table_name="triggers")
    op.drop_constraint("fk_triggers_eval_suite_id", "triggers", type_="foreignkey")
    op.drop_column("triggers", "eval_suite_id")
    op.drop_column("triggers", "run_kind")
