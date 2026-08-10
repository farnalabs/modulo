"""Runtime cutover: hardening columns, claim-token backfill, status cleanup

Revision ID: 0075_runtime_hardening_columns
Revises: 0073_run_node_attempt_count
Create Date: 2026-08-10

Runtime hardening columns for the SAQ cutover (plan F4):

* ``runs.enqueue_failed_at`` — timestamptz set when a dispatch enqueue fails.
* ``runs.sandbox_dispatch_state`` — text holding the sandbox dispatch lifecycle
  state so dispatch.py can resume/retry without a live in-memory handle.
* ``runs.sandbox_id`` — text E2B sandbox id surfaced for observability.
* ``hitl_claims.decision_payload`` — jsonb payload captured when a HITL gate
  decision is delivered (approve/reject) for the audit trail.

Claim-token hardening:

* ``runs.claim_token`` becomes NOT NULL. The F3a claim-token fence key
  (run:{id}:e2b:{claim_token}) is being removed and the claim token itself now
  lives permanently in ``runs.claim_token``. NULLs are backfilled with
  ``gen_random_uuid()::text`` in batches of 500 (the runs table is hot; a
  single UPDATE would hold a long table lock). A ``server_default`` of
  ``gen_random_uuid()::text`` keeps old-app INSERTs during the bluegreen
  cutover from violating NOT NULL.

Status-constraint cleanup:

* ``ck_runs_status`` is recreated WITHOUT the never-entered
  ``waiting_for_lock`` sub-state (the advisory-lock concurrency scope was never
  implemented — the product map records the status value as never entered).
  Any rows that somehow hold ``waiting_for_lock`` are backfilled to ``pending``
  before the constraint is re-added.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0075_runtime_hardening_columns"
down_revision: str | None = "0074_run_node_telemetry_json"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUN_STATUSES_WITHOUT_WAITING = (
    "'pending', 'running', 'awaiting_human', 'claimed', 'complete', 'failed', 'cancelled', 'eval_failed'"
)
_RUN_STATUSES_LEGACY = (
    "'pending', 'running', 'awaiting_human', 'claimed', 'waiting_for_lock', "
    "'complete', 'failed', 'cancelled', 'eval_failed'"
)

_BATCH_SIZE = 500


def _backfill_claim_tokens() -> None:
    """Backfill NULL runs.claim_token in batches to avoid a long table lock."""
    bind = op.get_bind()
    while True:
        result = bind.execute(
            sa.text(
                "UPDATE runs SET claim_token = gen_random_uuid()::text "
                "WHERE claim_token IS NULL AND id IN ("
                "SELECT id FROM runs WHERE claim_token IS NULL LIMIT :limit"
                ")"
            ),
            {"limit": _BATCH_SIZE},
        )
        if result.rowcount == 0:
            break


def upgrade() -> None:
    # 1. Claim-token hardening: batch backfill, then NOT NULL + server default
    #    so old-app INSERTs during the bluegreen cutover never violate NOT NULL.
    _backfill_claim_tokens()
    op.alter_column(
        "runs",
        "claim_token",
        nullable=False,
        server_default=sa.text("gen_random_uuid()::text"),
    )

    # 2-4. Runtime hardening columns on runs.
    op.add_column("runs", sa.Column("enqueue_failed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("sandbox_dispatch_state", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("sandbox_id", sa.Text(), nullable=True))

    # 5. HITL decision audit payload.
    op.add_column("hitl_claims", sa.Column("decision_payload", JSONB(), nullable=True))

    # 6. Recreate ck_runs_status WITHOUT the never-entered 'waiting_for_lock'
    #    sub-state. Backfill any stray rows first (the drop must come before the
    #    backfill so the pre-existing constraint cannot reject the rewrite).
    op.drop_constraint("ck_runs_status", "runs", type_="check")
    op.execute("UPDATE runs SET status = 'pending' WHERE status = 'waiting_for_lock'")
    op.create_check_constraint("ck_runs_status", "runs", f"status IN ({_RUN_STATUSES_WITHOUT_WAITING})")


def downgrade() -> None:
    # Restore the legacy status set first (mirror of the upgrade's step 6).
    op.drop_constraint("ck_runs_status", "runs", type_="check")
    op.create_check_constraint("ck_runs_status", "runs", f"status IN ({_RUN_STATUSES_LEGACY})")

    op.drop_column("hitl_claims", "decision_payload")

    op.drop_column("runs", "sandbox_id")
    op.drop_column("runs", "sandbox_dispatch_state")
    op.drop_column("runs", "enqueue_failed_at")

    # claim_token returns to nullable (the backfill values are retained — the
    # downgrade only relaxes the constraint, it does not undo generated data).
    op.alter_column("runs", "claim_token", server_default=None, nullable=True)
