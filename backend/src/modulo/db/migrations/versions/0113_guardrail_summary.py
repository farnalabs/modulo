"""Guardrail summary telemetry column on runs (FAR-223 item 11).

Revision ID: 0113_guardrail_summary
Revises: 0112_feedback_correction_state
Create Date: 2026-08-16

FAR-223 PR B adds ``runs.guardrail_summary_json`` (JSON NULL) — a
point-in-time snapshot of the guardrail interception written at create_run
when guardrails ran, so run detail can read the summary without re-deriving
it from eval_results + audit events. Shape: {bound, evaluated, passed,
violated, observed, errored, redacted, skipped, expected_skips,
unexpected_skips}. Generic ``sa.JSON`` keeps SQLite/MariaDB parity (the
``run_classification`` precedent, migration 0100).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0113_guardrail_summary"
down_revision: str | None = "0112_feedback_correction_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("guardrail_summary_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "guardrail_summary_json")
