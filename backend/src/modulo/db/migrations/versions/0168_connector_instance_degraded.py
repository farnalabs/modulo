"""connector_instances — degraded-skip marker columns (FAR-495).

Revision ID: 0168_connector_instance_degraded
Revises: 0167_add_hot_query_indexes
Create Date: 2026-08-30

When a connector instance's stored credentials are missing or malformed,
``ConnectorHub.initialise()`` skips it with a warning so one broken
connector does not block a run. The skip was previously invisible to the
data model: operators could not tell which connector instances were
degraded without trawling logs.

Two nullable columns record the skip on the ``connector_instances`` row:

* ``degraded_at`` — when the hub last skipped this instance during
  initialisation (TIMESTAMPTZ, no default; only written by
  ``mark_instances_degraded``).
* ``last_skip_error`` — a short ``"{ExcType}: {message}"`` summary of why
  initialisation was skipped (NUL-stripped and truncated to 2000 chars by
  the hub, matching the sibling ``last_health_check_error`` column).

Storing new credentials via the connector update endpoint clears both, and
the run executor clears them for instances that initialise successfully, so
the marker reflects the *current* connector state.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0168_connector_instance_degraded"
down_revision: str | None = "0167_add_hot_query_indexes"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column("connector_instances", sa.Column("degraded_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("connector_instances", sa.Column("last_skip_error", sa.String(length=2000), nullable=True))


def downgrade() -> None:
    op.drop_column("connector_instances", "last_skip_error")
    op.drop_column("connector_instances", "degraded_at")
