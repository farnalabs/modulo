"""Add pipelines.retry_policy.

Revision ID: 0081_pipeline_retry_policy
Revises: 0080_add_mobile_sidebar_rail_flag
Create Date: 2026-08-10

Adds the pipeline-level ``retry_policy`` JSON column: ``{"on": ["stall" |
"timeout" | "failure"], "max_retries": N}``. When a run ends in a configured
terminal state and the attempt budget remains, the executor resets the run to
``pending`` and re-raises so SAQ re-dispatches it as a new attempt. An empty
``{}`` (the default) means no retry policy — current behaviour unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0081_pipeline_retry_policy"
down_revision: str | None = "0080_add_mobile_sidebar_rail_flag"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pipelines",
        sa.Column("retry_policy", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    op.drop_column("pipelines", "retry_policy")
