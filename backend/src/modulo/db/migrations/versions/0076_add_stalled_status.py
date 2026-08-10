"""Add 'stalled' as a terminal run status

Revision ID: 0076_add_stalled_status
Revises: 0075_runtime_hardening_columns
Create Date: 2026-08-10

A sandbox-agent node that STALLS (idle watchdog fired — agent silent for
``stall_timeout_seconds``) or TIMES OUT (node wall-clock timeout) RETURNS a
failed output dict instead of raising, so the run was previously recorded as
``complete``. The executor now surfaces a ``stalled`` terminal status for such
runs (``_stream_graph`` returns ``("stalled", "executor_stalled", reason)``).

This migration recreates ``ck_runs_status`` WITHOUT the never-reached legacy
sub-state ``waiting_for_lock`` (already removed by 0075) and WITH ``stalled``
added. Any rows that somehow hold ``stalled`` before the constraint is re-added
would be rejected — but no production row can be ``stalled`` yet because no
code has ever written it (the status is introduced by this change).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0076_add_stalled_status"
down_revision: str | None = "0075_runtime_hardening_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUN_STATUSES = (
    "'pending', 'running', 'awaiting_human', 'claimed', 'complete', 'failed', 'cancelled', 'eval_failed', 'stalled'"
)
# The status set 0075 installed (no 'waiting_for_lock', no 'stalled').
_RUN_STATUSES_0075 = (
    "'pending', 'running', 'awaiting_human', 'claimed', 'complete', 'failed', 'cancelled', 'eval_failed'"
)


def upgrade() -> None:
    op.drop_constraint("ck_runs_status", "runs", type_="check")
    op.create_check_constraint("ck_runs_status", "runs", f"status IN ({_RUN_STATUSES})")


def downgrade() -> None:
    op.drop_constraint("ck_runs_status", "runs", type_="check")
    op.create_check_constraint("ck_runs_status", "runs", f"status IN ({_RUN_STATUSES_0075})")
