"""Index trigger_events.received_at for age-based retention (FAR-167)

Revision ID: 0092_trigger_events_retention
Revises: 0091_run_evidence
Create Date: 2026-08-13

FAR-167 adds an age-based retention job for ``trigger_events`` (the table had
no cleanup of its own and grew without bound). The cleanup sweeps
``SELECT id FROM trigger_events WHERE received_at < cutoff ORDER BY id
LIMIT 500``, so ``received_at`` needs a supporting index to keep the sweep
bounded.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0092_trigger_events_retention"
down_revision: str | None = "0091_run_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_trigger_events_received_at", "trigger_events", ["received_at"])


def downgrade() -> None:
    op.drop_index("ix_trigger_events_received_at", table_name="trigger_events")
