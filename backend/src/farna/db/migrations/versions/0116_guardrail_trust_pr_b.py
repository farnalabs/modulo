"""Guardrail trust model PR B (FAR-309): snapshot pin fingerprint + two-step soft-delete.

Revision ID: 0116_guardrail_trust_pr_b
Revises: 0115_notification_preferences
Create Date: 2026-08-18

PR B ships three trust-model controls:

1. **Run-start snapshot-integrity re-verify** — ``pipeline_snapshots`` gains
   ``guardrail_pins_fingerprint`` (canonical SHA-256 over the serialized
   ``guardrail_pins_json`` captured at snapshot creation). The replay seam
   re-computes the fingerprint of the LOADED pins and fails closed on a
   mismatch (tampered/drifted pin set). Nullable: legacy snapshots predating
   the fingerprint are still trusted.

2. **Two-step soft-delete with audit** — ``eval_definitions`` gains nullable
   ``deleted_at`` / ``deleted_by``. Guardrail eval definitions are
   SOFT-deleted (stamped) on ``DELETE`` so snapshot pins referencing them take
   the skipped-with-audit path; a second admin step (``?purge=true``)
   hard-removes soft-deleted rows. The live binding seam excludes soft-deleted
   rows.

3. **Break-glass invariant (per-scope + org-global)** — no schema change; the
   deny markers are added to the guardrail admin routes and the org kill-switch
   endpoints in the route layer.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0116_guardrail_trust_pr_b"
down_revision: str | None = "0115_notification_preferences"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pipeline_snapshots",
        sa.Column("guardrail_pins_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column("eval_definitions", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("eval_definitions", sa.Column("deleted_by", sa.Uuid(), nullable=True))
    op.create_index("ix_eval_definitions_deleted_at", "eval_definitions", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_eval_definitions_deleted_at", table_name="eval_definitions")
    op.drop_column("eval_definitions", "deleted_by")
    op.drop_column("eval_definitions", "deleted_at")
    op.drop_column("pipeline_snapshots", "guardrail_pins_fingerprint")
