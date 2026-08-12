"""Add 'budget_exceeded' as a terminal run status (FAR-104)

Revision ID: 0090_add_budget_exceeded_status
Revises: 0088_drop_stages
Create Date: 2026-08-12

FAR-104 implements per-agent token budget enforcement in the cost controller:
when a run's accumulated tokens for an agent node exceed that agent's
``token_budget``, the run transitions to the terminal ``budget_exceeded``
state (error message "This run exceeded its token budget."). The status must
be legal under ``ck_runs_status``, so this migration recreates the constraint
with ``budget_exceeded`` added.

No production row can hold ``budget_exceeded`` yet because no code has ever
written it (the status is introduced by this change).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0090_add_budget_exceeded_status"
down_revision: str | None = "0088_drop_stages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUN_STATUSES = (
    "'pending', 'running', 'awaiting_human', 'claimed', 'complete', 'failed', "
    "'cancelled', 'eval_failed', 'stalled', 'budget_exceeded'"
)
# The status set 0077 installed (no 'budget_exceeded').
_RUN_STATUSES_0077 = (
    "'pending', 'running', 'awaiting_human', 'claimed', 'complete', 'failed', 'cancelled', 'eval_failed', 'stalled'"
)


def upgrade() -> None:
    op.drop_constraint("ck_runs_status", "runs", type_="check")
    op.create_check_constraint("ck_runs_status", "runs", f"status IN ({_RUN_STATUSES})")


def downgrade() -> None:
    op.drop_constraint("ck_runs_status", "runs", type_="check")
    op.create_check_constraint("ck_runs_status", "runs", f"status IN ({_RUN_STATUSES_0077})")
